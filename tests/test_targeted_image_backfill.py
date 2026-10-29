# encoding: UTF-8
"""核验日期入口的定位计算与已过期图片续跑记录。"""

from datetime import date
from contextlib import nullcontext
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch
import pytest
import targeted_image_backfill as backfill

from targeted_image_backfill import (
    IMAGE_ROW,
    calendar_day_point,
    close_history,
    configure_output_encoding,
    expired_attempts,
    ensure_clickable,
    history_window,
    preview_image,
    record_attempt,
    select_day,
    validated_window_click,
    validated_window_point_click,
    validated_window_wheel,
    wait_for_date_heading,
)
from src.modules.window.wechat_chat_adapter import AttachmentNotClickableError
from src.services.chat_attachment_service import ChatAutomationError


def test_calendar_confirmation_applies_hovered_day_without_dismissing_popup():
    """旧文件缺口日历要求先更新悬停日期再确认，点击后才应用筛选。"""
    def node(kind, name='', cls='', bounds=(0, 0, 30, 30)):
        return SimpleNamespace(ControlTypeName=kind, Name=name, ClassName=cls,
                               BoundingRectangle=bounds)
    tab = node('TabItemControl', '日期')
    panel = node('PaneControl', cls='mmui::DateNumPanel', bounds=(0, 50, 424, 470))
    confirm = node('ButtonControl', '确定', bounds=(10, 480, 90, 510))
    history = SimpleNamespace(handle=1, control=object())
    calendar = SimpleNamespace(handle=2, control=node('WindowControl', cls='mmui::XPopover'))
    nodes = [node('TextControl', '2026年'), node('TextControl', '3月'), panel, confirm]
    hovered = applied = False
    adapter = Mock()
    user32 = Mock()
    user32.ScreenToClient.return_value = True

    def post(handle, message, buttons, position):
        nonlocal hovered, applied
        if handle == calendar.handle:
            if message == 0x0200:
                hovered = True
            elif message == 0x0202 and position == (495 << 16) | 50:
                applied = hovered
        return True

    user32.PostMessageW.side_effect = post
    with patch('targeted_image_backfill.one_window', return_value=calendar), \
            patch('targeted_image_backfill.walk', side_effect=lambda control:
                  [tab] if control is history.control else nodes), \
            patch('targeted_image_backfill.wait_for_date_heading', return_value=history), \
            patch('targeted_image_backfill._user32', return_value=user32), \
            patch('targeted_image_backfill.time.sleep'):
        select_day(adapter, history, '测试群', 'chat-id', date(2026, 3, 24))
    assert applied
    assert all(call.args[0].window is not calendar for call in adapter._activate.call_args_list)


