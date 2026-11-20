"""消息 → agent 分发.

流程: listener 收消息 → check(触发规则) → 会话队列(每 key 一个 worker, 串行) →
backend.run(带记忆前缀+XML消息+输出规则) → reply_fn 发回钉钉.
"""
from __future__ import annotations

import asyncio
import time
from xml.sax.saxutils import escape, quoteattr

from . import accounts, memory
from .agent import get_backend

INSTRUCTIONS = ('以上是钉钉会话里新到的一条消息(XML 里有会话/发送者/内容)。你是这个数字人,完整对话历史你都有。'
                '如果对方也是机器人且只是互相客套寒暄,直接结束不要再接话。')
OUTPUT_RULES = '不需要回复就直接结束,不要解释、不要复述消息。不要输出任何思考过程,只输出要发送的回复正文。'
SEND_HELP = ('回复直接输出正文即可(控制台会代发)。ding memory add/rm/edit 维护长期记忆;'
             'ding task add --in 30m / --at "2026-01-01 09:00" / --daily 09:00 "要做的事" 安排定时任务'
             '(到点后在当前会话里交给你处理),ding task 查看,ding task rm 编号 删除。')
STALE_SECONDS = 600
REVIEW_KEY = '_review'


def _ts(msg: dict) -> str:
    t = msg.get('time')
    if not t:
        return ''
    try:
        return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(int(t) / 1000))
    except Exception:
        return str(t)


def build_xml_message(m: dict) -> str:
    lines = ['<message>']
    lines.append(f'  <chat id={quoteattr(str(m.get("conversation_id")))} name={quoteattr(str(m.get("nick") or ""))}/>')
    lines.append(f'  <sender id={quoteattr(str(m.get("sender")))} name={quoteattr(str(m.get("nick") or ""))}/>')
    if m.get('msg_id'):
        lines.append(f'  <msg_id>{escape(str(m["msg_id"]))}</msg_id>')
    lines.append(f'  <time>{escape(_ts(m))}</time>')
    lines.append(f'  <content>{escape(str(m.get("text") or ""))}</content>')
    lines.append('</message>')
    return '\n'.join(lines)


def is_stale(msg: dict, now: float = None) -> bool:
    try:
        return (now or time.time()) * 1000 - int(msg.get('time') or 0) > STALE_SECONDS * 1000
    except (TypeError, ValueError):
        return False


class SessionWorker:
    """一个会话 key 一个队列串行处理 (agent 慢, 不能并发)."""

    def __init__(self, dispatcher: 'AgentDispatcher', session_key: str):
        self.dispatcher = dispatcher
        self.session_key = session_key
        self.queue: asyncio.Queue = asyncio.Queue()
        self.busy = False
        self.current = None
        self._interrupted = False
        self.task = asyncio.create_task(self._run())

    async def _run(self):
        while True:
            msg = await self.queue.get()
            self.busy, self._interrupted = True, False
            self.current = asyncio.ensure_future(self.dispatcher.handle_message(self.session_key, msg))
            try:
                await self.current
            except asyncio.CancelledError:
                if not self._interrupted:
                    raise       # worker 整体被取消 (停账号), 排队消息留在队列里
            except Exception as e:
                self.dispatcher.log(f'处理消息异常 [{self.session_key}]: {e}')
            self.busy, self.current = False, None

    def interrupt(self) -> bool:
        if self.current and not self.current.done():
            self._interrupted = True
            self.current.cancel()
            return True
        return False


