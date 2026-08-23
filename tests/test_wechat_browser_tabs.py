from __future__ import annotations

import unittest
from unittest.mock import patch

from src.modules.window import wechat_browser_tabs as browser_tabs
from src.modules.window.window_models import WindowInfo


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


if __name__ == "__main__":
    unittest.main()
