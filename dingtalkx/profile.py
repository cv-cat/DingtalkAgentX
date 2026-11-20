"""钉钉资料接口 — 登录后从服务器取: 账号姓名/头像/手机, 任意 uid 的资料, 头像下载.

全部走 LWP warm 请求 (client 已登录):
  getMyUserProfileExtension  空 body → 全量资料
  getUserProfileByUids       [int uid] → {1:[{...user}]}
头像: mediaId @lALP... → CSpace HTTP 网关 (space.dingtalk.com) + dt_s cookie.
"""
from __future__ import annotations

import os
import time

import requests

PROFILE_TTL = 6 * 3600
AVATAR_TTL = 7 * 86400
AVATAR_MAX = 2 * 1024 * 1024

# 头像/文件域名白名单 (钉钉自家 CDN)
ALLOWED_SUFFIXES = ('dingtalk.com', 'alicdn.com', 'aliyuncs.com', 'dingtalkapps.com')
_EXT = {'image/png': '.png', 'image/jpeg': '.jpg', 'image/webp': '.webp', 'image/gif': '.gif'}
_CTYPE = {v: k for k, v in _EXT.items()}


def _s(v):
    if isinstance(v, (bytes, bytearray)):
        return v.decode('utf-8', 'replace')
    return v if isinstance(v, str) else ''


def _as_dict(o):
    return o if isinstance(o, dict) else {}


# ---------- LWP 资料接口 ----------

async def fetch_my_profile(client) -> dict:
    """client: 已 /reg 的 LwpClient. 返回 {name, avatar_media, mobile, real_name,
    corp_id, org_name, org_id_list, job_number, fetched_at}. (async, await 调用)"""
    r = await client.request("/r/Adaptor/UserMixI/getMyUserProfileExtension", b"",
                             profile="warm", timeout=12)
    if r.status != 200:
        return {'fetched_at': time.time()}
    from .listener import decode_body
    o = _as_dict(decode_body(r.body))
    u = _as_dict(o.get(1))
    orgs = [_as_dict(_as_dict(x.get(1))) for x in (o.get(2) or [])]
    main_org = orgs[0] if orgs else {}
    return {
        'uid': str(u.get(1) or ''),
        'name': _s(u.get(2)),
        'avatar_media': _s(u.get(5)),
        'mobile': _s(u.get(9)),
        'real_name': _s(u.get(27)) or _s(u.get(2)),
        'corp_id': _s(main_org.get(4)),
        'org_name': _s(main_org.get(5)),
        'org_ids': [str(g.get(4)) for g in orgs],
        'job_number': _s(u.get(46)),
        'fetched_at': time.time(),
    }


async def fetch_users_profile(client, uids: list) -> list[dict]:
    """getUserProfileByUids: body = pack([int(uid), ...]) → {1:[{...}]}. (async)"""
    body = pack_int_uids(uids)
    if not body:
        return []
    r = await client.request("/r/Adaptor/UserMixI/getUserProfileByUids", body,
                             profile="warm", timeout=12)
    if r.status != 200:
        return []
    from .listener import decode_body
    o = _as_dict(decode_body(r.body))
    out = []
    for x in (o.get(1) or []):
        x = _as_dict(x)
        out.append({
            'uid': str(x.get(1) or ''),
            'name': _s(x.get(2)),
            'avatar_media': _s(x.get(5)),
            'mobile': _s(x.get(9)),
        })
    return out


def pack_int_uids(uids: list) -> bytes:
    from .lwp.wire import pack
    return pack([int(u) for u in uids if str(u).isdigit()])


