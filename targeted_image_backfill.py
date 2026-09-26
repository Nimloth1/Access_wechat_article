# encoding: UTF-8
# version: 1.0 @20260923: 按落盘缺口日期和微信消息 ID 定向补原图;
# version: 1.1 @20260924: 适配聊天记录星期标签，支持按目标消息 ID 文件筛选;
# version: 1.2 @20260924: 修复月份菜单定位，支持导出与界面会话名不同;
# version: 1.3 @20260926: 任务结束或异常时关闭聊天记录窗口;
# version: 1.4 @20260926: 微信 Qt 按钮不响应 UIA Click 时改用校验后的窗口消息点击;
# version: 1.5 @20260926: 日期筛选及图片预览操作也使用校验后的窗口消息;
__version__ = 'v1.5'
__platform__ = 'nt'
__pc__ = ['PW']
__py__ = 'py3.11+'

"""从群聊记录的日期入口定位缺图消息，并以真实落盘文件确认结果。"""

import argparse
from collections import defaultdict
import ctypes
from ctypes import wintypes
from datetime import date, datetime
import json
import re
from pathlib import Path
import time

from chat_attachments import DEFAULT_EXPORT_ROOT, LOG_DIR, task_lock
from src.modules.window.wechat_chat_adapter import ChatContext, WechatChatAdapter, walk, unique
from src.modules.window.wechat_home_window_finder import attach_uia_control, enumerate_desktop_windows, rect_to_tuple
from src.services.chat_attachment_service import ChatAutomationError
from src.services.wechat_attachment_inventory import load_inventory


PREVIEW_ID = re.compile(r'^PreviewWindow_(.+)_(\d+)_(\d+)_\d+_\d+$')
IMAGE_ROW = re.compile(r'^图片 .+ (\d{2}:\d{2})$')
ATTEMPT_LOG = LOG_DIR / 'targeted_image_attempts.jsonl'


def _user32():
    """延迟获取 user32，便于在非 Windows 环境导入并测试纯逻辑。"""
    return ctypes.windll.user32


def validated_window_point_click(adapter, context, x, y, *, clicks=1):
    """校验屏幕坐标归属后，向已核验窗口发送 Qt 可识别的鼠标点击。"""
    adapter._activate(context)
    adapter._check_point(context, x, y)
    point = wintypes.POINT(x, y)
    user32 = _user32()
    if not user32.ScreenToClient(context.window.handle, ctypes.byref(point)):
        raise ChatAutomationError('无法把按钮坐标转换为微信窗口坐标')
    lparam = ((point.y & 0xffff) << 16) | (point.x & 0xffff)
    for _ in range(clicks):
        if not user32.PostMessageW(context.window.handle, 0x0201, 0x0001, lparam):
            raise ChatAutomationError('无法向微信窗口发送鼠标按下消息')
        time.sleep(0.05)
        if not user32.PostMessageW(context.window.handle, 0x0202, 0, lparam):
            raise ChatAutomationError('无法向微信窗口发送鼠标释放消息')
        time.sleep(0.05)


