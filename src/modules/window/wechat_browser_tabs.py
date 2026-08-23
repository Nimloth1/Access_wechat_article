from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
import ctypes
from ctypes import wintypes
import hashlib
import platform
import re
import time
import unicodedata
from typing import Any, Protocol

from src.modules.window.article_clicker import _post_message_click
from src.modules.window.wechat_home_window_finder import (
    EXPLICIT_HOME_TITLES,
    WECHAT_CHROME_CLASS_MARKER,
    WECHAT_HOME_PROCESS,
    attach_uia_control,
    enumerate_win32_windows,
    enumerate_uia_windows,
    rect_to_tuple,
)
from src.modules.window.window_models import BrowserTabInfo


WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
VK_CONTROL = 0x11
VK_W = 0x57
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
SW_RESTORE = 9
GA_ROOT = 2

# 微信文章页加载过程中可能短暂暴露这些标题，不能把它们当作最终文章标题。
_TEMPORARY_TAB_TITLE_KEYS = {
    "aboutblank",
    "微信公众平台",
    "加载中",
    "正在加载",
    "新标签页",
}
_MIN_TRUNCATED_PREFIX_LENGTH = 10


def enumerate_wechat_chrome_windows() -> list[Any]:
    """先用 Win32 筛选微信窗口，再按句柄挂接 UIA 控件。"""
    candidates = [
        window
        for window in enumerate_win32_windows()
        if window.process_name.lower() == WECHAT_HOME_PROCESS
        and WECHAT_CHROME_CLASS_MARKER in window.class_name.lower()
        and window.visible
        and not window.is_minimized
        and window.has_valid_rect
    ]
    if not candidates:
        candidates = [
            window
            for window in enumerate_uia_windows()
            if window.process_name.lower() == WECHAT_HOME_PROCESS
            and WECHAT_CHROME_CLASS_MARKER in window.class_name.lower()
            and window.visible
            and not window.is_minimized
            and window.has_valid_rect
        ]
    return [attach_uia_control(window) for window in candidates]


class BrowserTabAdapter(Protocol):
    def list_tabs(self) -> list[BrowserTabInfo]: ...

    def close_tab(self, selected: BrowserTabInfo, *, home_window_handle: int) -> None: ...


class ArticleTabNotFoundError(RuntimeError):
    """本次点击后没有检测到可确认的文章标签。"""


