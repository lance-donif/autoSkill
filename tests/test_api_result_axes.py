"""Slow responses and wrong business content retain independent verdicts and actual evidence."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill import builder, db, execution_inputs, projects, runner


class SlowHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        time.sleep(0.03)
        data = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class ApiResultAxesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
        threading.Thread(target=cls.site.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.site.shutdown()
        cls.site.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": self.tmp.name, "AUTOSKILL_HEADED": "0"})
        environment.start()
        self.addCleanup(environment.stop)
        self.pid = projects.create("Separate verdicts", f"http://127.0.0.1:{self.site.server_port}", project_id="axes")["id"]

    def execute(self, expected=True, *, observed=False, maximum=1):
        case = builder.api_case("TC-API-001", "Content and latency", "GET", "/", {"status": 200, "json_equals": {"/ok": expected}, "max_ms": maximum})
        if observed:
            case["extensions"]["oracle"] = {"source": "observation"}
        projects.save_cases(self.pid, [case])
        run = runner.create(self.pid)
        final = runner.execute(run["id"])
        row = db.one("SELECT * FROM results WHERE run_id = ?", (run["id"],))
        self.assertTrue(execution_inputs.result_evidence(final, row))
        return final, row, json.loads(row["detail"])

    def test_latency_failure_does_not_turn_passing_business_checks_into_a_business_failure(self):
        final, row, detail = self.execute()
        self.assertEqual((final["status"], row["status"]), ("Fail", "Fail"))
        self.assertEqual((detail["business_status"], detail["business_verified"], detail["performance_status"]), ("Pass", True, "Fail"))
        self.assertEqual(detail["execution_status"], "completed")
        self.assertFalse(detail["performance_assertions"][0]["passed"])
        self.assertTrue(all(check["passed"] for check in detail["assertions"] if check["scope"] == "business"))

    def test_content_failure_survives_when_performance_passes(self):
        final, _row, detail = self.execute(False, maximum=10000)
        self.assertEqual(final["status"], "Fail")
        self.assertEqual((detail["business_status"], detail["business_verified"], detail["performance_status"]), ("Fail", False, "Pass"))

    def test_observed_content_stays_unverified_even_when_a_performance_target_fails(self):
        final, _row, detail = self.execute(observed=True)
        self.assertEqual(final["status"], "Fail")
        self.assertEqual((detail["business_status"], detail["business_verified"], detail["performance_status"]), ("Unverified", False, "Fail"))


if __name__ == "__main__":
    unittest.main()
