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


def load_target(export: Path, target_id: str, *, streamed_file: bool = False) -> dict:
    if type(streamed_file) is not bool:
        raise ValueError('streamed_file must be an explicit boolean')
    native_limit = (128 if streamed_file else 100) * 1024**2
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
            or type(a.get('size')) is not int or not 0 < a['size'] <= native_limit
            or not a.get('path') or not message.get('local_id')):
        raise ValueError('file metadata absent, unsafe type, or exceeds scoped native-file limit')
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
    today = datetime.now().date()
    if day.year == today.year:
        dates.add(f'{day.month}/{day.day}')
    relative = {0: '今天', 1: '昨天', 2: '前天'}.get((today - day).days)
    if relative:
        dates.add(relative)
    name = ' '.join(filename.split())
    # A previous failed receive adds this status between the intact name and date.
    return any(normalized == name + status + value
               for value in dates for status in (' ', ' 下载失败 ', ' 已清理 ', ' 已过期 '))


def cleared_row(label: str, filename: str, day) -> bool:
    name = ' '.join(filename.split())
    normalized = ' '.join(label.split())
    return row_matches(label, filename, day) and any(
        normalized.startswith(name+' '+status+' ') for status in ('已清理', '已过期'))


def cleared_file_label(rows, filename: str, day) -> str | None:
    """A receive may change its search card to cleared without opening a receiver."""
    matches = [row.Name for row in rows if row_matches(row.Name, filename, day)]
    if len(matches) > 1:
        raise ValueError('文件状态更新后文件名和日期不再唯一')
    return matches[0] if matches and cleared_row(matches[0], filename, day) else None


def exact_file_row(rows, filename, day):
    matches = [row for row in rows if row_matches(row.Name, filename, day)]
    if len(matches) != 1:
        raise ValueError(f'文件名和日期必须唯一，实际找到 {len(matches)} 个')
    return matches[0]


def receive_button(nodes):
    buttons = [n for n in nodes if n.ControlTypeName == 'ButtonControl'
               and n.Name in {'接收文件', '继续下载'}]
    if len(buttons) != 1:
        raise ValueError('接收或续传按钮必须唯一')
    return buttons[0]


def download_menu_item(menu, before, pid):
    from targeted_image_backfill import walk
    if (menu.handle in before or menu.control.ProcessId != pid
            or menu.control.ClassName != 'mmui::XMenu'):
        raise ValueError('拒绝接管已有或其他进程的文件菜单')
    items = [n for n in walk(menu.control) if n.ControlTypeName == 'MenuItemControl'
             and n.Name == '下载']
    if len(items) != 1:
        raise ValueError('原生下载菜单必须唯一')
    return items[0]


