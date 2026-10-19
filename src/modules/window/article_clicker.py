from __future__ import annotations

import ctypes
from dataclasses import dataclass
import os
import platform
import time
from typing import Any, Callable

from src.domain.models import ArticleTarget


WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
MK_LBUTTON = 0x0001


@dataclass(frozen=True, slots=True)
class ClickResult:
    method: str
    target_handle: int
    click_x: int
    click_y: int
    native_error: str = ""


class ArticleClicker:
    """对已刷新确认的文章目标执行一次窗口消息点击。"""

    def __init__(
        self,
        *,
        native_click: Callable[[int, int, int], None] | None = None,
        uia_click: Callable[[Any], None] | None = None,
        screen_click: Callable[..., None] | None = None,
    ) -> None:
        self._native_click = native_click or _post_message_click
        # 兼容旧工厂/测试注入参数，但主页点击不再调用 UIA Click 或系统鼠标。
        del uia_click, screen_click
        self._article_message_transport = None

    def click(self, target: ArticleTarget) -> ClickResult:
        if os.environ.get("WECHAT_WINDOW_MESSAGES") == "1":
            return self._invoke_message_article(target)
        return self.click_point(
            target.home_window_handle,
            target.click_x,
            target.click_y,
        )

    def _resolve_message_target(self, target: ArticleTarget) -> ArticleTarget:
        """队列只传元数据；在执行线程重新读取同一主页，绑定唯一文章控件。"""
        from src.modules.window.wechat_home_window_finder import enumerate_win32_windows, attach_uia_control
        from src.modules.window.wechat_home_reader import WechatHomeReader
        from src.modules.window.article_card_reader import UiaArticleCardReader
        windows = [w for w in enumerate_win32_windows() if w.handle == target.home_window_handle]
        if len(windows) != 1:
            raise RuntimeError("后台文章主页句柄已失效")
        window = attach_uia_control(windows[0])
        if WechatHomeReader().read(window).account_name != target.account_name:
            raise RuntimeError("后台文章主页账号已变化")
        rows = UiaArticleCardReader().read_viewport(window, account_name=target.account_name).targets
        matches = [r for r in rows if " ".join(r.title.split()) == " ".join(target.title.split())
                   and (not target.published_date or r.published_date == target.published_date)]
        if len(matches) != 1:
            raise RuntimeError(f"后台文章当前不可唯一定位: {target.title}; matches={len(matches)}")
        return matches[0]

    def _invoke_message_article(self, target: ArticleTarget) -> ClickResult:
        """后台模式调用精确文章卡片的 UIA Invoke，不把合成鼠标事件当作成功。"""
        from types import SimpleNamespace
        from src.modules.window.wechat_home_window_finder import rect_to_tuple
        if target.control is None:
            target = self._resolve_message_target(target)
        if self._article_message_transport is None:
            from src.modules.window.wechat_window_messages import WindowMessageTransport
            self._article_message_transport = WindowMessageTransport(process_names={"wechatappex.exe"})
        context = SimpleNamespace(window=SimpleNamespace(
            handle=target.home_window_handle, control=target.control))
        self._article_message_transport.activate(context)
        self._article_message_transport.check_point(context, target.click_x, target.click_y)
        expected = " ".join(target.title.split())
        current = target.control
        for _ in range(5):
            if current is None:
                break
            if current.ProcessId != target.control.ProcessId:
                raise RuntimeError("文章卡片进程归属改变，已停止调用")
            pattern = current.GetPattern(10000)  # UIA InvokePattern
            if pattern is not None:
                queue, nodes = [current], []
                while queue:
                    node = queue.pop()
                    nodes.append(node)
                    if len(nodes) > 32:
                        raise RuntimeError("文章可调用卡片范围超限，已停止")
                    queue.extend(node.GetChildren())
                matches = [node for node in nodes if " ".join(node.Name.split()) == expected]
                left, top, right, bottom = rect_to_tuple(current.BoundingRectangle)
                if len(matches) == 1 and left <= target.click_x < right and top <= target.click_y < bottom:
                    method = invoke_accessible_action(current)
                    return ClickResult(method, target.home_window_handle, target.click_x, target.click_y)
            current = current.GetParentControl()
        raise RuntimeError("后台模式未找到已核对文章标题的可调用卡片")

    def click_point(
        self,
        home_window_handle: int,
        click_x: int,
        click_y: int,
    ) -> ClickResult:
        """向主页指定屏幕坐标发送一次后台点击，不移动系统鼠标。"""

        try:
            # 主页文章点击统一发给微信窗口/子窗口，不调用系统鼠标，避免抢占用户当前鼠标。
            self._native_click(
                int(home_window_handle),
                int(click_x),
                int(click_y),
            )
            return ClickResult(
                method="win32_post_message",
                target_handle=int(home_window_handle),
                click_x=int(click_x),
                click_y=int(click_y),
            )
        except Exception as native_error:
            raise RuntimeError(f"Win32 点击失败：{native_error}") from native_error


