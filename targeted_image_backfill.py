# encoding: UTF-8
# version: 1.0 @20260923: 按落盘缺口日期和微信消息 ID 定向补原图;
# version: 1.1 @20260924: 适配聊天记录星期标签，支持按目标消息 ID 文件筛选;
# version: 1.2 @20260924: 修复月份菜单定位，支持导出与界面会话名不同;
# version: 1.3 @20260926: 任务结束或异常时关闭聊天记录窗口;
# version: 1.4 @20260926: 微信 Qt 按钮不响应 UIA Click 时改用校验后的窗口消息点击;
# version: 1.5 @20260926: 日期筛选及图片预览操作也使用校验后的窗口消息;
# version: 1.6 @20260926: 无远控鼠标时用窗口消息滚动聊天记录列表;
# version: 1.7 @20260926: 图片预览创建较慢时轮询并重试，避免中断整批;
# version: 1.8 @20260926: Windows 日志固定为 UTF-8，支持含 emoji 的会话名;
# version: 1.9 @20260926: 缩小记录列表翻页跨度，避免图片行落在页面间隙;
# version: 2.0 @20260930: 等待日期筛选标题刷新，并在失败时记录实际显示的日期;
# version: 2.1 @20260930: 未定位图片时记录目标与可见图片时间，便于区分列表缺失和格式变化;
# version: 2.2 @20260930: 日期校验失败时按周日首列重新定位日历，适配微信布局变化;
# version: 2.3 @20260930: 未定位目标另存结构化诊断，避免监控正文截断后丢失证据;
# version: 2.4 @20261010: 日期标题滞留前一天时按附件行末时间核验，避免误报筛选失败;
# version: 2.5 @20261010: 月份菜单不再激活弹层，滚动后核验可见选项与实际月份;
#                           兼容需确认的日期弹层，更新悬停日期后核验实际筛选结果;
# version: 2.6 @20261013: 点击或滚动位置被遮挡时强制置顶微信并重新核验一次;
# version: 2.7 @20261014: 遮挡现场日志区分强制激活前后的坐标核验;
# version: 2.8 @20261016: 末尾未定位时重建日期查询一次，支持不下载的预览身份验证;
__version__ = 'v2.8'
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
import sys
import time

from chat_attachments import DEFAULT_EXPORT_ROOT, LOG_DIR, task_lock
from src.modules.window.wechat_chat_adapter import (AttachmentNotClickableError, ChatContext,
                                                     WechatChatAdapter, message_day, walk, unique)
from src.modules.window.wechat_home_window_finder import attach_uia_control, enumerate_desktop_windows, rect_to_tuple
from src.services.chat_attachment_service import ChatAutomationError
from src.services.wechat_attachment_inventory import load_inventory


PREVIEW_ID = re.compile(r'^PreviewWindow_(.+)_(\d+)_(\d+)_\d+_\d+$')
IMAGE_ROW = re.compile(r'^图片(?: .+)? (\d{2}:\d{2})$')
ROW_DATE = re.compile(r'(?:^|\s)((?:(?:\d{4}年)?\d{1,2}月\d{1,2}日(?:\s+星期[一二三四五六日天])?'
                      r'|今天|昨天|前天|星期[一二三四五六日天])\s+\d{1,2}:\d{2})$')
ROW_TIME_ONLY = re.compile(r'(?:^|\s)\d{1,2}:\d{2}$')
ATTEMPT_LOG = LOG_DIR / 'targeted_image_attempts.jsonl'


