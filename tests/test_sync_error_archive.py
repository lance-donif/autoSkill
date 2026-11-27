"""Sync-folder imports and single-ZIP failure archiving for Linux deployments."""
import datetime
import json
import os
import sys
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill import builder, failure_archive, projects, runner, share, sync_import
from autoskill.server.app import create_app


class StatusTarget(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return

    def do_GET(self):
        body = json.dumps({"ok": self.server.status == 200}).encode()
        self.send_response(self.server.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _bundle(folder: Path, name: str, project_id: str, cases, status: str = "pass") -> Path:
    file = folder / name
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps({"project_id": project_id, "cases": cases, "status": status}, ensure_ascii=False), encoding="utf-8")
    return file


class SyncTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.sync = self.folder / "sync"
        self.sync.mkdir()
        self.environment = patch.dict(
            os.environ,
            {
                "AUTOSKILL_WORKSPACE": str(self.folder / "data"),
                "AUTOSKILL_SYNC_DIR": str(self.sync),
                "AUTOSKILL_SYNC_STABLE_SECONDS": "0",
                "AUTOSKILL_BRIDGE": "off",
                "AUTOSKILL_HEADED": "0",
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.pid = projects.create("同步项目", "http://127.0.0.1:9", project_id="sync-project")["id"]

    def test_scan_imports_only_changed_passing_bundles_and_keeps_the_source(self):
        case = builder.api_case("API-SYNCED", "同步来的用例", "GET", "/ok", {"status": 200}, source="开发同步")
        file = _bundle(self.sync, "release/cases.json", self.pid, [case])
        first = sync_import.scan()
        self.assertEqual(first["status"], "ok")
        self.assertEqual(first["imported"], 1)
        imported = projects.case(self.pid, case["id"])
        self.assertEqual(imported["extensions"]["sync"]["file"], "release/cases.json")
        revision = imported["revision"]
        second = sync_import.scan()
        self.assertTrue(second["files"][0]["unchanged"])
        self.assertEqual(projects.case(self.pid, case["id"])["revision"], revision)
        changed = {**case, "title": "同步来的用例（修订）"}
        file.write_text(json.dumps({"project_id": self.pid, "cases": [changed]}, ensure_ascii=False), encoding="utf-8")
        third = sync_import.scan()
        self.assertEqual(third["imported"], 1)
        self.assertGreater(projects.case(self.pid, case["id"])["revision"], revision)

    def test_non_passing_broken_and_unknown_project_files_become_receipts(self):
        _bundle(self.sync, "failed.json", self.pid, [builder.api_case("API-FAILED", "失败文件", "GET", "/ok", {"status": 200})], status="fail")
        (self.sync / "broken.json").write_text("{not json", encoding="utf-8")
        _bundle(self.sync, "unknown.json", "missing-project", [builder.api_case("API-UNKNOWN", "未知项目", "GET", "/ok", {"status": 200})])
        receipt = sync_import.scan()
        self.assertEqual(receipt["imported"], 0)
        rows = {row["file"]: row for row in receipt["files"]}
        self.assertEqual(rows["failed.json"]["status"], "skipped")
        self.assertEqual(rows["broken.json"]["status"], "failed")
        self.assertEqual(rows["unknown.json"]["status"], "failed")
        self.assertEqual(projects.cases(self.pid), [])

    def test_share_zip_is_imported_into_another_data_root(self):
        case = builder.api_case("API-ZIPPED", "打包用例", "GET", "/ok", {"status": 200})
        projects.save_cases(self.pid, [case])
        archive = self.folder / "share.zip"
        share.pack([self.pid], str(archive))
        second_data, second_sync = self.folder / "data-2", self.folder / "sync-2"
        second_sync.mkdir()
        (second_sync / archive.name).write_bytes(archive.read_bytes())
        with patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(second_data), "AUTOSKILL_SYNC_DIR": str(second_sync),
                                     "AUTOSKILL_SYNC_STABLE_SECONDS": "0"}):
            receipt = sync_import.scan()
            self.assertEqual(receipt["imported"], 1)
            self.assertEqual(receipt["files"][0]["projects"], [self.pid])
            self.assertEqual(projects.case(self.pid, case["id"])["title"], case["title"])

    def test_dashboard_reports_the_configured_folders_and_scans(self):
        with TestClient(create_app(8787, start_worker=False), base_url="http://127.0.0.1:8787") as client:
            status = client.get("/api/sync")
            self.assertEqual(status.status_code, 200)
            payload = status.json()
            self.assertEqual(payload["sync_dir"], str(self.sync.resolve()))
            self.assertEqual(payload["conflicts_count"], 0)
            self.assertEqual(payload["conflicts"], [])
            scanned = client.post("/api/sync/scan")
            self.assertEqual(scanned.status_code, 200)
            self.assertEqual(scanned.json()["status"], "ok")


class ErrorArchiveTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.errors = self.folder / "errors"
        self.environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.folder / "data"), "AUTOSKILL_ERROR_DIR": str(self.errors),
                                                   "AUTOSKILL_BRIDGE": "off", "AUTOSKILL_HEADED": "0"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), StatusTarget)
        self.server.status = 200
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.pid = projects.create("错误归档", f"http://127.0.0.1:{self.server.server_port}", project_id="error-archive")["id"]

    def test_failed_run_is_archived_as_single_zip_and_passing_run_is_not(self):
        failing = builder.api_case("API-FAIL", "预期失败", "GET", "/status", {"status": 200})
        passing = builder.api_case("API-PASS", "预期通过", "GET", "/status", {"status": 200})
        projects.save_cases(self.pid, [failing, passing])
        self.server.status = 500
        failed = runner.create(self.pid, [failing["id"]], api_only=True)
        self.assertEqual(runner.execute(failed["id"])["status"], "Fail")
        stamp = datetime.datetime.now().strftime("%Y-%m-%d")  # noqa: DTZ005  # Archive folders use the local day.
        day_dir = self.errors / stamp
        zips = sorted(day_dir.glob("*.zip"))
        self.assertEqual(len(zips), 1, list(day_dir.iterdir()) if day_dir.exists() else [])
        archive_path = zips[0]
        self.assertRegex(archive_path.name, rf"^{self.pid}-error-\d{{8}}-\d{{4}}-{failed['id'][-6:]}\.zip$")
        with zipfile.ZipFile(archive_path) as zf:
            names = set(zf.namelist())
            self.assertIn("manifest.json", names)
            self.assertIn("errors.md", names)
            self.assertIn(f"cases/{failing['id']}.json", names)
            self.assertIn(f"evidence/logs/{failing['id']}.json", names)
            manifest = json.loads(zf.read("manifest.json"))
            self.assertEqual(manifest["schema"], 2)
            self.assertEqual(manifest["kind"], "error_pack")
            self.assertEqual(manifest["counts"]["failed"], 1)
            self.assertEqual(manifest["counts"]["total"], 1)
            self.assertEqual(manifest["counts"]["executed"], 1)
            self.assertEqual(manifest["counts"]["not_run"], 0)
            self.assertEqual(manifest["cases"][0]["case_id"], failing["id"])
            self.assertEqual(manifest["cases"][0]["response"]["status"], 500)
            self.assertEqual(manifest["cases"][0]["case_file"], f"cases/{failing['id']}.json")
            self.assertIn(failing["id"], zf.read("errors.md").decode("utf-8"))

        # Collision in the same minute appends -2 rather than overwriting.
        second = failure_archive.for_run(failed["id"])
        self.assertIsNotNone(second)
        self.assertTrue(second["filename"].endswith("-2.zip"))
        self.assertEqual(len(list(day_dir.glob("*.zip"))), 2)

        # Passing runs produce no error pack.
        self.server.status = 200
        passed = runner.create(self.pid, [passing["id"]], api_only=True)
        self.assertEqual(runner.execute(passed["id"])["status"], "Pass")
        self.assertEqual(len(list(day_dir.glob("*.zip"))), 2)


if __name__ == "__main__":
    unittest.main()