@pytest.mark.parametrize('scroll_needed', [False, True, 'stuck', 'blocked'])
def test_month_popup_stays_open_until_verified_target_is_clicked(scroll_needed):
    """重现旧菜单被激活而关闭、滚轮不更新目标可见位置的两种失败。"""
    def node(kind, name='', **values):
        return SimpleNamespace(ControlTypeName=kind, Name=name, ClassName='',
                               BoundingRectangle=(10, 10, 30, 30), **values)

    history = SimpleNamespace(handle=1, control=object())
    calendar = SimpleNamespace(handle=2, control=object())
    option = node('MenuItemControl', '5月')
    option.GetInvokePattern = Mock(return_value=SimpleNamespace(Invoke=Mock()))
    option.BoundingRectangle = (10, -100, 80, -60) if scroll_needed else (10, 50, 80, 90)
    menu = SimpleNamespace(handle=3, control=SimpleNamespace(
        ClassName='mmui::XMenu', BoundingRectangle=(0, 0, 100, 200),
        WheelUp=Mock(), WheelDown=Mock()))
    panel = node('PaneControl')
    panel.ClassName = 'mmui::DateNumPanel'
    panel.BoundingRectangle = (0, 0, 424, 420)
    tab = node('TabItemControl', '日期')
    month = node('TextControl', '10月')
    nodes = [node('TextControl', '2026年'), month, node('ComboBoxControl'),
             node('ComboBoxControl'), panel]
    closed = False
    adapter = Mock()

    def activate(context):
        nonlocal closed
        if context.window is menu:
            closed = True

    def post(handle, message, buttons, position):
        if handle == menu.handle and not closed:
            if message == 0x020A and scroll_needed != 'stuck':
                option.BoundingRectangle = (10, 50, 80, 90)
            elif message == 0x0202:
                month.Name = '5月'
        return True

    adapter._activate.side_effect = activate
    if scroll_needed == 'blocked':
        def check_point(context, x, y, *, phase):
            if context.window is menu:
                raise ChatAutomationError('菜单被遮挡')
        adapter._check_point.side_effect = check_point
    user32 = Mock()
    user32.ScreenToClient.return_value = True
    user32.PostMessageW.side_effect = post
    menu_window = SimpleNamespace(process_name='Weixin.exe', visible=True)
    with patch('targeted_image_backfill.one_window', side_effect=lambda cls, **kw:
               menu if cls == 'mmui::XMenu' else calendar), \
            patch('targeted_image_backfill.enumerate_desktop_windows', return_value=[menu_window]), \
            patch('targeted_image_backfill.attach_uia_control', return_value=menu), \
            patch('targeted_image_backfill.walk', side_effect=lambda control:
                  [tab] if control is history.control else [option] if control is menu.control else nodes), \
            patch('targeted_image_backfill.wait_for_date_heading', return_value=history), \
            patch('targeted_image_backfill._user32', return_value=user32), \
            patch('targeted_image_backfill.time.sleep'):
        if scroll_needed in ('stuck', 'blocked'):
            with pytest.raises(ChatAutomationError, match='未进入可见区域|菜单被遮挡'):
                select_day(adapter, history, '测试群', 'chat-id', date(2026, 5, 6))
            assert not any(call.args[0] == menu.handle and call.args[1] in (0x0201, 0x0202)
                           for call in user32.PostMessageW.call_args_list)
            return
        assert select_day(adapter, history, '测试群', 'chat-id', date(2026, 5, 6)) is history
    assert month.Name == '5月'
    assert not closed
    assert adapter._check_point.call_count >= 3


def test_image_row_time_accepts_wechat_weekday_label():
    """日期筛选后微信用星期标签显示图片时间，仍能定位目标分钟。"""
    assert IMAGE_ROW.fullmatch('图片 星期五 09:00').group(1) == '09:00'
    assert IMAGE_ROW.fullmatch('图片 9月18日 09:00').group(1) == '09:00'
    assert IMAGE_ROW.fullmatch('图片 09:00').group(1) == '09:00'
    assert IMAGE_ROW.fullmatch('图片 09:00 other') is None


def test_configure_output_encoding_uses_utf8_for_emoji_chat_names():
    """Windows 默认 GBK 下也能输出含 emoji 的会话名。"""
    stdout = Mock()
    stderr = Mock()
    with patch('targeted_image_backfill.sys.stdout', stdout), \
            patch('targeted_image_backfill.sys.stderr', stderr):
        configure_output_encoding()
    stdout.reconfigure.assert_called_once_with(encoding='utf-8', errors='backslashreplace')
    stderr.reconfigure.assert_called_once_with(encoding='utf-8', errors='backslashreplace')


def test_calendar_day_point_matches_observed_august_and_september_cells():
    """同一日历尺寸下按正确星期列定位两个月的已验证日期。"""
    assert calendar_day_point(date(2026, 8, 12), (1514, 608, 1938, 1028)) == (1662, 820)
    assert calendar_day_point(date(2026, 9, 8), (1542, 470, 1966, 830)) == (1626, 618)
    assert calendar_day_point(date(2026, 9, 28), (1542, 470, 1966, 830),
                              sunday_first=True) == (1626, 810)
    assert calendar_day_point(date(2026, 9, 29), (1542, 470, 1966, 830),
                              sunday_first=True) == (1690, 810)


