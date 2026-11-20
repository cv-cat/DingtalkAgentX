"""监听管理 — 连接/重连/消息分发/agent 自动回复.

链路:
- /reg 带 token 注册 → 服务器自动推 /s/sync /s/synca /s/feynman
- 每条推送回 20B 裸 ack (client.py)
- 消息事件 = /s/sync /s/synca 推送里 instr 1000 blob: {1:{1:msgId,2:cid,3:{1:sender},6:ts,7:{2:{1:text}},24:nick}, 2:{1:0}, 3:{...}}
- 心跳 42s / getState + ackDiff 登录后各一次
- agent: dispatcher.py (claude/codex CLI), 规则优先, agent 兜底
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import string
import time
from collections import deque
from datetime import datetime
from typing import Callable, Optional

import msgpack
import zlib

from . import consts
from .lwp.client import LwpClient
from .lwp.wire import pack
from .api import get_state_body, ack_diff_body, update_to_view_body, send_text_body

DATA_DIR = os.path.expanduser("~/.dingtalkx")
MSG_LOG = os.path.join(DATA_DIR, "messages.jsonl")
# /reg 帧模板 (含订阅 state): 缺文件时走构造路径, token 头由账号登录态传入
REG_FRAME = os.path.join(os.path.dirname(__file__), "reg_frame_template.bin")

# 身份默认留空: 实际发送体一律用登录账号的真实 nick/avatar (profile 接口回填)
NICK = ""
AVATAR = ""
ORG_FALLBACK = ""
BACKLOG_SKIP_MS = 300_000


def send_uuid() -> str:
    al = string.ascii_letters + string.digits + "@#$"
    return "".join(random.choices(al, k=8))


def send_single_body(text: str, cid: str) -> bytes:
    """单聊变体: 7:{} 空 map, 18 无 org ext."""
    return pack({
        1: send_uuid(), 2: cid, 3: 1, 4: 1,
        5: {1: 1, 2: {1: text}, 7: []},
        6: 1, 7: {}, 8: NICK,
        18: {1: 5, 4: AVATAR},
    })


def load_reg_frame(path: str = REG_FRAME) -> tuple[str, bytes]:
    """/reg 帧模板: 提取 token 头 + body, 两者一起用."""
    b = open(path, "rb").read()
    sep = b.find(b"\n\n")
    head = b[:sep].decode("utf-8", "replace")
    token = ""
    for line in head.split("\n"):
        if line.startswith("token:"):
            token = line.split(":", 1)[1]
    return token, b[sep + 2:]


def as_str(x) -> str:
    return x.decode("utf-8", "replace") if isinstance(x, bytes) else str(x)


def decode_body(body: bytes):
    if body[:2] == b"\x1f\x8b":
        body = zlib.decompressobj(31).decompress(body)
    u = msgpack.Unpacker(raw=True, strict_map_key=False)
    u.feed(body.rstrip(b"\x0a") or body)
    try:
        return next(u)
    except StopIteration:
        return None


def extract_message_events(body: bytes) -> list[dict]:
    """/s/ 推送 body → instr 1000 消息事件列表 (ev[1] = 消息体)."""
    o = decode_body(body)
    if not isinstance(o, dict):
        return []
    inner = o.get(1)
    if not isinstance(inner, dict):
        return []
    out = []
    for it in inner.get(6) or []:
        if isinstance(it, dict) and it.get(1) == 1000:
            try:
                ev = msgpack.unpackb(it[2], raw=True, strict_map_key=False)
                if isinstance(ev, dict) and isinstance(ev.get(1), dict):
                    out.append(ev[1])
            except Exception:
                pass
    return out


def extract_push_meta(body: bytes) -> dict:
    """推送里除消息外的东西: instr id 列表 + syncKey (inner.4)."""
    o = decode_body(body)
    meta = {"instr": [], "sync_key": None}
    if isinstance(o, dict):
        inner = o.get(1)
        if isinstance(inner, dict):
            meta["sync_key"] = inner.get(4)
            for it in inner.get(6) or []:
                if isinstance(it, dict):
                    meta["instr"].append(it.get(1))
    return meta


def event_to_json(m: dict) -> dict:
    """instr 1000 消息体 → 控制台/存储用的扁平结构. 多模态: contentType 非 1 → [图片] 等标签."""
    try:
        sender = as_str(m[3][1])
    except Exception:
        sender = ""
    try:
        text = as_str(m[7][2][1])
    except Exception:
        text = ""
    ctype = 1
    try:
        ctype = m[7][1] or 1     # 1=文本, 其他=富媒体
    except Exception:
        ctype = 1
    if text and ctype not in (1, None):
        text = _MEDIA_LABELS.get(ctype, f"[类型{ctype}]") + (f" {text[:24]}" if text else "")
    try:
        ext = m[9]
        ext = {as_str(k): as_str(v) for k, v in ext.items()} if isinstance(ext, dict) else {}
    except Exception:
        ext = {}
    return {
        "msg_id": as_str(m.get(1)),
        "conversation_id": as_str(m.get(2)),
        "sender": sender,
        "content_type": ctype,
        "text": text,
        "time": int(m.get(6) or 0),
        "nick": as_str(m.get(24) or ""),
        "open_conversation_id": ext.get("openConversationId", ""),
        "direction": "in",      # 推送来的都是别人发的; 发送侧回执单独落 out
    }


# contentType → 显示标签 (推送 instr 1000 里的 7:{1:type,2:{1:...}})
_MEDIA_LABELS = {
    1: "", 2: "[图片]", 3: "[语音]", 4: "[视频]", 5: "[文件]", 6: "[链接]",
    10: "[表情]", 15: "[位置]", 17: "[语音通话]", 18: "[视频通话]", 20: "[卡片]", 24: "[合并转发]",
}


class Rule:
    def __init__(self, trigger: str, reply: str, enabled=True, rid=None):
        self.id = rid or send_uuid()
        self.trigger = trigger
        self.reply = reply
        self.enabled = enabled

    def match(self, text: str) -> bool:
        return self.enabled and text.strip() == self.trigger.strip()

    def dump(self):
        return {"id": self.id, "trigger": self.trigger, "reply": self.reply, "enabled": self.enabled}


class ListenerError(Exception):
    pass


class DingListener:
    """单账号监听: 连接生命周期 + 消息分发 + 规则自动回复 + 消息存储."""

    def __init__(self, token: Optional[str] = None, reg_body: Optional[bytes] = None,
                 nick: str = NICK, state_file: Optional[str] = None,
                 did: Optional[str] = None, uid: Optional[str] = None,
                 cfg: Optional[dict] = None, agent: bool = True):
        frame_token, frame_body = load_reg_frame()
        self.token = token or frame_token
        self.reg_body = reg_body or frame_body
        self.nick = nick
        self.avatar_media = AVATAR
        self.org_id = ORG_FALLBACK
        self.did = did or consts.DID
        self.uid = str(uid or consts.UID)
        self.account_id = self.uid
        self.cfg = cfg or {}
        self.agent_enabled = agent
        self.dispatcher = None
        self._known_profiles: dict = {}    # uid → {name, avatar_media} (getUserProfileByUids 缓存)
        self.client: Optional[LwpClient] = None
        self.state = "idle"          # idle/connecting/running/reconnecting/error
        self.error = ""
        self.started_at = 0
        self.seen_msgids: set = set()
        self.rules: list[Rule] = []
        self.handlers: list[Callable[[dict], None]] = []
        self.messages: deque = deque(maxlen=500)
        self.state_file = state_file or os.path.join(DATA_DIR, "listener_state.json")
        self._task: Optional[asyncio.Task] = None
        self._start_ms = 0
        self.log_fn: Optional[Callable[[str], None]] = None   # web 控制台日志桥
        self._load_rules()

    def log(self, text: str):
        try:
            if self.log_fn:
                self.log_fn(text)
        except Exception:
            pass

    # ---------- 状态 ----------
    @property
    def running(self):
        return self._task is not None and not self._task.done()

    def status(self) -> dict:
        return {
            "state": self.state,
            "error": self.error,
            "uid": consts.UID,
            "nick": self.nick,
            "started_at": self.started_at,
            "rules": len([r for r in self.rules if r.enabled]),
            "messages": len(self.messages),
        }

    # ---------- 规则 ----------
    def _load_rules(self):
        try:
            data = json.load(open(self.state_file, encoding="utf-8"))
            self.rules = [Rule(**r) for r in data.get("rules", [])]
        except Exception:
            # 账号 settings.json 里的 rules (web 控制台写入)
            try:
                alt = os.path.join(os.path.dirname(self.state_file or ""), "settings.json")
                if os.path.exists(alt):
                    data = json.load(open(alt, encoding="utf-8"))
                    self.rules = [Rule(**r) for r in data.get("rules", [])]
                    return
            except Exception:
                pass
            self.rules = []

    def _save_rules(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        json.dump({"rules": [r.dump() for r in self.rules]},
                  open(self.state_file, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    def set_rules(self, rows: list[dict]):
        self.rules = [Rule(r.get("trigger", ""), r.get("reply", ""),
                           r.get("enabled", True), r.get("id")) for r in rows]
        self._save_rules()

    def on_message(self, handler: Callable[[dict], None]):
        self.handlers.append(handler)

    # ---------- 生命周期 ----------
    async def start(self):
        if self.running:
            return
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self):
        if self.dispatcher:
            try:
                await self.dispatcher.shutdown()
            except Exception:
                pass
            self.dispatcher = None
        if self._task:
            self._task.cancel()
            self._task = None
        if self.client:
            try:
                await self.client.close()
            except Exception:
                pass
        self.state = "stopped"

    async def _run(self):
        while True:
            try:
                await self._session()
                self.state = "running"
                await asyncio.sleep(3600)   # session 正常结束 (不应发生)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.error = f"{type(e).__name__}: {e}"
                self.state = "reconnecting"
                self.log(f'连接断开: {self.error} → 5s 后重连')
                await asyncio.sleep(5)

    async def _session(self):
        self.state = "connecting"
        self.error = ""
        c = LwpClient()
        self.client = c
        c.device_id = self.did
        c.token = self.token
        info = await c.connect()
        r = await c.request("/reg", self.reg_body, profile="reg_small", timeout=12)
        if r.status != 200:
            raise ListenerError(f"/reg {r.status} {r.body[:60]!r}")
        c.header_mode = "session"
        self.state = "running"
        self.started_at = int(time.time() * 1000)
        self._start_ms = self.started_at
        self.log(f'连接成功 did={self.did[:8]}… /reg 200')
        # agent dispatcher (设置里 agent_backend != none 时挂载)
        self._setup_agent()
        # 真实资料: getMyUserProfileExtension → 姓名/头像/手机/组织 (meta.profile)
        await self._refresh_profile()
        now = int(time.time() * 1000)
        await c.request("/r/Sync/getState", get_state_body(now), profile="warm", timeout=12)
        await c.request("/r/Sync/ackDiff", ack_diff_body(now), profile="warm", timeout=12)
        hb = asyncio.get_running_loop().create_task(c.heartbeat_loop(42))
        c.on_any_push = self._on_push
        try:
            last_task_check = 0.0
            while True:
                await asyncio.sleep(2)
                if c._reader_task and c._reader_task.done():
                    raise ConnectionResetError(str(c._reader_task.exception()))
                # 定时任务扫描 (每 20s 一次, 到点交给创建会话的 agent)
                if self.dispatcher and time.time() - last_task_check >= 20:
                    last_task_check = time.time()
                    asyncio.get_running_loop().create_task(self._run_due_tasks())
        finally:
            hb.cancel()

    async def _run_due_tasks(self):
        """到点任务: 当成一条消息交回创建它的会话; 过期一次性的作废, daily 顺延。"""
        try:
            from . import tasks as tasks_mod
            now = datetime.now()
            for task in tasks_mod.list_tasks(self.account_id):
                verdict = tasks_mod.due(task, now)
                if verdict == 'expire':
                    tasks_mod.remove(self.account_id, task['id'])
                    self.log(f"定时任务 #{task['id']} 已过期超过 1 小时,不再执行: {task['content'][:40]}")
                elif verdict == 'run':
                    if task.get('kind') == 'daily':
                        tasks_mod.reschedule(self.account_id, task['id'],
                                             int(tasks_mod.next_daily(task.get('daily_at'), now).timestamp()))
                    else:
                        tasks_mod.remove(self.account_id, task['id'])
                    self.log(f"定时任务 #{task['id']} 到点: {task['content'][:40]}")
                    await self.dispatcher.enqueue_task(task)
        except Exception as e:
            self.log(f'定时任务扫描出错: {e}')

    # ---------- agent ----------
    def _setup_agent(self):
        if not self.agent_enabled:
            self.dispatcher = None
            return
        from .agent import get_backend
        from .dispatcher import AgentDispatcher
        backend_name = (self.cfg or {}).get('agent_backend') or 'none'
        # 无条件挂载: none 时 NullBackend 返回空, 定时任务/回顾链路仍在
        backend = get_backend(backend_name, cfg=self.cfg, account_id=self.account_id)

        async def reply_fn(chat_id: str, text: str):
            out = await self.reply(text, chat_id)
            if out.get('status') != 200:
                raise ListenerError(f"send {out.get('status')}")
            return out

        self.dispatcher = AgentDispatcher(backend, reply_fn, cfg=self.cfg,
                                           account_id=self.account_id, log_fn=self.log)
        self.log(f'agent 已挂载: {backend_name}' + (' (仅记录,不回复)' if backend_name == 'none' else ''))

    async def _refresh_profile(self):
        """登录后拉真实资料 → accounts meta.profile (头像/姓名/手机/组织)."""
        try:
            from . import profile as profile_mod
            prof = await profile_mod.refresh(self.account_id, self.client)
            if prof.get('name'):
                self.nick = prof['name']
                if prof.get('avatar_media'):
                    self.avatar_media = prof['avatar_media']
                if prof.get('org_ids'):
                    self.org_id = prof['org_ids'][0]
                self._known_profiles[prof.get('uid') or self.uid] = prof
                self.log(f'资料已同步: {prof["name"]} · {prof.get("org_name") or "组织未知"}')
        except Exception as e:
            self.log(f'资料同步失败(不影响收发): {e}')

    async def fetch_users_profile(self, uids: list) -> list:
        """对外暴露 getUserProfileByUids (uid 必须可转 int)."""
        from . import profile as profile_mod
        if not self.client:
            return []
        return await profile_mod.fetch_users_profile(self.client, uids)

    async def fetch_chat_info(self, cids: list, compensated: bool = True) -> list:
        """对外暴露 getByIds/listByIdsForCompensated 会话详情 (open_cid/成员/last_ts)."""
        from . import profile as profile_mod
        if not self.client:
            return []
        return await profile_mod.fetch_chat_info(self.client, cids, compensated)

    async def fetch_feed_chats(self) -> list:
        """对外暴露 listNewestExtV3 全量会话列表."""
        from . import profile as profile_mod
        if not self.client:
            return []
        return await profile_mod.fetch_feed_chats(self.client)

    async def fetch_chat_info_unlimited(self, cid: str) -> dict:
        """getByIdUnlimited 单会话详情 + 最新消息锚点 (pagination 翻页起点)."""
        from . import profile as profile_mod
        if not self.client:
            return {}
        return await profile_mod.fetch_chat_info_unlimited(self.client, cid)

    async def fetch_members(self, cid: str, offset: int = 0, count: int = 100) -> list:
        """listMembers 群成员列表."""
        from . import profile as profile_mod
        if not self.client:
            return []
        return await profile_mod.fetch_members(self.client, cid, offset, count)

    async def fetch_history(self, cid: str, msg_id: int | None = None,
                            count: int = 20, max_pages: int = 5) -> list:
        """listMessagesPagination 消息历史."""
        from . import profile as profile_mod
        if not self.client:
            return []
        return await profile_mod.fetch_history(self.client, cid, msg_id, count, max_pages)

    def user_avatar_url(self, uid: str) -> str:
        """控制台头像地址 (有 mediaId 才提供)."""
        prof = getattr(self, '_known_profiles', {}).get(str(uid))
        if prof and prof.get('avatar_media'):
            return f'/api/accounts/{self.account_id}/avatars/user/{uid}'
        return ''

    def stats(self) -> dict:
        if self.dispatcher:
            return self.dispatcher.stats()
        return {"busy": 0, "queued": 0}

    # ---------- 推送分发 ----------
    def _on_push(self, topic: str, body: bytes):
        try:
            for m in extract_message_events(body):
                msg_id = m.get(1)
                if msg_id in self.seen_msgids:
                    continue
                self.seen_msgids.add(msg_id)
                ts = m.get(6) or 0
                rec = event_to_json(m)
                rec["topic"] = topic
                if ts and ts < self._start_ms - BACKLOG_SKIP_MS:
                    rec["backlog"] = True     # 登录前旧消息: 记录但不触发规则
                self._record(rec)
                self.log(f'{"收" if rec["direction"] == "in" else "发"} {rec["nick"] or rec["sender"]}: {(rec["text"] or "[消息]")[:40]}' +
                         (' (登录前积压)' if rec.get("backlog") else ''))
                for h in self.handlers:
                    try:
                        h(rec)
                    except Exception:
                        pass
                if not rec.get("backlog"):
                    self._dispatch(rec, m)
        except Exception:
            pass

    def _record(self, rec: dict):
        rec.setdefault("account", self.uid)
        self.messages.append(rec)
        # 发送者资料补全 (异步, 不阻塞推送循环)
        try:
            loop = asyncio.get_running_loop()
            sid = rec.get("sender") or ""
            if sid and sid not in self._known_profiles and sid != self.uid:
                loop.create_task(self._enrich_profile(sid))
        except RuntimeError:
            pass
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(MSG_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass

    async def _enrich_profile(self, uid: str):
        """getUserProfileByUids → 缓存姓名/头像 (群消息显示真实昵称用)."""
        try:
            profs = await self.fetch_users_profile([uid])
            if profs:
                p = profs[0]
                if p.get('name'):
                    self._known_profiles[uid] = p
        except Exception:
            pass

    def _dispatch(self, rec: dict, m: dict):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return   # 无事件循环上下文 (理论上不会发生, listener 都在 loop 里跑)
        for rule in self.rules:
            if rule.match(rec.get("text", "")):
                self.log(f'规则命中 [{rule.trigger}] → 自动回复')
                loop.create_task(
                    self.reply(rule.reply, rec["conversation_id"], rec["msg_id"]))
                return
        # 规则没接住 → 交给 agent (dispatcher 里还有 triggers 名单过滤)
        if self.dispatcher and rec.get("direction") == "in":
            loop.create_task(self.dispatcher.submit(rec, my_uid=self.uid))

    # ---------- 动作 ----------
    async def reply(self, text: str, conversation_id: str, msg_id=None):
        if msg_id:
            try:
                await self.client.request("/r/IDLMessageStatus/updateToViewV2",
                                          update_to_view_body(int(msg_id), conversation_id),
                                          profile="warm", timeout=8)
            except Exception:
                pass
        if ":" in conversation_id:
            body = send_single_body(text, conversation_id)
        else:
            body = send_text_body(text, conversation_id, self.org_id or ORG_FALLBACK,
                                  self.nick, self.avatar_media or AVATAR)
        r = await self.client.request("/r/IDLSend/send", body, profile="warm", timeout=12)
        out = {"conversation_id": conversation_id, "text": text,
               "status": r.status, "body_len": len(body)}
        self.log(f'send → {r.status} ({len(body)}B)')
        if r.status == 200:
            self._record({"msg_id": "", "conversation_id": conversation_id, "sender": self.uid,
                          "text": text, "time": int(time.time() * 1000), "nick": self.nick,
                          "open_conversation_id": "", "direction": "out", "topic": "local"})
        return out

    async def send_text(self, conversation_id: str, text: str):
        return await self.reply(text, conversation_id)