def invoke_accessible_action(control: Any) -> str:
    """优先使用控件明确提供的 MSAA 默认动作；不在一次调用失败后重发。"""
    legacy = control.GetPattern(10018)  # LegacyIAccessiblePattern
    action = legacy.DefaultAction if legacy is not None else ""
    if isinstance(action, str) and action.strip().lower() in {"按", "按下", "点击", "单击", "press", "click"}:
        legacy.DoDefaultAction()
        return "uia_legacy_default_action"
    pattern = control.GetPattern(10000)
    if pattern is None:
        raise RuntimeError("后台控件没有可调用的动作")
    pattern.Invoke()
    return "uia_invoke"


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _post_message_click(hwnd: int, screen_x: int, screen_y: int) -> None:
    if platform.system() != "Windows":
        raise OSError("Win32 点击仅支持 Windows")
    if hwnd <= 0:
        raise ValueError("点击目标 hwnd 不能为空")
    user32 = ctypes.windll.user32
    screen_point = _Point(int(screen_x), int(screen_y))
    setter, previous_context = _enter_per_monitor_dpi_context(user32)
    try:
        # UIA 给出物理坐标；WindowFromPoint 也必须在同一个 DPI 坐标域内执行。
        target_hwnd = _resolve_click_message_target(user32, int(hwnd), screen_point)
        point = _Point(screen_point.x, screen_point.y)
        if not user32.ScreenToClient(target_hwnd, ctypes.byref(point)):
            raise RuntimeError("屏幕坐标转换为窗口坐标失败")
    finally:
        _restore_dpi_context(setter, previous_context)
    lparam = (point.y & 0xFFFF) << 16 | (point.x & 0xFFFF)
    if not user32.PostMessageW(target_hwnd, WM_MOUSEMOVE, 0, lparam):
        raise RuntimeError("发送鼠标移动消息失败")
    time.sleep(0.02)
    if not user32.PostMessageW(target_hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lparam):
        raise RuntimeError("发送鼠标按下消息失败")
    time.sleep(0.04)
    if not user32.PostMessageW(target_hwnd, WM_LBUTTONUP, 0, lparam):
        raise RuntimeError("发送鼠标释放消息失败")
    # Chromium 会异步处理合成鼠标消息，短暂等待可避免标题轮询先于导航开始。
    time.sleep(0.25)


def _resolve_click_message_target(user32: Any, home_hwnd: int, point: _Point) -> int:
    """只允许把消息发给主页本身或鼠标点下方的主页子窗口。"""
    if os.environ.get("WECHAT_WINDOW_MESSAGES") == "1":
        return _message_child_at_point(user32, home_hwnd, point)
    try:
        candidate = int(user32.WindowFromPoint(point) or 0)
        if candidate == int(home_hwnd):
            return candidate
        if candidate > 0 and bool(user32.IsChild(int(home_hwnd), candidate)):
            return candidate
    except Exception:
        pass
    return int(home_hwnd)


def _message_child_at_point(user32: Any, home_hwnd: int, point: _Point) -> int:
    """只在已核验主页内部按原生子窗口层级命中，锁屏不会改变接收窗口。"""
    from ctypes import wintypes
    query = user32.ChildWindowFromPointEx
    try:
        query.argtypes = [wintypes.HWND, _Point, wintypes.UINT]
        query.restype = wintypes.HWND
    except AttributeError:
        pass
    root_pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(home_hwnd, ctypes.byref(root_pid))
    if not root_pid.value:
        raise RuntimeError("主页进程身份无法核验")
    current = int(home_hwnd)
    for _ in range(8):
        local = _Point(point.x, point.y)
        if not user32.ScreenToClient(current, ctypes.byref(local)):
            raise RuntimeError("后台子窗口坐标转换失败")
        # Chromium 渲染 HWND 带透明样式，但仍是定向消息接收者；不能按全屏命中跳过它。
        child = int(query(current, local, 1 | 2) or 0)
        if not child:
            raise RuntimeError("后台子窗口命中失败，已停止点击")
        if child == current:
            return current
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(child, ctypes.byref(pid))
        if not user32.IsChild(home_hwnd, child) or pid.value != root_pid.value:
            raise RuntimeError("后台点击子窗口不属于已核验的微信主页进程")
        current = child
    raise RuntimeError("后台子窗口层级超限，已停止点击")


def _screen_to_client_physical(user32: Any, hwnd: int, point: _Point) -> None:
    """在每显示器 DPI 坐标域中转换 UIA 物理坐标，避免缩放后点击偏移。"""
    setter, previous_context = _enter_per_monitor_dpi_context(user32)
    try:
        if not user32.ScreenToClient(int(hwnd), ctypes.byref(point)):
            raise RuntimeError("屏幕坐标转换为窗口坐标失败")
    finally:
        _restore_dpi_context(setter, previous_context)


def _enter_per_monitor_dpi_context(user32: Any) -> tuple[Any, Any]:
    setter = getattr(user32, "SetThreadDpiAwarenessContext", None)
    previous_context: Any = None
    if callable(setter):
        try:
            setter.argtypes = [ctypes.c_void_p]
            setter.restype = ctypes.c_void_p
        except Exception:
            pass
        previous_context = setter(ctypes.c_void_p(-4))
    return setter, previous_context


def _restore_dpi_context(setter: Any, previous_context: Any) -> None:
    if callable(setter) and previous_context:
        raw_context = getattr(previous_context, "value", previous_context)
        setter(ctypes.c_void_p(raw_context))
