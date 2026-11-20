"""长期记忆: 每账号一个 memory.md, § 分隔, 2000 字上限."""
from __future__ import annotations

import os

from . import accounts

MEMORY_LIMIT = 2000
SEPARATOR = '\n§\n'


class MemoryEditError(Exception):
    pass


def path_of(account_id: str) -> str:
    return os.path.join(accounts.BASE, account_id, 'memory.md')


def entries(account_id: str) -> list:
    p = path_of(account_id)
    if not os.path.exists(p):
        return []
    text = open(p, encoding='utf-8').read()
    return [e.strip() for e in text.split('§') if e.strip()]


def used(items: list) -> int:
    return sum(len(e) for e in items)


def _write(account_id: str, items: list):
    p = path_of(account_id)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + '.tmp'
    open(tmp, 'w', encoding='utf-8').write(SEPARATOR.join(items) + ('\n' if items else ''))
    os.replace(tmp, p)


def _clean(text: str) -> str:
    text = ' '.join((text or '').replace('§', ' ').split())
    if not text:
        raise MemoryEditError('内容不能为空')
    return text


def _index(items: list, n: int) -> int:
    if not 1 <= n <= len(items):
        raise MemoryEditError(f'没有第 {n} 条(共 {len(items)} 条)')
    return n - 1


def add(account_id: str, text: str) -> list:
    text = _clean(text)
    items = entries(account_id)
    if text in items:
        raise MemoryEditError('已经有这一条了')
    items = items + [text]
    if used(items) > MEMORY_LIMIT:
        raise MemoryEditError(f'记忆已用 {used(entries(account_id))}/{MEMORY_LIMIT} 字,写满前先删旧条目')
    _write(account_id, items)
    return items


def edit(account_id: str, n: int, text: str) -> list:
    items = entries(account_id)
    items[_index(items, n)] = _clean(text)
    if used(items) > MEMORY_LIMIT:
        raise MemoryEditError(f'记忆已用 {used(entries(account_id))}/{MEMORY_LIMIT} 字,改完会超出上限')
    _write(account_id, items)
    return items


def remove(account_id: str, n: int) -> list:
    items = entries(account_id)
    items.pop(_index(items, n))
    _write(account_id, items)
    return items


def usage_text(items: list) -> str:
    return f'已用 {used(items)}/{MEMORY_LIMIT} 字'


def prompt_block(account_id: str) -> str:
    items = entries(account_id)
    lines = ['<memory>']
    lines += [f'{i}. {e}' for i, e in enumerate(items, 1)] or ['(还没有记忆)']
    lines.append('</memory>')
    lines.append(f'以上是你的长期记忆({usage_text(items)}),所有会话共用。值得长期记住的事(人物、偏好、约定、待办)'
                 f'用 ding memory add "内容" --account {account_id} 记下,一条一句话;过时的用 ding memory rm 编号 --account {account_id} 删掉,'
                 f'要改用 ding memory edit 编号 "内容" --account {account_id}。写满了会失败,先删或合并旧的。'
                 f'不要把一个会话里的事透露给其他会话里的人。')
    return '\n'.join(lines)


REVIEW_PROMPT = ('现在是每日回顾:下面是最近 24 小时的聊天记录。请据此整理长期记忆:'
                 '新出现的、值得长期记住的事(人物、偏好、约定、待办)用 ding memory add "内容"{acct} 记下;'
                 '过时的、已经办完的用 ding memory rm 编号{acct} 删掉;重复或啰嗦的用 ding memory edit 编号 "内容"{acct} 合并精简。'
                 '记忆要保持精简,宁缺毋滥。不要给任何人发消息,整理完直接结束。')
