# encoding: UTF-8
# version: 1.0 @20261014: 按原始 HWND 记录遮挡窗口、输入桌面和会话状态;
"""仅在操作失败时读取 Win32 状态，不激活或修改任何窗口。"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from datetime import datetime
import json


__version__ = 'v1.0'
__platform__ = 'nt'
__pc__ = ['PW']
__py__ = 'py3.13+'


class _SessionData(ctypes.Structure):
    """WTSINFOEX_LEVEL1_W 的完整布局，避免锁屏标志的偏移错误。"""
    _fields_ = [
        ('session_id', wintypes.DWORD), ('state', ctypes.c_int), ('flags', wintypes.LONG),
        ('station', ctypes.c_wchar * 33), ('user', ctypes.c_wchar * 21),
        ('domain', ctypes.c_wchar * 18),
        *[(name, ctypes.c_longlong) for name in
          ('logon', 'connect', 'disconnect', 'last_input', 'current')],
        *[(name, wintypes.DWORD) for name in
          ('incoming_bytes', 'outgoing_bytes', 'incoming_frames', 'outgoing_frames',
           'incoming_compressed', 'outgoing_compressed')],
    ]


class _SessionInfo(ctypes.Structure):
    """WTSINFOEXW 的 level 与对齐后的 data。"""
    _fields_ = [('level', wintypes.DWORD), ('data', _SessionData)]


class _Reader:
    """绑定有指针宽度的只读 API，并保留各字段的读取失败原因。"""

    def __init__(self):
        """创建独立 DLL 包装器，不改变现有自动化代码的 API 签名。"""
        self.errors = {}
        self.user32 = ctypes.WinDLL('user32', use_last_error=True)
        self.kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        self.wts = ctypes.WinDLL('wtsapi32', use_last_error=True)
        h, d, b, p = wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, ctypes.c_void_p
        signatures = {
            self.user32: {
                'IsWindow': ([h], b), 'IsWindowVisible': ([h], b), 'IsIconic': ([h], b),
                'GetForegroundWindow': ([], h), 'WindowFromPoint': ([wintypes.POINT], h),
                'GetWindowThreadProcessId': ([h, ctypes.POINTER(d)], d),
                'GetWindowTextW': ([h, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
                'GetClassNameW': ([h, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
                'GetWindowRect': ([h, ctypes.POINTER(wintypes.RECT)], b),
                'GetWindowLongW': ([h, ctypes.c_int], wintypes.LONG),
                'GetAncestor': ([h, wintypes.UINT], h), 'GetWindow': ([h, wintypes.UINT], h),
                'GetThreadDesktop': ([d], h), 'OpenInputDesktop': ([d, b, d], h),
                'CloseDesktop': ([h], b),
                'GetUserObjectInformationW': ([h, ctypes.c_int, p, d, ctypes.POINTER(d)], b),
                'GetDpiForWindow': ([h], wintypes.UINT),
                'GetThreadDpiAwarenessContext': ([], h),
                'GetAwarenessFromDpiAwarenessContext': ([h], ctypes.c_int),
                'GetSystemMetrics': ([ctypes.c_int], ctypes.c_int),
            },
            self.kernel32: {
                'GetCurrentProcessId': ([], d), 'GetCurrentThreadId': ([], d),
                'ProcessIdToSessionId': ([d, ctypes.POINTER(d)], b),
            },
            self.wts: {
                'WTSQuerySessionInformationW': ([h, d, ctypes.c_int, ctypes.POINTER(p),
                                                ctypes.POINTER(d)], b),
                'WTSFreeMemory': ([p], None),
            },
        }
        for library, functions in signatures.items():
            for name, (args, result) in functions.items():
                function = getattr(library, name)
                function.argtypes, function.restype = args, result

    def read(self, name, callback):
        """读取一个字段；失败返回 null 并保留异常，而非伪造正常状态。"""
        try:
            return callback()
        except Exception as exc:
            self.errors[name] = f'{type(exc).__name__}: {exc}'
            return None

    def require(self, result):
        """将 Win32 的失败返回值转为带错误码的诊断异常。"""
        if not result:
            raise ctypes.WinError(ctypes.get_last_error())
        return result

    def text(self, function, handle):
        """读取标题或类名；空标题保留为空，不因此丢弃窗口。"""
        buffer = ctypes.create_unicode_buffer(1024)
        ctypes.set_last_error(0)
        if not function(handle, buffer, len(buffer)) and ctypes.get_last_error():
            raise ctypes.WinError(ctypes.get_last_error())
        return buffer.value

    def desktop_name(self, handle):
        """读取已有桌面句柄的名字，不切换桌面。"""
        self.require(handle)
        buffer, needed = ctypes.create_unicode_buffer(512), wintypes.DWORD()
        self.require(self.user32.GetUserObjectInformationW(
            handle, 2, buffer, ctypes.sizeof(buffer), ctypes.byref(needed)))
        return buffer.value

    def process(self, pid):
        """记录进程身份与启动时间；权限不足时明确记录错误。"""
        result = {}
        try:
            import psutil
            process = psutil.Process(pid)
        except Exception as exc:
            self.errors[f'process.{pid}'] = f'{type(exc).__name__}: {exc}'
            return result
        for name, callback in (('name', process.name), ('path', process.exe),
                               ('started_at', lambda: datetime.fromtimestamp(
                                   process.create_time()).astimezone().isoformat())):
            result[name] = self.read(f'process.{pid}.{name}', callback)
        return result

    def window(self, handle):
        """直接读取任意 HWND，包括未枚举到的子窗口和无标题窗口。"""
        result = {'handle': handle, 'valid': bool(self.user32.IsWindow(handle))}
        if not result['valid']:
            return result
        u, pid = self.user32, wintypes.DWORD()
        thread = self.read(f'{handle}.thread_id', lambda: self.require(
            u.GetWindowThreadProcessId(handle, ctypes.byref(pid))))
        result.update(thread_id=thread, pid=pid.value)
        result['process'] = self.process(pid.value) if pid.value else {}

        def rect():
            """返回与当前 DPI 上下文一致的屏幕矩形。"""
            value = wintypes.RECT()
            self.require(u.GetWindowRect(handle, ctypes.byref(value)))
            return [value.left, value.top, value.right, value.bottom]

        def exstyle():
            """读取扩展样式，零值与读取失败分别处理。"""
            ctypes.set_last_error(0)
            value = u.GetWindowLongW(handle, -20)
            if not value and ctypes.get_last_error():
                raise ctypes.WinError(ctypes.get_last_error())
            return value & 0xffffffff

        def session_id():
            """确认窗口进程所属会话，便于与采集进程的会话对照。"""
            value = wintypes.DWORD()
            self.require(self.kernel32.ProcessIdToSessionId(pid.value, ctypes.byref(value)))
            return value.value

        fields = {
            'title': lambda: self.text(u.GetWindowTextW, handle),
            'class_name': lambda: self.text(u.GetClassNameW, handle), 'rect': rect,
            'visible': lambda: bool(u.IsWindowVisible(handle)),
            'minimized': lambda: bool(u.IsIconic(handle)), 'exstyle': exstyle,
            'root': lambda: int(u.GetAncestor(handle, 2) or 0),
            'owner': lambda: int(u.GetWindow(handle, 4) or 0),
            'session_id': session_id,
            'desktop': lambda: self.desktop_name(u.GetThreadDesktop(thread)) if thread else None,
            'dpi': lambda: self.require(u.GetDpiForWindow(handle)),
        }
        for name, callback in fields.items():
            result[name] = self.read(f'{handle}.{name}', callback)
        style = result['exstyle']
        for name, mask in (('topmost', 0x8), ('toolwindow', 0x80), ('noactivate', 0x08000000)):
            result[name] = bool(style & mask) if style is not None else None
        return result

    def input_desktop(self):
        """打开输入桌面做只读查询，始终释放自有句柄。"""
        desktop = self.require(self.user32.OpenInputDesktop(0, False, 0x1))
        try:
            return self.desktop_name(desktop)
        finally:
            self.read('CloseDesktop', lambda: self.require(self.user32.CloseDesktop(desktop)))

    def session(self):
        """记录当前进程会话的连接及锁屏状态，未知标志保留为 null。"""
        session_id, size, buffer = wintypes.DWORD(), wintypes.DWORD(), ctypes.c_void_p()
        self.require(self.kernel32.ProcessIdToSessionId(
            self.kernel32.GetCurrentProcessId(), ctypes.byref(session_id)))
        result = {'id': session_id.value}

        def query():
            """解析 WTSINFOEXW 并释放 WTS 分配的内存。"""
            self.require(self.wts.WTSQuerySessionInformationW(
                None, session_id.value, 25, ctypes.byref(buffer), ctypes.byref(size)))
            try:
                if not buffer.value or size.value < ctypes.sizeof(_SessionInfo):
                    raise ValueError(f'WTSINFOEXW 长度不足: {size.value}')
                info = ctypes.cast(buffer, ctypes.POINTER(_SessionInfo)).contents
                if info.level != 1:
                    raise ValueError(f'WTSINFOEXW level={info.level}')
                return {'state': info.data.state, 'flags': info.data.flags,
                        'locked': {0: True, 1: False}.get(info.data.flags),
                        'station': info.data.station}
            finally:
                self.wts.WTSFreeMemory(buffer)

        result['info'] = self.read('WTSQuerySessionInformationW', query)
        return result


def collect_window_diagnostic(home_handle: int, *, blocker_handle: int = 0,
                              point: tuple[int, int] | None = None, phase: str) -> dict:
    """返回失败现场的结构化快照，字段失败原因一并保留。"""
    reader = _Reader()
    u, k = reader.user32, reader.kernel32
    foreground = reader.read('GetForegroundWindow', lambda: int(u.GetForegroundWindow() or 0))
    hit = reader.read('WindowFromPoint', lambda: int(u.WindowFromPoint(
        wintypes.POINT(*point)) or 0)) if point is not None else None
    windows = {}
    roles = {'blocker': blocker_handle, 'target': home_handle, 'foreground': foreground,
             'point_owner_now': hit}
    # 记录第一次命中的 HWND，以及采样时的命中；避免把短暂遮挡的后续窗口误作原窗口。
    for handle in list(roles.values()):
        if handle and str(handle) not in windows:
            window = reader.read(f'window.{handle}', lambda h=handle: reader.window(h))
            windows[str(handle)] = window
            for related in ('root', 'owner'):
                related_handle = (window or {}).get(related)
                if related_handle and str(related_handle) not in windows:
                    windows[str(related_handle)] = reader.read(
                        f'window.{related_handle}', lambda h=related_handle: reader.window(h))
    desktop = {
        'input': reader.read('input_desktop', reader.input_desktop),
        'thread': reader.read('thread_desktop', lambda: reader.desktop_name(
            u.GetThreadDesktop(k.GetCurrentThreadId()))),
        'session': reader.read('session', reader.session),
        'dpi_awareness': reader.read('dpi_awareness', lambda:
            u.GetAwarenessFromDpiAwarenessContext(u.GetThreadDpiAwarenessContext())),
        'virtual_screen': [u.GetSystemMetrics(i) for i in (76, 77, 78, 79)],
    }
    return {'time': datetime.now().astimezone().isoformat(), 'phase': phase,
            'point': point, 'roles': roles, 'windows': windows, 'desktop': desktop,
            'errors': reader.errors}


def record_window_diagnostic(home_handle: int, *, blocker_handle: int = 0,
                             point: tuple[int, int] | None = None, phase: str,
                             failure: dict | None = None) -> dict:
    """向任务标准输出写一行 JSON；诊断失败不替换原业务异常。"""
    try:
        snapshot = collect_window_diagnostic(home_handle, blocker_handle=blocker_handle,
                                             point=point, phase=phase)
    except Exception as exc:
        snapshot = {'time': datetime.now().astimezone().isoformat(), 'phase': phase,
                    'point': point, 'target': home_handle, 'blocker': blocker_handle,
                    'diagnostic_error': f'{type(exc).__name__}: {exc}'}
    if failure is not None:
        snapshot['failure'] = failure
    try:
        # ASCII 转义保留所有标题字符，也适用于非 UTF-8 的任务标准输出。
        print('[window-diagnostic] ' + json.dumps(snapshot, ensure_ascii=True), flush=True)
    except Exception:
        pass  # 输出故障不能覆盖原来的操作停止原因。
    return snapshot