def validated_window_click(adapter, context, control):
    """点击已核验窗口中的控件中心。"""
    left, top, right, bottom = rect_to_tuple(control.BoundingRectangle)
    validated_window_point_click(adapter, context, (left + right) // 2, (top + bottom) // 2)


def parse_args():
    """解析群名、日期范围和单次处理上限。"""
    parser = argparse.ArgumentParser(description='按落盘缺口定向补齐微信群原图')
    parser.add_argument('--chat', required=True, help='完整群名')
    parser.add_argument('--export-chat', help='日导出中的会话名与微信界面显示不同时使用')
    parser.add_argument('--since', type=date.fromisoformat, default=date(2026, 1, 1))
    parser.add_argument('--max-items', type=int, default=0, help='本次最多核验的缺口数，0 表示不限')
    parser.add_argument('--retry-expired', action='store_true', help='重试微信已明确提示过期的图片')
    parser.add_argument('--target-ids-file', type=Path, help='每行一个完整消息 ID，如 群ID:消息ID')
    parser.add_argument('--export-root', type=Path, default=DEFAULT_EXPORT_ROOT)
    return parser.parse_args()


def one_window(class_name, *, session_id=None):
    """只接受属于当前微信会话的唯一可见窗口。"""
    matches = []
    for window in enumerate_desktop_windows():
        if (window.process_name.lower() != 'weixin.exe' or not window.visible or
                window.is_minimized):
            continue
        control = attach_uia_control(window).control
        if control.ClassName != class_name:
            continue
        if session_id and session_id not in control.AutomationId:
            continue
        matches.append(window)
    return attach_uia_control(unique(matches, class_name))


def context_for(window, chat):
    """为已经核验的微信子窗口创建操作上下文。"""
    return ChatContext(window, chat, forwarded=True)


def close_preview(adapter, chat, session_id):
    """关闭本群的图片预览，保留聊天记录窗口。"""
    try:
        window = one_window('mmui::PreviewWindow', session_id=session_id)
    except ChatAutomationError as exc:
        if '实际找到 0 个' in str(exc):
            return
        raise
    context = context_for(window, chat)
    button = unique([node for node in walk(window.control) if node.ControlTypeName == 'ButtonControl'
                     and node.Name == '关闭'], '图片预览关闭按钮')
    validated_window_click(adapter, context, button)


def close_history(adapter, chat, session_id):
    """关闭本次补图使用的聊天记录窗口。"""
    try:
        window = one_window('mmui::SearchMsgUniqueChatWindow', session_id=session_id)
    except ChatAutomationError as exc:
        if '实际找到 0 个' in str(exc):
            return
        raise
    context = context_for(window, chat)
    button = unique([node for node in walk(window.control) if node.ControlTypeName == 'ButtonControl'
                     and node.Name == '关闭'], '聊天记录关闭按钮')
    validated_window_click(adapter, context, button)


def history_window(adapter, main, chat, session_id):
    """打开或复用当前群的聊天记录窗口。"""
    try:
        return one_window('mmui::SearchMsgUniqueChatWindow', session_id=session_id)
    except ChatAutomationError as exc:
        if '实际找到 0 个' not in str(exc):
            raise
    button = unique([node for node in walk(main.window.control)
                     if node.ControlTypeName == 'ButtonControl' and node.Name == '聊天记录'], '聊天记录按钮')
    validated_window_click(adapter, main, button)
    deadline = time.monotonic() + 5
    while True:
        try:
            return one_window('mmui::SearchMsgUniqueChatWindow', session_id=session_id)
        except ChatAutomationError as exc:
            if '实际找到 0 个' not in str(exc) or time.monotonic() >= deadline:
                raise
        time.sleep(0.2)


def calendar_day_point(day, panel_bounds):
    """按星期列和月内行计算日期格中心，适配日历的 DPI 缩放。"""
    left, top, right, _ = panel_bounds
    scale = (right - left) / 424
    column = day.weekday()
    row = (date(day.year, day.month, 1).weekday() + day.day - 1) // 7
    return (round(left + (20 + 64 * column) * scale),
            round(top + (84 + 64 * row) * scale))


def select_day(adapter, history, chat, session_id, day):
    """在日期日历中选择一天，并核对筛选结果标题。"""
    context = context_for(history, chat)
    for _ in range(3):
        tab = unique([node for node in walk(history.control) if node.ControlTypeName == 'TabItemControl'
                      and node.Name == '日期'], '日期标签')
        validated_window_click(adapter, context, tab)
        try:
            calendar = one_window('mmui::XPopover')
        except ChatAutomationError as exc:
            if '实际找到 0 个' in str(exc):
                continue
            raise
        if any(node.Name == f'{day.year}年' for node in walk(calendar.control)):
            break
    else:
        raise ChatAutomationError(f'日历年份不是 {day.year}，停止定位')
    context = context_for(calendar, chat)
    nodes = walk(calendar.control)
    month = next((node.Name for node in nodes if node.ControlTypeName == 'TextControl'
                  and re.fullmatch(r'\d+月', node.Name)), None)
    if month != f'{day.month}月':
        combos = [node for node in nodes if node.ControlTypeName == 'ComboBoxControl']
        validated_window_click(adapter, context, unique(combos[1:2], '月份下拉框'))
        menus = [window for window in enumerate_desktop_windows()
                 if window.process_name.lower() == 'weixin.exe' and window.visible and
                 attach_uia_control(window).control.ClassName == 'mmui::XMenu']
        menu = attach_uia_control(unique(menus, '月份菜单'))
        menu_context = context_for(menu, chat)
        for _ in range(20):
            option = unique([node for node in walk(menu.control) if node.ControlTypeName == 'MenuItemControl'
                             and node.Name == f'{day.month}月'], '目标月份')
            rect = rect_to_tuple(option.BoundingRectangle)
            bounds = rect_to_tuple(menu.control.BoundingRectangle)
            viewport = (bounds[0], bounds[1] + 30, bounds[2], bounds[3] - 30)
            center_y = (rect[1] + rect[3]) // 2
            if viewport[1] + 8 <= center_y <= viewport[3] - 8:
                break
            adapter._check_point(menu_context, (viewport[0] + viewport[2]) // 2,
                                 (viewport[1] + viewport[3]) // 2)
            (menu.control.WheelUp if center_y < viewport[1] + 8 else menu.control.WheelDown)(wheelTimes=1)
            time.sleep(0.1)
        else:
            # 菜单在部分会话中不响应滚轮，使用控件的语义调用并在下方核验月份。
            option.GetInvokePattern().Invoke()
        if viewport[1] + 8 <= center_y <= viewport[3] - 8:
            # 激活临时菜单窗口会使其关闭；直接校验当前可点击层级后点击。
            adapter._check_point(menu_context, (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2)
            validated_window_click(adapter, menu_context, option)
        calendar = one_window('mmui::XPopover')
        context = context_for(calendar, chat)
        if not any(node.ControlTypeName == 'TextControl' and node.Name == f'{day.month}月'
                   for node in walk(calendar.control)):
            raise ChatAutomationError(f'月份菜单未切换到 {day.month}月')
    panel = unique([node for node in walk(calendar.control) if node.ClassName == 'mmui::DateNumPanel'],
                   '日历日期面板')
    x, y = calendar_day_point(day, rect_to_tuple(panel.BoundingRectangle))
    validated_window_point_click(adapter, context, x, y)
    time.sleep(0.2)
    history = one_window('mmui::SearchMsgUniqueChatWindow', session_id=session_id)
    heading = f'{day.year}年{day.month}月{day.day}日'
    if not any(node.ControlTypeName == 'TextControl' and node.Name == heading
               for node in walk(history.control)):
        raise ChatAutomationError(f'未确认日期筛选结果为 {heading}，停止处理')
    return history


def preview_image(adapter, history, chat, row, session_id):
    """双击可见图片正文，返回微信预览中的稳定消息 ID。"""
    context = context_for(history, chat)
    listing = unique([node for node in walk(history.control) if node.AutomationId == 'search_message_list'],
                     '筛选消息列表')
    bounds = rect_to_tuple(row.BoundingRectangle)
    view = rect_to_tuple(listing.BoundingRectangle)
    top = max(bounds[1], view[1])
    bottom = min(bounds[3], view[3])
    if bottom - top < 120:
        return None
    x = bounds[0] + round(250 * (view[2] - view[0]) / 1966)
    y = top + min(round(180 * (view[2] - view[0]) / 1966), (bottom - top) // 2)
    validated_window_point_click(adapter, context, x, y, clicks=2)
    time.sleep(0.2)
    preview = one_window('mmui::PreviewWindow', session_id=session_id)
    match = PREVIEW_ID.fullmatch(preview.control.AutomationId)
    if not match or match.group(1) != session_id:
        raise ChatAutomationError('图片预览消息 ID 无法与目标群对应')
    return preview, int(match.group(2)), int(match.group(3))


def current_state(export_root, chat, day, local_id, timestamp):
    """从清单重新核验一条图片是否已生成原图。"""
    _, items = load_inventory(export_root, chat, since=day, until=day)
    matches = [item for item in items if item['local_id'] == local_id and
               item['timestamp'] == timestamp and item['kind'] == 'image']
    return unique(matches, '清单图片')['state']


def expired_attempts():
    """读取已核验的微信过期标记，避免下次重复打开同一张。"""
    if not ATTEMPT_LOG.is_file():
        return set()
    records = [json.loads(line) for line in ATTEMPT_LOG.read_text(encoding='utf-8').splitlines() if line]
    return {record['key'] for record in records if record.get('result') == 'expired'}


def record_attempt(key, result):
    """每处理一张立即保存其结果，以便中断后准确续跑。"""
    ATTEMPT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with ATTEMPT_LOG.open('a', encoding='utf-8', newline='\n') as log:
        log.write(json.dumps({'key': key, 'result': result,
                              'at': datetime.now().isoformat(timespec='seconds')}, ensure_ascii=False) + '\n')
        log.flush()


def run_day(adapter, history, chat, session_id, day, targets, export_root, export_chat, limit):
    """只打开目标分钟的图片，用预览 ID 精确匹配待补消息。"""
    history = select_day(adapter, history, chat, session_id, day)
    target_minutes = {f'{datetime.fromtimestamp(item["timestamp"]).hour:02d}:'
                      f'{datetime.fromtimestamp(item["timestamp"]).minute:02d}'
                      for item in targets.values()}
    examined = set()
    previous_layout = None
    stationary = 0
    handled = 0
    for page in range(300):
        if not targets or (limit and handled >= limit):
            break
        history = one_window('mmui::SearchMsgUniqueChatWindow', session_id=session_id)
        listing = unique([node for node in walk(history.control) if node.AutomationId == 'search_message_list'],
                         '筛选消息列表')
        rows = list(listing.GetChildren())
        layout = tuple((node.Name, rect_to_tuple(node.BoundingRectangle)) for node in rows)
        stationary = stationary + 1 if layout == previous_layout else 0
        if stationary >= 2:
            break
        previous_layout = layout
        for row in rows:
            if not targets or (limit and handled >= limit):
                break
            match = IMAGE_ROW.fullmatch(row.Name)
            if not match or match.group(1) not in target_minutes:
                continue
            marker = (row.Name, tuple(row.GetRuntimeId()), rect_to_tuple(row.BoundingRectangle))
            if marker in examined:
                continue
            opened = preview_image(adapter, history, chat, row, session_id)
            if opened is None:
                continue
            examined.add(marker)
            preview, timestamp, local_id = opened
            key = (timestamp, local_id)
            try:
                if key not in targets:
                    print(f'{day} {local_id}: 非缺口图片，跳过', flush=True)
                    continue
                nodes = walk(preview.control)
                expired = any('已过期或被清理' in node.Name for node in nodes)
                if not expired:
                    buttons = [node for node in nodes if node.ControlTypeName == 'ButtonControl' and
                               node.Name in {'查看原图', '下载原图', 'View Original'}]
                    if buttons:
                        validated_window_click(adapter, context_for(preview, chat),
                                               unique(buttons, '查看原图按钮'))
                    for _ in range(10):
                        time.sleep(0.5)
                        if current_state(export_root, export_chat, day, local_id, timestamp) == 'original':
                            break
                state = current_state(export_root, export_chat, day, local_id, timestamp)
                record_attempt(targets[key]['key'], 'expired' if expired else state)
                print(f'{day} {datetime.fromtimestamp(timestamp):%H:%M:%S} id={local_id}: '
                      f'{"微信已过期" if expired else state}', flush=True)
                handled += 1
                del targets[key]
                target_minutes = {f'{datetime.fromtimestamp(item["timestamp"]).hour:02d}:'
                                  f'{datetime.fromtimestamp(item["timestamp"]).minute:02d}'
                                  for item in targets.values()}
            finally:
                close_preview(adapter, chat, session_id)
        if targets and (not limit or handled < limit):
            adapter._wheel(context_for(history, chat), older=False, steps=6)
            time.sleep(0.2)
    return handled


def main():
    """按缺口日期运行，结束后重新核验清单摘要。"""
    print(f'微信定向原图补齐 {__version__}', flush=True)
    args = parse_args()
    if args.max_items < 0:
        raise ValueError('--max-items 不能为负数')
    export_chat = args.export_chat or args.chat
    session_id, items = load_inventory(args.export_root, export_chat, since=args.since)
    if args.target_ids_file:
        target_ids = {line.strip() for line in args.target_ids_file.read_text(encoding='utf-8-sig').splitlines()
                      if line.strip()}
        if not target_ids:
            raise ValueError('目标消息 ID 文件为空')
        items = [item for item in items if f'{item["session_id"]}:{item["message_id"]}' in target_ids]
    pending = {(item['timestamp'], item['local_id']): item for item in items
               if item['kind'] == 'image' and item['state'] != 'original'}
    if not args.retry_expired:
        expired = expired_attempts()
        pending = {key: item for key, item in pending.items() if item['key'] not in expired}
        if expired:
            print(f'跳过已确认过期的图片；可用 --retry-expired 重试', flush=True)
    by_day = defaultdict(dict)
    for key, item in pending.items():
        by_day[datetime.fromtimestamp(item['timestamp']).date()][key] = item
    print(f'{args.chat}: 待补原图 {len(pending)} 张，分布在 {len(by_day)} 天', flush=True)
    if not pending:
        return
    with task_lock():
        adapter = WechatChatAdapter()
        with adapter.compatibility(True):
            close_preview(adapter, args.chat, session_id)
            main_chat = adapter.open_chat(args.chat)
            try:
                history = history_window(adapter, main_chat, args.chat, session_id)
                handled = 0
                for day, targets in sorted(by_day.items()):
                    if args.max_items and handled >= args.max_items:
                        break
                    print(f'{day}: 缺口 {len(targets)} 张', flush=True)
                    handled += run_day(adapter, history, args.chat, session_id, day, targets,
                                       args.export_root, export_chat,
                                       args.max_items - handled if args.max_items else 0)
                    if targets:
                        print(f'{day}: 尚未定位 {len(targets)} 张', flush=True)
                print(f'本次已核验 {handled} 张；重新运行会按当前落盘状态选缺口。', flush=True)
            finally:
                try:
                    close_preview(adapter, args.chat, session_id)
                finally:
                    close_history(adapter, args.chat, session_id)


if __name__ == '__main__':
    main()
