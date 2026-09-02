from __future__ import annotations

import unittest
from unittest.mock import patch

from src.modules.window import wechat_home_window_finder as finder
from src.modules.window.window_models import WindowInfo


def make_home_window(*, control: object | None = None) -> WindowInfo:
    """创建一个符合公众号主页外壳规则的测试窗口。"""
    return WindowInfo(
        handle=853206,
        title="微信",
        class_name="Chrome_WidgetWin_0",
        process_name="WeChatAppEx.exe",
        rect=(100, 100, 1200, 900),
        control=control,
    )


class WechatHomeWindowFinderTests(unittest.TestCase):
    def test_desktop_enumeration_prefers_win32(self) -> None:
        """Win32 有结果时不应触发可能阻塞的 UIA 桌面枚举。"""
        window = make_home_window()
        with (
            patch.object(finder, "enumerate_win32_windows", return_value=[window]),
            patch.object(finder, "enumerate_uia_windows") as enumerate_uia,
        ):
            result = finder.enumerate_desktop_windows()

        self.assertEqual(result, [window])
        enumerate_uia.assert_not_called()

    def test_desktop_enumeration_falls_back_to_uia(self) -> None:
        """Win32 不可用时仍保留原有 UIA 回退路径。"""
        window = make_home_window()
        with (
            patch.object(finder, "enumerate_win32_windows", return_value=[]),
            patch.object(finder, "enumerate_uia_windows", return_value=[window]),
        ):
            result = finder.enumerate_desktop_windows()

        self.assertEqual(result, [window])

    def test_selected_window_is_attached_to_uia_by_handle(self) -> None:
        """选择 Win32 窗口后再按句柄挂接 UIA 控件。"""
        window = make_home_window()
        attached = make_home_window(control=object())
        with (
            patch.object(finder, "enumerate_desktop_windows", return_value=[window]),
            patch.object(finder, "attach_uia_control", return_value=attached) as attach,
        ):
            result = finder.find_wechat_home_window(use_article_probe=False)

        self.assertIs(result, attached)
        attach.assert_called_once_with(window)


if __name__ == "__main__":
    unittest.main()