def test_wait_for_date_heading_allows_delayed_ui_refresh():
    history = SimpleNamespace(control=object())
    old = SimpleNamespace(ControlTypeName='TextControl', Name='2026年9月28日')
    target = SimpleNamespace(ControlTypeName='TextControl', Name='2026年9月29日')
    with patch('targeted_image_backfill.one_window', return_value=history), \
            patch('targeted_image_backfill.walk', side_effect=[[old], [target]]), \
            patch('targeted_image_backfill.time.sleep') as sleep:
        assert wait_for_date_heading('chat-id', target.Name) is history
    assert sleep.call_count == 2


def test_wait_for_date_heading_reports_actual_date_on_failure():
    history = SimpleNamespace(control=object())
    old = SimpleNamespace(ControlTypeName='TextControl', Name='2026年9月28日')
    with patch('targeted_image_backfill.one_window', return_value=history), \
            patch('targeted_image_backfill.walk', return_value=[old]), \
            patch('targeted_image_backfill.time.sleep'):
        try:
            wait_for_date_heading('chat-id', '2026年9月29日')
        except ChatAutomationError as exc:
            assert '页面日期=2026年9月28日' in str(exc)
        else:
            raise AssertionError('expected date mismatch')


@pytest.mark.parametrize('label', [
    '图片 昨天 09:17',
    '图片 星期五 09:17',
    '图片 10月9日 09:17',
    '文件 2026年10月9日 星期五 09:17',
    '聊天记录\n群聊的聊天记录\n昨天 09:17',
])
def test_date_selection_accepts_target_row_below_previous_day_heading(label):
    """微信保留上一条日期标题时，用附件行末的明确日期核验目标日。"""
    history = SimpleNamespace(control=object())
    old = SimpleNamespace(ControlTypeName='TextControl', Name='2026年10月8日')
    listing = SimpleNamespace(ControlTypeName='ListControl', AutomationId='search_message_list',
                              GetChildren=lambda: [SimpleNamespace(Name=label)])
    with patch('targeted_image_backfill.one_window', return_value=history), \
            patch('targeted_image_backfill.walk', return_value=[old, listing]), \
            patch('targeted_image_backfill.date', wraps=date) as clock, \
            patch('targeted_image_backfill.time.sleep'):
        clock.today.return_value = date(2026, 10, 10)
        assert wait_for_date_heading('chat-id', '2026年10月9日') is history


@pytest.mark.parametrize('label', [
    '图片 前天 09:17',
    '图片 09:17',
    '图片 10月9日 内容里的日期 10月8日 09:17',
    '文本 昨天 09:17',
])
def test_date_selection_rejects_rows_without_target_attachment_date(label):
    """正文日期、纯时刻和其他日期不能替代目标附件的明确时间标签。"""
    history = SimpleNamespace(control=object())
    old = SimpleNamespace(ControlTypeName='TextControl', Name='2026年10月8日')
    listing = SimpleNamespace(ControlTypeName='ListControl', AutomationId='search_message_list',
                              GetChildren=lambda: [SimpleNamespace(Name=label)])
    with patch('targeted_image_backfill.one_window', return_value=history), \
            patch('targeted_image_backfill.walk', return_value=[old, listing]), \
            patch('targeted_image_backfill.date', wraps=date) as clock, \
            patch('targeted_image_backfill.time.sleep'):
        clock.today.return_value = date(2026, 10, 10)
        with pytest.raises(ChatAutomationError, match='页面日期=2026年10月8日'):
            wait_for_date_heading('chat-id', '2026年10月9日')


@pytest.mark.parametrize('label', [
    '图片 17:05',
    '文件 17:05',
    '聊天记录\n群聊的聊天记录\n17:05',
])
def test_date_selection_accepts_time_only_row_when_target_is_today(label):
    """稀疏群筛到当天时沿用旧日期标题，当天附件行只显示时分。"""
    history = SimpleNamespace(control=object())
    old = SimpleNamespace(ControlTypeName='TextControl', Name='2026年10月26日')
    listing = SimpleNamespace(ControlTypeName='ListControl', AutomationId='search_message_list',
                              GetChildren=lambda: [SimpleNamespace(Name=label)])
    with patch('targeted_image_backfill.one_window', return_value=history), \
            patch('targeted_image_backfill.walk', return_value=[old, listing]), \
            patch('targeted_image_backfill.date', wraps=date) as clock, \
            patch('targeted_image_backfill.time.sleep'):
        clock.today.return_value = date(2026, 10, 28)
        assert wait_for_date_heading('chat-id', '2026年10月28日') is history


