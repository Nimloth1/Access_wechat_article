# encoding: UTF-8
"""验证无标题窗口、锁屏状态及诊断读取失败，不操作真实微信。"""
import ctypes
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from src.modules.window.window_diagnostics import (
    _Reader, _SessionInfo, collect_window_diagnostic, record_window_diagnostic,
)


def make_reader():
    """构造可按需替换只读 API 的诊断器。"""
    reader = _Reader.__new__(_Reader)
    reader.errors = {}
    return reader


def test_untitled_child_window_keeps_process_identity_and_topmost():
    """无标题的命中子窗口仍有 PID、根窗、进程和扩展样式。"""
    reader = make_reader()

    def thread(handle, pid):
        """填充 Win32 进程 ID 出参。"""
        pid._obj.value = 42
        return 7

    def rect(handle, value):
        """填充实际窗口矩形。"""
        value._obj.left, value._obj.top = 10, 20
        value._obj.right, value._obj.bottom = 30, 40
        return True

    reader.user32 = SimpleNamespace(
        IsWindow=Mock(return_value=True), GetWindowThreadProcessId=Mock(side_effect=thread),
        GetWindowTextW=Mock(return_value=0), GetClassNameW=Mock(return_value=0),
        GetWindowRect=Mock(side_effect=rect), IsWindowVisible=Mock(return_value=True),
        IsIconic=Mock(return_value=False), GetWindowLongW=Mock(return_value=0x08000088),
        GetAncestor=Mock(return_value=99), GetWindow=Mock(return_value=100),
        GetThreadDesktop=Mock(return_value=123), GetDpiForWindow=Mock(return_value=144))
    reader.desktop_name = Mock(return_value='Default')
    reader.process = Mock(return_value={'name': 'wetype.exe', 'path': 'C:/wetype.exe'})
    reader.kernel32 = SimpleNamespace(ProcessIdToSessionId=lambda pid, value:
                                     setattr(value._obj, 'value', 3) or True)

    window = reader.window(555)

    assert window['title'] == ''
    assert window['pid'] == 42 and window['root'] == 99 and window['owner'] == 100
    assert window['process']['name'] == 'wetype.exe'
    assert window['session_id'] == 3
    assert window['topmost'] and window['toolwindow'] and window['noactivate']
    assert window['rect'] == [10, 20, 30, 40] and window['dpi'] == 144
    assert reader.errors == {}


def test_input_desktop_name_failure_closes_handle_and_keeps_error():
    """输入桌面名字不可读时释放句柄，并保留失败而非当作 Default。"""
    reader = make_reader()
    reader.user32 = SimpleNamespace(OpenInputDesktop=Mock(return_value=123),
                                    CloseDesktop=Mock(return_value=True))
    reader.desktop_name = Mock(side_effect=PermissionError('拒绝访问'))

    assert reader.read('input_desktop', reader.input_desktop) is None
    assert '拒绝访问' in reader.errors['input_desktop']
    reader.user32.CloseDesktop.assert_called_once_with(123)


@pytest.mark.parametrize('flags,locked', [(0, True), (1, False), (-1, None)])
def test_session_flags_distinguish_locked_unlocked_and_unknown(flags, locked):
    """解析真实对齐的 WTS 结构，未知锁屏标志不推断为解锁。"""
    reader = make_reader()
    info = _SessionInfo()
    info.level, info.data.session_id = 1, 3
    info.data.state, info.data.flags, info.data.station = 0, flags, 'Console'

    def session_id(pid, value):
        """填充当前进程会话 ID。"""
        value._obj.value = 3
        return True

    def query(server, session, kind, buffer, size):
        """返回内存中的原生结构。"""
        assert session == 3 and kind == 25
        buffer._obj.value, size._obj.value = ctypes.addressof(info), ctypes.sizeof(info)
        return True

    reader.kernel32 = SimpleNamespace(GetCurrentProcessId=lambda: 50,
                                     ProcessIdToSessionId=session_id)
    reader.wts = SimpleNamespace(WTSQuerySessionInformationW=query, WTSFreeMemory=Mock())

    session = reader.session()

    assert session['id'] == 3 and session['info']['locked'] is locked
    assert session['info']['flags'] == flags
    reader.wts.WTSFreeMemory.assert_called_once()


def test_snapshot_keeps_original_blocker_even_if_point_owner_changes():
    """短暂窗口已失效时不把采样期间新命中的窗口归因成原始遮挡。"""
    reader = make_reader()
    reader.user32 = SimpleNamespace(GetForegroundWindow=lambda: 1,
                                    WindowFromPoint=lambda point: 4,
                                    GetThreadDesktop=lambda thread: 123,
                                    GetThreadDpiAwarenessContext=lambda: 0,
                                    GetAwarenessFromDpiAwarenessContext=lambda context: 0,
                                    GetSystemMetrics=lambda index: 100)
    reader.kernel32 = SimpleNamespace(GetCurrentThreadId=lambda: 7)
    reader.window = lambda handle: {'handle': handle, 'valid': handle != 3,
                                   'root': 2 if handle == 4 else handle, 'owner': 0}
    reader.input_desktop = lambda: 'Default'
    reader.desktop_name = lambda handle: 'Default'
    reader.session = lambda: {'id': 3}
    with patch('src.modules.window.window_diagnostics._Reader', return_value=reader):
        snapshot = collect_window_diagnostic(1, blocker_handle=3, point=(10, 20), phase='after_force')
    assert snapshot['roles']['blocker'] == 3 and snapshot['roles']['point_owner_now'] == 4
    assert snapshot['windows']['3']['valid'] is False
    assert snapshot['windows']['2']['valid'] is True


def test_record_logs_diagnostic_failure_without_raising(capsys):
    """诊断 API 初始化失败时仍输出可解析记录，原操作异常可继续传播。"""
    with patch('src.modules.window.window_diagnostics.collect_window_diagnostic',
               side_effect=OSError('API 不可用')):
        record_window_diagnostic(1, blocker_handle=3, point=(10, 20), phase='before_force')
    line = capsys.readouterr().out
    assert line.startswith('[window-diagnostic] ')
    result = json.loads(line[len('[window-diagnostic] '):])
    assert result['blocker'] == 3 and result['phase'] == 'before_force'
    assert 'API 不可用' in result['diagnostic_error']