async def fetch_chat_info(client, cids: list, compensated: bool = True) -> list[dict]:
    """getByIds / listByIdsForCompensated: body = 裸数组 [cid, ...].

    响应: [{1:{1:cid,2:type,6:{openConversationId,member_org_ids,...},7:lastTs,...},
            2:[成员profile], 3:sync, 5:{}}]
    返回 [{cid, type, open_cid, last_ts, members:[uid...]}]."""
    from .lwp.wire import pack
    body = pack([str(c).encode() for c in cids if str(c).strip()])
    if body == b'\x90':
        return []
    uri = '/r/IDLConversation/listByIdsForCompensated' if compensated else '/r/IDLConversation/getByIds'
    r = await client.request(uri, body, profile='warm', timeout=12)
    if r.status != 200:
        return []
    from .listener import decode_body
    o = decode_body(r.body)
    if not isinstance(o, list):
        o = [o]
    out = []
    for item in o:
        item = _as_dict(item)
        conv = _as_dict(item.get(1))
        ext_raw = conv.get(6) or {}
        # str 键在 raw msgpack 里是 bytes, 统一转 str
        ext = {_s(k): v for k, v in ext_raw.items()} if isinstance(ext_raw, dict) else {}
        members = []
        for m in (item.get(2) or []):
            md = _as_dict(m)
            m1 = _as_dict(md.get(1))
            uid = m1.get(18)
            if uid:
                members.append(str(uid))
        out.append({
            'cid': _s(conv.get(1)),
            'type': conv.get(3),
            'open_cid': _s(ext.get('openConversationId')),
            'last_ts': conv.get(7),
            'members': members,
        })
    return out


async def fetch_feed_chats(client) -> list[dict]:
    """listNewestExtV3: body = 空 JSON 对象 {} (wukongidl JSON 编码).

    响应 {1:[会话], 2:[分类], 5:[分类]}: 每会话 {1:conv模型, 2:[成员], 3:sync, 5:统计}.
    conv: 1=cid, 2=类型, 3=子类型, 4=群名, 5=群头像, 6=ext(openConversationId/intUid/tagCodes),
          7=lastMsgTs, 38=未读数, 39=群名alt."""
    r = await client.request('/r/IDLConversation/listNewestExtV3', b'{}',
                             profile='warm', timeout=15)
    if r.status != 200:
        return []
    from .listener import decode_body
    o = decode_body(r.body)
    if not isinstance(o, dict):
        return []
    out = []
    for item in (o.get(1) or []):
        d = _as_dict(item)
        conv = _as_dict(d.get(1))
        ext_raw = conv.get(6) or {}
        ext = {_s(k): v for k, v in ext_raw.items()} if isinstance(ext_raw, dict) else {}
        members = []
        for m in (d.get(2) or []):
            md = _as_dict(m)
            m1 = _as_dict(md.get(1))
            uid = m1.get(18)
            if uid:
                members.append(str(uid))
        out.append({
            'cid': _s(conv.get(1)),
            'type': conv.get(2),
            'subtype': conv.get(3),
            'title': _s(conv.get(4)) or _s(conv.get(39)),
            'open_cid': _s(ext.get('openConversationId')),
            'last_ts': conv.get(7),
            'unread': conv.get(38) or 0,
            'members': members,
        })
    return out


async def fetch_nav_tabs(client, cid: str) -> dict:
    """ConvNavTabI/listTab: {1: cid} → {1:[],2:F,3:F,4:0,5:ts}. 会话页签配置."""
    from .lwp.wire import pack
    r = await client.request('/r/Adaptor/ConvNavTabI/listTab', pack({1: str(cid)}),
                             profile='warm', timeout=10)
    if r.status != 200:
        return {}
    from .listener import decode_body
    o = _as_dict(decode_body(r.body))
    return {'tabs': o.get(1) or [], 'has_more': bool(o.get(2)), 'ts': o.get(5)}


