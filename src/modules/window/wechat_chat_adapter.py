# encoding: UTF-8
"""微信聊天 UIA 适配器；控件不可见、身份不明确时停止，不用盲点兜底。"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
from datetime import date, timedelta
import hashlib
import re
from threading import Event
import time
from typing import Any

from src.modules.window.home_window_activation import WindowsHomeWindowGuard
from src.modules.window.wechat_home_window_finder import attach_uia_control, enumerate_desktop_windows, rect_to_tuple
from src.services.chat_attachment_service import ChatAutomationError, ChatMessage, ChatPage


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
    match = re.fullmatch(r"(\d{4})[年/.-](\d{1,2})[月/.-](\d{1,2})日?(?:\s+\d{1,2}:\d{2})?", text)
    if match:
        try:
            return date(*map(int, match.groups()))
        except ValueError:
            return None
    for label, days in (("今天", 0), ("昨天", 1), ("前天", 2)):
        if re.fullmatch(label + r"(?:\s+\d{1,2}:\d{2})?", text):
            return today - timedelta(days=days)
    # 不将缺少年份/日期的时刻擅自补成今天，以免越过用户边界。
    if re.fullmatch(r"\d{1,2}:\d{2}", text):
        return inherited
    return None


def attachment_kind(nodes: list[Any]) -> tuple[str, Any | None]:
    """只从消息气泡识别类型，正文提到“图片/文件”不触发点击。"""
    bubbles = [n for n in nodes if n.ClassName == "mmui::ChatBubbleItemView"]
    for bubble in bubbles:
        label = bubble.Name.strip()
        for kind, prefixes in (("forward", ("聊天记录", "聊天記錄", "Chat History")),
                               ("image", ("图片", "圖片", "Image")),
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

    def diagnose(self) -> dict:
        """仅检查控件可读性，不点击、切换对话或收集聊天正文。"""
        windows = [w for w in enumerate_desktop_windows()
                   if w.process_name.lower() in {"weixin.exe", "wechat.exe"}
                   and w.title in {"微信", "Weixin", "WeChat"} and w.visible and not w.is_minimized]
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

    def _click(self, context: ChatContext, control: Any, *, double: bool = False) -> None:
        """激活并核验点击坐标所属窗口；取消时不点击。"""
        if self.cancelled():
            raise ChatAutomationError("已取消")
        self._nodes(context)
        self.guard.activate(context.window)
        rect = rect_to_tuple(control.BoundingRectangle)
        x, y = (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2
        from types import SimpleNamespace
        self.guard.ensure_target_clickable(context.window, SimpleNamespace(
            home_window_handle=context.window.handle, click_x=x, click_y=y))
        if double:
            control.DoubleClick(simulateMove=False)
        else:
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
        self._click(context, search)
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
        self._pause(0.5)
        context.name = name
        self._nodes(context)
        return context

    def chat_name(self, context: ChatContext) -> str:
        """返回已经核验的对话名称。"""
        return context.name

    def read_page(self, context: ChatContext) -> ChatPage:
        """读取完整可见消息行；不把同内容不同消息合并。"""
        listing = self._list(context)
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
            if row.ClassName == "mmui::ChatItemView" and message_day(row.Name, self.today, inherited):
                inherited = message_day(row.Name, self.today, inherited)
                continue
            bounds = rect_to_tuple(row.BoundingRectangle)
            if row.IsOffscreen or bounds[1] < viewport[1] or bounds[3] > viewport[3] or bounds[3] <= bounds[1]:
                continue
            kind, bubble = attachment_kind(nodes)
            # 不解析消息正文里的日期，防止把正文当作聊天时间。
            day = inherited
            # 整条消息文本作为重叠锚点；下载状态按钮不进入锚点。
            content = row.Name or "\n".join(n.Name for n in nodes if n.ClassName.startswith("mmui::Chat"))
            if kind == "file":
                content = "\n".join(line for line in content.splitlines()
                                    if line.strip() not in DOWNLOAD_NAMES | AVAILABLE_NAMES |
                                    {"未下载", "已下载", "下载完成", "下载中", "Downloaded"})
            signature = hashlib.sha256((row.ClassName + "\n" + content).encode("utf-8")).hexdigest()
            messages.append(ChatMessage(signature, kind, day, (tuple(row.GetRuntimeId()), bubble)))
        at_start = False
        pattern = listing.GetScrollPattern()
        if pattern is not None:
            # 外层到达滚动顶部可能仍在加载更早记录，不能当作历史结束。
            at_start = context.forwarded and (not pattern.VerticallyScrollable or pattern.VerticalScrollPercent == 0)
        if not messages and listing.GetChildren():
            raise ChatAutomationError("消息列表有内容，但未识别到完整可见消息行，停止以免漏扫")
        return ChatPage(tuple(messages), at_start)

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
        self.guard.activate(context.window)
        if self.cancelled():
            return
        pattern = listing.GetScrollPattern()
        if pattern is None or not pattern.VerticallyScrollable:
            raise ChatAutomationError("消息列表不支持可验证的滚动，已停止")
        pattern.Scroll(self.auto.ScrollAmount.NoAmount, self.auto.ScrollAmount.SmallDecrement)
        self._pause(0.6)

    def _child_window(self, context: ChatContext, message: ChatMessage, class_name: str) -> ChatContext:
        """只接管本次点击新产生、同微信进程的指定类型窗口。"""
        before = {w.handle for w in enumerate_desktop_windows()}
        bubble = self._fresh_target(context, message)
        self._click(context, bubble, double=(class_name == PREVIEW_CLASS))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not self.cancelled():
            matches = []
            parent_pid = context.window.control.ProcessId
            for w in enumerate_desktop_windows():
                if w.handle in before or not w.visible or w.process_name.lower() not in {"weixin.exe", "wechat.exe"}:
                    continue
                candidate = attach_uia_control(w)
                if candidate.control.ProcessId == parent_pid and candidate.control.ClassName == class_name:
                    matches.append(candidate)
            if matches:
                return ChatContext(unique(matches, "附件子窗口"), context.name, True)
            self._pause(0.2)
        raise ChatAutomationError("未确认新附件窗口，停止任务；请手动检查当前微信界面")

    def open_forward(self, context: ChatContext, message: ChatMessage) -> ChatContext:
        """进入合并转发，先定位列表底部，再按倒序遍历。"""
        child = self._child_window(context, message, FORWARD_CLASS)
        try:
            listing = self._list(child)
            pattern = listing.GetScrollPattern()
            if pattern is not None and pattern.VerticallyScrollable:
                pattern.SetScrollPercent(-1, 100)
                self._pause(0.5)
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
                n.Name.strip() in names and not n.IsOffscreen]

    def download(self, context: ChatContext, message: ChatMessage, timeout: float) -> str:
        """图片使用原图按钮；文件只点击下载按钮，不打开文件。"""
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

    def _download_file(self, context: ChatContext, message: ChatMessage, timeout: float) -> str:
        """通过气泡内明确的下载按钮下载；不双击，以免执行已下载附件。"""
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
                if not buttons:
                    return "unconfirmed"
                self._click(context, unique(buttons, "文件下载按钮"))
                clicked = True
            self._pause(0.25)
        return "cancelled" if self.cancelled() else "unconfirmed"
