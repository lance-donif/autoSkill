"""Host references preserve explicit selection and read existing task state without executing work."""
import base64
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "skills/autoskill/app"
sys.path.insert(0, str(APP))

from autoskill import builder, db, host_bindings, host_context, native_run, paging, paths, projects, runner, task_context, task_reports, workflow
from autoskill.server import jobs, scheduler
from autoskill.server.app import create_app

CHILD = """
import json, sys
from autoskill import host_bindings
try:
    result = getattr(host_bindings, sys.argv[1])(json.loads(sys.argv[2]))
except host_bindings.BindingError as error:
    result = {'error': error.code}
print(json.dumps(result))
"""


class HostContextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.folder / "workspace"), "AUTOSKILL_SESSION": "",
                                                   "AUTOSKILL_CASE_SKILL": str(self.folder / "sheet"), "AUTOSKILL_HEADED": "0"})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def project(self):
        project_id = projects.create("Host fixture", "http://127.0.0.1:9", project_id="host-project")["id"]
        projects.save_cases(project_id, [builder.api_case("TC-API-001", "Synthetic request", "GET", "/", {"status": 200, "json_equals": {"/ok": True}})])
        return project_id

    def bind(self, task="task-A", session="host-A", **values):
        return host_bindings.upsert({"host": "dsh", "host_session_id": session, "task_session_id": task,
                                     "idempotency_key": "bind-" + task, **values})["binding"]

    def select(self, binding, revision=0, key="select"):
        return host_bindings.select({"host": "dsh", "host_session_id": binding["host_session_id"], "binding_id": binding["id"],
                                     "expected_revision": revision, "idempotency_key": key})

    @staticmethod
    def context(session="host-A", **options):
        return host_context.read("dsh", session, **options)

    def child(self, action, body):
        result = subprocess.run([sys.executable, "-c", CHILD, action, json.dumps(body)], cwd=ROOT,
                                env={**os.environ, "PYTHONPATH": str(APP)}, capture_output=True, text=True, timeout=20, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    @staticmethod
    def logical_state():
        with host_bindings.snapshot() as connection:
            names = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            return {name: [tuple(row) for row in connection.execute(f'SELECT * FROM "{name}" ORDER BY rowid')] for name in names}

    @staticmethod
    def files():
        return {str(file.relative_to(paths.workspace())): hashlib.sha256(file.read_bytes()).hexdigest()
                for file in paths.workspace().rglob("*") if file.is_file() and not file.name.startswith("autoskill.db")}

    def test_empty_reads_do_not_create_workspace_or_select_a_task(self):
        self.assertFalse(paths.workspace().exists())
        for read in (self.context, lambda: host_bindings.list_bindings("dsh", "host-A"),
                     lambda: host_bindings.request_receipt("dsh", "host-A", kind="binding", key="missing")):
            read()
            self.assertFalse(paths.workspace().exists())
        first = self.bind()
        self.assertEqual(self.context()["status"], "selection_required")
        self.assertEqual(self.context()["bindings"]["items"][0]["id"], first["id"])
        self.assertEqual(self.context("host-B")["bindings"]["page"]["total"], 0)
        second = self.bind("task-B")
        self.select(first)
        self.assertEqual(self.context()["binding"]["id"], first["id"])
        self.assertEqual(self.context(binding_id=second["id"])["binding"]["id"], second["id"])
        self.assertEqual(self.context()["selection"]["binding_id"], first["id"])
        with self.assertRaises(host_bindings.BindingError):
            self.context("host-B", binding_id=first["id"])

    def test_reading_an_old_database_does_not_add_schema_or_files(self):
        local = paths.workspace() / ".local"
        local.mkdir(parents=True)
        with sqlite3.connect(local / "autoskill.db") as connection:
            connection.execute("CREATE TABLE existing_note(value TEXT)")
            connection.execute("INSERT INTO existing_note VALUES('preserved')")
        before = {file.name: file.read_bytes() for file in local.iterdir()}
        self.assertEqual(self.context()["status"], "selection_required")
        self.assertEqual({file.name: file.read_bytes() for file in local.iterdir()}, before)

    def test_binding_storage_is_workspace_local_and_portable_without_machine_identity(self):
        binding = self.bind("会话 / task", label="Portable references")
        self.select(binding)
        original = self.context()
        portable = self.folder / "copied-data"
        shutil.copytree(paths.workspace(), portable)
        with patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.folder / "unrelated-data")}):
            self.assertEqual(self.context()["status"], "selection_required")
            self.assertFalse(paths.workspace().exists())
        with patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(portable)}):
            self.assertEqual(self.context(), original)
        self.assertNotIn(str(self.folder), json.dumps(binding))

    def test_binding_receipts_replay_and_conflicting_payload_does_not_mutate(self):
        body = {"host": "dsh", "host_session_id": "host-A", "task_session_id": "", "idempotency_key": "same"}
        first = host_bindings.upsert(body)
        self.assertEqual(host_bindings.upsert(body), first)
        self.assertEqual(host_bindings.request_receipt("dsh", "host-A", kind="binding", key="same")["response"], first)
        self.assertEqual(host_bindings.request_receipt("dsh", "host-B", kind="binding", key="same")["state"], "missing")
        with self.assertRaises(host_bindings.BindingError) as raised:
            host_bindings.upsert({**body, "label": "Different intent"})
        self.assertEqual(raised.exception.code, "idempotency_conflict")
        self.assertEqual(self.context()["bindings"]["page"]["total"], 1)

    def test_real_processes_share_idempotency_and_fence_conflicting_updates(self):
        body = {"host": "dsh", "host_session_id": "host-A", "task_session_id": "task-A", "idempotency_key": "concurrent"}
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(lambda _: self.child("upsert", body), range(2)))
        self.assertEqual(replies[0], replies[1])
        binding = replies[0]["binding"]
        updates = [{**body, "binding_id": binding["id"], "expected_revision": 1, "label": label, "idempotency_key": label}
                   for label in ("Left", "Right")]
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(lambda value: self.child("upsert", value), updates))
        self.assertEqual(sum(reply.get("error") == "revision_conflict" for reply in replies), 1)
        saved = self.context(binding_id=binding["id"])["binding"]
        self.assertEqual(saved["revision"], 2)
        self.assertEqual(saved["label"], next(reply["binding"]["label"] for reply in replies if "binding" in reply))

    def test_real_process_selection_cas_and_old_receipt_do_not_overwrite_new_selection(self):
        first, second = self.bind(), self.bind("task-B")
        requests = [{"host": "dsh", "host_session_id": "host-A", "binding_id": item["id"], "expected_revision": 0,
                     "idempotency_key": item["id"]} for item in (first, second)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(lambda value: self.child("select", value), requests))
        self.assertEqual(sum(reply.get("error") == "revision_conflict" for reply in replies), 1)
        chosen = next(reply["selection"] for reply in replies if "selection" in reply)
        self.assertEqual(self.context()["selection"], chosen)
        other = first if chosen["binding_id"] == second["id"] else second
        self.select(other, revision=1, key="later-selection")
        original = next(body for body in requests if body["binding_id"] == chosen["binding_id"])
        self.assertEqual(host_bindings.select(original)["selection"], chosen)
        self.assertEqual(self.context()["selection"]["binding_id"], other["id"])

    def test_binding_pages_are_scope_bound_and_expire_after_actual_revision(self):
        records = [self.bind("task-" + str(index)) for index in range(7)]
        page = host_bindings.list_bindings("dsh", "host-A", limit=3)["bindings"]
        self.assertEqual((page["page"]["total"], len(page["items"])), (7, 3))
        following = host_bindings.list_bindings("dsh", "host-A", limit=3, cursor=page["page"]["next_cursor"])["bindings"]
        self.assertTrue({row["id"] for row in page["items"]}.isdisjoint(row["id"] for row in following["items"]))
        with self.assertRaises(paging.PageError):
            host_bindings.list_bindings("dsh", "host-B", cursor=page["page"]["next_cursor"])
        host_bindings.upsert({"host": "dsh", "host_session_id": "host-A", "binding_id": records[0]["id"], "label": "Changed",
                              "expected_revision": 1, "idempotency_key": "update"})
        with self.assertRaises(paging.PageError) as raised:
            host_bindings.list_bindings("dsh", "host-A", cursor=page["page"]["next_cursor"])
        self.assertEqual(raised.exception.code, "cursor_expired")

    def test_context_is_read_only_and_revision_is_stable_until_scoped_state_changes(self):
        project = self.project()
        flow = workflow.create(project, {"title": "Workflow", "sources": [{"id": "source", "text": "Expected business output"}]}, idempotency_key="flow")
        workflow.next_task(flow["workflow_id"], idempotency_key="claim")
        binding = self.bind(project_id=project, workflow_id=flow["workflow_id"])
        self.select(binding)
        before, files = self.logical_state(), self.files()
        with patch.object(workflow, "status", side_effect=AssertionError("Must not reconcile")), \
                patch.object(task_reports, "for_run", side_effect=AssertionError("Must not export")), \
                patch.object(task_reports, "for_workflow", side_effect=AssertionError("Must not export")), \
                patch.object(native_run, "get", side_effect=AssertionError("Must not load execution")), \
                patch.object(db, "init", side_effect=AssertionError("Must not migrate")):
            first, second = self.context(), self.context()
        self.assertEqual(first, second)
        self.assertEqual(self.logical_state(), before)
        self.assertEqual(self.files(), files)
        self.bind("task-other", session="host-B")
        self.assertEqual(self.context()["context_revision"], first["context_revision"])
        self.assertEqual(first["pending"]["workflow_items"], 1)
        self.assertIsNone(first["cli_context"]["workflow_binding"])

    def test_workflow_runs_follow_cycle_links_and_old_cycle_never_follows_revise(self):
        project = self.project()
        flow = workflow.create(project, {"title": "Workflow", "sources": [{"id": "source", "text": "Original requirement"}]}, idempotency_key="flow")
        run = runner.create(project, session_id=flow["workflow_id"])
        with db.connect() as connection:
            connection.execute("UPDATE workflow_items SET run_id=? WHERE workflow_id=?", (run["id"], flow["workflow_id"]))
        binding = self.bind(project_id=project, workflow_id=flow["workflow_id"])
        self.select(binding)
        first = self.context()
        self.assertEqual(first["runs"]["items"][0]["id"], run["id"])
        self.assertNotEqual(run["session_id"], binding["task_session_id"])
        workflow.revise(flow["workflow_id"], {"sources": [{"id": "source2", "text": "New requirement"}]}, idempotency_key="revise")
        later = self.context()
        self.assertEqual(later["status"], "stale_cycle")
        self.assertEqual(later["workflow"]["cycle_id"], first["workflow"]["cycle_id"])
        self.assertNotEqual(later["workflow"]["cycle_id"], later["workflow"]["current_cycle_id"])
        self.assertEqual(later["reports"][0]["report_id"], first["reports"][0]["report_id"])
        self.assertIsNone(later["workflow"]["conclusion"])

    def test_legacy_session_does_not_claim_all_unnamed_runs_and_reports_remain_unverified(self):
        project = self.project()
        run = runner.create(project, session_id="")
        legacy = self.bind("", project_id=project)
        self.select(legacy)
        self.assertEqual(self.context()["runs"]["page"]["total"], 0)
        explicit = self.bind("direct", project_id=project, run_id=run["id"])
        folder = paths.workspace() / ".local" / "runs" / run["id"] / "test-report"
        folder.mkdir(parents=True)
        report_id = "run_" + run["id"]
        receipt = {"report_id": report_id, "kind": "run", "task_id": run["id"], "status": "ready", "excel": str(folder / "missing.xlsx"), "sha256": "declared"}
        file = folder / "receipt.json"
        file.write_text(json.dumps(receipt), encoding="utf-8")
        db.update("runs", run["id"], {"status": "Pass"})
        original = file.read_bytes()
        revision = db.one("SELECT revision FROM state_revision WHERE id=1")["revision"]
        result = self.context(binding_id=explicit["id"])
        self.assertEqual(result["reports"][0]["verification"], "invalid")
        self.assertEqual(result["reports"][0]["receipt_status"], "failed")
        self.assertEqual(file.read_bytes(), original)
        self.assertEqual(sorted(path.name for path in folder.iterdir()), ["receipt.json"])
        self.assertEqual(db.one("SELECT revision FROM state_revision WHERE id=1")["revision"], revision)
        self.assertEqual(task_reports.get(report_id)["status"], "failed")
        db.execute("DELETE FROM runs WHERE id=?", (run["id"],))
        result = self.context(binding_id=explicit["id"])
        self.assertEqual(result["status"], "reference_missing")
        self.assertEqual(result["reports"], [])

    def test_project_reports_are_verified_without_export_or_persistence(self):
        project = self.project()
        run = runner.create(project, session_id="task-A")
        finished = jobs.cancel(run["id"])
        self.assertEqual(finished["test_report"]["status"], "ready")
        self.assertTrue(Path(finished["test_report"]["excel"]).is_relative_to(projects.path(project) / "reports"))
        binding = self.bind(project_id=project, run_id=run["id"])
        before, files = self.logical_state(), self.files()
        with patch.object(task_reports, "for_run", side_effect=AssertionError("Reading must not export")):
            result = self.context(binding_id=binding["id"])
        report = result["reports"][0]
        self.assertEqual((report["receipt_status"], report["verification"]), ("ready", "verified"))
        self.assertEqual({item["format"] for item in report["artifacts"]}, {"excel", "markdown", "bugs"})
        self.assertEqual(self.logical_state(), before)
        self.assertEqual(self.files(), files)
        for kind in ("excel", "markdown", "bugs"):
            with self.subTest(artifact=kind):
                artifact = Path(finished["test_report"][kind])
                original = artifact.read_bytes()
                artifact.write_bytes(original + b"tampered")
                facts, files = self.logical_state(), self.files()
                with patch.object(task_reports, "for_run", side_effect=AssertionError("Reading must not repair artifacts")):
                    invalid = self.context(binding_id=binding["id"])["reports"][0]
                self.assertEqual((invalid["receipt_status"], invalid["verification"]), ("failed", "invalid"))
                self.assertEqual(self.logical_state(), facts)
                self.assertEqual(self.files(), files)
                artifact.write_bytes(original)
        index = paths.local() / "task-report-locations" / ("run_" + run["id"] + ".json")
        index.write_text(json.dumps({"project_id": project, "folder": str(self.folder / "outside")}), encoding="utf-8")
        files = self.files()
        unavailable = self.context(binding_id=binding["id"])["reports"][0]
        self.assertEqual(unavailable["receipt_status"], "unavailable")
        self.assertEqual(self.files(), files)

    def test_run_pages_are_bounded_and_context_changes_with_progress(self):
        project = self.project()
        rows = [runner.create(project, session_id="task-A") for _ in range(11)]
        other = runner.create(project, session_id="task-B")
        binding = self.bind(project_id=project)
        self.select(binding)
        first = self.context(runs_limit=3)
        self.assertEqual((len(first["runs"]["items"]), first["runs"]["page"]["total"]), (3, 11))
        self.assertTrue(all(row["can_cancel"] for row in first["runs"]["items"]))
        following = self.context(runs_limit=3, runs_cursor=first["runs"]["page"]["next_cursor"])
        self.assertNotIn(other["id"], {row["id"] for row in following["runs"]["items"]})
        db.execute("UPDATE runs SET status='Running',done=1 WHERE id=?", (rows[-1]["id"],))
        self.assertNotEqual(self.context(runs_limit=3)["context_revision"], first["context_revision"])

    def test_a_read_transaction_does_not_mix_a_concurrent_run_update(self):
        project = self.project()
        run = runner.create(project, session_id="task-A")
        self.select(self.bind(project_id=project))
        original = host_context._runs

        def update_during_read(*args, **kwargs):
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(db.execute, "UPDATE runs SET status='Running',done=1 WHERE id=?", (run["id"],)).result(timeout=10)
            return original(*args, **kwargs)

        with patch.object(host_context, "_runs", side_effect=update_during_read):
            before = self.context()
        after = self.context()
        self.assertEqual((before["runs"]["items"][0]["status"], before["runs"]["items"][0]["done"]), ("Queued", 0))
        self.assertEqual((after["runs"]["items"][0]["status"], after["runs"]["items"][0]["done"]), ("Running", 1))
        self.assertNotEqual(before["context_revision"], after["context_revision"])

    def test_http_contract_and_exact_request_lookup_do_not_renew_workflow_leases(self):
        project = self.project()
        flow = workflow.create(project, {"title": "HTTP workflow", "sources": [{"id": "source", "text": "Requirements"}]}, idempotency_key="flow")
        claimed = workflow.next_task(flow["workflow_id"], idempotency_key="original")
        with TestClient(create_app(start_worker=False), base_url="http://127.0.0.1:8787") as client:
            body = {"host": "dsh", "host_session_id": "A/中文", "task_session_id": "task with spaces", "workflow_id": flow["workflow_id"], "idempotency_key": "bind"}
            result = client.post("/api/host/bindings", json=body)
            self.assertEqual(result.status_code, 200, result.text)
            item = result.json()["binding"]
            args = {"host": "dsh", "host_session_id": "A/中文"}
            self.assertEqual(client.get("/api/host/context", params=args).json()["status"], "selection_required")
            select = client.post("/api/host/bindings/select", json={**args, "binding_id": item["id"], "expected_revision": 0, "idempotency_key": "select"})
            self.assertEqual(select.status_code, 200)
            self.assertEqual(client.get("/api/host/context", params=args).json()["binding"]["id"], item["id"])
            original = client.get("/api/host/requests", params={**args, "kind": "workflow", "scope": flow["workflow_id"], "key": "original"})
            self.assertEqual(original.json()["response"], claimed)
            self.assertEqual(original.json()["state"], "completed")
            before = self.logical_state()
            client.get("/api/host/requests", params={**args, "kind": "workflow", "scope": flow["workflow_id"], "key": "missing"})
            client.get("/api/host/context", params=args)
            self.assertEqual(self.logical_state(), before)
            bad = client.post("/api/host/bindings/select", json={**args, "binding_id": item["id"], "expected_revision": 0, "idempotency_key": "stale"})
            self.assertEqual((bad.status_code, bad.json()["code"]), (400, "revision_conflict"))

    def test_utf8_session_headers_override_plain_headers_and_keep_json_host_identity(self):
        app = create_app(start_worker=False)

        @app.get("/test-session-identity")
        def selected_session():
            return {"session_id": task_context.session_id()}

        app.router.routes.insert(0, app.router.routes.pop())  # Place the fixture before the dashboard's static fallback.

        def encoded(text):
            return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")

        headers = {"X-Autoskill-Session-B64": encoded("任务甲 / 🙂"), "X-Autoskill-Session": "obsolete",
                   "X-Autoskill-Host-Session-B64": encoded("宿主甲"), "X-Autoskill-Host-Session": "obsolete-host"}
        with TestClient(app, base_url="http://127.0.0.1:8787") as client:
            self.assertEqual(client.get("/test-session-identity", headers=headers).json()["session_id"], "任务甲 / 🙂")
            self.assertIsNone(client.get("/test-session-identity", headers={**headers, "X-Autoskill-Session-B64": ""}).json()["session_id"])
            self.assertEqual(client.get("/test-session-identity", headers={"X-Autoskill-Session": "legacy"}).json()["session_id"], "legacy")
            with patch.object(scheduler, "create", side_effect=lambda spec: spec), patch.object(jobs, "ensure_worker", return_value={}):
                self.assertEqual(client.post("/api/tasks", headers=headers, json={}).json()["host_session_id"], "宿主甲")
            with patch.object(native_run, "enqueue", return_value={"id": "synthetic"}) as enqueue, patch.object(jobs, "ensure_worker", return_value={}):
                client.post("/api/native-runs", headers=headers, json={"project_id": "fixture", "asset_id": "asset"})
                self.assertEqual(enqueue.call_args.kwargs["host_session_id"], "宿主甲")
                client.post("/api/native-runs", headers=headers, json={"host_session_id": "正文显式身份"})
                self.assertEqual(enqueue.call_args.kwargs["host_session_id"], "正文显式身份")
            for bad in ("invalid!", "_w"):
                result = client.get("/test-session-identity", headers={"X-Autoskill-Session-B64": bad})
                self.assertEqual((result.status_code, result.json()["code"]), (400, "validation_error"))


if __name__ == "__main__":
    unittest.main()
