"""Exercise deletion through actual CLI children and the host bridge without losing unrelated data."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "skills/autoskill/app"
sys.path.insert(0, str(APP))

from autoskill import bridge, builder, db, paths, projects, runner, secrets


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        data = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class CLIDeletionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.folder / "workspace"), "AUTOSKILL_BRIDGE": "off",
                                              "AUTOSKILL_CASE_SKILL": str(self.folder / "plan"), "AUTOSKILL_SESSION": "",
                                              "AUTOSKILL_RUNTIME_CONTEXT": "", "AUTOSKILL_HEADED": "0"})
        environment.start()
        self.addCleanup(environment.stop)
        self.pid = projects.create("CLI 删除", f"http://127.0.0.1:{self.server.server_port}", project_id="cli-delete")["id"]
        cases = [builder.api_case(f"TC-API-{number:03}", f"业务 {number}", "GET", "/ok", {"status": 200, "json_equals": {"/ok": True}})
                 for number in (1, 2, 3)]
        projects.save_cases(self.pid, cases)

    def command(self, *argv):
        result = subprocess.run([sys.executable, "-m", "autoskill", *argv], cwd=self.folder,
                                env={**os.environ, "PYTHONPATH": str(APP)}, capture_output=True,
                                text=True, encoding="utf-8", check=False, timeout=30)
        self.assertNotIn("Traceback", result.stderr)
        return result.returncode, json.loads(result.stdout)

    def test_cli_deletes_actual_run_and_case_separately_with_partial_errors_and_retries(self):
        secrets.set_secret("RETAINED_TEST_CREDENTIAL", "synthetic-only")
        run = runner.execute(runner.create(self.pid, ["TC-API-001"])["id"])
        self.assertEqual(run["status"], "Pass")
        evidence = paths.local() / "runs" / run["id"]
        self.assertTrue(evidence.is_dir())
        code, result = self.command("delete", "case", self.pid, "TC-API-001")
        self.assertEqual((code, len(result["deleted"]), result["errors"]), (0, 1, []))
        self.assertIsNotNone(db.one("SELECT id FROM runs WHERE id = ?", (run["id"],)))
        self.assertTrue(evidence.is_dir())
        outside = self.folder / "outside.txt"
        outside.write_text("retained", encoding="utf-8")
        code, result = self.command("delete", "run", run["id"], "../outside.txt")
        self.assertEqual((code, len(result["deleted"]), len(result["errors"])), (2, 1, 1))
        self.assertEqual(result["deleted"][0]["run_id"], run["id"])
        self.assertIsNone(db.one("SELECT id FROM runs WHERE id = ?", (run["id"],)))
        self.assertFalse(evidence.exists())
        self.assertEqual(outside.read_text(encoding="utf-8"), "retained")
        self.assertEqual({case["id"] for case in projects.cases(self.pid)}, {"TC-API-002", "TC-API-003"})
        self.assertIn("RETAINED_TEST_CREDENTIAL", secrets.names())
        code, result = self.command("delete", "run", run["id"])
        self.assertEqual((code, result["deleted"][0]["already_deleted"]), (0, True))

    def test_bridge_runs_bulk_deletion_in_the_bound_workspace_and_rejects_missing_arguments(self):
        response = bridge.execute(["delete", "case", self.pid, "TC-API-001", "TC-API-002"], session_id="task-delete")
        result = json.loads(response["stdout"])
        self.assertEqual((response["exit"], len(result["deleted"]), result["errors"]), (0, 2, []))
        self.assertEqual([case["id"] for case in projects.cases(self.pid)], ["TC-API-003"])
        code, result = self.command("delete", "run")
        self.assertEqual((code, result["code"]), (2, "validation_error"))
        self.assertEqual(projects.get(self.pid)["id"], self.pid)


if __name__ == "__main__":
    unittest.main()