def request_menu_download(adapter, history, row, chat):
    """下载已核验的唯一卡片；菜单不接收激活消息，避免被Qt销毁。"""
    import ctypes
    from ctypes import wintypes
    from targeted_image_backfill import (context_for, enumerate_desktop_windows, one_window,
        rect_to_tuple, validated_window_point_click, ChatAutomationError)
    before = {w.handle for w in enumerate_desktop_windows()}
    context = context_for(history, chat)
    destination, x, y = adapter._click_target(context, row)
    user = ctypes.WinDLL('user32', use_last_error=True)
    user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user.PostMessageW.restype = wintypes.BOOL
    user.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    user.ScreenToClient.restype = wintypes.BOOL
    user.IsWindow.argtypes = [wintypes.HWND]
    user.IsWindow.restype = wintypes.BOOL
    point = wintypes.POINT(x, y)
    if not user.ScreenToClient(destination.window.handle, ctypes.byref(point)):
        raise ChatAutomationError('文件卡片坐标转换失败')
    position = ((point.y & 0xffff) << 16) | (point.x & 0xffff)
    for message, flags in ((0x0204, 2), (0x0205, 0)):
        if not user.PostMessageW(destination.window.handle, message, flags, position):
            raise ChatAutomationError('无法打开已核验文件卡片菜单')
        time.sleep(.05)
    deadline = time.monotonic() + 5
    while True:
        try:
            menu = one_window('mmui::XMenu')
            break
        except ChatAutomationError as exc:
            if '实际找到 0 个' not in str(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(.2)
    item = download_menu_item(menu, before, history.control.ProcessId)
    menu_context = context_for(menu, chat)
    adapter._message_transport.verify_window(menu_context)
    try:
        bounds = rect_to_tuple(item.BoundingRectangle)
        validated_window_point_click(adapter, menu_context, (bounds[0]+bounds[2])//2,
                                     (bounds[1]+bounds[3])//2, activate=False)
    finally:
        if user.IsWindow(menu.handle):
            adapter._message_transport.verify_window(menu_context)
            user.PostMessageW(menu.handle, 0x0010, 0, 0)


def receive(export: Path, target_id: str, chat: str, timeout: float = 60, *, streamed_file: bool = False) -> dict:
    """An explicit streamed-file caller must retain external tree/I/O/space guards.

    Only native file bytes are received; content_file hashes in 1MiB blocks.
    The CLI and daily callers keep the default 100MiB, and no viewer is opened.
    """
    message = load_target(export, target_id, streamed_file=streamed_file)
    attachment = message['attachment']
    existing = content_file(attachment)
    if existing:
        return {'target_id': target_id, 'status': 'already_present', 'path': str(existing)}
    parent = Path(attachment['path']).parent
    before_stats = ({str(p): (p.stat().st_size, p.stat().st_mtime_ns)
                     for p in parent.iterdir() if p.is_file()} if parent.is_dir() else {})
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
                edit.GetValuePattern().SetValue(' '.join(filename.split()))
                last = None
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    rows = [n for n in walk(history.control) if n.ClassName == 'mmui::SearchUniqueSubtypeCellView']
                    labels = tuple(n.Name for n in rows)
                    if labels and labels == last:
                        break
                    last = labels
                    time.sleep(.25)
                row = exact_file_row(rows, filename, day)
                # Other names or dates are irrelevant; equal name AND date must stay unique.
                row = exact_file_row([r for r in walk(history.control)
                                      if r.ClassName == 'mmui::SearchUniqueSubtypeCellView'], filename, day)
                if cleared_row(row.Name, filename, day):
                    raise ChatAutomationError('微信明确提示文件已过期或已清理：'+row.Name)
                if adapter._message_transport is not None:
                    request_menu_download(adapter, history, row, chat)
                else:
                    validated_window_click(adapter, context_for(history, chat), row)
                deadline = time.monotonic() + timeout
                last_card_check = 0.0
                while time.monotonic() < deadline:
                    if adapter.cancelled():
                        raise ChatAutomationError('已取消')
                    landed = content_file(attachment)
                    if landed:
                        return {'target_id': target_id, 'status': ('downloaded' if before_stats.get(str(landed)) != (landed.stat().st_size, landed.stat().st_mtime_ns) else 'receiver_accepted_cached'), 'path': str(landed),
                                'bytes': landed.stat().st_size, 'md5': attachment['md5']}
                    if time.monotonic() - last_card_check >= 2:
                        current_rows = [n for n in walk(history.control)
                                        if n.ClassName == 'mmui::SearchUniqueSubtypeCellView']
                        cleared = cleared_file_label(current_rows, filename, day)
                        if cleared:
                            raise ChatAutomationError('微信明确提示文件已过期或已清理：'+cleared)
                        last_card_check = time.monotonic()
                    if owned is None:
                        try:
                            receiver = one_window('mmui::FileReceiveWindow')
                        except ChatAutomationError as exc:
                            if '实际找到 0 个' not in str(exc):
                                raise
                        else:
                            if receiver.handle in before:
                                raise ChatAutomationError('拒绝接管已有文件接收窗口')
                            owned = receiver
                            nodes = walk(receiver.control)
                            unique([n for n in nodes if n.ControlTypeName == 'TextControl' and n.Name == filename], '接收文件名')
                            labels = {n.Name for n in nodes}
                            if any('过期' in n or '已被清理' in n or 'expired' in n.lower() for n in labels):
                                raise ChatAutomationError('微信明确提示文件已过期')
                            # The native menu may already have started the transfer.
                            buttons = [n for n in nodes if n.ControlTypeName == 'ButtonControl'
                                       and n.Name in {'接收文件', '继续下载'}]
                            if buttons:
                                validated_window_click(adapter, context_for(receiver, chat), receive_button(nodes))
                    if owned is not None:
                        labels = {n.Name for n in walk(owned.control)}
                        if any('过期' in n or '已被清理' in n or 'expired' in n.lower() for n in labels):
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
