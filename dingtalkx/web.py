"""DingAgentX Web 后端.

Manager: 每账号一个 DingListener 任务 + 一个扫码登录流程 + 环形日志.
安全模型: 默认只听 127.0.0.1.
端点:
  GET  /api/meta                    版本/后端列表
  GET  /api/runtimes                agent CLI 安装情况
  GET  /api/accounts                账号列表
  POST /api/accounts/add            发起扫码 (返回 {account_id, state})
  GET  /api/accounts/{id}/login/status  扫码进度 (含 qr png base64)
  DELETE /api/accounts/{id}/login   取消扫码
  GET/POST /api/accounts/{id}/start|stop|restart
  GET  /api/accounts/{id}           详情 (状态/设置/身份)
  GET  /api/accounts/{id}/avatars/user/{uid}   头像 (缓存+下载)
  GET  /api/accounts/{id}/chats     会话列表
  GET  /api/accounts/{id}/chats/{cid}/messages?before=cursor
  POST /api/accounts/{id}/chats/{cid}/send  {text}
  POST /api/accounts/{id}/chats/{cid}/reset  清 agent 会话
  GET/PUT /api/accounts/{id}/settings
  GET  /api/accounts/{id}/logs?since=seq
  GET  /api/accounts/{id}/overview  近7天收发统计
  GET  /api/accounts/{id}/memory    长期记忆
  POST/PUT/DELETE /api/accounts/{id}/memory[/{n}]
  GET  /api/accounts/{id}/search?q= 真实搜索 (getUserProfileByUids+消息流水)
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import time
from collections import deque

from aiohttp import web

from . import accounts, consts, memory as memory_mod, profile as profile_mod
from .agent import BACKENDS, runtimes
from .listener import DingListener, DATA_DIR, MSG_LOG, REG_FRAME

try:
    import qrcode
except ImportError:
    qrcode = None


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


class LogBuffer:
    """按账号分桶的环形日志 (deque.append 线程安全)."""
    MAX = 400

    def __init__(self):
        self.buckets: dict[str, deque] = {}
        self.seq = 0

    def add(self, account_id: str, text: str, level: str = 'INFO'):
        self.seq += 1
        self.buckets.setdefault(account_id or '_system',
                                deque(maxlen=self.MAX)).append({
            'seq': self.seq, 'time': time.strftime('%H:%M:%S'),
            'level': level, 'text': text,
        })

    def get(self, account_id: str, since: int = 0) -> list:
        return [e for e in self.buckets.get(account_id, ())
                if e['seq'] > since]

    def drop(self, account_id: str):
        self.buckets.pop(account_id, None)


class LoginFlow:
    """一条进行中的扫码登录."""
    def __init__(self, manager: 'Manager'):
        self.mgr = manager
        self.state = 'pending'      # pending|scanned|success|failed|cancelled
        self.message = ''
        self.qr_b64 = ''
        self.did = ''
        self.account_id = ''
        self._task: asyncio.Task | None = None
        self._stop = False

    def snapshot(self) -> dict:
        return {'account_id': self.account_id, 'state': self.state,
                'message': self.message, 'qr': self.qr_b64}

    async def run(self):
        """完整链: 随机 did → QR → 等扫 → 凭证 → 落账号目录."""
        import secrets
        from .lwp.client import LwpClient
        from .auth import (QrLoginFlow, _decode_token, _creds_from_any,
                           qr_token_body, qr_login_body)
        c = LwpClient()
        self.did = ''.join(secrets.choice('0123456789abcdef') for _ in range(32))
        c.device_id = self.did
        tag = 'login:' + self.did[:8]
        hb = None
        try:
            info = await c.connect()
            self.mgr.logs.add(self.account_id or tag, f'lws 握手 code={info.get("code")}')
            hb = asyncio.create_task(c.heartbeat_loop(42))

            def on_any(topic, body):
                from .auth import _match_lwpqr_event
                if _match_lwpqr_event(body, 'scan') and self.state == 'pending':
                    self.state, self.message = 'scanned', '已扫码, 请在手机上确认'
            c.on_any_push = on_any

            await c.request(consts.URI_REG_INIT, b"", profile='reg_init_short', timeout=12)
            r = await c.request(consts.URI_QR_GENERATE,
                                qr_token_body(consts.DEVICE_TICKET),
                                profile='login', timeout=15)
            token = _decode_token(r.body)
            if not token.startswith('lwpqr_'):
                self.state, self.message = 'failed', f'票据被拒: {r.body[:60]!r}'
                return
            self.mgr.logs.add(self.account_id or tag, f'QR token {token[:20]}..')

            url = consts.QR_LOGIN_URL + token
            if qrcode:
                buf = io.BytesIO()
                qrcode.make(url).save(buf)
                self.qr_b64 = 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode()

            flow = QrLoginFlow.__new__(QrLoginFlow)
            flow.client = c
            flow.qr_token = token
            flow.device_ticket = consts.DEVICE_TICKET
            flow.fingerprint = consts.DEVICE_FP
            flow.mac = consts.DEVICE_MAC
            flow.history_uids = list(consts.HISTORY_UIDS)
            flow.raw_credentials = b''
            body = qr_login_body(token, flow.device_ticket, flow.fingerprint,
                                 flow.mac, flow.history_uids)

            for i in range(90):   # 12 分钟
                if self._stop:
                    self.state = 'cancelled'
                    return
                await asyncio.sleep(8)
                try:
                    r2 = await c.request(consts.URI_QR_LOGIN_CONFIRM, body,
                                         profile='login', timeout=10)
                except Exception:
                    continue
                if r2.status == 200 and len(r2.body) > 1000:
                    raw = r2.body
                    if raw[:2] == b'\x1f\x8b':
                        import zlib
                        raw = zlib.decompressobj(31).decompress(raw)
                    creds = _creds_from_any(raw)
                    if creds is None:
                        continue
                    uid = str(creds.uid or '')
                    accounts.save_creds(uid, raw, self.did)
                    self.account_id = uid
                    self.state, self.message = 'success', f'登录成功: {creds.nick}'
                    self.mgr.logs.add(uid, f'登录成功 uid={uid} nick={creds.nick}')
                    return
        except Exception as e:
            self.state, self.message = 'failed', f'{type(e).__name__}: {e}'
        finally:
            if hb:
                hb.cancel()
            try:
                await c.close()
            except Exception:
                pass

    def cancel(self):
        self._stop = True
        if self._task and not self._task.done():
            self._task.cancel()


class Manager:
    def __init__(self):
        self.listeners: dict[str, DingListener] = {}
        self.logins: dict[str, LoginFlow] = {}
        self.logs = LogBuffer()

    # ---------- 账号 ----------
    def require(self, account_id: str) -> str:
        try:
            account_id = accounts.normalize(account_id)
        except ValueError as e:
            raise NotFound(str(e))
        if not accounts.exists(account_id):
            raise NotFound(f'账号不存在: {account_id}')
        return account_id

    def meta(self) -> dict:
        return {'name': 'DingAgentX', 'version': '0.2.0', 'platform': 'dingtalk',
                'backends': [{'id': k, 'label': v['label'],
                              'installed': (not v['binary']) or bool(__import__('shutil').which(v['binary']))}
                             for k, v in BACKENDS.items()],
                'server_time': time.time()}

    def list(self) -> list[dict]:
        return [self.describe_row(a) for a in accounts.list_all()]

    def describe_row(self, a: dict) -> dict:
        uid = a['id']
        lis = self.listeners.get(uid)
        if lis is not None:
            state, error = lis.state, lis.error
        elif not accounts.token_of(uid):
            state, error = 'no_login', ''
        else:
            state, error = 'stopped', ''
        prof = (a.get('profile') or {})
        settings = accounts.get_settings(uid)
        return {'id': uid,
                'name': a.get('nick') or prof.get('name') or uid,
                'state': state, 'error': error,
                'company': prof.get('org_name') or '',
                'avatar': f'/api/accounts/{uid}/avatars/user/{uid}' if prof.get('avatar_media') else '',
                'backend': settings.get('agent_backend') or 'none',
                'memory': bool(settings.get('memory_enabled')),
                'queue': lis.stats() if lis else {'busy': 0, 'queued': 0},
                'created': a.get('created')}

    def describe(self, account_id: str) -> dict:
        account_id = self.require(account_id)
        meta = accounts.read_meta(account_id) or {}
        lis = self.listeners.get(account_id)
        login = self.logins.get(account_id)
        if lis is not None:
            state, error = lis.state, lis.error
        elif login is not None:
            state, error = 'login_flow', ''
        elif not accounts.token_of(account_id):
            state, error = 'no_login', ''
        else:
            state, error = 'stopped', ''
        prof = meta.get('profile') or {}
        return {'id': account_id,
                'name': meta.get('nick') or prof.get('name') or account_id,
                'uid': meta.get('uid'), 'nick': meta.get('nick'),
                'did': meta.get('did'),
                'avatar': f'/api/accounts/{account_id}/avatars/user/{account_id}' if prof.get('avatar_media') else '',
                'company': prof.get('org_name') or '',
                'mobile': prof.get('mobile') or '',
                'token_expired': meta.get('token_expired'),
                'state': state, 'error': error,
                'queue': lis.stats() if lis else {'busy': 0, 'queued': 0},
                'settings': accounts.get_settings(account_id)}

    # ---------- 生命周期 ----------
    def _make_listener(self, account_id: str) -> DingListener:
        meta = accounts.read_meta(account_id) or {}
        token = accounts.token_of(account_id)
        did = accounts.did_of(account_id) or meta.get('did')
        prof = meta.get('profile') or {}
        lis = DingListener(token=token, did=did, uid=account_id,
                           nick=meta.get('nick') or prof.get('name') or account_id,
                           state_file=os.path.join(accounts.BASE, account_id, 'rules.json'),
                           cfg=accounts.get_settings(account_id), agent=True)
        lis.log_fn = lambda text, _id=account_id: self.logs.add(_id, text)
        return lis

    async def start(self, account_id: str) -> dict:
        account_id = self.require(account_id)
        lis = self.listeners.get(account_id)
        if lis is None:
            lis = self._make_listener(account_id)
            self.listeners[account_id] = lis
        if not lis.running:
            await lis.start()
        return {'id': account_id, 'state': lis.status()['state']}

    async def stop(self, account_id: str) -> dict:
        lis = self.listeners.get(self.require(account_id))
        if lis:
            await lis.stop()
            self.listeners.pop(account_id, None)
        return {'id': account_id, 'state': 'stopped'}

    async def restart(self, account_id: str) -> dict:
        await self.stop(account_id)
        return await self.start(account_id)

    async def delete(self, account_id: str):
        await self.stop(account_id)
        accounts.delete(self.require(account_id))
        self.logs.drop(account_id)

    async def autostart_all(self):
        for a in accounts.list_all():
            if accounts.get_settings(a['id']).get('autostart'):
                try:
                    await self.start(a['id'])
                except Exception as e:
                    self.logs.add(a['id'], f'autostart 失败: {e}')

    async def shutdown(self):
        for lis in list(self.listeners.values()):
            try:
                await lis.stop()
            except Exception:
                pass

    # ---------- 登录 ----------
    async def add_by_qr(self) -> dict:
        flow = LoginFlow(self)
        flow._task = asyncio.get_running_loop().create_task(flow.run())
        key = f'pending_{int(time.time()*1000)}'
        self.logins[key] = flow
        return {'account_id': key, 'state': flow.state}

    def login_snapshot(self, key: str) -> dict:
        flow = self.logins.get(key)
        if flow is None:
            raise NotFound('登录流程不存在')
        snap = flow.snapshot()
        if flow.state == 'success' and flow.account_id:
            # 转正: 账号目录已建, 摘掉 pending
            self.logins.pop(key, None)
        return snap

    def cancel_login(self, key: str):
        flow = self.logins.pop(key, None)
        if flow:
            flow.cancel()

    # ---------- 消息 ----------
    def _thread_title(self, account_id: str, chat: str) -> str:
        for r in self.storage_rows(account_id, chat=chat, limit=50):
            if r.get('direction') == 'in' and r.get('nick'):
                return r['nick']
        return chat

    def storage_rows(self, account_id: str, chat: str = '', limit: int = 200,
                     before: tuple | None = None) -> list[dict]:
        """messages.jsonl 读侧: 尾部截取 + 会话过滤 + 账号过滤(多账号共用流水)."""
        rows = []
        try:
            with open(MSG_LOG, encoding='utf-8') as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get('account') not in (account_id, None):
                        continue
                    if chat and rec.get('conversation_id') != chat:
                        continue
                    rows.append(rec)
        except OSError:
            return []
        if before:
            rows = [r for r in rows if (r.get('time') or 0, r.get('msg_id') or '') < before]
        return rows[-limit:]

    def chats(self, account_id: str) -> list[dict]:
        """从消息流水聚合会话 (最后一条 + 计数).

        运行中且 listener 在线时, 服务器侧 fetch_chat_info 补 open_cid/成员."""
        rows = self.storage_rows(account_id, limit=2000)
        by: dict[str, dict] = {}
        for r in rows:
            cid = r.get('conversation_id') or ''
            if not cid:
                continue
            ent = by.setdefault(cid, {'id': cid, 'title': r.get('nick') or cid,
                                      'last': '', 'last_time': 0, 'last_direction': '',
                                      'in': 0, 'out': 0})
            if (r.get('time') or 0) >= ent['last_time']:
                ent['last'] = r.get('text') or ''
                ent['last_time'] = r.get('time') or 0
                ent['last_direction'] = r.get('direction') or ''
                if r.get('direction') == 'in':
                    ent['title'] = r.get('nick') or ent['title']
            if r.get('direction') == 'in':
                ent['in'] += 1
            else:
                ent['out'] += 1
        return sorted(by.values(), key=lambda e: -e['last_time'])

    async def _chats_with_server(self, account_id: str) -> list[dict]:
        """本地聚合 + 服务器侧全量会话 (listNewestExtV3).

        listener 在线 → 直接用服务器会话列表 (含群名/未读/open_cid);
        掉线时回退本地消息流水聚合."""
        lis = self.listeners.get(account_id)
        if lis and getattr(lis, 'running', False) and lis.client:
            try:
                feed = await lis.fetch_feed_chats()
                if feed:
                    local = {e['id']: e for e in self.chats(account_id)}
                    out = []
                    for info in feed:
                        cid = info.get('cid') or ''
                        loc = local.get(cid) or {}
                        out.append({
                            'id': cid,
                            'title': info.get('title') or loc.get('title') or cid,
                            'last': loc.get('last') or '',
                            'last_time': loc.get('last_time') or info.get('last_ts') or 0,
                            'last_direction': loc.get('last_direction') or '',
                            'in': loc.get('in') or 0,
                            'out': loc.get('out') or 0,
                            'open_cid': info.get('open_cid') or '',
                            'unread': info.get('unread') or 0,
                            'server_last_ts': info.get('last_ts') or 0,
                            **({'members': info['members']} if info.get('members') else {}),
                        })
                    return out
            except Exception:
                pass
        return self.chats(account_id)

    def overview(self, account_id: str) -> dict:
        rows = self.storage_rows(account_id, limit=5000)
        days: dict[str, dict] = {}
        for r in rows:
            d = time.strftime('%m-%d', time.localtime((r.get('time') or 0) / 1000))
            ent = days.setdefault(d, {'date': d, 'in': 0, 'out': 0})
            ent['in' if r.get('direction') == 'in' else 'out'] += 1
        ordered = list(days.values())[-7:]
        while len(ordered) < 7:   # 补空天
            ordered.insert(0, {'date': '', 'in': 0, 'out': 0})
        recent = rows[-20:][::-1]
        counts = {'in': sum(1 for r in rows if r.get('direction') == 'in'),
                  'out': sum(1 for r in rows if r.get('direction') == 'out')}
        return {'days': ordered, 'total': len(rows),
                'counts': counts,
                'chats': self.chats(account_id), 'recent': recent}

    async def send(self, account_id: str, chat: str, text: str) -> dict:
        lis = self.listeners.get(self.require(account_id))
        if not lis or not lis.running:
            raise NotFound('监听未运行')
        return await lis.send_text(chat, text)

    # ---------- agent / 会话管理 ----------
    def chat_reset(self, account_id: str, chat: str) -> dict:
        """清掉这个会话的 agent 上下文 (claude --resume / codex thread)."""
        lis = self.listeners.get(self.require(account_id))
        if lis and lis.dispatcher:
            n = lis.dispatcher.clear_session(str(chat))
            return {'ok': True, 'cleared': n, 'chat': chat}
        # 没运行: 直接重置持久会话 id
        from .agent import get_backend
        backend = get_backend(cfg=accounts.get_settings(account_id), account_id=account_id)
        if backend.name != 'none':
            backend.reset_session(str(chat))
        return {'ok': True, 'cleared': 0, 'chat': chat}

    # ---------- 搜索 ----------
    def search(self, account_id: str, q: str) -> dict:
        """消息流水关键词搜索 + 会话标题匹配 (本地索引, 服务器搜索接口未开放)."""
        account_id = self.require(account_id)
        q = (q or '').strip()
        if not q:
            return {'q': q, 'messages': [], 'chats': []}
        rows = self.storage_rows(account_id, limit=2000)
        hits = [r for r in rows
                if q.lower() in (r.get('text') or '').lower()
                or q.lower() in (r.get('nick') or '').lower()
                or q in (r.get('conversation_id') or '')][-100:]
        chat_hits = [c for c in self.chats(account_id)
                     if q.lower() in (c.get('title') or '').lower()][:20]
        return {'q': q, 'messages': hits, 'chats': chat_hits}

    # ---------- 头像 ----------
    async def avatar_response(self, account_id: str, uid: str) -> tuple:
        """(bytes, content_type) — 缓存优先, 没有尝试拉远程."""
        self.require(account_id)
        p, ctype = profile_mod.cached_file(account_id, f'user_{uid}')
        if p:
            return open(p, 'rb').read(), ctype
        # 运行中: getUserProfileByUids → mediaId → 下载 (async, 不能阻塞 loop)
        lis = self.listeners.get(account_id)
        if lis and lis.running:
            try:
                profs = await lis.fetch_users_profile([uid])
                for prof in profs:
                    if prof.get('avatar_media') and str(prof.get('uid')) == str(uid):
                        buf, ct = await asyncio.to_thread(
                            profile_mod.download_avatar, prof['avatar_media'],
                            lis.did or '')
                        if buf:
                            profile_mod.write_cache(account_id, f'user_{uid}', buf, ct)
                            return buf, ct
            except Exception:
                pass
        raise NotFound('没有这个头像')

    # ---------- 资料刷新 ----------
    async def refresh_profile(self, account_id: str) -> dict:
        lis = self.listeners.get(self.require(account_id))
        if not lis or not lis.running:
            raise NotFound('账号未运行, 无法请求资料接口')
        await lis._refresh_profile()
        return self.describe(account_id)

    # ---------- 人员搜索/取名 ----------
    async def people(self, account_id: str, q: str = '', ids: list = None) -> list:
        """q: getUserProfileByUids 无法按名搜, 本地名单/消息流水兜底; ids: 取名字."""
        account_id = self.require(account_id)
        ids = [str(i) for i in (ids or []) if str(i).strip()][:200]
        lis = self.listeners.get(account_id)
        known = dict(getattr(lis, '_known_profiles', {}) or {})
        # 本地流水补充 uid → nick
        for r in self.storage_rows(account_id, limit=2000):
            if r.get('sender') and r.get('nick'):
                known.setdefault(str(r['sender']), {'uid': r['sender'], 'name': r['nick']})
        if q:
            q = q.strip().lower()
            out = []
            for uid, p in known.items():
                name = p.get('name') or ''
                if q in name.lower() or q in uid:
                    out.append({'id': uid, 'name': name, 'desc': p.get('org_name') or '',
                                'avatar': f'/api/accounts/{account_id}/avatars/user/{uid}'})
            # 运行中且是纯数字 → 服务器侧取一次资料 (找 uid)
            if lis and lis.running and q.isdigit():
                try:
                    profs = await lis.fetch_users_profile([q])
                    for p in profs:
                        if p.get('uid'):
                            out.append({'id': str(p['uid']), 'name': p.get('name') or '',
                                        'desc': '', 'avatar': f'/api/accounts/{account_id}/avatars/user/{p["uid"]}'})
                except Exception:
                    pass
            # 去重
            seen, uniq = set(), []
            for p in out:
                if p['id'] not in seen:
                    seen.add(p['id'])
                    uniq.append(p)
            return uniq[:20]
        # ids 模式: 运行中可以现查名字
        missing = [i for i in ids if i not in known]
        if lis and lis.running and missing:
            try:
                for p in await lis.fetch_users_profile(missing[:30]):
                    if p.get('uid'):
                        known[str(p['uid'])] = p
            except Exception:
                pass
        return [{'id': i, 'name': (known.get(i) or {}).get('name') or '',
                 'desc': (known.get(i) or {}).get('org_name') or '',
                 'avatar': f'/api/accounts/{account_id}/avatars/user/{i}'} for i in ids]

    # ---------- 每会话 agent 开关 ----------
    def set_chat_agent(self, account_id: str, chat: str, enabled: bool) -> dict:
        account_id = self.require(account_id)
        settings = accounts.get_settings(account_id)
        triggers = settings.get('triggers') or {}
        include = [str(x) for x in triggers.get('include_chats') or []]
        exclude = [str(x) for x in triggers.get('exclude_chats') or []]
        if enabled:
            if chat in exclude:
                exclude.remove(chat)
            # 原 include 名单之外的人,加进来 (名单为空=全允许)
            if include and chat not in include:
                include.append(chat)
            triggers['include_chats'], triggers['exclude_chats'] = include, exclude
        else:
            if chat not in exclude:
                exclude.append(chat)
            triggers['exclude_chats'] = exclude
        settings['triggers'] = triggers
        accounts.set_settings(account_id, settings)
        return {'ok': True, 'enabled': enabled, 'chat': chat}

    def chat_agent_enabled(self, account_id: str, chat: str) -> bool:
        settings = accounts.get_settings(account_id)
        t = settings.get('triggers') or {}
        include = [str(x) for x in t.get('include_chats') or []]
        exclude = [str(x) for x in t.get('exclude_chats') or []]
        return chat not in exclude and (not include or chat in include)

    # ---------- 定时任务 ----------
    def tasks(self, account_id: str) -> list:
        account_id = self.require(account_id)
        p = os.path.join(accounts.BASE, account_id, 'tasks.json')
        try:
            return json.load(open(p, encoding='utf-8'))
        except (OSError, ValueError):
            return []

    def remove_task(self, account_id: str, task_id: int) -> dict:
        account_id = self.require(account_id)
        p = os.path.join(accounts.BASE, account_id, 'tasks.json')
        try:
            rows = json.load(open(p, encoding='utf-8'))
        except (OSError, ValueError):
            rows = []
        rows = [r for r in rows if r.get('id') != task_id]
        json.dump(rows, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        return {'ok': True}

    # ---------- 记忆回顾 ----------
    async def memory_review(self, account_id: str) -> dict:
        account_id = self.require(account_id)
        cfg = accounts.get_settings(account_id)
        if not cfg.get('memory_enabled'):
            raise Conflict('长期记忆没有开启')
        if (cfg.get('agent_backend') or 'none') == 'none':
            raise Conflict('没有接入 Agent，无法回顾')
        lis = self.listeners.get(account_id)
        if not lis or not lis.running or not lis.dispatcher:
            raise Conflict('账号运行中才能回顾，请先启动')
        # 取最近 24h 聊天,拼 prompt 丢给 agent
        import time as _t
        cutoff = (time.time() - 86400) * 1000
        rows = [r for r in self.storage_rows(account_id, limit=5000) if (r.get('time') or 0) >= cutoff]
        if not rows:
            return {'ok': True, 'result': 'empty'}
        from .memory import REVIEW_PROMPT
        lines = [f'{_t.strftime("%m-%d %H:%M", _t.localtime((r.get("time") or 0)/1000))} '
                 f'{r.get("nick") or r.get("sender")}: {r.get("text") or "[消息]"}' for r in rows[-500:]]
        prompt = memory_mod.prompt_block(account_id) + '\n\n' + '\n'.join(lines) + '\n\n' + REVIEW_PROMPT
        out = await lis.dispatcher.backend.run('_review', prompt, chat_id='')
        if not out:
            raise Conflict('回顾没有产出 (agent 返回空)')
        meta = accounts.read_meta(account_id) or {}
        meta['memory_review_last'] = time.strftime('%m-%d %H:%M')
        accounts.write_meta(account_id, meta)
        return {'ok': True, 'result': out[:200]}

    # ---------- 记忆读侧 ----------
    def memory(self, account_id: str) -> dict:
        aid = self.require(account_id)
        items = memory_mod.entries(aid)
        last_review = (accounts.read_meta(aid) or {}).get('memory_review_last') or ''
        return {'entries': items, 'used': memory_mod.used(items),
                'limit': memory_mod.MEMORY_LIMIT, 'usage': memory_mod.usage_text(items),
                'last_review': last_review}

    # ---------- 每日统计 ----------
    def stats(self, account_id: str) -> dict:
        account_id = self.require(account_id)
        rows = self.storage_rows(account_id, limit=5000)
        days: dict[str, dict] = {}
        for r in rows:
            d = time.strftime('%m-%d', time.localtime((r.get('time') or 0) / 1000))
            ent = days.setdefault(d, {'date': d, 'in': 0, 'out': 0})
            ent['in' if r.get('direction') == 'in' else 'out'] += 1
        ordered = list(days.values())[-7:]
        return {'days': ordered,
                'total_in': sum(d['in'] for d in ordered),
                'total_out': sum(d['out'] for d in ordered)}


# ============ aiohttp 装配 ============

def _json(data, status=200):
    return web.json_response(data, status=status)


def _err(e: Exception, status=404):
    return _json({'error': str(e)}, status=status)


def create_app(manager: Manager | None = None) -> web.Application:
    mgr = manager or Manager()
    loop = asyncio.get_event_loop()

    async def on_start(app):
        await mgr.autostart_all()

    async def on_cleanup(app):
        await mgr.shutdown()

    app = web.Application()
    app.on_startup.append(on_start)
    app.on_cleanup.append(on_cleanup)
    r = web.AppRouter(app) if False else app.router

    # ---- meta / accounts ----
    async def h_meta(req):
        return _json({**mgr.meta(), 'auth_required': False})

    async def h_accounts(req):
        return _json(mgr.list())

    async def h_account_add(req):
        return _json(await mgr.add_by_qr())

    async def h_login_status(req):
        return _json(mgr.login_snapshot(req.match_info['id']))

    async def h_login_cancel(req):
        mgr.cancel_login(req.match_info['id'])
        return _json({'ok': True})

    async def h_account(req):
        return _json(mgr.describe(req.match_info['id']))

    async def h_account_delete(req):
        await mgr.delete(req.match_info['id'])
        return _json({'ok': True})

    async def h_start(req):
        return _json(await mgr.start(req.match_info['id']))

    async def h_stop(req):
        return _json(await mgr.stop(req.match_info['id']))

    async def h_restart(req):
        return _json(await mgr.restart(req.match_info['id']))

    async def h_settings_get(req):
        return _json(accounts.get_settings(mgr.require(req.match_info['id'])))

    async def h_settings_put(req):
        aid = mgr.require(req.match_info['id'])
        body = await req.json()
        old = accounts.get_settings(aid)
        merged = {**old, **body}
        accounts.set_settings(aid, merged)
        # rules 变化热应用到运行中的 listener
        lis = mgr.listeners.get(aid)
        if lis and isinstance(body.get('rules'), list):
            lis.set_rules(body['rules'])
        return _json(merged)

    async def h_logs(req):
        since = int(req.query.get('since') or 0)
        return _json({'logs': mgr.logs.get(req.match_info['id'], since)})

    async def h_chats(req):
        return _json(await mgr._chats_with_server(req.match_info['id']))

    async def h_messages(req):
        aid = req.match_info['id']
        chat = req.match_info['chat']
        limit = int(req.query.get('limit') or 200)
        return _json({'messages': mgr.storage_rows(aid, chat=chat, limit=limit),
                      'title': mgr._thread_title(aid, chat)})

    async def h_history(req):
        """服务器侧消息历史 (listMessagesPagination).

        GET /api/accounts/{id}/chats/{chat}/history?count=20&anchor=<mid>&pages=5
        无 anchor 时从最新消息向前翻. 本地流水为空 (刚登录) 也能拿到全部历史."""
        aid = mgr.require(req.match_info['id'])
        chat = req.match_info['chat']
        count = min(int(req.query.get('count') or 20), 100)
        pages = min(int(req.query.get('pages') or 5), 20)
        anchor = req.query.get('anchor')
        lis = mgr.listeners.get(aid)
        if not lis or not getattr(lis, 'running', False) or not lis.client:
            return _json({'messages': [], 'error': 'listener not running'})
        anchor_mid = int(anchor) if anchor and anchor.isdigit() else None
        msgs = await lis.fetch_history(chat, anchor_mid, count, pages)
        return _json({'messages': msgs, 'count': len(msgs)})

    async def h_members(req):
        """服务器侧群成员列表 (IDLConversation/listMembers).

        GET /api/accounts/{id}/chats/{chat}/members?offset=0&count=100"""
        aid = mgr.require(req.match_info['id'])
        chat = req.match_info['chat']
        offset = max(int(req.query.get('offset') or 0), 0)
        count = min(int(req.query.get('count') or 100), 500)
        lis = mgr.listeners.get(aid)
        if not lis or not getattr(lis, 'running', False) or not lis.client:
            return _json({'members': [], 'error': 'listener not running'})
        members = await lis.fetch_members(chat, offset, count)
        return _json({'members': members, 'count': len(members)})

    async def h_chat_info(req):
        """服务器侧单会话详情 (getByIdUnlimited): 44键模型 + 最新消息锚点."""
        aid = mgr.require(req.match_info['id'])
        chat = req.match_info['chat']
        lis = mgr.listeners.get(aid)
        if not lis or not getattr(lis, 'running', False) or not lis.client:
            return _json({'error': 'listener not running'})
        info = await lis.fetch_chat_info_unlimited(chat)
        info.pop('ext', None)
        return _json(info)

    async def h_send(req):
        body = await req.json()
        try:
            return _json(await mgr.send(req.match_info['id'],
                                        req.match_info['chat'],
                                        str(body.get('text') or '')))
        except NotFound as e:
            return _err(e, 409)

    async def h_overview(req):
        return _json(mgr.overview(req.match_info['id']))

    # ---- runtimes / avatar / profile ----
    async def h_runtimes(req):
        return _json(await asyncio.to_thread(runtimes))

    async def h_avatar(req):
        try:
            buf, ctype = await mgr.avatar_response(req.match_info['id'],
                                                   req.match_info['uid'])
            return web.Response(body=buf, content_type=ctype or 'image/jpeg',
                                headers={'cache-control': 'private, max-age=86400'})
        except NotFound as e:
            return _err(e, 404)

    async def h_profile_refresh(req):
        try:
            return _json(await mgr.refresh_profile(req.match_info['id']))
        except NotFound as e:
            return _err(e, 409)

    # ---- 会话管理 / 搜索 ----
    async def h_chat_reset(req):
        return _json(mgr.chat_reset(req.match_info['id'], req.match_info['chat']))

    async def h_search(req):
        return _json(mgr.search(req.match_info['id'], req.query.get('q') or ''))

    # ---- 记忆 ----
    async def h_memory_get(req):
        return _json(mgr.memory(mgr.require(req.match_info['id'])))

    async def h_memory_add(req):
        aid = mgr.require(req.match_info['id'])
        body = await req.json()
        try:
            memory_mod.add(aid, str(body.get('text') or ''))
        except memory_mod.MemoryEditError as e:
            return _json({'error': str(e)}, status=400)
        return _json(mgr.memory(aid))

    async def h_memory_edit(req):
        aid = mgr.require(req.match_info['id'])
        body = await req.json()
        try:
            memory_mod.edit(aid, int(req.match_info['n']),
                            str(body.get('text') or ''))
        except (memory_mod.MemoryEditError, ValueError) as e:
            return _json({'error': str(e)}, status=400)
        return _json(mgr.memory(aid))

    async def h_memory_delete(req):
        aid = mgr.require(req.match_info['id'])
        try:
            memory_mod.remove(aid, int(req.match_info['n']))
        except (memory_mod.MemoryEditError, ValueError) as e:
            return _json({'error': str(e)}, status=400)
        return _json(mgr.memory(aid))

    # ---- people / 会话 agent 开关 / 定时任务 / 回顾 / 统计 ----
    async def h_people(req):
        q = req.query.get('q') or ''
        ids = [i for i in (req.query.get('ids') or '').split(',') if i]
        return _json(await mgr.people(req.match_info['id'], q, ids))

    async def h_chat_agent(req):
        body = await req.json()
        try:
            return _json(mgr.set_chat_agent(req.match_info['id'],
                                            req.match_info['chat'],
                                            bool(body.get('enabled'))))
        except NotFound as e:
            return _err(e, 404)

    async def h_tasks(req):
        return _json(mgr.tasks(req.match_info['id']))

    async def h_task_delete(req):
        try:
            return _json(mgr.remove_task(req.match_info['id'], int(req.match_info['tid'])))
        except ValueError:
            return _json({'error': 'bad task id'}, status=400)

    async def h_memory_review(req):
        try:
            return _json(await mgr.memory_review(req.match_info['id']))
        except Conflict as e:
            return _json({'error': str(e)}, status=409)

    async def h_stats(req):
        return _json(mgr.stats(req.match_info['id']))

    r.add_get('/api/meta', h_meta)
    r.add_get('/api/runtimes', h_runtimes)
    r.add_get('/api/accounts', h_accounts)
    r.add_post('/api/accounts/add', h_account_add)
    r.add_get('/api/accounts/{id}/login/status', h_login_status)
    r.add_delete('/api/accounts/{id}/login', h_login_cancel)
    r.add_get('/api/accounts/{id}', h_account)
    r.add_delete('/api/accounts/{id}', h_account_delete)
    r.add_post('/api/accounts/{id}/start', h_start)
    r.add_post('/api/accounts/{id}/stop', h_stop)
    r.add_post('/api/accounts/{id}/restart', h_restart)
    r.add_post('/api/accounts/{id}/profile/refresh', h_profile_refresh)
    r.add_get('/api/accounts/{id}/settings', h_settings_get)
    r.add_put('/api/accounts/{id}/settings', h_settings_put)
    r.add_get('/api/accounts/{id}/logs', h_logs)
    r.add_get('/api/accounts/{id}/chats', h_chats)
    r.add_get('/api/accounts/{id}/chats/{chat}/messages', h_messages)
    r.add_get('/api/accounts/{id}/chats/{chat}/history', h_history)
    r.add_get('/api/accounts/{id}/chats/{chat}/members', h_members)
    r.add_get('/api/accounts/{id}/chats/{chat}/info', h_chat_info)
    r.add_post('/api/accounts/{id}/chats/{chat}/send', h_send)
    r.add_post('/api/accounts/{id}/chats/{chat}/reset', h_chat_reset)
    r.add_get('/api/accounts/{id}/overview', h_overview)
    r.add_get('/api/accounts/{id}/avatars/user/{uid}', h_avatar)
    r.add_get('/api/accounts/{id}/search', h_search)
    r.add_get('/api/accounts/{id}/memory', h_memory_get)
    r.add_post('/api/accounts/{id}/memory', h_memory_add)
    r.add_put('/api/accounts/{id}/memory/{n}', h_memory_edit)
    r.add_delete('/api/accounts/{id}/memory/{n}', h_memory_delete)
    r.add_post('/api/accounts/{id}/memory/review', h_memory_review)
    r.add_get('/api/accounts/{id}/people', h_people)
    r.add_put('/api/accounts/{id}/chats/{chat}/agent', h_chat_agent)
    r.add_get('/api/accounts/{id}/tasks', h_tasks)
    r.add_delete('/api/accounts/{id}/tasks/{tid}', h_task_delete)
    r.add_get('/api/accounts/{id}/stats', h_stats)

    # ---- 前端静态 ----
    static_dir = os.path.join(os.path.dirname(__file__), 'web_static')
    if os.path.isdir(static_dir):
        async def h_index(req):
            return web.FileResponse(os.path.join(static_dir, 'index.html'))
        r.add_get('/', h_index)
        r.add_static('/static', static_dir)

    return app


def run_console(port: int = 8770):
    print(f'DingAgentX console: http://127.0.0.1:{port}', flush=True)
    web.run_app(create_app(), host='127.0.0.1', port=port, print=None)


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=8770)
    run_console(ap.parse_args().port)
