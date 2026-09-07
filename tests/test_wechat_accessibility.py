# encoding: UTF-8
"""校验无障碍兼容读取的版本边界和恢复行为，不修改真实进程。"""
from pathlib import Path
import struct
import unittest
from unittest.mock import patch

from src.modules.window import wechat_accessibility as module


def sample_pe():
    """创建只包含 Qt 字符串引用和数据位条件分支的最小 x64 PE。"""
    data = bytearray(0xA00)
    data[:2] = b'MZ'
    struct.pack_into('<I', data, 60, 0x80)
    data[0x80:0x84] = b'PE\0\0'
    struct.pack_into('<HH', data, 0x84, 0x8664, 3)
    struct.pack_into('<H', data, 0x94, 0xF0)
    for i, (name, va, raw, size, flags) in enumerate([
        (b'.text', 0x1000, 0x400, 0x200, 0x60000020),
        (b'.rdata', 0x2000, 0x600, 0x200, 0x40000040),
        (b'.data', 0x3000, 0x800, 0x200, 0xC0000040),
    ]):
        offset = 0x188 + i * 40
        data[offset:offset + len(name)] = name
        struct.pack_into('<IIII', data, offset + 8, size, va, size, raw)
        struct.pack_into('<I', data, offset + 36, flags)
    label = b'qt.accessibility.core\0'
    data[0x600:0x600 + len(label)] = label
    data[0x400:0x407] = b'\x48\x8d\x0d' + struct.pack('<i', 0x2000 - 0x1007)
    instruction = b'\x48\x85\xc9\x0f\x84\0\0\0\0\x80\x3d' + struct.pack('<i', 0x3000 - 0x1050) + b'\x00\x0f\x84'
    data[0x440:0x440 + len(instruction)] = instruction
    return bytes(data)


class AccessibilityScanTests(unittest.TestCase):
    def test_unique_candidate_is_writable_nonexecutable_data(self):
        """只接受有 Qt 字符串引用佐证的数据节布尔位。"""
        candidates = module.find_accessibility_candidates(sample_pe())
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]['rva'], 0x3000)

    def test_no_string_reference_means_no_candidate(self):
        """不能在缺少 Qt 证据时退化为模式盲匹配。"""
        data = bytearray(sample_pe())
        data[0x400:0x407] = b'\0' * 7
        self.assertEqual(module.find_accessibility_candidates(bytes(data)), [])

    def test_executable_target_rejected(self):
        """即使模式匹配也不允许指向代码节。"""
        data = bytearray(sample_pe())
        struct.pack_into('<I', data, 0x188 + 2 * 40 + 36, 0xE0000040)
        self.assertEqual(module.find_accessibility_candidates(bytes(data)), [])

    def test_malformed_pe_rejected(self):
        """损坏的二进制不作猜测。"""
        with self.assertRaises(ValueError):
            module.find_accessibility_candidates(b'not a PE')

    def test_unknown_image_multiple_matches_and_nonboolean_rejected(self):
        """严格的映像固定、唯一性和旧值检查。"""
        report = {'sha256': module.VERIFIED_IMAGE, 'candidates': [{'rva': module.VERIFIED_RVA, 'value': 0}]}
        for invalid in (
            {**report, 'sha256': 'other'},
            {**report, 'candidates': report['candidates'] * 2},
            {**report, 'candidates': [{'rva': module.VERIFIED_RVA, 'value': 2}]},
        ):
            with self.subTest(report=invalid), self.assertRaises(ValueError):
                module.validate_gate(invalid)


class Memory:
    """模拟单字节标志和代码校验，不调用操作系统。"""
    def __init__(self, value=0):
        """设置初始标志。"""
        self.value = value
        self.writes = []
        self.closed = False

    def read(self, address, size=1):
        """返回指定校验代码或当前标志。"""
        return b'\xaa' if address == 0x1010 else bytes([self.value])

    def write_flag(self, address, expected, value):
        """模拟比较后写入。"""
        if self.value != expected:
            raise RuntimeError('value changed')
        self.value = value
        self.writes.append(value)

    def close(self):
        """记录句柄已关闭。"""
        self.closed = True


class AccessibilityRestoreTests(unittest.TestCase):
    def run_context(self, value, fail):
        """构建已核验候选并测试上下文的完整生命周期。"""
        memory = Memory(value)
        report = {'sha256': module.VERIFIED_IMAGE, 'dll_path': 'Weixin.dll', 'candidates': [{
            'rva': module.VERIFIED_RVA, 'rva_hex': hex(module.VERIFIED_RVA), 'value': value,
            'instruction_rva': 0x10, 'instruction_hex': 'aa'}]}
        with patch.object(module, '_ProcessMemory', return_value=memory), \
             patch.object(module, 'inspect_accessibility', return_value=report), \
             patch.object(module, 'weixin_module', return_value=(0x1000, 0xFFFFFFF, Path('Weixin.dll'))):
            try:
                with module.temporary_accessibility(123):
                    self.assertEqual(memory.value, 1)
                    if fail:
                        raise RuntimeError('task failed')
            except RuntimeError:
                if not fail:
                    raise
        self.assertEqual(memory.value, value)
        self.assertTrue(memory.closed)
        return memory.writes

    def test_success_restores_original_byte(self):
        """正常结束恢复 0。"""
        self.assertEqual(self.run_context(0, False), [1, 0])

    def test_exception_restores_original_byte(self):
        """异常结束也恢复 0。"""
        self.assertEqual(self.run_context(0, True), [1, 0])

    def test_existing_accessibility_left_unchanged(self):
        """原先已启用时不负责将它关闭。"""
        self.assertEqual(self.run_context(1, False), [])


if __name__ == '__main__':
    unittest.main()