@pytest.mark.parametrize('today, attempts', [(date(2026, 3, 24), 1), (date(2026, 3, 25), 2)])
def test_select_day_skips_sunday_first_retry_for_today(today, attempts):
    """当天筛选未匹配时不改按周日首列点到别的日期。"""
    def node(kind, name='', cls='', bounds=(0, 0, 30, 30)):
        return SimpleNamespace(ControlTypeName=kind, Name=name, ClassName=cls,
                               BoundingRectangle=bounds)
    tab = node('TabItemControl', '日期')
    panel = node('PaneControl', cls='mmui::DateNumPanel', bounds=(0, 50, 424, 470))
    history = SimpleNamespace(handle=1, control=object())
    calendar = SimpleNamespace(handle=2, control=node('WindowControl', cls='mmui::XPopover'))
    nodes = [node('TextControl', '2026年'), node('TextControl', '3月'), panel]
    user32 = Mock()
    user32.ScreenToClient.return_value = True
    user32.PostMessageW.return_value = True
    with patch('targeted_image_backfill.one_window', return_value=calendar), \
            patch('targeted_image_backfill.walk', side_effect=lambda control:
                  [tab] if control is history.control else nodes), \
            patch('targeted_image_backfill.wait_for_date_heading',
                  side_effect=ChatAutomationError('页面日期=2026年3月22日')) as wait, \
            patch('targeted_image_backfill._user32', return_value=user32), \
            patch('targeted_image_backfill.date', wraps=date) as clock, \
            patch('targeted_image_backfill.time.sleep'):
        clock.today.return_value = today
        with pytest.raises(ChatAutomationError, match='页面日期'):
            select_day(Mock(), history, '测试群', 'chat-id', date(2026, 3, 24))
    assert wait.call_count == attempts


def test_expired_attempts_only_skips_confirmed_expiry(tmp_path):
    """缩略图未补成原图时仍可重试，微信明确过期的消息才跳过。"""
    journal = tmp_path / 'attempts.jsonl'
    with patch('targeted_image_backfill.ATTEMPT_LOG', journal):
        record_attempt('chat:1:100', 'thumbnail')
        record_attempt('chat:2:101', 'expired')
        assert expired_attempts() == {'chat:2:101'}
        entries = [json.loads(line) for line in journal.read_text(encoding='utf-8').splitlines()]
        assert [entry['result'] for entry in entries] == ['thumbnail', 'expired']


def test_current_state_finds_image_in_previous_day_export(tmp_path):
    # 北京时间 23:50 的图片在东京时间已是次日，仍在前一天的日导出里。
    message = {'timestamp': 1793202631, 'local_id': 4241763, 'message_id': 'm1',
               'attachment': {'kind': 'image'}}
    payload = {'conversations': [{'session_name': '群', 'session_id': 's1', 'messages': [message]}]}
    (tmp_path / 'wechat_date_json_20261028.json').write_text(json.dumps(payload), encoding='utf-8')
    expected = backfill.load_inventory(tmp_path, '群')[1][0]['state']

    assert backfill.current_state(tmp_path, '群', date(2026, 10, 29), 4241763, 1793202631) == expected


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
    adapter._check_point.assert_called_once_with(context, 120, 230, phase='before_force')
    assert user32.PostMessageW.call_args_list == [
        ((1234, 0x0201, 0x0001, (30 << 16) | 20),),
        ((1234, 0x0202, 0, (30 << 16) | 20),),
    ]
    assert sleep_mock.call_count == 2
    sleep_mock.assert_called_with(0.05)


