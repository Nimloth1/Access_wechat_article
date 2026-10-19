from __future__ import annotations

import ctypes
import os
import platform
import time
from typing import Any, Callable

from src.domain.models import ArticleTarget
from src.modules.window.window_models import WindowInfo


SW_RESTORE = 9
GA_ROOT = 2


class HomeWindowNotClickableError(RuntimeError):
    """主页未处于可点击层级，或目标坐标当前不属于该主页。"""

    def __init__(self, message: str, *, owner_handle: int = 0) -> None:
        """保留实际遮挡句柄，便于区分前台窗口和点击点所属窗口。"""
        super().__init__(message)
        self.owner_handle = owner_handle


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class WindowsHomeWindowGuard:
    """激活已确认的微信主页，并在点击前核对坐标所属窗口。"""

    def __init__(
        self,
        *,
        user32: Any | None = None,
        kernel32: Any | None = None,
        platform_name: str | None = None,
        activation_wait_seconds: float = 0.25,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._user32 = user32
        self._kernel32 = kernel32
        self._platform_name = platform_name or platform.system()
        self._activation_wait_seconds = max(0.0, float(activation_wait_seconds))
        self._sleep = sleep
        self._message_transport = None

    @property
    def message_mode(self) -> bool:
        return os.environ.get("WECHAT_WINDOW_MESSAGES") == "1"

    def _window_messages(self):
        if self._message_transport is None:
            from src.modules.window.wechat_window_messages import WindowMessageTransport
            self._message_transport = WindowMessageTransport(
                process_names={"weixin.exe", "wechat.exe", "wechatappex.exe"})
        return self._message_transport

    def activate(self, home_window: WindowInfo, *, force: bool = False) -> None:
        if self.message_mode:
            from types import SimpleNamespace
            self._window_messages().activate(SimpleNamespace(window=home_window), force=force)
            return
        if self._platform_name != "Windows":
            raise HomeWindowNotClickableError("微信主页激活仅支持 Windows")
        if home_window.handle <= 0:
            raise HomeWindowNotClickableError("微信主页窗口句柄无效")

        user32 = self._resolve_user32()
        if not bool(user32.IsWindow(int(home_window.handle))):
            raise HomeWindowNotClickableError(
                f"微信主页窗口已经失效：{home_window.handle}"
            )
        is_iconic = bool(user32.IsIconic(int(home_window.handle)))
        if not is_iconic and not force:
            foreground = int(user32.GetForegroundWindow() or 0)
            if _belongs_to_home(user32, home_window.handle, foreground):
                # 关闭文章标签后主页通常已经在前台，直接复用可省去固定激活等待。
                return
        if is_iconic:
            user32.ShowWindow(int(home_window.handle), SW_RESTORE)

        # UIA SetActive 对 Chromium 壳通常比单独调用 SetForegroundWindow 更稳定。
        control = home_window.control
        set_active = getattr(control, "SetActive", None)
        if callable(set_active):
            try:
                set_active()
            except Exception:
                pass
        user32.BringWindowToTop(int(home_window.handle))
        user32.SetForegroundWindow(int(home_window.handle))
        foreground = int(user32.GetForegroundWindow() or 0)
        if not _belongs_to_home(user32, home_window.handle, foreground):
            self._activate_with_attached_input(user32, home_window.handle, foreground)
        if self._activation_wait_seconds:
            self._sleep(self._activation_wait_seconds)

    def ensure_target_clickable(
        self,
        home_window: WindowInfo,
        target: ArticleTarget,
    ) -> None:
        if target.home_window_handle != home_window.handle:
            raise HomeWindowNotClickableError(
                "目标文章与当前微信主页窗口句柄不一致"
            )
        if self.message_mode:
            from types import SimpleNamespace
            self._window_messages().check_point(
                SimpleNamespace(window=home_window), target.click_x, target.click_y)
            return
        user32 = self._resolve_user32()
        point = _Point(int(target.click_x), int(target.click_y))
        # UIA 的 BoundingRectangle 与随后 Control.Click 使用当前线程的屏幕坐标。
        # 临时切到 Per-Monitor-V2 会在混合 DPI/远程显示环境里把同一坐标再次换算，
        # 导致 WindowFromPoint 命中 Explorer 的 Backstop Window 而不是微信。
        owner_handle = int(user32.WindowFromPoint(point) or 0)

        if _belongs_to_home(user32, home_window.handle, owner_handle):
            return
        raise HomeWindowNotClickableError(
            "文章坐标不在微信主页可点击层级："
            f"({target.click_x}, {target.click_y}) 当前属于窗口 {owner_handle}",
            owner_handle=owner_handle,
        )

    def _resolve_user32(self) -> Any:
        if self._user32 is not None:
            return self._user32
        return ctypes.windll.user32

    def _activate_with_attached_input(
        self,
        user32: Any,
        home_handle: int,
        foreground_handle: int,
    ) -> None:
        kernel32 = self._kernel32 or ctypes.windll.kernel32
        current_thread = int(kernel32.GetCurrentThreadId() or 0)
        foreground_thread = int(
            user32.GetWindowThreadProcessId(int(foreground_handle), None) or 0
        )
        attached = False
        if current_thread > 0 and foreground_thread > 0 and current_thread != foreground_thread:
            attached = bool(
                user32.AttachThreadInput(current_thread, foreground_thread, True)
            )
        try:
            user32.BringWindowToTop(int(home_handle))
            user32.SetForegroundWindow(int(home_handle))
        finally:
            if attached:
                user32.AttachThreadInput(current_thread, foreground_thread, False)


def _belongs_to_home(user32: Any, home_handle: int, owner_handle: int) -> bool:
    if owner_handle <= 0:
        return False
    if owner_handle == int(home_handle):
        return True
    try:
        if bool(user32.IsChild(int(home_handle), int(owner_handle))):
            return True
        return int(user32.GetAncestor(int(owner_handle), GA_ROOT) or 0) == int(home_handle)
    except Exception:
        return False


__all__ = ["HomeWindowNotClickableError", "WindowsHomeWindowGuard"]
