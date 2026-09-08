# encoding: UTF-8
"""微信聊天 UIA 适配器；控件不可见、身份不明确时停止，不用盲点兜底。"""
from __future__ import annotations

import ctypes
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, timedelta
import hashlib
from pathlib import Path
import re
from threading import Event
import time
from typing import Any

from src.modules.window.home_window_activation import WindowsHomeWindowGuard
from src.modules.window.article_date_filter import normalize_home_date_text
from src.modules.window.wechat_accessibility import temporary_accessibility
from src.modules.window.wechat_home_window_finder import attach_uia_control, enumerate_desktop_windows, rect_to_tuple
from src.services.chat_attachment_service import ChatAutomationError, ChatMessage, ChatPage, older_prefix, page_identity


MESSAGE_LIST_NAMES = {"消息", "Messages", "訊息"}
FORWARD_CLASS = "mmui::RecordDetailWindow"
PREVIEW_CLASS = "mmui::PreviewWindow"
DOWNLOAD_NAMES = {"下载", "Download", "下載"}
ORIGINAL_NAMES = {"查看原图", "查看原圖", "View Original", "下载原图"}
AVAILABLE_NAMES = {"打开", "Open", "打开文件夹", "Open Folder", "另存为", "Save As"}


def walk(control: Any, limit: int = 2500) -> list[Any]:
    """有界遍历控件；超限明确报错，不返回可能漏消息的半棵树。"""
    result, pending = [], [control]
    while pending:
        node = pending.pop()
        result.append(node)
        if len(result) > limit:
            raise ChatAutomationError("控件数量超出诊断上限，停止扫描")
        pending.extend(reversed(node.GetChildren()))
    return result


def unique(nodes: list[Any], label: str) -> Any:
    """只有唯一命中才允许操作。"""
    if len(nodes) != 1:
        raise ChatAutomationError(f"{label}需要唯一匹配，实际找到 {len(nodes)} 个")
    return nodes[0]


def message_day(text: str, today: date, inherited: date | None = None) -> date | None:
    """解析完整日期或明确相对日期；纯时刻只使用已知日期归属。"""
    text = text.strip()
    # 点击时间标签后，微信会显示“9月8日 星期二 12:09”。
    text = re.sub(r'(?<=日)\s+星期[一二三四五六日天](?=\s|$)', '', text)
    match = re.fullmatch(r"(\d{4})[年/.-](\d{1,2})[月/.-](\d{1,2})日?(?:\s+\d{1,2}:\d{2})?", text)
    if match:
        try:
            return date(*map(int, match.groups()))
        except ValueError:
            return None
    for label, days in (("今天", 0), ("昨天", 1), ("前天", 2)):
        if re.fullmatch(label + r"(?:\s+\d{1,2}:\d{2})?", text):
            return today - timedelta(days=days)
    calendar = re.fullmatch(r'((?:星期[一二三四五六日天])|(?:\d{1,2}月\d{1,2}日))(?:\s+\d{1,2}:\d{2})?', text)
    if calendar:
        return normalize_home_date_text(calendar.group(1), today=today)
    # 不将缺少年份/日期的时刻擅自补成今天，以免越过用户边界。
    if re.fullmatch(r"\d{1,2}:\d{2}", text):
        return inherited
    return None


def attachment_kind(nodes: list[Any]) -> tuple[str, Any | None]:
    """只从消息气泡识别类型，正文提到“图片/文件”不触发点击。"""
    bubbles = [n for n in nodes if n.ClassName in {"mmui::ChatBubbleItemView", "mmui::ChatBubbleReferItemView"}]
    for bubble in bubbles:
        label = bubble.Name.strip()
        if label.startswith(('聊天记录', '聊天記錄', 'Chat History')):
            return 'forward', bubble
        for kind, prefixes in (("image", ("图片", "圖片", "Image")),
                               ("file", ("文件", "檔案", "File"))):
            if any(label == p or label.startswith(p + "\n") or label.startswith(p + " ") or
                   label.startswith(p + "：") or label.startswith(p + ":") for p in prefixes):
                return kind, bubble
    return "text", None


@dataclass
class ChatContext:
    window: Any
    name: str
    forwarded: bool = False
    reached_start: bool = False
    dates: dict[tuple, date] = field(default_factory=dict)
    expanded_dates: set[tuple] = field(default_factory=set)
    identities: dict[tuple, tuple] = field(default_factory=dict)
    resolve_dates: bool = False


