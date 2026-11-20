#!/usr/bin/env python3
"""DingAgentX CLI (agent 和人都用同一套).

  ding memory [list|add|edit|rm] ...   长期记忆 (每账号一个 memory.md)
  ding task list|add|rm ...            定时任务 (到点后交给创建会话的 agent)
  ding chats                           本地会话列表
  ding messages <chat_id>              本地消息流水
  ding send <chat_id> <text>           发消息 (走 lwp 长连接)
  ding account                         当前账号 id

agent 子进程环境带 DINGTALKX_AGENT=1 / DINGTALKX_ACCOUNT / DINGTALKX_CHAT_ID
(见 agent.py CliBackend.run),task add 不带 --chat 时记到当前会话。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime


def _is_agent() -> bool:
    return os.environ.get('DINGTALKX_AGENT') == '1'


def _default_account() -> str:
    """agent 子进程里取注入的账号;人工执行时取唯一登录账号。"""
    env = os.environ.get('DINGTALKX_ACCOUNT')
    if env:
        return env
    base = os.path.join(os.path.expanduser('~'), '.dingtalkx', 'accounts')
    try:
        ids = [n for n in sorted(os.listdir(base)) if n.isdigit()]
    except OSError:
        ids = []
    if len(ids) == 1:
        return ids[0]
    if not ids:
        print('还没有账号。先在控制台扫码登录,或用 --account 指定。', file=sys.stderr)
        sys.exit(1)
    print(f'有 {len(ids)} 个账号,用 --account 指定: {" ".join(ids)}', file=sys.stderr)
    sys.exit(1)


def cmd_memory(args) -> int:
    from dingtalkx import memory
    account_id = args.account
    try:
        if args.action == 'add':
            items = memory.add(account_id, ' '.join(args.args))
        elif args.action == 'edit':
            if len(args.args) < 2 or not args.args[0].isdigit():
                print('用法: ding memory edit 编号 "新的内容"', file=sys.stderr)
                return 1
            items = memory.edit(account_id, int(args.args[0]), ' '.join(args.args[1:]))
        elif args.action == 'rm':
            if len(args.args) != 1 or not args.args[0].isdigit():
                print('用法: ding memory rm 编号', file=sys.stderr)
                return 1
            items = memory.remove(account_id, int(args.args[0]))
        else:
            items = memory.entries(account_id)
    except memory.MemoryEditError as e:
        print(str(e), file=sys.stderr)
        return 1
    for i, e in enumerate(items, 1):
        print(f'{i}. {e}')
    if not items:
        print('(还没有记忆)')
    print(memory.usage_text(items))
    return 0


def cmd_task(args) -> int:
    from dingtalkx import tasks
    from datetime import datetime
    account_id = args.account
    if args.action == 'rm':
        if len(args.args) != 1 or not args.args[0].isdigit():
            print('用法: ding task rm 编号', file=sys.stderr)
            return 1
        if not tasks.remove(account_id, int(args.args[0])):
            print(f'没有编号 {args.args[0]} 的定时任务', file=sys.stderr)
            return 1
        print(f'已删除定时任务 #{args.args[0]}')
        return 0
    if args.action == 'add':
        content = ' '.join(args.args).strip()
        # agent 里执行时默认记到当前会话(环境变量由 agent.py 注入),到点后在这个会话里处理
        chat_id = args.chat or os.environ.get('DINGTALKX_CHAT_ID') or ''
        if not content:
            print('要写上到点后做什么,比如: ding task add --in 30m "提醒我交周报"', file=sys.stderr)
            return 1
        if not chat_id:
            print('不知道到点后在哪个会话里处理,请用 --chat 指定会话 id', file=sys.stderr)
            return 1
        try:
            if args.daily:
                daily, kind = tasks.parse_hhmm(args.daily), 'daily'
                run_at = int(tasks.next_daily(daily, datetime.now()).timestamp())
            else:
                daily, kind = '', 'once'
                run_at = int((tasks.parse_in(args.in_) if args.in_ else tasks.parse_at(args.at)).timestamp())
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 1
        tid = tasks.add(account_id, chat_id, kind, content, run_at=run_at, daily_at=daily,
                        created_by='agent' if _is_agent() else 'user')
        when = f'每天 {daily}' if daily else datetime.fromtimestamp(run_at).strftime('%Y-%m-%d %H:%M')
        print(f'已添加定时任务 #{tid}: {when} {content}')
        return 0
    rows = tasks.list_tasks(account_id)
    for t in rows:
        print(f"#{t['id']}\t{tasks.describe(t)}\t{t['chat_id']}\t{t['content']}")
    if not rows:
        print('(没有定时任务)')
    return 0


def cmd_chats(args) -> int:
    """本地会话列表 (消息流水聚合)."""
    path = _msglog()
    rows = {}
    try:
        for line in open(path, encoding='utf-8'):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            cid = str(r.get('conversation_id') or '')
            if not cid:
                continue
            row = rows.get(cid)
            if not row or (r.get('time') or 0) > row['time']:
                rows[cid] = {'time': r.get('time') or 0, 'text': r.get('text') or '[消息]'}
    except OSError:
        pass
    for cid, r in sorted(rows.items(), key=lambda kv: -kv[1]['time'])[:args.limit]:
        kind = '群聊' if ':' in cid else '私聊'
        print(f"{cid}\t{kind}\t{r['text'][:40]}\t更新于 {datetime.fromtimestamp(r['time'] / 1000).strftime('%m-%d %H:%M') if r['time'] else '?'}")
    if not rows:
        print('(还没有会话)')
    return 0


def cmd_messages(args) -> int:
    path = _msglog()
    out = []
    try:
        for line in open(path, encoding='utf-8'):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if str(r.get('conversation_id')) != args.chat_id:
                continue
            out.append(r)
    except OSError:
        pass
    for r in out[-args.limit:]:
        ts = datetime.fromtimestamp((r.get('time') or 0) / 1000).strftime('%m-%d %H:%M') if r.get('time') else ''
        direction = '→' if r.get('direction') == 'out' else '←'
        content = (r.get('text') or '[消息]')
        shown = content[:120] + ('…' if len(content) > 120 else '')
        print(f'[{ts}]{direction} {r.get("nick") or r.get("sender") or "?"}: {shown}')
    if not out:
        print('(没有消息记录)')
    return 0


def _msglog() -> str:
    """账号消息流水 (listener MSG_LOG)."""
    from dingtalkx.listener import DATA_DIR
    return os.path.join(DATA_DIR, 'messages.jsonl')


def cmd_account(args) -> int:
    print(args.account)
    return 0


def cmd_send(args) -> int:
    """发消息: 起一个 listener 会话 (和 web 控制台同一构造), 用它的 reply 链路."""
    from dingtalkx import accounts as _acc
    from dingtalkx.listener import DingListener
    text = ' '.join(args.text)
    account_id = args.account

    async def go():
        meta = _acc.read_meta(account_id) or {}
        lis = DingListener(token=_acc.token_of(account_id),
                           did=_acc.did_of(account_id) or meta.get('did'),
                           uid=account_id,
                           nick=meta.get('nick') or (meta.get('profile') or {}).get('name') or account_id,
                           state_file=os.path.join(_acc.BASE, account_id, 'rules.json'),
                           cfg=_acc.get_settings(account_id), agent=False)
        try:
            await lis.start()
            for _ in range(60):
                if lis.state == 'running' and lis.client:
                    break
                await asyncio.sleep(0.5)
            if lis.state != 'running':
                print('连接超时', file=sys.stderr)
                return 1
            out = await lis.reply(text, args.chat_id)
            print(out.get('status'), 'body_len', out.get('len'))
            return 0 if out.get('status') == 200 else 1
        finally:
            await lis.stop()

    return asyncio.run(go())


def main() -> int:
    acct = argparse.ArgumentParser(add_help=False)
    acct.add_argument('--account', default=None, metavar='ID', help='账号 id (默认唯一登录账号/agent 注入)')
    p = argparse.ArgumentParser(prog='ding', description='钉钉消息通道 CLI (DingAgentX)')
    sub = p.add_subparsers(dest='cmd', required=True)

    q = sub.add_parser('memory', parents=[acct], help='长期记忆:查看、添加、修改、删除条目')
    msub = q.add_subparsers(dest='action')
    msub.add_parser('list', parents=[acct])
    q2 = msub.add_parser('add', parents=[acct])
    q2.add_argument('args', nargs='+')
    q2 = msub.add_parser('edit', parents=[acct])
    q2.add_argument('args', nargs='+')
    q2 = msub.add_parser('rm', parents=[acct])
    q2.add_argument('args', nargs='+')

    q = sub.add_parser('task', parents=[acct], help='定时任务:到点后交给 agent 处理')
    tsub = q.add_subparsers(dest='action')
    tsub.add_parser('list', parents=[acct])
    q2 = tsub.add_parser('add', parents=[acct])
    q2.add_argument('args', nargs='+')
    g = q2.add_mutually_exclusive_group()
    g.add_argument('--in', dest='in_', help='多久之后,如 30m / 2h / 1d')
    g.add_argument('--at', help='指定时间,如 "2026-01-01 09:00" 或 "09:00"')
    g.add_argument('--daily', help='每天,如 09:00')
    q2.add_argument('--chat', help='会话 id (agent 里默认当前会话)')
    q2 = tsub.add_parser('rm', parents=[acct])
    q2.add_argument('args', nargs='+')

    q = sub.add_parser('chats', parents=[acct], help='列出本地会话')
    q.add_argument('--limit', type=int, default=30)
    q = sub.add_parser('messages', parents=[acct], help='读本地消息记录')
    q.add_argument('chat_id')
    q.add_argument('--limit', type=int, default=50)
    q = sub.add_parser('send', parents=[acct], help='发送文字消息')
    q.add_argument('chat_id')
    q.add_argument('text', nargs='+')
    q = sub.add_parser('account', parents=[acct], help='当前账号 id')

    args = p.parse_args()
    if not getattr(args, 'account', None):
        args.account = _default_account()
    try:
        from dingtalkx.accounts import normalize
        args.account = normalize(args.account)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    return {
        'memory': cmd_memory,
        'task': cmd_task,
        'chats': cmd_chats,
        'messages': cmd_messages,
        'send': cmd_send,
        'account': cmd_account,
    }[args.cmd](args)


if __name__ == '__main__':
    sys.exit(main())
