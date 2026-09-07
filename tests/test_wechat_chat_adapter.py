# encoding: UTF-8
"""用控件树快照验证适配器的停止条件，不触碰真实微信。"""
from datetime import date
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from src.modules.window.wechat_chat_adapter import ChatContext, WechatChatAdapter
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

    def test_key_input_keeps_verified_row_focus_and_rechecks_before_each_key(self):
        """不能调用会重新聚焦列表的 Control.SendKeys；焦点离开后不再输入。"""
        adapter = self.adapter(None)
        adapter._nodes = Mock()
        adapter._activate = Mock()
        adapter.cancelled = lambda: False
        adapter._focus_within = Mock(side_effect=[True, False])
        adapter.auto = SimpleNamespace(SendKeys=Mock())
        control = SimpleNamespace(SendKeys=Mock(side_effect=AssertionError('焦点不应重置')))
        adapter._send_keys(None, control, '{Up}')
        with self.assertRaises(ChatAutomationError):
            adapter._send_keys(None, control, '{Up}')
        adapter.auto.SendKeys.assert_called_once_with('{Up}')

    def test_selection_that_moves_viewport_does_not_also_scroll(self):
        """选中长消息已改变视口时先核验重叠，防止额外向上移动丢失锚点。"""
        row = Node('old', identity=1)
        row.GetSelectionItemPattern = lambda: SimpleNamespace(Select=lambda: None)
        row.SetFocus = lambda: None
        listing = Node(kind='ListControl', children=(row,))
        adapter = self.adapter(listing)
        adapter._activate = Mock()
        adapter.cancelled = lambda: False
        adapter._focus_within = lambda control: True
        adapter._pause = lambda seconds: None
        adapter._send_keys = Mock(side_effect=AssertionError('不应叠加滚动'))
        first = ChatMessage('one', 'text', None, ((1,), None))
        earlier = ChatMessage('zero', 'text', None, ((2,), None))
        adapter.read_page = Mock(side_effect=[ChatPage((first,)), ChatPage((earlier, first))])
        adapter.scroll_older(ChatContext(None, 'A'))
        adapter._send_keys.assert_not_called()

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
