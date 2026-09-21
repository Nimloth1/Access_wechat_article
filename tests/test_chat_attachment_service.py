# encoding: UTF-8
"""附件任务的边界与恢复测试，模拟多屏真实消息序列。"""
from datetime import date
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from chat_attachments import options_from_dict, parse_args
from src.services.chat_attachment_service import (
    AttachmentOptions, ChatAttachmentService, ChatAutomationError, ChatMessage, ChatPage, older_prefix,
)
from src.services.chat_attachment_progress import ChatAttachmentProgress
from src.modules.window.wechat_chat_adapter import attachment_kind, message_day


TODAY = date(2026, 9, 6)


def msg(key, kind='image', day=TODAY):
    """创建有顺序标识的测试消息。"""
    return ChatMessage(key, kind, day)


class FakeAdapter:
    """模拟聊天列表、转发子窗口、下载回执与停止信号。"""

    def __init__(self, pages, forwards=None):
        """为各对话设置从新到旧的页面序列。"""
        self.pages = pages
        self.forwards = forwards or {}
        self.positions = {}
        self.opened = []
        self.downloaded = []
        self.closed = []
        self.stop = False
        self.status = 'unconfirmed'
        self.cancel_after = 0

    def open_chat(self, name):
        """记录对话切换。"""
        name = name or '当前'
        self.opened.append(name)
        self.positions[name] = 0
        return name

    def chat_name(self, context):
        """返回模拟对话名。"""
        return context

    def read_page(self, context):
        """读取当前页面。"""
        return self.pages[context][self.positions[context]]

    def scroll_older(self, context):
        """向前翻一页。"""
        self.positions[context] += 1

    def download(self, context, message, timeout):
        """模拟附件操作和取消。"""
        self.downloaded.append((context, message.signature))
        if self.cancel_after and len(self.downloaded) == self.cancel_after:
            self.stop = True
        return self.status

    def open_forward(self, context, message):
        """进入预设的转发窗口。"""
        child = self.forwards[message.signature]
        self.positions[child] = 0
        return child

    def close_forward(self, child, parent):
        """记录层级清理顺序。"""
        self.closed.append((child, parent))

    def cancelled(self):
        """返回模拟停止状态。"""
        return self.stop

    def resolve_day(self, context, message):
        """默认无法补全日期，具体测试可替换解析结果。"""
        return None

    def prepare_dates(self, context):
        """模拟适配器不需要操作日期标签。"""
        pass