class WechatChatAdapter:
    """支持暴露 mmui 控件树的 Windows 微信；F9 或停止事件随时取消。"""

    def __init__(self, stop: Event | None = None) -> None:
        """延迟导入平台依赖，允许其他平台运行规则测试。"""
        try:
            import uiautomation as auto
        except ImportError as exc:
            raise ChatAutomationError("缺少 uiautomation，请使用项目 .venv 环境") from exc
        self.auto = auto
        self.stop = stop or Event()
        self.guard = WindowsHomeWindowGuard()
        self.main = None
        self.today = date.today()

    def cancelled(self) -> bool:
        """检查停止按钮和 F9，不向其他窗口发送按键。"""
        if ctypes.windll.user32.GetAsyncKeyState(0x78) & 0x8000:
            self.stop.set()
        return self.stop.is_set()

    @contextmanager
    def compatibility(self, enabled: bool):
        """显式选择时启用当前进程兼容读取，任务结束恢复运行时标志。"""
        if not enabled:
            yield
            return
        self.diagnose()
        with temporary_accessibility(self.main.control.ProcessId):
            yield

    def _pause(self, seconds: float) -> None:
        """短间隔等待以响应取消。"""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not self.cancelled():
            self.stop.wait(min(0.1, max(0, deadline - time.monotonic())))

    def _window(self, window: Any) -> Any:
        """每次读取都重新枚举句柄，避免使用失效窗口。"""
        candidates = [w for w in enumerate_desktop_windows()
                      if w.handle == window.handle and w.process_name.lower() in {"weixin.exe", "wechat.exe"}]
        current = unique(candidates, "微信窗口")
        if not current.visible or current.is_minimized:
            raise ChatAutomationError("微信窗口不可见或已最小化")
        return attach_uia_control(current)

    def _activate(self, context: ChatContext) -> None:
        """桌面不可交互时停止，在获得前台窗口后才允许输入或调用控件。"""
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        user32.OpenInputDesktop.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        user32.OpenInputDesktop.restype = ctypes.c_void_p
        user32.CloseDesktop.argtypes = [ctypes.c_void_p]
        desktop = user32.OpenInputDesktop(0, False, 0x100)
        if not desktop:
            raise ChatAutomationError('桌面不可交互，请解锁 Windows 并保持登录会话连接后重试')
        user32.CloseDesktop(desktop)
        self.guard.activate(context.window)
        if not user32.GetForegroundWindow():
            raise ChatAutomationError('未能获得可交互的前台窗口，停止操作')

    def diagnose(self) -> dict:
        """仅检查控件可读性，不点击、切换对话或收集聊天正文。"""
        windows = [w for w in enumerate_desktop_windows()
                   if w.process_name.lower() in {"weixin.exe", "wechat.exe"}
                   and w.title in {"微信", "Weixin", "WeChat"} and w.visible and not w.is_minimized
                   and w.rect[2] - w.rect[0] >= 400 and w.rect[3] - w.rect[1] >= 400]
        self.main = attach_uia_control(unique(windows, "微信主窗口"))
        nodes = walk(self.main.control)
        lists = [n for n in nodes if n.ControlTypeName == "ListControl" and n.Name in MESSAGE_LIST_NAMES]
        titles = [n for n in nodes if n.AutomationId.endswith("current_chat_name_label")]
        return {"ready": len(lists) == 1 and len(titles) == 1, "node_count": len(nodes),
                "message_lists": len(lists), "chat_titles": len(titles),
                "window_class": self.main.class_name,
                "detail": "可读取聊天控件" if len(lists) == 1 and len(titles) == 1 else
                "未识别到聊天消息列表和标题。请先打开对话；若只有 MMUIRenderSubWindowHW 外壳，当前微信未暴露 UIA 控件，不能运行。"}

    def _nodes(self, context: ChatContext) -> list[Any]:
        """刷新窗口和标题，外层对话切换后立即中断。"""
        context.window = self._window(context.window)
        nodes = walk(context.window.control)
        if not context.forwarded:
            title = unique([n for n in nodes if n.AutomationId.endswith("current_chat_name_label")], "当前对话标题")
            if title.Name.strip() != context.name:
                raise ChatAutomationError("当前对话已改变，停止任务")
        return nodes

    def _list(self, context: ChatContext) -> Any:
        """找到当前层级唯一的消息列表。"""
        nodes = self._nodes(context)
        if context.forwarded:
            return unique([n for n in nodes if n.ControlTypeName == "ListControl" and
                           n.ClassName == "mmui::RecyclerListView"], "转发内部消息列表")
        return unique([n for n in nodes if n.ControlTypeName == "ListControl" and
                       n.Name in MESSAGE_LIST_NAMES], "消息列表")

    def _click(self, context: ChatContext, control: Any, *, cleanup: bool = False) -> None:
        """核验窗口归属后点击；任务取消时仅允许关闭本任务的接收窗口。"""
        if self.cancelled() and not cleanup:
            raise ChatAutomationError("已取消")
        self._nodes(context)
        target_context = context
        handle = control.GetTopLevelControl().NativeWindowHandle
        if handle and handle != context.window.handle:
            owner = handle
            for _ in range(8):
                owner = ctypes.windll.user32.GetWindow(owner, 4)  # GW_OWNER
                if owner == context.window.handle:
                    break
                if not owner:
                    raise ChatAutomationError('操作控件不属于本次对话窗口')
            else:
                raise ChatAutomationError('无法确认弹出窗口归属')
            window = unique([w for w in enumerate_desktop_windows() if w.handle == handle], '控件弹出窗口')
            target_context = ChatContext(self._window(window), context.name, True)
            if target_context.window.control.ProcessId != context.window.control.ProcessId:
                raise ChatAutomationError('控件窗口进程改变')
        self._activate(target_context)
        rect = rect_to_tuple(control.BoundingRectangle)
        x, y = (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2
        from types import SimpleNamespace
        self.guard.ensure_target_clickable(target_context.window, SimpleNamespace(
            home_window_handle=target_context.window.handle, click_x=x, click_y=y))
        control.Click(simulateMove=False)

    def open_chat(self, name: str | None) -> ChatContext:
        """选择当前对话或精确搜索指定对话；不向聊天输入框输入文字。"""
        diagnostic = self.diagnose()
        if not diagnostic["ready"]:
            raise ChatAutomationError(diagnostic["detail"])
        nodes = walk(self.main.control)
        title = unique([n for n in nodes if n.AutomationId.endswith("current_chat_name_label")], "当前对话标题")
        context = ChatContext(self.main, title.Name.strip())
        if name is None or name == context.name:
            return context
        search = unique([n for n in nodes if n.ControlTypeName == "EditControl" and
                         n.ClassName == "mmui::XValidatorTextEdit" and n.Name in {"搜索", "Search", "搜尋"}], "对话搜索框")
        self._activate(context)
        search.SetFocus()
        self._pause(0.2)
        if not self._focus_within(search):
            raise ChatAutomationError('未确认焦点在对话搜索框，未输入搜索内容')
        pattern = search.GetValuePattern()
        if pattern is None:
            raise ChatAutomationError("搜索框不支持明确设置内容，未输入文字")
        pattern.SetValue(name)
        self._pause(1)
        nodes = self._nodes(context)
        results = unique([n for n in nodes if n.AutomationId == "search_list"], "对话搜索结果")
        matches = [row for row in results.GetChildren() if row.ControlTypeName == "ListItemControl" and
                   any(n.Name.strip() == name for n in walk(row))]
        self._click(context, unique(matches, f"对话“{name}”"))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not self.cancelled():
            context.window = self._window(context.window)
            titles = [n for n in walk(context.window.control)
                      if n.AutomationId.endswith("current_chat_name_label")]
            if len(titles) == 1 and titles[0].Name.strip() == name:
                context.name = name
                return context
            self._pause(0.2)
        raise ChatAutomationError("未在等待时间内确认目标对话已打开")

    def chat_name(self, context: ChatContext) -> str:
        """返回已经核验的对话名称。"""
        return context.name

    def read_page(self, context: ChatContext) -> ChatPage:
        """读取与视口相交的消息行，保留跨页长消息作为衔接锚点。"""
        listing = self._list(context)
        if context.resolve_dates:
            self._expand_dates(context, listing)
        viewport = rect_to_tuple(listing.BoundingRectangle)
        messages = []
        inherited = None
        for row in listing.GetChildren():
            nodes = walk(row)
            if row.ControlTypeName != "ListItemControl":
                # 只读取明确独立的时间标签，不在任意分组正文里寻找日期。
                if row.ControlTypeName == "TextControl":
                    parsed = message_day(row.Name, self.today, inherited)
                    if parsed:
                        inherited = parsed
                continue
            # 时间分隔条不是聊天消息，不能消耗用户指定的条数。
            if row.ClassName == "mmui::ChatItemView":
                # 时间行即使格式未知也不算聊天消息，并清除旧日期避免误归属。
                inherited = message_day(row.Name, self.today, None if context.resolve_dates else inherited)
                continue
            bounds = rect_to_tuple(row.BoundingRectangle)
            if row.IsOffscreen or bounds[3] <= viewport[1] or bounds[1] >= viewport[3] or bounds[3] <= bounds[1]:
                continue
            kind, bubble = attachment_kind(nodes)
            # 不解析消息正文里的日期，防止把正文当作聊天时间。
            raw_identity = tuple(row.GetRuntimeId())
            identity = context.identities.get(raw_identity, raw_identity)
            day = inherited or context.dates.get(identity)
            if day is not None:
                context.dates[identity] = day
            # 整条消息文本作为重叠锚点；下载状态按钮不进入锚点。
            content = row.Name or "\n".join(n.Name for n in nodes if n.ClassName.startswith("mmui::Chat"))
            if kind == "file":
                content = "\n".join(content.splitlines()[:2])
            signature = hashlib.sha256((row.ClassName + "\n" + content).encode("utf-8")).hexdigest()
            messages.append(ChatMessage(signature, kind, day, (identity, bubble)))
        at_start = context.reached_start
        pattern = listing.GetScrollPattern()
        if pattern is not None:
            # 外层到达滚动顶部可能仍在加载更早记录，不能当作历史结束。
            at_start = at_start or (context.forwarded and (not pattern.VerticallyScrollable or pattern.VerticalScrollPercent == 0))
        if not messages and listing.GetChildren():
            raise ChatAutomationError("消息列表有内容，但未识别到完整可见消息行，停止以免漏扫")
        return ChatPage(tuple(messages), at_start)

    def prepare_dates(self, context: ChatContext) -> None:
        """按日期运行时启用时间行展开；按条数运行不产生日期探查动作。"""
        context.resolve_dates = True

    def _expand_dates(self, context: ChatContext, listing: Any = None) -> None:
        """单击可见的缩略时间行，读取微信展开的日期；同一行只尝试一次。"""
        listing = self._list(context) if listing is None else listing
        viewport = rect_to_tuple(listing.BoundingRectangle)
        for row in listing.GetChildren():
            if row.ClassName != 'mmui::ChatItemView':
                continue
            identity = tuple(row.GetRuntimeId())
            bounds = rect_to_tuple(row.BoundingRectangle)
            if (identity in context.expanded_dates or row.IsOffscreen or
                    bounds[1] < viewport[1] or bounds[3] > viewport[3] or
                    re.search(r'\d+[年月/-]\d+', row.Name)):
                continue
            self._click(context, row)
            context.expanded_dates.add(identity)
            deadline = time.monotonic() + 1
            while message_day(row.Name, self.today) is None and time.monotonic() < deadline and not self.cancelled():
                self._pause(0.1)
            if message_day(row.Name, self.today) is None:
                raise ChatAutomationError('点击时间标签后仍未读到完整日期，停止以免错误归属')

    def resolve_day(self, context: ChatContext, message: ChatMessage) -> date | None:
        """补全日期；必要时向前寻找相邻时间行，再原路滚回并核验消息位置。"""
        target = message.control[0]
        if target in context.dates:
            return context.dates[target]
        self._expand_dates(context)
        original = self.read_page(context)
        if target in context.dates:
            return context.dates[target]
        # 只传播连续且尚无日期分隔的区间；每次滚动都核验相邻消息重叠。
        pending = {m.control[0] for m in original.messages if m.day is None}
        original_layout = self._message_layout(context, original)
        previous = original
        steps, stationary = 0, 0
        position = self._page_position(context)
        try:
            for _ in range(80):
                if self.cancelled():
                    return None
                self._wheel(context, older=True)
                steps += 1
                self._pause(0.2)
                self._expand_dates(context)
                current = self.read_page(context)
                if page_identity(current) != page_identity(previous):
                    older_prefix(previous, current)
                dates = {m.day for m in current.messages if m.control[0] in pending and m.day is not None}
                if len(dates) > 1:
                    raise ChatAutomationError('补全日期时区间出现多个日期，停止以免错误归属')
                if dates:
                    found = dates.pop()
                    context.dates.update({key: found for key in pending})
                    return found
                pending.update(m.control[0] for m in current.messages if m.day is None)
                current_position = self._page_position(context)
                stationary = stationary + 1 if current_position == position else 0
                if stationary >= 3:
                    break
                previous, position = current, current_position
            return None
        finally:
            if not self.cancelled():
                for _ in range(steps):
                    self._wheel(context, older=False)
                    self._pause(0.2)
                restored = self.read_page(context)
                if page_identity(restored) != page_identity(original):
                    # 滚出视口的控件可能被重建；完整有序内容和逐行位置均恢复才重新绑定。
                    if self._message_layout(context, restored) != original_layout:
                        raise ChatAutomationError('补全日期后未能恢复原消息位置，停止以免漏扫')
                    for old, new in zip(original.messages, restored.messages):
                        old_id, new_id = old.control[0], new.control[0]
                        for raw, canonical in list(context.identities.items()):
                            if canonical == new_id:
                                context.identities[raw] = old_id
                        context.identities[new_id] = old_id
                        if new.day:
                            context.dates[old_id] = new.day

    def _message_layout(self, context: ChatContext, page: ChatPage) -> tuple:
        """读取各消息有序内容与矩形，用于日期探查后的整页返回核验。"""
        rectangles = {context.identities.get(key, key): bounds for key, bounds in self._page_position(context)}
        return tuple((m.signature, rectangles[m.control[0]]) for m in page.messages)

    def _fresh_target(self, context: ChatContext, message: ChatMessage) -> Any:
        """返回操作前重新定位的气泡；布局重建不凭旧坐标继续。"""
        messages = self.read_page(context).messages
        matches = [m for m in messages if m.signature == message.signature and m.control[0] == message.control[0]]
        target = unique(matches, "待处理消息")
        if target.control[1] is None:
            raise ChatAutomationError("没有可确认的附件气泡")
        return target.control[1]

    def scroll_older(self, context: ChatContext) -> None:
        """在消息列表内小幅向上滚动，保留翻页重叠锚点。"""
        listing = self._list(context)
        self._activate(context)
        if self.cancelled():
            return
        pattern = listing.GetScrollPattern()
        if pattern is not None and pattern.VerticallyScrollable:
            pattern.Scroll(self.auto.ScrollAmount.NoAmount, self.auto.ScrollAmount.SmallDecrement)
            self._pause(0.6)
            return
        # 微信未提供 ScrollPattern 时，使用视口内小幅滚轮，不依赖消息方向键。
        before = tuple((m.signature, m.control[0]) for m in self.read_page(context).messages)
        position = self._page_position(context)
        stationary = 0
        for _ in range(200):
            if self.cancelled():
                return
            self._wheel(context, older=True)
            self._pause(0.4)
            after = tuple((m.signature, m.control[0]) for m in self.read_page(context).messages)
            if after != before:
                return
            current_position = self._page_position(context)
            stationary = stationary + 1 if current_position == position else 0
            position = current_position
            if stationary >= 3:
                if context.forwarded:
                    context.reached_start = True
                    return
                break
        raise ChatAutomationError("消息列表没有向前移动，未确认已读完历史")

    def _page_position(self, context: ChatContext) -> tuple:
        """包含行几何位置，区分长消息内部滚动和真正停止移动。"""
        return tuple((tuple(row.GetRuntimeId()), rect_to_tuple(row.BoundingRectangle))
                     for row in self._list(context).GetChildren())

    def _wheel(self, context: ChatContext, *, older: bool, steps: int = 1) -> None:
        """只在已核验消息视口内部发送滚轮，重新校验窗口和取消状态。"""
        listing = self._list(context)
        self._activate(context)
        if self.cancelled():
            raise ChatAutomationError('已取消')
        bounds = rect_to_tuple(listing.BoundingRectangle)
        from types import SimpleNamespace
        self.guard.ensure_target_clickable(context.window, SimpleNamespace(
            home_window_handle=context.window.handle,
            click_x=(bounds[0] + bounds[2]) // 2, click_y=(bounds[1] + bounds[3]) // 2))
        action = listing.WheelUp if older else listing.WheelDown
        action(wheelTimes=steps, waitTime=0.1)

    def _focus_within(self, control: Any) -> bool:
        """列表焦点可以落在其选中消息上，但不能落入聊天输入框。"""
        target = tuple(control.GetRuntimeId())
        focused = self.auto.GetFocusedControl()
        for _ in range(8):
            if focused is None:
                return False
            if tuple(focused.GetRuntimeId()) == target:
                return True
            focused = focused.GetParentControl()
        return False

    def _child_window(self, context: ChatContext, message: ChatMessage, class_name: str) -> ChatContext:
        """只接管本次点击新产生、同微信进程的指定类型窗口。"""
        before = {w.handle for w in enumerate_desktop_windows()}
        bubble = self._fresh_target(context, message)
        if self.cancelled():
            raise ChatAutomationError('已取消')
        self._activate(context)
        self._open_bubble(context, message, bubble)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not self.cancelled():
            matches = []
            parent_pid = context.window.control.ProcessId
            for w in enumerate_desktop_windows():
                if w.handle in before or not w.visible or w.process_name.lower() not in {"weixin.exe", "wechat.exe"}:
                    continue
                candidate = attach_uia_control(w)
                if candidate.control.ProcessId == parent_pid and any(n.ClassName == class_name for n in walk(candidate.control)):
                    matches.append(candidate)
            if matches:
                return ChatContext(unique(matches, "附件子窗口"), context.name, True)
            self._pause(0.2)
        raise ChatAutomationError("未确认新附件窗口，停止任务；请手动检查当前微信界面")

    def _bubble_point(self, context: ChatContext, message: ChatMessage, bubble: Any) -> tuple[int, int]:
        """在已识别附件正文内定位可见内容，避开整行中心的空白和头像。"""
        if message.kind not in {'image', 'forward', 'file'}:
            raise ChatAutomationError('只允许定位附件气泡')
        self._activate(context)
        dpi = ctypes.windll.user32.GetDpiForWindow(context.window.handle)
        if dpi <= 0:
            raise ChatAutomationError('无法读取微信窗口缩放，未点击附件')
        # UIA 矩形使用物理像素；头像和昵称的排除距离必须随窗口 DPI 同步放大。
        side, header, inset = (round(value * dpi / 96) for value in (64, 36, 8))
        listing = self._list(context)
        viewport = rect_to_tuple(listing.BoundingRectangle)
        row = rect_to_tuple(bubble.BoundingRectangle)
        # 倒序扫描时最早一条可能刚露出底边；先让正文进入视口再定位。
        for _ in range(20):
            visible_height = min(row[3] - inset, viewport[3]) - max(row[1] + header, viewport[1])
            if visible_height >= side or row[1] >= viewport[1]:
                break
            self._wheel(context, older=True)
            self._pause(0.3)
            bubble = self._fresh_target(context, message)
            listing = self._list(context)
            viewport = rect_to_tuple(listing.BoundingRectangle)
            row = rect_to_tuple(bubble.BoundingRectangle)
        top, bottom = max(row[1] + header, viewport[1]), min(row[3] - inset, viewport[3])
        left, right = max(row[0] + side, viewport[0]), min(row[2] - side, viewport[2])
        if right - left < 24 or bottom - top < 24:
            raise ChatAutomationError('附件可见内容不足，请调整起始位置后重试')
        # 仅在已核验消息的正文区域采样；不持久化图像，不调用外部识别服务。
        with listing.ToBitmap(x=left - viewport[0], y=top - viewport[1],
                              width=right - left, height=bottom - top) as bitmap:
            background = bitmap.GetPixelColor(bitmap.Width - 1, bitmap.Height - 1)
            scores = []
            for y in range(12, bitmap.Height - 11, 12):
                for x in range(12, bitmap.Width - 11, 12):
                    score = sum(sum(abs(((bitmap.GetPixelColor(x + dx, y + dy) >> shift) & 255) -
                                        ((background >> shift) & 255)) for shift in (0, 8, 16))
                                for dx in (-8, 0, 8) for dy in (-8, 0, 8))
                    scores.append((score, x, y))
            score, x, y = max(scores, default=(0, 0, 0))
            if score < 450:
                raise ChatAutomationError('未能在附件消息内定位可见内容，未点击')
        fresh = self._fresh_target(context, message)
        if (rect_to_tuple(fresh.BoundingRectangle) != row or
                ctypes.windll.user32.GetDpiForWindow(context.window.handle) != dpi):
            raise ChatAutomationError('截图后消息位置或窗口缩放已改变，未点击')
        self._activate(context)
        from types import SimpleNamespace
        self.guard.ensure_target_clickable(context.window, SimpleNamespace(
            home_window_handle=context.window.handle, click_x=left + x, click_y=top + y))
        if self.cancelled():
            raise ChatAutomationError('已取消')
        return left + x - row[0], top + y - row[1]

    def _open_bubble(self, context: ChatContext, message: ChatMessage, bubble: Any) -> None:
        """仅双击图片和转发正文；文件不经过这个入口。"""
        if message.kind not in {'image', 'forward'}:
            raise ChatAutomationError('不允许双击文件')
        x, y = self._bubble_point(context, message, bubble)
        bubble.DoubleClick(x=x, y=y, simulateMove=False, waitTime=0.2)

    def open_forward(self, context: ChatContext, message: ChatMessage) -> ChatContext:
        """进入合并转发，先定位列表底部，再按倒序遍历。"""
        child = self._child_window(context, message, FORWARD_CLASS)
        try:
            listing = self._list(child)
            pattern = listing.GetScrollPattern()
            if pattern is not None and pattern.VerticallyScrollable:
                pattern.SetScrollPercent(-1, 100)
                self._pause(0.5)
            elif pattern is None:
                # 有限的转发窗口从底部处理，避免最后一条只露出发送者时就被计入并点击。
                position = self._page_position(child)
                stationary = 0
                for _ in range(100):
                    self._wheel(child, older=False, steps=10)
                    self._pause(0.3)
                    current = self._page_position(child)
                    stationary = stationary + 1 if current == position else 0
                    position = current
                    if stationary >= 2:
                        break
                else:
                    raise ChatAutomationError('未确认转发记录底部，停止扫描')
            return child
        except Exception:
            self.close_forward(child, context)
            raise

    def close_forward(self, child: ChatContext, parent: ChatContext) -> None:
        """关闭已确认的子窗口并核验返回位置，不发送全局 Escape。"""
        window = self._window(child.window)
        pattern = window.control.GetWindowPattern()
        if pattern is None:
            raise ChatAutomationError("子窗口不支持明确关闭；请手动关闭后再运行")
        pattern.Close()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if not any(w.handle == window.handle for w in enumerate_desktop_windows()):
                self._nodes(parent)
                return
            time.sleep(0.1)
        raise ChatAutomationError("附件窗口尚未关闭，停止后续操作")

    def _buttons(self, context: ChatContext, names: set[str]) -> list[Any]:
        """只查找明确可见的按钮，不把正文当作操作入口。"""
        return [n for n in self._nodes(context) if n.ControlTypeName == "ButtonControl" and
                (n.Name.strip() in names or (names == ORIGINAL_NAMES and any(
                    re.fullmatch(re.escape(label) + r'\s*[（(]\s*\d+(?:\.\d+)?\s*(?:[KMG]?B|字节)\s*[）)]',
                                 n.Name.strip(), re.IGNORECASE) for label in names))) and not n.IsOffscreen]

    def download(self, context: ChatContext, message: ChatMessage, timeout: float) -> str:
        """图片寻找原图按钮，文件下载后核验菜单；微信可能自动打开文档。"""
        if message.kind == "file":
            return self._download_file(context, message, timeout)
        child = self._child_window(context, message, PREVIEW_CLASS)
        try:
            deadline = time.monotonic() + timeout
            opened_at = time.monotonic()
            clicked = False
            while time.monotonic() < deadline and not self.cancelled():
                nodes = self._nodes(child)
                labels = {n.Name.strip() for n in nodes}
                if any("过期" in s or "被清理" in s or "expired" in s.lower() for s in labels):
                    return "expired"
                originals = self._buttons(child, ORIGINAL_NAMES)
                if originals and not clicked:
                    self._click(child, unique(originals, "查看原图按钮"))
                    clicked = True
                if labels & {"原图下载完成", "原图已下载", "Original downloaded"}:
                    return "confirmed"
                if not originals and not clicked and time.monotonic() - opened_at >= 2:
                    return "unconfirmed"
                self._pause(0.25)
            return "cancelled" if self.cancelled() else "unconfirmed"
        finally:
            self.close_forward(child, context)

    def browse_images(self, context: ChatContext, message: ChatMessage, clicks: int,
                      interval: float, progress: Any) -> str:
        """打开一张图片后连续点击上一张；仅记录动作，不推断日期和原图完成。"""
        child = self._child_window(context, message, PREVIEW_CLASS)
        try:
            progress('preview_opened')
            for index in range(clicks + 1):
                if self.cancelled():
                    return 'cancelled'
                originals = self._buttons(child, ORIGINAL_NAMES)
                if originals:
                    self._click(child, unique(originals, '查看原图按钮'))
                self._pause(interval)
                if self.cancelled():
                    return 'cancelled'
                if index == clicks:
                    return 'previous_limit'
                buttons = self._buttons(child, {'上一张', '上一張', 'Previous'})
                if not buttons:
                    return 'previous_unavailable'
                button = unique(buttons, '上一张按钮')
                if not button.IsEnabled:
                    return 'previous_unavailable'
                self._click(child, button)
                progress('previous_clicked')
        finally:
            self.close_forward(child, context)

    def _download_file(self, context: ChatContext, message: ChatMessage, timeout: float) -> str:
        """使用下载按钮或未下载文档的单击入口；已可用文件不再打开。"""
        deadline = time.monotonic() + timeout
        clicked = False
        while time.monotonic() < deadline and not self.cancelled():
            nodes = walk(self._fresh_target(context, message))
            labels = {n.Name.strip() for n in nodes}
            if any("过期" in s or "expired" in s.lower() for s in labels):
                return "expired"
            if labels & {"下载完成", "已下载", "Downloaded"}:
                return "confirmed" if clicked else "available"
            available = [n for n in nodes if n.ControlTypeName == "ButtonControl" and
                         n.Name.strip() in AVAILABLE_NAMES and not n.IsOffscreen]
            if available:
                return "confirmed" if clicked else "available"
            buttons = [n for n in nodes if n.ControlTypeName == "ButtonControl" and
                       n.Name.strip() in DOWNLOAD_NAMES and not n.IsOffscreen]
            if not clicked:
                missing = any(re.search(r'(?:^|\s)未下载(?:\s|$)', n.Name) for n in nodes)
                if not missing and not buttons:
                    return 'skipped_file'
                if not buttons:
                    lines = self._fresh_target(context, message).Name.splitlines()
                    # 本机微信未下载文档没有下载菜单，单击会下载后交给关联查看器。
                    # 只允许普通文档/压缩包；程序、脚本、快捷方式不使用这个回退入口。
                    if missing and len(lines) >= 2 and Path(lines[1]).suffix.lower() in {
                            '.pdf', '.docx', '.xlsx', '.pptx', '.txt', '.csv', '.zip', '.rar', '.7z'}:
                        bubble = self._fresh_target(context, message)
                        x, y = self._bubble_point(context, message, bubble)
                        try:
                            bubble.Click(x=x, y=y, simulateMove=False, waitTime=0.2)
                            return self._file_menu_download(context, message, max(1, deadline - time.monotonic()), clicked=True)
                        finally:
                            self._file_receipt(context, lines[1], accept=False)
                    return self._file_menu_download(context, message, timeout)
                self._click(context, unique(buttons, "文件下载按钮"))
                clicked = True
            self._pause(0.25)
        return "cancelled" if self.cancelled() else "unconfirmed"

    def _file_menu(self, context: ChatContext, message: ChatMessage) -> list[Any]:
        """在已核验文件正文内打开右键菜单，避开整行空白区。"""
        bubble = self._fresh_target(context, message)
        self._activate(context)
        if self.cancelled():
            return []
        x, y = self._bubble_point(context, message, bubble)
        bubble.RightClick(x=x, y=y, simulateMove=False, waitTime=0.1)
        self._pause(0.15)
        return [n for n in self._nodes(context) if n.ControlTypeName == 'MenuItemControl'
                and n.ClassName == 'mmui::XMenuView' and not n.IsOffscreen]

    def _dismiss_menu(self, context: ChatContext) -> None:
        """只有当前对话中仍有本任务菜单时才发送退出菜单的按键。"""
        if any(n.ControlTypeName == 'MenuItemControl' and n.ClassName == 'mmui::XMenuView'
               for n in self._nodes(context)):
            self._activate(context)
            self.auto.SendKeys('{Esc}')

    def _file_menu_download(self, context: ChatContext, message: ChatMessage, timeout: float, *, clicked: bool = False) -> str:
        """用明确的下载菜单项操作，已可用文件不打开。"""
        deadline = time.monotonic() + timeout
        while not self.cancelled() and time.monotonic() < deadline:
            if clicked:
                filename = self._fresh_target(context, message).Name.splitlines()[1]
                self._file_receipt(context, filename, accept=True)
            try:
                items = self._file_menu(context, message)
                names = {item.Name.strip() for item in items}
                if names & {'在文件夹中显示', 'Show in Folder'}:
                    return 'confirmed' if clicked else 'available'
                downloads = [item for item in items if item.Name.strip() in DOWNLOAD_NAMES]
                if not clicked:
                    if not downloads:
                        return 'unconfirmed'
                    item = unique(downloads, '文件下载菜单')
                    self._click(context, item)
                    clicked = True
            finally:
                self._dismiss_menu(context)
            self._pause(1)
        return 'cancelled' if self.cancelled() else 'unconfirmed'

    def _file_receipt(self, context: ChatContext, filename: str, *, accept: bool) -> None:
        """处理本次文件的接收窗口；精确匹配文件名，退出时只取消这个窗口。"""
        dialogs = [n for n in self._nodes(context) if n.ClassName == 'mmui::XDialog'
                   and any(c.ControlTypeName == 'TextControl' and c.Name == filename for c in walk(n))]
        if not dialogs:
            return
        dialog = unique(dialogs, '文件接收窗口')
        if accept and self.cancelled():
            return
        label = '接收文件' if accept else '取消'
        button = unique([n for n in walk(dialog) if n.ControlTypeName == 'ButtonControl' and n.Name == label], label)
        self._click(context, button, cleanup=not accept)
        self._pause(0.5)
