from __future__ import annotations

from collections.abc import Callable, Iterable
import ctypes
from ctypes import wintypes
import os
import platform
from pathlib import Path
import time
from typing import Any

from src.modules.window.window_models import WindowInfo


EXPLICIT_HOME_TITLES = frozenset({"公众号", "服务号", "订阅号"})
GENERIC_WECHAT_TITLES = frozenset({"微信", "weixin", "wechat"})
WECHAT_HOME_PROCESS = "wechatappex.exe"
WECHAT_CHROME_CLASS_MARKER = "chrome_widgetwin"

_USER32 = ctypes.windll.user32 if platform.system() == "Windows" else None
_ENUM_WINDOWS_PROC = (
    ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    if platform.system() == "Windows"
    else None
)


class WechatHomeWindowFindTimeout(RuntimeError):
    """查找微信主页窗口超过配置时间。"""


class WechatHomeWindowMinimized(RuntimeError):
    """检测到微信主页窗口处于最小化状态。"""

    def __init__(self, window: WindowInfo) -> None:
        self.window = window
        super().__init__("微信主页窗口处于最小化状态")


def is_wechat_home_candidate(window: WindowInfo) -> bool:
    """判断窗口是否是可操作的公众号主页，而不是聊天窗或文章详情页。"""
    title = window.title.strip()
    normalized_title = title.lower()
    if not _is_visible_for_operation(window):
        return False
    if window.process_name.strip().lower() != WECHAT_HOME_PROCESS:
        return False
    if WECHAT_CHROME_CLASS_MARKER not in window.class_name.strip().lower():
        return False
    if title in EXPLICIT_HOME_TITLES:
        return True
    return normalized_title in GENERIC_WECHAT_TITLES


def score_wechat_home_candidate(window: WindowInfo) -> int:
    if not is_wechat_home_candidate(window):
        return -1
    return 10_000 if window.title.strip() in EXPLICIT_HOME_TITLES else 1_000


