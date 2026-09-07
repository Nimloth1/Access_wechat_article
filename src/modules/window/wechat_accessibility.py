# encoding: UTF-8
"""定位微信 Qt 无障碍运行时标志；仅允许已校验版本的临时兼容读取。"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import struct


# 只允许本机只读核验过的映像；版本变化必须重新核验，不凭相近模式写入。
VERIFIED_IMAGE = '7ad9753d11c2baf5c900aac50ddf56a8170aa85c46129d661325ff88505befb1'
VERIFIED_RVA = 0x0AB5F198


@dataclass(frozen=True)
class Section:
    name: str
    address: int
    size: int
    offset: int
    raw_size: int
    flags: int


def pe_sections(data: bytes) -> list[Section]:
    """校验 x64 PE 并读取节表，异常二进制不作兼容猜测。"""
    if len(data) < 64 or data[:2] != b'MZ':
        raise ValueError('不是有效 PE 文件')
    pe = struct.unpack_from('<I', data, 60)[0]
    if pe + 24 > len(data) or data[pe:pe + 4] != b'PE\0\0':
        raise ValueError('PE 头缺失')
    machine, count = struct.unpack_from('<HH', data, pe + 4)
    optional_size = struct.unpack_from('<H', data, pe + 20)[0]
    table = pe + 24 + optional_size
    if machine != 0x8664 or not 1 <= count <= 96 or table + count * 40 > len(data):
        raise ValueError('仅支持有效的 x64 PE 节表')
    result = []
    for index in range(count):
        start = table + index * 40
        name = data[start:start + 8].split(b'\0')[0].decode('ascii', errors='replace')
        virtual_size, address, raw_size, offset = struct.unpack_from('<IIII', data, start + 8)
        flags = struct.unpack_from('<I', data, start + 36)[0]
        if offset + raw_size > len(data):
            raise ValueError('PE 节越界')
        result.append(Section(name, address, max(virtual_size, raw_size), offset, raw_size, flags))
    return result


def find_accessibility_candidates(data: bytes) -> list[dict]:
    """通过 Qt 日志字符串引用和邻近条件分支定位数据位候选，仅返回证据。"""
    sections = pe_sections(data)
    string_rvas = set()
    for section in sections:
        raw = data[section.offset:section.offset + section.raw_size]
        for match in re.finditer(rb'qt\.accessibility\.core\x00', raw):
            string_rvas.add(section.address + match.start())
    if not string_rvas:
        return []
    xrefs = []
    code_sections = [section for section in sections if section.flags & 0x20000000]
    for section in code_sections:
        raw = data[section.offset:section.offset + section.raw_size]
        # x64 LEA reg,[RIP+disp32]：仅检查指向已确认 Qt 字符串的引用。
        for match in re.finditer(rb'[\x40-\x4f]\x8d[\x05\x0d\x15\x1d\x25\x2d\x35\x3d].{4}', raw, re.DOTALL):
            address = section.address + match.start()
            target = address + 7 + struct.unpack_from('<i', match.group(), 3)[0]
            if target in string_rvas:
                xrefs.append(address)
    if not xrefs:
        return []
    candidates = {}
    pattern = rb'\x48\x85\xc9\x0f\x84.{4}\x80\x3d(.{4})\x00\x0f\x84'
    for section in code_sections:
        raw = data[section.offset:section.offset + section.raw_size]
        for match in re.finditer(pattern, raw, re.DOTALL):
            instruction = section.address + match.start()
            target = instruction + 16 + struct.unpack('<i', match.group(1))[0]
            distance = min(abs(instruction - address) for address in xrefs)
            target_section = next((s for s in sections if s.address <= target < s.address + s.size), None)
            if distance > 0x20000 or target_section is None:
                continue
            if not target_section.flags & 0x80000000 or target_section.flags & 0x20000000:
                continue
            evidence = {'rva': target, 'instruction_rva': instruction, 'distance': distance,
                        'section': target_section.name, 'instruction_hex': match.group().hex()}
            if target not in candidates or distance < candidates[target]['distance']:
                candidates[target] = evidence
    return sorted(candidates.values(), key=lambda value: value['distance'])


class ModuleEntry(ctypes.Structure):
    """Windows MODULEENTRY32W ABI。"""
    _fields_ = [('size', wintypes.DWORD), ('module_id', wintypes.DWORD), ('pid', wintypes.DWORD),
                ('global_usage', wintypes.DWORD), ('process_usage', wintypes.DWORD),
                ('base', ctypes.c_void_p), ('image_size', wintypes.DWORD), ('module', wintypes.HMODULE),
                ('name', wintypes.WCHAR * 256), ('path', wintypes.WCHAR * 260)]


def weixin_module(pid: int) -> tuple[int, int, Path]:
    """只枚举指定进程的模块，返回唯一的 Weixin.dll。"""
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Module32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ModuleEntry)]
    kernel.Module32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ModuleEntry)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel.CreateToolhelp32Snapshot(0x18, pid)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    matches = []
    try:
        entry = ModuleEntry()
        entry.size = ctypes.sizeof(entry)
        valid = kernel.Module32FirstW(snapshot, ctypes.byref(entry))
        while valid:
            if entry.name.lower() == 'weixin.dll':
                matches.append((entry.base, entry.image_size, Path(entry.path)))
            valid = kernel.Module32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    if len(matches) != 1:
        raise ValueError(f'进程中找到 {len(matches)} 个 Weixin.dll，无法唯一定位')
    return matches[0]


def inspect_accessibility(pid: int) -> dict:
    """只读检查磁盘映像与对应候选字节，不修改任何状态。"""
    base, image_size, path = weixin_module(pid)
    data = path.read_bytes()
    candidates = find_accessibility_candidates(data)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                        ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1010, False, pid)  # 查询信息和读取权限
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        for candidate in candidates:
            if not 0 <= candidate['rva'] < image_size:
                raise ValueError('候选地址超出加载映像')
            value = ctypes.c_ubyte()
            received = ctypes.c_size_t()
            ok = kernel.ReadProcessMemory(handle, base + candidate['rva'], ctypes.byref(value), 1, ctypes.byref(received))
            if not ok or received.value != 1:
                raise ctypes.WinError(ctypes.get_last_error())
            candidate['value'] = value.value
            candidate['rva_hex'] = hex(candidate['rva'])
    finally:
        kernel.CloseHandle(handle)
    return {'pid': pid, 'dll_path': str(path), 'sha256': hashlib.sha256(data).hexdigest(),
            'candidates': candidates, 'read_only': True}


def validate_gate(report: dict) -> dict:
    """写入前校验版本、唯一候选和旧值；不采用任意版本的最近距离候选。"""
    if report['sha256'] != VERIFIED_IMAGE:
        raise ValueError('当前微信映像尚未核验，不能启用兼容读取')
    candidates = report['candidates']
    if len(candidates) != 1 or candidates[0]['rva'] != VERIFIED_RVA:
        raise ValueError('无障碍标志不能唯一匹配已核验地址')
    gate = candidates[0]
    if gate['value'] not in (0, 1):
        raise ValueError('无障碍标志不是预期布尔值，未修改')
    return gate


class _ProcessMemory:
    """持有同一个进程句柄，限定字节读写并在退出时关闭。"""

    def __init__(self, pid: int) -> None:
        """按指定进程打开查询与内存权限，不注入或执行代码。"""
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        parameters = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                      ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        self.kernel.ReadProcessMemory.argtypes = parameters
        self.kernel.WriteProcessMemory.argtypes = parameters
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.OpenProcess(0x1038, False, pid)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())

    def read(self, address: int, size: int = 1) -> bytes:
        """只读取校验需要的少量字节。"""
        if not 1 <= size <= 32:
            raise ValueError('读取长度超出校验范围')
        buffer = ctypes.create_string_buffer(size)
        received = ctypes.c_size_t()
        ok = self.kernel.ReadProcessMemory(self.handle, address, buffer, size, ctypes.byref(received))
        if not ok or received.value != size:
            raise ctypes.WinError(ctypes.get_last_error())
        return buffer.raw

    def write_flag(self, address: int, expected: int, value: int) -> None:
        """比较旧值后只写一个布尔字节；调用端登记恢复责任后再读回。"""
        if expected not in (0, 1) or value not in (0, 1):
            raise ValueError('只允许布尔标志')
        if self.read(address) != bytes([expected]):
            raise RuntimeError('微信标志已被其他操作改变，停止修改')
        written = ctypes.c_size_t()
        buffer = ctypes.c_ubyte(value)
        ok = self.kernel.WriteProcessMemory(self.handle, address, ctypes.byref(buffer), 1, ctypes.byref(written))
        if not ok or written.value != 1:
            raise RuntimeError('无障碍标志写入失败')

    def close(self) -> None:
        """释放进程句柄。"""
        self.kernel.CloseHandle(self.handle)


@contextmanager
def temporary_accessibility(pid: int):
    """仅在上下文内启用已核验版本的读屏位，正常或异常退出均恢复旧值。"""
    memory = _ProcessMemory(pid)
    changed = False
    address = None
    try:
        report = inspect_accessibility(pid)
        gate = validate_gate(report)
        base, _, path = weixin_module(pid)
        if str(path) != report['dll_path']:
            raise RuntimeError('校验期间微信模块改变')
        instruction = bytes.fromhex(gate['instruction_hex'])
        if memory.read(base + gate['instruction_rva'], len(instruction)) != instruction:
            raise RuntimeError('内存代码与已校验映像不一致，未修改')
        address = base + gate['rva']
        if gate['value'] == 0:
            memory.write_flag(address, 0, 1)
            changed = True
            if memory.read(address) != b'\x01':
                raise RuntimeError('无障碍标志读回校验失败')
        yield {'pid': pid, 'rva': gate['rva_hex'], 'changed': changed}
    finally:
        try:
            if changed and address is not None:
                current = memory.read(address)[0]
                if current == 1:
                    memory.write_flag(address, 1, 0)
                    if memory.read(address) != b'\x00':
                        raise RuntimeError('未确认无障碍标志恢复成功')
                elif current != 0:
                    raise RuntimeError('退出时无障碍标志已改变，未覆盖其他状态')
        finally:
            memory.close()
