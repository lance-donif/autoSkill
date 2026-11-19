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

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill import builder, data_refs, db, execution_inputs, projects, runner
from autoskill.server.app import create_app


class SlowHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        time.sleep(0.03)
        data = b'{"ok":true}'
        self.send_response(401 if self.path == "/anonymous" else 200)
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

    def test_anonymous_status_mismatch_is_a_business_failure(self):
        case = builder.api_case("TC-ANONYMOUS", "Anonymous rejection", "GET", "/anonymous", {"status": 402})
        projects.save_cases(self.pid, [case])
        run = runner.create(self.pid, api_only=True)
        final = runner.execute(run["id"])
        row = db.one("SELECT * FROM results WHERE run_id = ?", (run["id"],))
        detail = json.loads(row["detail"])
        self.assertEqual((final["status"], row["category"], detail["business_status"]), ("Fail", "assertion_failed", "Fail"))
        self.assertEqual([(check["expected"], check["actual"]) for check in detail["assertions"]], [(402, 401)])
        self.assertTrue(execution_inputs.result_evidence(final, row))

    def test_report_projects_http_from_each_bound_attempt_receipt(self):
        case = builder.api_case("TC-RETRY", "Retry fixture", "POST", "/same", {"status": 200})
        projects.save_cases(self.pid, [case])
        run = runner.create(self.pid, [case["id"]])
        session = runner.Session(run)
        self.addCleanup(session.close)
        requests = [{"method": "POST", "url": "https://example.invalid/same", "json": {"input": number}} for number in (1, 2)]
        responses = [{"status": code, "body": body} for code, body in ((503, "first"), (200, "second"))]
        for number, (request, response) in enumerate(zip(requests, responses, strict=True), 1):
            outcome = {"status": "Fail" if number == 1 else "Pass", "request": request, "response": response,
                       "duration_ms": number, "assertions": [{"name": "状态码", "passed": number == 2}]}
            last = session.attempt(case, "case" if number == 1 else "relogin_retry", lambda _id, value=outcome: value)
        runner.record(run, case, session.summarize(case, last), False, db.now())
        client = TestClient(create_app(start_worker=False), base_url="http://127.0.0.1:8787")
        endpoint = "/api/runs/" + run["id"]
        attempts = client.get(endpoint).json()["results"][0]["detail"]["attempts"]
        self.assertEqual([item["request"] for item in attempts], requests)
        self.assertEqual([item["response"] for item in attempts], responses)
        self.assertEqual([item["duration_ms"] for item in attempts], [1, 2])
        row = db.one("SELECT id, detail FROM results WHERE run_id = ?", (run["id"],))
        original = json.loads(row["detail"])
        file = data_refs.resolve(original["attempts"][0]["path"])
        receipt = json.loads(file.read_text(encoding="utf-8"))
        for damage in ("bad hash", "wrong case"):
            with self.subTest(damage=damage):
                detail = json.loads(row["detail"])
                if damage == "bad hash":
                    detail["attempts"][0]["sha256"] = "0" * 64
                else:
                    altered = {**receipt, "case_id": "another-case"}
                    execution_inputs.write_json(file, altered)
                    detail["attempts"][0]["sha256"] = execution_inputs.content_hash(altered)
                db.execute("UPDATE results SET detail=? WHERE id=?", (json.dumps(detail), row["id"]))
                attempt = client.get(endpoint).json()["results"][0]["detail"]["attempts"][0]
                self.assertNotIn("request", attempt)
                self.assertNotIn("response", attempt)


if __name__ == "__main__":
    unittest.main()