def test_validated_window_point_double_click_posts_two_complete_clicks():
    """图片正文可在无物理远控鼠标时用窗口消息双击。"""
    context = SimpleNamespace(window=SimpleNamespace(handle=4321))
    adapter = Mock()
    user32 = Mock()
    user32.ScreenToClient.return_value = True
    user32.PostMessageW.return_value = True

    with patch('targeted_image_backfill._user32', return_value=user32), \
            patch('targeted_image_backfill.time.sleep'):
        validated_window_point_click(adapter, context, 12, 34, clicks=2)

    assert [args[0][1] for args in user32.PostMessageW.call_args_list] == [
        0x0201, 0x0202, 0x0201, 0x0202]


def test_occluded_point_forces_foreground_then_rechecks():
    context = SimpleNamespace(window=SimpleNamespace(control=SimpleNamespace(ClassName='other')))
    adapter = Mock()
    adapter._check_point.side_effect = [AttachmentNotClickableError('blocked'), None]

    ensure_clickable(adapter, context, 12, 34)

    assert adapter._activate.call_args_list == [
        ((context,), {}), ((context,), {'force': True})]
    assert adapter._check_point.call_count == 2
    assert [call.kwargs['phase'] for call in adapter._check_point.call_args_list] == [
        'before_force', 'after_force']


def test_occluded_point_still_fails_after_one_foreground_retry():
    context = SimpleNamespace(window=SimpleNamespace(control=SimpleNamespace(ClassName='other')))
    adapter = Mock()
    adapter._check_point.side_effect = AttachmentNotClickableError('blocked')

    with pytest.raises(AttachmentNotClickableError, match='blocked'):
        ensure_clickable(adapter, context, 12, 34)

    assert adapter._activate.call_count == 2
    assert adapter._check_point.call_count == 2


def test_calendar_popup_never_forces_foreground():
    context = SimpleNamespace(window=SimpleNamespace(control=SimpleNamespace(ClassName='mmui::XPopover')))
    adapter = Mock()
    adapter._check_point.side_effect = AttachmentNotClickableError('blocked')

    with pytest.raises(AttachmentNotClickableError):
        ensure_clickable(adapter, context, 12, 34)

    adapter._activate.assert_not_called()


def test_validated_window_wheel_posts_screen_coordinates_and_negative_delta():
    """向下翻聊天记录时发送负滚轮增量并保留屏幕坐标。"""
    control = SimpleNamespace(BoundingRectangle=(100, 200, 300, 600))
    context = SimpleNamespace(window=SimpleNamespace(handle=9876))
    adapter = Mock()
    user32 = Mock()
    user32.PostMessageW.return_value = True

    with patch('targeted_image_backfill._user32', return_value=user32), \
            patch('targeted_image_backfill.time.sleep'):
        validated_window_wheel(adapter, context, control, older=False, steps=2)

    adapter._check_point.assert_called_once_with(context, 200, 400, phase='before_force')
    expected_wparam = ((-120) & 0xffff) << 16
    expected_lparam = (400 << 16) | 200
    assert user32.PostMessageW.call_args_list == [
        ((9876, 0x020A, expected_wparam, expected_lparam),),
        ((9876, 0x020A, expected_wparam, expected_lparam),),
    ]


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


def test_preview_image_waits_for_delayed_preview_window():
    """图片双击后预览窗口创建较慢时继续轮询。"""
    listing = SimpleNamespace(AutomationId='search_message_list', BoundingRectangle=(0, 0, 1000, 800))
    row = SimpleNamespace(BoundingRectangle=(100, 100, 900, 500))
    history = SimpleNamespace(control=object())
    preview = SimpleNamespace(control=SimpleNamespace(AutomationId='PreviewWindow_chat-id_123_456_0_0'))
    adapter = Mock()
    missing = ChatAutomationError('mmui::PreviewWindow需要唯一匹配，实际找到 0 个')

    with patch('targeted_image_backfill.walk', return_value=[listing]), \
            patch('targeted_image_backfill.context_for', return_value='history-context'), \
            patch('targeted_image_backfill.validated_window_point_click') as click_mock, \
            patch('targeted_image_backfill.one_window', side_effect=[missing, preview]), \
            patch('targeted_image_backfill.time.sleep') as sleep_mock:
        result = preview_image(adapter, history, '测试群', row, 'chat-id')

    assert result == (preview, 123, 456)
    click_mock.assert_called_once()
    sleep_mock.assert_called_once_with(0.1)


