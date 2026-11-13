"""Relocation journals survive actual process death without replaying unresolved business work."""
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "skills/autoskill/app"
sys.path.insert(0, str(APP))

from autoskill import builder, data_refs, data_runtime, data_transfer, db, dialogue_judge, execution_inputs, projects, runner, runtime_identity, workflow
from autoskill.server import jobs, scheduler

CRASH = """
import os, sys
from pathlib import Path
from autoskill import data_runtime, paths
phase = sys.argv[1]
apply = data_runtime._apply_database
write = data_runtime._durable_json
replace = Path.replace
def apply_database(root, event):
    if phase == 'before_database': os._exit(77)
    apply(root, event)
    if phase == 'after_database': os._exit(77)
def durable(file, value, root):
    write(file, value, root)
    if phase == 'after_marker' and file.name == 'runtime-location.json': os._exit(77)
def move(self, target):
    result = replace(self, target)
    if phase == 'after_archive' and '/runtime/' in str(target): os._exit(77)
    return result
data_runtime._apply_database = apply_database
data_runtime._durable_json = durable
Path.replace = move
data_runtime.attach(paths.workspace(), force=True)
raise SystemExit('crash point not reached')
"""


class Handler(BaseHTTPRequestHandler):
    calls = 0

    def do_GET(self):
        type(self).calls += 1
        body = b'{"priority":"medium"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class RelocationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / "data"
        environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.root), "AUTOSKILL_SESSION": "relocation-test",
                                              "AUTOSKILL_CASE_SKILL": str(self.root / ".local/case-plans/default/autoskill-cases"), "AUTOSKILL_HEADED": "0"})
        environment.start()
        self.addCleanup(environment.stop)
        # These fixtures represent a stopped foreign runtime. Its fake owners and
        # pending writes must not be reaped by an unrelated live dispatcher.
        monitor = patch.object(jobs, "ensure_worker")
        monitor.start()
        self.addCleanup(monitor.stop)
        self.project = projects.create("Relocation", f"http://127.0.0.1:{self.server.server_port}", project_id="relocation")["id"]
        self.before_calls = Handler.calls
        self.now = datetime.datetime.now(datetime.UTC)

    def task(self, **extra):
        return scheduler.create({"project_id": self.project, "title": "Preserve recurring work", "kind": "maintenance", "workflow_id": "flow-test",
                                 "host": "dsh", "host_session_id": "old-host-session", "rule": {"kind": "interval", "minutes": 5}, **extra})

    def fixture(self):
        task = self.task()
        delivery = scheduler.run_now(task["id"], "original-intent")
        claim = scheduler.claim_delivery(delivery["id"], "old-owner")
        run = {"id": db.new_id("run"), "project_id": self.project, "engine": "http", "status": "Running", "total": 2,
               "done": 1, "failed": 1, "pid": 99999999, "created_at": db.now(), "reason": "retained partial failure"}
        db.insert("runs", run)
        flow = workflow.create(self.project, {"title": "Unresolved work", "sources": [{"id": "s1", "text": "Expected priority high"}]},
                               idempotency_key="flow-create")["workflow_id"]
        leased = workflow.next_task(flow, idempotency_key="flow-claim")["task"]
        workflow.operation_intent("click", {"postcondition": {"text": "created"}}, side_effect=True, binding=leased, idempotency_key="issued-before-crash")
        dialogue_judge.init()
        db.insert("dialogue_judgements", {"id": "judge-old", "run_id": run["id"], "case_id": "case-old", "turn": 1, "input_hash": "original-hash",
                                         "frozen_input": {"answer": "original"}, "state": "leased", "attempt_id": "judge-attempt-old", "created_at": db.now()})
        db.insert("dialogue_judge_attempts", {"id": "judge-attempt-old", "judge_id": "judge-old", "generation": 1, "owner": "old-host",
                                             "lease_token": "old-judge-token", "lease_until": (self.now + datetime.timedelta(hours=1)).isoformat(),
                                             "state": "leased", "created_at": db.now()})
        runtime = {"service.json": b'{"pid":99999999}', "host-deliveries/dsh/delivery.json": b'{"state":"prepared","session_id":"old-host-session"}'}
        for name, data in runtime.items():
            file = self.root / ".local" / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(data)
        return task, claim, run, runtime

    def event(self, identity):
        file = self.root / ".local/relocations" / identity / "event.json"
        return file, json.loads(file.read_bytes())

    def assert_recovered(self, task, claim, run, runtime, result):
        file, event = self.event(result["event_id"])
        original = event["original_rows"]
        self.assertEqual(original["runs"][0]["status"], "Running")
        self.assertEqual(original["host_outbox"][0]["token"], claim["token"])
        self.assertEqual(original["workflow_attempts"][0]["state"], "leased")
        self.assertEqual(original["workflow_items"][0]["state"], "leased")
        self.assertEqual(original["workflow_operations"][0]["state"], "intended")
        self.assertEqual(original["dialogue_judgements"][0]["frozen_input"], json.dumps({"answer": "original"}))
        self.assertEqual(original["dialogue_judge_attempts"][0]["lease_token"], "old-judge-token")
        current = scheduler.get(task["id"])
        self.assertEqual((current["enabled"], current["next_due"], current["revision"]), (True, task["next_due"], task["revision"] + 1))
        self.assertEqual(current["host_session_id"], "")
        migrated = db.one("SELECT * FROM runs WHERE id=?", (run["id"],))
        self.assertEqual((migrated["status"], migrated["failed"], migrated["done"]), ("Fail", 1, 1))
        self.assertEqual(db.one("SELECT state FROM dialogue_judgements WHERE id='judge-old'")["state"], "cancelled")
        for item in event["runtime_files"]:
            name = item["source"].removeprefix(".local/")
            for original_name, content in runtime.items():
                if original_name == name or original_name.startswith(name + "/"):
                    suffix = original_name[len(name):].lstrip("/")
                    archived = self.root / item["saved"]
                    self.assertEqual((archived / suffix if suffix else archived).read_bytes(), content)
            self.assertFalse((self.root / item["source"]).exists())
        before = file.read_bytes()
        self.assertEqual(data_runtime.attach(self.root), {"relocated": False})
        self.assertEqual(file.read_bytes(), before)
        self.assertEqual(db.one("SELECT COUNT(*) n FROM data_relocation_receipts")["n"], 1)
        self.assertEqual(Handler.calls, self.before_calls)

    def crash_and_resume(self, phase):
        task, claim, run, runtime = self.fixture()
        child = subprocess.run([sys.executable, "-c", CRASH, phase], env={**os.environ, "PYTHONPATH": str(APP)}, cwd=self.parent,
                               capture_output=True, text=True, timeout=20, check=False)
        self.assertEqual(child.returncode, 77, child.stdout + child.stderr)
        pending = json.loads((self.root / ".local/relocation-pending.json").read_bytes())
        file, _event = self.event(pending["event_id"])
        before = file.read_bytes()
        result = data_runtime.attach(self.root, force=True)
        self.assertEqual(result["event_id"], pending["event_id"])
        self.assertEqual(file.read_bytes(), before)
        self.assertFalse((self.root / ".local/relocation-pending.json").exists())
        self.assert_recovered(task, claim, run, runtime, result)

    def test_process_crash_after_journal_before_database_resumes_original_intent(self):
        self.crash_and_resume("before_database")

    def test_process_crash_after_database_commit_does_not_apply_revisions_twice(self):
        self.crash_and_resume("after_database")

    def test_process_crash_after_partial_runtime_archive_resumes_without_overwriting(self):
        self.crash_and_resume("after_archive")

    def test_process_crash_after_marker_still_finishes_the_same_pending_journal(self):
        self.crash_and_resume("after_marker")

    def test_journal_write_failure_changes_no_database_rows_or_runtime_files(self):
        _task, _claim, _run, runtime = self.fixture()
        before = {table: db.rows("SELECT * FROM " + table) for table in data_runtime.TABLES}
        with patch.object(data_runtime, "_durable_json", side_effect=OSError("injected full disk")), self.assertRaises(OSError):
            data_runtime.attach(self.root, force=True)
        self.assertEqual({table: db.rows("SELECT * FROM " + table) for table in data_runtime.TABLES}, before)
        for name, contents in runtime.items():
            self.assertEqual((self.root / ".local" / name).read_bytes(), contents)

    def test_old_outbox_tokens_and_session_targets_are_fenced_until_explicit_investigation(self):
        task, claim, run, runtime = self.fixture()
        result = data_runtime.attach(self.root, force=True)
        self.assert_recovered(task, claim, run, runtime, result)
        delivery = scheduler.pending_deliveries()["items"][0]
        self.assertEqual((delivery["state"], delivery["owner"], delivery["token"]), ("unknown", "", ""))
        self.assertEqual(delivery["payload"]["parameters"]["host_session_id"], "")
        with self.assertRaisesRegex(ValueError, "租约"):
            scheduler.ack_delivery(delivery["id"], claim["token"], {"host": "old-host"})
        self.assertEqual(scheduler.claim_delivery(delivery["id"], "new-owner")["state"], "unknown")
        scheduler.resolve_delivery(delivery["id"], retry=True, reason="Current host verified the old prompt was not issued")
        fresh = scheduler.claim_delivery(delivery["id"], "new-owner")
        self.assertNotEqual(fresh["token"], claim["token"])
        self.assertEqual(fresh["payload"]["parameters"]["host_session_id"], "")

    def test_future_definitions_survive_and_missed_times_advance_without_firing(self):
        future = self.task(host_session_id="")
        future_at = self.task(host_session_id="", rule={"kind": "at", "at": (self.now + datetime.timedelta(days=1)).isoformat()})
        interval = self.task()
        daily = self.task(rule={"kind": "daily", "time": "09:00", "timezone": "Asia/Shanghai"})
        old_at = self.task(rule={"kind": "at", "at": (self.now + datetime.timedelta(hours=1)).isoformat()})
        yesterday = (self.now - datetime.timedelta(days=1)).isoformat()
        for task in (interval, daily, old_at):
            db.update("tasks", task["id"], {"next_due": yesterday})
        result = data_runtime.attach(self.root, force=True)
        self.assertEqual(scheduler.get(future["id"])["revision"], future["revision"])
        self.assertEqual(scheduler.get(future_at["id"])["next_due"], future_at["next_due"])
        for task in (interval, daily):
            current = scheduler.get(task["id"])
            self.assertTrue(current["enabled"])
            self.assertGreater(datetime.datetime.fromisoformat(current["next_due"]), self.now)
            self.assertEqual(current["revision"], task["revision"] + 1)
        self.assertFalse(scheduler.get(old_at["id"])["enabled"])
        self.assertEqual(scheduler.get(old_at["id"])["rule"], old_at["rule"])
        with self.assertRaisesRegex(ValueError, "其他会话修改"):
            scheduler.update(interval["id"], {"title": "stale overwrite", "expected_revision": interval["revision"]})
        scheduler.tick(self.now)
        self.assertEqual(db.rows("SELECT * FROM host_outbox"), [])
        archived = self.event(result["event_id"])[1]["tasks"]
        self.assertIn(old_at["id"], {row["id"] for row in archived})

    def test_staged_attachment_records_the_final_location_without_marker_rewrite(self):
        target = self.parent / "final-data"
        result = data_runtime.attach(self.root, force=True, location_root=target)
        marker = json.loads((self.root / ".local/runtime-location.json").read_bytes())
        self.assertEqual(marker["location_id"], runtime_identity.location_id(target))
        self.assertEqual(self.event(result["event_id"])[1]["location"]["location_id"], marker["location_id"])
        self.root.replace(target)
        self.assertEqual(data_runtime.attach(target), {"relocated": False})

    def candidate(self):
        flow = workflow.create(self.project, {"title": "Preserve real failure", "sources": [{"id": "s1", "text": "Priority must be high"}],
                                              "validation": {"repetitions": 2}}, idempotency_key="candidate-create")["workflow_id"]

        def submit(payload):
            task = workflow.next_task(flow, idempotency_key=uuid.uuid4().hex)["task"]
            workflow.submit(flow, task["id"], lease_token=task["lease_token"], attempt_id=task["attempt_id"], idempotency_key=uuid.uuid4().hex, payload=payload)

        submit({"requirements": [{"id": "r1", "text": "Priority high", "source_ids": ["s1"]}]})
        submit({"scenarios": [{"id": "sc1", "title": "Priority", "requirement_ids": ["r1"], "oracle_ids": ["o1"]}],
                "oracles": [{"id": "o1", "kind": "api", "requirement_ids": ["r1"], "checks": {"status": 200, "json_equals": {"/priority": "high"}}}]})
        cases = [builder.api_case(f"TC-API-00{number}", f"Priority {number}", "GET", "/priority", {"status": 200, "json_equals": {"/priority": "high"}})
                 for number in (1, 2)]
        submit({"cases": cases, "bindings": {"sc1": [case["id"] for case in cases]}})
        workflow.next_task(flow, idempotency_key="queue-candidate")
        return flow, db.one("SELECT * FROM workflow_items WHERE workflow_id=? AND phase='execute'", (flow,))

    def test_partial_real_fail_enters_investigation_and_never_automatically_repeats(self):
        class Crashed(BaseException):
            pass

        with patch.object(runner, "spawn") as spawn, patch.object(jobs, "ensure_worker") as dispatch:
            flow, item = self.candidate()
            original = runner.Session.run_one

            def interrupted(session, case):
                outcome = original(session, case)
                if case["id"] == "TC-API-002":
                    raise Crashed()
                return outcome

            with patch.object(runner.Session, "run_one", interrupted), self.assertRaises(Crashed):
                runner.execute(item["run_id"])
            run = db.one("SELECT * FROM runs WHERE id=?", (item["run_id"],))
            retained = db.one("SELECT * FROM results WHERE run_id=?", (run["id"],))
            self.assertEqual((run["status"], run["done"], run["failed"]), ("Running", 1, 1))
            self.assertTrue(execution_inputs.result_evidence(run, retained))
            operation = db.one("SELECT * FROM workflow_operations WHERE item_id=?", (item["id"],))
            spawn.reset_mock()
            dispatch.reset_mock()
            data_runtime.attach(self.root, force=True)
            investigation = workflow.next_task(flow, idempotency_key="after-relocation")["task"]
            self.assertEqual(investigation["phase"], "investigate")
            self.assertEqual(db.one("SELECT status FROM runs WHERE id=?", (run["id"],))["status"], "Fail")
            self.assertEqual(db.rows("SELECT * FROM results WHERE run_id=?", (run["id"],)), [retained])
            self.assertEqual(db.one("SELECT COUNT(*) n FROM runs")["n"], 1)
            self.assertEqual(Handler.calls - self.before_calls, 2)
            spawn.assert_not_called()
            # The new host investigation lease is monitored, without replaying
            # the original run or issuing another business request.
            dispatch.assert_called_once()
            with self.assertRaisesRegex(ValueError, "收据"):
                workflow.operation_receipt({"operation_id": operation["id"], "receipt_token": operation["receipt_token"]}, {"stale": True})

    def test_schedule_paths_and_idempotent_receipts_project_the_current_data_copy(self):
        spec = {"host_session_id": "", "idempotency_key": "same-definition"}
        original = self.task(**spec)
        stored = db.one("SELECT * FROM tasks WHERE id=?", (original["id"],))
        self.assertFalse(data_refs.is_absolute(stored["plan_path"]))
        receipt = db.one("SELECT response FROM schedule_requests WHERE request_key='same-definition'")["response"]
        self.assertFalse(data_refs.is_absolute(json.loads(receipt)["plan_path"]))
        marker = self.root / "data-root.json"
        data_transfer.write_json(marker, {"schema_version": 1, "data_id": "schedule-copy", "legacy_roots": [str(self.root)]})
        target = self.parent / "copied-data"
        shutil.copytree(self.root, target)
        os.environ["AUTOSKILL_WORKSPACE"] = str(target)
        current = scheduler.get(original["id"])
        self.assertTrue(Path(current["plan_path"]).is_relative_to(target))
        retry = self.task(**spec)
        self.assertEqual(retry["plan_path"], current["plan_path"])
        self.assertEqual(db.one("SELECT response FROM schedule_requests WHERE request_key='same-definition'")["response"], receipt)
        updated = scheduler.update(original["id"], {"title": "Edited in this copy", "expected_revision": current["revision"]})
        self.assertTrue(Path(updated["plan_path"]).is_relative_to(target))
        self.assertFalse(data_refs.is_absolute(db.one("SELECT plan_path FROM tasks WHERE id=?", (original["id"],))["plan_path"]))
        self.assertEqual(hashlib.sha256(marker.read_bytes()).digest(), hashlib.sha256((target / "data-root.json").read_bytes()).digest())


if __name__ == "__main__":
    unittest.main()
