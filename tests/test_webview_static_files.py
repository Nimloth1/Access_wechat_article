from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dev_server import mount_webview_static_files


class WebviewStaticFilesTests(unittest.TestCase):
    def test_frontend_assets_use_webview_compatible_content_types(self) -> None:
        """Windows WebView2 必须以正确 MIME 类型加载脚本和样式。"""
        with TemporaryDirectory() as directory:
            webview_dir = Path(directory)
            assets_dir = webview_dir / "assets"
            assets_dir.mkdir()
            (webview_dir / "index.html").write_text(
                '<script type="module" src="/assets/app.js"></script>',
                encoding="utf-8",
            )
            (assets_dir / "app.js").write_text("export {};", encoding="utf-8")
            (assets_dir / "app.css").write_text("body {}", encoding="utf-8")

            app = FastAPI()
            mount_webview_static_files(app, webview_dir)

            client = TestClient(app)
            script_response = client.get("/assets/app.js")
            style_response = client.get("/assets/app.css")

        self.assertEqual(script_response.status_code, 200)
        self.assertEqual(
            script_response.headers["content-type"],
            "application/javascript",
        )
        self.assertEqual(style_response.status_code, 200)
        self.assertEqual(style_response.headers["content-type"], "text/css; charset=utf-8")


if __name__ == "__main__":
    unittest.main()
