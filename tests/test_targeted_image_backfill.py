# encoding: UTF-8
"""核验日期入口的定位计算与已过期图片续跑记录。"""

from datetime import date
import json
from unittest.mock import patch

from targeted_image_backfill import calendar_day_point, expired_attempts, record_attempt


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