def test_one_window_skips_unavailable_uia_and_reuses_attached_control():
    """窗口消失或挂接失败不阻断后续有效窗口，也不重复挂接。"""
    from targeted_image_backfill import one_window
    missing = SimpleNamespace(process_name='Weixin.exe', visible=True, is_minimized=False, control=None)
    valid = SimpleNamespace(process_name='Weixin.exe', visible=True, is_minimized=False, control=None)
    attached = SimpleNamespace(control=SimpleNamespace(ClassName='preview', AutomationId='chat-id'))
    with patch('targeted_image_backfill.enumerate_desktop_windows', return_value=[missing, valid]), \
            patch('targeted_image_backfill.attach_uia_control', side_effect=[missing, attached]) as attach:
        assert one_window('preview', session_id='chat-id') is attached
    assert attach.call_count == 2


def test_one_window_without_uia_uses_existing_missing_window_error():
    """全都无 UIA 控件时，仍进入已有轮询/清理使用的零匹配分支。"""
    import pytest
    from targeted_image_backfill import one_window
    missing = SimpleNamespace(process_name='Weixin.exe', visible=True, is_minimized=False, control=None)
    with patch('targeted_image_backfill.enumerate_desktop_windows', return_value=[missing]), \
            patch('targeted_image_backfill.attach_uia_control', return_value=missing):
        with pytest.raises(ChatAutomationError, match='实际找到 0 个'):
            one_window('preview')


def test_stalled_date_query_rebuilds_window_and_verifies_preview_without_downloading(tmp_path):
    """旧日期列表停在文字末尾时重建窗口，从新结果精确核验消息身份。"""
    stamp, local_id = 1792073150, 6368
    target = {'key': 'chat-id:6368:1792073150', 'message_id': 4186492, 'timestamp': stamp}
    targets = {(stamp, local_id): target}
    old, fresh = SimpleNamespace(handle=1, control='old'), SimpleNamespace(handle=2, control='fresh')
    text = SimpleNamespace(Name='机构数据10月...更多 昨天 22:05', ClassName='mmui::ChatTextItemView',
                           AutomationId='row', BoundingRectangle=(0, 400, 600, 465))
    image = SimpleNamespace(Name='图片 昨天 22:05', AutomationId='row',
                            BoundingRectangle=(0, 100, 600, 400), GetRuntimeId=lambda: [1])
    listings = {key: SimpleNamespace(AutomationId='search_message_list',
                                     BoundingRectangle=(0, 0, 600, 500),
                                     GetChildren=lambda row=row: [row])
                for key, row in [('old', text), ('fresh', image)]}
    preview = SimpleNamespace(control=SimpleNamespace(
        AutomationId='PreviewWindow_chat-id_1792073150_6368_0_0'))
    active = [old]
    adapter = Mock()
    adapter.cancelled.return_value = False

    def reopen(*args):
        """模拟丢弃旧查询后，新窗口暴露此前缺失的图片行。"""
        active[0] = fresh
        return fresh

    evidence = tmp_path / 'locate.jsonl'
    with patch.object(backfill, 'select_day', side_effect=lambda a, h, *args: h), \
            patch.object(backfill, 'one_window', side_effect=lambda *args, **kw: active[0]), \
            patch.object(backfill, 'walk', side_effect=lambda control: [listings[control]]), \
            patch.object(backfill, 'history_window', side_effect=reopen) as history, \
            patch.object(backfill, 'close_history') as close, \
            patch.object(backfill, 'preview_image', return_value=(preview, stamp, local_id)), \
            patch.object(backfill, 'close_preview'), patch.object(backfill, 'validated_window_wheel'), \
            patch.object(backfill, 'validated_window_click') as download, \
            patch.object(backfill, 'current_state') as state, \
            patch.object(backfill, 'record_attempt') as attempt, patch.object(backfill.time, 'sleep'):
        count, current = backfill.run_day(adapter, old, '群', 'chat-id', date(2026, 10, 15), targets,
                                          tmp_path, '群', 0, locate_only=True, diagnostic_log=evidence)
    assert count == 1 and not targets
    assert current.control == 'fresh'
    close.assert_called_once_with(adapter, '群', 'chat-id')
    history.assert_called_once()
    download.assert_not_called()
    state.assert_not_called()
    attempt.assert_not_called()
    records = [json.loads(line) for line in evidence.read_text(encoding='utf-8').splitlines()]
    assert [item['event'] for item in records].count('rebuild_date_query') == 1
    verified = [item for item in records if item['event'] == 'preview_verified']
    assert verified[0]['message_id'] == 4186492
    assert verified[0]['automation_id'] == preview.control.AutomationId
    assert any(item['event'] == 'unlocated' and item['last_rows'][0]['tail'].endswith('22:05')
               for item in records)


