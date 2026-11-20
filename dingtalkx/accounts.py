"""账号注册表.

账号 = ~/.dingtalkx/accounts/<id>/ 目录:
  meta.json     {name, did, uid, nick, created}
  creds.msgpack 登录凭证
  settings.json 账号设置
id 用 uid (数字字符串).
"""
from __future__ import annotations

import json
import os
import re
import time

BASE = os.path.join(os.path.expanduser("~"), ".dingtalkx", "accounts")
_ID = re.compile(r'^\d{4,20}$')


def _p(account_id: str, *rest: str) -> str:
    return os.path.join(BASE, account_id, *rest)


def normalize(account_id: str) -> str:
    account_id = str(account_id or '').strip()
    if not _ID.match(account_id):
        raise ValueError(f'账号 id 应为纯数字 uid: {account_id!r}')
    return account_id


def exists(account_id: str) -> bool:
    try:
        return os.path.isdir(_p(normalize(account_id)))
    except ValueError:
        return False


def list_all() -> list[dict]:
    out = []
    if not os.path.isdir(BASE):
        return out
    for name in sorted(os.listdir(BASE)):
        try:
            normalize(name)
        except ValueError:
            continue
        meta = read_meta(name)
        if meta:
            out.append({'id': name, **meta})
    return out


def read_meta(account_id: str) -> dict | None:
    try:
        p = _p(normalize(account_id), 'meta.json')
        return json.load(open(p, encoding='utf-8'))
    except (ValueError, OSError, json.JSONDecodeError):
        return None


def write_meta(account_id: str, meta: dict):
    account_id = normalize(account_id)
    os.makedirs(_p(account_id), exist_ok=True)
    meta = {'id': account_id, 'created': meta.get('created') or int(time.time()), **meta}
    json.dump(meta, open(_p(account_id, 'meta.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=2)


def delete(account_id: str):
    import shutil
    account_id = normalize(account_id)
    if os.path.isdir(_p(account_id)):
        shutil.rmtree(_p(account_id), ignore_errors=True)


def display_name(account_id: str) -> str:
    meta = read_meta(account_id) or {}
    return meta.get('name') or meta.get('nick') or f'账号 {account_id}'


# ---------- 凭证 ----------

def save_creds(account_id: str, raw: bytes, did: str):
    """登录成功: 27KB 凭证 + meta 更新."""
    account_id = normalize(account_id)
    os.makedirs(_p(account_id), exist_ok=True)
    open(_p(account_id, 'creds.msgpack'), 'wb').write(raw)
    try:
        import msgpack
        obj = msgpack.unpackb(raw, raw=False, strict_map_key=False)
        write_meta(account_id, {
            'uid': str(obj.get(3) or account_id),
            'nick': obj.get(4) or '',
            'token': obj.get(10) or '',
            'token_expired': obj.get(12) or 0,
            'did': did,
        })
    except Exception:
        pass


def load_creds(account_id: str) -> bytes:
    return open(_p(normalize(account_id), 'creds.msgpack'), 'rb').read()


def token_of(account_id: str) -> str:
    return (read_meta(account_id) or {}).get('token') or ''


def did_of(account_id: str) -> str:
    return (read_meta(account_id) or {}).get('did') or ''


# ---------- 设置 ----------

def get_settings(account_id: str) -> dict:
    try:
        return json.load(open(_p(normalize(account_id), 'settings.json'), encoding='utf-8'))
    except (ValueError, OSError, json.JSONDecodeError):
        return {}


def set_settings(account_id: str, settings: dict):
    account_id = normalize(account_id)
    os.makedirs(_p(account_id), exist_ok=True)
    json.dump(settings, open(_p(account_id, 'settings.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=2)
