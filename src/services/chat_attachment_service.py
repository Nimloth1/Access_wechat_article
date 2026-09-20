# encoding: UTF-8
"""聊天附件遍历；界面操作由适配器提供，计数和范围规则不依赖微信。"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
import traceback
from typing import Any, Callable, Protocol


class ChatAutomationError(RuntimeError):
    """无法可靠继续时停止当前对话。"""


@dataclass(frozen=True)
class AttachmentOptions:
    chats: tuple[str, ...] = ()
    since: date | None = None
    max_messages: int = 100
    images: bool = True
    files: bool = True
    forwards: bool = True
    max_depth: int = 3
    max_forward_messages: int = 1000
    download_timeout: float = 30.0
    dry_run: bool = False
    compatibility: bool = False
    image_route: str = 'messages'
    previous_clicks: int = 100
    image_interval: float = 1.0

    def __post_init__(self) -> None:
        """校验边界，0 条仅在给定日期时表示不限数量。"""
        if self.max_messages < 0 or (self.max_messages == 0 and self.since is None):
            raise ValueError("扫描条数必须大于 0；按日期限制时可填 0 表示不限条数")
        if self.max_depth < 1 or self.max_forward_messages < 1:
            raise ValueError("转发层数和内部消息上限必须大于 0")
        if not 1 <= self.download_timeout <= 600:
            raise ValueError("下载等待时间必须在 1 至 600 秒之间")
        if not self.images and not self.files:
            raise ValueError("至少选择图片或文件")
        if len(set(self.chats)) != len(self.chats) or any(not x.strip() for x in self.chats):
            raise ValueError("对话名称不能为空或重复")
        if self.image_route not in {'messages', 'viewer'}:
            raise ValueError('图片路线必须为 messages 或 viewer')
        if self.previous_clicks < 0 or not 0.2 <= self.image_interval <= 60:
            raise ValueError('上一张次数不能为负，图片间隔须在 0.2 至 60 秒之间')
        if self.image_route == 'viewer' and (self.since is not None or not self.images):
            raise ValueError('连续图片路线必须选择图片并清空截止日期；该路线只处理图片')


@dataclass(frozen=True)
class ChatMessage:
    signature: str
    kind: str
    day: date | None
    control: Any = field(default=None, compare=False, repr=False)
    text: str = field(default='', compare=False)


@dataclass(frozen=True)
class ChatPage:
    messages: tuple[ChatMessage, ...]  # 按界面从上到下排列
    at_start: bool = False


class ChatAdapter(Protocol):
    """聊天窗口适配器；每次动作必须重新验证目标与归属窗口。"""

    def open_chat(self, name: str | None) -> Any: ...
    def chat_name(self, context: Any) -> str: ...
    def read_page(self, context: Any) -> ChatPage: ...
    def scroll_older(self, context: Any) -> bool: ...
    def download(self, context: Any, message: ChatMessage, timeout: float) -> str: ...
    def open_forward(self, context: Any, message: ChatMessage) -> Any: ...
    def close_forward(self, child: Any, parent: Any) -> None: ...
    def cancelled(self) -> bool: ...
    def resolve_day(self, context: Any, message: ChatMessage) -> date | None: ...
    def prepare_dates(self, context: Any) -> None: ...
    def browse_images(self, context: Any, message: ChatMessage, clicks: int,
                      interval: float, progress: Callable[[str], None]) -> str: ...


def page_identity(page: ChatPage) -> tuple:
    """页面身份不包含后续补全的日期；控件实例仍参与衔接核验。"""
    return tuple((m.signature, m.control[0] if isinstance(m.control, tuple) and len(m.control) == 2 else None)
                 for m in page.messages)


def older_prefix(previous: ChatPage, current: ChatPage) -> int:
    """定位旧页最早消息在新页中的唯一位置，核验整个共同范围后返回新增数。"""
    old = page_identity(previous)
    new = page_identity(current)
    if not old or not new:
        raise ChatAutomationError("消息列表变空，无法确认翻页位置")
    if old == new:
        raise ChatAutomationError("滚动后消息未变化，未确认已到历史起点")
    # 消息高度或视口变化时，重叠段后面还可能露出较新消息，不能强制重叠贴住新页底部。
    matches = [start for start in range(len(new))
               if new[start:start + len(old)] == old[:min(len(old), len(new) - start)]]
    if len(matches) != 1:
        raise ChatAutomationError("翻页重叠消息缺失或有歧义，已停止以免漏计或重复下载")
    return matches[0]


class ChatAttachmentService:
    """顺序扫描外层和嵌套转发；附件完成数独立于消息扫描数。"""

    def __init__(self, adapter: ChatAdapter, emit: Callable[[dict], None]) -> None:
        """接收界面适配器和逐条持久化的事件回调。"""
        self.adapter = adapter
        self.emit = emit
        self.counts: Counter = Counter()

    def _event(self, event: str, **details: Any) -> None:
        """输出不包含聊天正文的结构化事件。"""
        self.emit({"time": datetime.now().isoformat(timespec="seconds"), "event": event, **details})

    def run(self, options: AttachmentOptions) -> dict:
        """按对话顺序执行，异常保留结果，取消后不切换新对话。"""
        self.counts.clear()
        for requested in options.chats or (None,):
            if self.adapter.cancelled():
                break
            try:
                context = self.adapter.open_chat(requested)
                if options.since:
                    self.adapter.prepare_dates(context)
                name = self.adapter.chat_name(context)
                self._event("chat_started", chat=name)
                budget = [options.max_forward_messages]
                reason = self._scan(context, options, name, (), budget)
                self._event("chat_stopped", chat=name, reason=reason)
            except ChatAutomationError as exc:
                if self.adapter.cancelled():
                    self._event("cancelled", chat=requested or "当前对话")
                    break
                self.counts["errors"] += 1
                self._event("error", chat=requested or "当前对话", detail=str(exc), traceback=traceback.format_exc(limit=8))
                # 窗口归属或导航异常后，不再自动切换其他对话。
                break
        result = dict(self.counts)
        result["cancelled"] = self.adapter.cancelled()
        self._event("finished", counts=result)
        return result

    def _scan(self, context: Any, options: AttachmentOptions, chat: str,
              path: tuple[int, ...], budget: list[int]) -> str:
        """从当前位置向前扫描；内部预算由该对话全部转发共享。"""
        previous = None
        scanned = 0
        while not self.adapter.cancelled():
            page = self.adapter.read_page(context)
            if not page.messages:
                return "empty"
            if previous is not None and page.at_start and page_identity(page) == page_identity(previous):
                return "history_start"
            try:
                count = len(page.messages) if previous is None else older_prefix(previous, page)
            except ChatAutomationError:
                # 仅保存摘要与控件标识，不保存正文；下次失败能区分布局变化与身份丢失。
                self._event('pagination_error', chat=chat, path=path,
                            previous=page_identity(previous), current=page_identity(page))
                raise
            for message in reversed(page.messages[:count]):
                if self.adapter.cancelled():
                    return "cancelled"
                if not path and options.max_messages and scanned >= options.max_messages:
                    return "message_limit"
                if path and budget[0] <= 0:
                    return "forward_message_limit"
                scanned += 1
                self.counts["forward_messages" if path else "messages"] += 1
                if path:
                    budget[0] -= 1
                item_path = path + (scanned,)
                # 外层时间决定整张合并转发是否在范围内。
                if not path and options.since:
                    if message.day is None:
                        self._event('date_resolving', chat=chat, path=item_path)
                    day = message.day or self.adapter.resolve_day(context, message)
                    if self.adapter.cancelled():
                        return 'cancelled'
                    if day is None:
                        self.counts["unknown_date"] += 1
                        self._event("skipped", chat=chat, path=item_path, reason="unknown_date")
                        continue
                    if message.day is None:
                        self._event('date_resolved', chat=chat, path=item_path, day=day.isoformat())
                    if day < options.since:
                        return "date_boundary"
                if options.image_route == 'viewer':
                    if message.kind != 'image':
                        continue
                    if options.dry_run:
                        self.counts['candidate'] += 1
                        self._event('candidate', chat=chat, path=item_path, kind='image')
                        return 'viewer_candidate'
                    viewer_counts: Counter = Counter()
                    def progress(status: str) -> None:
                        """记录预览和点击次数，不推断下载完成或图片唯一性。"""
                        self.counts[status] += 1
                        viewer_counts[status] += 1
                        self._event('viewer_progress', chat=chat, status=status, count=viewer_counts[status])
                    return self.adapter.browse_images(context, message, options.previous_clicks,
                                                      options.image_interval, progress)
                if message.kind == "forward" and options.forwards:
                    if options.dry_run:
                        self._event("candidate", chat=chat, path=item_path, kind="forward")
                    elif len(path) >= options.max_depth:
                        self.counts["depth_limited"] += 1
                        self._event("skipped", chat=chat, path=item_path, reason="depth_limit")
                    elif budget[0] <= 0:
                        self._event("skipped", chat=chat, path=item_path, reason="forward_message_limit")
                    else:
                        child = self.adapter.open_forward(context, message)
                        try:
                            self._event('forward_started', chat=chat, path=item_path)
                            reason = self._scan(child, options, chat, item_path, budget)
                            self._event("forward_stopped", chat=chat, path=item_path, reason=reason)
                        finally:
                            self.adapter.close_forward(child, context)
                elif (message.kind == "image" and options.images) or (message.kind == "file" and options.files):
                    status = "candidate" if options.dry_run else self.adapter.download(context, message, options.download_timeout)
                    if status not in {"candidate", "confirmed", "available", "unconfirmed", "expired", "failed", "cancelled", "skipped_file"}:
                        raise ChatAutomationError(f"未知附件状态：{status}")
                    self.counts[status] += 1
                    self._event("attachment", chat=chat, path=item_path, kind=message.kind, status=status)
            if count:
                self._event("scan_progress", chat=chat, path=path, scanned=scanned)
            if page.at_start:
                return "history_start"
            if not path and options.max_messages and scanned >= options.max_messages:
                return "message_limit"
            if path and budget[0] <= 0:
                return "forward_message_limit"
            previous = page
            # 为点击刚露出的气泡而小幅滚动时，先处理因此新出现的消息，不能再叠加翻页。
            if page_identity(self.adapter.read_page(context)) != page_identity(page):
                continue
            if self.adapter.scroll_older(context):
                self._event('pagination_recovered', chat=chat, path=path)
        return "cancelled"