@pytest.mark.parametrize('handled,limit,cancelled,scans', [(0, 0, False, 2), (1, 1, False, 1),
                                                        (0, 0, True, 1)])
def test_query_rebuild_is_bounded_and_respects_limit_or_cancel(handled, limit, cancelled, scans):
    """第二个查询仍缺消息时保留缺口，不无限重试；限额和取消时不重建。"""
    adapter = Mock()
    adapter.cancelled.return_value = cancelled
    targets = {(1, 1): {'key': 'chat:1:1'}}
    with patch.object(backfill, 'scan_day', return_value=handled) as scan, \
            patch.object(backfill, 'close_history') as close, \
            patch.object(backfill, 'history_window', return_value=SimpleNamespace(handle=2)):
        _, history = backfill.run_day(adapter, SimpleNamespace(handle=1), '群', 'chat', date(2026, 10, 15),
                                      targets, None, '群', limit)
    assert scan.call_count == scans
    assert close.call_count == scans - 1
    # 重建后旧窗口已关闭，必须把新窗口交回给下一天。
    assert history.handle == (2 if scans == 2 else 1)
    assert targets


@pytest.mark.parametrize('automation_id', ['PreviewWindow_other_123_456_0_0', 'invalid'])
def test_preview_image_rejects_wrong_session_or_unparseable_identity(automation_id):
    """刷新查询也不能放松预览 AutomationId 的群身份校验。"""
    listing = SimpleNamespace(AutomationId='search_message_list', BoundingRectangle=(0, 0, 1000, 800))
    row = SimpleNamespace(BoundingRectangle=(100, 100, 900, 500))
    preview = SimpleNamespace(control=SimpleNamespace(AutomationId=automation_id))
    with patch.object(backfill, 'walk', return_value=[listing]), \
            patch.object(backfill, 'validated_window_point_click'), \
            patch.object(backfill, 'one_window', return_value=preview):
        with pytest.raises(ChatAutomationError, match='无法与目标群对应'):
            preview_image(Mock(), SimpleNamespace(control=object()), '群', row, 'chat-id')


def test_main_passes_rebuilt_history_window_to_next_day(tmp_path):
    """前一天重建查询后旧窗口已关闭，下一天必须用新窗口。"""
    options = SimpleNamespace(chat='群', export_chat=None, export_root=tmp_path,
                              since=date(2026, 10, 29), max_items=0, target_ids_file=None,
                              locate_only=False, retry_expired=True, diagnostic_log=None)
    items = [{'session_id': 'chat', 'message_id': n, 'kind': 'image', 'state': 'thumbnail',
              'timestamp': stamp, 'local_id': n, 'key': f'chat:{n}:{stamp}'}
             for n, stamp in [(1, 1793170800), (2, 1793289000)]]  # 相隔 30 多小时，任何时区都分属两天
    adapter = Mock()
    adapter.compatibility.return_value = nullcontext()
    first, rebuilt = SimpleNamespace(handle=1), SimpleNamespace(handle=2)
    seen = []

    def run(adapter, history, *args, **kwargs):
        seen.append(history)
        return 1, rebuilt

    with patch.object(backfill, 'parse_args', return_value=options), \
            patch.object(backfill, 'load_inventory', return_value=('chat', items)), \
            patch.object(backfill, 'task_lock', return_value=nullcontext()), \
            patch.object(backfill, 'WechatChatAdapter', return_value=adapter), \
            patch.object(backfill, 'close_preview'), patch.object(backfill, 'close_history'), \
            patch.object(backfill, 'history_window', return_value=first), \
            patch.object(backfill, 'run_day', side_effect=run):
        backfill.main()
    assert seen == [first, rebuilt]


