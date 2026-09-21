# encoding: UTF-8
"""群聊附件任务的持久进度；只保存能够证明完整处理的自然日。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import json
from pathlib import Path


@dataclass
class ChatAttachmentProgress:
    """以群名和自然日保存保守检查点。"""

    path: Path

    def __post_init__(self) -> None:
        """读取已有状态；损坏文件明确报错，避免静默跳过消息。"""
        self.data = {"version": 1, "chats": {}}
        if self.path.exists():
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict) or loaded.get("version") != 1 or not isinstance(loaded.get("chats"), dict):
                raise ValueError(f"附件进度文件格式无效：{self.path}")
            self.data = loaded

    def completed(self, chat: str, day: date) -> bool:
        """返回该群该自然日是否已被完整处理。"""
        values = self.data["chats"].get(chat, {}).get("completed_dates", [])
        return day.isoformat() in values

    def attempted(self, chat: str, day: date) -> bool:
        """返回该日所有候选是否至少已执行过打开或下载动作。"""
        values = self.data["chats"].get(chat, {}).get("attempted_dates", [])
        return day.isoformat() in values

    def covers(self, chat: str, day: date, since: date | None, level: str) -> bool:
        """仅当检查点连续覆盖到本次截止日时，才允许提前结束扫描。"""
        if since is None:
            return False
        key = "completed_ranges" if level == "completed" else "attempted_ranges"
        for item in self.data["chats"].get(chat, {}).get(key, []):
            if item["start"] <= since.isoformat() <= day.isoformat() <= item["end"]:
                return True
        return False

    def mark_range(self, chat: str, start: date, end: date, level: str) -> bool:
        """保存一次完整遍历得到的连续日期范围。"""
        if end >= date.today() or end < start:
            return False
        key = "completed_ranges" if level == "completed" else "attempted_ranges"
        record = self.data["chats"].setdefault(chat, {})
        ranges = record.setdefault(key, [])
        value = {"start": start.isoformat(), "end": end.isoformat()}
        if value in ranges:
            return False
        ranges.append(value)
        ranges.sort(key=lambda item: (item["start"], item["end"]))
        record["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self._save()
        return True

    def mark_completed(self, chat: str, day: date) -> bool:
        """写入一个完整自然日，返回本次是否新增。"""
        if day >= date.today():
            return False
        record = self.data["chats"].setdefault(chat, {"completed_dates": [], "attempted_dates": []})
        values = set(record.get("completed_dates", []))
        before = len(values)
        values.add(day.isoformat())
        record["completed_dates"] = sorted(values)
        record["updated_at"] = datetime.now().isoformat(timespec="seconds")
        if len(values) == before:
            return False
        self._save()
        return True

    def mark_attempted(self, chat: str, day: date) -> bool:
        """记录整日已尝试；它不代表微信已给出文件落盘证明。"""
        if day >= date.today():
            return False
        record = self.data["chats"].setdefault(chat, {"completed_dates": [], "attempted_dates": []})
        values = set(record.get("attempted_dates", []))
        before = len(values)
        values.add(day.isoformat())
        record["attempted_dates"] = sorted(values)
        record["updated_at"] = datetime.now().isoformat(timespec="seconds")
        if len(values) == before:
            return False
        self._save()
        return True

    def _save(self) -> None:
        """使用同目录临时文件原子替换，避免中断留下半个 JSON。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.path)