async def fetch_chat_info_unlimited(client, cid: str) -> dict:
    """getByIdUnlimited: 裸 fixstr(cid) → 44键会话模型 + 最新消息信封 (pagination 锚点).

    返回 {cid, type, title, open_cid, last_ts, anchor_mid, last_msg, members}."""
    from .api import get_by_id_unlimited_body
    r = await client.request('/r/IDLConversation/getByIdUnlimited',
                             get_by_id_unlimited_body(cid), profile='warm', timeout=10)
    if r.status != 200:
        return {}
    from .listener import decode_body
    o = _as_dict(decode_body(r.body))
    conv = _as_dict(o.get(1))
    ext_raw = conv.get(6) or {}
    ext = {_s(k): v for k, v in ext_raw.items()} if isinstance(ext_raw, dict) else {}
    anchor_mid = None
    last_msg = None
    f2 = o.get(2)
    if isinstance(f2, list) and f2:
        m0 = _as_dict(f2[0])
        inner = _as_dict(m0.get(1))
        anchor_mid = inner.get(1)
        last_msg = _parse_msg_envelope(inner)
    return {
        'cid': _s(conv.get(1)),
        'type': conv.get(2),
        'title': _s(conv.get(4)),
        'open_cid': _s(ext.get('openConversationId')),
        'last_ts': conv.get(7),
        'anchor_mid': anchor_mid,
        'last_msg': last_msg,
        'ext': ext,
    }


def _parse_msg_envelope(m: dict) -> dict:
    """pagination/getMessageByIdWithCidV2 的消息信封 {1:mid,2:cid,3:sender,6:ts,7:{1:type,2:{1:text}}}."""
    body = _as_dict(m.get(7))
    content = _as_dict(body.get(2))
    sender = _as_dict(m.get(3))
    return {
        'msg_id': m.get(1),
        'cid': _s(m.get(2)),
        'sender_uid': sender.get(1),
        'ts': m.get(6),
        'content_type': body.get(1),
        'text': _s(content.get(1)),
    }


async def fetch_members(client, cid: str, offset: int = 0, count: int = 100) -> list[dict]:
    """listMembers: (cid, offset, count) → [{uid, role, nick, join_ts}].

    role: 1=群主 2/3=成员; 分页 offset+=len(result) 直到不足 count."""
    from .api import list_members_body
    r = await client.request('/r/IDLConversation/listMembers',
                             list_members_body(cid, offset, count), profile='warm', timeout=10)
    if r.status != 200:
        return []
    from .listener import decode_body
    o = decode_body(r.body)
    arr = o if isinstance(o, list) else (o.get(1) if isinstance(o, dict) else [])
    out = []
    for m in (arr or []):
        d = _as_dict(m)
        m1 = _as_dict(d.get(1))
        m3 = _as_dict(d.get(3))
        out.append({
            'uid': str(m1.get(1) or ''),
            'role': d.get(2),
            'nick': _s(m3.get(1)),
            'join_ts': d.get(5),
        })
    return out


async def fetch_history(client, cid: str, msg_id: int | None = None,
                        count: int = 20, max_pages: int = 5) -> list[dict]:
    """listMessagesPagination 拉历史.

    msg_id=None 时先用 fetch_chat_info_unlimited 取最新消息做锚点.
    响应 {1:[msg信封], 2:next_anchor, 3:hasMore}; 每页用最旧一条的 mid 继续向前翻."""
    from .api import pagination_body
    if msg_id is None:
        info = await fetch_chat_info_unlimited(client, cid)
        msg_id = info.get('anchor_mid')
        if not msg_id:
            return []
    msgs = []
    anchor = int(msg_id)
    for _ in range(max_pages):
        r = await client.request('/r/IDLMessage/listMessagesPagination',
                                 pagination_body(cid, anchor, count),
                                 profile='warm', timeout=12)
        if r.status != 200:
            break
        from .listener import decode_body
        o = _as_dict(decode_body(r.body))
        page = o.get(1) or []
        if not page:
            break
        for m in page:
            inner = _as_dict(_as_dict(m).get(1))
            msgs.append(_parse_msg_envelope(inner))
        has_more = o.get(3)
        if not has_more:
            break
        # 下一页锚点 = 本页最旧一条
        oldest = _as_dict(_as_dict(page[-1]).get(1))
        nxt = oldest.get(1)
        if not nxt or int(nxt) == anchor:
            break
        anchor = int(nxt)
    return msgs




# ---------- 头像下载 (CSpace HTTP 网关) ----------

