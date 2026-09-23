# encoding: UTF-8
"""用控件树快照验证适配器的停止条件，不触碰真实微信。"""
from datetime import date
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

from src.modules.window.wechat_chat_adapter import AttachmentNotClickableError, ChatContext, FORWARD_CLASS, ORIGINAL_NAMES, PREVIEW_CLASS, WechatChatAdapter, is_chat_list_search, local_chat_matches
from src.modules.window.home_window_activation import HomeWindowNotClickableError
from src.services.chat_attachment_service import ChatAutomationError, ChatMessage, ChatPage


class Node:
    """实现测试需要的最小 UIA 控件接口。"""

    def __init__(self, name='', kind='ListItemControl', cls='', children=(), identity=1, rect=(0, 10, 300, 100), parent=None):
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
        self.parent = parent

    def GetChildren(self):
        """返回控件子节点。"""
        return self.children

    def GetRuntimeId(self):
        """返回控件实例标识。"""
        return [self.identity]

    def GetParentControl(self):
        """返回模拟父控件。"""
        return self.parent

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

    def test_local_chat_match_accepts_most_used_but_excludes_history_and_web(self):
        """Fresh-login search may put a local chat under Most Used."""
        local = Node('180k拼团', children=(Node('180k拼团', kind='TextControl'),))
        history = Node('Jay Lee', children=(Node('180k拼团', kind='TextControl'),))
        web = Node('180k拼团', children=(Node('180k拼团', kind='TextControl'),))
        rows = [Node('最常使用'), local, Node('聊天记录'), history,
                Node('搜索网络结果'), web]

        self.assertEqual(local_chat_matches(rows, '180k拼团'), [local])

    @patch('src.modules.window.wechat_chat_adapter.temporary_accessibility')
    def test_compatibility_locates_shell_before_enabling_accessibility(self, temporary):
        """A fresh process must enable accessibility before inspecting descendants."""
        adapter = self.adapter(None)
        adapter.main = None
        adapter._locate_main = Mock(side_effect=lambda: setattr(
            adapter, 'main', SimpleNamespace(control=SimpleNamespace(ProcessId=123))))
        temporary.return_value.__enter__ = Mock(return_value=None)
        temporary.return_value.__exit__ = Mock(return_value=False)

        with adapter.compatibility(True):
            self.assertEqual(adapter.main.control.ProcessId, 123)

        adapter._locate_main.assert_called_once_with()
        temporary.assert_called_once_with(123)

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

    def test_read_page_reuses_date_after_runtime_id_changes(self):
        """点开附件导致控件重建时，稳定消息签名应避免再次探查日期。"""
        first = Node('same image', identity=1)
        listing = Node(kind='ListControl', children=(first,), rect=(0, 0, 400, 500))
        adapter = self.adapter(listing)
        context = ChatContext(None, 'A')
        original = adapter.read_page(context).messages[0]
        context.signature_dates[original.signature] = date(2026, 9, 5)

        listing.children = (Node('same image', identity=2),)

        rebuilt = adapter.read_page(context).messages[0]
        self.assertEqual(rebuilt.day, date(2026, 9, 5))
        self.assertEqual(context.dates[(2,)], date(2026, 9, 5))

    def test_date_scan_clicks_short_time_once_and_reads_expanded_label(self):
        """按日期扫描展开时间标签一次，使用控件文字而非正文或 OCR。"""
        header = Node('10:00', cls='mmui::ChatItemView')
        row = Node('message', identity=2)
        listing = Node(kind='ListControl', children=(header, row), rect=(0, 0, 400, 500))
        adapter = self.adapter(listing)
        adapter._pause = Mock()
        adapter._click = Mock(side_effect=lambda context, node: setattr(node, 'Name', '9月5日 星期六 10:00'))
        context = ChatContext(None, 'A', resolve_dates=True)
        self.assertEqual(adapter.read_page(context).messages[0].day, date(2026, 9, 5))
        adapter.read_page(context)
        adapter._click.assert_called_once_with(context, header)

    def test_date_probe_rebinds_rebuilt_rows_only_after_full_layout_match(self):
        """探查日期后仅在有序内容和位置均恢复时接受重建控件。"""
        for same_layout in (True, False):
            adapter = self.adapter(None)
            context = ChatContext(None, 'A')
            original = ChatPage((ChatMessage('a', 'image', None, ((1,), None)),))
            dated = ChatPage((ChatMessage('a', 'image', date(2026, 9, 5), ((1,), None)),))
            restored = ChatPage((ChatMessage('a', 'image', None, ((2,), None)),))
            adapter.read_page = Mock(side_effect=[original, dated] + [restored] * (1 if same_layout else 241))
            adapter._expand_dates = Mock()
            adapter._message_layout = Mock(side_effect=[('original',)] +
                                            [('original' if same_layout else 'changed',)] *
                                            (1 if same_layout else 241))
            adapter._page_position = Mock(return_value=('position',))
            adapter._wheel = Mock()
            adapter._pause = Mock()
            adapter.cancelled = lambda: False
            if same_layout:
                self.assertEqual(adapter.resolve_day(context, original.messages[0]), date(2026, 9, 5))
                self.assertEqual(context.identities[(2,)], (1,))
            else:
                with self.assertRaises(ChatAutomationError):
                    adapter.resolve_day(context, original.messages[0])
                self.assertFalse(context.identities)
            directions = [c.kwargs['older'] for c in adapter._wheel.call_args_list]
            self.assertEqual(directions[:2], [True, False])
            self.assertEqual(len(directions), 2 if same_layout else 482)

    def test_date_probe_never_accepts_lost_page_overlap(self):
        """向前找日期时跳过消息必须报错，不能把远处日期赋给目标。"""
        adapter = self.adapter(None)
        original = ChatPage((ChatMessage('a', 'image', None, ((1,), None)),))
        unrelated = ChatPage((ChatMessage('b', 'image', date(2026, 9, 5), ((2,), None)),))
        adapter.read_page = Mock(side_effect=[original, unrelated, original])
        adapter._expand_dates = Mock()
        adapter._message_layout = Mock(return_value=('layout',))
        adapter._page_position = Mock(return_value=('position',))
        adapter._wheel = Mock()
        adapter._pause = Mock()
        adapter.cancelled = lambda: False
        context = ChatContext(None, 'A')
        with self.assertRaises(ChatAutomationError):
            adapter.resolve_day(context, original.messages[0])
        self.assertFalse(context.dates)

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

    def test_reused_runtime_identity_does_not_merge_current_messages(self):
        """旧别名撞到当前真实 ID 时退回当前 ID，避免同名附件出现双匹配。"""
        first = Node('图片', cls='mmui::ChatImageItemView', identity=1)
        second = Node('图片', cls='mmui::ChatImageItemView', identity=2)
        listing = Node(kind='ListControl', children=(first, second), rect=(0, 0, 400, 500))
        context = ChatContext(None, 'A', identities={(1,): (2,)})
        page = self.adapter(listing).read_page(context)
        self.assertEqual([message.control[0] for message in page.messages], [(1,), (2,)])
        self.assertNotIn((1,), context.identities)

    def test_duplicate_alias_targets_fall_back_to_current_runtime_ids(self):
        """两个当前控件不能共享同一个历史别名。"""
        rows = (Node('图片', cls='mmui::ChatImageItemView', identity=1),
                Node('图片', cls='mmui::ChatImageItemView', identity=2))
        listing = Node(kind='ListControl', children=rows, rect=(0, 0, 400, 500))
        context = ChatContext(None, 'A', identities={(1,): (9,), (2,): (9,)})
        page = self.adapter(listing).read_page(context)
        self.assertEqual([message.control[0] for message in page.messages], [(1,), (2,)])
        self.assertFalse(context.identities)

    def test_file_without_explicit_download_control_is_not_opened(self):
        """文件名不是下载按钮，不可双击文件作为下载的兜底。"""
        adapter = self.adapter(None)
        adapter.cancelled = lambda: False
        adapter._fresh_target = lambda *args: Node('附件.exe')
        adapter._file_menu_download = lambda *args: 'unconfirmed'
        adapter._click = lambda *args, **kwargs: self.fail('不应点击附件文件')
        self.assertEqual(adapter._download_file(None, None, 1), 'skipped_file')

    def test_file_without_missing_marker_never_opens_menu(self):
        """没有未下载标记直接跳过，不浪费右键菜单核验动作。"""
        adapter = self.adapter(None)
        adapter.cancelled = lambda: False
        adapter._fresh_target = lambda *args: Node('文件\nreport.pdf\n2.8M')
        adapter._file_menu_download = Mock(side_effect=AssertionError('不应打开菜单'))
        self.assertEqual(adapter._download_file(None, None, 1), 'skipped_file')

    def test_missing_marker_on_size_line_triggers_document_download(self):
        """大小和未下载在同一行时仍能识别，已下载文件不进入此入口。"""
        bubble = Node('文件\nreport.pdf\n2.8M 未下载')
        bubble.Click = Mock()
        adapter = self.adapter(None)
        adapter.cancelled = lambda: False
        adapter._fresh_target = lambda *args: bubble
        adapter._bubble_point = lambda *args: (80, 50)
        adapter._file_receipt = Mock()
        adapter._wait_file = Mock(return_value='confirmed')
        adapter._file_menu_download = Mock(side_effect=AssertionError('接收后不可右键核验'))
        self.assertEqual(adapter._download_file(None, None, 1), 'confirmed')
        bubble.Click.assert_called_once()
        adapter._file_receipt.assert_called_with(None, 'report.pdf', accept=False)

    def test_file_wait_handles_delayed_receipt_without_opening_menu(self):
        """下载中途出现接收窗口也只操作匹配弹窗，完成标记出现后返回。"""
        adapter = self.adapter(None)
        adapter.cancelled = lambda: False
        adapter._pause = Mock()
        adapter._file_receipt = Mock()
        adapter._fresh_target = Mock(side_effect=[
            Node('文件\nreport.pdf\n未下载'), Node('文件\nreport.pdf\n下载中 50%'),
            Node(children=(Node('已下载', kind='TextControl'),)),
        ])
        adapter._file_menu = Mock(side_effect=AssertionError('下载期间不可打开菜单'))
        self.assertEqual(adapter._wait_file(None, None, 'report.pdf', 5), 'confirmed')
        self.assertEqual(adapter._file_receipt.call_count, 3)
        adapter._file_receipt.assert_called_with(None, 'report.pdf', accept=True)

    def test_missing_marker_disappearance_requires_stability_and_is_not_confirmation(self):
        """标记短暂消失后恢复下载中会重置等待，稳定消失也不声称完成。"""
        adapter = self.adapter(None)
        now = [0.0]
        adapter.cancelled = lambda: False
        adapter._pause = lambda seconds: now.__setitem__(0, now[0] + seconds)
        adapter._file_receipt = Mock()
        empty = Node('文件\nreport.pdf\n2.8M')
        states = [empty, Node('下载中 50%')] + [empty] * 5
        adapter._fresh_target = Mock(side_effect=states)
        with patch('src.modules.window.wechat_chat_adapter.time.monotonic', side_effect=lambda: now[0]):
            self.assertEqual(adapter._wait_file(None, None, 'report.pdf', 5), 'unconfirmed')
        self.assertEqual(adapter._fresh_target.call_count, 7)

    def test_file_wait_stops_on_expiry_timeout_and_cancel(self):
        """无完成证据不得无限等待；取消时不再触碰接收窗口。"""
        for label, cancel, expected in [('已过期', False, 'expired'), ('未下载', False, 'unconfirmed'), ('未下载', True, 'cancelled')]:
            adapter = self.adapter(None)
            now = [0.0]
            adapter.cancelled = lambda: cancel
            adapter._pause = lambda seconds: now.__setitem__(0, now[0] + seconds)
            adapter._file_receipt = Mock()
            adapter._fresh_target = lambda *args: Node(label)
            with patch('src.modules.window.wechat_chat_adapter.time.monotonic', side_effect=lambda: now[0]):
                self.assertEqual(adapter._wait_file(None, None, 'report.pdf', 1), expected)
            if cancel:
                adapter._file_receipt.assert_not_called()

    def test_viewer_limits_clicks_and_always_closes_preview(self):
        """N 次上一张之外保留起始图片，取消和异常均关闭本次预览。"""
        for failure in (False, True):
            adapter = self.adapter(None)
            adapter._child_window = Mock(return_value='preview')
            adapter._buttons = lambda context, names: [] if names == ORIGINAL_NAMES else [SimpleNamespace(IsEnabled=True)]
            adapter._pause = Mock()
            adapter.cancelled = lambda: False
            adapter._click = Mock(side_effect=ChatAutomationError('failed') if failure else None)
            adapter.close_forward = Mock()
            progress = Mock()
            if failure:
                with self.assertRaises(ChatAutomationError):
                    adapter.browse_images('parent', None, 3, 1, progress)
            else:
                self.assertEqual(adapter.browse_images('parent', None, 3, 1, progress), 'previous_limit')
                self.assertEqual(adapter._click.call_count, 3)
                self.assertEqual(adapter._pause.call_count, 4)
                self.assertEqual([c.args[0] for c in progress.call_args_list],
                                 ['preview_opened'] + ['previous_clicked'] * 3)
            adapter.close_forward.assert_called_once_with('preview', 'parent')

    def test_viewer_disabled_previous_stops_without_clicking(self):
        """上一张到边界不可用时不继续空点。"""
        adapter = self.adapter(None)
        adapter._child_window = lambda *args: 'preview'
        adapter._buttons = lambda context, names: [] if names == ORIGINAL_NAMES else [SimpleNamespace(IsEnabled=False)]
        adapter._pause = Mock()
        adapter.cancelled = lambda: False
        adapter._click = Mock()
        adapter.close_forward = Mock()
        self.assertEqual(adapter.browse_images(None, None, 3, 1, Mock()), 'previous_unavailable')
        adapter._click.assert_not_called()

    def test_scroll_waits_through_long_message_and_returns_when_new_row_appears(self):
        """长消息滚动中身份未变化仍继续，出现新行后立即交给服务核验衔接。"""
        listing = Node(kind='ListControl')
        listing.pattern = SimpleNamespace(VerticallyScrollable=True, Scroll=Mock())
        adapter = self.adapter(listing)
        adapter._activate = Mock()
        adapter.cancelled = lambda: False
        adapter._pause = lambda seconds: None
        adapter._wheel = Mock()
        adapter._page_position = Mock(side_effect=[(100,), (80,)])
        adapter._message_layout = Mock(return_value=('layout',))
        first = ChatMessage('one', 'text', None, ((1,), None))
        earlier = ChatMessage('zero', 'text', None, ((2,), None))
        adapter.read_page = Mock(side_effect=[ChatPage((first,)), ChatPage((first,)), ChatPage((earlier, first))])
        adapter.scroll_older(ChatContext(None, 'A'))
        self.assertEqual(adapter._wheel.call_count, 2)
        listing.pattern.Scroll.assert_not_called()

    def test_wheel_rechecks_fresh_layout_after_transient_obstruction(self):
        """临时遮挡时尚未滚动；重新激活并读取新位置后只发送一次滚轮。"""
        first = Node(rect=(0, 0, 400, 500))
        second = Node(rect=(100, 100, 500, 600))
        first.WheelUp, second.WheelUp = Mock(), Mock()
        adapter = self.adapter(None)
        adapter._list = Mock(side_effect=[first, second])
        adapter._activate = Mock()
        adapter._pause = Mock()
        adapter.cancelled = lambda: False
        adapter._check_point = Mock(side_effect=[ChatAutomationError('遮挡'), None])
        context = ChatContext(None, 'A', True)
        adapter._wheel(context, older=True)
        self.assertEqual([c.args[1:] for c in adapter._check_point.call_args_list], [(200, 250), (300, 350)])
        self.assertEqual(adapter._activate.call_count, 2)
        first.WheelUp.assert_not_called()
        second.WheelUp.assert_called_once_with(wheelTimes=1, waitTime=0.1)

    def test_persistent_obstruction_or_cancellation_never_sends_wheel(self):
        """持续遮挡最多核验两次，重试前取消也不能继续滚动。"""
        for cancel in (False, True):
            listing = Node()
            listing.WheelDown = Mock()
            adapter = self.adapter(listing)
            adapter._activate = Mock()
            adapter._pause = Mock()
            adapter.cancelled = Mock(side_effect=[False, cancel])
            adapter._check_point = Mock(side_effect=ChatAutomationError('遮挡'))
            with self.assertRaises(ChatAutomationError):
                adapter._wheel(ChatContext(None, 'A'), older=False)
            self.assertEqual(adapter._check_point.call_count, 1 if cancel else 2)
            listing.WheelDown.assert_not_called()

    def test_wheel_action_error_is_not_retried(self):
        """滚轮已调用后结果不明时不重试，防止重复滚动漏消息。"""
        listing = Node()
        listing.WheelUp = Mock(side_effect=ChatAutomationError('结果不明'))
        adapter = self.adapter(listing)
        adapter._activate = Mock()
        adapter.cancelled = lambda: False
        adapter._check_point = Mock()
        with self.assertRaises(ChatAutomationError):
            adapter._wheel(ChatContext(None, 'A'), older=True)
        listing.WheelUp.assert_called_once()

    def test_obstruction_becomes_task_error_with_foreground_application(self):
        """共享文章模块的遮挡异常转成任务错误，保留计数并显示前台应用。"""
        adapter = self.adapter(None)
        adapter.guard = SimpleNamespace(ensure_target_clickable=Mock(
            side_effect=HomeWindowNotClickableError('文章坐标不在微信主页可点击层级')))
        user32 = SimpleNamespace(GetForegroundWindow=Mock(return_value=2))
        window = SimpleNamespace(handle=2, process_name='viewer.exe', class_name='Viewer')
        with patch('ctypes.windll', SimpleNamespace(user32=user32), create=True), patch(
                'src.modules.window.wechat_chat_adapter.enumerate_desktop_windows', return_value=[window]):
            with self.assertRaisesRegex(ChatAutomationError, 'viewer.exe') as error:
                adapter._check_point(ChatContext(SimpleNamespace(handle=1), 'A'), 10, 20)
        self.assertNotIn('文章坐标', str(error.exception))

    def test_obstruction_reports_blocker_even_when_wechat_is_foreground(self):
        """前台是微信不能解释遮挡，必须另记点击点真正所属的 PDF 窗口。"""
        adapter = self.adapter(None)
        adapter.guard = SimpleNamespace(ensure_target_clickable=Mock(side_effect=
            HomeWindowNotClickableError('坐标被遮挡', owner_handle=3)))
        user32 = SimpleNamespace(GetForegroundWindow=Mock(return_value=1), GetAncestor=Mock(return_value=2))
        windows = [SimpleNamespace(handle=1, process_name='Weixin.exe', class_name='Qt'),
                   SimpleNamespace(handle=2, process_name='msedge.exe', class_name='Chrome', rect=(0, 0, 1000, 1000))]
        with patch('ctypes.windll', SimpleNamespace(user32=user32), create=True), patch(
                'src.modules.window.wechat_chat_adapter.enumerate_desktop_windows', return_value=windows):
            with self.assertRaisesRegex(AttachmentNotClickableError, '遮挡窗口：msedge.exe'):
                adapter._check_point(ChatContext(SimpleNamespace(handle=1), 'A'), 10, 20)

    def test_forward_stationary_scroll_marks_start_but_outer_does_not(self):
        """有限转发的稳定边缘可确认起点，外层懒加载不能据此报告全部完成。"""
        for forwarded in (True, False):
            adapter = self.adapter(Node(kind='ListControl'))
            adapter._activate = Mock()
            adapter.cancelled = lambda: False
            adapter._pause = lambda seconds: None
            adapter._wheel = Mock()
            adapter._page_position = lambda context: (100,)
            adapter._message_layout = Mock(return_value=('layout',))
            adapter.read_page = lambda context: ChatPage((ChatMessage('one', 'text', None, ((1,), None)),))
            context = ChatContext(None, 'A', forwarded)
            if forwarded:
                adapter.scroll_older(context)
                self.assertTrue(context.reached_start)
            else:
                with self.assertRaises(ChatAutomationError):
                    adapter.scroll_older(context)

    def test_scroll_rebuild_restores_whole_page_then_retries_once(self):
        """整页重建先退回核验；原位不符停止，连续重建则安全换新基线。"""
        from dataclasses import replace
        def page(ids):
            """用同名图片模拟不能直接按正文区分的控件。"""
            return ChatPage(tuple(ChatMessage('图片', 'image', None, ((i,), None)) for i in ids))
        for failure in ('', 'layout', 'again'):
            with self.subTest(failure=failure):
                adapter = self.adapter(Node(kind='ListControl'))
                adapter._activate = Mock()
                adapter._pause = Mock()
                adapter._wheel = Mock()
                adapter.cancelled = lambda: False
                adapter._page_position = Mock(return_value=())
                adapter._message_layout = Mock(side_effect=[('original',),
                    ('different' if failure == 'layout' else 'original',), ('original',)])
                snapshots = iter([page((1, 2)), page((9, 11, 12)), page((11, 12)),
                                  page((11, 12)), page((9, 21, 22) if failure == 'again' else (9, 11, 12))])
                def read(context):
                    """与真实读取一样应用已核验的控件别名。"""
                    return ChatPage(tuple(replace(m, control=(context.identities.get(m.control[0], m.control[0]), None))
                                          for m in next(snapshots).messages))
                adapter.read_page = read
                context = ChatContext(None, 'A')
                if failure == 'layout':
                    with self.assertRaises(ChatAutomationError):
                        adapter.scroll_older(context)
                else:
                    self.assertTrue(adapter.scroll_older(context))
                    self.assertEqual(context.identities,
                                     {} if failure == 'again' else {(11,): (1,), (12,): (2,)})
                self.assertEqual([call.kwargs['older'] for call in adapter._wheel.call_args_list],
                                 [True, False] if failure == 'layout' else [True, False, True])

    def test_rebinding_swapped_ids_does_not_merge_messages(self):
        """微信重用并交换控件标识时同时更新映射，不能把两条消息合并。"""
        adapter = self.adapter(None)
        adapter._message_layout = Mock(return_value=('layout',))
        original = ChatPage((ChatMessage('a', 'image', None, ((1,), None)), ChatMessage('b', 'image', None, ((2,), None))))
        restored = ChatPage((ChatMessage('a', 'image', None, ((2,), None)), ChatMessage('b', 'image', None, ((1,), None))))
        context = ChatContext(None, 'A', identities={(1,): (1,), (2,): (2,)})
        adapter._verify_restored_page(context, original, ('layout',), restored)
        self.assertEqual(context.identities, {(1,): (2,), (2,): (1,)})

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

    def test_attachment_point_excludes_sender_at_each_window_scale(self):
        """模拟头像比附件更深色的消息，100/150/200% 下均只能选中正文。"""
        for dpi in (96, 144, 192):
            for kind in ('image', 'forward', 'file'):
                with self.subTest(dpi=dpi, kind=kind):
                    scale = dpi / 96
                    row = (100, 200, 100 + round(740 * scale), 200 + round(200 * scale))
                    bubble = Node(rect=row)
                    listing = Node(kind='ListControl', rect=(row[0], row[1], row[2], row[3] + 100))
                    adapter = self.adapter(listing)
                    adapter._activate = Mock()
                    adapter._fresh_target = lambda *args: bubble
                    adapter.cancelled = lambda: False
                    adapter.guard = SimpleNamespace(ensure_target_clickable=Mock())
                    context = ChatContext(SimpleNamespace(handle=1), 'A')

                    def capture(**crop):
                        """生成局部像素：头像/昵称黑色，附件灰色，其余白色。"""
                        adapter._activate.assert_called()
                        bitmap = MagicMock()
                        bitmap.Width, bitmap.Height = crop['width'], crop['height']
                        bitmap.__enter__.return_value = bitmap

                        def pixel(x, y):
                            """按消息中的逻辑位置返回颜色，以复现固定像素裁剪误点。"""
                            x, y = (crop['x'] + x) / scale, (crop['y'] + y) / scale
                            if (20 <= x <= 56 and 8 <= y <= 44) or (64 <= x <= 160 and 8 <= y <= 26):
                                return 0
                            return 0xBBBBBB if 64 <= x <= 280 and 36 <= y <= 180 else 0xFFFFFF

                        bitmap.GetPixelColor.side_effect = pixel
                        return bitmap

                    listing.ToBitmap = capture
                    user32 = SimpleNamespace(GetDpiForWindow=Mock(return_value=dpi))
                    with patch('ctypes.windll', SimpleNamespace(user32=user32), create=True):
                        x, y = adapter._bubble_point(context, ChatMessage('a', kind, None), bubble)
                    self.assertTrue(64 <= x / scale <= 280)
                    self.assertTrue(36 <= y / scale <= 180)

    def test_unknown_window_scale_stops_before_capture(self):
        """无法读取缩放时禁止退回固定像素并点击。"""
        adapter = self.adapter(None)
        adapter._activate = Mock()
        adapter.cancelled = lambda: False
        context = ChatContext(SimpleNamespace(handle=1), 'A')
        user32 = SimpleNamespace(GetDpiForWindow=Mock(return_value=0))
        with patch('ctypes.windll', SimpleNamespace(user32=user32), create=True):
            with self.assertRaisesRegex(ChatAutomationError, '缩放'):
                adapter._bubble_point(context, ChatMessage('a', 'image', None), None)

    def test_attachment_obstruction_recaptures_moved_bubble_before_single_click(self):
        """PDF 在采样时抢前台，恢复后重读已移动的消息，只发送一次新坐标点击。"""
        for kind in ('image', 'forward'):
            with self.subTest(kind=kind):
                old, fresh = Mock(), Mock()
                adapter = self.adapter(None)
                adapter.cancelled = lambda: False
                adapter._pause = Mock()
                adapter._activate = Mock()
                adapter._fresh_target = Mock(return_value=fresh)
                adapter._locate_bubble_point = Mock(side_effect=[AttachmentNotClickableError('PDF 遮挡'), (120, 60)])
                message = ChatMessage('a', kind, None)
                adapter._open_bubble('context', message, old)
                self.assertEqual(adapter._locate_bubble_point.call_args_list[1].args, ('context', message, fresh))
                old.DoubleClick.assert_not_called()
                fresh.DoubleClick.assert_called_once_with(x=120, y=60, simulateMove=False, waitTime=0.2)

    def test_attachment_recovery_is_bounded_and_does_not_retry_identity_errors(self):
        """持续遮挡最多定位两次；消息身份改变不进入恢复，也不点击。"""
        for error, attempts in ((AttachmentNotClickableError('遮挡'), 2), (ChatAutomationError('消息身份改变'), 1)):
            with self.subTest(error=type(error).__name__):
                bubble = Mock()
                adapter = self.adapter(None)
                adapter.cancelled = lambda: False
                adapter._pause = Mock()
                adapter._activate = Mock()
                adapter._fresh_target = Mock(return_value=bubble)
                adapter._locate_bubble_point = Mock(side_effect=error)
                with self.assertRaises(ChatAutomationError):
                    adapter._open_bubble('context', ChatMessage('a', 'image', None), bubble)
                self.assertEqual(adapter._locate_bubble_point.call_count, attempts)
                bubble.DoubleClick.assert_not_called()

    def test_wechat_tooltip_recovery_moves_cursor_and_retries_three_times(self):
        """微信自身提示框随鼠标移开后可消失，其他遮挡仍只重试一次。"""
        adapter = self.adapter(None)
        adapter.cancelled = lambda: False
        adapter._pause = Mock()
        adapter._activate = Mock()
        adapter._fresh_target = Mock(return_value=Mock())
        adapter._locate_bubble_point = Mock(side_effect=[
            AttachmentNotClickableError('遮挡窗口：Weixin.exe / Qt51514QWindowToolTipSaveBits'),
            (12, 34),
        ])
        context = SimpleNamespace(window=SimpleNamespace(rect=(100, 200, 900, 800)))
        user32 = SimpleNamespace(SetCursorPos=Mock())
        with patch('ctypes.windll', SimpleNamespace(user32=user32), create=True):
            self.assertEqual(adapter._bubble_point(context, ChatMessage('a', 'image', None), Mock()), (12, 34))
        user32.SetCursorPos.assert_called_once_with(500, 220)

    def test_cancel_during_attachment_recovery_sends_no_more_input(self):
        """恢复等待中按停止，不再激活、重新读取或点击。"""
        adapter = self.adapter(None)
        stopped = [False]
        adapter.cancelled = lambda: stopped[0]
        adapter._pause = lambda seconds: stopped.__setitem__(0, True)
        adapter._activate = Mock()
        adapter._fresh_target = Mock()
        adapter._locate_bubble_point = Mock(side_effect=AttachmentNotClickableError('遮挡'))
        with self.assertRaisesRegex(ChatAutomationError, '已取消'):
            adapter._bubble_point('context', ChatMessage('a', 'image', None), Mock())
        adapter._activate.assert_not_called()
        adapter._fresh_target.assert_not_called()

    def test_file_result_restores_chat_after_viewer_open_only_when_needed(self):
        """接收完成或超时后恢复对话；跳过文件和取消时不抢前台。"""
        for status in ('confirmed', 'unconfirmed', 'skipped_file', 'available', 'cancelled'):
            with self.subTest(status=status):
                adapter = self.adapter(None)
                adapter.cancelled = lambda: status == 'cancelled'
                adapter._download_file = Mock(return_value=status)
                adapter._nodes = Mock()
                adapter._activate = Mock()
                adapter._fresh_target = Mock()
                adapter._restore_attachment_position = Mock()
                message = ChatMessage('a', 'file', None)
                self.assertEqual(adapter.download('context', message, 1), status)
                if status in ('confirmed', 'unconfirmed'):
                    adapter._nodes.assert_called_once_with('context')
                    adapter._activate.assert_called_once_with('context')
                    adapter._fresh_target.assert_called_once_with('context', message)
                else:
                    adapter._activate.assert_not_called()
                adapter._restore_attachment_position.assert_called_once_with('context')

    def test_clipped_attachment_reveals_body_in_correct_direction(self):
        """顶部仅露底边、底部仅露昵称时先滚动正文，覆盖三种附件与三档缩放。"""
        for dpi in (96, 144, 192):
            for older in (True, False):
                for kind in ('image', 'file', 'forward'):
                    with self.subTest(dpi=dpi, older=older, kind=kind):
                        scale = dpi / 96
                        initial_top = -182 if older else 490
                        bubble = Node(rect=(0, round(initial_top * scale), round(600 * scale), round((initial_top + 200) * scale)))
                        listing = Node(kind='ListControl', rect=(0, 0, round(600 * scale), round(500 * scale)))
                        adapter = self.adapter(listing)
                        adapter._activate = Mock()
                        adapter._pause = Mock()
                        adapter.cancelled = lambda: False
                        adapter._fresh_target = lambda *args: bubble
                        adapter._check_point = Mock()
                        message = ChatMessage('a', kind, None, ((1,), bubble))
                        original = ChatPage((message,))
                        adapter.read_page = Mock(return_value=original)
                        adapter._message_layout = Mock(return_value=('original',))
                        context = ChatContext(SimpleNamespace(handle=1), 'A')

                        def wheel(*args, **kwargs):
                            """一次小滚动露出正文；返回时模拟原始位置。"""
                            offset = round((120 if kwargs['older'] else -120) * scale)
                            l, t, r, b = bubble.BoundingRectangle
                            bubble.BoundingRectangle = (l, t + offset, r, b + offset)

                        adapter._wheel = Mock(side_effect=wheel)

                        def capture(**crop):
                            """只在气泡正文提供色差，右侧留空作为背景。"""
                            bitmap = MagicMock()
                            bitmap.Width, bitmap.Height = crop['width'], crop['height']
                            bitmap.__enter__.return_value = bitmap
                            bitmap.GetPixelColor.side_effect = lambda x, y: 0xBBBBBB if x < bitmap.Width // 2 else 0xFFFFFF
                            return bitmap

                        listing.ToBitmap = capture
                        user32 = SimpleNamespace(GetDpiForWindow=Mock(return_value=dpi))
                        with patch('ctypes.windll', SimpleNamespace(user32=user32), create=True):
                            x, y = adapter._bubble_point(context, message, bubble)
                        self.assertGreaterEqual(x, round(64 * scale))
                        self.assertGreaterEqual(y, round(36 * scale))
                        adapter._wheel.assert_called_once_with(context, older=older)
                        if older:
                            self.assertIsNone(context.attachment_position)
                        else:
                            self.assertEqual(context.attachment_position, (original, ('original',), 1))
                            adapter._restore_attachment_position(context)
                            self.assertIsNone(context.attachment_position)
                            self.assertEqual(bubble.BoundingRectangle[1], round(initial_top * scale))

    def test_attachment_restore_rebinds_only_matching_page_and_respects_cancel(self):
        """恢复页几何不同即停止；整页相同可绑定新控件，取消时不滚动。"""
        original = ChatPage((ChatMessage('a', 'image', None, ((1,), None)),))
        restored = ChatPage((ChatMessage('a', 'image', None, ((2,), None)),))
        for changed, cancelled in ((False, False), (True, False), (False, True)):
            with self.subTest(changed=changed, cancelled=cancelled):
                adapter = self.adapter(None)
                adapter.cancelled = lambda: cancelled
                adapter._wheel = Mock()
                adapter._pause = Mock()
                adapter.read_page = Mock(return_value=restored)
                adapter._message_layout = Mock(return_value=('changed' if changed else 'original',))
                context = ChatContext(None, 'A', attachment_position=(original, ('original',), 1))
                if changed:
                    with self.assertRaisesRegex(ChatAutomationError, '恢复原消息位置'):
                        adapter._restore_attachment_position(context)
                    self.assertFalse(context.identities)
                else:
                    adapter._restore_attachment_position(context)
                    if cancelled:
                        adapter._wheel.assert_not_called()
                    else:
                        self.assertEqual(context.identities[(2,)], (1,))
                        self.assertIsNone(context.attachment_position)

    def test_clipped_attachment_stops_when_scroll_does_not_move(self):
        """微信没有移动时不盲点昵称，也不无限滚动。"""
        bubble = Node(rect=(0, 490, 600, 690))
        adapter = self.adapter(Node(rect=(0, 0, 600, 500)))
        adapter._activate = Mock()
        adapter._pause = Mock()
        adapter.cancelled = lambda: False
        adapter._fresh_target = lambda *args: bubble
        adapter._wheel = Mock()
        adapter.read_page = Mock(return_value=ChatPage(()))
        adapter._message_layout = Mock(return_value=())
        adapter._check_point = Mock()
        context = ChatContext(SimpleNamespace(handle=1), 'A')
        user32 = SimpleNamespace(GetDpiForWindow=Mock(return_value=96))
        with patch('ctypes.windll', SimpleNamespace(user32=user32), create=True):
            with self.assertRaisesRegex(ChatAutomationError, '自动滚动后'):
                adapter._bubble_point(context, ChatMessage('a', 'image', None), bubble)
        adapter._wheel.assert_called_once()
        adapter._check_point.assert_not_called()

    def test_file_result_restores_temporary_scroll(self):
        """文件成功或失败退出都恢复临时滚动，与图片/转发关闭行为一致。"""
        for failure in (False, True):
            adapter = self.adapter(None)
            adapter.cancelled = lambda: False
            adapter._nodes = Mock()
            adapter._activate = Mock()
            adapter._fresh_target = Mock()
            adapter._restore_attachment_position = Mock()
            adapter._download_file = Mock(return_value='unconfirmed', side_effect=ChatAutomationError('失败') if failure else None)
            if failure:
                with self.assertRaises(ChatAutomationError):
                    adapter.download('parent', ChatMessage('a', 'file', None), 1)
            else:
                self.assertEqual(adapter.download('parent', ChatMessage('a', 'file', None), 1), 'unconfirmed')
            adapter._restore_attachment_position.assert_called_once_with('parent')

    def test_child_close_restores_parent_scan_position(self):
        """本次图片/转发窗口确认关闭后才恢复父窗口，防止滚轮落在子窗口。"""
        adapter = self.adapter(None)
        pattern = SimpleNamespace(Close=Mock())
        window = SimpleNamespace(handle=2, control=SimpleNamespace(GetWindowPattern=lambda: pattern))
        adapter._window = Mock(return_value=window)
        adapter._nodes = Mock()
        adapter._restore_attachment_position = Mock(side_effect=lambda parent: pattern.Close.assert_called_once())
        with patch('src.modules.window.wechat_chat_adapter.enumerate_desktop_windows', return_value=[]):
            adapter.close_forward(ChatContext(window, 'A', True), 'parent')
        adapter._restore_attachment_position.assert_called_once_with('parent')

    def test_child_window_recognizes_reuse_without_accepting_unrelated_windows(self):
        """复现同句柄转发被误拒；类型、进程、标题、前台和祖先限制仍有效。"""
        for case in ('new', 'hidden', 'reused', 'wrong_title', 'wrong_pid', 'wrong_type',
                     'background', 'ancestor', 'old_preview', 'not_activated', 'cancelled'):
            with self.subTest(case=case):
                expected = PREVIEW_CLASS if case == 'old_preview' else FORWARD_CLASS
                control = Node(cls='other' if case == 'wrong_type' else expected)
                control.ProcessId = 99 if case == 'wrong_pid' else 10
                window = SimpleNamespace(handle=2, title='其他聊天记录' if case == 'wrong_title' else 'Leada的聊天记录',
                                         visible=True, is_minimized=False, process_name='Weixin.exe', control=control)
                before = [] if case == 'new' else [SimpleNamespace(handle=2, visible=case != 'hidden')]
                adapter = self.adapter(None)
                adapter._fresh_target = Mock(return_value=Node('聊天记录Leada的聊天记录Leada: [图片]'))
                adapter._activate = Mock()
                adapter._open_bubble = Mock()
                adapter._pause = Mock()
                adapter.cancelled = lambda: case == 'cancelled'
                context = ChatContext(SimpleNamespace(handle=1, control=SimpleNamespace(ProcessId=10)), 'A',
                                      ancestors=(2,) if case == 'ancestor' else ())
                foreground = Mock(side_effect=[3 if case == 'not_activated' else 1, 3 if case == 'background' else 2])
                with patch('ctypes.windll', SimpleNamespace(user32=SimpleNamespace(GetForegroundWindow=foreground)), create=True), patch(
                        'src.modules.window.wechat_chat_adapter.enumerate_desktop_windows', side_effect=[before, [window]]), patch(
                        'src.modules.window.wechat_chat_adapter.attach_uia_control', side_effect=lambda w: w), patch(
                        'src.modules.window.wechat_chat_adapter.time.monotonic', side_effect=[0, 0, 6]):
                    if case in {'new', 'hidden', 'reused'}:
                        child = adapter._child_window(context, ChatMessage('forward', 'forward', None), expected)
                        self.assertEqual(child.window.handle, 2)
                        self.assertEqual(child.owned, case != 'reused')
                        self.assertEqual(child.ancestors, (1,))
                    else:
                        with self.assertRaises(ChatAutomationError):
                            adapter._child_window(context, ChatMessage('forward', 'forward', None), expected)
                self.assertEqual(adapter._open_bubble.call_count, 0 if case in {'not_activated', 'cancelled'} else 1)

    def test_borrowed_forward_returns_to_parent_without_closing_user_window(self):
        """复用的用户窗口不关闭，仍恢复父层焦点与扫描位置。"""
        adapter = self.adapter(None)
        adapter._window = Mock(side_effect=AssertionError('不应关闭用户窗口'))
        adapter._nodes = Mock()
        adapter._activate = Mock()
        adapter._restore_attachment_position = Mock()
        adapter.close_forward(ChatContext(None, 'A', True, owned=False), 'parent')
        adapter._nodes.assert_called_once_with('parent')
        adapter._activate.assert_called_once_with('parent')
        adapter._restore_attachment_position.assert_called_once_with('parent')

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
        master = Node(cls='mmui::ChatMasterView')
        search_field = Node(cls='mmui::XSearchField', parent=master)
        search = Node('搜索', kind='EditControl', cls='mmui::XValidatorTextEdit', parent=search_field)
        search.SetFocus = Mock()
        value = SimpleNamespace(SetValue=Mock())
        search.GetValuePattern = lambda: value
        result = Node('180k拼团')
        results = Node(kind='ListControl', children=(Node('搜索网络结果'), Node('180k拼团'),
                       Node('群聊'), result, Node('聊天记录'), Node('180k拼团')))
        results.AutomationId = 'search_list'
        root = Node(kind='WindowControl', children=(old, search))
        adapter = self.adapter(None)
        adapter.main = SimpleNamespace(control=root)
        adapter.diagnose = lambda: {'ready': True}
        adapter._nodes = lambda context: [old, search, results]
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
        adapter._click.assert_called_once_with(context, result, keep_parent_active=True)
        for recovered in (True, False):
            with self.subTest(focus_recovered=recovered):
                adapter._click.reset_mock()
                value.SetValue.reset_mock()
                adapter._focus_within = Mock(side_effect=[False, recovered])
                adapter._window = Mock(return_value=SimpleNamespace(control=Node(children=(target,))))
                if recovered:
                    adapter.open_chat('180k拼团')
                    value.SetValue.assert_called_once_with('180k拼团')
                    self.assertEqual(adapter._click.call_count, 2)
                else:
                    with self.assertRaisesRegex(ChatAutomationError, '焦点'):
                        adapter.open_chat('180k拼团')
                    value.SetValue.assert_not_called()
                    self.assertEqual(adapter._click.call_count, 1)
                self.assertIs(adapter._click.call_args_list[0].args[1], search)

    def test_search_popup_keeps_parent_active_but_checks_popup_owner(self):
        """搜索弹层不抢焦点；点击仍核验弹层所属进程和坐标。"""
        adapter = self.adapter(None)
        parent = ChatContext(SimpleNamespace(handle=10, control=SimpleNamespace(ProcessId=1)), '群')
        popup = SimpleNamespace(handle=20, control=SimpleNamespace(ProcessId=1))
        control = Node()
        control.GetTopLevelControl = lambda: SimpleNamespace(NativeWindowHandle=20)
        control.Click = Mock()
        adapter.cancelled = lambda: False
        adapter._nodes = Mock()
        adapter._window = lambda window: window
        adapter._activate = Mock()
        adapter._check_point = Mock()
        user32 = SimpleNamespace(GetWindow=Mock(return_value=10))
        with patch('ctypes.windll', SimpleNamespace(user32=user32), create=True), patch(
                'src.modules.window.wechat_chat_adapter.enumerate_desktop_windows', return_value=[popup]):
            adapter._click(parent, control, keep_parent_active=True)
        adapter._activate.assert_called_once_with(parent)
        self.assertEqual(adapter._check_point.call_args.args[0].window.handle, 20)
        control.Click.assert_called_once_with(simulateMove=False)
        control.Click.reset_mock()
        adapter._activate.reset_mock()
        user32.GetWindow.return_value = 0
        with patch('ctypes.windll', SimpleNamespace(user32=user32), create=True):
            with self.assertRaisesRegex(ChatAutomationError, '不属于'):
                adapter._click(parent, control, keep_parent_active=True)
        adapter._activate.assert_not_called()
        control.Click.assert_not_called()

    def test_local_search_categories(self):
        """排除网络和记录，保留本地重名歧义及公众号入口，不猜无分类结果。"""
        target = Node('目标')
        self.assertEqual(local_chat_matches([target], '目标'), [])
        self.assertEqual(local_chat_matches([Node('搜索网络结果'), target], '目标'), [])
        self.assertEqual(local_chat_matches([Node('群聊'), target, Node('聊天记录'), Node('目标')], '目标'), [target])
        self.assertEqual(len(local_chat_matches([Node('群聊'), target, Node('目标')], '目标')), 2)
        self.assertEqual(local_chat_matches([Node('公众号'), target], '目标'), [target])
        self.assertEqual(local_chat_matches([Node('群聊'), Node('目标其他')], '目标'), [])

    def test_local_search_prefers_group_over_most_used_duplicate(self):
        """同一会话出现在快捷分类和正式分类时，只返回正式分类。"""
        recent = Node('目标')
        group = Node('目标')
        rows = [Node('最常使用'), recent, Node('群聊'), group]
        self.assertEqual(local_chat_matches(rows, '目标'), [group])

    def test_chat_list_search_excludes_group_member_sidebar(self):
        """群成员侧栏打开时，不把其搜索框当成会话搜索框。"""
        master = Node(cls='mmui::ChatMasterView')
        master_field = Node(cls='mmui::XSearchField', parent=master)
        chat_search = Node('搜索', kind='EditControl', cls='mmui::XValidatorTextEdit', parent=master_field)
        member_panel = Node(cls='mmui::ChatRoomMemberInfoView')
        member_field = Node(cls='mmui::XSearchField', parent=member_panel)
        member_search = Node('搜索', kind='EditControl', cls='mmui::XValidatorTextEdit', parent=member_field)

        self.assertTrue(is_chat_list_search(chat_search))
        self.assertFalse(is_chat_list_search(member_search))


if __name__ == '__main__':
    unittest.main()
