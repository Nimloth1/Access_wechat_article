from __future__ import annotations

import unittest
from dataclasses import replace
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
            result = finder.find_wechat_home_window()

        self.assertIs(result, attached)
        attach.assert_called_once_with(window)

    def test_generic_window_does_not_require_readable_articles_before_activation(self) -> None:
        """Win32 返回无 UIA 的微信窗口，默认入口仍须找到它。"""
        window = make_home_window()
        with patch.object(finder, "attach_uia_control", side_effect=lambda value: value):
            result = finder.find_wechat_home_window(enumerate_windows=lambda: [window])
        self.assertIs(result, window)

    def test_unrelated_and_hidden_windows_are_rejected(self) -> None:
        """不把聊天窗、文章标题窗口或隐藏的 Chromium 辅助窗口当成主页。"""
        window = make_home_window()
        for changes in (
            {"process_name": "Weixin.exe"},
            {"class_name": "Qt51514QWindowIcon"},
            {"title": "某篇文章"},
            {"visible": False},
            {"rect": (0, 0, 0, 0)},
        ):
            with self.subTest(changes=changes):
                self.assertIsNone(finder.find_wechat_home_window(
                    enumerate_windows=lambda: [replace(window, **changes)],
                ))

    def test_explicit_home_title_wins_over_generic_title(self) -> None:
        """多个微信窗口时优先选择明确的公众号主页。"""
        generic = make_home_window()
        explicit = replace(generic, handle=1, title="公众号")
        with patch.object(finder, "attach_uia_control", side_effect=lambda value: value):
            result = finder.find_wechat_home_window(
                enumerate_windows=lambda: [generic, explicit],
            )
        self.assertIs(result, explicit)

    def test_minimized_home_reports_actionable_error(self) -> None:
        """最小化主页仍返回专门异常，避免误报未打开。"""
        window = replace(make_home_window(), is_minimized=True)
        with patch.object(finder, "attach_uia_control", side_effect=lambda value: value):
            with self.assertRaises(finder.WechatHomeWindowMinimized):
                finder.find_wechat_home_window(enumerate_windows=lambda: [window])


if __name__ == "__main__":
    unittest.main()