class ChatAttachmentServiceTests(unittest.TestCase):
    def run_task(self, adapter, **kwargs):
        """运行任务并同时返回事件与统计。"""
        events = []
        result = ChatAttachmentService(adapter, events.append).run(AttachmentOptions(**kwargs))
        return result, events

    def test_overlapping_pages_do_not_duplicate_downloads(self):
        """相邻页的重叠消息只处理一次。"""
        adapter = FakeAdapter({'当前': [ChatPage(tuple(map(msg, 'cde'))), ChatPage(tuple(map(msg, 'abc')), True)]})
        result, _ = self.run_task(adapter)
        self.assertEqual([key for _, key in adapter.downloaded], list('edcba'))
        self.assertEqual(result['messages'], 5)

    def test_same_content_on_one_page_counts_as_two_messages(self):
        """同一页两个相同气泡不能简单按内容去重。"""
        adapter = FakeAdapter({'当前': [ChatPage((msg('same'), msg('same')), True)]})
        result, _ = self.run_task(adapter)
        self.assertEqual(result['messages'], 2)
        self.assertEqual(len(adapter.downloaded), 2)

    def test_ambiguous_overlap_stops(self):
        """重复锚点不能用最长匹配掩盖歧义。"""
        with self.assertRaises(ChatAutomationError):
            older_prefix(ChatPage((msg('a'), msg('a'), msg('b'))), ChatPage((msg('x'), msg('a'), msg('a'))))

    def test_missing_overlap_stops(self):
        """大幅跳页没有可靠锚点时停止。"""
        with self.assertRaises(ChatAutomationError):
            older_prefix(ChatPage((msg('b'),)), ChatPage((msg('a'),)))

    def test_overlap_may_be_inside_expanded_viewport(self):
        """视口扩展后重叠段不必在新页末尾；只处理旧页最早锚点之前的消息。"""
        self.assertEqual(older_prefix(ChatPage(tuple(map(msg, 'bc'))), ChatPage(tuple(map(msg, 'abcd')))), 1)
        self.assertEqual(older_prefix(ChatPage(tuple(map(msg, 'bc'))), ChatPage(tuple(map(msg, 'bcd')))), 0)

    def test_unique_signature_overlap_recovers_runtime_id_reuse(self):
        """实例 ID 被复用时，只接受至少两种签名构成的唯一完整重叠。"""
        def item(signature, identity):
            return ChatMessage(signature, 'image', None, ((identity,), None))
        previous = ChatPage(tuple(item(signature, identity) for signature, identity in
                                  zip('ABCDXX', range(1, 7))))
        current = ChatPage(tuple(item(signature, identity) for signature, identity in
                                 zip('XABCDX', (5, 1, 2, 3, 4, 9))))
        self.assertEqual(older_prefix(previous, current), 1)

    def test_expanded_page_never_reprocesses_newer_messages(self):
        """布局变化露出较新消息不扩大起始边界，连续翻页也不重复计数。"""
        adapter = FakeAdapter({'当前': [ChatPage(tuple(map(msg, 'de'))),
            ChatPage(tuple(map(msg, 'cdef'))), ChatPage(tuple(map(msg, 'abcd')), True)]})
        result, _ = self.run_task(adapter)
        self.assertEqual([key for _, key in adapter.downloaded], list('edcba'))
        self.assertEqual(result['messages'], 5)
        self.assertNotIn('errors', result)

    def test_overlap_rejects_deleted_or_reordered_middle(self):
        """找到单个相同锚点仍不够，共同范围中间缺失或重排必须拒绝。"""
        for content in ('xac', 'xacb', 'xbc'):
            with self.subTest(content=content), self.assertRaises(ChatAutomationError):
                older_prefix(ChatPage(tuple(map(msg, 'abc'))), ChatPage(tuple(map(msg, content))))

    def test_long_scan_with_repeated_images_and_varying_viewport(self):
        """连续两百条同名图片，页面宽松/收缩交替时仍按实例各处理一次。"""
        messages = tuple(ChatMessage('图片', 'image', TODAY, ((index,), None)) for index in range(200))
        pages = [ChatPage(messages[195:])]
        for start in range(194, -1, -1):
            pages.append(ChatPage(messages[start:min(200, start + (3, 8, 5)[start % 3])], start == 0))
        adapter = FakeAdapter({'当前': pages})
        ids = []
        adapter.download = lambda context, message, timeout: ids.append(message.control[0][0]) or 'unconfirmed'
        result, _ = self.run_task(adapter, max_messages=200)
        self.assertEqual(ids, list(reversed(range(200))))
        self.assertEqual(result['messages'], 200)
        self.assertNotIn('errors', result)

    def test_runtime_ids_distinguish_identical_messages_across_pages(self):
        """重复内容可通过稳定实例标识衔接，但替换后的实例不能沿用。"""
        first = ChatMessage('same', 'image', TODAY, ((1,), None))
        second = ChatMessage('same', 'image', TODAY, ((2,), None))
        earlier = ChatMessage('same', 'image', TODAY, ((0,), None))
        self.assertEqual(older_prefix(ChatPage((first, second)), ChatPage((earlier, first))), 1)
        with self.assertRaises(ChatAutomationError):
            older_prefix(ChatPage((first, second)), ChatPage((earlier,)))

    def test_unchanged_page_is_not_reported_as_history_end(self):
        """滚动失败不等同于到达历史起点。"""
        page = ChatPage((msg('a'),))
        adapter = FakeAdapter({'当前': [page, page]})
        result, events = self.run_task(adapter)
        self.assertEqual(result['errors'], 1)
        error = next(e for e in events if e['event'] == 'error')
        self.assertIn('ChatAutomationError', error['traceback'])
        self.assertIn('older_prefix', error['traceback'])

    def test_verified_forward_start_may_return_unchanged_page(self):
        """适配器确认有限转发起点后，相同页面应结束而非重复处理。"""
        adapter = FakeAdapter({'当前': [ChatPage((msg('f', 'forward'),), True)],
            's': [ChatPage((msg('1'),)), ChatPage((msg('1'),), True)]}, {'f': 's'})
        result, _ = self.run_task(adapter)
        self.assertEqual(result['forward_messages'], 1)
        self.assertNotIn('errors', result)

    def test_download_revealing_older_rows_does_not_scroll_past_them(self):
        """处理露出边缘的附件导致视口移动后，先处理新行再继续翻页。"""
        adapter = FakeAdapter({'当前': [ChatPage(tuple(map(msg, 'cd'))),
            ChatPage(tuple(map(msg, 'abc')), True)]})
        download = adapter.download
        def move_when_oldest_is_processed(context, message, timeout):
            """模拟定位气泡时向上滚动。"""
            status = download(context, message, timeout)
            if message.signature == 'c':
                adapter.positions[context] = 1
            return status
        adapter.download = move_when_oldest_is_processed
        result, _ = self.run_task(adapter)
        self.assertEqual([key for _, key in adapter.downloaded], list('dcba'))
        self.assertEqual(result['messages'], 4)
        self.assertNotIn('errors', result)

    def test_limit_counts_text_and_does_not_scroll_after_limit(self):
        """条数指所有外层消息，不是附件数或点击数。"""
        adapter = FakeAdapter({'当前': [ChatPage((msg('old'), msg('image'), msg('text', 'text')))]})
        result, _ = self.run_task(adapter, max_messages=2)
        self.assertEqual(result['messages'], 2)
        self.assertEqual(adapter.downloaded, [('当前', 'image')])
        self.assertEqual(adapter.positions['当前'], 0)

    def test_limit_exactly_equals_page_size(self):
        """处理完一整页正好达到上限时不能继续翻页。"""
        adapter = FakeAdapter({'当前': [ChatPage((msg('a'), msg('b')))]})
        self.run_task(adapter, max_messages=2)
        self.assertEqual(adapter.positions['当前'], 0)

    def test_date_boundary_inclusive_and_unknown_dates_skipped(self):
        """包含截止当天，无法归属日期的消息不猜测。"""
        adapter = FakeAdapter({'当前': [ChatPage((msg('old', day=date(2026, 8, 31)),
            msg('boundary', day=date(2026, 9, 1)), msg('unknown', day=None), msg('new')))]})
        result, events = self.run_task(adapter, since=date(2026, 9, 1))
        self.assertEqual(adapter.downloaded, [('当前', 'new'), ('当前', 'boundary')])
        self.assertEqual(result['unknown_date'], 1)
        self.assertTrue(any(e.get('reason') == 'date_boundary' for e in events))

    def test_expanded_date_allows_forward_and_respects_boundary(self):
        """未知日期补全后，范围内进入转发，范围外直接停止。"""
        for resolved in (TODAY, date(2020, 1, 1)):
            adapter = FakeAdapter({'当前': [ChatPage((msg('f', 'forward', None),), True)],
                                   '子': [ChatPage((msg('file', 'file'),), True)]}, {'f': '子'})
            adapter.resolve_day = Mock(return_value=resolved)
            result, _ = self.run_task(adapter, since=TODAY)
            self.assertEqual(len(adapter.downloaded), int(resolved == TODAY))
            self.assertNotIn('unknown_date', result)

    def test_viewer_route_opens_once_and_ignores_file_and_forward(self):
        """连续模式找到图片后只运行一次预览任务，不进入转发或打开文件。"""
        adapter = FakeAdapter({'当前': [ChatPage((msg('image'), msg('f', 'forward'), msg('file', 'file')))]})
        adapter.browse_images = Mock(return_value='previous_limit')
        result, _ = self.run_task(adapter, image_route='viewer', previous_clicks=3, image_interval=1.5)
        self.assertEqual(result['messages'], 3)
        self.assertEqual(adapter.downloaded, [])
        self.assertEqual(adapter.closed, [])
        self.assertEqual(adapter.browse_images.call_count, 1)
        self.assertEqual(adapter.browse_images.call_args.args[2:4], (3, 1.5))

    def test_viewer_dry_run_never_opens_preview(self):
        """连续图片路线的试扫描只找起始候选。"""
        adapter = FakeAdapter({'当前': [ChatPage((msg('image'),))]})
        adapter.browse_images = Mock()
        result, _ = self.run_task(adapter, image_route='viewer', dry_run=True)
        adapter.browse_images.assert_not_called()
        self.assertEqual(result['candidate'], 1)

    def test_forward_uses_outer_date_and_separate_budget(self):
        """范围内转发卡片可包含较早的内部消息，内部不消耗外层条数。"""
        adapter = FakeAdapter({'当前': [ChatPage((msg('forward', 'forward'),), True)],
            '子': [ChatPage((msg('old', day=date(2020, 1, 1)), msg('file', 'file')), True)]}, {'forward': '子'})
        result, _ = self.run_task(adapter, since=TODAY, max_messages=1)
        self.assertEqual(result['messages'], 1)
        self.assertEqual(result['forward_messages'], 2)
        self.assertEqual(len(adapter.downloaded), 2)
        self.assertEqual(adapter.closed, [('子', '当前')])

    def test_shared_forward_budget_caps_multiple_cards(self):
        """内部消息上限为每个对话共享，而不是每张卡片重新计算。"""
        adapter = FakeAdapter({'当前': [ChatPage((msg('f1', 'forward'), msg('f2', 'forward')), True)],
            's1': [ChatPage((msg('1'),), True)], 's2': [ChatPage((msg('2'),), True)]}, {'f1': 's1', 'f2': 's2'})
        result, _ = self.run_task(adapter, max_forward_messages=1)
        self.assertEqual(result['forward_messages'], 1)
        self.assertEqual(adapter.downloaded, [('s2', '2')])

    def test_nested_depth_limit(self):
        """层数上限不会意外允许多进入一层。"""
        adapter = FakeAdapter({'当前': [ChatPage((msg('f1', 'forward'),), True)],
            's1': [ChatPage((msg('f2', 'forward'),), True)]}, {'f1': 's1'})
        result, _ = self.run_task(adapter, max_depth=1)
        self.assertEqual(result['depth_limited'], 1)
        self.assertEqual(adapter.closed, [('s1', '当前')])

    def test_child_failure_still_closes_child_and_stops_other_chats(self):
        """子窗口异常时必须返回父窗口，再停止任务。"""
        adapter = FakeAdapter({'A': [ChatPage((msg('f', 'forward'),), True)],
            's': [ChatPage((msg('1'),)), ChatPage((msg('2'),))]}, {'f': 's'})
        result, _ = self.run_task(adapter, chats=('A', 'B'))
        self.assertEqual(adapter.closed, [('s', 'A')])
        self.assertEqual(adapter.opened, ['A'])
        self.assertEqual(result['errors'], 1)

    def test_cancel_closes_child_and_does_not_switch_chat(self):
        """下载后收到停止信号，不再处理另一张附件或切换对话。"""
        adapter = FakeAdapter({'A': [ChatPage((msg('f', 'forward'),), True)],
            's': [ChatPage((msg('1'), msg('2')), True)]}, {'f': 's'})
        adapter.cancel_after = 1
        result, _ = self.run_task(adapter, chats=('A', 'B'))
        self.assertTrue(result['cancelled'])
        self.assertEqual(len(adapter.downloaded), 1)
        self.assertEqual(adapter.closed, [('s', 'A')])
        self.assertEqual(adapter.opened, ['A'])

    def test_unconfirmed_is_never_counted_as_confirmed(self):
        """点击或打开成功并不是下载成功。"""
        adapter = FakeAdapter({'当前': [ChatPage((msg('1'),), True)]})
        result, _ = self.run_task(adapter)
        self.assertEqual(result['unconfirmed'], 1)
        self.assertNotIn('confirmed', result)

    def test_dry_run_never_opens_forward_or_downloads(self):
        """仅扫描模式只列出外层候选。"""
        adapter = FakeAdapter({'当前': [ChatPage((msg('f', 'forward'), msg('i')), True)]})
        result, events = self.run_task(adapter, dry_run=True)
        self.assertEqual(adapter.downloaded, [])
        self.assertEqual(result['candidate'], 1)
        self.assertTrue(any(e.get('kind') == 'forward' for e in events))

    def test_conversation_limit_is_per_chat(self):
        """指定多个对话时各自应用消息条数上限。"""
        adapter = FakeAdapter({name: [ChatPage((msg('1'), msg('2')))] for name in ('A', 'B')})
        result, _ = self.run_task(adapter, chats=('A', 'B'), max_messages=1)
        self.assertEqual(result['messages'], 2)
        self.assertEqual(adapter.opened, ['A', 'B'])

    def test_same_day_is_never_used_as_a_checkpoint_boundary(self):
        """同一天可以分多批来消息，不能因旧批次完成就跳过当天较早或较晚消息。"""
        with tempfile.TemporaryDirectory() as directory:
            progress = ChatAttachmentProgress(Path(directory) / 'progress.json')
            adapter = FakeAdapter({'群': [ChatPage((msg('batch-1', day=date(2026, 9, 18)),
                                                    msg('batch-2', day=date(2026, 9, 18))), True)]})
            adapter.status = 'available'
            ChatAttachmentService(adapter, lambda _event: None, progress).run(
                AttachmentOptions(chats=('群',), since=date(2026, 9, 18)))
            self.assertEqual(adapter.downloaded, [('群', 'batch-2'), ('群', 'batch-1')])

    def test_resume_opened_does_not_skip_unverified_messages(self):
        """兼容旧参数时也不能把仅打开过的整天当成落盘证明。"""
        with tempfile.TemporaryDirectory() as directory:
            progress = ChatAttachmentProgress(Path(directory) / 'progress.json')
            retry = FakeAdapter({'群': [ChatPage((msg('image', day=date(2026, 9, 18)),
                                                  msg('new', day=date(2026, 9, 19))), True)]})
            ChatAttachmentService(retry, lambda _event: None, progress).run(
                AttachmentOptions(chats=('群',), since=date(2026, 9, 18), resume_opened=True))
            self.assertEqual(retry.downloaded, [('群', 'new'), ('群', 'image')])