@pytest.mark.parametrize('located', [False, True])
def test_locate_cli_includes_originals_and_requires_actual_location(tmp_path, located):
    """四图已完成也必须实际定位；未定位时抛错，不能以空待办返回成功。"""
    ids = tmp_path / 'ids.txt'
    ids.write_text('chat:42\n', encoding='utf-8')
    options = SimpleNamespace(chat='群', export_chat=None, export_root=tmp_path,
                              since=date(2026, 10, 15), max_items=0, target_ids_file=ids,
                              locate_only=True, diagnostic_log=tmp_path / 'evidence.jsonl')
    item = {'session_id': 'chat', 'message_id': 42, 'kind': 'image', 'state': 'original',
            'timestamp': 1792073150, 'local_id': 6368, 'key': 'chat:6368:1792073150'}
    adapter = Mock()
    adapter.compatibility.return_value = nullcontext()

    def scan(*args, **kwargs):
        """只有实际核验成功的模拟结果才能清除目标。"""
        assert kwargs['locate_only']
        assert args[5] == {(item['timestamp'], item['local_id']): item}
        if located:
            args[5].clear()
        return int(located), args[1]

    with patch.object(backfill, 'parse_args', return_value=options), \
            patch.object(backfill, 'load_inventory', return_value=('chat', [item])), \
            patch.object(backfill, 'task_lock', return_value=nullcontext()), \
            patch.object(backfill, 'WechatChatAdapter', return_value=adapter), \
            patch.object(backfill, 'close_preview'), patch.object(backfill, 'close_history'), \
            patch.object(backfill, 'history_window'), patch.object(backfill, 'expired_attempts') as expired, \
            patch.object(backfill, 'run_day', side_effect=scan) as run:
        if located:
            backfill.main()
        else:
            with pytest.raises(ChatAutomationError, match='未找到全部'):
                backfill.main()
        run.assert_called_once()
        expired.assert_not_called()


@pytest.mark.parametrize('expired', [False, True])
def test_normal_scan_preserves_original_download_and_expiration_rules(tmp_path, expired):
    """正常任务仍按真实过期提示处理，不能让重建查询改变原图或过期判定。"""
    stamp, local_id = 1792073150, 6368
    targets = {(stamp, local_id): {'key': 'chat:6368:1792073150', 'timestamp': stamp}}
    row = SimpleNamespace(Name='图片 昨天 22:05', BoundingRectangle=(0, 0, 600, 400),
                          GetRuntimeId=lambda: [1])
    listing = SimpleNamespace(AutomationId='search_message_list', GetChildren=lambda: [row],
                              BoundingRectangle=(0, 0, 600, 500))
    history, preview = SimpleNamespace(control='history'), SimpleNamespace(control='preview')
    nodes = [SimpleNamespace(Name='图片已过期或被清理' if expired else '查看原图',
                             ControlTypeName='TextControl' if expired else 'ButtonControl')]
    adapter = Mock()
    adapter.cancelled.return_value = False
    with patch.object(backfill, 'select_day', return_value=history), \
            patch.object(backfill, 'one_window', return_value=history), \
            patch.object(backfill, 'walk', side_effect=lambda control:
                         [listing] if control == 'history' else nodes), \
            patch.object(backfill, 'preview_image', return_value=(preview, stamp, local_id)), \
            patch.object(backfill, 'current_state', return_value='original'), \
            patch.object(backfill, 'record_attempt') as record, \
            patch.object(backfill, 'validated_window_click') as click, \
            patch.object(backfill, 'close_preview'), patch.object(backfill.time, 'sleep'):
        count = backfill.scan_day(adapter, history, '群', 'chat', date(2026, 10, 15),
                                  targets, tmp_path, '群', 0)
    assert count == 1 and not targets
    assert click.call_count == int(not expired)
    record.assert_called_once_with('chat:6368:1792073150', 'expired' if expired else 'original')
