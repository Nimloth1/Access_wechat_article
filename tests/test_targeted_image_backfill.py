# encoding: UTF-8
"""核验日期入口的定位计算与已过期图片续跑记录。"""

from datetime import date
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from targeted_image_backfill import (
    IMAGE_ROW,
    calendar_day_point,
    close_history,
    expired_attempts,
    history_window,
    record_attempt,
    validated_window_click,
)
from src.services.chat_attachment_service import ChatAutomationError


def test_image_row_time_accepts_wechat_weekday_label():
    """日期筛选后微信用星期标签显示图片时间，仍能定位目标分钟。"""
    assert IMAGE_ROW.fullmatch('图片 星期五 09:00').group(1) == '09:00'
    assert IMAGE_ROW.fullmatch('图片 9月18日 09:00').group(1) == '09:00'


def test_calendar_day_point_matches_observed_august_and_september_cells():
    """同一日历尺寸下按正确星期列定位两个月的已验证日期。"""
    assert calendar_day_point(date(2026, 8, 12), (1514, 608, 1938, 1028)) == (1662, 820)
    assert calendar_day_point(date(2026, 9, 8), (1542, 470, 1966, 830)) == (1626, 618)


def test_expired_attempts_only_skips_confirmed_expiry(tmp_path):
    """缩略图未补成原图时仍可重试，微信明确过期的消息才跳过。"""
    journal = tmp_path / 'attempts.jsonl'
    with patch('targeted_image_backfill.ATTEMPT_LOG', journal):
        record_attempt('chat:1:100', 'thumbnail')
        record_attempt('chat:2:101', 'expired')
        assert expired_attempts() == {'chat:2:101'}
        entries = [json.loads(line) for line in journal.read_text(encoding='utf-8').splitlines()]
        assert [entry['result'] for entry in entries] == ['thumbnail', 'expired']


def test_validated_window_click_checks_owner_then_posts_client_click():
    """窗口消息点击前仍执行前台和坐标归属校验。"""
    control = SimpleNamespace(BoundingRectangle=(110, 220, 130, 240))
    context = SimpleNamespace(window=SimpleNamespace(handle=1234))
    adapter = Mock()
    user32 = Mock()
    user32.ScreenToClient.side_effect = lambda handle, point: (
        setattr(point._obj, 'x', point._obj.x - 100) or
        setattr(point._obj, 'y', point._obj.y - 200) or True)
    user32.PostMessageW.return_value = True

    with patch('targeted_image_backfill._user32', return_value=user32), \
            patch('targeted_image_backfill.time.sleep') as sleep_mock:
        validated_window_click(adapter, context, control)

    adapter._activate.assert_called_once_with(context)
    adapter._check_point.assert_called_once_with(context, 120, 230)
    assert user32.PostMessageW.call_args_list == [
        ((1234, 0x0201, 0x0001, (30 << 16) | 20),),
        ((1234, 0x0202, 0, (30 << 16) | 20),),
    ]
    sleep_mock.assert_called_once_with(0.08)


def test_close_history_clicks_only_the_history_close_button():
    """补图收尾关闭聊天记录窗口，不操作微信主窗口。"""
    close_button = SimpleNamespace(ControlTypeName='ButtonControl', Name='关闭')
    other_button = SimpleNamespace(ControlTypeName='ButtonControl', Name='最小化')
    window = SimpleNamespace(control=object())
    adapter = Mock()

    with patch('targeted_image_backfill.one_window', return_value=window) as one_window_mock, \
            patch('targeted_image_backfill.walk', return_value=[other_button, close_button]), \
            patch('targeted_image_backfill.context_for', return_value='history-context'), \
            patch('targeted_image_backfill.validated_window_click') as click_mock:
        close_history(adapter, '测试群', 'chat-id')

    one_window_mock.assert_called_once_with('mmui::SearchMsgUniqueChatWindow', session_id='chat-id')
    click_mock.assert_called_once_with(adapter, 'history-context', close_button)


def test_history_window_polls_until_delayed_window_appears():
    """聊天记录窗口创建较慢时轮询等待，不重复点击入口。"""
    history_button = SimpleNamespace(ControlTypeName='ButtonControl', Name='聊天记录')
    main = SimpleNamespace(window=SimpleNamespace(control=object()))
    history = SimpleNamespace(control=object())
    adapter = Mock()
    missing = ChatAutomationError('mmui::SearchMsgUniqueChatWindow需要唯一匹配，实际找到 0 个')

    with patch('targeted_image_backfill.one_window', side_effect=[missing, missing, history]) as one_window_mock, \
            patch('targeted_image_backfill.walk', return_value=[history_button]), \
            patch('targeted_image_backfill.validated_window_click') as click_mock, \
            patch('targeted_image_backfill.time.sleep') as sleep_mock:
        result = history_window(adapter, main, '测试群', 'chat-id')

    assert result is history
    assert one_window_mock.call_count == 3
    click_mock.assert_called_once_with(adapter, main, history_button)
    sleep_mock.assert_called_once_with(0.2)
