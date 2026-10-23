"""Receive one exact ordinary file through WeChat's file search and receiver."""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import time

ALLOWED = {'.pdf', '.docx', '.xlsx', '.pptx', '.txt', '.csv', '.zip', '.rar', '.7z'}


def load_target(export: Path, target_id: str) -> dict:
    sid, mid = target_id.rsplit(':', 1)
    payload = json.loads(export.read_text(encoding='utf-8-sig'))
    messages = [m for c in payload['conversations'] if c['session_id'] == sid
                for m in c['messages'] if str(m['message_id']) == mid]
    if len(messages) != 1:
        raise ValueError('exact file message missing or ambiguous')
    message = messages[0]
    a = message.get('attachment') or {}
    if (a.get('kind') != 'file' or Path(a.get('filename', '')).suffix.lower() not in ALLOWED
            or not re.fullmatch('[a-fA-F0-9]{32}', a.get('md5') or '')
            or type(a.get('size')) is not int or not 0 < a['size'] <= 32 * 1024**2
            or not a.get('path') or not message.get('local_id')):
        raise ValueError('file metadata absent, unsafe type, or exceeds 32 MiB')
    return message


def content_file(attachment: dict) -> Path | None:
    """Do not trust names, numbered copies or a disappearing UI progress marker."""
    expected = Path(attachment['path'])
    if not expected.parent.is_dir():
        return None
    for count, candidate in enumerate(expected.parent.iterdir()):
        if count >= 100000:
            raise ValueError('native file directory exceeds scoped scan limit')
        if not candidate.is_file() or candidate.stat().st_size != attachment['size']:
            continue
        digest = hashlib.md5()
        with candidate.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024**2), b''):
                digest.update(chunk)
        if digest.hexdigest() == attachment['md5'].lower():
            return candidate
    return None


def row_matches(label: str, filename: str, day) -> bool:
    normalized = ' '.join(label.split())
    # WeChat omits the year for the current year. Never infer an older year.
    dates = {f'{day.year}/{day.month}/{day.day}', f'{day.year}年{day.month}月{day.day}日'}
    if day.year == datetime.now().year:
        dates.add(f'{day.month}/{day.day}')
    return any(normalized == ' '.join(filename.split()) + ' ' + value for value in dates)


def receive(export: Path, target_id: str, chat: str, timeout: float = 60) -> dict:
    message = load_target(export, target_id)
    attachment = message['attachment']
    existing = content_file(attachment)
    if existing:
        return {'target_id': target_id, 'status': 'already_present', 'path': str(existing)}
    before_stats = {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in Path(attachment['path']).parent.iterdir() if p.is_file()}
    from targeted_image_backfill import (task_lock, WechatChatAdapter, walk, unique, history_window,
        context_for, validated_window_click, one_window, close_history, rect_to_tuple,
        enumerate_desktop_windows, ChatAutomationError)
    sid = target_id.rsplit(':', 1)[0]
    filename = attachment['filename']
    day = datetime.fromtimestamp(message['timestamp']).date()
    owned = None
    history_owned = False
    with task_lock():
        adapter = WechatChatAdapter()
        with adapter.compatibility(True):
            main = adapter.open_chat(chat)
            # Refuse to reuse a receiver belonging to a previous/user operation.
            before = {w.handle for w in enumerate_desktop_windows()}
            try:
                history = history_window(adapter, main, chat, sid)
                history_owned = history.handle not in before
                tab = unique([n for n in walk(history.control) if n.ControlTypeName == 'TabItemControl'
                              and n.Name == '文件'], '文件分类')
                validated_window_click(adapter, context_for(history, chat), tab)
                edit = unique([n for n in walk(history.control) if n.ControlTypeName == 'EditControl'], '文件搜索框')
                validated_window_click(adapter, context_for(history, chat), edit)
                edit.GetValuePattern().SetValue(filename)
                last = None
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    rows = [n for n in walk(history.control) if n.ClassName == 'mmui::SearchUniqueSubtypeCellView']
                    labels = tuple(n.Name for n in rows)
                    if labels and labels == last:
                        break
                    last = labels
                    time.sleep(.25)
                row = unique([r for r in rows if row_matches(r.Name, filename, day)], '文件名和日期')
                # A second occurrence of this filename cannot be disambiguated by order.
                if len(rows) != 1:
                    raise ChatAutomationError('文件搜索结果不唯一，停止下载')
                row = unique([r for r in walk(history.control) if r.ClassName == 'mmui::SearchUniqueSubtypeCellView'
                              and row_matches(r.Name, filename, day)], '重读文件身份')
                validated_window_click(adapter, context_for(history, chat), row)
                deadline = time.monotonic() + 5
                while True:
                    try:
                        receiver = one_window('mmui::FileReceiveWindow')
                        break
                    except ChatAutomationError as exc:
                        if '实际找到 0 个' not in str(exc) or time.monotonic() >= deadline:
                            raise
                        time.sleep(.2)
                if receiver.handle in before:
                    raise ChatAutomationError('拒绝接管已有文件接收窗口')
                owned = receiver
                nodes = walk(receiver.control)
                unique([n for n in nodes if n.ControlTypeName == 'TextControl' and n.Name == filename], '接收文件名')
                validated_window_click(adapter, context_for(receiver, chat), unique(
                    [n for n in nodes if n.ControlTypeName == 'ButtonControl' and n.Name == '接收文件'], '接收按钮'))
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    if adapter.cancelled():
                        raise ChatAutomationError('已取消')
                    landed = content_file(attachment)
                    if landed:
                        return {'target_id': target_id, 'status': ('downloaded' if before_stats.get(str(landed)) != (landed.stat().st_size, landed.stat().st_mtime_ns) else 'receiver_accepted_cached'), 'path': str(landed),
                                'bytes': landed.stat().st_size, 'md5': attachment['md5']}
                    labels = {n.Name for n in walk(receiver.control)}
                    if any(('过期' in name or '已被清理' in name) or 'expired' in name.lower() for name in labels):
                        raise ChatAutomationError('微信明确提示文件已过期')
                    time.sleep(.5)
                raise ChatAutomationError('接收后未取得符合原生大小和MD5的文件')
            finally:
                if owned is not None:
                    buttons = [n for n in walk(owned.control) if n.ControlTypeName == 'ButtonControl' and n.Name == '关闭']
                    validated_window_click(adapter, context_for(owned, chat), unique(buttons, '本项接收窗口关闭'))
                if history_owned:
                    close_history(adapter, chat, sid)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--export', type=Path, required=True)
    p.add_argument('--target-id', required=True)
    p.add_argument('--chat', required=True)
    p.add_argument('--timeout', type=float, default=60)
    args = p.parse_args()
    if not 1 <= args.timeout <= 180:
        p.error('timeout must be 1..180 seconds')
    from targeted_image_backfill import configure_output_encoding
    configure_output_encoding()
    print(json.dumps(receive(args.export, args.target_id, args.chat, args.timeout), ensure_ascii=True), flush=True)


if __name__ == '__main__':
    main()
