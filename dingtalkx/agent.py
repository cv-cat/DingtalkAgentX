"""本地 coding agent 接入.

claude: `claude -p --dangerously-skip-permissions [--model m] --session-id|--resume <id>`
codex:  `codex exec --json --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox [-m model] -`
会话 id 按账号+会话 key 持久 (storage: 账号目录 agent_sessions.json).
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import shutil
import subprocess
import uuid

from . import accounts

BACKENDS = {
    'claude': {'label': 'Claude Code', 'binary': 'claude'},
    'codex': {'label': 'Codex', 'binary': 'codex'},
    'none': {'label': '不接入 agent(仅收发)', 'binary': ''},
}

CLAUDE_FLAGS = ['-p', '--dangerously-skip-permissions', '--model', 'sonnet']
CODEX_FLAGS = ['--json', '--skip-git-repo-check', '--dangerously-bypass-approvals-and-sandbox']

# claude 不加 --model 时读本机默认 (yun/glm-... 会被拒), 显式 sonnet 兜底;
# codex 默认模型正常, 不用兜. 用户在设置里填 agent_model 时覆盖.


def _kill_tree(proc):
    """连同子进程一起杀 (codex 是 .cmd → node, 只杀壳没用)."""
    if proc.returncode is not None:
        return
    try:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(proc.pid)],
                           capture_output=True, timeout=10)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        pass
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        pass


# ---------- 会话存储 (每账号一个 json) ----------

def _sessions_path(account_id: str) -> str:
    return os.path.join(accounts.BASE, account_id, 'agent_sessions.json')


def _load_sessions(account_id: str) -> dict:
    try:
        return json.load(open(_sessions_path(account_id), encoding='utf-8'))
    except Exception:
        return {}


def _save_sessions(account_id: str, data: dict):
    os.makedirs(os.path.dirname(_sessions_path(account_id)), exist_ok=True)
    json.dump(data, open(_sessions_path(account_id), 'w', encoding='utf-8'), ensure_ascii=False)


class AgentBackend:
    name = 'base'

    async def run(self, session_key: str, prompt: str, chat_id: str = '') -> str:
        raise NotImplementedError

    def reset_session(self, session_key: str):
        pass

    def session_started(self, session_key: str) -> bool:
        return True


class CliBackend(AgentBackend):
    binary = ''

    def __init__(self, cfg: dict = None, account_id: str = None):
        self.account_id = accounts.normalize(account_id)
        self.cfg = cfg if cfg is not None else (accounts.get_settings(self.account_id) or {})

    def _new_session_id(self, session_key: str, stable: bool) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, 'dingtalkx:' + session_key)) if stable else str(uuid.uuid4())

    def _key(self, session_key: str) -> str:
        return session_key

    def _session(self, skey: str) -> dict:
        data = _load_sessions(self.account_id)
        row = data.get(skey)
        if not row:
            sid = self._new_session_id(skey, stable=True)
            _save_sessions(self.account_id, {**data, skey: {'id': sid, 'started': False}})
            return {'id': sid, 'started': False}
        return {'id': row.get('id', ''), 'started': bool(row.get('started'))}

    def _save_session(self, skey: str, sid: str, started: bool):
        data = _load_sessions(self.account_id)
        data[skey] = {'id': sid, 'started': started}
        _save_sessions(self.account_id, data)

    def reset_session(self, session_key: str):
        skey = self._key(session_key)
        self._save_session(skey, self._new_session_id(skey, stable=False), False)

    def session_started(self, session_key: str) -> bool:
        return self._session(self._key(session_key))['started']

    def model_args(self) -> list:
        m = self.cfg.get('agent_model')
        return ['--model', m] if m and self.name == 'claude' else (['-m', m] if m else [])

    def build_args(self, sess: dict) -> list:
        raise NotImplementedError

    def parse(self, stdout: str, sess: dict) -> tuple:
        raise NotImplementedError

    async def run(self, session_key: str, prompt: str, chat_id: str = '') -> str:
        skey = self._key(session_key)
        sess = self._session(skey)
        exe = shutil.which(self.binary)
        if not exe:
            return ''
        workdir = self.cfg.get('workdir') or None
        if workdir and not os.path.isdir(workdir):
            return ''
        args = [exe] + self.build_args(sess)
        env = {**os.environ, 'DINGTALKX_AGENT': '1', 'DINGTALKX_ACCOUNT': self.account_id,
               'DINGTALKX_CHAT_ID': chat_id or ''}
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, stdin=asyncio.subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, cwd=workdir, env=env)
        except FileNotFoundError:
            return ''
        timeout = int(self.cfg.get('agent_timeout') or 0) or None
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(prompt.encode('utf-8')), timeout)
        except asyncio.TimeoutError:
            _kill_tree(proc)
            await proc.wait()
            self._abandon_first_turn(skey, sess)
            return ''
        except asyncio.CancelledError:
            _kill_tree(proc)
            self._abandon_first_turn(skey, sess)
            raise
        out = stdout.decode('utf-8', errors='replace').strip()
        if proc.returncode != 0:
            self._abandon_first_turn(skey, sess)
            return ''
        text, agent_sid = self.parse(out, sess)
        if agent_sid and ((not sess['started']) or agent_sid != sess['id']):
            self._save_session(skey, agent_sid, True)
        return text

    def _abandon_first_turn(self, skey, sess):
        if sess['started']:
            return
        try:
            self._save_session(skey, self._new_session_id(skey, stable=False), False)
        except Exception:
            pass


class ClaudeCodeBackend(CliBackend):
    name = 'claude'
    binary = 'claude'

    def build_args(self, sess: dict) -> list:
        base = list(CLAUDE_FLAGS)
        m = self.cfg.get('agent_model')
        if m:                    # 用户指定时替换兜底的 sonnet
            base = ['-p', '--dangerously-skip-permissions', '--model', m]
        return base + (['--resume', sess['id']] if sess['started'] else ['--session-id', sess['id']])

    def parse(self, stdout, sess):
        return stdout, sess['id']


class CodexBackend(CliBackend):
    name = 'codex'
    binary = 'codex'

    def _new_session_id(self, session_key, stable):
        return ''           # codex 自己分配 thread id

    def _key(self, session_key):
        return 'codex|' + session_key

    def build_args(self, sess: dict) -> list:
        if sess['started'] and sess['id']:
            return ['exec', 'resume'] + CODEX_FLAGS + self.model_args() + [sess['id'], '-']
        return ['exec'] + CODEX_FLAGS + self.model_args() + ['-']

    def parse(self, stdout, sess):
        thread_id, text = '', ''
        for line in stdout.splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            kind = ev.get('type')
            if kind == 'thread.started':
                thread_id = ev.get('thread_id') or thread_id
            elif kind == 'item.completed':
                item = ev.get('item') or {}
                if item.get('type') == 'agent_message':
                    text = item.get('text') or text
        return text, thread_id


class NullBackend(AgentBackend):
    name = 'none'

    async def run(self, session_key: str, prompt: str, chat_id: str = '') -> str:
        return ''


def get_backend(name: str = None, cfg: dict = None, account_id: str = None) -> AgentBackend:
    account_id = accounts.normalize(account_id)
    cfg = cfg if cfg is not None else (accounts.get_settings(account_id) or {})
    name = name or cfg.get('agent_backend') or 'none'
    if name == 'claude':
        return ClaudeCodeBackend(cfg=cfg, account_id=account_id)
    if name == 'codex':
        return CodexBackend(cfg=cfg, account_id=account_id)
    return NullBackend()


def runtimes() -> list:
    """本机 agent CLI 安装情况 (设置页显示)."""
    out = []
    for key, info in BACKENDS.items():
        if not info['binary']:
            out.append({'id': key, 'label': info['label'], 'installed': True, 'version': ''})
            continue
        path = shutil.which(info['binary'])
        version = ''
        if path:
            try:
                r = subprocess.run([path, '--version'], capture_output=True, timeout=8,
                                   text=True, encoding='utf-8', errors='replace')
                text = (r.stdout or r.stderr).strip()
                version = text.splitlines()[0][:80] if text else ''
            except Exception:
                version = ''
        out.append({'id': key, 'label': info['label'], 'installed': bool(path), 'version': version})
    return out
