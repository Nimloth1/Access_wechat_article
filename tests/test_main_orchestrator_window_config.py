from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.app import main_orchestrator


class MainOrchestratorWindowConfigTests(unittest.TestCase):
    def test_capture_task_uses_configured_home_window_options(self) -> None:
        """正式采集任务必须使用配置中的主页查找参数。"""
        config = SimpleNamespace(
            window=SimpleNamespace(
                home_find_timeout_seconds=3.0,
            ),
            proxy=SimpleNamespace(
                host="127.0.0.1",
                port=18000,
                ca_cert_path=".mitmproxy/mitmproxy-ca-cert.cer",
            ),
            storage=SimpleNamespace(
                article_storage_root="storages",
                temp_dir="data/tmp",
            ),
            request=SimpleNamespace(request_timeout_seconds=10.0),
            comment=SimpleNamespace(
                max_concurrent_processes=3,
                request_timeout_seconds=10.0,
                page_interval_seconds=0.5,
                max_pages=5,
            ),
        )
        article_reader = object()
        window_factory = MagicMock()
        window_factory.create_reader.return_value = article_reader
        runtime = SimpleNamespace(
            config=config,
            window_factory=window_factory,
            capture_factory=MagicMock(),
            database_write_coordinator=object(),
            single_capture_settings=object(),
        )

        with (
            patch.object(main_orchestrator, "ArticleCaptureService") as capture_service,
            patch.object(main_orchestrator, "CaptureTaskManager"),
            patch.object(main_orchestrator, "CapturePreflightService"),
            patch.object(main_orchestrator, "HtmlParseSaveService"),
            patch.object(main_orchestrator, "CollectedArticleLookupService"),
        ):
            main_orchestrator.create_capture_task_manager(
                runtime=runtime,
                db_path="data/sql/awa-v2.1.sqlite3",
            )

        home_finder = capture_service.call_args.kwargs["home_finder"]
        home_finder()
        window_factory.find_home_window.assert_called_once_with(
            reader=article_reader,
            timeout_seconds=3.0,
        )


if __name__ == "__main__":
    unittest.main()
