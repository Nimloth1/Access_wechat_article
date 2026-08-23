from __future__ import annotations

from dataclasses import dataclass
import multiprocessing
from pathlib import Path
from typing import Any

from src.config.app_config import AppConfig
from src.services.capture.article_capture_service import ArticleCaptureService
from src.services.capture.attempt_history_service import AttemptHistoryService
from src.services.capture.capture_runtime_factory import CaptureRuntimeFactory
from src.services.capture.collected_article_lookup_service import (
    CollectedArticleLookupService,
)
from src.services.capture.comment_job_manager import CommentJobManager
from src.services.capture.html_parse_save_service import HtmlParseSaveService
from src.services.capture.single_article_capture_service import SingleCaptureSettings
from src.services.capture.window_runtime_factory import WindowRuntimeFactory
from src.services.config.config_service import ConfigService
from src.services.runtime.preflight_service import CapturePreflightService
from src.services.runtime.database_write_coordinator import DatabaseWriteCoordinator
from src.services.task.task_manager import CaptureTaskManager


@dataclass(frozen=True, slots=True)
class ApplicationRuntime:
    """应用启动后共享的只读配置和由配置装配出的运行组件。"""

    config_service: ConfigService | Any
    config: AppConfig
    window_factory: WindowRuntimeFactory
    capture_factory: CaptureRuntimeFactory
    single_capture_settings: SingleCaptureSettings
    database_write_coordinator: DatabaseWriteCoordinator


class _SingleCaptureServiceProxy:
    """延迟绑定主页 cursor，保证单篇采集服务只依赖当前任务上下文。"""

    def __init__(self, capture_factory: Any) -> None:
        self._capture_factory = capture_factory
        self._service: Any | None = None

    def bind_cursor(self, cursor: Any) -> Any:
        # cursor 必须在识别公众号主页并拿到账号名称后才能创建。
        self._service = self._capture_factory.create_single_article_service(cursor=cursor)
        return cursor

    def capture_once(self, **kwargs: Any) -> Any:
        if self._service is None:
            raise RuntimeError("单篇采集服务尚未绑定主页 cursor")
        return self._service.capture_once(**kwargs)


def load_application_runtime(
    *,
    project_root: str | Path,
    config_path: str | Path | None = None,
) -> ApplicationRuntime:
    """启动阶段合并 system.yaml 与 custom.yaml，并生成共享运行时上下文。"""
    config_service = ConfigService(
        project_root=project_root,
        config_path=config_path,
    )
    return build_application_runtime(
        config_service=config_service,
        config=config_service.current,
    )


def build_application_runtime(
    *,
    config_service: ConfigService | Any,
    config: AppConfig,
) -> ApplicationRuntime:
    """根据内存配置重建运行时对象；用于配置保存后刷新主流程上下文。"""
    window_factory = WindowRuntimeFactory(config)
    process_context = multiprocessing.get_context("spawn")
    write_coordinator = DatabaseWriteCoordinator(context=process_context)
    return ApplicationRuntime(
        config_service=config_service,
        config=config,
        window_factory=window_factory,
        capture_factory=CaptureRuntimeFactory(
            config,
            window_factory=window_factory,
        ),
        single_capture_settings=SingleCaptureSettings.from_app_config(config),
        database_write_coordinator=write_coordinator,
    )


def create_capture_task_manager(
    *,
    runtime: ApplicationRuntime | Any,
    db_path: str | Path,
    runtime_logger: Any | None = None,
) -> CaptureTaskManager:
    """装配正式文章采集主流程；只做任务编排，不直接执行底层业务细节。"""
    config = runtime.config
    window_factory = runtime.window_factory
    article_reader = window_factory.create_reader()
    single_capture = _SingleCaptureServiceProxy(runtime.capture_factory)
    write_coordinator = runtime.database_write_coordinator

    def create_cursor(home_window: Any, account_name: str) -> Any:
        cursor = window_factory.create_cursor(
            reader=article_reader,
            account_name=account_name,
        )
        return single_capture.bind_cursor(cursor)

    def restore_home_focus(_context: Any, home_window: Any | None) -> None:
        if home_window is None or not window_factory.restore_focus_after_close:
            return
        window_factory.create_home_guard().activate(home_window)

    capture_service = ArticleCaptureService(
        preflight=CapturePreflightService(
            proxy_host=config.proxy.host,
            proxy_port=config.proxy.port,
            ca_cert_path=config.proxy.ca_cert_path,
        ),
        home_finder=lambda: window_factory.find_home_window(
            reader=article_reader,
            timeout_seconds=config.window.home_find_timeout_seconds,
            use_article_probe=config.window.home_find_use_article_probe,
        ),
        home_reader=window_factory.create_home_reader(),
        cursor_factory=create_cursor,
        single_capture=single_capture,
        html_save=HtmlParseSaveService(write_coordinator=write_coordinator),
        comment_job_manager_factory=lambda: CommentJobManager(
            write_coordinator=write_coordinator,
            max_concurrent_processes=config.comment.max_concurrent_processes,
            ready_timeout_seconds=min(5.0, config.comment.request_timeout_seconds),
            result_timeout_seconds=_comment_result_timeout_seconds(config),
        ),
        collected_lookup=CollectedArticleLookupService(),
        history_factory=lambda path: AttemptHistoryService(
            database_path=path,
            write_coordinator=write_coordinator,
        ),
        cleanup=restore_home_focus,
    )
    return CaptureTaskManager(
        capture_service=capture_service,
        db_path=db_path,
        storage_root=config.storage.article_storage_root,
        temp_root=config.storage.temp_dir,
        single_capture_settings=runtime.single_capture_settings,
        request_timeout_seconds=config.request.request_timeout_seconds,
        comment_timeout_seconds=config.comment.request_timeout_seconds,
        comment_page_interval_seconds=config.comment.page_interval_seconds,
        comment_max_pages=config.comment.max_pages,
        runtime_logger=runtime_logger,
    )


def _comment_result_timeout_seconds(config: AppConfig) -> float:
    timeout = max(1.0, float(config.comment.request_timeout_seconds))
    pages = max(1, int(config.comment.max_pages))
    interval = max(0.0, float(config.comment.page_interval_seconds))
    return max(30.0, timeout * (pages + 2) + interval * pages + 15.0)


__all__ = [
    "ApplicationRuntime",
    "build_application_runtime",
    "create_capture_task_manager",
    "load_application_runtime",
]
