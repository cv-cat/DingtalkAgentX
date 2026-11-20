"""扫码登录.

generateForLwpQrV2: 生成 lwpqr token, 用它拼出二维码内容
qrcodeLoginForLwp: 手机确认后取回完整登录凭证 (access_token 等)
二维码内容: http://qr.dingtalk.com/action/login?code=<lwpqr_UUID>
"""
from __future__ import annotations

import json

from . import consts
from .lwp.client import LwpClient
from .lwp.wire import pack, pack_values
from .models import AuthCredentials


def history_json(uids: list[str]) -> str:
    return json.dumps(
        {"history_users": {"historyLoginUIds": uids, "localNumber": ""}},
        separators=(",", ":"),
    )


def qr_token_body(ticket: str) -> bytes:
    body = pack(ticket)
    if not body.startswith(b"\xda"):
        raise ValueError("ticket must be str16, not str8/bin")
    return body


def qr_login_body(token: str, ticket: str, fingerprint: str, mac: str,
                  uids: list[str]) -> bytes:
    """字段 11/18/20 都不能少, 顺序固定."""
    return pack_values(
        token,
        ticket,
        "Windows 10.0",
        "Windows 10.0 Native",
        {11: fingerprint, 18: [mac], 20: history_json(uids)},
    )


def qr_image_url(token: str) -> str:
    return consts.QR_LOGIN_URL + token


class QrLoginFlow:
    def __init__(self, client: LwpClient, device_ticket: str | None = None,
                 fingerprint: str | None = None, mac: str | None = None,
                 history_uids: list[str] | None = None):
        self.client = client
        self.device_ticket = device_ticket or consts.DEVICE_TICKET
        self.fingerprint = fingerprint or consts.DEVICE_FP
        self.mac = mac or consts.DEVICE_MAC
        self.history_uids = history_uids or list(consts.HISTORY_UIDS)
        self.qr_token = ""
        self.qr_url = ""
        self.raw_credentials = b""

    async def prepare(self) -> str:
        """登录页连接: /reg/init (414B 头, 无 dt) + generateForLwpQrV2."""
        c = self.client
        c.header_mode = "login"
        if len(c.device_id) != 32:
            raise ValueError("did length != 32")
        await c.request(consts.URI_REG_INIT, b"", profile="reg_init_short")
        body = qr_token_body(self.device_ticket)
        resp = await c.request(consts.URI_QR_GENERATE, body, profile="login")
        token = _decode_token(resp.body)
        if not token.startswith("lwpqr_"):
            raise RuntimeError(f"QR token rejected status={resp.status} body={resp.body[:80]!r}")
        self.qr_token = token
        self.qr_url = qr_image_url(token)
        return self.qr_url

    async def confirm(self) -> AuthCredentials:
        body = qr_login_body(
            self.qr_token, self.device_ticket, self.fingerprint, self.mac, self.history_uids,
        )
        resp = await self.client.request(consts.URI_QR_LOGIN_CONFIRM, body, profile="login")
        raw = resp.body
        if raw[:2] == b"\x1f\x8b":
            import zlib
            raw = zlib.decompressobj(31).decompress(raw)
        self.raw_credentials = raw
        creds = _creds_from_any(raw)
        if creds is None:
            raise RuntimeError(f"qrcodeLoginForLwp status={resp.status} len={len(raw)} head={raw[:40]!r}")
        if creds.access_token:
            self.client.token = creds.access_token
        return creds

    async def run(self) -> AuthCredentials:
        await self.prepare()
        await self.client.wait_push_event(
            matcher=lambda topic, body: _match_lwpqr_event(body, "scan"))
        await self.client.wait_push_event(
            matcher=lambda topic, body: _match_lwpqr_event(body, "login"))
        return await self.confirm()


def _creds_from_any(raw: bytes):
    """200 文本帧或裸 msgpack 都认; 凭证在 body 里."""
    import msgpack
    candidates = [raw]
    sep = raw.find(b"\n\n")
    if sep >= 0:
        candidates.append(raw[sep + 2:])
    for blob in candidates:
        if blob[:2] == b"\x1f\x8b":
            import zlib
            try:
                blob = zlib.decompressobj(31).decompress(blob)
            except Exception:
                continue
        try:
            obj = msgpack.unpackb(blob, raw=False, strict_map_key=False)
        except Exception:
            continue
        if isinstance(obj, dict) and (obj.get(10) or obj.get(3)):
            return AuthCredentials(obj)
    return None


def _decode_token(body: bytes) -> str:
    if len(body) > 3 and body[0] == 0xDA:
        n = int.from_bytes(body[1:3], "big")
        return body[3:3 + n].decode()
    try:
        import msgpack
        obj = msgpack.unpackb(body, raw=False, strict_map_key=False)
        return obj if isinstance(obj, str) else body.decode()
    except Exception:
        return body.decode(errors="replace")


def _as_text(v):
    if isinstance(v, (bytes, bytearray)):
        try:
            return v.decode()
        except Exception:
            return ""
    return v if isinstance(v, str) else ""


def _match_lwpqr_event(push_body: bytes, event: str) -> bool:
    import msgpack
    blobs = [push_body]
    sep = push_body.find(b"\n\n")
    if sep >= 0:
        blobs.append(push_body[sep + 2:])
    for blob in blobs:
        try:
            unpacker = msgpack.Unpacker(raw=True, strict_map_key=False)
            unpacker.feed(blob)
            for obj in unpacker:
                if _walk_event(obj, event):
                    return True
        except Exception:
            continue
    return False


def _walk_event(obj, event: str) -> bool:
    if isinstance(obj, dict):
        kind = _as_text(obj.get(1) if 1 in obj else obj.get(b"1"))
        ev = _as_text(obj.get(3) if 3 in obj else obj.get(b"3"))
        if kind == "lwpqr" and ev == event:
            return True
        return any(_walk_event(v, event) for v in obj.values())
    if isinstance(obj, list):
        return any(_walk_event(v, event) for v in obj)
    if isinstance(obj, (bytes, bytearray)) and len(obj) >= 2:
        import msgpack
        try:
            inner = msgpack.unpackb(bytes(obj), raw=True, strict_map_key=False)
        except Exception:
            return False
        if isinstance(inner, (dict, list)):
            return _walk_event(inner, event)
    return False
