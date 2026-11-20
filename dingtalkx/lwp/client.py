"""LWP 长链客户端 — lws 握手 + 加密收发循环.

链路结构:
1. hello 请求: [0x10][len][0x87 0x80] + TLV(01=ver,02=sni,03=客户端公钥,04/05=双nonce种子,82=vhost,84=ua)
2. 会话 KEY = X25519(客户端临时私钥, 服务器 pin 静态公钥) — 无 KDF
3. 数据记录: [0x30][len][0x80 0x00] + ChaCha20-Poly1305(ct+tag, 8B nonce, AAD=空)
4. nonce: 双计数器 (rx/tx 独立, 种子=hello TLV 04/05), 每条记录 +1 (大端 u64)
5. LWP 帧: 类HTTP文本 "LWP /r/xxx\\nmid:<hex> \\n<hdr>:<v>\\n...\\n\\n" + msgpack body
"""
from __future__ import annotations

import asyncio
import os
import struct
from dataclasses import dataclass, field
from typing import Callable, Optional

from .. import consts
from . import crypto
from .frame import LwpFrame, build_hello, parse_frame, parse_hello_resp
from .wire import encode_frame, headers_for, profile_for

# 服务器 pin 静态公钥 (lws URL pub-key 参数)
PIN_SERVER_PUB = bytes.fromhex(
    "118675f66fa3df0dbdd4418b2a2711d95593873e7f037e17515cfe64f660b512"
)

# 直连 IP 池 (绕 DNS, 与域名同源)
LWS_DIRECT_IPS = ["59.82.29.130"]


def parse_hello_tlv(pt: bytes) -> dict[int, bytes]:
    """hello resp 明文 TLV: [tag(1B)][len(2B BE)][value]..."""
    out: dict[int, bytes] = {}
    i = 0
    while i + 3 <= len(pt):
        tag = pt[i]
        ln = int.from_bytes(pt[i + 1:i + 3], 'big')
        if i + 3 + ln > len(pt):
            break
        out[tag] = pt[i + 3:i + 3 + ln]
        i += 3 + ln
    return out


@dataclass
class RpcRequest:
    uri: str
    body: bytes = b""
    headers: dict = field(default_factory=dict)
    mid: int = 0


@dataclass
class RpcResponse:
    uri: str
    mid: int = 0
    status: int = 0
    headers: dict = field(default_factory=dict)
    body: bytes = b""


PushHandler = Callable[[str, bytes], None]


