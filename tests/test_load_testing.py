"""Real HTTP load validates concurrent isolation, frozen inputs, failure retention and cancellation."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))
from fastapi.testclient import TestClient

from autoskill import builder, db, execution_inputs, load_testing, paths, projects, reporting, runner, sessions, task_report_data
from autoskill.server import jobs, metrics
from autoskill.server.app import create_app


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        return

    def do_GET(self):
        server = self.server
        with server.lock:
            server.active += 1
            server.maximum = max(server.maximum, server.active)
            server.seen.append((self.path, self.headers.get("Cookie", ""), self.headers.get("X-Token", "")))
            server.entered.set()
        try:
            time.sleep(.12)
            token = uuid.uuid4().hex
            is_login = self.path == "/login"
            okay = self.headers.get("Cookie", "") == "identity=" + self.headers.get("X-Token", "")
            data = json.dumps({"token": token, "value": 42}).encode()
            self.send_response(200 if is_login or okay else 401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            if is_login:
                self.send_header("Set-Cookie", f"identity={token}; Path=/")
            self.end_headers()
            self.wfile.write(data)
        finally:
            with server.lock:
                server.active -= 1


class LoadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous = os.environ.get("AUTOSKILL_WORKSPACE")
        os.environ["AUTOSKILL_WORKSPACE"] = self.tmp.name
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.lock = threading.Lock()
        self.server.active = self.server.maximum = 0
        self.server.seen = []
        self.server.entered = threading.Event()
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .02}, daemon=True).start()
        projects.create("负载测试", f"http://127.0.0.1:{self.server.server_port}", project_id="load")
        login = builder.api_case("LOGIN", "登录", "GET", "/login", {"status": 200, "json_equals": {"/value": 42}}, extract={"token": "/token"})
        target = builder.api_case("TARGET", "查看", "GET", "/target", {"status": 200, "json_equals": {"/value": 42}},
                                  headers={"X-Token": "{{token}}"}, depends_on=["LOGIN"])
        projects.save_cases("load", [login, target])
        projects.set_auth("load", {"api_case": "LOGIN"})

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        if self.previous is None:
            os.environ.pop("AUTOSKILL_WORKSPACE", None)
        else:
            os.environ["AUTOSKILL_WORKSPACE"] = self.previous
        self.tmp.cleanup()

    def test_real_concurrency_preserves_per_iteration_login_and_frozen_case(self):
        sessions.save("load", variables={"token": "existing-login"})
        before = sessions.load("load")
        run = load_testing.enqueue("load", ["TARGET"], concurrency=3, iterations=6)
        changed = projects.case("load", "TARGET")
        changed["assertions"]["json_equals"]["/value"] = -1
        projects.save_cases("load", [changed])
        final = runner.execute(run["id"])
        self.assertEqual(final["status"], "Pass", final)
        detail = load_testing.get(run["id"])
        self.assertEqual((detail["done"], detail["total"], detail["metrics"]["request_count"]), (6, 6, 12))
        self.assertGreaterEqual(self.server.maximum, 2)
        login_requests = [item for item in self.server.seen if item[0] == "/login"]
        targets = [item for item in self.server.seen if item[0] == "/target"]
        self.assertTrue(all(not item[1] for item in login_requests))
        self.assertEqual(len({item[2] for item in targets}), 6)
        self.assertTrue(all(item[1] == "identity=" + item[2] for item in targets))
        self.assertEqual(sessions.load("load"), before)
        self.assertEqual(metrics.summary(metrics.Scope())["results"]["executions"], 0)
        self.assertEqual(reporting.latest_results("load"), {})
        for row in detail["results"]:
            self.assertTrue(execution_inputs.result_evidence(final, row))

    def test_failed_dependency_is_not_retried_or_sent_as_target(self):
        login = projects.case("load", "LOGIN")
        login["assertions"]["status"] = 201
        projects.save_cases("load", [login])
        run = load_testing.enqueue("load", ["TARGET"], concurrency=2, iterations=3)
        runner.execute(run["id"])
        detail = load_testing.get(run["id"])
        self.assertEqual((detail["status"], detail["done"], detail["blocked"]), ("Blocked", 3, 3))
        self.assertEqual(len(self.server.seen), 3)
        self.assertIsNone(detail["metrics"]["p95_ms"])
        self.assertEqual(detail["metrics"]["error_rate"], 1)
        self.assertEqual(len(task_report_data.run_snapshot(detail)["cases"]), 3)

    def test_cancel_stops_ramp_and_retains_unexecuted_denominator(self):
        run = load_testing.enqueue("load", ["TARGET"], concurrency=3, iterations=100, ramp_up_seconds=30)
        thread = threading.Thread(target=runner.execute, args=(run["id"],))
        thread.start()
        self.assertTrue(self.server.entered.wait(10))
        jobs.cancel(run["id"])
        thread.join(10)
        self.assertFalse(thread.is_alive())
        detail = load_testing.get(run["id"])
        self.assertEqual(detail["status"], "Cancelled")
        self.assertGreater(detail["metrics"]["unexecuted_cases"], 0)
        self.assertEqual(detail["metrics"]["planned_cases"], 100)
        self.assertEqual(len(self.server.seen), 1)

    def test_http_rejects_invalid_load_without_queueing_and_can_read_run(self):
        with TestClient(create_app(start_worker=False), base_url="http://127.0.0.1:8787") as client:
            options = client.get("/api/load-tests/options").json()
            self.assertEqual(options["total"], 2)
            body = {"items": [{"project_id": "load", "case_ids": ["TARGET"]}], "concurrency": 0, "iterations": 2}
            self.assertEqual(client.post("/api/load-tests", json=body).status_code, 400)
            self.assertEqual(db.rows("SELECT * FROM runs"), [])
            body["concurrency"] = 1
            with patch.object(jobs, "ensure_worker", return_value={"status": "test"}):
                made = client.post("/api/load-tests", json=body)
            self.assertEqual(made.status_code, 200, made.text)
            identity = made.json()["runs"][0]["id"]
            runner.execute(identity)
            self.assertEqual(client.get("/api/load-tests/" + identity).json()["load"]["iterations"], 2)
            self.assertEqual(client.get("/api/load-tests").json()["total"], 1)
            self.assertTrue((paths.local() / "runs" / identity / "attempts").is_dir())


if __name__ == "__main__":
    unittest.main()