class ConfigurationAndRecognitionTests(unittest.TestCase):
    def test_config_and_dry_run_flag_cannot_silently_override_each_other(self):
        """显式只扫描不能被配置文件悄悄忽略。"""
        with patch('sys.stderr'), self.assertRaises(SystemExit) as raised:
            parse_args(['--config', 'config.json', '--dry-run'])
        self.assertEqual(raised.exception.code, 2)

    def test_rejects_unbounded_scan_without_date(self):
        """没有日期或数量边界不能直接启动。"""
        with self.assertRaises(ValueError):
            AttachmentOptions(max_messages=0)
        self.assertEqual(AttachmentOptions(max_messages=0, since=TODAY).max_messages, 0)

    def test_config_rejects_mistyped_values(self):
        """配置拼错字段或类型时显示错误而不静默降级。"""
        for values in ({'images': 'false'}, {'max_messages': True}, {'chats': '群A'},
                       {'unknown': 1}, {'chats': ['A', 'A']}, {'download_timeout': '30'}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                options_from_dict(values)

    def test_date_parser_never_assumes_time_only_means_today(self):
        """过往页面单独出现时刻时不可补成今天。"""
        self.assertIsNone(message_day('12:30', TODAY))
        self.assertEqual(message_day('昨天 12:30', TODAY), date(2026, 9, 5))
        self.assertEqual(message_day('2025年1月2日 12:30', TODAY), date(2025, 1, 2))
        self.assertEqual(message_day('星期四 12:30', TODAY), date(2026, 9, 3))
        self.assertEqual(message_day('12月31日 12:30', date(2026, 1, 2)), date(2025, 12, 31))
        self.assertIsNone(message_day('2026-02-30', TODAY))
        self.assertIsNone(message_day('会议时间2020-01-01', TODAY))
        self.assertEqual(message_day('9月8日 星期二 12:09', date(2026, 9, 8)), date(2026, 9, 8))
        self.assertEqual(message_day('2025年9月8日 星期一 12:09', TODAY), date(2025, 9, 8))

    def test_viewer_config_rejects_date_and_invalid_limits(self):
        """连续图片不能悄悄忽略日期，也不接受错误的次数或等待时间。"""
        for values in ({'image_route': 'viewer', 'since': '2026-09-01'},
                       {'image_route': 'viewer', 'images': False}, {'previous_clicks': -1},
                       {'previous_clicks': True}, {'image_interval': float('nan')},
                       {'image_interval': '1'}, {'image_route': 'unknown'}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                options_from_dict(values)

    def test_text_about_files_is_not_an_attachment(self):
        """类型识别必须同时匹配结构和标签。"""
        from types import SimpleNamespace
        text = SimpleNamespace(ClassName='mmui::ChatTextItemView', Name='文件：请下载')
        self.assertEqual(attachment_kind([text]), ('text', None))
        bubble = SimpleNamespace(ClassName='mmui::ChatBubbleItemView', Name='文件\nreport.pdf')
        self.assertEqual(attachment_kind([bubble]), ('file', bubble))
        image = SimpleNamespace(ClassName='mmui::ChatBubbleReferItemView', Name='图片 2026年9月8日 07:57')
        self.assertEqual(attachment_kind([image]), ('image', image))
        forward = SimpleNamespace(ClassName='mmui::ChatBubbleItemView', Name='聊天记录Leada的聊天记录')
        self.assertEqual(attachment_kind([forward]), ('forward', forward))


if __name__ == '__main__':
    unittest.main()
