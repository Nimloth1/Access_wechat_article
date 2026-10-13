from types import SimpleNamespace
from unittest.mock import Mock

from src.modules.window.home_window_activation import WindowsHomeWindowGuard


class FakeUser32:
    def __init__(self) -> None:
        self.dpi_context_calls = 0

    def WindowFromPoint(self, point):
        assert (point.x, point.y) == (1319, 56)
        return 6165560

    def IsChild(self, parent, child):
        return False

    def GetAncestor(self, handle, flag):
        return handle

    def SetThreadDpiAwarenessContext(self, context):
        self.dpi_context_calls += 1
        return None


def test_click_guard_keeps_uia_coordinate_context():
    user32 = FakeUser32()
    guard = WindowsHomeWindowGuard(user32=user32, platform_name="Windows")
    home = SimpleNamespace(handle=6165560)
    target = SimpleNamespace(
        home_window_handle=6165560,
        click_x=1319,
        click_y=56,
    )

    guard.ensure_target_clickable(home, target)

    assert user32.dpi_context_calls == 0


def test_force_activation_does_not_skip_foreground_home():
    user32 = Mock()
    user32.IsWindow.return_value = True
    user32.IsIconic.return_value = False
    user32.GetForegroundWindow.return_value = 123
    home = SimpleNamespace(handle=123, control=SimpleNamespace(SetActive=Mock()))
    guard = WindowsHomeWindowGuard(user32=user32, platform_name='Windows',
                                   activation_wait_seconds=0)

    guard.activate(home, force=True)

    home.control.SetActive.assert_called_once()
    user32.BringWindowToTop.assert_called_once_with(123)
    user32.SetForegroundWindow.assert_called_once_with(123)