class AgentDispatcher:
    def __init__(self, backend, reply_fn, cfg: dict = None, account_id: str = None, log_fn=None):
        self.account_id = accounts.normalize(account_id)
        self.cfg = cfg if cfg is not None else (accounts.get_settings(self.account_id) or {})
        self.backend = backend
        self.reply_fn = reply_fn
        self.log_fn = log_fn or (lambda text: None)
        self.workers: dict[str, SessionWorker] = {}
        self.memory_seen: dict[str, str] = {}

    def log(self, text: str):
        try:
            self.log_fn(text)
        except Exception:
            pass

    def session_key_of(self, msg: dict) -> str:
        scope = self.cfg.get('context_scope') or 'chat'
        if scope == 'global':
            return 'global'
        return str(msg.get('conversation_id'))

    # ---------- 触发判定 ----------
    def check(self, msg: dict, my_uid: str) -> str:
        """'ok' 交给 agent; 'denied' 名单外; 'skip' 不处理."""
        if str(msg.get('sender')) == str(my_uid):
            return 'skip'          # 自己发的 (发送回执) 不触发
        t = self.cfg.get('triggers') or {}
        content = (msg.get('text') or '').strip()
        cid = str(msg.get('conversation_id'))
        sender_id = str(msg.get('sender'))
        is_group = ':' in cid      # 钉钉 cid 含 ':' = 群, 纯数字 = 单聊
        if t.get('prefix') and not content.startswith(t['prefix']):
            return 'skip'
        if not is_group and t.get('dm_enabled', True) is False:
            return 'skip'
        if t.get('group_only', False) and not is_group:
            return 'skip'
        include = [str(x) for x in t.get('include_chats') or []]
        if include and cid not in include:
            return 'skip'
        exclude = [str(x) for x in t.get('exclude_chats') or []]
        if exclude and cid in exclude:
            return 'skip'
        blocked = [str(x) for x in t.get('exclude_senders') or []]
        if blocked and sender_id in blocked:
            return 'skip'
        keywords = t.get('keywords') or []
        if keywords and not any(kw in content for kw in keywords):
            return 'skip'
        if t.get('group_at_only') and is_group and '@' not in content:
            return 'skip'
        senders = [str(x) for x in t.get('include_senders') or []]
        if senders and sender_id not in senders:
            return 'denied'
        return 'ok'

    # ---------- 提交 ----------
    async def submit(self, msg: dict, my_uid: str = '', key: str = None):
        if not (msg.get('_task') or msg.get('_review')):
            verdict = self.check(msg, my_uid)
            if verdict != 'ok':
                if verdict == 'denied':
                    self.log(f'名单外发送者: {msg.get("sender")}')
                return verdict
        key = key or (str(msg.get('conversation_id')) if msg.get('_task') else self.session_key_of(msg))
        if key not in self.workers:
            self.workers[key] = SessionWorker(self, key)
        self.log(f'→ agent [{key[:24]}]: {(msg.get("text") or msg.get("_task") or "[回顾]")[:50]}')
        await self.workers[key].queue.put(dict(msg))
        return 'ok'

    async def enqueue_task(self, task: dict):
        """定时任务到点:当成一条消息交给创建它的那个会话。"""
        msg = {'_task': task.get('content') or '', 'conversation_id': str(task.get('chat_id') or ''),
               'time': int(time.time() * 1000), 'direction': 'in', 'nick': '', 'sender': ''}
        await self.submit(msg)

    async def enqueue_review(self, transcript: str) -> bool:
        """每日回顾:独立会话,不和聊天混。上一次还没结束时返回 False。"""
        w = self.workers.get(REVIEW_KEY)
        if w and (w.busy or not w.queue.empty()):
            return False
        self.backend.reset_session(REVIEW_KEY)
        if REVIEW_KEY not in self.workers:
            self.workers[REVIEW_KEY] = SessionWorker(self, REVIEW_KEY)
        await self.workers[REVIEW_KEY].queue.put({'_review': transcript, 'conversation_id': '',
                                                  'time': int(time.time() * 1000), 'direction': 'in',
                                                  'nick': '', 'sender': ''})
        return True

    def stats(self) -> dict:
        ws = list(self.workers.values())
        return {'busy': sum(1 for w in ws if w.busy), 'queued': sum(w.queue.qsize() for w in ws)}

    def clear_session(self, session_key: str) -> int:
        w = self.workers.get(session_key)
        n = 0
        if w:
            w.interrupt()
            while True:
                try:
                    w.queue.get_nowait()
                    n += 1
                except asyncio.QueueEmpty:
                    break
        self.backend.reset_session(session_key)
        self.memory_seen.pop(session_key, None)
        return n

    async def shutdown(self):
        workers, self.workers = list(self.workers.values()), {}
        for w in workers:
            w.task.cancel()
        await asyncio.gather(*(w.task for w in workers), return_exceptions=True)

    # ---------- prompt ----------
    def _memory_prefix(self, session_key: str) -> str:
        if not self.cfg.get('memory_enabled'):
            return ''
        block = memory.prompt_block(self.account_id)
        if self.memory_seen.get(session_key) == block:
            return ''
        self.memory_seen[session_key] = block
        return block + '\n\n'

    def build_prompt(self, session_key: str, msg: dict) -> str:
        if msg.get('_review') is not None:
            from .memory import REVIEW_PROMPT
            return (memory.prompt_block(self.account_id) + '\n\n' + REVIEW_PROMPT.format(acct=f' --account {self.account_id}')
                    + '\n\n<chats>\n' + (msg.get('_review') or '') + '\n</chats>')
        chat_id = str(msg.get('conversation_id') or '')
        if msg.get('_task') is not None:
            body = (f'<task>\n  <chat id={quoteattr(chat_id)}/>\n'
                    f'  <content>{escape(str(msg.get("_task") or ""))}</content>\n</task>')
            lead = '以上是你之前安排的定时任务,现在到点了,请执行。做完直接输出要发给这个会话的正文(会代发),不需要时输出空。'
            return body + '\n\n' + lead
        body = build_xml_message(msg)
        lead = self.cfg.get('system_prompt') or INSTRUCTIONS
        return self._memory_prefix(session_key) + body + '\n\n' + lead + '\n' + OUTPUT_RULES + '\n' + SEND_HELP

    async def handle_message(self, session_key: str, msg: dict):
        prompt = self.build_prompt(session_key, msg)
        is_review = msg.get('_review') is not None
        try:
            out = await self.backend.run(session_key, prompt, chat_id=str(msg.get('conversation_id') or ''))
        finally:
            if not self.backend.session_started(session_key):
                self.memory_seen.pop(session_key, None)
        if out:
            out = out.strip()
            self.log(f'← agent [{session_key[:24]}]: {out[:60]}')
            if is_review:
                return          # 回顾只整理记忆,不发消息
            try:
                await self.reply_fn(str(msg.get('conversation_id')), out)
            except Exception as e:
                self.log(f'回复发送失败: {e}')
