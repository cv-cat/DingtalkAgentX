"""LWP 帧编解码 — 文本帧格式.

响应帧明文示例:
    "200\\nmid:00510012 \\ncs-idx-ver:202107201728143780\\nserver-timestamp:...\\n\\n" + body

格式 = 类 HTTP 精简文本:
    <status>\\nmid:<hex> \\n<header>:<value>\\n...\\n\\n<body>

请求帧:
    "LWP /r/xxx\\nmid:<hex> \\n<header>:<value>\\n...\\n\\n<body>"
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class LwpFrame:
    startline: str = ""       # 请求 "LWP /r/xxx" / 响应 "200"
    uri: str = ""
    mid: str = ""             # 8 hex chars, e.g. "00510012"
    status: int = 0
    headers: dict = field(default_factory=dict)
    body: bytes = b""

    @property
    def is_response(self) -> bool:
        return self.startline and not self.startline.startswith("LWP ")

    @property
    def is_push(self) -> bool:
        return self.uri.startswith("/s/")


def encode_request(uri: str, mid: str, headers: dict, body: bytes) -> bytes:
    lines = [f"LWP {uri}", f"mid:{mid} "]
    for k, v in headers.items():
        lines.append(f"{k}:{v}")
    return ("\n".join(lines) + "\n\n").encode() + body


def parse_frame(data: bytes) -> LwpFrame:
    """解析一条解密后的 LWP 文本帧 (响应或推送)."""
    # 头部与 body 以空行分隔
    sep = data.find(b"\n\n")
    head, body = (data, b"") if sep < 0 else (data[:sep], data[sep + 2:])
    lines = head.decode(errors="replace").split("\n")
    f = LwpFrame(body=body)
    first = lines[0] if lines else ""
    if first.startswith("LWP "):
        f.startline = first
        f.uri = first[4:].strip()
    elif first.startswith("/"):
        # 推送帧: 首行直接是 uri (如 "/s/msg"), 无 "LWP " 前缀
        f.startline = first
        f.uri = first.strip()
    else:
        f.startline = first
        try:
            f.status = int(first)
        except ValueError:
            pass
    for ln in lines[1:]:
        if ":" not in ln:
            continue
        k, v = ln.split(":", 1)
        k = k.strip()
        if k == "mid":
            f.mid = v.strip()
        else:
            f.headers[k] = v.strip()
    return f


# ---- hello 帧编解码 ----
def build_hello(client_pub: bytes, nonce_a: bytes, nonce_b: bytes,
                sni: str = "dk", vhost: str = "WK",
                ua: str = "win/4.3.14-dingtalk.atlas4-SNAPSHOT") -> bytes:
    """hello 请求: [0x10][len][0x87 0x80] + TLV(tag1B, len2B BE, value).

    字段:
      01=ver(0)  02=sni  03=客户端临时公钥(32B)  04=nonce_a(8B)  05=nonce_b(8B)
      82=vhost   84=ua
    """
    def tlv(tag: int, v: bytes) -> bytes:
        return bytes([tag]) + len(v).to_bytes(2, "big") + v

    payload = b"".join([
        tlv(0x01, b"\x00"),
        tlv(0x02, sni.encode()),
        tlv(0x03, client_pub),
        tlv(0x04, nonce_a),
        tlv(0x05, nonce_b),
        tlv(0x82, vhost.encode()),
        tlv(0x84, ua.encode()),
    ])
    return bytes([0x10, len(payload) & 0xFF, 0x87, 0x80]) + payload


def parse_hello_resp(pt: bytes) -> dict[int, bytes]:
    """hello 响应明文 TLV: [tag(1B)][len(2B BE)][value]...

    字段:
      0x01(20B)=服务器会话nonce  0x80(2B)=机房"HZ"  0x81(4B)=TTL秒(86400, u32BE)
      0x82(2B)=返回码 u16BE (200=成功)  0x83(68B)=会话ID "k0_..."
    注: 线上 hello resp payload 是 AEAD 密文, 需先解密.
    """
    out: dict[int, bytes] = {}
    i = 0
    while i + 3 <= len(pt):
        tag = pt[i]
        ln = int.from_bytes(pt[i + 1:i + 3], "big")
        if i + 3 + ln > len(pt):
            break
        out[tag] = pt[i + 3:i + 3 + ln]
        i += 3 + ln
    return out