class WechatBrowserTabService:
    """负责文章标签基线、有效文章页确认和精确关闭，不处理 MITM。"""

    def __init__(
        self,
        *,
        adapter: BrowserTabAdapter,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._adapter = adapter
        self._monotonic = monotonic
        self._sleep = sleep

    def capture_baseline(self) -> dict[str, str]:
        active = self._active_tab()
        if active is not None:
            return {active.tab_id: _article_title_match_key(active.title)}
        return {
            item.tab_id: _article_title_match_key(item.title)
            for item in self._adapter.list_tabs()
        }

    def wait_for_article_tab(
        self,
        *,
        target_title: str,
        baseline: Mapping[str, str],
        timeout_seconds: float,
        stable_delay_seconds: float,
        poll_interval_seconds: float | None = None,
        poll_initial_interval_seconds: float | None = None,
        poll_max_interval_seconds: float | None = None,
    ) -> BrowserTabInfo:
        expected = _article_title_match_key(target_title)
        if not expected:
            raise ValueError("target_title 不能为空")
        deadline = self._monotonic() + max(0.0, float(timeout_seconds))
        interval, max_interval = _poll_interval_bounds(
            poll_interval_seconds=poll_interval_seconds,
            poll_initial_interval_seconds=poll_initial_interval_seconds,
            poll_max_interval_seconds=poll_max_interval_seconds,
        )

        while True:
            candidate = self._find_current_target(expected, baseline)
            if candidate is not None:
                delay = max(0.0, float(stable_delay_seconds))
                if delay:
                    self._sleep(delay)
                confirmed = self._confirm_same_target(candidate, expected)
                if confirmed is not None:
                    return confirmed

            now = self._monotonic()
            if now >= deadline:
                raise ArticleTabNotFoundError(f"未检测到目标文章标签：{target_title}")
            self._sleep(min(interval, max(0.0, deadline - now)))
            interval = min(max_interval, interval * 1.5)

    def wait_for_opened_article_tab(
        self,
        *,
        timeout_seconds: float,
        stable_delay_seconds: float,
        baseline: Mapping[str, str] | None = None,
        poll_interval_seconds: float | None = None,
        poll_initial_interval_seconds: float | None = None,
        poll_max_interval_seconds: float | None = None,
    ) -> BrowserTabInfo:
        """等待点击后出现的有效文章页，不再要求标签标题与主页候选标题一致。"""
        baseline = baseline or {}
        deadline = self._monotonic() + max(0.0, float(timeout_seconds))
        interval, max_interval = _poll_interval_bounds(
            poll_interval_seconds=poll_interval_seconds,
            poll_initial_interval_seconds=poll_initial_interval_seconds,
            poll_max_interval_seconds=poll_max_interval_seconds,
        )

        while True:
            candidate = self._find_current_opened_article(baseline)
            if candidate is not None:
                delay = max(0.0, float(stable_delay_seconds))
                if delay:
                    self._sleep(delay)
                confirmed = self._confirm_same_opened_article(candidate, baseline)
                if confirmed is not None:
                    return confirmed

            now = self._monotonic()
            if now >= deadline:
                raise ArticleTabNotFoundError("未检测到已打开的文章标签")
            self._sleep(min(interval, max(0.0, deadline - now)))
            interval = min(max_interval, interval * 1.5)

    def close_article_tab(
        self,
        selected: BrowserTabInfo,
        *,
        home_window_handle: int,
    ) -> None:
        self._adapter.close_tab(selected, home_window_handle=home_window_handle)

    def list_article_tabs(self) -> list[BrowserTabInfo]:
        """返回当前可关闭的微信文章页标签，供诊断工具只读展示和手动关闭。"""
        return [
            item
            for item in self._adapter.list_tabs()
            if _is_opened_article_tab(item, {})
        ]

    def _active_tab(self) -> BrowserTabInfo | None:
        active_tab = getattr(self._adapter, "active_tab", None)
        if not callable(active_tab):
            return None
        try:
            return active_tab()
        except Exception:
            return None

    def _find_current_target(
        self,
        expected: str,
        baseline: Mapping[str, str],
    ) -> BrowserTabInfo | None:
        active = self._active_tab()
        if active is not None:
            return self._find_new_target(expected, baseline, [active])
        return self._find_new_target(expected, baseline, self._adapter.list_tabs())

    def _find_current_opened_article(
        self,
        baseline: Mapping[str, str],
    ) -> BrowserTabInfo | None:
        active = self._active_tab()
        if active is not None:
            return active if _is_opened_article_tab(active, baseline) else None
        return self._find_opened_article(self._adapter.list_tabs(), baseline)

    @staticmethod
    def _find_new_target(
        expected: str,
        baseline: Mapping[str, str],
        tabs: list[BrowserTabInfo],
    ) -> BrowserTabInfo | None:
        for item in tabs:
            current_title = _article_title_match_key(item.title)
            if _match_article_title_keys(expected, current_title) is None:
                continue
            if item.tab_id not in baseline or baseline[item.tab_id] != current_title:
                return item
        return None

    @staticmethod
    def _find_opened_article(
        tabs: list[BrowserTabInfo],
        baseline: Mapping[str, str],
    ) -> BrowserTabInfo | None:
        for item in tabs:
            if _is_opened_article_tab(item, baseline):
                return item
        return None

    def _confirm_same_target(
        self,
        candidate: BrowserTabInfo,
        expected: str,
    ) -> BrowserTabInfo | None:
        active = self._active_tab()
        if active is not None:
            if active.tab_id != candidate.tab_id:
                return None
            if _match_article_title_keys(
                expected,
                _article_title_match_key(active.title),
            ) is not None:
                return active
            return None
        for item in self._adapter.list_tabs():
            if item.tab_id != candidate.tab_id:
                continue
            if _match_article_title_keys(
                expected,
                _article_title_match_key(item.title),
            ) is not None:
                return item
        return None

    def _confirm_same_opened_article(
        self,
        candidate: BrowserTabInfo,
        baseline: Mapping[str, str],
    ) -> BrowserTabInfo | None:
        active = self._active_tab()
        if active is not None:
            if active.tab_id != candidate.tab_id:
                return None
            return active if _is_opened_article_tab(active, baseline) else None
        for item in self._adapter.list_tabs():
            if item.tab_id == candidate.tab_id and _is_opened_article_tab(item, baseline):
                return item
        return None


class UiaWechatBrowserTabAdapter:
    """读取 WeChatAppEx Chromium 标签，并通过 Ctrl+W 关闭当前文章标签。"""

    def __init__(
        self,
        *,
        max_depth: int = 8,
        max_nodes_per_window: int = 1500,
        enumerate_windows: Callable[[], list[Any]] = enumerate_wechat_chrome_windows,
        active_document_closer: Callable[[int], None] | None = None,
        document_page_returner: Callable[[int], None] | None = None,
        document_keyboard_closer: Callable[[int], None] | None = None,
        document_return_timeout_seconds: float = 3.0,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._max_depth = max(1, int(max_depth))
        self._max_nodes = max(1, int(max_nodes_per_window))
        self._enumerate_windows = enumerate_windows
        # 旧参数只保留兼容；当前关闭策略统一为向微信内置浏览器发送 Ctrl+W。
        self._document_page_returner = document_page_returner or active_document_closer
        self._document_keyboard_closer = document_keyboard_closer or _post_ctrl_w
        self._document_return_timeout_seconds = max(0.1, float(document_return_timeout_seconds))
        self._monotonic = monotonic
        self._sleep = sleep

    def active_tab(self) -> BrowserTabInfo | None:
        for window in _foreground_first(self._wechat_chrome_windows()):
            selected_tab = self._selected_tab_from_control(window.handle, window.control)
            if selected_tab is not None:
                return selected_tab
            document_tab = _document_page_tab(window.handle, window.control)
            if document_tab is not None:
                return document_tab
            if window.title.strip():
                return BrowserTabInfo(
                    tab_id=f"window:{window.handle}",
                    owner_handle=window.handle,
                    title=window.title.strip(),
                    rect=window.rect,
                    is_active=True,
                    control=window.control,
                )
        return None

    def list_tabs(self) -> list[BrowserTabInfo]:
        result: list[BrowserTabInfo] = []
        for window in self._wechat_chrome_windows():
            tabs = self._tabs_from_control(window.handle, window.control)
            if tabs:
                result.extend(tabs)
                continue
            document_tab = _document_page_tab(window.handle, window.control)
            if document_tab is not None:
                result.append(document_tab)
                continue
            # 某些微信版本不暴露 TabItem，此时仅把顶层标题作为窗口级标签。
            if window.title.strip():
                result.append(
                    BrowserTabInfo(
                        tab_id=f"window:{window.handle}",
                        owner_handle=window.handle,
                        title=window.title.strip(),
                        rect=window.rect,
                        is_active=True,
                        control=window.control,
                    )
                )
        return _deduplicate_tabs(result)

    def close_tab(self, selected: BrowserTabInfo, *, home_window_handle: int) -> None:
        if normalize_article_title(selected.title) in {
            normalize_article_title(item) for item in EXPLICIT_HOME_TITLES
        }:
            raise RuntimeError("拒绝关闭公众号主页标签")
        self._require_close_owner_window(selected.owner_handle)

        if not selected.is_active and not selected.tab_id.startswith("window:"):
            raise RuntimeError("拒绝关闭非活动文章标签，避免 Ctrl+W 关闭错误标签")

        # 关闭文章标签统一发给微信内置浏览器窗口，不再调用标签关闭按钮 Invoke、
        # UIA Click、中键点击或 WM_CLOSE，避免误触其它应用或关闭主页窗口。
        _activate_tab_control(selected.control)
        self._document_keyboard_closer(selected.owner_handle)
        if selected.tab_id.startswith("document:"):
            self._wait_until_document_title_changes(selected)
        return

    def _require_close_owner_window(self, owner_handle: int) -> Any:
        owner = next(
            (
                window
                for window in self._wechat_chrome_windows()
                if int(getattr(window, "handle", 0)) == int(owner_handle)
            ),
            None,
        )
        if owner is None:
            raise RuntimeError("文章标签所属微信内置浏览器窗口已不可用，拒绝关闭标签")
        if bool(getattr(owner, "is_minimized", False)):
            raise RuntimeError("文章标签所属微信内置浏览器窗口处于最小化状态，拒绝关闭标签")
        if not _valid_rect(rect_to_tuple(getattr(owner, "rect", None))):
            raise RuntimeError("文章标签所属微信内置浏览器窗口区域无效，拒绝关闭标签")
        return owner

    def _wait_until_document_title_changes(self, selected: BrowserTabInfo) -> None:
        expected = normalize_article_title(selected.title)
        deadline = self._monotonic() + self._document_return_timeout_seconds
        while True:
            current = self.active_tab()
            if current is not None and current.tab_id != selected.tab_id:
                current = None
            if current is None:
                current = next(
                    (item for item in self.list_tabs() if item.tab_id == selected.tab_id),
                    None,
                )
            if current is None or normalize_article_title(current.title) != expected:
                return
            now = self._monotonic()
            if now >= deadline:
                raise RuntimeError("文章页返回后标题未变化，拒绝报告已关闭")
            self._sleep(min(0.05, max(0.0, deadline - now)))

    def _wechat_chrome_windows(self) -> list[Any]:
        return [
            window
            for window in self._enumerate_windows()
            if window.process_name.lower() == WECHAT_HOME_PROCESS
            and WECHAT_CHROME_CLASS_MARKER in window.class_name.lower()
        ]

    def _selected_tab_from_control(
        self,
        owner_handle: int,
        root: Any,
    ) -> BrowserTabInfo | None:
        if root is None:
            return None
        queue: deque[tuple[Any, int]] = deque([(root, 0)])
        visited = 0
        while queue and visited < self._max_nodes:
            control, depth = queue.popleft()
            visited += 1
            if depth > 0 and _looks_like_tab_control(control) and _is_selected(control):
                title = str(_safe_get(control, "Name", "") or "").strip()
                rect = rect_to_tuple(_safe_get(control, "BoundingRectangle", None))
                if title and _valid_rect(rect):
                    return BrowserTabInfo(
                        tab_id=_tab_id(owner_handle, control, title, rect),
                        owner_handle=owner_handle,
                        title=title,
                        rect=rect,
                        is_active=True,
                        control=control,
                    )
            if depth >= self._max_depth:
                continue
            try:
                children = control.GetChildren()
            except Exception:
                children = []
            queue.extend((child, depth + 1) for child in children or [])
        return None

    def _tabs_from_control(self, owner_handle: int, root: Any) -> list[BrowserTabInfo]:
        if root is None:
            return []
        queue: deque[tuple[Any, int]] = deque([(root, 0)])
        tabs: list[BrowserTabInfo] = []
        visited = 0
        while queue and visited < self._max_nodes:
            control, depth = queue.popleft()
            visited += 1
            if depth > 0 and _looks_like_tab_control(control):
                title = str(_safe_get(control, "Name", "") or "").strip()
                rect = rect_to_tuple(_safe_get(control, "BoundingRectangle", None))
                if title and _valid_rect(rect):
                    tabs.append(
                        BrowserTabInfo(
                            tab_id=_tab_id(owner_handle, control, title, rect),
                            owner_handle=owner_handle,
                            title=title,
                            rect=rect,
                            is_active=_is_selected(control),
                            control=control,
                        )
                    )
            if depth >= self._max_depth:
                continue
            try:
                children = control.GetChildren()
            except Exception:
                children = []
            for child in children or []:
                queue.append((child, depth + 1))
        return tabs


def normalize_article_title(title: str) -> str:
    value = unicodedata.normalize("NFKC", str(title or ""))
    value = value.replace("\u200b", "").replace("\ufeff", "").strip()
    return re.sub(r"\s+", " ", value)


def _poll_interval_bounds(
    *,
    poll_interval_seconds: float | None,
    poll_initial_interval_seconds: float | None,
    poll_max_interval_seconds: float | None,
) -> tuple[float, float]:
    # 旧调用只传 poll_interval_seconds，等价于“最大轮询间隔”。
    max_interval_source = (
        poll_max_interval_seconds
        if poll_max_interval_seconds is not None
        else poll_interval_seconds
    )
    max_interval = max(0.01, float(max_interval_source or 0.05))
    initial_interval_source = (
        poll_initial_interval_seconds
        if poll_initial_interval_seconds is not None
        else min(0.05, max_interval)
    )
    initial_interval = max(0.01, float(initial_interval_source))
    return min(initial_interval, max_interval), max_interval


def _article_title_match_key(title: str) -> str:
    """生成标题比较键，忽略截断省略号、排版空白和中英文标点差异。"""
    normalized = normalize_article_title(title).casefold()
    return "".join(
        char
        for char in normalized
        if unicodedata.category(char).startswith(("L", "N"))
    )


def match_article_tab_title(target_title: str, observed_title: str) -> str | None:
    """判断标签标题片段是否属于目标文章，并返回可记录的匹配级别。"""
    return _match_article_title_keys(
        _article_title_match_key(target_title),
        _article_title_match_key(observed_title),
    )


def _match_article_title_keys(expected: str, observed: str) -> str | None:
    if not expected or not observed or observed in _TEMPORARY_TAB_TITLE_KEYS:
        return None
    if expected == observed:
        return "exact"
    if len(observed) >= _MIN_TRUNCATED_PREFIX_LENGTH and expected.startswith(observed):
        return "display_prefix"
    if len(expected) >= _MIN_TRUNCATED_PREFIX_LENGTH and observed.startswith(expected):
        return "target_prefix"
    return None


def _is_opened_article_tab(
    item: BrowserTabInfo,
    baseline: Mapping[str, str],
) -> bool:
    title = normalize_article_title(item.title)
    title_key = _article_title_match_key(title)
    if not title_key or title_key in _TEMPORARY_TAB_TITLE_KEYS:
        return False
    if title in {normalize_article_title(value) for value in EXPLICIT_HOME_TITLES}:
        return False
    # 基线只用于排除点击前主页/旧状态，不再和目标文章标题做内容比对。
    baseline_title = baseline.get(item.tab_id)
    return baseline_title is None or baseline_title != title_key


def _looks_like_tab_control(control: Any) -> bool:
    control_type = str(_safe_get(control, "ControlTypeName", "") or "").lower()
    class_name = str(_safe_get(control, "ClassName", "") or "").lower()
    return "tabitem" in control_type or "tab-item" in class_name or class_name == "tabitem"


def _tab_id(
    owner_handle: int,
    control: Any,
    title: str,
    rect: tuple[int, int, int, int],
) -> str:
    runtime_id = ""
    try:
        runtime_id = ".".join(str(item) for item in control.GetRuntimeId())
    except Exception:
        runtime_id = str(_safe_get(control, "AutomationId", "") or "")
    source = f"{owner_handle}\n{runtime_id}\n{normalize_article_title(title)}\n{rect}"
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:20]


def _is_selected(control: Any) -> bool:
    try:
        pattern = control.GetSelectionItemPattern()
        return bool(pattern.IsSelected)
    except Exception:
        return False


def _is_close_button_control(control: Any) -> bool:
    name = normalize_article_title(str(_safe_get(control, "Name", "") or "")).lower()
    control_type = str(_safe_get(control, "ControlTypeName", "") or "").lower()
    return name in {"关闭", "关闭标签页", "close"} and "button" in control_type


def _document_page_tab(owner_handle: int, root: Any) -> BrowserTabInfo | None:
    if root is None:
        return None
    queue: deque[tuple[Any, int]] = deque([(root, 0)])
    while queue:
        control, depth = queue.popleft()
        control_type = str(_safe_get(control, "ControlTypeName", "") or "").lower()
        if control_type == "documentcontrol":
            title = normalize_article_title(str(_safe_get(control, "Name", "") or ""))
            rect = rect_to_tuple(_safe_get(control, "BoundingRectangle", None))
            if title and _valid_rect(rect):
                tab_control = _find_tab_row_for_title(root, title) or control
                return BrowserTabInfo(
                    tab_id=f"document:{owner_handle}",
                    owner_handle=owner_handle,
                    title=title,
                    rect=rect,
                    is_active=True,
                    control=tab_control,
                )
        if depth >= 14:
            continue
        try:
            children = control.GetChildren()
        except Exception:
            children = []
        queue.extend((child, depth + 1) for child in children or [])
    return None


def _find_tab_row_for_title(root: Any, expected_title: str) -> Any | None:
    queue: deque[tuple[Any, int]] = deque([(root, 0)])
    while queue:
        control, depth = queue.popleft()
        try:
            children = list(control.GetChildren() or [])
        except Exception:
            children = []
        if _control_has_direct_title_and_close(control, expected_title, children=children):
            return control
        if depth < 12:
            queue.extend((item, depth + 1) for item in children)
    return None


def _control_has_direct_title_and_close(
    control: Any,
    expected_title: str,
    *,
    children: list[Any] | None = None,
) -> bool:
    if children is None:
        try:
            children = list(control.GetChildren() or [])
        except Exception:
            return False
    expected = normalize_article_title(expected_title)
    direct_names = {
        normalize_article_title(str(_safe_get(item, "Name", "") or ""))
        for item in children
    }
    return expected in direct_names and any(_is_close_button_control(item) for item in children)


_ULONG_PTR = ctypes.c_ulonglong


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", _MOUSEINPUT),
        ("ki", _KEYBDINPUT),
        ("hi", _HARDWAREINPUT),
    ]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [
        ("type", wintypes.DWORD),
        ("u", _INPUT_UNION),
    ]