CSpace_HOST = 'https://space.dingtalk.com'


def avatar_candidates(media_id: str, did: str) -> list:
    """mediaId @lALP... 的候选下载地址."""
    if not media_id:
        return []
    mid = media_id.lstrip('@')
    return [
        f'{CSpace_HOST}/api/download?mediaId={media_id}',
        f'{CSpace_HOST}/v1/files/{mid}/download',
        f'https://im.dingtalk.com/api/download?mediaId={media_id}&did={did}',
    ]


def _host(url: str) -> str:
    from urllib.parse import urlparse
    return urlparse(url).hostname or ''


def _allowed(url: str) -> bool:
    host = _host(url)
    return any(host == s or host.endswith('.' + s) for s in ALLOWED_SUFFIXES)


def download_avatar(media_id: str, did: str, dt_s_cookie: str = '') -> tuple:
    """返回 (bytes, content_type);失败 (b'', ''). dt_s: CSpace session cookie."""
    headers = {'user-agent': 'Win/10.0 (Windows NT;zh_CN) App/9.0.8-Release.261105001 AliApp(DingTalk/9.0.8-Release)',
               'referer': 'https://im.dingtalk.com/', 'accept': 'image/*'}
    if dt_s_cookie:
        headers['cookie'] = 'dt_s=' + dt_s_cookie
    if did:
        headers['deviceid'] = did
    for url in avatar_candidates(media_id, did):
        if not _allowed(url):
            continue
        try:
            r = requests.get(url, headers=headers, timeout=10, stream=True, allow_redirects=False)
            if r.status_code in (301, 302, 307, 308):
                target = r.headers.get('location', '')
                if not _allowed(target):
                    continue
                r = requests.get(target, headers=headers, timeout=10, stream=True, allow_redirects=False)
            if r.status_code != 200:
                continue
            ctype = r.headers.get('content-type', '').split(';')[0].strip()
            if ctype and ctype not in _EXT:
                continue
            buf = b''
            for chunk in r.iter_content(1 << 14):
                buf += chunk
                if len(buf) > AVATAR_MAX:
                    break
            if buf:
                return buf, ctype or 'image/jpeg'
        except Exception:
            continue
    return b'', ''


# ---------- 本地缓存 (账号目录 avatars/) ----------

def cache_dir(account_id: str) -> str:
    from . import accounts
    return os.path.join(accounts.BASE, account_id, 'avatars')


def cached_file(account_id: str, name: str):
    d = cache_dir(account_id)
    for ext, ctype in _CTYPE.items():
        p = os.path.join(d, name + ext)
        if os.path.exists(p):
            return p, ctype
    return None, ''


def write_cache(account_id: str, name: str, data: bytes, ctype: str):
    d = cache_dir(account_id)
    os.makedirs(d, exist_ok=True)
    old, _ = cached_file(account_id, name)
    if old:
        os.remove(old)
    open(os.path.join(d, name + _EXT.get(ctype, '.jpg')), 'wb').write(data)


# ---------- meta 桥 ----------

def load(account_id: str) -> dict:
    from . import accounts
    return (accounts.read_meta(account_id) or {}).get('profile') or {}


def save(account_id: str, profile: dict):
    from . import accounts
    meta = accounts.read_meta(account_id) or {}
    meta['profile'] = profile
    accounts.write_meta(account_id, meta)


def stale(profile: dict) -> bool:
    return not profile or time.time() - profile.get('fetched_at', 0) > PROFILE_TTL


async def refresh(account_id: str, client) -> dict:
    """登录后刷新自己资料 → 存 meta.profile. client 缺省/未注册时读缓存."""
    from . import accounts
    try:
        prof = await fetch_my_profile(client)
        if prof.get('name'):
            save(account_id, prof)
            meta = accounts.read_meta(account_id) or {}
            if not meta.get('nick') and prof.get('name'):
                meta['nick'] = prof['name']
                accounts.write_meta(account_id, meta)
            return prof
    except Exception:
        pass
    return load(account_id)