def find_wechat_home_window(
    *,
    enumerate_windows: Callable[[], Iterable[WindowInfo]] | None = None,
    timeout_seconds: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> WindowInfo | None:
    """选择最可信的微信公众账号主页窗口。"""
    enumerate_callback = enumerate_windows or enumerate_desktop_windows
    deadline = (
        monotonic() + max(0.0, float(timeout_seconds))
        if timeout_seconds is not None
        else None
    )
    candidates: list[tuple[int, WindowInfo]] = []
    minimized_candidates: list[WindowInfo] = []
    for window in enumerate_callback():
        _raise_if_deadline_expired(deadline, monotonic)
        if not _is_wechat_shell_window(window):
            continue
        title = window.title.strip()
        normalized_title = title.lower()
        if window.is_minimized:
            if _has_home_like_title(window):
                minimized_candidates.append(window)
            continue
        if not _is_visible_for_operation(window):
            continue
        if title not in EXPLICIT_HOME_TITLES and normalized_title not in GENERIC_WECHAT_TITLES:
            continue
        # 此时尚未激活窗口或挂接 UIA；主页内容由后续读取流程确认。
        score = score_wechat_home_candidate(window)
        if score >= 0:
            candidates.append((score, window))
    if not candidates:
        if minimized_candidates:
            minimized_candidates.sort(key=_minimized_candidate_score, reverse=True)
            raise WechatHomeWindowMinimized(
                attach_uia_control(minimized_candidates[0])
            )
        return None
    candidates.sort(key=lambda item: (item[0], item[1].handle), reverse=True)
    return attach_uia_control(candidates[0][1])


def _is_wechat_shell_window(window: WindowInfo) -> bool:
    """只做轻量窗口外壳过滤，避免对无关窗口执行昂贵的 UIA 文章读取。"""
    if window.handle <= 0:
        return False
    if window.process_name.strip().lower() != WECHAT_HOME_PROCESS:
        return False
    return WECHAT_CHROME_CLASS_MARKER in window.class_name.strip().lower()


def _is_visible_for_operation(window: WindowInfo) -> bool:
    """只有可见且有有效区域的窗口才允许继续读取主页或文章卡片。"""
    if window.handle <= 0 or window.is_minimized:
        return False
    return window.visible and window.has_valid_rect


def _has_home_like_title(window: WindowInfo) -> bool:
    title = window.title.strip()
    return title in EXPLICIT_HOME_TITLES or title.lower() in GENERIC_WECHAT_TITLES


def _minimized_candidate_score(window: WindowInfo) -> tuple[int, int]:
    title_score = 10_000 if window.title.strip() in EXPLICIT_HOME_TITLES else 1_000
    return (title_score, window.handle)


def _raise_if_deadline_expired(
    deadline: float | None,
    monotonic: Callable[[], float],
) -> None:
    if deadline is not None and monotonic() > deadline:
        raise WechatHomeWindowFindTimeout("查找微信主页窗口超时")


def enumerate_desktop_windows() -> list[WindowInfo]:
    """优先用 Win32 枚举顶层窗口，失败时回退 UIA。"""
    windows = enumerate_win32_windows()
    if windows:
        return windows
    return enumerate_uia_windows()


def enumerate_win32_windows(*, user32: Any | None = None) -> list[WindowInfo]:
    """用 Win32 快速枚举顶层窗口，避免 UIA 桌面枚举长时间阻塞。"""
    if platform.system() != "Windows" or _ENUM_WINDOWS_PROC is None:
        return []
    resolved_user32 = user32 or _USER32
    if resolved_user32 is None:
        return []

    windows: list[WindowInfo] = []

    @_ENUM_WINDOWS_PROC
    def callback(handle: int, _lparam: int) -> bool:
        resolved_handle = int(handle)
        if resolved_handle <= 0:
            return True
        title = _window_text(resolved_user32, resolved_handle)
        class_name = _class_name(resolved_user32, resolved_handle)
        process_id = wintypes.DWORD()
        resolved_user32.GetWindowThreadProcessId(
            resolved_handle,
            ctypes.byref(process_id),
        )
        process_name = _process_name(int(process_id.value))
        normalized_process = process_name.lower()
        if (
            not title
            and "wechat" not in normalized_process
            and "weixin" not in normalized_process
        ):
            return True
        rect = _window_rect(resolved_user32, resolved_handle)
        is_minimized = _is_minimized_window(
            resolved_handle,
            user32=resolved_user32,
        )
        windows.append(
            WindowInfo(
                handle=resolved_handle,
                title=title,
                class_name=class_name,
                process_name=process_name,
                rect=rect,
                visible=bool(resolved_user32.IsWindowVisible(resolved_handle)),
                is_minimized=is_minimized,
                control=None,
            )
        )
        return True

    resolved_user32.EnumWindows(callback, 0)
    return windows


def attach_uia_control(window: WindowInfo) -> WindowInfo:
    """按已找到的窗口句柄挂接 UIA 控件，供后续读取文章结构。"""
    message_browser = (os.environ.get("WECHAT_WINDOW_MESSAGES") == "1"
                       and window.process_name.lower() == WECHAT_HOME_PROCESS
                       and WECHAT_CHROME_CLASS_MARKER in window.class_name.lower())
    if window.handle <= 0:
        return window
    if window.control is not None and not message_browser:
        return window
    if message_browser:
        import uiautomation as auto
        control = auto.ControlFromHandle(_active_message_render(window))
        if control is None:
            raise RuntimeError("后台模式未读到活动网页渲染控件")
        from dataclasses import replace
        return replace(window, control=control)
    try:
        import uiautomation as auto

        control = auto.ControlFromHandle(int(window.handle))
    except Exception:
        return window
    if control is None:
        return window
    return WindowInfo(
        handle=window.handle,
        title=window.title,
        class_name=window.class_name,
        process_name=window.process_name,
        rect=window.rect,
        visible=window.visible,
        is_minimized=window.is_minimized,
        control=control,
    )


def _active_message_render(window: WindowInfo) -> int:
    """只挂接当前原生渲染窗口，排除仍暴露 UIA 的后台旧主页。"""
    from src.modules.window.article_clicker import _resolve_click_message_target, _Point
    left, top, right, bottom = window.rect
    target = _resolve_click_message_target(
        _USER32, window.handle, _Point((left + right) // 2, top + (bottom - top) * 2 // 3))
    name = ctypes.create_unicode_buffer(256)
    _USER32.GetClassNameW(target, name, 256)
    if name.value != "Chrome_RenderWidgetHostHWND":
        raise RuntimeError("后台模式无法唯一核验活动网页渲染窗口")
    return target


def enumerate_uia_windows(
    auto_module: Any | None = None,
    *,
    user32: Any | None = None,
) -> list[WindowInfo]:
    """读取桌面顶层 UIA 窗口；异常窗口会被跳过。"""
    if platform.system() != "Windows":
        return []
    if auto_module is None:
        try:
            import uiautomation as auto_module
        except Exception:
            return []
    try:
        root = auto_module.GetRootControl()
        controls = root.GetChildren()
    except Exception:
        return []

    windows: list[WindowInfo] = []
    for control in controls or []:
        handle = _safe_int(_safe_get(control, "NativeWindowHandle", 0))
        process_id = _safe_int(_safe_get(control, "ProcessId", 0))
        rect = rect_to_tuple(_safe_get(control, "BoundingRectangle", None))
        is_offscreen = bool(_safe_get(control, "IsOffscreen", False))
        is_minimized = _is_minimized_window(handle, user32=user32)
        windows.append(
            WindowInfo(
                handle=handle,
                title=str(_safe_get(control, "Name", "") or ""),
                class_name=str(_safe_get(control, "ClassName", "") or ""),
                process_name=_process_name(process_id),
                rect=rect,
                visible=not is_offscreen,
                is_minimized=is_minimized,
                control=control,
            )
        )
    return windows


def rect_to_tuple(rect: Any) -> tuple[int, int, int, int]:
    if isinstance(rect, (tuple, list)) and len(rect) == 4:
        return tuple(int(value) for value in rect)
    for names in (("left", "top", "right", "bottom"), ("Left", "Top", "Right", "Bottom")):
        values = [_safe_get(rect, name, None) for name in names]
        if all(value is not None for value in values):
            return tuple(int(value) for value in values)
    return (0, 0, 0, 0)


def _window_text(user32: Any, handle: int) -> str:
    length = int(user32.GetWindowTextLengthW(handle) or 0)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(handle, buffer, length + 1)
    return str(buffer.value or "")


def _class_name(user32: Any, handle: int) -> str:
    buffer = ctypes.create_unicode_buffer(512)
    user32.GetClassNameW(handle, buffer, 512)
    return str(buffer.value or "")


def _window_rect(user32: Any, handle: int) -> tuple[int, int, int, int]:
    rect = wintypes.RECT()
    if not user32.GetWindowRect(handle, ctypes.byref(rect)):
        return (0, 0, 0, 0)
    return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))


def _process_name(process_id: int) -> str:
    if process_id <= 0:
        return ""
    try:
        import psutil

        return Path(str(psutil.Process(process_id).name() or "")).name
    except Exception:
        return ""


def _is_minimized_window(handle: int, *, user32: Any | None = None) -> bool:
    if handle <= 0:
        return False
    try:
        resolved_user32 = user32 or _USER32 or ctypes.windll.user32
        return bool(resolved_user32.IsIconic(int(handle)))
    except Exception:
        return False


def _safe_get(value: Any, name: str, default: Any) -> Any:
    try:
        return getattr(value, name)
    except Exception:
        return default


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