def _post_ctrl_w(handle: int) -> None:
    """兼容旧调用名：关闭文章页时发送真实 Ctrl+W 键盘输入。"""
    _send_ctrl_w_to_foreground(handle)


def _send_ctrl_w_to_foreground(
    handle: int,
    *,
    user32: Any | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """先激活微信内置浏览器窗口，再通过 SendInput 发送真实 Ctrl+W。"""
    if platform.system() != "Windows":
        raise OSError("标签关闭仅支持 Windows")
    hwnd = int(handle or 0)
    if hwnd <= 0:
        raise ValueError("标签关闭目标窗口句柄不能为空")

    user32 = user32 or ctypes.windll.user32
    _activate_keyboard_target(user32, hwnd, sleep=sleep)
    if not _foreground_belongs_to_handle(user32, hwnd):
        raise RuntimeError("无法激活文章所属微信内置浏览器窗口，已取消 Ctrl+W 发送")

    inputs = (_INPUT * 4)(
        _keyboard_input(VK_CONTROL, 0),
        _keyboard_input(VK_W, 0),
        _keyboard_input(VK_W, KEYEVENTF_KEYUP),
        _keyboard_input(VK_CONTROL, KEYEVENTF_KEYUP),
    )
    sent = int(user32.SendInput(len(inputs), inputs, ctypes.sizeof(_INPUT)) or 0)
    if sent != len(inputs):
        last_error = _last_windows_error(user32)
        raise RuntimeError(
            "发送 Ctrl+W 真实键盘输入失败："
            f"SendInput={sent}/{len(inputs)}; GetLastError={last_error}"
        )


def _keyboard_input(vk: int, flags: int) -> _INPUT:
    entry = _INPUT()
    entry.type = INPUT_KEYBOARD
    entry.ki = _KEYBDINPUT(int(vk), 0, int(flags), 0, 0)
    return entry


def _last_windows_error(user32: Any) -> int:
    try:
        return int(user32.GetLastError() or 0)
    except Exception:
        return int(ctypes.get_last_error() or 0)


def _activate_keyboard_target(
    user32: Any,
    handle: int,
    *,
    kernel32: Any | None = None,
    sleep: Callable[[float], None],
) -> None:
    # Ctrl+W 是前台快捷键，必须先让微信内置浏览器成为键盘输入目标。
    try:
        user32.ShowWindow(int(handle), SW_RESTORE)
    except Exception:
        pass
    try:
        user32.BringWindowToTop(int(handle))
    except Exception:
        pass
    try:
        user32.SetForegroundWindow(int(handle))
    except Exception:
        pass
    try:
        user32.SetFocus(int(handle))
    except Exception:
        pass
    sleep(0.05)
    if _foreground_belongs_to_handle(user32, handle):
        return

    _activate_with_attached_input(
        user32,
        int(handle),
        int(user32.GetForegroundWindow() or 0),
        kernel32=kernel32,
    )
    sleep(0.05)
    if _foreground_belongs_to_handle(user32, handle):
        return

    switch_to_this_window = getattr(user32, "SwitchToThisWindow", None)
    if callable(switch_to_this_window):
        try:
            switch_to_this_window(int(handle), True)
            sleep(0.05)
        except Exception:
            pass


def _activate_tab_control(control: Any) -> None:
    # UIA 控件激活有时比 Win32 前台切换更容易把 Chromium 标签页设为键盘目标。
    if control is None:
        return
    for method_name in ("SetFocus", "SetActive"):
        method = getattr(control, method_name, None)
        if not callable(method):
            continue
        try:
            method()
        except Exception:
            pass


def _activate_with_attached_input(
    user32: Any,
    handle: int,
    foreground_handle: int,
    *,
    kernel32: Any | None = None,
) -> None:
    try:
        kernel32 = kernel32 or ctypes.windll.kernel32
        current_thread = int(kernel32.GetCurrentThreadId() or 0)
        target_thread = int(user32.GetWindowThreadProcessId(int(handle), None) or 0)
        foreground_thread = int(
            user32.GetWindowThreadProcessId(int(foreground_handle), None) or 0
        )
    except Exception:
        return

    attached_threads: list[int] = []
    for thread_id in {target_thread, foreground_thread}:
        if thread_id <= 0 or thread_id == current_thread:
            continue
        try:
            if bool(user32.AttachThreadInput(current_thread, thread_id, True)):
                attached_threads.append(thread_id)
        except Exception:
            pass
    try:
        try:
            user32.BringWindowToTop(int(handle))
        except Exception:
            pass
        try:
            user32.SetForegroundWindow(int(handle))
        except Exception:
            pass
        try:
            user32.SetFocus(int(handle))
        except Exception:
            pass
    finally:
        for thread_id in attached_threads:
            try:
                user32.AttachThreadInput(current_thread, thread_id, False)
            except Exception:
                pass


def _foreground_belongs_to_handle(user32: Any, handle: int) -> bool:
    try:
        foreground = int(user32.GetForegroundWindow() or 0)
    except Exception:
        return False
    if foreground == int(handle):
        return True
    try:
        if bool(user32.IsChild(int(handle), foreground)):
            return True
        return int(user32.GetAncestor(int(foreground), GA_ROOT) or 0) == int(handle)
    except Exception:
        return False


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _deduplicate_tabs(tabs: list[BrowserTabInfo]) -> list[BrowserTabInfo]:
    result: list[BrowserTabInfo] = []
    seen: set[str] = set()
    for item in tabs:
        if item.tab_id in seen:
            continue
        seen.add(item.tab_id)
        result.append(item)
    return result


def _valid_rect(rect: tuple[int, int, int, int]) -> bool:
    return rect[2] > rect[0] and rect[3] > rect[1]


def _safe_get(value: Any, name: str, default: Any) -> Any:
    try:
        return getattr(value, name)
    except Exception:
        return default


def _foreground_first(windows: list[Any]) -> list[Any]:
    foreground = _foreground_window_handle()
    if foreground <= 0:
        return windows
    return sorted(
        windows,
        key=lambda window: int(getattr(window, "handle", 0)) == foreground,
        reverse=True,
    )


def _foreground_window_handle() -> int:
    if platform.system() != "Windows":
        return 0
    try:
        return int(ctypes.windll.user32.GetForegroundWindow() or 0)
    except Exception:
        return 0
