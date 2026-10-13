# encoding: UTF-8
# version: 2.1 @20260920: 按搜索分类匹配本地对话，排除网络搜索与聊天记录;
# version: 2.2 @20260920: 搜索弹层保留主窗口焦点，激活后重取搜索框;
# version: 2.3 @20260920: 拒绝复用运行时 ID 引起的跨页消息映射冲突;
# version: 2.4 @20260920: 日期探查按日期标签精确找回原页，单图预览失败继续扫描;
# version: 2.5 @20260921: 搜索结果重复时优先正式分类，不将“最常使用”副本当成重名会话;
# version: 2.6 @20260921: 仅使用会话列表搜索框，排除群成员侧栏的同名搜索框;
# version: 2.7 @20260923: 按消息签名缓存已补全日期，避免点图后控件重建重复回滚;
# version: 2.8 @20260923: 连续页面无日期分隔条时沿用最近核验日期，减少大图逐张回滚;
# version: 2.9 @20260923: 从公众号等非聊天页可通过左侧会话搜索切换到指定对话;
# version: 3.0 @20260923: 排除微信日期弹窗，前台切换瞬断时有限重试;
# version: 3.1 @20260926: 会话搜索结果支持无物理远控鼠标的窗口消息点击;
# version: 3.2 @20261012: 公众号任务兼容读屏、最小化恢复及独立账号窗口导航;
# version: 3.3 @20261013: 微信已在前台仍被局部遮挡时强制置顶并复查坐标;
"""微信聊天 UIA 适配器；控件不可见、身份不明确时停止，不用盲点兜底。"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, timedelta
import hashlib
from pathlib import Path
import re
from threading import Event
import time
from typing import Any

from src.modules.window.home_window_activation import HomeWindowNotClickableError, WindowsHomeWindowGuard
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


class AttachmentNotClickableError(ChatAutomationError):
    """输入尚未发送时的临时遮挡，允许重新读取附件后有限重试。"""


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


def local_chat_matches(rows: list[Any], name: str) -> list[Any]:
    """只返回本地对话分类中的精确名称匹配，未知分类不作为入口。"""
    preferred = {'群聊', '联系人', '公众号', 'Group Chats', 'Contacts', 'Official Accounts'}
    fallback = {'最常使用', 'Most Used'}
    allowed = preferred | fallback
    headers = allowed | {'搜索网络结果', '聊天记录', '更多搜索结果',
                         'Search Web', 'Chat History', 'More Search Results'}
    section = None
    matches = []
    fallback_matches = []
    for row in rows:
        label = row.Name.strip()
        if label in headers:
            section = label
            continue
        if section in allowed and row.ControlTypeName == 'ListItemControl' and any(
                node.Name.strip() == name for node in walk(row)):
            (matches if section in preferred else fallback_matches).append(row)
    return matches or fallback_matches


def unique(nodes: list[Any], label: str) -> Any:
    """只有唯一命中才允许操作。"""
    if len(nodes) != 1:
        raise ChatAutomationError(f"{label}需要唯一匹配，实际找到 {len(nodes)} 个")
    return nodes[0]


def is_chat_list_search(control: Any) -> bool:
    """只接受左侧会话列表内的搜索框。"""
    parent = control.GetParentControl()
    for _ in range(8):
        if parent is None:
            return False
        if parent.ClassName == 'mmui::ChatMasterView':
            return True
        parent = parent.GetParentControl()
    return False


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
    switching: bool = False
    reached_start: bool = False
    dates: dict[tuple, date] = field(default_factory=dict)
    signature_dates: dict[str, date] = field(default_factory=dict)
    scan_day: date | None = None
    expanded_dates: set[tuple] = field(default_factory=set)
    identities: dict[tuple, tuple] = field(default_factory=dict)
    resolve_dates: bool = False
    attachment_position: tuple[ChatPage, tuple, int] | None = None
    owned: bool = True
    ancestors: tuple[int, ...] = ()


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
        self._locate_main()
        with temporary_accessibility(self.main.control.ProcessId):
            yield

    def _locate_main(self) -> Any:
        """Locate the WeChat shell without requiring readable UIA descendants."""
        self.main = attach_uia_control(unique(self._main_windows(), "微信主窗口"))
        return self.main

    def _main_windows(self) -> list[Any]:
        """只匹配微信主壳；允许任务恢复最小化窗口，排除日期和账号弹窗。"""
        return [w for w in enumerate_desktop_windows()
                   if w.process_name.lower() in {"weixin.exe", "wechat.exe"}
                   and w.title in {"微信", "Weixin", "WeChat"}
                   and w.class_name == "Qt51514QWindowIcon" and w.visible
                   and (w.is_minimized or
                        w.rect[2] - w.rect[0] >= 400 and w.rect[3] - w.rect[1] >= 400)]

    def _search_edits(self, nodes: list[Any]) -> list[Any]:
        """只计数左侧会话搜索框，排除群成员搜索。"""
        return [n for n in nodes if n.ControlTypeName == 'EditControl'
                and n.ClassName == 'mmui::XValidatorTextEdit'
                and n.Name in {'搜索', 'Search', '搜尋'} and is_chat_list_search(n)]

    @contextmanager
    def account_accessibility(self, mode: str = 'auto', *, activate: bool = False):
        """必要时启用已核验兼容读屏，整个采集结束后恢复；诊断不激活窗口。"""
        self._locate_main()
        raw_nodes = walk(self.main.control)
        raw_count = len(self._search_edits(raw_nodes))
        enabled = mode == 'on' or mode == 'auto' and raw_count == 0
        with self.compatibility(enabled):
            # 空壳 UIA 的 SetActive 可能阻塞，先启用读屏，再激活并刷新窗口。
            if activate:
                self._activate(ChatContext(self.main, '', switching=True))
                self.main = self._window(self.main)
            nodes = walk(self.main.control) if enabled else raw_nodes
            count = len(self._search_edits(nodes))
            yield dict(wechat_ready=count == 1 and not self.main.is_minimized,
                       main_window_count=1, search_edit_count=count,
                       raw_search_edit_count=raw_count, node_count=len(nodes),
                       compatibility_used=enabled, accessibility=mode,
                       detail='微信搜索框可读' if count == 1 else '微信会话搜索框不可唯一识别')

    def account_diagnose(self, mode: str = 'auto') -> dict:
        """检查交互桌面及公众号搜索能力；不暴露聊天正文，不切换账号。"""
        result = dict(wechat_ready=False, main_window_count=0, search_edit_count=0,
                      compatibility_used=False, accessibility=mode)
        try:
            user32 = ctypes.WinDLL('user32', use_last_error=True)
            user32.OpenInputDesktop.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
            user32.OpenInputDesktop.restype = ctypes.c_void_p
            user32.CloseDesktop.argtypes = [ctypes.c_void_p]
            desktop = user32.OpenInputDesktop(0, False, 0x100)
            if not desktop:
                raise ChatAutomationError('桌面不可交互，请解锁 Windows')
            user32.CloseDesktop(desktop)
            windows = self._main_windows()
            result['main_window_count'] = len(windows)
            window = unique(windows, '微信主窗口')
            if window.is_minimized:
                raise ChatAutomationError('微信主窗口已最小化；任务可自动恢复后重查')
            with self.account_accessibility(mode) as probe:
                result.update(probe)
        except Exception as exc:
            result.update(wechat_ready=False, detail=f'{type(exc).__name__}: {exc}')
        return result

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

    def _activate(self, context: ChatContext, *, force: bool = False) -> None:
        """桌面不可交互时停止，在获得前台窗口后才允许输入或调用控件。"""
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        user32.OpenInputDesktop.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        user32.OpenInputDesktop.restype = ctypes.c_void_p
        user32.CloseDesktop.argtypes = [ctypes.c_void_p]
        desktop = user32.OpenInputDesktop(0, False, 0x100)
        if not desktop:
            raise ChatAutomationError('桌面不可交互，请解锁 Windows 并保持登录会话连接后重试')
        user32.CloseDesktop(desktop)
        if force:
            context.window = self._window(context.window)
        for _ in range(5):
            self.guard.activate(context.window, force=force)
            if user32.GetForegroundWindow():
                return
            self._pause(0.2)
        raise ChatAutomationError('未能获得可交互的前台窗口，停止操作')

    def diagnose(self) -> dict:
        """仅检查控件可读性，不点击、切换对话或收集聊天正文。"""
        self._locate_main()
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
        deadline = time.monotonic() + 5
        while True:
            context.window = self._window(context.window)
            nodes = walk(context.window.control)
            # 转发内部页、或正在按名字搜索切换会话时，不要求已有当前对话标题。
            if context.forwarded or context.switching:
                return nodes
            titles = [n for n in nodes if n.AutomationId.endswith("current_chat_name_label")]
            if len(titles) == 1:
                if titles[0].Name.strip() != context.name:
                    raise ChatAutomationError("当前对话已改变，停止任务")
                return nodes
            if len(titles) > 1 or time.monotonic() >= deadline or self.cancelled():
                unique(titles, "当前对话标题")
            self._pause(0.2)

    def _list(self, context: ChatContext) -> Any:
        """找到当前层级唯一的消息列表。"""
        nodes = self._nodes(context)
        if context.forwarded:
            return unique([n for n in nodes if n.ControlTypeName == "ListControl" and
                           n.ClassName == "mmui::RecyclerListView"], "转发内部消息列表")
        return unique([n for n in nodes if n.ControlTypeName == "ListControl" and
                       n.Name in MESSAGE_LIST_NAMES], "消息列表")

    def _click_target(self, context: ChatContext, control: Any, *, cleanup: bool = False,
                      keep_parent_active: bool = False) -> tuple[ChatContext, int, int]:
        """核验控件窗口归属和坐标，返回实际接收点击的窗口。"""
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
        # 搜索弹层不接受激活：激活它会令主窗口失焦并销毁搜索结果。
        # 仍按上面核验过的弹层句柄检查点击坐标，不放宽窗口归属。
        self._activate(context if keep_parent_active else target_context)
        rect = rect_to_tuple(control.BoundingRectangle)
        x, y = (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2
        try:
            self._check_point(target_context, x, y)
        except AttachmentNotClickableError:
            if target_context is not context or keep_parent_active:
                raise
            self._activate(context, force=True)
            self._check_point(context, x, y)
        return target_context, x, y

    def _click(self, context: ChatContext, control: Any, *, cleanup: bool = False,
               keep_parent_active: bool = False) -> None:
        """核验窗口归属后点击；任务取消时仅允许关闭本任务的接收窗口。"""
        self._click_target(context, control, cleanup=cleanup, keep_parent_active=keep_parent_active)
        control.Click(simulateMove=False)

    def _post_click(self, context: ChatContext, control: Any, *, cleanup: bool = False,
                    keep_parent_active: bool = False) -> None:
        """核验后投递窗口消息点击，供远控已退出而 UIA Click 不生效时使用。"""
        target, x, y = self._click_target(
            context, control, cleanup=cleanup, keep_parent_active=keep_parent_active)
        point = wintypes.POINT(x, y)
        user32 = ctypes.windll.user32
        if not user32.ScreenToClient(target.window.handle, ctypes.byref(point)):
            raise ChatAutomationError('无法把操作坐标转换为微信窗口坐标')
        lparam = ((point.y & 0xffff) << 16) | (point.x & 0xffff)
        if not user32.PostMessageW(target.window.handle, 0x0201, 0x0001, lparam):
            raise ChatAutomationError('无法向微信窗口发送鼠标按下消息')
        self._pause(0.05)
        if not user32.PostMessageW(target.window.handle, 0x0202, 0, lparam):
            raise ChatAutomationError('无法向微信窗口发送鼠标释放消息')

    def _check_point(self, context: ChatContext, x: int, y: int) -> None:
        """核验附件坐标归属；遮挡错误纳入任务摘要，并记录前台应用以便诊断。"""
        from types import SimpleNamespace
        try:
            self.guard.ensure_target_clickable(context.window, SimpleNamespace(
                home_window_handle=context.window.handle, click_x=x, click_y=y))
        except HomeWindowNotClickableError as exc:
            foreground = ctypes.windll.user32.GetForegroundWindow()
            desktop_windows = enumerate_desktop_windows()
            windows = [w for w in desktop_windows if w.handle == foreground]
            detail = f'{windows[0].process_name} / {windows[0].class_name}' if windows else '不可识别'
            owner = exc.owner_handle
            root = ctypes.windll.user32.GetAncestor(owner, 2) if owner else 0
            blockers = [w for w in desktop_windows if w.handle in {owner, root}]
            blocker = f'{blockers[0].process_name} / {blockers[0].class_name} / {blockers[0].rect}' if blockers else f'句柄 {owner}'
            raise AttachmentNotClickableError(
                f'附件操作位置被遮挡或窗口已变化，已停止；前台应用：{detail}；'
                f'遮挡窗口：{blocker}；'
                f'目标窗口 {context.window.handle}；{str(exc).replace("文章坐标", "操作坐标").replace("微信主页", "目标微信窗口")}') from exc

    def open_chat(self, name: str | None) -> ChatContext:
        """选择当前对话或精确搜索指定对话；不向聊天输入框输入文字。

        指定 ``name`` 时允许冷启动：即便当前没有可读的对话标题，也会直接找
        左侧会话搜索框并打开目标会话，供公众号自动进历史消息页使用。
        """
        diagnostic = self.diagnose()
        if not diagnostic["ready"] and name is None:
            raise ChatAutomationError(diagnostic["detail"])
        nodes = walk(self.main.control)
        titles = [n for n in nodes if n.AutomationId.endswith("current_chat_name_label")]
        if name is None:
            title = unique(titles, "当前对话标题")
            context = ChatContext(self.main, title.Name.strip())
            if not diagnostic["ready"]:
                raise ChatAutomationError(diagnostic["detail"])
            return context
        if len(titles) == 1 and titles[0].Name.strip() == name:
            context = ChatContext(self.main, name)
            if not diagnostic["ready"]:
                raise ChatAutomationError(diagnostic["detail"])
            return context
        bootstrap = titles[0].Name.strip() if len(titles) == 1 else ""
        context = ChatContext(self.main, bootstrap, switching=True)
        self._activate(context)
        nodes = self._nodes(context)
        search = unique([n for n in nodes if n.ControlTypeName == "EditControl" and
                         n.ClassName == "mmui::XValidatorTextEdit" and n.Name in {"搜索", "Search", "搜尋"} and
                         is_chat_list_search(n)], "对话搜索框")
        search.SetFocus()
        self._pause(0.2)
        if not self._focus_within(search):
            self._click(context, search)
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
        matches = local_chat_matches(results.GetChildren(), name)
        self._post_click(context, unique(matches, f"对话“{name}”"), keep_parent_active=True)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not self.cancelled():
            context.window = self._window(context.window)
            opened = [n for n in walk(context.window.control)
                      if n.AutomationId.endswith("current_chat_name_label")]
            if len(opened) == 1 and opened[0].Name.strip() == name:
                context.name = name
                context.switching = False
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
        children = listing.GetChildren()
        messages = []
        rows = [row for row in children if row.ControlTypeName == "ListItemControl"]
        raw_ids = {tuple(row.GetRuntimeId()) for row in rows}
        alias_counts: dict[tuple, int] = {}
        for raw_identity in raw_ids:
            canonical = context.identities.get(raw_identity, raw_identity)
            alias_counts[canonical] = alias_counts.get(canonical, 0) + 1
        has_date_boundary = any(
            row.ClassName == "mmui::ChatItemView" and not row.IsOffscreen
            and rect_to_tuple(row.BoundingRectangle)[3] > viewport[1]
            and rect_to_tuple(row.BoundingRectangle)[1] < viewport[3]
            for row in children
        )
        inherited = None if has_date_boundary else context.scan_day
        for row in children:
            nodes = walk(row)
            if row.ControlTypeName != "ListItemControl":
                bounds = rect_to_tuple(row.BoundingRectangle)
                if row.IsOffscreen or bounds[3] <= viewport[1] or bounds[1] >= viewport[3]:
                    continue
                # 只读取明确独立的时间标签，不在任意分组正文里寻找日期。
                if row.ControlTypeName == "TextControl":
                    parsed = message_day(row.Name, self.today, inherited)
                    if parsed:
                        inherited = parsed
                continue
            # 时间分隔条不是聊天消息，不能消耗用户指定的条数。
            if row.ClassName == "mmui::ChatItemView":
                bounds = rect_to_tuple(row.BoundingRectangle)
                if row.IsOffscreen or bounds[3] <= viewport[1] or bounds[1] >= viewport[3]:
                    continue
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
            # Qt 会在滚动后复用 runtime id。旧别名若撞到当前真实 id，或多个
            # 当前控件映射到同一旧 id，就不能继续沿用，否则会误点同名附件。
            if (identity != raw_identity and identity in raw_ids) or alias_counts[identity] > 1:
                identity = raw_identity
                context.identities.pop(raw_identity, None)
            # 整条消息文本作为重叠锚点；下载状态按钮不进入锚点。
            content = row.Name or "\n".join(n.Name for n in nodes if n.ClassName.startswith("mmui::Chat"))
            visible_text = content
            if kind == "file":
                content = "\n".join(content.splitlines()[:2])
            signature = hashlib.sha256((row.ClassName + "\n" + content).encode("utf-8")).hexdigest()
            day = inherited or context.dates.get(identity) or context.signature_dates.get(signature)
            if day is not None:
                context.dates[identity] = day
                context.signature_dates[signature] = day
            messages.append(ChatMessage(signature, kind, day, (identity, bubble), visible_text))
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

    def page_snapshot(self, context: ChatContext) -> dict:
        """返回可见文本和实例标识；标识仅用于当前窗口，不是微信永久消息 ID。"""
        page = self.read_page(context)
        items = []
        for message in page.messages:
            identity = (context.window.handle, context.name, message.control[0], message.signature)
            message_id = hashlib.sha256(repr(identity).encode('utf-8')).hexdigest()
            items.append(dict(id=message_id, signature=message.signature, kind=message.kind,
                              text=message.text, date=message.day.isoformat() if message.day else None))
        # 包含几何位置，长消息内部滚动同样会使旧页面凭据失效。
        token = hashlib.sha256(repr(([item['id'] for item in items], self._page_position(context))).encode('utf-8')).hexdigest()
        return dict(chat=context.name, page_token=token, visible_messages=items, at_start=page.at_start)

    def click_visible_message(self, context: ChatContext, message_id: str, page_token: str) -> dict:
        """重读页面和气泡后单击；拒绝过期标识、文件执行和非气泡控件。"""
        snapshot = self.page_snapshot(context)
        if snapshot['page_token'] != page_token:
            raise ChatAutomationError('页面已变化，请重新读取后再点击')
        index = next((i for i, item in enumerate(snapshot['visible_messages']) if item['id'] == message_id), None)
        if index is None:
            raise ChatAutomationError('消息不在当前可见页')
        message = self.read_page(context).messages[index]
        if message.kind == 'file':
            raise ChatAutomationError('V2 不单击文件消息，避免启动关联程序')
        rows = [row for row in self._list(context).GetChildren()
                if context.identities.get(tuple(row.GetRuntimeId()), tuple(row.GetRuntimeId())) == message.control[0]]
        row = unique(rows, '消息实例')
        bubbles = [n for n in walk(row) if n.ClassName in {'mmui::ChatBubbleItemView', 'mmui::ChatBubbleReferItemView'}
                   and not n.IsOffscreen]
        bubble = unique(bubbles, '消息气泡')
        bounds = rect_to_tuple(bubble.BoundingRectangle)
        viewport = rect_to_tuple(self._list(context).BoundingRectangle)
        x, y = (bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2
        if not (viewport[0] < x < viewport[2] and viewport[1] < y < viewport[3]):
            raise ChatAutomationError('消息气泡中心不在可见视口内，请先滚动后重新读取')
        if self.page_snapshot(context)['page_token'] != page_token:
            raise ChatAutomationError('点击前页面已变化')
        self._click(context, bubble)
        return dict(clicked=True, message_id=message_id, chat=context.name)

    def open_account(self, name: str) -> None:
        """精确搜索本地公众号会话，通过明确的历史消息入口打开主页。"""
        try:
            context = self.open_chat(name)
        except ChatAutomationError as exc:
            if str(exc) != '未在等待时间内确认目标对话已打开':
                raise
            # 搜索已唯一命中目标，但新版微信可能将公众号打开在独立窗口。
            windows = [w for w in enumerate_desktop_windows()
                       if w.title == name and w.process_name.lower() in {'weixin.exe', 'wechat.exe'}
                       and w.visible and not w.is_minimized]
            window = attach_uia_control(unique(windows, '独立公众号窗口'))
            if window.control.ProcessId != self.main.control.ProcessId:
                raise ChatAutomationError('独立公众号窗口不属于当前微信进程')
            context = ChatContext(window, name, switching=True)
        labels = {'公众号主页', '查看历史消息', '历史消息', '全部消息', 'View History', 'All Messages'}
        entries = [n for n in self._nodes(context) if n.ControlTypeName in {'ButtonControl', 'HyperlinkControl'}
                   and n.Name.strip() in labels and not n.IsOffscreen]
        if not entries:
            title = unique([n for n in self._nodes(context)
                            if n.AutomationId.endswith('current_chat_name_label') and n.Name.strip() == name], '公众号标题')
            self._click(context, title)
            self._pause(0.5)
            entries = [n for n in self._nodes(context) if n.ControlTypeName in {'ButtonControl', 'HyperlinkControl'}
                       and n.Name.strip() in labels and not n.IsOffscreen]
        entry = unique(entries, '公众号历史消息入口')
        if entry.Name.strip() == '公众号主页':
            self._post_click(context, entry)
        else:
            self._click(context, entry)
        self._pause(1)

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
            context.scan_day = context.dates[target]
            return context.dates[target]
        if message.signature in context.signature_dates:
            context.scan_day = context.signature_dates[message.signature]
            return context.signature_dates[message.signature]
        self._expand_dates(context)
        original = self.read_page(context)
        if target in context.dates:
            return context.dates[target]
        # 只传播连续且尚无日期分隔的区间；每次滚动都核验相邻消息重叠。
        unresolved = [m for m in original.messages if m.day is None]
        pending = {m.control[0] for m in unresolved}
        pending_signatures = {m.signature for m in unresolved}
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
                    context.signature_dates.update({key: found for key in pending_signatures})
                    context.scan_day = found
                    return found
                unresolved = [m for m in current.messages if m.day is None]
                pending.update(m.control[0] for m in unresolved)
                pending_signatures.update(m.signature for m in unresolved)
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
                self._restore_page_layout(context, original, original_layout)

    def _verify_restored_page(self, context: ChatContext, original: ChatPage,
                              layout: tuple, restored: ChatPage, *, layout_verified: bool = False) -> None:
        """核验返回的整页位置；控件重建时仅按完整有序内容和矩形重新绑定。"""
        if not layout_verified and self._message_layout(context, restored) != layout:
            raise ChatAutomationError('未能恢复原消息位置，停止以免漏扫')
        if page_identity(restored) != page_identity(original):
            replacements = {new.control[0]: old.control[0] for old, new in zip(original.messages, restored.messages)}
            # 一次性替换，避免控件标识交换时逐项更新把两个消息合并成同一个。
            context.identities = {raw: replacements.get(canonical, canonical)
                                  for raw, canonical in context.identities.items()}
            context.identities.update(replacements)
            for old, new in zip(original.messages, restored.messages):
                if new.day:
                    context.dates[old.control[0]] = new.day

    def _restore_page_layout(self, context: ChatContext, original: ChatPage,
                             layout: tuple, *, search_steps: int = 120) -> None:
        """固定步数未回到原页时双向有界查找；只接受完整布局精确命中。"""
        restored = self.read_page(context)
        if self._message_layout(context, restored) == layout:
            self._verify_restored_page(context, original, layout, restored, layout_verified=True)
            return
        for older in (True, False):
            moved = 0
            for _ in range(search_steps):
                self._wheel(context, older=older)
                moved += 1
                self._pause(0.2)
                restored = self.read_page(context)
                if self._message_layout(context, restored) == layout:
                    self._verify_restored_page(context, original, layout, restored, layout_verified=True)
                    return
            # 回到本轮搜索起点，再从相反方向查找。
            for _ in range(moved):
                self._wheel(context, older=not older)
                self._pause(0.2)
        raise ChatAutomationError('未能精确找回日期探查前的原消息位置，停止以免漏扫')

    def _restore_attachment_position(self, context: ChatContext) -> None:
        """底部附件处理完后撤回临时滚动，恢复扫描锚点；取消后不再滚动。"""
        if context.attachment_position is None or self.cancelled():
            return
        original, layout, steps = context.attachment_position
        for _ in range(steps):
            self._wheel(context, older=True)
            self._pause(0.3)
        self._verify_restored_page(context, original, layout, self.read_page(context))
        context.attachment_position = None

    def _message_layout(self, context: ChatContext, page: ChatPage) -> tuple:
        """读取消息及日期标签布局，用于日期探查后的原页精确核验。"""
        rectangles = {context.identities.get(key, key): bounds for key, bounds in self._page_position(context)}
        messages = tuple((m.signature, rectangles[m.control[0]]) for m in page.messages)
        labels = tuple((row.Name.strip(), rect_to_tuple(row.BoundingRectangle))
                       for row in self._list(context).GetChildren()
                       if row.ClassName == 'mmui::ChatItemView' and not row.IsOffscreen)
        return messages, labels

    def _fresh_target(self, context: ChatContext, message: ChatMessage) -> Any:
        """返回操作前重新定位的气泡；布局重建不凭旧坐标继续。"""
        messages = self.read_page(context).messages
        matches = [m for m in messages if m.signature == message.signature and m.control[0] == message.control[0]]
        target = unique(matches, "待处理消息")
        if target.control[1] is None:
            raise ChatAutomationError("没有可确认的附件气泡")
        return target.control[1]

    def scroll_older(self, context: ChatContext, *, recover: bool = True) -> bool:
        """小幅向上翻页；控件重建时退回核验整页再翻一次，返回是否恢复过。"""
        listing = self._list(context)
        self._activate(context)
        if self.cancelled():
            return False
        # 微信的 ScrollPattern 会在图片密集页重排相同控件却不移动视口；统一使用
        # 视口内小幅滚轮，并在每一步核验页面身份和几何位置。
        original = self.read_page(context)
        before = page_identity(original)
        layout = self._message_layout(context, original)
        position = self._page_position(context)
        stationary = 0
        for step in range(1, 201):
            if self.cancelled():
                return False
            self._wheel(context, older=True)
            self._pause(0.4)
            current = self.read_page(context)
            if page_identity(current) != before:
                try:
                    older_prefix(original, current)
                except ChatAutomationError:
                    if not recover:
                        # 某些图片密集页面每次越过虚拟列表边界都会重建全部控件，
                        # 无法保留 runtime id。滚轮为小步且页面确已变化时，允许
                        # 调用端把当前可见页作为新基线；宁可重复检查，不跨页猜配。
                        context.identities.clear()
                        return True
                    # 加载历史可能重建整个可见列表。先撤回本次滚动，核验原页完整
                    # 内容、顺序及几何位置，再重绑新控件；不能凭“都是图片”直接匹配。
                    for _ in range(step):
                        self._wheel(context, older=False)
                        self._pause(0.3)
                    self._verify_restored_page(context, original, layout, self.read_page(context))
                    self.scroll_older(context, recover=False)
                    return True
                return False
            current_position = self._page_position(context)
            stationary = stationary + 1 if current_position == position else 0
            position = current_position
            if stationary >= 3:
                if context.forwarded:
                    context.reached_start = True
                    return False
                break
        raise ChatAutomationError("消息列表没有向前移动，未确认已读完历史")

    def scroll_latest(self, context: ChatContext) -> None:
        """有界滚动到最新消息；连续三次位置不变才确认底部。"""
        position = self._page_position(context)
        stationary = 0
        for _ in range(100):
            self._wheel(context, older=False, steps=10)
            self._pause(0.3)
            current = self._page_position(context)
            stationary = stationary + 1 if current == position else 0
            position = current
            if stationary >= 3:
                return
        raise ChatAutomationError('未确认已滚动到最新消息，停止任务')

    def _page_position(self, context: ChatContext) -> tuple:
        """包含行几何位置，区分长消息内部滚动和真正停止移动。"""
        return tuple((tuple(row.GetRuntimeId()), rect_to_tuple(row.BoundingRectangle))
                     for row in self._list(context).GetChildren())

    def _wheel(self, context: ChatContext, *, older: bool, steps: int = 1) -> None:
        """只在已核验消息视口内部发送滚轮，重新校验窗口和取消状态。"""
        for attempt in range(2):
            self._activate(context, force=bool(attempt))
            # 文件下载可能唤起查看器；重新激活后再读取布局，不能复用失焦前的位置。
            listing = self._list(context)
            if self.cancelled():
                raise ChatAutomationError('已取消')
            bounds = rect_to_tuple(listing.BoundingRectangle)
            try:
                self._check_point(context, (bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2)
            except ChatAutomationError:
                if attempt:
                    raise
                # 此时尚未发送滚轮；只允许等待并重新激活核验一次，仍遮挡就停止。
                self._pause(0.25)
                continue
            action = listing.WheelUp if older else listing.WheelDown
            action(wheelTimes=steps, waitTime=0.1)
            return

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
        """识别新出现或点击后复用的附件窗口，保留用户原已打开的转发窗口。"""
        before = {w.handle: w for w in enumerate_desktop_windows()}
        bubble = self._fresh_target(context, message)
        label = bubble.Name
        if self.cancelled():
            raise ChatAutomationError('已取消')
        self._activate(context)
        if ctypes.windll.user32.GetForegroundWindow() != context.window.handle:
            raise ChatAutomationError('未能激活附件所在窗口，未点击')
        self._open_bubble(context, message, bubble)
        deadline = time.monotonic() + 5
        ancestors = context.ancestors + (context.window.handle,)
        while time.monotonic() < deadline and not self.cancelled():
            matches = []
            parent_pid = context.window.control.ProcessId
            foreground = ctypes.windll.user32.GetForegroundWindow()
            for w in enumerate_desktop_windows():
                if w.handle in ancestors or not w.visible or w.is_minimized or w.process_name.lower() not in {"weixin.exe", "wechat.exe"}:
                    continue
                previous = before.get(w.handle)
                # 旧窗口必须由这次点击带到前台。已可见的转发还须与卡片标题相符；
                # 不接管内容身份未确认的旧图片预览，也不进入任意已打开的同名窗口。
                if previous is not None:
                    if foreground != w.handle:
                        continue
                    if previous.visible and not (class_name == FORWARD_CLASS and
                            w.title.strip() and w.title.strip() in label):
                        continue
                candidate = attach_uia_control(w)
                if candidate.control.ProcessId == parent_pid and any(n.ClassName == class_name for n in walk(candidate.control)):
                    matches.append(candidate)
            if matches:
                window = unique(matches, "附件子窗口")
                previous = before.get(window.handle)
                return ChatContext(window, context.name, True,
                                   owned=previous is None or not previous.visible, ancestors=ancestors)
            self._pause(0.2)
        raise ChatAutomationError("未确认附件窗口：未出现匹配的新窗口或可核验的复用窗口，停止任务")

    def _bubble_point(self, context: ChatContext, message: ChatMessage, bubble: Any) -> tuple[int, int]:
        """遮挡时重新激活、读取与采样一次；不重复已经发送的附件点击。"""
        for attempt in range(4):
            if self.cancelled():
                raise ChatAutomationError('已取消')
            try:
                return self._locate_bubble_point(context, message, bubble)
            except AttachmentNotClickableError as exc:
                tooltip = 'Weixin.exe / Qt51514QWindowToolTipSaveBits' in str(exc)
                if (attempt >= (3 if tooltip else 1)) or self.cancelled():
                    raise
                if tooltip:
                    rect = context.window.rect
                    ctypes.windll.user32.SetCursorPos((rect[0] + rect[2]) // 2, rect[1] + 20)
                self._pause(0.6 if tooltip else 0.3)
                if self.cancelled():
                    raise ChatAutomationError('已取消')
                self._activate(context, force=True)
                bubble = self._fresh_target(context, message)

    def _locate_bubble_point(self, context: ChatContext, message: ChatMessage, bubble: Any) -> tuple[int, int]:
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
        # 顶部只露底边和底部只露昵称都可能出现；按裁切方向露出正文。
        # 向下滚动会暂时隐藏较早消息，必须保存整页并在附件关闭后恢复。
        for _ in range(20):
            visible_height = min(row[3] - inset, viewport[3]) - max(row[1] + header, viewport[1])
            if visible_height >= side or (visible_height >= 24 and row[1] >= viewport[1]):
                break
            if self.cancelled():
                raise ChatAutomationError('已取消')
            older = row[1] < viewport[1]
            if not older and row[3] <= viewport[3]:
                break
            if not older:
                if context.attachment_position is None:
                    original = self.read_page(context)
                    context.attachment_position = (original, self._message_layout(context, original), 0)
            previous_row = row
            self._wheel(context, older=older)
            if not older:
                original, layout, steps = context.attachment_position
                context.attachment_position = (original, layout, steps + 1)
            self._pause(0.3)
            bubble = self._fresh_target(context, message)
            listing = self._list(context)
            viewport = rect_to_tuple(listing.BoundingRectangle)
            row = rect_to_tuple(bubble.BoundingRectangle)
            if row == previous_row:
                break
        top, bottom = max(row[1] + header, viewport[1]), min(row[3] - inset, viewport[3])
        left, right = max(row[0] + side, viewport[0]), min(row[2] - side, viewport[2])
        if right - left < 24 or bottom - top < 24:
            raise ChatAutomationError(f'自动滚动后附件正文仍不足：消息矩形 {row}，列表矩形 {viewport}，DPI {dpi}')
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
                if message.kind != 'image':
                    raise ChatAutomationError('未能在附件消息内定位可见内容，未点击')
                # 纯白截图、扫描件留白等图片可能没有足够色差；图片类型已由气泡控件
                # 明确确认，且采样区域已排除头像、昵称和裁切边缘，此时点击正文中心。
                x, y = bitmap.Width // 2, bitmap.Height // 2
        fresh = self._fresh_target(context, message)
        if (rect_to_tuple(fresh.BoundingRectangle) != row or
                rect_to_tuple(self._list(context).BoundingRectangle) != viewport or
                ctypes.windll.user32.GetDpiForWindow(context.window.handle) != dpi):
            raise ChatAutomationError('截图后消息位置或窗口缩放已改变，未点击')
        # 截图后不能再激活窗口并沿用旧像素：若 PDF 此时抢到前台，交给外层重采样。
        self._check_point(context, left + x, top + y)
        if self.cancelled():
            raise ChatAutomationError('已取消')
        return left + x - row[0], top + y - row[1]

    def _open_bubble(self, context: ChatContext, message: ChatMessage, bubble: Any) -> None:
        """仅双击图片和转发正文；文件不经过这个入口。"""
        if message.kind not in {'image', 'forward'}:
            raise ChatAutomationError('不允许双击文件')
        x, y = self._bubble_point(context, message, bubble)
        bubble = self._fresh_target(context, message)
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
        if not child.owned:
            self._nodes(parent)
            self._activate(parent)
            self._restore_attachment_position(parent)
            return
        window = self._window(child.window)
        pattern = window.control.GetWindowPattern()
        if pattern is None:
            raise ChatAutomationError("子窗口不支持明确关闭；请手动关闭后再运行")
        pattern.Close()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if not any(w.handle == window.handle for w in enumerate_desktop_windows()):
                self._nodes(parent)
                self._restore_attachment_position(parent)
                return
            time.sleep(0.1)
        raise ChatAutomationError("附件窗口尚未关闭，停止后续操作")

    def _buttons(self, context: ChatContext, names: set[str]) -> list[Any]:
        """只查找明确可见的按钮，不把正文当作操作入口。"""
        return [n for n in self._nodes(context) if n.ControlTypeName == "ButtonControl" and
                (n.Name.strip() in names or (names == ORIGINAL_NAMES and any(
                    re.fullmatch(re.escape(label) + r'\s*[（(]\s*\d+(?:\.\d+)?\s*(?:[KMG]?B|字节)\s*[）)]',
                                 n.Name.strip(), re.IGNORECASE) for label in names))) and not n.IsOffscreen]

    def download(self, context: ChatContext, message: ChatMessage, timeout: float,
                 verify_files: bool = False) -> str:
        """图片寻找原图按钮，文件等待下载状态；完成后恢复临时调整的位置。"""
        if message.kind == "file":
            try:
                status = self._download_file(context, message, timeout, verify_files)
                if status not in {'skipped_file', 'available', 'cancelled'} and not self.cancelled():
                    # 单击接收可能异步打开 PDF 等关联查看器；返回扫描前恢复原对话。
                    self._nodes(context)
                    self._activate(context)
                    self._fresh_target(context, message)
                return status
            finally:
                self._restore_attachment_position(context)
        try:
            child = self._child_window(context, message, PREVIEW_CLASS)
        except ChatAutomationError as exc:
            if not str(exc).startswith('未确认附件窗口：'):
                raise
            # 图片已完成一次经过坐标核验的双击；预览未出现通常表示缓存失效或
            # 当前气泡不支持预览。确认仍在原对话后记录失败并继续，不拖垮整周任务。
            self._nodes(context)
            self._restore_attachment_position(context)
            return 'failed'
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

    def _download_file(self, context: ChatContext, message: ChatMessage, timeout: float,
                       verify_existing: bool = False) -> str:
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
                    return self._file_menu_download(context, message, timeout) if verify_existing else 'skipped_file'
                if not buttons:
                    lines = self._fresh_target(context, message).Name.splitlines()
                    # 本机微信未下载文档没有下载菜单，单击会下载后交给关联查看器。
                    # 只允许普通文档/压缩包；程序、脚本、快捷方式不使用这个回退入口。
                    if missing and len(lines) >= 2 and Path(lines[1]).suffix.lower() in {
                            '.pdf', '.docx', '.xlsx', '.pptx', '.txt', '.csv', '.zip', '.rar', '.7z'}:
                        bubble = self._fresh_target(context, message)
                        x, y = self._bubble_point(context, message, bubble)
                        bubble = self._fresh_target(context, message)
                        try:
                            bubble.Click(x=x, y=y, simulateMove=False, waitTime=0.2)
                            return self._wait_file(context, message, lines[1], max(1, deadline - time.monotonic()))
                        finally:
                            self._file_receipt(context, lines[1], accept=False)
                    return self._file_menu_download(context, message, timeout)
                self._click(context, unique(buttons, "文件下载按钮"))
                clicked = True
            self._pause(0.25)
        return "cancelled" if self.cancelled() else "unconfirmed"

    def _wait_file(self, context: ChatContext, message: ChatMessage, filename: str, timeout: float) -> str:
        """单击接收后只轮询状态与匹配弹窗，不在下载期间右键文件争抢焦点。"""
        deadline = time.monotonic() + timeout
        ready_since = None
        while time.monotonic() < deadline and not self.cancelled():
            self._file_receipt(context, filename, accept=True)
            nodes = walk(self._fresh_target(context, message))
            labels = {n.Name.strip() for n in nodes}
            if any('过期' in label or 'expired' in label.lower() for label in labels):
                return 'expired'
            if labels & {'下载完成', '已下载', 'Downloaded'}:
                return 'confirmed'
            if any(n.ControlTypeName == 'ButtonControl' and n.Name.strip() in AVAILABLE_NAMES
                   and not n.IsOffscreen for n in nodes):
                return 'confirmed'
            pending = any(re.search(r'(?:^|\s)未下载(?:\s|$)|下载中|接收中|\d+(?:\.\d+)?%', label)
                          for label in labels)
            if pending:
                ready_since = None
            elif ready_since is None:
                ready_since = time.monotonic()
            elif time.monotonic() - ready_since >= 1:
                # 标记消失不等于磁盘文件完整；留出弹窗异步出现的时间后记待核验。
                return 'unconfirmed'
            self._pause(0.25)
        return 'cancelled' if self.cancelled() else 'unconfirmed'

    def _file_menu(self, context: ChatContext, message: ChatMessage) -> list[Any]:
        """在已核验文件正文内打开右键菜单，避开整行空白区。"""
        bubble = self._fresh_target(context, message)
        self._activate(context)
        if self.cancelled():
            return []
        x, y = self._bubble_point(context, message, bubble)
        bubble = self._fresh_target(context, message)
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
