# encoding: UTF-8
"""从 wechat_self 日导出中建立可核验的群聊附件清单。"""
from __future__ import annotations

from datetime import date
import json
from pathlib import Path
from typing import Any, Iterable


ORIGINAL_VARIANTS = {"original", "original_w", "hd"}
THUMB_VARIANTS = {"thumb", "thumb_w"}


def _exists(value: Any) -> bool:
    """检查导出路径；非法 Windows 路径视为未落盘而不是让任务崩溃。"""
    if not isinstance(value, str) or not value:
        return False
    try:
        return Path(value).is_file()
    except OSError:
        return False


def _variant(path: str, resource_md5: str) -> str | None:
    """从 dat 文件名识别微信图片版本。"""
    stem = Path(path).stem
    if stem == resource_md5:
        return "original"
    return {f"{resource_md5}_W": "original_w", f"{resource_md5}_h": "hd",
            f"{resource_md5}_t": "thumb", f"{resource_md5}_t_W": "thumb_w"}.get(stem)


def attachment_state(attachment: dict[str, Any]) -> tuple[str, str | None]:
    """返回附件状态及证明路径；缩略图明确保持为待补原图。"""
    kind = attachment.get("kind")
    if kind == "file":
        path = attachment.get("path")
        return ("confirmed", path) if _exists(path) else ("missing", None)
    if kind != "image":
        return "pending", None
    resource_md5 = str(attachment.get("resource_md5") or "")
    candidates: list[tuple[str, str]] = []
    for path in attachment.get("dat_paths") or []:
        if isinstance(path, str) and _exists(path):
            variant = _variant(path, resource_md5)
            if variant:
                candidates.append((variant, path))
    direct_path = attachment.get("dat_path")
    direct_variant = attachment.get("image_variant")
    if isinstance(direct_path, str) and isinstance(direct_variant, str) and _exists(direct_path):
        candidates.append((direct_variant, direct_path))
    # 导出生成后，微信仍可能补下原图；按同一 resource_md5 检查固定兄弟文件，
    # 不要求先刷新 JSON 才能证明新文件已落盘。
    known_paths = [path for path in (attachment.get("dat_paths") or []) if isinstance(path, str)]
    if isinstance(direct_path, str):
        known_paths.append(direct_path)
    if resource_md5 and known_paths:
        parent = Path(known_paths[0]).parent
        for suffix, variant in (("", "original"), ("_W", "original_w"), ("_h", "hd"),
                                ("_t", "thumb"), ("_t_W", "thumb_w")):
            candidate = parent / f"{resource_md5}{suffix}.dat"
            if _exists(str(candidate)):
                candidates.append((variant, str(candidate)))
    for variant, path in candidates:
        if variant in ORIGINAL_VARIANTS:
            return "original", path
    for variant, path in candidates:
        if variant in THUMB_VARIANTS:
            return "thumbnail", path
    return "missing", None


def load_inventory(export_root: Path, chat_name: str, since: date | None = None,
                   until: date | None = None) -> tuple[str, list[dict[str, Any]]]:
    """读取日期范围内指定群的图片和文件，以稳定数据库键返回。"""
    if not export_root.is_dir():
        raise ValueError(f"wechat_self 日导出目录不存在：{export_root}")
    session_id: str | None = None
    result: list[dict[str, Any]] = []
    for source in sorted(export_root.glob("wechat_date_json_*.json")):
        raw = source.stem.removeprefix("wechat_date_json_")
        if len(raw) != 8 or not raw.isdigit():
            continue
        day = date(int(raw[:4]), int(raw[4:6]), int(raw[6:]))
        if (since and day < since) or (until and day > until):
            continue
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
        conversations: Iterable[dict[str, Any]] = payload.get("conversations") or []
        for conversation in conversations:
            if conversation.get("session_name") != chat_name:
                continue
            current_id = str(conversation.get("session_id") or "")
            if session_id and current_id != session_id:
                raise ValueError(f"群名 {chat_name} 对应多个会话 ID，拒绝合并")
            session_id = current_id
            for message in conversation.get("messages") or []:
                attachment = message.get("attachment") or {}
                if attachment.get("kind") not in {"image", "file"}:
                    continue
                state, evidence = attachment_state(attachment)
                timestamp = int(message["timestamp"])
                local_id = int(message["local_id"])
                result.append({
                    "key": f"{current_id}:{local_id}:{timestamp}",
                    "session_id": current_id,
                    "kind": attachment["kind"],
                    "state": state,
                    "timestamp": timestamp,
                    "local_id": local_id,
                    "message_id": message.get("message_id"),
                    "evidence_path": evidence,
                })
    if not session_id:
        raise ValueError(f"日导出中没有找到群聊：{chat_name}")
    unique = {item["key"]: item for item in result}
    return session_id, sorted(unique.values(), key=lambda item: (item["timestamp"], item["local_id"]))
