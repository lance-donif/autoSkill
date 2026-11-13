"""Execution request receipts survive concurrent admission, lost responses and deletion."""
import copy
import json
import os
import sqlite3
import subprocess
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
APP = ROOT / "skills/autoskill/app"
sys.path.insert(0, str(APP))

from autoskill import builder, db, deletion, execution_inputs, execution_requests, native_assets, paths, projects, runner, task_context
from autoskill.server import jobs
from autoskill.server.app import create_app

SUBMIT = """
import json, os, sys
from autoskill import execution_requests
from autoskill.server import jobs
jobs.ensure_worker = lambda: {"status": "unavailable", "pid": None, "reason": "test controls dispatch"}
body = json.load(sys.stdin)
result = execution_requests.start(**body)
if os.environ.get("REQUEST_EXIT_WITHOUT_REPLY") == "1":
    os._exit(0)
sys.stdout.write(json.dumps(result))
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        with self.server.guard:
            self.server.calls.append(self.path)
        body = b'{"ok":true,"answer":42}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ExecutionRequestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.site.guard = threading.Lock()
        threading.Thread(target=cls.site.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.site.shutdown()
        cls.site.server_close()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.root / "workspace"), "AUTOSKILL_CASE_SKILL": str(self.root / "plan"),
                                                   "AUTOSKILL_HEADED": "0", "AUTOSKILL_SESSION": "", "AUTOSKILL_QUEUE_IDLE_SECONDS": "0.2"})
        self.environment.start()
        self.dispatch = patch.object(jobs, "ensure_worker", return_value={"status": "unavailable", "pid": None, "reason": "test controls dispatch"})
        self.worker = self.dispatch.start()
        self.children = []
        self.site.calls = []
        self.pid = projects.create("Execution request fixtures", f"http://127.0.0.1:{self.site.server_port}", project_id="request-test")["id"]
        self.case = builder.api_case("TC-API-001", "One business mutation", "POST", "/effect", {"status": 200, "json_equals": {"/ok": True}})
        projects.save_cases(self.pid, [self.case])
        self.scope = json.dumps(["dsh", "host-session-a"], separators=(",", ":"))
        self.request = {"family": "replay", "project_id": self.pid, "case_ids": [self.case["id"]], "session_id": "task-a"}

    def tearDown(self):
        self.dispatch.stop()
        for child in self.children:
            if child.poll() is None:
                child.terminate()
            child.communicate(timeout=10)
        # Wait for the owned dispatcher and executor report writers before removing the workspace.
        for child in list(jobs._dispatchers.values()):
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.terminate()
                child.wait(timeout=10)
        jobs._dispatchers.clear()
        pids = {row["pid"] for row in db.rows("SELECT pid FROM runs WHERE pid IS NOT NULL")} - {os.getpid()}
        deadline = time.monotonic() + 15
        while any(runner.process_alive(pid) for pid in pids) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(any(runner.process_alive(pid) for pid in pids), "An executor still owns the test workspace")
        runner.reap_children()
        self.environment.stop()
        self.temporary.cleanup()

    def submit(self, key="operation-1", request=None, scope=None):
        return execution_requests.start(scope or self.scope, key, self.request if request is None else request)

    def process(self, *, lost_reply=False):
        environment = paths.child_env()
        environment["REQUEST_EXIT_WITHOUT_REPLY"] = "1" if lost_reply else "0"
        child = subprocess.Popen([sys.executable, "-c", SUBMIT], cwd=APP, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.children.append(child)
        child.stdin.write(json.dumps({"scope": self.scope, "idempotency_key": "operation-1", "request": self.request}).encode())
        child.stdin.close()
        child.stdin = None
        return child

    def test_concurrent_processes_create_one_run_receipt_and_frozen_input(self):
        children = [self.process() for _ in range(5)]
        replies = []
        for child in children:
            output, errors = child.communicate(timeout=30)
            self.assertEqual(child.returncode, 0, errors.decode(errors="replace"))
            replies.append(json.loads(output))
        self.assertEqual(len({reply["run_id"] for reply in replies}), 1)
        self.assertEqual(len({reply["request_id"] for reply in replies}), 1)
        self.assertEqual(len(db.rows("SELECT * FROM runs")), 1)
        self.assertEqual(len(db.rows("SELECT * FROM execution_requests")), 1)
        self.assertEqual(len(list((paths.local() / "execution-inputs").glob("*.json"))), 1)
        self.assertEqual(self.site.calls, [])

    def test_lost_process_reply_recovers_before_freezing_changed_live_cases(self):
        child = self.process(lost_reply=True)
        output, errors = child.communicate(timeout=30)
        self.assertEqual((child.returncode, output), (0, b""), errors.decode(errors="replace"))
        original = execution_requests.lookup(self.scope, "operation-1")
        self.case["path"] = "/changed-after-admission"
        projects.save_cases(self.pid, [self.case])
        with patch.object(runner, "prepare", side_effect=AssertionError("A retry must not freeze again")):
            retry = self.submit()
        self.assertEqual(retry["run_id"], original["run_id"])
        run = db.one("SELECT * FROM runs WHERE id = ?", (retry["run_id"],))
        self.assertEqual(execution_inputs.load(run)["cases"][self.case["id"]]["path"], "/effect")
        self.assertEqual(len(db.rows("SELECT * FROM runs")), 1)

    def test_conflicting_payload_is_rejected_and_json_key_order_is_immaterial(self):
        first = self.submit()
        reordered = dict(reversed(list(self.request.items())))
        with patch.object(runner, "prepare", side_effect=AssertionError("Existing intent must be read first")):
            self.assertEqual(self.submit(request=reordered)["payload_hash"], first["payload_hash"])
            for change in ({"session_id": "task-b"}, {"force_login": True}, {"engine": "selenium"}):
                with self.subTest(change=change), self.assertRaises(execution_requests.ExecutionRequestError) as failure:
                    self.submit(request={**self.request, **change})
                self.assertEqual((failure.exception.code, failure.exception.status_code), ("idempotency_conflict", 409))
                self.assertEqual(failure.exception.details["run_id"], first["run_id"])
        self.assertEqual(len(db.rows("SELECT * FROM runs")), 1)

    def test_scope_and_operation_identity_isolate_legitimate_executions(self):
        first = self.submit()
        second = self.submit(scope='["dsh","host-session-b"]', request={**self.request, "session_id": "task-b"})
        third = self.submit(key="operation-2")
        self.assertEqual(len({reply["run_id"] for reply in (first, second, third)}), 3)
        self.assertEqual([first["run"]["session_id"], second["run"]["session_id"]], ["task-a", "task-b"])

    def test_receipt_and_enqueue_roll_back_together_before_dispatch(self):
        with db.connect() as connection:
            connection.execute("CREATE TRIGGER reject_request BEFORE INSERT ON execution_requests BEGIN SELECT RAISE(ABORT, 'synthetic storage failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.submit()
        self.assertEqual(db.rows("SELECT * FROM runs"), [])
        self.assertEqual(db.rows("SELECT * FROM execution_requests"), [])
        self.worker.assert_not_called()
        db.execute("DROP TRIGGER reject_request")
        self.assertEqual(self.submit()["state"], "accepted")

    def test_dispatch_observes_committed_rows_and_failure_preserves_the_receipt(self):
        def unavailable():
            receipt = execution_requests.lookup(self.scope, "operation-1")
            self.assertEqual(receipt["run"]["status"], "Queued")
            self.assertIsNotNone(db.one("SELECT * FROM runs WHERE id = ?", (receipt["run_id"],)))
            raise OSError("synthetic dispatcher resource failure")

        self.worker.side_effect = unavailable
        first = self.submit()
        self.assertEqual(first["queue_dispatcher"]["status"], "unavailable")
        self.worker.side_effect = None
        with patch.object(runner, "prepare", side_effect=AssertionError("Do not replace the admitted intent")):
            retry = self.submit()
        self.assertEqual(retry["run_id"], first["run_id"])
        self.assertEqual(len(db.rows("SELECT * FROM runs")), 1)

    def test_deleted_execution_keeps_request_tombstone_without_revival(self):
        first = self.submit()
        result = deletion.delete_runs([first["run_id"]], start_worker=False)
        self.assertFalse(result["errors"])
        self.assertEqual(len(db.rows("SELECT * FROM execution_requests")), 1)
        self.worker.reset_mock()
        with patch.object(runner, "prepare", side_effect=AssertionError("Deleted request must not freeze again")):
            retry = self.submit()
        self.assertEqual((retry["state"], retry["run_id"], retry["run"]), ("deleted", first["run_id"], None))
        self.assertEqual(execution_requests.lookup(self.scope, "operation-1"), retry)
        self.assertEqual(db.rows("SELECT * FROM runs"), [])
        self.worker.assert_not_called()

    def test_missing_run_is_visible_without_reconstructing_its_execution(self):
        original = self.submit()
        db.execute("DELETE FROM runs WHERE id = ?", (original["run_id"],))
        self.worker.reset_mock()
        with patch.object(runner, "prepare", side_effect=AssertionError("An incomplete receipt is not a new intent")):
            response = self.submit()
        self.assertEqual((response["state"], response["run"]), ("missing", None))
        self.worker.assert_not_called()

    def test_lookup_is_read_only_and_does_not_initialize_an_absent_workspace(self):
        empty = self.root / "absent-workspace"
        with patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(empty)}), self.assertRaises(execution_requests.ExecutionRequestError) as failure:
            execution_requests.lookup(self.scope, "absent")
        self.assertEqual(failure.exception.status_code, 404)
        self.assertFalse(empty.exists())
        original = self.submit()
        before = {file.relative_to(paths.local()): file.read_bytes() for file in paths.local().rglob("*.json")}
        self.worker.reset_mock()
        with patch.object(db, "init", side_effect=AssertionError("Lookup cannot initialize state")):
            current = execution_requests.lookup(self.scope, "operation-1")
        self.assertEqual(current["run_id"], original["run_id"])
        self.assertEqual(before, {file.relative_to(paths.local()): file.read_bytes() for file in paths.local().rglob("*.json")})
        self.worker.assert_not_called()

    def test_omitted_context_is_captured_once_and_not_re_resolved_on_retry(self):
        request = {key: value for key, value in self.request.items() if key != "session_id"}
        with task_context.use_session("original-context"):
            first = self.submit(request=request)
        with task_context.use_session("different-context"), patch.dict(os.environ, {"AUTOSKILL_HEADED": "1"}):
            second = self.submit(request=request)
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertEqual(second["run"]["session_id"], "original-context")
        self.assertEqual(db.one("SELECT headed FROM runs WHERE id = ?", (first["run_id"],))["headed"], 0)

    def test_invalid_json_values_and_failed_preparation_never_create_an_intent(self):
        invalid = [{**self.request, 1: "not a JSON object key"},
                   {"family": "native", "project_id": self.pid, "asset_id": "missing", "parameters": {"rate": float("nan")}},
                   {**self.request, "project_id": "missing-project"}, {**self.request, "case_ids": ["missing-case"]}]
        for request in invalid:
            with self.subTest(request=request), self.assertRaises(execution_requests.ExecutionRequestError) as failure:
                self.submit(request=request)
            self.assertEqual(failure.exception.code, "validation_error")
        self.assertEqual(db.rows("SELECT * FROM runs"), [])
        self.assertEqual(db.rows("SELECT * FROM execution_requests"), [])
        self.worker.assert_not_called()

    def test_http_contract_errors_do_not_admit_work_and_old_run_api_remains_available(self):
        with TestClient(create_app(start_worker=False), base_url="http://127.0.0.1") as client:
            missing = client.get("/api/execution-requests", params={"scope": self.scope, "idempotency_key": "missing"})
            self.assertEqual((missing.status_code, missing.json()["code"]), (404, "request_not_found"))
            body = {"scope": self.scope, "idempotency_key": "operation-1", "request": self.request}
            first = client.post("/api/execution-requests", json=body)
            self.assertEqual(first.status_code, 200, first.text)
            changed = copy.deepcopy(body)
            changed["request"]["force_login"] = True
            conflict = client.post("/api/execution-requests", json=changed)
            self.assertEqual((conflict.status_code, conflict.json()["code"]), (409, "idempotency_conflict"))
            for request in (None, [], {**self.request, "headed": "false"}, {**self.request, "ignored_parameter": True}):
                rejected = client.post("/api/execution-requests", json={**body, "idempotency_key": "invalid", "request": request})
                self.assertEqual(rejected.status_code, 400, rejected.text)
            recovered = client.get("/api/execution-requests", params={"scope": self.scope, "idempotency_key": "operation-1"})
            self.assertEqual(recovered.json()["run_id"], first.json()["run_id"])
            self.assertEqual(recovered.headers["cache-control"], "no-store")
            self.assertEqual(client.get("/api/runs/" + first.json()["run_id"]).status_code, 200)
        self.assertEqual(len(db.rows("SELECT * FROM runs")), 1)

    def test_public_http_admits_replay_and_native_then_real_worker_executes_each_once(self):
        file = self.root / "conversation.json"
        file.write_text(json.dumps({"endpoint": {"url": f"http://127.0.0.1:{self.site.server_port}/ask"},
                                    "samples": [{"id": "number", "question": "Value?", "expected": 42}]}), encoding="utf-8")
        asset = native_assets.import_asset(self.pid, str(file), "ai-eval")
        native = {"family": "native", "project_id": self.pid, "asset_id": asset["id"], "session_id": "task-native", "host": "dsh", "host_session_id": "host-session-a"}
        self.dispatch.stop()
        with TestClient(create_app(start_worker=False), base_url="http://127.0.0.1") as client:
            replies = []
            for index, request in enumerate((self.request, native)):
                body = {"scope": self.scope, "idempotency_key": "real-" + str(index), "request": request}
                response = client.post("/api/execution-requests", json=body)
                self.assertEqual(response.status_code, 200, response.text)
                receipt = response.json()
                self.assertIn(receipt["queue_dispatcher"]["status"], ("started", "running"), receipt)
                repeated = client.post("/api/execution-requests", json=body)
                self.assertEqual(repeated.json()["run_id"], receipt["run_id"])
                replies.append(receipt)
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                statuses = [client.get("/api/execution-requests", params={"scope": self.scope, "idempotency_key": row["idempotency_key"]}).json()
                            for row in replies]
                if all(row["run"]["status"] in db.FINISHED for row in statuses):
                    break
                time.sleep(0.04)
            self.assertEqual([row["run"]["status"] for row in statuses], ["Pass", "Pass"], statuses)
        self.assertEqual(sorted(self.site.calls), ["/ask", "/effect"])
        self.assertEqual(len(db.rows("SELECT * FROM runs")), 2)
        self.assertEqual(len(db.rows("SELECT * FROM results")), 2)
        for receipt in replies:
            run = db.one("SELECT * FROM runs WHERE id = ?", (receipt["run_id"],))
            self.assertTrue(Path(run["input_manifest"]).is_file())
            result = db.one("SELECT detail FROM results WHERE run_id = ?", (receipt["run_id"],))
            self.assertTrue(json.loads(result["detail"])["business_verified"])


if __name__ == "__main__":
    unittest.main()
