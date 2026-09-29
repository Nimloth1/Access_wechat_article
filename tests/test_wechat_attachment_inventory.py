# encoding: UTF-8
from datetime import date
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from src.services.chat_attachment_progress import ChatAttachmentProgress
from src.services.wechat_attachment_inventory import attachment_state, load_inventory


class WechatAttachmentInventoryTests(unittest.TestCase):
    def test_image_requires_original_capable_variant(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            thumb = root / 'abc_t.dat'
            thumb.write_bytes(b'thumb')
            self.assertEqual(attachment_state({
                'kind': 'image', 'resource_md5': 'abc', 'dat_paths': [str(thumb)]
            }), ('thumbnail', str(thumb)))
            original = root / 'abc_h.dat'
            original.write_bytes(b'original')
            self.assertEqual(attachment_state({
                'kind': 'image', 'resource_md5': 'abc', 'dat_paths': [str(thumb), str(original)]
            }), ('original', str(original)))

    def test_decoded_thumbnail_is_not_promoted_by_unverified_original_dat(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            thumb = root / 'abc_t.jpg'
            thumb.write_bytes(b'decoded thumbnail')
            original_dat = root / 'abc.dat'
            original_dat.write_bytes(b'undecodable original')
            self.assertEqual(attachment_state({
                'kind': 'image', 'resource_md5': 'abc', 'image_variant': 'thumb',
                'path': str(thumb), 'dat_paths': [str(original_dat)],
            }), ('thumbnail', str(thumb)))

    def test_numbered_file_copy_is_confirmed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            numbered = root / 'report(1).pdf'
            numbered.write_bytes(b'pdf')
            self.assertEqual(attachment_state({'kind': 'file', 'path': str(root / 'report.pdf')}),
                             ('confirmed', str(numbered)))

    def test_hashed_file_name_is_confirmed_by_size_and_md5(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hashed = root / '48df40ea528631dcca04f3e11d868d6a.pdf'
            hashed.write_bytes(b'actual attachment')
            attachment = {
                'kind': 'file',
                'path': str(root / 'name: invalid on Windows.pdf'),
                'size': hashed.stat().st_size,
                'md5': hashlib.md5(hashed.read_bytes()).hexdigest(),
            }
            self.assertEqual(attachment_state(attachment), ('confirmed', str(hashed)))

    def test_same_size_without_matching_md5_remains_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'candidate.pdf').write_bytes(b'wrong')
            attachment = {
                'kind': 'file',
                'path': str(root / 'missing.pdf'),
                'size': 5,
                'md5': hashlib.md5(b'right').hexdigest(),
            }
            self.assertEqual(attachment_state(attachment), ('missing', None))

    def test_inventory_uses_stable_message_keys_and_intraday_cursor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / 'file.pdf'
            media.write_bytes(b'data')
            payload = {'conversations': [{'session_id': 'room@chatroom', 'session_name': '群', 'messages': [
                {'message_id': 10, 'local_id': 20, 'timestamp': 100, 'attachment': {'kind': 'file', 'path': str(media)}},
                {'message_id': 11, 'local_id': 21, 'timestamp': 100, 'attachment': {'kind': 'file', 'path': str(root / 'missing')}},
            ]}]}
            (root / 'wechat_date_json_20260918.json').write_text(json.dumps(payload), encoding='utf-8')
            session_id, items = load_inventory(root, '群', since=date(2026, 9, 18))
            self.assertEqual([item['key'] for item in items], [
                'room@chatroom:20:100', 'room@chatroom:21:100'])
            progress = ChatAttachmentProgress(root / 'progress.json')
            counts = progress.reconcile(session_id, '群', items)
            self.assertEqual(counts, {'confirmed': 1, 'missing': 1})
            self.assertEqual(progress.data['chats'][session_id]['cursor'], {'timestamp': 100, 'local_id': 21})
            self.assertEqual([item['local_id'] for item in progress.pending(session_id)], [21])

    def test_v1_date_ranges_are_not_migrated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'progress.json'
            path.write_text(json.dumps({'version': 1, 'chats': {'群': {'completed_ranges': [
                {'start': '2026-09-18', 'end': '2026-09-18'}]}}}), encoding='utf-8')
            progress = ChatAttachmentProgress(path)
            self.assertEqual(progress.data, {'version': 2, 'chats': {}})


if __name__ == '__main__':
    unittest.main()
