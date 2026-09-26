from types import SimpleNamespace

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
