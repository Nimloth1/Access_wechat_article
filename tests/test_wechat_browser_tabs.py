from __future__ import annotations

import unittest
from unittest.mock import patch

from src.modules.window import wechat_browser_tabs as browser_tabs
from src.modules.window.window_models import BrowserTabInfo, WindowInfo


def make_window(
    *,
    process_name: str = "WeChatAppEx.exe",
    class_name: str = "Chrome_WidgetWin_0",
    visible: bool = True,
    is_minimized: bool = False,
    rect: tuple[int, int, int, int] = (100, 100, 1200, 900),
    control: object | None = None,
) -> WindowInfo:
    """创建浏览器窗口枚举测试数据。"""
    return WindowInfo(
        handle=853206,
        title="微信",
        class_name=class_name,
        process_name=process_name,
        rect=rect,
        visible=visible,
        is_minimized=is_minimized,
        control=control,
    )


class WechatBrowserWindowEnumerationTests(unittest.TestCase):
    def test_win32_candidates_are_filtered_before_uia_attachment(self) -> None:
        """只对可操作的微信 Chromium 窗口挂接 UIA。"""
        candidate = make_window()
        unrelated = make_window(process_name="chrome.exe")
        attached = make_window(control=object())
        with (
            patch.object(
                browser_tabs,
                "enumerate_win32_windows",
                return_value=[candidate, unrelated],
            ),
            patch.object(browser_tabs, "enumerate_uia_windows") as enumerate_uia,
            patch.object(
                browser_tabs,
                "attach_uia_control",
                return_value=attached,
            ) as attach,
        ):
            result = browser_tabs.enumerate_wechat_chrome_windows()

        self.assertEqual(result, [attached])
        attach.assert_called_once_with(candidate)
        enumerate_uia.assert_not_called()

    def test_uia_is_only_a_fallback_when_win32_has_no_candidate(self) -> None:
        """Win32 没有候选窗口时才允许回退全桌面 UIA。"""
        fallback = make_window(control=object())
        with (
            patch.object(
                browser_tabs,
                "enumerate_win32_windows",
                return_value=[],
            ),
            patch.object(
                browser_tabs,
                "enumerate_uia_windows",
                return_value=[fallback],
            ) as enumerate_uia,
        ):
            result = browser_tabs.enumerate_wechat_chrome_windows()

        self.assertEqual(result, [fallback])
        enumerate_uia.assert_called_once_with()

    def test_adapter_uses_fast_wechat_enumerator_by_default(self) -> None:
        """标签适配器默认不得直接使用全桌面 UIA 枚举。"""
        adapter = browser_tabs.UiaWechatBrowserTabAdapter()

        self.assertIs(
            adapter._enumerate_windows,
            browser_tabs.enumerate_wechat_chrome_windows,
        )

    def test_stale_document_title_does_not_discard_captured_article(self) -> None:
        """微信返回主页后标题可能滞留，不能把它当成关闭失败。"""
        times = iter((0.0, 1.0))
        selected = BrowserTabInfo(
            tab_id="document:853206",
            owner_handle=853206,
            title="生猪盘面小调研",
            rect=(100, 100, 1200, 900),
            is_active=True,
        )
        adapter = browser_tabs.UiaWechatBrowserTabAdapter(
            enumerate_windows=lambda: [],
            document_return_timeout_seconds=0.1,
            monotonic=lambda: next(times),
            sleep=lambda _seconds: None,
        )
        adapter.active_tab = lambda: selected

        self.assertFalse(adapter._wait_until_document_title_changes(selected))

    def test_transient_document_title_change_does_not_confirm_close(self) -> None:
        """标题短暂变化后恢复时不能把仍打开的文章误判为已关闭。"""
        times = iter((0.0, 0.01, 0.02, 1.0))
        selected = BrowserTabInfo(
            tab_id="document:853206",
            owner_handle=853206,
            title="降波了",
            rect=(100, 100, 1200, 900),
            is_active=True,
        )
        temporary = BrowserTabInfo(
            tab_id=selected.tab_id,
            owner_handle=selected.owner_handle,
            title="加载中",
            rect=selected.rect,
            is_active=True,
        )
        states = iter((temporary, selected, selected, selected))
        adapter = browser_tabs.UiaWechatBrowserTabAdapter(
            enumerate_windows=lambda: [],
            document_return_timeout_seconds=0.1,
            monotonic=lambda: next(times),
            sleep=lambda _seconds: None,
        )
        adapter.active_tab = lambda: next(states)

        self.assertFalse(adapter._wait_until_document_title_changes(selected))

    def test_keyboard_activation_preserves_existing_document_focus(self) -> None:
        """窗口已在前台时不能用顶层 SetFocus 覆盖文章 Document 焦点。"""
        class User32:
            def __init__(self) -> None:
                self.focused: list[int] = []

            @staticmethod
            def GetForegroundWindow() -> int:
                return 853206

            def SetFocus(self, handle: int) -> None:
                self.focused.append(handle)

        user32 = User32()

        browser_tabs._activate_keyboard_target(
            user32,
            853206,
            sleep=lambda _seconds: None,
        )

        self.assertEqual(user32.focused, [])

    def test_ctrl_w_is_sent_directly_to_article_document_control(self) -> None:
        """优先向文章控件发键，避免快捷键落到微信顶层窗口。"""
        class DocumentControl:
            def __init__(self) -> None:
                self.keys: list[str] = []

            def SendKeys(self, keys: str) -> None:
                self.keys.append(keys)

        control = DocumentControl()

        self.assertTrue(browser_tabs._send_ctrl_w_to_control(control))
        self.assertEqual(control.keys, ["{Ctrl}w"])

    def test_article_tab_close_invokes_only_direct_close_button(self) -> None:
        """只允许调用已匹配文章标签行的直接子级关闭按钮。"""
        class Control:
            def __init__(self, name: str, control_type: str = "TextControl") -> None:
                self.Name = name
                self.ControlTypeName = control_type
                self.invoked = 0
                self.children: list[Control] = []

            def GetChildren(self) -> list[Control]:
                return self.children

            def Click(self) -> None:
                self.invoked += 1

        title = Control("降波了")
        tab_close = Control("关闭", "ButtonControl")
        window_close = Control("关闭", "ButtonControl")
        tab_row = Control("")
        tab_row.children = [title, tab_close]
        window_root = Control("")
        window_root.children = [window_close, tab_row]

        self.assertTrue(browser_tabs._invoke_direct_close_button(tab_row))
        self.assertEqual(tab_close.invoked, 1)
        self.assertEqual(window_close.invoked, 0)

    def test_article_tab_close_falls_back_when_direct_button_is_unavailable(self) -> None:
        """标签行中没有关闭按钮时保留快捷键兜底路径。"""
        class TabRow:
            @staticmethod
            def GetChildren() -> list[object]:
                return []

        self.assertFalse(browser_tabs._invoke_direct_close_button(TabRow()))


if __name__ == "__main__":
    unittest.main()
