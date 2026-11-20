#!/usr/bin/env python3
"""DingAgentX CLI.

  python cli.py qr
  python cli.py listen
  python cli.py send <conversation_id> <text>
  python cli.py search [org_id]
  python cli.py api <uri> [hex_or_json_body]
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dingtalkx import consts
from dingtalkx.api import DingTalkApi
from dingtalkx.auth import QrLoginFlow, _match_lwpqr_event
from dingtalkx.lwp.client import LwpClient
from dingtalkx.lwp.wire import RPC_URIS
from dingtalkx.session import bind_session, load_login, save_login

CRED_DIR = os.path.expanduser("~/.dingtalkx")


async def _open():
    creds = load_login()
    if not creds or not creds.get("access_token"):
        print("没有凭证. 先运行: python cli.py qr")
        return None, None
    client = LwpClient()
    client.device_id = consts.DID
    info = await client.connect()
    print("hello", info.get("code"))
    api, reg = await bind_session(client, creds["access_token"])
    print("/reg", reg.status, reg.headers.get("reg-uid", ""))
    if reg.status != 200:
        print("会话注册失败", reg.body[:80])
        await client.close()
        return None, None
    api.nick = creds.get("nick") or ""
    return client, api


async def cmd_qr():
    client = LwpClient()
    client.device_id = consts.DID
    info = await client.connect()
    print("hello", info.get("code"), "sid", info.get("session_id", "")[:24])
    flow = QrLoginFlow(client)
    url = await flow.prepare()
    print(url)
    try:
        import qrcode
        qrcode.QRCode(border=1).add_data(url)
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.print_ascii(invert=True)
    except Exception as e:
        print("二维码打印跳过", type(e).__name__)
    print("用手机钉钉扫上面的码, 确认后继续")
    await client.wait_push_event(lambda topic, body: _match_lwpqr_event(body, "scan"), timeout=180)
    print("已扫码, 等手机确认")
    await client.wait_push_event(lambda topic, body: _match_lwpqr_event(body, "login"), timeout=180)
    creds = await flow.confirm()
    path = save_login(creds, flow.raw_credentials)
    print("登录", creds.uid, creds.nick, "凭证", path)
    api, reg = await bind_session(client, creds.access_token)
    print("/reg", reg.status, reg.headers.get("reg-uid", ""), reg.body[:60])
    await client.close()


async def cmd_listen():
    client, api = await _open()
    if not api:
        return
    api.on_message(lambda m: print("[msg]", json.dumps(m, ensure_ascii=False, default=str)[:400]))
    await api.start_listening()
    await client.heartbeat_loop()


async def cmd_send(cid: str, text: str):
    client, api = await _open()
    if not api:
        return
    result = await api.send_text(cid, text)
    print(result["status"], "body_len", result["len"])
    await client.close()


async def cmd_search(org_id: str):
    client, api = await _open()
    if not api:
        return
    api.org_id = org_id
    out = await api.search(org_id)
    for name, resp in out.items():
        print(name, resp.status, len(resp.body))
    await client.close()


async def cmd_api(uri: str, body_text: str):
    if uri not in RPC_URIS and uri.lstrip() not in RPC_URIS:
        print("接口不在 9.0.8 发送目录里:", uri)
        return
    body = body_text.encode() if body_text else b""
    client = LwpClient()
    await client.connect()
    client.header_mode = "session"
    api = DingTalkApi(client)
    resp = await api.call(uri, body)
    print(resp.status, len(resp.body))
    await client.close()


async def main():
    if len(sys.argv) < 2:
        print(__doc__)
        print("已登记接口", len(RPC_URIS))
        return
    cmd = sys.argv[1]
    if cmd == "qr":
        await cmd_qr()
    elif cmd == "listen":
        await cmd_listen()
    elif cmd == "send" and len(sys.argv) >= 4:
        await cmd_send(sys.argv[2], " ".join(sys.argv[3:]))
    elif cmd == "search":
        await cmd_search(sys.argv[2] if len(sys.argv) > 2 else consts.ORG_ID)
    elif cmd == "api" and len(sys.argv) >= 3:
        await cmd_api(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "")
    else:
        print(__doc__)


if __name__ == "__main__":
    asyncio.run(main())
