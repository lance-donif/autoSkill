"""An upgraded dashboard must bypass old HTML, styles and transitive module cache entries."""
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urljoin

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))
from fastapi.testclient import TestClient

from autoskill import paths
from autoskill.server.app import create_app


class DashboardCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ui = self.root / "app/ui"
        shutil.copytree(paths.APP / "ui", self.ui)
        self.environ = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.root / "workspace")})
        self.environ.start()
        self.addCleanup(self.environ.stop)

    def client(self):
        with patch.object(paths, "APP", self.ui.parent):
            app = create_app(start_worker=False)
        client = TestClient(app, base_url="http://127.0.0.1:8787")
        self.addCleanup(client.close)
        return client

    def test_every_entry_and_relative_module_uses_the_same_content_revision(self):
        client = self.client()
        revision = client.get("/api/status").json()["ui_revision"]
        entry = client.get("/?embed=1&frame=activation")
        urls = re.findall(r'(?:href|src)="(/_ui/[^\"]+)"', entry.text)
        self.assertEqual(len(urls), 5)
        for url in urls:
            self.assertTrue(url.startswith(f"/_ui/{revision}/"))
            asset = client.get(url)
            self.assertEqual(asset.status_code, 200)
            self.assertEqual(asset.headers["cache-control"], "no-store")
        main_url = next(url for url in urls if url.endswith("/main.js"))
        for module in ("./core.js", "./pages/dashboard.js", "./pages/../ui.js"):
            response = client.get(urljoin(main_url, module))
            self.assertEqual(response.status_code, 200)
            self.assertIn("javascript", response.headers["content-type"])
        self.assertEqual(client.get("/_ui/obsolete/js/main.js").status_code, 404)

    def test_child_module_edit_changes_all_entry_urls_with_identical_file_metadata(self):
        module = self.ui / "js/pages/dashboard.js"
        module.write_text(module.read_text() + "\n// Cache fixture old\n", encoding="utf-8")
        before = module.stat()
        first = self.client()
        old_html = first.get("/").text
        old_revision = first.get("/api/status").json()["ui_revision"]
        module.write_text(module.read_text().replace("Cache fixture old", "Cache fixture new"), encoding="utf-8")
        os.utime(module, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(module.stat().st_size, before.st_size)
        second = self.client()
        new_revision = second.get("/api/status").json()["ui_revision"]
        self.assertNotEqual(old_revision, new_revision)
        self.assertNotEqual(old_html, second.get("/").text)
        self.assertEqual(second.get("/api/status").json()["version"], first.get("/api/status").json()["version"])
        self.assertIn("Cache fixture new", second.get(f"/_ui/{new_revision}/js/pages/dashboard.js").text)

    def test_old_conditional_validators_cannot_hide_changed_same_size_assets(self):
        css = self.ui / "css/base.css"
        css.write_text(css.read_text() + "\n/* Cache fixture old */\n", encoding="utf-8")
        before = css.stat()
        first = self.client().get("/css/base.css")
        css.write_text(css.read_text().replace("Cache fixture old", "Cache fixture new"), encoding="utf-8")
        os.utime(css, ns=(before.st_atime_ns, before.st_mtime_ns))
        client = self.client()
        revision = client.get("/api/status").json()["ui_revision"]
        for url in ("/css/base.css", f"/_ui/{revision}/css/base.css"):
            for header, value in (("If-None-Match", first.headers["etag"]), ("If-Modified-Since", first.headers["last-modified"])):
                with self.subTest(url=url, header=header):
                    response = client.get(url, headers={header: value})
                    self.assertEqual(response.status_code, 200)
                    self.assertIn("Cache fixture new", response.text)
                    self.assertEqual(response.headers["cache-control"], "no-store")

    def test_document_and_status_do_not_store_or_reuse_old_entries(self):
        client = self.client()
        for url in ("/", "/index.html", "/?embed=1", "/api/status"):
            with self.subTest(url=url):
                response = client.get(url, headers={"If-None-Match": "*", "If-Modified-Since": "Wed, 01 Jan 2098 00:00:00 GMT"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertTrue(response.content)
                self.assertNotIn("clear-site-data", response.headers)
                self.assertNotIn("set-cookie", response.headers)
        head = client.head("/")
        self.assertEqual((head.status_code, head.content), (200, b""))
        self.assertEqual(head.headers["cache-control"], "no-store")


if __name__ == "__main__":
    unittest.main()
