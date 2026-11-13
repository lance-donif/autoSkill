"""Real HTTP native reruns retain frozen assets and never select IR cases."""
import hashlib
import os
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from test_data_dashboard import DataServer

from autoskill import db, native_assets, native_run, paths, projects


class NativeRerunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.root / "workspace"), "AUTOSKILL_HEADED": "0",
                                               "AUTOSKILL_BRIDGE": "off", "AUTOSKILL_SESSION": "", "AUTOSKILL_RUNTIME_CONTEXT": "",
                                               "AUTOSKILL_WORKFLOW_BINDING": "", "AUTOSKILL_QUEUE_IDLE_SECONDS": "0.1"})
        environment.start()
        self.addCleanup(environment.stop)
        self.pid = projects.create("原生重跑", "http://native-rerun.example", project_id="native-rerun")["id"]
        self.source = self.root / "original"
        self.source.mkdir()
        self.script = self.source / "test_saved.py"
        self.script.write_text("import os,unittest\nclass Saved(unittest.TestCase):\n"
                               " def test_original_pass(self): self.assertEqual(os.environ['PINNED_VALUE'],'42')\n"
                               " def test_original_fail(self): self.assertEqual(41,42)\n")
        self.asset = native_assets.import_asset(self.pid, str(self.source), "selenium-unittest")
        queued = native_run.enqueue(self.pid, self.asset["id"], parameters={"env": {"PINNED_VALUE": "42"}, "timeout_seconds": 10},
                                    session_id="native-original", host="dsh", host_session_id="native-host")
        self.run = native_run.execute(queued["id"])
        self.assertEqual(self.run["status"], "Fail")
        self.assertEqual(projects.cases(self.pid), [])
        self.addCleanup(self.wait_dispatcher)
        self.server = DataServer(paths.workspace())
        self.addCleanup(self.server.close)

    def wait_dispatcher(self):
        state = self.root / "workspace" / ".local" / "queue-worker.json"
        deadline = time.monotonic() + 15
        while state.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(state.exists(), "The isolated queue must finish reporting before fixture cleanup")

    def fingerprint(self, folder):
        return {str(file.relative_to(folder)): hashlib.sha256(file.read_bytes()).hexdigest()
                for file in folder.rglob("*") if file.is_file()}

    def test_full_asset_rerun_uses_original_head_parameters_and_preserves_old_evidence(self):
        old_manifest = native_run._frozen(self.run)
        old_folder = paths.local() / "runs" / self.run["id"]
        old_files = self.fingerprint(old_folder)
        old_row = db.one("SELECT * FROM runs WHERE id=?", (self.run["id"],))
        self.script.write_text("import unittest\nclass Changed(unittest.TestCase):\n def test_new_version(self): self.fail('changed asset')\n")
        changed = native_assets.import_asset(self.pid, str(self.source), "selenium-unittest", asset_id=self.asset["id"])
        self.assertNotEqual(changed["head"], self.asset["head"])
        created = self.server.api("/runs/" + self.run["id"] + "/rerun", "POST", {})
        self.assertNotEqual(created["id"], self.run["id"])
        self.assertEqual(created["family"], "native")
        deadline = time.monotonic() + 15
        row = None
        while time.monotonic() < deadline:
            row = db.one("SELECT * FROM runs WHERE id=?", (created["id"],))
            if row["status"] in db.FINISHED:
                break
            time.sleep(0.02)
        self.assertEqual(row["status"], "Fail", row)
        later = native_run._frozen(row)
        for key in ("asset_id", "head", "asset_version", "parameters", "frozen_files", "runtime_context"):
            self.assertEqual(later[key], old_manifest[key], key)
        results = db.rows("SELECT case_title,status FROM results WHERE run_id=?", (created["id"],))
        self.assertEqual(len(results), 2)
        self.assertEqual(sorted(item["status"] for item in results), ["Fail", "Pass"])
        self.assertTrue(all("test_original_" in item["case_title"] for item in results))
        self.assertEqual(db.one("SELECT * FROM runs WHERE id=?", (self.run["id"],)), old_row)
        self.assertEqual(self.fingerprint(old_folder), old_files)

    def test_partial_native_rerun_is_explicitly_rejected_without_creating_another_run(self):
        count = db.one("SELECT COUNT(*) n FROM runs")["n"]
        for selection in ({"failed_only": True}, {"case_ids": ["one-case"]}, {"case_ids": []}):
            with self.subTest(selection=selection), self.assertRaises(RuntimeError) as error:
                self.server.api("/runs/" + self.run["id"] + "/rerun", "POST", selection)
            self.assertIsInstance(error.exception.__cause__, urllib.error.HTTPError)
            self.assertEqual(error.exception.__cause__.code, 400)
            self.assertIn("native_partial_rerun_unsupported", str(error.exception))
            self.assertIn("完整原生资产", str(error.exception))
        self.assertEqual(db.one("SELECT COUNT(*) n FROM runs")["n"], count)
