from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from contextlib import nullcontext
from unittest.mock import Mock, patch
import pytest
import targeted_file_backfill as files
from src.modules.window.wechat_chat_adapter import WechatChatAdapter
from src.services.chat_attachment_service import ChatAutomationError


def test_receiver_does_not_count_as_a_second_shell():
    shell, receiver = object(), object()
    attached = {shell: SimpleNamespace(control=SimpleNamespace(ProcessId=1, ClassName='mmui::MainWindow')),
                receiver: SimpleNamespace(control=SimpleNamespace(ProcessId=1, ClassName='mmui::FileReceiveWindow'))}
    adapter = object.__new__(WechatChatAdapter)
    adapter._main_windows = Mock(return_value=[receiver, shell])
    with patch('src.modules.window.wechat_chat_adapter.attach_uia_control', side_effect=attached.__getitem__), \
         patch('src.modules.window.wechat_chat_adapter.temporary_accessibility', side_effect=lambda pid: nullcontext()):
        assert adapter._locate_main() is attached[shell]
        attached[receiver].control.ClassName = 'mmui::MainWindow'
        with pytest.raises(ChatAutomationError):
            adapter._locate_main()


def fixture_export(tmp_path, **updates):
    a = {'kind': 'file', 'filename': 'one.pdf', 'size': 8, 'md5': 'a'*32, 'path': str(tmp_path/'one.pdf')}
    a.update(updates)
    m = {'message_id': 1, 'local_id': 2, 'timestamp': 100, 'attachment': a}
    p = tmp_path/'export.json'
    p.write_text(json.dumps({'conversations': [{'session_id': 'g', 'messages': [m]}]}))
    return p


@pytest.mark.parametrize('updates', [{'filename':'one.exe'}, {'md5':None}, {'size':0}, {'size':33*1024**2}, {'path':None}])
def test_bad_native_metadata_refuses_gui(tmp_path, updates):
    with pytest.raises(ValueError):
        files.load_target(fixture_export(tmp_path, **updates), 'g:1')


def test_existing_hash_named_file_is_verified_and_skips_gui(tmp_path):
    content = b'%PDF-one'
    right = tmp_path/'hash.pdf';right.write_bytes(content)
    (tmp_path/'one.pdf').write_bytes(b'wrong123')
    p = fixture_export(tmp_path, md5=hashlib.md5(content).hexdigest())
    assert files.receive(p, 'g:1', 'chat') == {'target_id': 'g:1', 'status': 'already_present', 'path': str(right)}
    right.write_bytes(b'wrong456')
    assert files.content_file(files.load_target(p, 'g:1')['attachment']) is None


def test_row_requires_exact_filename_and_unambiguous_year():
    day=datetime.now().date().replace(month=4,day=20)
    assert files.row_matches('one.pdf  4/20', 'one.pdf', day)
    assert files.row_matches('one.pdf 下载失败 4/20', 'one.pdf', day)
    assert not files.row_matches('one.pdf other.pdf 下载失败 4/20', 'one.pdf', day)
    assert not files.row_matches('other.pdf  4/20', 'one.pdf', day)
    assert not files.row_matches('one.pdf  4/21', 'one.pdf', day)
    assert not files.row_matches('one.pdf  4/20', 'one.pdf', day.replace(year=day.year-1))


def test_relative_file_date_is_resolved_against_current_local_day():
    today = datetime.now().date()
    assert files.row_matches('one.pdf 昨天', 'one.pdf', today-timedelta(days=1))
    assert files.row_matches('one.pdf 今天', 'one.pdf', today)
    assert files.row_matches('one.pdf 前天', 'one.pdf', today-timedelta(days=2))
    assert not files.row_matches('one.pdf 昨天', 'one.pdf', today-timedelta(days=2))


@pytest.mark.parametrize('name', ['接收文件', '继续下载'])
def test_receiver_accepts_explicit_receive_or_resume(name):
    button = SimpleNamespace(ControlTypeName='ButtonControl', Name=name)
    text = SimpleNamespace(ControlTypeName='TextControl', Name=name)
    assert files.receive_button([button, text]) is button


def test_receiver_refuses_open_and_ambiguous_actions():
    def button(name):
        return SimpleNamespace(ControlTypeName='ButtonControl', Name=name)
    with pytest.raises(ValueError):
        files.receive_button([button('打开文件')])
    with pytest.raises(ValueError):
        files.receive_button([button('接收文件'), button('继续下载')])


def test_download_menu_requires_new_same_process_window_and_exact_action():
    import targeted_image_backfill as images
    item = SimpleNamespace(ControlTypeName='MenuItemControl', Name='下载')
    save_as = SimpleNamespace(ControlTypeName='MenuItemControl', Name='下载到...')
    menu = SimpleNamespace(handle=7, control=SimpleNamespace(ProcessId=42, ClassName='mmui::XMenu'))
    with patch.object(images, 'walk', return_value=[item, save_as]):
        assert files.download_menu_item(menu, {1}, 42) is item
        for before, pid in [({7}, 42), ({1}, 99)]:
            with pytest.raises(ValueError):
                files.download_menu_item(menu, before, pid)
    with patch.object(images, 'walk', return_value=[save_as]):
        with pytest.raises(ValueError):
            files.download_menu_item(menu, {1}, 42)


def test_download_menu_refuses_input_before_failed_card_guard():
    import targeted_image_backfill as images
    adapter = Mock()
    adapter._click_target.side_effect = ChatAutomationError('wrong owner')
    with patch.object(images, 'enumerate_desktop_windows', return_value=[]), \
         patch('ctypes.WinDLL', create=True) as user:
        with pytest.raises(ChatAutomationError):
            files.request_menu_download(adapter, object(), object(), 'chat')
        user.assert_not_called()


def test_disconnected_calendar_fits_reported_children_without_bypassing_bounds():
    import targeted_image_backfill as images
    root=SimpleNamespace(ClassName='mmui::XPopover',BoundingRectangle=(100,200,428,608))
    calendar=SimpleNamespace(handle=1,control=root)
    child=SimpleNamespace(BoundingRectangle=(110,220,756,1016))
    transport=Mock();transport.verify_window.side_effect=[(100,200,428,608),(100,200,756,1016)]
    adapter=SimpleNamespace(_message_transport=transport)
    user=Mock();user.SetWindowPos.return_value=True
    with patch.object(images,'walk',return_value=[root,child]),patch.object(images,'_user32',return_value=user), \
         patch.object(images,'one_window',return_value=calendar),patch.object(images.time,'sleep'):
        assert images.fit_calendar_popup(adapter,calendar,'chat') is calendar
    user.SetWindowPos.assert_called_once_with(1,0,100,200,656,816,0x14)
    assert transport.verify_window.call_count==2


def test_calendar_refuses_unbounded_target():
    import targeted_image_backfill as images
    root=SimpleNamespace(ClassName='mmui::XPopover',BoundingRectangle=(0,0,100,100))
    cal=SimpleNamespace(handle=1,control=root)
    adapter=SimpleNamespace(_message_transport=Mock())
    adapter._message_transport.verify_window.return_value=(0,0,100,100)
    with patch.object(images,'walk',return_value=[root,SimpleNamespace(BoundingRectangle=(1,1,3000,4000))]), \
         patch.object(images,'_user32') as user:
        with pytest.raises(ChatAutomationError,match='尺寸异常'):
            images.fit_calendar_popup(adapter,cal,'chat')
        user.assert_not_called()
