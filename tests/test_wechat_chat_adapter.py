# encoding: UTF-8
"""用控件树快照验证适配器的停止条件，不触碰真实微信。"""
from datetime import date
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from src.modules.window.wechat_chat_adapter import ChatContext, ORIGINAL_NAMES, WechatChatAdapter
from src.services.chat_attachment_service import ChatAutomationError, ChatMessage, ChatPage


class Node:
    """实现测试需要的最小 UIA 控件接口。"""

    def __init__(self, name='', kind='ListItemControl', cls='', children=(), identity=1, rect=(0, 10, 300, 100)):
        """创建有边界、类型与运行时标识的控件。"""
        self.Name = name
        self.ControlTypeName = kind
        self.ClassName = cls
        self.AutomationId = ''
        self.IsOffscreen = False
        self.BoundingRectangle = rect
        self.children = children
        self.identity = identity
        self.pattern = None

    def GetChildren(self):
        """返回控件子节点。"""
        return self.children

    def GetRuntimeId(self):
        """返回控件实例标识。"""
        return [self.identity]

    def GetScrollPattern(self):
        """返回模拟滚动属性。"""
        return self.pattern


class WechatChatAdapterTests(unittest.TestCase):
    def adapter(self, listing):
        """构建不导入 Windows UIA 的只读适配器。"""
        adapter = WechatChatAdapter.__new__(WechatChatAdapter)
        adapter.today = date(2026, 9, 6)
        adapter._list = lambda context: listing
        return adapter

    def test_date_in_message_body_does_not_become_message_time(self):
        """普通正文含日期时不可触发截止条件。"""
        body = Node('2020-01-01', 'TextControl', 'mmui::ChatTextItemView')
        row = Node('2020-01-01', cls='mmui::ChatTextItemView', children=(body,))
        listing = Node(kind='ListControl', children=(row,), rect=(0, 0, 400, 500))
        page = self.adapter(listing).read_page(ChatContext(None, 'A'))
        self.assertIsNone(page.messages[0].day)

    def test_date_separator_applies_to_following_messages_only(self):
        """日期标记只向下归属，标记上方的日期保持未知。"""
        before = Node('old', identity=1)
        header = Node('2026年9月5日 10:00', cls='mmui::ChatItemView')
        after = Node('new', identity=2)
        listing = Node(kind='ListControl', children=(before, header, after), rect=(0, 0, 400, 500))
        page = self.adapter(listing).read_page(ChatContext(None, 'A'))
        self.assertEqual(len(page.messages), 2)
        self.assertIsNone(page.messages[0].day)
        self.assertEqual(page.messages[1].day, date(2026, 9, 5))

    def test_partial_row_is_counted_without_using_row_center(self):
        """跨页长消息保留作重叠锚点；附件打开使用控件 Invoke 而非行中心。"""
        partial = Node('partial', rect=(0, -10, 300, 100))
        full = Node('full', identity=2)
        listing = Node(kind='ListControl', children=(partial, full), rect=(0, 0, 400, 500))
        page = self.adapter(listing).read_page(ChatContext(None, 'A'))
        self.assertEqual(len(page.messages), 2)
        self.assertEqual(page.messages[0].control[0], (1,))

    def test_scroll_top_is_not_outer_history_end(self):
        """外层顶部可能触发懒加载，不能直接报告扫描全部完成。"""
        listing = Node(kind='ListControl', children=(Node('message'),), rect=(0, 0, 400, 500))
        listing.pattern = SimpleNamespace(VerticallyScrollable=True, VerticalScrollPercent=0)
        adapter = self.adapter(listing)
        self.assertFalse(adapter.read_page(ChatContext(None, 'A')).at_start)
        self.assertTrue(adapter.read_page(ChatContext(None, 'A', True)).at_start)

    def test_recycled_or_missing_message_identity_stops(self):
        """相同文本但不同控件实例不能使用旧定位继续点击。"""
        adapter = self.adapter(None)
        original = ChatMessage('same', 'image', None, ((1,), object()))
        replacement = ChatMessage('same', 'image', None, ((2,), object()))
        adapter.read_page = lambda context: ChatPage((replacement,))
        with self.assertRaises(ChatAutomationError):
            adapter._fresh_target(None, original)

    def test_file_without_explicit_download_control_is_not_opened(self):
        """文件名不是下载按钮，不可双击文件作为下载的兜底。"""
        adapter = self.adapter(None)
        adapter.cancelled = lambda: False
        adapter._fresh_target = lambda *args: Node('附件.exe')
        adapter._file_menu_download = lambda *args: 'unconfirmed'
        adapter._click = lambda *args, **kwargs: self.fail('不应点击附件文件')
        self.assertEqual(adapter._download_file(None, None, 1), 'unconfirmed')

    def test_scroll_waits_through_long_message_and_returns_when_new_row_appears(self):
        """长消息滚动中身份未变化仍继续，出现新行后立即交给服务核验衔接。"""
        listing = Node(kind='ListControl')
        adapter = self.adapter(listing)
        adapter._activate = Mock()
        adapter.cancelled = lambda: False
        adapter._pause = lambda seconds: None
        adapter._wheel = Mock()
        adapter._page_position = Mock(side_effect=[(100,), (80,)])
        first = ChatMessage('one', 'text', None, ((1,), None))
        earlier = ChatMessage('zero', 'text', None, ((2,), None))
        adapter.read_page = Mock(side_effect=[ChatPage((first,)), ChatPage((first,)), ChatPage((earlier, first))])
        adapter.scroll_older(ChatContext(None, 'A'))
        self.assertEqual(adapter._wheel.call_count, 2)

    def test_forward_stationary_scroll_marks_start_but_outer_does_not(self):
        """有限转发的稳定边缘可确认起点，外层懒加载不能据此报告全部完成。"""
        for forwarded in (True, False):
            adapter = self.adapter(Node(kind='ListControl'))
            adapter._activate = Mock()
            adapter.cancelled = lambda: False
            adapter._pause = lambda seconds: None
            adapter._wheel = Mock()
            adapter._page_position = lambda context: (100,)
            adapter.read_page = lambda context: ChatPage((ChatMessage('one', 'text', None, ((1,), None)),))
            context = ChatContext(None, 'A', forwarded)
            if forwarded:
                adapter.scroll_older(context)
                self.assertTrue(context.reached_start)
            else:
                with self.assertRaises(ChatAutomationError):
                    adapter.scroll_older(context)

    def test_program_file_never_uses_single_click_download_fallback(self):
        """未下载程序没有明确下载入口时也不点击，避免微信下载后自动运行。"""
        adapter = self.adapter(None)
        adapter.cancelled = lambda: False
        adapter._fresh_target = lambda *args: Node('文件\nsetup.exe\n未下载')
        adapter._file_menu_download = lambda *args: 'unconfirmed'
        adapter._bubble_point = Mock(side_effect=AssertionError('不应定位可执行文件'))
        self.assertEqual(adapter._download_file(None, None, 1), 'unconfirmed')

    def test_file_cannot_use_image_double_click_entry(self):
        """即使调用者传错入口，也不能把文件作为图片双击打开。"""
        adapter = self.adapter(None)
        with self.assertRaises(ChatAutomationError):
            adapter._open_bubble(None, ChatMessage('file', 'file', None), Mock())

    def test_original_button_accepts_size_suffix_but_not_unrelated_text(self):
        """原图按钮可以包含文件大小，但普通说明或正文不是下载入口。"""
        button = Node('查看原图（1.2 MB）', kind='ButtonControl')
        other = Node('查看原图说明', kind='ButtonControl')
        text = Node('查看原图', kind='TextControl')
        adapter = self.adapter(None)
        adapter._nodes = lambda context: [button, other, text]
        self.assertEqual(adapter._buttons(None, ORIGINAL_NAMES), [button])

    def test_receipt_requires_exact_filename_and_cancels_only_matching_dialog(self):
        """接收和取消操作都限定在本次文件名匹配的对话框内。"""
        filename = Node('report.pdf', kind='TextControl')
        accept = Node('接收文件', kind='ButtonControl')
        cancel = Node('取消', kind='ButtonControl')
        dialog = Node(cls='mmui::XDialog', children=(filename, accept, cancel))
        adapter = self.adapter(None)
        adapter._nodes = lambda context: [dialog]
        adapter._click = Mock()
        adapter._pause = lambda seconds: None
        adapter.cancelled = lambda: False
        adapter._file_receipt(None, 'other.pdf', accept=True)
        adapter._click.assert_not_called()
        adapter._file_receipt(None, 'report.pdf', accept=True)
        adapter._click.assert_called_with(None, accept, cleanup=False)
        adapter._file_receipt(None, 'report.pdf', accept=False)
        adapter._click.assert_called_with(None, cancel, cleanup=True)

    def test_locked_desktop_stops_before_window_activation(self):
        """输入桌面不可访问时不尝试激活微信或发送按键。"""
        adapter = self.adapter(None)
        adapter.guard = SimpleNamespace(activate=Mock())
        user32 = SimpleNamespace(OpenInputDesktop=Mock(return_value=None), CloseDesktop=Mock())
        with patch('ctypes.WinDLL', return_value=user32, create=True), self.assertRaisesRegex(ChatAutomationError, '解锁'):
            adapter._activate(ChatContext(None, 'A'))
        adapter.guard.activate.assert_not_called()

    def test_chat_navigation_waits_for_exact_title(self):
        """调用搜索结果后仍显示旧标题时等待刷新，确认完整目标名后才返回。"""
        old = Node('旧群', kind='TextControl')
        old.AutomationId = 'current_chat_name_label'
        target = Node('180k拼团', kind='TextControl')
        target.AutomationId = 'current_chat_name_label'
        search = Node('搜索', kind='EditControl', cls='mmui::XValidatorTextEdit')
        search.SetFocus = Mock()
        value = SimpleNamespace(SetValue=Mock())
        search.GetValuePattern = lambda: value
        result = Node('180k拼团')
        results = Node(kind='ListControl', children=(result,))
        results.AutomationId = 'search_list'
        root = Node(kind='WindowControl', children=(old, search))
        adapter = self.adapter(None)
        adapter.main = SimpleNamespace(control=root)
        adapter.diagnose = lambda: {'ready': True}
        adapter._nodes = lambda context: [results]
        adapter._activate = Mock()
        adapter._focus_within = lambda control: True
        adapter._click = Mock()
        adapter._pause = Mock()
        adapter.cancelled = lambda: False
        adapter._window = Mock(side_effect=[
            SimpleNamespace(control=Node(children=(old,))),
            SimpleNamespace(control=Node(children=(target,))),
        ])
        context = adapter.open_chat('180k拼团')
        self.assertEqual(context.name, '180k拼团')
        self.assertEqual(adapter._window.call_count, 2)
        value.SetValue.assert_called_once_with('180k拼团')


if __name__ == '__main__':
    unittest.main()