def configure_output_encoding():
    """避免 Windows 默认 GBK 因会话名含 emoji 而让任务在打印日志时退出。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='backslashreplace')


def _user32():
    """延迟获取 user32，便于在非 Windows 环境导入并测试纯逻辑。"""
    return ctypes.windll.user32


def ensure_clickable(adapter, context, x, y, *, activate=True):
    """遮挡时强制置顶一次，重新核验坐标；弹层保持原有焦点。"""
    calendar_popup = getattr(getattr(context.window, 'control', None), 'ClassName', '') == 'mmui::XPopover'
    if activate and not calendar_popup:
        adapter._activate(context)
    try:
        adapter._check_point(context, x, y, phase='before_force')
    except AttachmentNotClickableError:
        if not activate or calendar_popup:
            raise
        adapter._activate(context, force=True)
        adapter._check_point(context, x, y, phase='after_force')


def validated_window_point_click(adapter, context, x, y, *, clicks=1, activate=True):
    """校验屏幕坐标归属后，向已核验窗口发送 Qt 可识别的鼠标点击。"""
    calendar_popup = getattr(getattr(context.window, 'control', None), 'ClassName', '') == 'mmui::XPopover'
    ensure_clickable(adapter, context, x, y, activate=activate)
    point = wintypes.POINT(x, y)
    user32 = _user32()
    if not user32.ScreenToClient(context.window.handle, ctypes.byref(point)):
        raise ChatAutomationError('无法把按钮坐标转换为微信窗口坐标')
    lparam = ((point.y & 0xffff) << 16) | (point.x & 0xffff)
    if calendar_popup:
        # 日期面板依赖悬停状态；直接投递按钮事件可能仍选中上一次日期。
        if not user32.PostMessageW(context.window.handle, 0x0200, 0, lparam):
            raise ChatAutomationError('无法更新已核验日历的悬停位置')
        time.sleep(0.1)
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


def validated_window_wheel(adapter, context, control, *, older, steps, activate=True):
    """在已核验列表中心投递滚轮消息，不依赖远控软件维持物理鼠标。"""
    left, top, right, bottom = rect_to_tuple(control.BoundingRectangle)
    x, y = (left + right) // 2, (top + bottom) // 2
    ensure_clickable(adapter, context, x, y, activate=activate)
    delta = 120 if older else -120
    wparam = (delta & 0xffff) << 16
    lparam = ((y & 0xffff) << 16) | (x & 0xffff)
    user32 = _user32()
    for _ in range(steps):
        if not user32.PostMessageW(context.window.handle, 0x020A, wparam, lparam):
            raise ChatAutomationError('无法向微信窗口发送滚轮消息')
        time.sleep(0.05)


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
    parser.add_argument('--locate-only', action='store_true',
                        help='仅定位指定消息并核验预览 ID，不点击原图或写入下载结果')
    parser.add_argument('--diagnostic-log', type=Path, help='逐页列表与预览身份诊断 JSONL')
    return parser.parse_args()


def one_window(class_name, *, session_id=None):
    """只接受属于当前微信会话的唯一可见窗口。"""
    matches = []
    for window in enumerate_desktop_windows():
        if (window.process_name.lower() != 'weixin.exe' or not window.visible or
                window.is_minimized):
            continue
        attached = attach_uia_control(window)
        control = attached.control
        if control is None or control.ClassName != class_name:
            continue
        if session_id and session_id not in control.AutomationId:
            continue
        matches.append(attached)
    return unique(matches, class_name)


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


def calendar_day_point(day, panel_bounds, *, sunday_first=False):
    """按星期列和月内行计算日期格中心，适配日历的 DPI 缩放。"""
    left, top, right, _ = panel_bounds
    scale = (right - left) / 424
    offset = 1 if sunday_first else 0
    column = (day.weekday() + offset) % 7
    row = ((date(day.year, day.month, 1).weekday() + offset) % 7 + day.day - 1) // 7
    return (round(left + (20 + 64 * column) * scale),
            round(top + (84 + 64 * row) * scale))


def fit_calendar_popup(adapter, calendar, chat):
    """Fit this task's popup to its reported content after a disconnected display change.

    Qt can retain enlarged child layout while creating a smaller native popup.
    Resize only that verified popup; never accept points outside its real HWND.
    """
    transport = vars(adapter).get('_message_transport')
    if transport is None or calendar.control.ClassName != 'mmui::XPopover':
        return calendar
    context = context_for(calendar, chat)
    bounds = transport.verify_window(context)
    children = [rect_to_tuple(n.BoundingRectangle) for n in walk(calendar.control)[1:]]
    children = [r for r in children if r[2] > r[0] and r[3] > r[1]]
    if not children or all(bounds[0] <= r[0] and bounds[1] <= r[1]
                           and r[2] <= bounds[2] and r[3] <= bounds[3] for r in children):
        return calendar
    if any(r[0] < bounds[0] or r[1] < bounds[1] for r in children):
        raise ChatAutomationError('日期弹层内容超出左上边界，停止调整')
    width = max(r[2] for r in children) - bounds[0]
    height = max(r[3] for r in children) - bounds[1]
    if not 0 < width <= 1200 or not 0 < height <= 1600:
        raise ChatAutomationError('日期弹层内容尺寸异常，停止调整')
    user32 = _user32()
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.SetWindowPos.restype = wintypes.BOOL
    if not user32.SetWindowPos(calendar.handle, 0, bounds[0], bounds[1], width, height, 0x14):
        raise ChatAutomationError('无法调整本任务日期弹层')
    time.sleep(.2)
    refreshed = one_window('mmui::XPopover')
    if refreshed.handle != calendar.handle:
        raise ChatAutomationError('日期弹层身份改变，停止调整')
    actual = transport.verify_window(context_for(refreshed, chat))
    if actual[2] < bounds[0] + width or actual[3] < bounds[1] + height:
        raise ChatAutomationError('日期弹层未容纳完整内容')
    return refreshed


def select_day(adapter, history, chat, session_id, day, *, sunday_first=False):
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
    calendar = fit_calendar_popup(adapter, calendar, chat)
    context = context_for(calendar, chat)
    nodes = walk(calendar.control)
    month = next((node.Name for node in nodes if node.ControlTypeName == 'TextControl'
                  and re.fullmatch(r'\d+月', node.Name)), None)
    if month != f'{day.month}月':
        combos = [node for node in nodes if node.ControlTypeName == 'ComboBoxControl']
        validated_window_click(adapter, context, unique(combos[1:2], '月份下拉框'))
        menu = one_window('mmui::XMenu')
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
            # 临时菜单不能激活；向已核验窗口投递滚轮后重新读取选项矩形。
            validated_window_wheel(adapter, menu_context, menu.control,
                                   older=center_y < viewport[1] + 8, steps=1, activate=False)
            time.sleep(0.2)
        else:
            raise ChatAutomationError(f'月份菜单目标 {day.month}月未进入可见区域；'
                                      f'选项={rect}，菜单={bounds}')
        validated_window_point_click(adapter, menu_context, (rect[0] + rect[2]) // 2,
                                     center_y, activate=False)
        for _ in range(15):
            calendar = one_window('mmui::XPopover')
            if any(node.ControlTypeName == 'TextControl' and node.Name == f'{day.month}月'
                   for node in walk(calendar.control)):
                break
            time.sleep(0.2)
        else:
            raise ChatAutomationError(f'月份菜单未切换到 {day.month}月')
        context = context_for(calendar, chat)
    panel = unique([node for node in walk(calendar.control) if node.ClassName == 'mmui::DateNumPanel'],
                   '日历日期面板')
    panel_bounds = rect_to_tuple(panel.BoundingRectangle)
    x, y = calendar_day_point(day, panel_bounds,
                              sunday_first=sunday_first)
    print(f'{day}: 日历面板={panel_bounds}，日期坐标=({x}, {y})，周日首列={sunday_first}', flush=True)
    validated_window_point_click(adapter, context, x, y)
    time.sleep(0.2)
    try:
        remaining = one_window('mmui::XPopover')
    except ChatAutomationError as exc:
        if '实际找到 0 个' not in str(exc):
            raise
        print(f'{day}: 日期点击后弹层已关闭', flush=True)
    else:
        if remaining.handle != calendar.handle:
            raise ChatAutomationError('日期弹层已改变，停止确认')
        confirms = [node for node in walk(remaining.control)
                    if node.ControlTypeName == 'ButtonControl' and node.Name == '确定']
        if confirms:
            print(f'{day}: 确认日期选择', flush=True)
            validated_window_click(adapter, context_for(remaining, chat),
                                   unique(confirms, '日期确认按钮'))
    heading = f'{day.year}年{day.month}月{day.day}日'
    try:
        return wait_for_date_heading(session_id, heading)
    except ChatAutomationError:
        # 当天未匹配不是星期列错位，按周日首列重试只会点到别的日期。
        if sunday_first or day == date.today():
            raise
        print(f'{day}: 日期筛选未匹配，按周日首列重试', flush=True)
        return select_day(adapter, history, chat, session_id, day, sunday_first=True)


def wait_for_date_heading(session_id, heading):
    """用日期标题或附件行末日期核验结果，兼容顶部保留前一天分隔条。"""
    today = date.today()
    target_day = message_day(heading, today)
    observed = set()
    for _ in range(15):
        time.sleep(0.2)
        history = one_window('mmui::SearchMsgUniqueChatWindow', session_id=session_id)
        nodes = walk(history.control)
        labels = {node.Name for node in nodes
                  if node.ControlTypeName == 'TextControl'}
        if heading in labels:
            return history
        # 跳到稀疏消息日期时，微信可能沿用上一条分隔标题；附件时间仍是目标日。
        for listing in nodes:
            if listing.ControlTypeName != 'ListControl' or listing.AutomationId != 'search_message_list':
                continue
            for row in listing.GetChildren():
                if not row.Name.startswith(('图片 ', '文件 ', '聊天记录')):
                    continue
                stamp = ROW_DATE.search(row.Name)
                if stamp and target_day is not None and message_day(stamp.group(1), today) == target_day:
                    return history
                # 微信只给当天消息显示纯时分；目标日是今天时它就是目标日的附件。
                if not stamp and target_day == today and ROW_TIME_ONLY.search(row.Name):
                    return history
        observed.update(name for name in labels
                        if re.fullmatch(r'\d{4}年\d{1,2}月\d{1,2}日', name))
    shown = ', '.join(sorted(observed)) or '未显示日期标题'
    raise ChatAutomationError(f'未确认日期筛选结果为 {heading}；页面日期={shown}，停止处理')


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
    preview = None
    for _ in range(2):
        validated_window_point_click(adapter, context, x, y, clicks=2)
        deadline = time.monotonic() + 2
        while True:
            try:
                preview = one_window('mmui::PreviewWindow', session_id=session_id)
                break
            except ChatAutomationError as exc:
                if '实际找到 0 个' not in str(exc) or time.monotonic() >= deadline:
                    break
            time.sleep(0.1)
        if preview is not None:
            break
    if preview is None:
        return None
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


def write_diagnostic(path, event, **data):
    """将定位现场写到独立日志，不改清单、原图缓存或导出。"""
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8', newline='\n') as stream:
        stream.write(json.dumps({'at': datetime.now().isoformat(), 'event': event, **data},
                                ensure_ascii=False) + '\n')


def run_day(adapter, history, chat, session_id, day, targets, export_root, export_chat, limit,
            *, locate_only=False, diagnostic_log=None):
    """列表末尾仍缺目标时重建查询一次，不在同一结果集上空滚。"""
    handled = 0
    for attempt in range(2):
        handled += scan_day(adapter, history, chat, session_id, day, targets,
                            export_root, export_chat, limit - handled if limit else 0,
                            locate_only=locate_only, diagnostic_log=diagnostic_log,
                            attempt=attempt)
        if not targets or (limit and handled >= limit) or adapter.cancelled():
            break
        if attempt == 0:
            print(f'{day}: 列表扫描仍有 {len(targets)} 张未定位，关闭旧记录窗口并重建日期查询一次',
                  flush=True)
            write_diagnostic(diagnostic_log, 'rebuild_date_query', old_handle=history.handle,
                             remaining_keys=sorted(item['key'] for item in targets.values()))
            close_history(adapter, chat, session_id)
            history = history_window(adapter, adapter.open_chat(chat), chat, session_id)
    return handled


def scan_day(adapter, history, chat, session_id, day, targets, export_root, export_chat, limit,
             *, locate_only=False, diagnostic_log=None, attempt=0):
    """只打开目标分钟的图片，用预览 ID 精确匹配待补消息。"""
    history = select_day(adapter, history, chat, session_id, day)
    target_minutes = {f'{datetime.fromtimestamp(item["timestamp"]).hour:02d}:'
                      f'{datetime.fromtimestamp(item["timestamp"]).minute:02d}'
                      for item in targets.values()}
    examined = set()
    observed_image_minutes = set()
    unmatched_image_labels = set()
    previous_layout = None
    stationary = 0
    handled = 0
    stop_reason = 'page_limit'
    for page in range(300):
        if adapter.cancelled():
            raise ChatAutomationError('已取消')
        if not targets or (limit and handled >= limit):
            stop_reason = 'item_limit' if targets else 'complete'
            break
        history = one_window('mmui::SearchMsgUniqueChatWindow', session_id=session_id)
        listing = unique([node for node in walk(history.control) if node.AutomationId == 'search_message_list'],
                         '筛选消息列表')
        rows = list(listing.GetChildren())
        layout = tuple((node.Name, rect_to_tuple(node.BoundingRectangle)) for node in rows)
        listing_rect = rect_to_tuple(listing.BoundingRectangle)
        if diagnostic_log is not None:
            write_diagnostic(diagnostic_log, 'date_list_page', page=page, attempt=attempt,
                             window_handle=history.handle, session_id=session_id,
                             day=day.isoformat(), listing_rect=listing_rect,
                             rows=[{'name': node.Name[:2000], 'tail': node.Name[-100:],
                                    'automation_id': node.AutomationId,
                                    'rect': rect_to_tuple(node.BoundingRectangle)} for node in rows])
        stationary = stationary + 1 if layout == previous_layout else 0
        if stationary >= 2:
            stop_reason = 'stationary'
            break
        previous_layout = layout
        for row in rows:
            if not targets or (limit and handled >= limit):
                break
            match = IMAGE_ROW.fullmatch(row.Name)
            if match:
                observed_image_minutes.add(match.group(1))
            elif row.Name.startswith('图片') and len(unmatched_image_labels) < 6:
                unmatched_image_labels.add(row.Name[:80])
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
                if locate_only:
                    automation_id = preview.control.AutomationId
                    write_diagnostic(diagnostic_log, 'preview_verified',
                                     session_id=session_id, message_id=targets[key]['message_id'],
                                     timestamp=timestamp, local_id=local_id, automation_id=automation_id)
                    print(f'仅定位核验 message_id={targets[key]["message_id"]} '
                          f'local_id={local_id} AutomationId={automation_id}', flush=True)
                    handled += 1
                    del targets[key]
                    target_minutes = {f'{datetime.fromtimestamp(item["timestamp"]):%H:%M}'
                                      for item in targets.values()}
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
            validated_window_wheel(adapter, context_for(history, chat), listing,
                                   older=False, steps=3)
            time.sleep(0.2)
    if targets:
        remaining_minutes = sorted({f'{datetime.fromtimestamp(item["timestamp"]):%H:%M}'
                                    for item in targets.values()})
        matched_minutes = sorted(set(remaining_minutes) & observed_image_minutes)
        print(f'{day}: 未定位目标分钟={remaining_minutes} '
              f'列表命中分钟={matched_minutes} '
              f'无法解析图片行={sorted(unmatched_image_labels)}', flush=True)
        diagnostic = {
            'event': 'unlocated',
            'at': datetime.now().isoformat(timespec='seconds'),
            'session_id': session_id,
            'day': day.isoformat(),
            'target_keys': sorted(item['key'] for item in targets.values()),
            'target_minutes': remaining_minutes,
            'matched_minutes': matched_minutes,
            'unmatched_image_labels': sorted(unmatched_image_labels),
            'attempt': attempt,
            'stop_reason': stop_reason,
            'scan_pages': page + 1,
            'window_handle': history.handle,
            'listing_rect': listing_rect,
            # 使用当页快照；滚动或关闭预览后，旧 UIA 行可能已经重建。
            'last_rows': [{'name': name[:200], 'tail': name[-100:], 'rect': bounds}
                          for name, bounds in layout],
        }
        path = diagnostic_log if locate_only else LOG_DIR / 'targeted_image_unlocated.jsonl'
        if path is None:
            return handled
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8', newline='\n') as log:
            log.write(json.dumps(diagnostic, ensure_ascii=False) + '\n')
    return handled


def main():
    """按缺口日期运行，结束后重新核验清单摘要。"""
    configure_output_encoding()
    print(f'微信定向原图补齐 {__version__}', flush=True)
    args = parse_args()
    if args.max_items < 0:
        raise ValueError('--max-items 不能为负数')
    if args.locate_only and (not args.target_ids_file or not args.diagnostic_log):
        raise ValueError('--locate-only 必须指定 --target-ids-file 和 --diagnostic-log')
    export_chat = args.export_chat or args.chat
    session_id, items = load_inventory(args.export_root, export_chat, since=args.since)
    if args.target_ids_file:
        target_ids = {line.strip() for line in args.target_ids_file.read_text(encoding='utf-8-sig').splitlines()
                      if line.strip()}
        if not target_ids:
            raise ValueError('目标消息 ID 文件为空')
        items = [item for item in items if f'{item["session_id"]}:{item["message_id"]}' in target_ids]
        if args.locate_only:
            found_ids = {f'{item["session_id"]}:{item["message_id"]}' for item in items
                         if item['kind'] == 'image'}
            if found_ids != target_ids:
                raise ValueError(f'导出中未找到指定图片消息 ID：{sorted(target_ids - found_ids)}')
    pending = {(item['timestamp'], item['local_id']): item for item in items
               if item['kind'] == 'image' and (args.locate_only or item['state'] != 'original')}
    if not args.locate_only and not args.retry_expired:
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
                                       args.max_items - handled if args.max_items else 0,
                                       locate_only=args.locate_only, diagnostic_log=args.diagnostic_log)
                    if targets:
                        print(f'{day}: 尚未定位 {len(targets)} 张', flush=True)
                print(f'本次已核验 {handled} 张；重新运行会按当前落盘状态选缺口。', flush=True)
                if args.locate_only and any(by_day.values()):
                    raise ChatAutomationError('只定位验证未找到全部指定图片，不能判为验证通过')
            finally:
                try:
                    close_preview(adapter, args.chat, session_id)
                finally:
                    close_history(adapter, args.chat, session_id)


if __name__ == '__main__':
    main()
