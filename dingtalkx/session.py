"""登录会话: 扫码凭证落盘, 再用 /reg 把登录头绑到长链上."""
from __future__ import annotations

import json
import os

from .lwp.client import LwpClient
from .api import DingTalkApi

CRED_DIR = os.path.expanduser("~/.dingtalkx")
CRED_JSON = os.path.join(CRED_DIR, "credentials.json")
REG_FRAME = os.path.join(os.path.dirname(__file__), "reg_frame_template.bin")


def load_reg_body(path: str = REG_FRAME) -> bytes:
    data = open(path, "rb").read()
    sep = data.find(b"\n\n")
    if sep < 0:
        raise ValueError("reg template has no header/body split")
    return data[sep + 2:]


def save_login(creds, raw: bytes | None = None) -> str:
    os.makedirs(CRED_DIR, exist_ok=True)
    with open(CRED_JSON, "w", encoding="utf-8") as f:
        json.dump(creds.dump(), f, ensure_ascii=False, indent=2)
    if raw:
        open(os.path.join(CRED_DIR, "login.msgpack"), "wb").write(raw)
    return CRED_JSON


def load_login() -> dict | None:
    if not os.path.exists(CRED_JSON):
        return None
    with open(CRED_JSON, encoding="utf-8") as f:
        return json.load(f)


async def bind_session(client: LwpClient, token: str, reg_body: bytes | None = None):
    """带 oauth_k1 token 发 /reg. 头型用 reg_small, len 按 body 实长重写."""
    api = DingTalkApi(client)
    body = load_reg_body() if reg_body is None else reg_body
    resp = await api.register(token, body, profile="reg_small")
    return api, resp
