# encoding: UTF-8
"""群聊附件的消息级持久进度。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
from typing import Any, Iterable


RETRY_STATES = {"pending", "thumbnail", "missing", "failed"}


@dataclass
class ChatAttachmentProgress:
    """以微信会话 ID 和稳定消息键保存附件状态。"""

    path: Path

    def __post_init__(self) -> None:
        """读取 v2 状态；v1 日期区间不迁移，避免继续错误跳过半天消息。"""
        self.data: dict[str, Any] = {"version": 2, "chats": {}}
        if not self.path.exists():
            return
        loaded = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict) or not isinstance(loaded.get("chats"), dict):
            raise ValueError(f"附件进度文件格式无效：{self.path}")
        if loaded.get("version") == 2:
            self.data = loaded
        elif loaded.get("version") != 1:
            raise ValueError(f"附件进度文件版本不受支持：{self.path}")

    def reconcile(self, session_id: str, chat_name: str,
                  items: Iterable[dict[str, Any]]) -> dict[str, int]:
        """用当前落盘证据更新消息状态，并返回本轮状态计数。"""
        chat = self.data["chats"].setdefault(session_id, {"name": chat_name, "items": {}})
        chat["name"] = chat_name
        records = chat.setdefault("items", {})
        counts: dict[str, int] = {}
        newest: tuple[int, int] | None = None
        changed = False
        now = datetime.now().isoformat(timespec="seconds")
        for item in items:
            key = str(item["key"])
            state = str(item["state"])
            cursor = (int(item["timestamp"]), int(item["local_id"]))
            newest = max(newest or cursor, cursor)
            previous = records.get(key, {})
            value = {
                "state": state,
                "kind": item["kind"],
                "timestamp": cursor[0],
                "local_id": cursor[1],
                "message_id": item.get("message_id"),
                "evidence_path": item.get("evidence_path"),
                "updated_at": previous.get("updated_at", now),
            }
            comparable = {name: value[name] for name in value if name != "updated_at"}
            previous_comparable = {name: previous.get(name) for name in comparable}
            if previous_comparable != comparable:
                value["updated_at"] = now
                records[key] = value
                changed = True
            counts[state] = counts.get(state, 0) + 1
        cursor_value = {"timestamp": newest[0], "local_id": newest[1]} if newest else None
        if chat.get("cursor") != cursor_value:
            chat["cursor"] = cursor_value
            changed = True
        if changed:
            chat["updated_at"] = now
            self._save()
        return counts

    def pending(self, session_id: str) -> list[dict[str, Any]]:
        """返回需要首次处理或重试的附件，按时间从新到旧排列。"""
        records = self.data["chats"].get(session_id, {}).get("items", {})
        values = [dict(value, key=key) for key, value in records.items()
                  if value.get("state") in RETRY_STATES]
        return sorted(values, key=lambda value: (value["timestamp"], value["local_id"]), reverse=True)

    def _save(self) -> None:
        """使用同目录临时文件原子替换，避免中断留下半个 JSON。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.path)
