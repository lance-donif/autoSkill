"""Recorded native mutations must remain visible in queued and mixed runs."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill import builder, db, projects, runner  # noqa: E402
from autoskill.replay import ui  # noqa: E402


class Health(BaseHTTPRequestHandler):
    requests = 0

    def log_message(self, *args):
        pass

    def do_GET(self):
        type(self).requests += 1
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class RunnerCapabilityBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Health)
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join(timeout=2)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": self.temporary.name, "AUTOSKILL_HEADED": "0"})
        self.environment.start()
        projects.create("Native boundaries", self.base, project_id="native-boundary")
        Health.requests = 0
        self.native = builder.ui_case("TC-NATIVE", "Unsupported state import", {"start_url": "/", "actions": [
            {"action": "native_step", "command": "state", "args": ["load", "saved.json"],
             "reason": "State import is not portable", "replayable": False}], "assertions": []})
        self.unrecorded = builder.ui_case("TC-UNRECORDED", "Designed but not recorded", {"start_url": "/"})
        self.unrecorded["extensions"].pop("automation")
        self.api = builder.api_case("TC-HTTP", "Health still runs", "GET", "/health", {"status": 200, "json_equals": {"/ok": True}})

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def run_queued(self, selected=None, engine="playwright"):
        queued = runner.create("native-boundary", selected, engine)
        with patch.object(runner, "make_driver", side_effect=AssertionError("Native preflight must not start a browser")) as driver:
            final = runner.execute(queued["id"])
        driver.assert_not_called()
        rows = db.rows("SELECT * FROM results WHERE run_id = ? ORDER BY id", (queued["id"],))
        return queued, final, rows

    def test_default_native_only_project_is_blocked_not_empty(self):
        projects.save_cases("native-boundary", [self.native, self.unrecorded])
        for engine in ("playwright", "selenium"):
            with self.subTest(engine=engine):
                queued, final, rows = self.run_queued(engine=engine)
                self.assertEqual(queued["case_ids"], ["TC-NATIVE"])
                self.assertEqual((final["status"], final["total"], final["done"], final["blocked"]), ("Blocked", 1, 1, 1))
                self.assertEqual([(row["case_id"], row["status"], row["category"]) for row in rows], [
                    ("TC-NATIVE", "Blocked", ui.UNSUPPORTED)])
                self.assertIn("State import is not portable", rows[0]["reason"])
        self.assertEqual(Health.requests, 0)

    def test_default_mixed_run_keeps_native_result_and_executes_http(self):
        projects.save_cases("native-boundary", [self.native, self.api, self.unrecorded])
        queued, final, rows = self.run_queued()
        self.assertEqual(set(queued["case_ids"]), {"TC-NATIVE", "TC-HTTP"})
        self.assertEqual((final["status"], final["total"], final["done"], final["passed"], final["blocked"]),
                         ("Blocked", 2, 2, 1, 1))
        self.assertEqual({row["case_id"]: row["status"] for row in rows}, {"TC-NATIVE": "Blocked", "TC-HTTP": "Pass"})
        self.assertEqual(Health.requests, 1)

    def test_explicit_native_login_case_cannot_reuse_its_way_to_pass(self):
        projects.save_cases("native-boundary", [self.native])
        projects.set_auth("native-boundary", {"ui_case": self.native["id"]})
        with patch.object(runner.Session, "ui_login", side_effect=AssertionError("Native login must preflight first")) as login:
            queued, final, rows = self.run_queued([self.native["id"]])
        login.assert_not_called()
        self.assertEqual((queued["total"], final["status"], rows[0]["category"]), (1, "Blocked", ui.UNSUPPORTED))

    def test_native_prerequisite_blocks_dependent_http_without_sending_it(self):
        self.api["extensions"]["depends_on"] = [self.native["id"]]
        projects.save_cases("native-boundary", [self.native, self.api])
        queued, final, rows = self.run_queued([self.api["id"]])
        self.assertEqual((queued["total"], final["done"], final["blocked"]), (1, 1, 1))
        self.assertEqual([(row["case_id"], row["prerequisite"], row["category"]) for row in rows], [
            ("TC-NATIVE", 1, ui.UNSUPPORTED), ("TC-HTTP", 0, "prerequisite_failed")])
        self.assertEqual(Health.requests, 0)

    def test_genuinely_unrecorded_ui_stays_out_of_default_run(self):
        projects.save_cases("native-boundary", [self.unrecorded])
        with self.assertRaisesRegex(ValueError, "先录制"):
            runner.create("native-boundary")
        with self.assertRaisesRegex(ValueError, "还没有可回放"):
            runner.create("native-boundary", [self.unrecorded["id"]])

    def test_cli_ui_kind_filter_keeps_recorded_native_case(self):
        from autoskill import cli

        projects.save_cases("native-boundary", [self.native, self.api, self.unrecorded])
        output = io.StringIO()
        with patch.dict(os.environ, {"AUTOSKILL_BRIDGE": "off"}), contextlib.redirect_stdout(output), \
                patch.object(runner, "make_driver", side_effect=AssertionError("Native preflight must not launch")) as driver, \
                patch("autoskill.plan.sync", return_value=None):
            code = cli.main(["run", "native-boundary", "--kind", "ui"])
        driver.assert_not_called()
        summary = json.loads(output.getvalue())
        self.assertEqual((code, summary["status"], summary["total"], summary["blocked"]), (2, "Blocked", 1, 1))
        self.assertEqual([row["case_id"] for row in summary["results"]], [self.native["id"]])
        self.assertIn("State import is not portable", summary["results"][0]["detail"])
        self.assertEqual(Health.requests, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
