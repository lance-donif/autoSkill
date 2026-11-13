"""Deletion preserves unrelated definitions, fences live executors and retries incomplete cleanup."""
import copy
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "skills/autoskill/app"
sys.path.insert(0, str(APP))

from autoskill import builder, db, deletion, dialogue_judge, domain, execution_inputs, native_assets, native_run, paths, projects, reporting, runner
from autoskill import workflow_state as state
from autoskill.server import jobs, metrics, scheduler
from autoskill.server.app import create_app


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/slow":
            self.server.entered.set()
            self.server.release.wait(timeout=10)
        data = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class DeletionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.entered, cls.server.release = threading.Event(), threading.Event()
        threading.Thread(target=cls.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.release.set()
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.folder / "workspace"), "AUTOSKILL_CASE_SKILL": str(self.folder / "plan"),
                                              "AUTOSKILL_BRIDGE": "off", "AUTOSKILL_HEADED": "0", "AUTOSKILL_SESSION": ""})
        environment.start()
        self.addCleanup(environment.stop)
        self.server.entered.clear()
        self.server.release.clear()
        self.pid = projects.create("删除验证", f"http://127.0.0.1:{self.server.server_port}", project_id="deletion")["id"]
        self.case = builder.api_case("TC-API-001", "业务断言", "GET", "/ok", {"status": 200, "json_equals": {"/ok": True}})
        projects.save_cases(self.pid, [self.case])
        self.case = projects.case(self.pid, self.case["id"])

    def finished(self, **options):
        run = runner.create(self.pid, [self.case["id"]], **options)
        return runner.execute(run["id"])

    def stop_child(self, pid):
        self.server.release.set()
        child = runner._children.get(pid)
        if child:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.terminate()
                child.wait(timeout=10)
        runner.reap_children()

    def sheet(self):
        file = self.folder / "plan/cases.xlsx"
        file.parent.mkdir(parents=True, exist_ok=True)
        book = Workbook()
        book.active.append(["用例编号", "执行结果", "回放用例", "预期结果", "用例标题"])
        book.active.append(["P-1", "通过", f"{self.pid}/{self.case['id']}", "独立业务预期", "保留设计"])
        book.save(file)
        book.close()
        return file

    def test_case_delete_preserves_other_contracts_frozen_history_and_auto_ids(self):
        head = domain.commit(self.pid, {"requirements/r.json": {"expected": True}}, None)["head"]
        run = self.finished()
        result = db.one("SELECT * FROM results WHERE run_id = ?", (run["id"],))
        dependent = builder.api_case("TC-API-002", "显式依赖", "GET", "/ok", {"status": 200, "json_equals": {"/ok": True}}, depends_on=[self.case["id"]])
        projects.save_cases(self.pid, [dependent])
        kept = projects.case(self.pid, dependent["id"])
        projects.set_auth(self.pid, {"api_case": self.case["id"], "custom": "preserved"})
        deleted = deletion.delete_cases([{"project_id": self.pid, "case_id": self.case["id"]}])
        self.assertFalse(deleted["errors"])
        self.assertEqual(projects.cases(self.pid), [kept])
        self.assertEqual(projects.auth(self.pid), {"custom": "preserved"})
        self.assertIsNone(db.one("SELECT * FROM cases WHERE project_id = ? AND case_id = ?", (self.pid, self.case["id"])))
        self.assertTrue(execution_inputs.result_evidence(run, result, self.case))
        self.assertEqual(domain.read(self.pid)["head"], head)
        with self.assertRaisesRegex(ValueError, "前置用例"):
            runner.prepare(self.pid, [dependent["id"]])
        deletion.delete_cases([{"project_id": self.pid, "case_id": dependent["id"]}])
        self.assertEqual(projects.next_case_id(self.pid, "api"), "TC-API-003")
        db.execute("DELETE FROM deleted_cases")
        self.assertEqual(projects.next_case_id(self.pid, "api"), "TC-API-003")
        projects.save_cases(self.pid, [self.case])
        self.assertEqual(projects.case(self.pid, self.case["id"])["id"], self.case["id"])

    def test_case_deletion_refreshes_sheet_without_editing_expected_value(self):
        file = self.sheet()
        result = deletion.delete_cases([{"project_id": self.pid, "case_id": self.case["id"]}])
        self.assertEqual(result["deleted"][0]["warnings"], [])
        book = load_workbook(file)
        self.addCleanup(book.close)
        self.assertEqual(book.active.cell(2, 2).value, "未执行")
        self.assertEqual(book.active.cell(2, 4).value, "独立业务预期")
        self.assertNotIn(self.case["id"], (projects.path(self.pid) / "SKILL.md").read_text())

    def test_finished_run_deletes_files_results_and_stats_but_keeps_cases_and_domain(self):
        domain.commit(self.pid, {"source.bin": b"immutable source"}, None)
        before = copy.deepcopy(projects.cases(self.pid))
        run = self.finished()
        folder = runner.evidence_dir(run["id"])
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "screenshot.png").write_bytes(b"synthetic")
        manifest = execution_inputs.load(run)
        report = reporting.build(self.pid)
        response = deletion.delete_runs([run["id"]], start_worker=False)
        self.assertEqual([item["run_id"] for item in response["deleted"]], [run["id"]])
        self.assertEqual(response["errors"], [])
        self.assertIsNone(db.one("SELECT * FROM runs WHERE id = ?", (run["id"],)))
        self.assertFalse(db.rows("SELECT * FROM results WHERE run_id = ?", (run["id"],)))
        self.assertFalse(folder.exists())
        self.assertFalse(Path(run["input_manifest"]).exists())
        for ref in manifest["sources"]["files"].values():
            self.assertFalse((paths.local() / "execution-inputs/objects" / ref["sha256"]).exists())
        self.assertFalse(Path(report["excel"]).exists())
        self.assertEqual(projects.cases(self.pid), before)
        self.assertEqual(domain.read_file(self.pid, "source.bin"), b"immutable source")
        self.assertEqual(metrics.run_counts(metrics.Scope())["total"], 0)
        self.assertTrue(deletion.delete_runs([run["id"]], start_worker=False)["deleted"][0]["already_deleted"])

    def test_shared_objects_and_prepared_unqueued_manifest_survive_other_run_deletion(self):
        domain.commit(self.pid, {"source.bin": b"shared"}, None)
        old = self.finished()
        prepared = runner.prepare(self.pid)
        refs = execution_inputs.load(prepared)["sources"]["files"]
        response = deletion.delete_runs([old["id"]], start_worker=False)
        self.assertFalse(response["errors"])
        self.assertTrue(Path(prepared["input_manifest"]).is_file())
        for ref in refs.values():
            self.assertTrue((paths.local() / "execution-inputs/objects" / ref["sha256"]).is_file())
        runner.enqueue(prepared)
        self.assertEqual(runner.execute(prepared["id"])["status"], "Pass")

    def test_prepare_in_another_process_and_deletion_gc_interleave_without_losing_inputs(self):
        domain.commit(self.pid, {"source.bin": b"concurrent source"}, None)
        old = self.finished()
        ready, release, receipt = (self.folder / name for name in ("ready", "release", "prepared.json"))
        script = (
            "import json,sys,time; from pathlib import Path; from autoskill import execution_inputs,runner; "
            "ready,release,receipt=map(Path,sys.argv[1:]); original=execution_inputs.write_json\n"
            "def pause(file,value):\n"
            " ready.touch()\n"
            " deadline=time.monotonic()+15\n"
            " while not release.exists() and time.monotonic()<deadline: time.sleep(.01)\n"
            " original(file,value)\n"
            "execution_inputs.write_json=pause\n"
            "receipt.write_text(json.dumps(runner.prepare('deletion')))\n"
        )
        child = subprocess.Popen([sys.executable, "-c", script, str(ready), str(release), str(receipt)],
                                 env={**os.environ, "PYTHONPATH": str(APP)}, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.addCleanup(child.stderr.close)
        try:
            deadline = time.monotonic() + 10
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists())
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(deletion.delete_runs, [old["id"]], start_worker=False)
                time.sleep(0.05)
                self.assertFalse(future.done())
                release.touch()
                result = future.result(timeout=20)
            self.assertEqual(child.wait(timeout=10), 0, child.stderr.read().decode())
            self.assertEqual(result["errors"], [])
            prepared = json.loads(receipt.read_text())
            self.assertTrue(execution_inputs.load(prepared)["sources"]["files"])
            runner.enqueue(prepared)
            self.assertEqual(runner.execute(prepared["id"])["status"], "Pass")
        finally:
            release.touch()
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=10)

    def test_object_directory_symlink_does_not_delete_external_content(self):
        run = self.finished()
        digest = next(iter(execution_inputs.load(run)["sources"]["files"].values()))["sha256"]
        objects = paths.local() / "execution-inputs/objects"
        saved = objects.with_name("original-objects")
        objects.rename(saved)
        external = self.folder / "external-objects"
        external.mkdir()
        # The external target is never read as execution evidence or followed by cleanup.
        (external / digest).write_bytes(b"keep")
        objects.symlink_to(external, target_is_directory=True)
        result = deletion.delete_runs([run["id"]], start_worker=False)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual((external / digest).read_bytes(), b"keep")
        self.assertTrue(Path(run["input_manifest"]).exists())
        objects.unlink()
        saved.rename(objects)
        self.assertEqual(len(deletion.delete_runs([run["id"]], start_worker=False)["deleted"]), 1)

    def test_native_manifests_are_independent_even_for_identical_frozen_content(self):
        file = self.folder / "ai.json"
        file.write_text(json.dumps({"endpoint": {"url": f"http://127.0.0.1:{self.server.server_port}/ok"}, "samples": [{"id": "a", "question": "q"}]}))
        asset = native_assets.import_asset(self.pid, str(file), "ai-eval")
        first = native_run.prepare(self.pid, asset["id"])
        prepared = native_run.prepare(self.pid, asset["id"])
        self.assertEqual(first["input_hash"], prepared["input_hash"])
        self.assertNotEqual(first["input_manifest"], prepared["input_manifest"])
        runner.enqueue(first)
        response = deletion.delete_runs([first["id"]], start_worker=False)
        self.assertEqual(response["errors"], [])
        self.assertFalse(Path(first["input_manifest"]).exists())
        self.assertTrue(Path(prepared["input_manifest"]).is_file())
        manifest = native_run._frozen(prepared)
        for ref in manifest["frozen_files"]:
            self.assertTrue((paths.local() / "native-inputs/objects" / ref["sha256"]).exists())
        self.assertEqual(native_assets.get(self.pid, asset["id"])["asset_version"], asset["asset_version"])

    def test_legacy_shared_manifest_is_removed_only_after_last_referencing_run(self):
        run = runner.create(self.pid)
        original = Path(run["input_manifest"])
        legacy = original.with_name(run["input_hash"] + ".json")
        original.replace(legacy)
        db.update("runs", run["id"], {"input_manifest": str(legacy)})
        other = {**run, "id": db.new_id("run"), "input_manifest": str(legacy)}
        runner.enqueue(other)
        self.assertFalse(deletion.delete_runs([run["id"]], start_worker=False)["errors"])
        self.assertTrue(legacy.exists())
        self.assertFalse(deletion.delete_runs([other["id"]], start_worker=False)["errors"])
        self.assertFalse(legacy.exists())

    def test_batch_file_failure_is_per_item_and_persistent_cleanup_retries(self):
        first, second = self.finished(), self.finished()
        broken = runner.evidence_dir(first["id"])
        original = deletion.shutil.rmtree

        def deny(path, *args, **kwargs):
            if Path(path) == broken:
                raise PermissionError("合成文件占用")
            return original(path, *args, **kwargs)

        with patch.object(deletion.shutil, "rmtree", side_effect=deny):
            result = deletion.delete_runs([first["id"], second["id"]], start_worker=False)
        self.assertEqual([item["run_id"] for item in result["deleted"]], [second["id"]])
        self.assertEqual([item["run_id"] for item in result["errors"]], [first["id"]])
        self.assertTrue(result["errors"][0]["retryable"])
        self.assertIn("占用", db.one("SELECT deletion_error FROM runs WHERE id = ?", (first["id"],))["deletion_error"])
        jobs.sweep()
        self.assertIsNone(db.one("SELECT id FROM runs WHERE id = ?", (first["id"],)))

    def test_cleanup_interruption_after_input_unlink_retains_object_inventory(self):
        domain.commit(self.pid, {"source.bin": b"to delete"}, None)
        run = self.finished()
        objects = execution_inputs.load(run)["sources"]["files"]
        with patch.object(deletion, "_reports", side_effect=KeyboardInterrupt("synthetic crash")), self.assertRaises(KeyboardInterrupt):
            deletion.delete_runs([run["id"]], start_worker=False)
        self.assertTrue(db.one("SELECT deletion_manifest FROM runs WHERE id = ?", (run["id"],))["deletion_manifest"])
        jobs.sweep()
        self.assertIsNone(db.one("SELECT id FROM runs WHERE id = ?", (run["id"],)))
        for ref in objects.values():
            self.assertFalse((paths.local() / "execution-inputs/objects" / ref["sha256"]).exists())

    def test_task_last_run_and_sheet_revert_after_last_evidence_is_deleted(self):
        db.insert("tasks", {"id": "task-fixture", "project_id": self.pid, "title": "保留任务", "engine": "playwright", "rule": {},
                            "enabled": 0, "created_at": db.now(), "updated_at": db.now()})
        first, last = self.finished(task_id="task-fixture"), self.finished(task_id="task-fixture")
        db.update("tasks", "task-fixture", {"last_run_id": last["id"]})
        db.insert("schedule_occurrences", {"id": "occurrence-fixture", "task_id": "task-fixture", "revision": 1, "due_at": "fixture", "status": "Pass",
                                           "run_id": last["id"], "created_at": db.now()})
        file = self.sheet()
        self.assertFalse(deletion.delete_runs([last["id"]], start_worker=False)["errors"])
        self.assertEqual(scheduler.get("task-fixture")["last_run_id"], first["id"])
        occurrence = db.one("SELECT * FROM schedule_occurrences WHERE id = 'occurrence-fixture'")
        self.assertIsNone(occurrence["run_id"])
        self.assertEqual(occurrence["status"], "deleted")
        self.assertFalse(deletion.delete_runs([first["id"]], start_worker=False)["errors"])
        self.assertIsNone(scheduler.get("task-fixture")["last_run_id"])
        book = load_workbook(file)
        self.addCleanup(book.close)
        self.assertEqual(book.active.cell(2, 2).value, "未执行")

    def test_judge_and_waiting_workflow_are_detached_without_erasing_domain_history(self):
        run = self.finished()
        state.init()
        db.insert("workflow_runtime", {"id": "flow", "project_id": self.pid, "title": "流程", "cycle_id": "cycle", "created_at": db.now(), "updated_at": db.now()})
        db.insert("workflow_items", {"id": "item", "workflow_id": "flow", "sequence": 1, "cycle_id": "cycle", "phase": "execute", "state": "waiting_execution",
                                     "run_id": run["id"], "created_at": db.now()})
        request = dialogue_judge.submit_request({"run_id": run["id"]}, {"id": self.case["id"]}, 0, "问题", [], "回答", {}, {}, idempotency_key="request")
        dialogue_judge.next_judgement(idempotency_key="claim", owner="deletion-test", run_id=run["id"])
        response = deletion.delete_runs([run["id"]], start_worker=False)
        self.assertEqual(response["errors"], [])
        self.assertFalse(db.rows("SELECT * FROM dialogue_judgements"))
        self.assertFalse(db.rows("SELECT * FROM dialogue_judge_attempts"))
        self.assertFalse(db.rows("SELECT * FROM host_outbox WHERE occurrence_id = ?", (request["judge_id"],)))
        self.assertFalse(db.rows("SELECT * FROM workflow_requests WHERE scope LIKE 'judge-%'"))
        item = db.one("SELECT * FROM workflow_items WHERE id = 'item'")
        self.assertEqual(item["state"], "cancelled")
        self.assertTrue(json.loads(item["context"])["evidence_deleted"])
        self.assertEqual(db.one("SELECT * FROM workflow_runtime WHERE id = 'flow'")["lifecycle"], "paused")
        with self.assertRaisesRegex(ValueError, "已删除"):
            dialogue_judge.submit_request({"run_id": run["id"]}, {"id": self.case["id"]}, 1, "迟到", [], "回答", {}, {}, idempotency_key="late")

    def test_active_real_child_cancels_then_deletes_without_late_results(self):
        slow = copy.deepcopy(self.case)
        slow["path"] = "/slow"
        projects.save_cases(self.pid, [slow])
        run = runner.create(self.pid)
        pid = runner.spawn(run["id"])
        self.assertIsNotNone(pid)
        self.addCleanup(self.stop_child, pid)
        self.addCleanup(self.server.release.set)
        self.assertTrue(self.server.entered.wait(timeout=15))
        response = deletion.delete_runs([run["id"]], start_worker=False)
        self.assertEqual([item["run_id"] for item in response["pending"]], [run["id"]])
        self.assertEqual(metrics.runs(metrics.Scope())["items"][0]["delete_requested"], 1)
        self.assertTrue(Path(run["input_manifest"]).exists())
        self.server.release.set()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            jobs.sweep()
            if not runner.process_alive(pid) and not db.one("SELECT id FROM runs WHERE id = ?", (run["id"],)):
                break
            time.sleep(0.02)
        self.assertFalse(runner.process_alive(pid))
        self.assertIsNone(db.one("SELECT id FROM runs WHERE id = ?", (run["id"],)))
        self.assertFalse(db.rows("SELECT * FROM results WHERE run_id = ?", (run["id"],)))
        self.assertFalse(runner.evidence_dir(run["id"]).exists())

    def test_native_process_tree_exits_before_its_evidence_is_removed(self):
        source = self.folder / "native"
        source.mkdir()
        (source / "test_slow.py").write_text("import time,unittest\nclass Slow(unittest.TestCase):\n def test_wait(self):\n  time.sleep(30)\n")
        asset = native_assets.import_asset(self.pid, str(source), "selenium-unittest", argv=["python", "-m", "unittest", "discover", "-v"])
        run = native_run.enqueue(self.pid, asset["id"])
        owner = runner.spawn(run["id"])
        self.addCleanup(self.stop_child, owner)
        heartbeat = runner.evidence_dir(run["id"]) / "native/heartbeat.json"
        deadline = time.monotonic() + 15
        while not heartbeat.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(heartbeat.exists())
        native_pid = json.loads(heartbeat.read_bytes())["pid"]
        response = deletion.delete_runs([run["id"]], start_worker=False)
        self.assertEqual(len(response["pending"]), 1)
        self.assertTrue(heartbeat.exists())
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            jobs.sweep()
            if not db.one("SELECT id FROM runs WHERE id = ?", (run["id"],)) and not runner.process_alive(owner):
                break
            time.sleep(0.02)
        self.assertFalse(runner.process_alive(native_pid))
        self.assertFalse(runner.process_alive(owner))
        self.assertIsNone(db.one("SELECT id FROM runs WHERE id = ?", (run["id"],)))
        self.assertFalse(heartbeat.parent.exists())

    def test_http_single_batch_partial_and_path_validation(self):
        run = self.finished()
        outside = self.folder / "keep.json"
        outside.write_text("preserve")
        db.update("runs", run["id"], {"input_manifest": str(outside)})
        client = TestClient(create_app(start_worker=False), base_url="http://127.0.0.1:8787")
        with patch.object(jobs, "ensure_worker", return_value={"status": "synthetic-test"}):
            result = client.post("/api/cases/delete", json={"items": [{"project_id": self.pid, "case_id": self.case["id"]},
                                                                       {"project_id": "missing", "case_id": "TC-1"},
                                                                       {"project_id": "../outside", "case_id": "TC-1"}]}).json()
            self.assertEqual((len(result["deleted"]), len(result["errors"])), (1, 2))
            self.assertEqual(client.post("/api/runs/delete", json={"run_ids": "wrong"}).status_code, 400)
            result = client.post("/api/runs/delete", json={"run_ids": [run["id"], "../outside", "missing-run"]}).json()
            self.assertEqual((len(result["deleted"]), len(result["errors"])), (1, 2))
            self.assertEqual(outside.read_text(), "preserve")
            self.assertTrue(client.get("/api/runs/" + run["id"]).json()["deletion_error"])
            self.assertEqual(client.delete(f"/api/projects/{self.pid}/cases/{self.case['id']}").status_code, 200)
            db.update("runs", run["id"], {"input_manifest": ""})
            result = client.delete("/api/runs/" + run["id"]).json()
            self.assertEqual(len(result["deleted"]), 1)
            self.assertEqual(client.get("/api/runs/" + run["id"]).status_code, 404)


if __name__ == "__main__":
    unittest.main()