class LwpClient:
    """单条 lws 长链上的 LWP 会话."""

    def __init__(self, host: str = consts.LWS_HOST, port: int = consts.LWS_PORT,
                 direct_ip: Optional[str] = None):
        self.host = host
        self.port = port
        self.direct_ip = direct_ip or (LWS_DIRECT_IPS[0] if host == consts.LWS_HOST else None)
        self._mid = 0x00510000
        self._pending: dict[int, asyncio.Future] = {}
        self._write_lock = asyncio.Lock()
        self._push_handlers: dict[str, list[PushHandler]] = {}
        self._event_waiters: list = []
        self.on_any_push = None   # debug: (topic, body) 回调, 优先于分发
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self.session_id: str = ""
        self.device_id: str = consts.DID
        self.token: str = ""
        self.corp_id: str = consts.CORP_ID
        self.fc_ver: str = consts.FC_VER
        self.set_ver: str = consts.SET_VER
        self.sync_header: str = consts.SYNC_HEADER
        self.via: str = consts.VIA
        # login: 冷心跳 407 + reg/init 414; session: 业务头只留 dt/len, 心跳 23
        self.header_mode: str = "login"
        # 加密状态
        self._priv = None
        self._key: Optional[bytes] = None
        self._tx_ctr = 0          # 发送计数器 (hello TLV 04 种子)
        self._rx_ctr = 0          # 接收计数器 (hello TLV 05 种子)
        self._rx_seed = b""
        self._tx_seed = b""
        self._hello_resp_raw: bytes = b""
        self._reader_task: Optional[asyncio.Task] = None

    # ---------- mid ----------
    def next_mid(self) -> int:
        # 随机起点 + 递增: 避免与旧会话 mid 碰撞触发服务器幂等重放 (旧密文解不开 -> CryptoError)
        if self._mid == 0x00510000 and not getattr(self, "_mid_seeded", False):
            import random as _r
            self._mid = _r.randint(0x100000, 0x7FFFF00)
            self._mid_seeded = True
        self._mid += 1
        return self._mid

    # ---------- 连接 / lws 握手 ----------
    async def connect(self):
        from cryptography.hazmat.primitives.asymmetric.x25519 import (
            X25519PrivateKey, X25519PublicKey,
        )
        target = self.direct_ip or self.host
        self._reader, self._writer = await asyncio.open_connection(target, self.port)

        # 1) 生成客户端临时 X25519 密钥对 + 双 nonce 种子
        self._priv = X25519PrivateKey.generate()
        client_pub = self._priv.public_key().public_bytes_raw()
        self._tx_seed = os.urandom(8)
        self._rx_seed = os.urandom(8)
        self._tx_ctr = int.from_bytes(self._tx_seed, "big")
        self._rx_ctr = int.from_bytes(self._rx_seed, "big")

        # 2) 发 hello ([0x10][len][0x87 0x80] + TLV)
        # TLV: 01=ver 02=sni 03=客户端公钥 04=tx种子(本端加密用) 05=rx种子(对端加密用) 82=vhost 84=ua
        hello = build_hello(client_pub, self._tx_seed, self._rx_seed,
                            sni=consts.LWS_SNI, vhost=consts.VHOST,
                            ua="win/4.3.14-dingtalk.atlas4-SNAPSHOT")  # 与客户端一致
        self._writer.write(hello)
        await self._writer.drain()

        # 3) 会话 KEY = 原始 X25519(私钥, pin 静态服务器公钥) — 无 KDF
        self._key = self._priv.exchange(X25519PublicKey.from_public_bytes(PIN_SERVER_PUB))

        # 4) 收 hello resp: [0x00][len][0x85 0x00] + AEAD(nonce=TLV05种子+1 大端) + 明文TLV
        hdr = await self._reader.readexactly(4)
        assert hdr[0] == 0x00, f"hello resp type=0x{hdr[0]:02x}"
        payload = await self._reader.readexactly(hdr[1])
        nonce = self._next_rx_nonce()           # hello resp = RX 记录 #1 (种子+1)
        pt = crypto.aead_decrypt(self._key, nonce, payload)
        fields = parse_hello_tlv(pt)
        code = int.from_bytes(fields.get(0x82, b"\x00\x00"), "big")
        if code != 200:
            raise ConnectionError(f"hello rejected code={code} fields={ {k: v[:20] for k, v in fields.items()} }")
        self.session_id = fields.get(0x83, b"").decode(errors="replace")  # k0_...

        # 5) 启动读循环 (响应/推送分发)
        self._reader_task = asyncio.create_task(self._read_loop())
        return dict(code=code, key=self._key.hex(),
                    tx_seed=self._tx_seed.hex(), rx_seed=self._rx_seed.hex(),
                    session_id=self.session_id,
                    hello_fields={k: v.hex() for k, v in fields.items()})

    async def close(self):
        if self._reader_task:
            self._reader_task.cancel()
            self._reader_task = None
        if self._writer:
            self._writer.close()
            await self._writer.wait_closed()

    # ---------- 记录层 (加密) ----------
    # nonce 映射: TX = TLV04种子+n, RX = TLV05种子+n, 独立计数
    def _next_tx_nonce(self) -> bytes:
        self._tx_ctr += 1
        return struct.pack(">Q", self._tx_ctr)

    def _next_rx_nonce(self) -> bytes:
        self._rx_ctr += 1
        return struct.pack(">Q", self._rx_ctr)

    def send_record(self, plaintext: bytes, rec_type: int = 0x30):
        wire = crypto.build_record(rec_type, plaintext, self._key, self._next_tx_nonce())
        self._writer.write(wire)

    async def recv_record(self) -> tuple[int, bytes]:
        hdr = await self._reader.readexactly(4)
        # 记录长度 = 12 位: ((type & 0x0F) << 8) | len8
        # type 高 4 位 0x3 = 数据记录族 (0x30..0x3F), 全部 AEAD 加密且消耗 nonce
        rec_type = hdr[0] & 0xF0
        plen = ((hdr[0] & 0x0F) << 8) | hdr[1]
        payload = await self._reader.readexactly(plen)
        nonce = self._next_rx_nonce()
        try:
            return rec_type, crypto.aead_decrypt(self._key, nonce, payload)
        except Exception:
            # 计数失步自愈: 大范围锚定
            base = self._rx_ctr
            for delta in (-2, -1, 1, 2, 3, 4, 5, -3, -4, 8, 16, 32, 64, 128, 256, -8, -16, -32, -64, -128, -256):
                try:
                    cand = struct.pack(">Q", (base + delta) % (1 << 64))
                    pt = crypto.aead_decrypt(self._key, cand, payload)
                    self._rx_ctr = int.from_bytes(cand, "big")
                    print(f"[heal] rx 计数重锚: {base} -> {self._rx_ctr} (delta {delta})", flush=True)
                    return rec_type, pt
                except Exception:
                    continue
            raise

    # ---------- LWP 帧 ----------
    def default_headers(self) -> dict:
        """兼容旧调用. 真正发出去的头由 headers_for 按固定顺序重排, 并强制带 did."""
        return {
            "app-key": consts.APP_KEY,
            "cache-header": consts.CACHE_LOGIN,
            "cs-idx-ver": "0",
            "did": self.device_id,
            "dt": "p",
            "tz": consts.TZ,
            "ua": consts.USER_AGENT,
            "vhost": consts.VHOST,
            "wv": consts.WV,
        }

    def build_headers(self, uri: str, body: bytes, profile: str | None = None) -> list[tuple[str, str]]:
        prof = profile or profile_for(uri, self.header_mode)
        rows = headers_for(prof, self, body)
        if prof in ("login", "hb_cold", "reg_init_short", "reg_init_full", "reg_small", "reg_corp", "reg_sub"):
            if not any(k == "did" for k, _ in rows) or len(self.device_id) != 32:
                raise ValueError("did missing or length != 32")
        if body and prof not in ("hb_cold", "hb_bare", "reg_init_short", "reg_init_full"):
            len_vals = [v for k, v in rows if k == "len"]
            if len_vals != [str(len(body))]:
                raise ValueError(f"len header {len_vals} != body {len(body)}")
        return rows

    def _encode_lwp(self, uri: str, mid: int, headers, body: bytes) -> bytes:
        if isinstance(headers, dict):
            rows = list(headers.items())
        else:
            rows = list(headers)
        return encode_frame(uri, mid, rows, body)

    async def request(self, uri: str, body: bytes = b"",
                      headers=None, timeout: float = 15.0,
                      profile: str | None = None) -> RpcResponse:
        mid = self.next_mid()
        if headers is None:
            rows = self.build_headers(uri, body, profile)
        elif isinstance(headers, dict):
            # 调用方只许覆盖值, 头集合和顺序仍走固定 profile, 避免漏 did
            rows = self.build_headers(uri, body, profile)
            overlay = dict(headers)
            overlay.pop("len", None)
            rows = [(k, overlay.get(k, v)) for k, v in rows]
            if body:
                rows = [(k, str(len(body)) if k == "len" else v) for k, v in rows]
        else:
            rows = list(headers)
        fut = asyncio.get_running_loop().create_future()
        async with self._write_lock:
            self._pending[mid] = fut
            self.send_record(self._encode_lwp(uri, mid, rows, body))
            await self._writer.drain()
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(mid, None)

    # ---------- 推送 ----------
    async def _ack_server_push(self, frame):
        """推送 ack 按连接阶段分两种:
        - 登录页 (header_mode=login): 全头 ack = '200\\nmid:<原mid> 0\\n' + hb_cold 8 头
          (QR 阶段每条 /s/syncDev 后必回)
        - 登录后会话 (header_mode=session): 20B 裸 ack '200\\nmid:<原mid> 0\\n\\n' 无任何附加头
        不 ack 会被服务器掐."""
        if not frame.mid:
            return
        if self.header_mode == "login":
            rows = headers_for("hb_cold", self, b"")
            lines = ["200", f"mid:{frame.mid} 0"] + [f"{k}:{v}" for k, v in rows]
            payload = ("\n".join(lines) + "\n\n").encode()
        else:
            payload = ("200\nmid:" + frame.mid + "\n\n").encode()
        async with self._write_lock:
            self.send_record(payload)
            await self._writer.drain()

    def on_push(self, topic: str, handler: PushHandler):
        self._push_handlers.setdefault(topic, []).append(handler)

    async def wait_push_event(self, matcher, timeout: float = 300.0):
        """等一条满足 matcher(topic, body)->bool 的推送, 返回 (topic, body)."""
        fut = asyncio.get_running_loop().create_future()
        entry = (matcher, fut)
        self._event_waiters.append(entry)
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            if entry in self._event_waiters:
                self._event_waiters.remove(entry)

    async def subscribe(self, topic: str):
        """LWP 推送随登录会话自动生效, 无显式订阅帧 — 占位."""
        return None

    async def heartbeat_loop(self, interval: float = 42.0):
        """42s 周期心跳 (与客户端一致)."""
        while True:
            try:
                await self.request(consts.HEARTBEAT_URI, timeout=10)
            except Exception:
                pass
            await asyncio.sleep(interval)

    def _collect_fragment(self, plaintext: bytes) -> bytes | None:
        """大包分片: 12 字节头 + 载荷. 头里的 u32 是拼接后的总长."""
        if plaintext.startswith(b"\x00\x00\x02\x00") and len(plaintext) >= 12:
            total = int.from_bytes(plaintext[8:12], "big")
            self._frag_buf = getattr(self, "_frag_buf", b"") + plaintext[12:]
            self._frag_need = total
            if len(self._frag_buf) < total:
                return None
            frame, self._frag_buf = self._frag_buf[:total], self._frag_buf[total:]
            return frame
        return plaintext

    # ---------- 读循环 ----------
    async def _read_loop(self):
        while True:
            rec_type, plaintext = await self.recv_record()
            dump = getattr(self, "debug_dir", None)
            if dump:
                try:
                    import os
                    os.makedirs(dump, exist_ok=True)
                    n = getattr(self, "_dbg_n", 0) + 1
                    self._dbg_n = n
                    open(os.path.join(dump, f"rx_{n:04d}_{len(plaintext)}.bin"), "wb").write(plaintext)
                except Exception:
                    pass
            if rec_type != 0x30:
                continue
            joined = self._collect_fragment(plaintext)
            if joined is None:
                continue
            frame = parse_frame(joined)
            if frame.headers.get("zip") == "true" and frame.body[:2] == b"\x1f\x8b":
                import zlib
                frame.body = zlib.decompressobj(31).decompress(frame.body)
            mid = int(frame.mid.split()[0], 16) if frame.mid else 0
            fut = self._pending.pop(mid, None)
            if fut and not fut.done():
                fut.set_result(RpcResponse(
                    uri=frame.uri, mid=mid, status=frame.status,
                    headers=frame.headers, body=frame.body,
                ))
            elif frame.uri.startswith("/s/"):
                await self._ack_server_push(frame)
                if self.on_any_push:
                    try:
                        self.on_any_push(frame.uri, frame.body)
                    except Exception:
                        pass
                for h in self._push_handlers.get(frame.uri, []):
                    h(frame.uri, frame.body)
                still = []
                for matcher, wfut in self._event_waiters:
                    try:
                        matched = matcher(frame.uri, frame.body)
                    except Exception:
                        matched = False
                    if matched and not wfut.done():
                        wfut.set_result((frame.uri, frame.body))
                    else:
                        still.append((matcher, wfut))
                self._event_waiters = still
