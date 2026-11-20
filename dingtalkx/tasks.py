"""定时任务:三种写法,到点后交给创建它的那个会话的 agent。

    --in 30m / 2h / 1d       多久之后执行一次
    --at "2026-01-01 09:00"  指定时间执行一次(也可以只写 "09:00",指今天或明天的这个时间)
    --daily 09:00            每天这个时间执行

存储: ~/.dingtalkx/accounts/<id>/tasks.json (按 id 递增)。
账号没在运行时到点了:一次性任务超过 LATE_LIMIT 作废,daily 顺延到下一次。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta

from . import accounts

LATE_LIMIT = 3600
_DURATION = re.compile(r'^\s*(\d+)\s*([smhd]|min|分钟|小时|天)\s*$')
_HHMM = re.compile(r'^([01]?\d|2[0-3]):([0-5]\d)$')
_UNITS = {'s': 1, 'm': 60, 'min': 60, '分钟': 60, 'h': 3600, '小时': 3600, 'd': 86400, '天': 86400}


def _path(account_id: str) -> str:
    return os.path.join(accounts.BASE, accounts.normalize(account_id), 'tasks.json')


def _load(account_id: str) -> list:
    try:
        return json.load(open(_path(account_id), encoding='utf-8'))
    except (OSError, ValueError):
        return []


def _save(account_id: str, rows: list):
    os.makedirs(os.path.dirname(_path(account_id)), exist_ok=True)
    tmp = _path(account_id) + '.tmp'
    open(tmp, 'w', encoding='utf-8').write(json.dumps(rows, ensure_ascii=False, indent=1))
    os.replace(tmp, _path(account_id))


def parse_in(text: str, now: datetime = None) -> datetime:
    m = _DURATION.match(text or '')
    if not m or int(m.group(1)) <= 0:
        raise ValueError(f'看不懂「{text}」,写成 30m、2h、1d 这样')
    return (now or datetime.now()) + timedelta(seconds=int(m.group(1)) * _UNITS[m.group(2)])


def parse_hhmm(text: str) -> str:
    m = _HHMM.match((text or '').strip())
    if not m:
        raise ValueError(f'看不懂「{text}」,写成 09:00 这样')
    return f'{int(m.group(1)):02d}:{m.group(2)}'


def parse_at(text: str, now: datetime = None) -> datetime:
    now = now or datetime.now()
    text = (text or '').strip()
    if _HHMM.match(text):
        h, mi = map(int, parse_hhmm(text).split(':'))
        t = now.replace(hour=h, minute=mi, second=0, microsecond=0)
        return t if t > now else t + timedelta(days=1)
    for fmt in ('%Y-%m-%d %H:%M', '%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M:%S', '%m-%d %H:%M'):
        try:
            t = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if fmt == '%m-%d %H:%M':
            t = t.replace(year=now.year)
        if t <= now:
            raise ValueError(f'{text} 已经过去了')
        return t
    raise ValueError(f'看不懂「{text}」,写成 "2026-01-01 09:00" 或 "09:00"')


def next_daily(hhmm: str, after: datetime) -> datetime:
    """after 之后第一次到 hhmm 的时间。"""
    h, mi = map(int, hhmm.split(':'))
    t = after.replace(hour=h, minute=mi, second=0, microsecond=0)
    return t if t > after else t + timedelta(days=1)


def add(account_id: str, chat_id: str, kind: str, content: str,
        run_at: int = 0, daily_at: str = '', created_by: str = 'user') -> int:
    rows = _load(account_id)
    tid = max((r.get('id', 0) for r in rows), default=0) + 1
    rows.append({'id': tid, 'chat_id': str(chat_id), 'anchor': '', 'kind': kind,
                 'run_at': int(run_at), 'daily_at': daily_at, 'content': content,
                 'created_by': created_by, 'created': int(datetime.now().timestamp())})
    rows.sort(key=lambda r: (r.get('run_at') or 0, r.get('id') or 0))
    _save(account_id, rows)
    return tid


def remove(account_id: str, task_id: int) -> bool:
    rows = _load(account_id)
    keep = [r for r in rows if r.get('id') != int(task_id)]
    if len(keep) == len(rows):
        return False
    _save(account_id, keep)
    return True


def reschedule(account_id: str, task_id: int, run_at: int):
    rows = _load(account_id)
    for r in rows:
        if r.get('id') == int(task_id):
            r['run_at'] = int(run_at)
    rows.sort(key=lambda r: (r.get('run_at') or 0, r.get('id') or 0))
    _save(account_id, rows)


def list_tasks(account_id: str) -> list:
    return _load(account_id)


def due(task: dict, now: datetime) -> str:
    """'run' 现在执行;'expire' 一次性任务过期太久,作废;'' 还没到。"""
    if task.get('run_at') and now.timestamp() >= task['run_at']:
        if task.get('kind') == 'once' and now.timestamp() - task['run_at'] > LATE_LIMIT:
            return 'expire'
        return 'run'
    return ''


def describe(task: dict) -> str:
    if task.get('kind') == 'daily':
        return f'每天 {task.get("daily_at") or "?"}'
    try:
        return datetime.fromtimestamp(task.get('run_at') or 0).strftime('%Y-%m-%d %H:%M')
    except (OSError, ValueError, OverflowError):
        return '?'
