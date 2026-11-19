"""Task Excel delivery reads real workbooks and isolates real HTTP/CLI executions."""
import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "skills/autoskill/app"
sys.path.insert(0, str(APP))

from autoskill import builder, data_refs, db, deletion, native_assets, native_reader, native_run, projects, reporting, runner, runtime_identity, task_report_data, task_reports, workflow
from autoskill.server import jobs
from autoskill.server.app import create_app


class ReportHandler(BaseHTTPRequestHandler):
    priority = "medium"

    def log_message(self, *args):
        pass

    def respond(self, value):
        data = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.respond({"priority": type(self).priority})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.respond({"answer": 40 if body["question"] == "first" else 60})


class TaskReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), ReportHandler)
        thread = threading.Thread(target=cls.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        cls.addClassCleanup(cls.server.server_close)
        cls.addClassCleanup(cls.server.shutdown)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.folder / "workspace"),
                                                   "AUTOSKILL_CASE_SKILL": str(self.folder / "plan"), "AUTOSKILL_BRIDGE": "off",
                                                   "AUTOSKILL_HEADED": "0", "AUTOSKILL_SESSION": "", "AUTOSKILL_WORKFLOW_BINDING": ""})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        ReportHandler.priority = "medium"
        self.pid = projects.create("=Task Excel", f"http://127.0.0.1:{self.server.server_port}", project_id="task-report")["id"]
        self.case = builder.api_case("TC-API-001", "=Expected high", "GET", "/priority", {"status": 200, "json_equals": {"/priority": "high"}})
        extra = builder.api_case("TC-API-002", "Unrelated case", "GET", "/priority", {"status": 200, "json_equals": {"/priority": "medium"}})
        projects.save_cases(self.pid, [self.case, extra])
        self.case = projects.case(self.pid, self.case["id"])

    def execute(self, case_id="TC-API-001"):
        return runner.execute(runner.create(self.pid, [case_id])["id"])

    def workbook(self, receipt):
        self.assertEqual(receipt["status"], "ready", receipt)
        self.assertEqual(receipt["template_id"], "test-cases-14-column-v1")
        self.assertTrue(receipt["validation"]["workbook_readable"])
        book = load_workbook(receipt["excel"], data_only=False)
        self.addCleanup(book.close)
        return book

    @staticmethod
    def rows(book, sheet="用例与结果"):
        values = list(book[sheet].values)
        return [dict(zip(values[0], row)) for row in values[1:]]

    def cli(self, *arguments, timeout=20):
        env = {**os.environ, "PYTHONPATH": str(APP)}
        result = subprocess.run([sys.executable, "-m", "autoskill", *arguments], cwd=ROOT, env=env,
                                capture_output=True, text=True, timeout=timeout, check=False)
        self.assertTrue(result.stdout.strip(), result.stderr)
        return result.returncode, json.loads(result.stdout)

    def native_asset(self):
        source = self.folder / "spec.json"
        source.write_text(json.dumps({"endpoint": {"url": f"http://127.0.0.1:{self.server.server_port}/chat",
                                                    "body": {"question": "{{question}}", "messages": "{{messages}}"}},
                                      "samples": [{"id": "conversation", "title": "Two turns are one case", "turns": [
                                          {"question": "first", "expected": 999}, {"question": "second", "expected": 60}]}]}), encoding="utf-8")
        return native_assets.import_asset(self.pid, str(source), "ai-eval", asset_id="report-conversation")

    def test_run_uses_frozen_scope_and_keeps_earlier_failure_after_later_success(self):
        first = self.execute()
        self.assertEqual(first["status"], "Fail")
        report = first["test_report"]
        book = self.workbook(report)
        rows = self.rows(book)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["用例编号"], rows[0]["本次结果"]), ("TC-API-001", "失败"))
        self.assertIn("high", rows[0]["断言"])
        self.assertEqual((book["用例与结果"]["D2"].value, book["用例与结果"]["D2"].data_type), ("=Expected high", "s"))
        old_bytes = Path(report["excel"]).read_bytes()
        changed = copy.deepcopy(self.case)
        changed["title"] = "Later definition"
        changed["assertions"]["json_equals"]["/priority"] = "medium"
        projects.save_cases(self.pid, [changed])
        second = self.execute()
        self.assertEqual(second["status"], "Pass")
        self.assertNotEqual(second["test_report"]["excel"], report["excel"])
        self.assertEqual(task_reports.for_run(first["id"]), report)
        self.assertEqual(Path(report["excel"]).read_bytes(), old_bytes)

    def test_blocked_and_cancelled_tasks_have_real_not_run_rows(self):
        run = runner.create(self.pid, [self.case["id"]])
        Path(run["input_manifest"]).unlink()
        finished = runner.execute(run["id"])
        self.assertEqual(finished["status"], "Blocked")
        rows = self.rows(self.workbook(finished["test_report"]))
        self.assertEqual((rows[0]["用例编号"], rows[0]["本次结果"]), (self.case["id"], "未执行"))
        queued = runner.create(self.pid, ["TC-API-002"])
        cancelled = jobs.cancel(queued["id"])
        self.assertEqual(cancelled["status"], "Cancelled")
        self.assertEqual(self.rows(self.workbook(cancelled["test_report"]))[0]["本次结果"], "未执行")

    def test_invalid_workbook_is_failed_delivery_and_retry_does_not_execute_again(self):
        with patch("openpyxl.Workbook.save", side_effect=lambda file: Path(file).write_bytes(b"not an xlsx")):
            result = self.execute()
        self.assertEqual(result["status"], "Fail")
        failed = result["test_report"]
        self.assertEqual(failed["status"], "failed")
        self.assertNotIn("excel", failed)
        self.assertNotIn("download_url", failed)
        before = db.one("SELECT COUNT(*) n FROM runs")["n"]
        receipt = task_reports.retry(failed["report_id"])
        self.assertEqual(self.rows(self.workbook(receipt))[0]["本次结果"], "失败")
        self.assertEqual(db.one("SELECT COUNT(*) n FROM runs")["n"], before)

    def test_recording_snapshot_failure_is_reported_without_throwing_and_definitions_are_not_run(self):
        recording = {"started_at": db.now(), "session_id": "recording session", "rand": "synthetic"}
        with patch.object(task_report_data, "recording_snapshot", side_effect=OSError("synthetic unavailable source")):
            failed = task_reports.for_recording(self.pid, recording, [self.case])
        self.assertEqual(failed["status"], "failed")
        self.assertNotIn("excel", failed)
        self.assertEqual(projects.case(self.pid, self.case["id"])["title"], self.case["title"])
        self.workbook(task_reports.retry(failed["report_id"]))
        recording["rand"] = "another"
        case = copy.deepcopy(self.case)
        case["extensions"]["recording_result"] = {"status": "Fail", "expected": "high", "actual": "medium"}
        ready = task_reports.for_recording(self.pid, recording, [case])
        row = self.rows(self.workbook(ready))[0]
        self.assertEqual(row["本次结果"], "失败")
        self.assertIn('"status": "Fail"', row["失败或阻塞原因"])
        changed = copy.deepcopy(case)
        changed["title"] = "Changed after recording"
        projects.save_cases(self.pid, [changed])
        Path(ready["excel"]).unlink()
        retried = task_reports.retry(ready["report_id"])
        self.assertEqual(self.rows(self.workbook(retried))[0]["用例标题"], case["title"])

    def test_late_export_failure_cannot_overwrite_another_threads_success(self):
        recording = {"started_at": db.now(), "session_id": "same", "rand": "same"}
        with patch.object(reporting, "write_task", side_effect=OSError("initial failure")):
            failed = task_reports.for_recording(self.pid, recording, [self.case])
        write = reporting.write_task
        persist = task_reports._persist_failure
        entered = threading.Event()
        calls = []
        def flaky(*args):
            calls.append(True)
            if len(calls) == 1:
                raise OSError("late failure")
            return write(*args)
        def delayed_failure(*args, **kwargs):
            entered.set()
            time.sleep(0.15)
            return persist(*args, **kwargs)
        with patch.object(reporting, "write_task", side_effect=flaky), patch.object(task_reports, "_persist_failure", side_effect=delayed_failure), ThreadPoolExecutor(2) as pool:
            first = pool.submit(task_reports.retry, failed["report_id"])
            self.assertTrue(entered.wait(3))
            second = pool.submit(task_reports.retry, failed["report_id"])
            self.assertEqual(first.result(timeout=5)["status"], "failed")
            self.assertEqual(second.result(timeout=5)["status"], "ready")
        self.workbook(task_reports.get(failed["report_id"]))

    def test_two_real_cli_sessions_and_cli_retry_preserve_independent_reports(self):
        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(self.cli, "--session", session, "run", self.pid, "--case", case_id)
                       for session, case_id in (("会话 甲", "TC-API-001"), ("会话 乙", "TC-API-002"))]
            responses = [future.result(timeout=25) for future in futures]
        self.assertEqual([code for code, _ in responses], [1, 0])
        reports = [value["test_report"] for _code, value in responses]
        self.assertEqual(len({report["excel"] for report in reports}), 2)
        for report in reports:
            self.workbook(report)
            code, value = self.cli("task-report", report["report_id"])
            self.assertEqual((code, value), (0, report))
        target = reports[0]
        Path(target["excel"]).write_bytes(b"corrupted")
        code, value = self.cli("task-report", target["report_id"])
        self.assertEqual((code, value["status"]), (2, "failed"))
        self.assertNotIn("excel", value)
        self.assertNotIn("download_url", value)
        before = db.one("SELECT COUNT(*) n FROM runs")["n"]
        code, fixed = self.cli("task-report", target["report_id"], "--retry")
        self.assertEqual(code, 0)
        self.workbook(fixed)
        self.assertEqual(db.one("SELECT COUNT(*) n FROM runs")["n"], before)

    def test_native_two_turns_remain_one_case_and_first_failure_is_retained(self):
        asset = self.native_asset()
        finished = native_run.execute(native_run.enqueue(self.pid, asset["id"])["id"])
        self.assertEqual(finished["status"], "Fail")
        book = self.workbook(finished["test_report"])
        self.assertEqual(len(self.rows(book)), 1)
        self.assertEqual(self.rows(book)[0]["本次结果"], "失败")
        turns = self.rows(book, "多轮对话")
        self.assertEqual([row["结果"] for row in turns], ["失败", "通过"])
        self.assertEqual([row["实际回答"] for row in turns], [40, 60])
        self.assertEqual(len({row["所属用例"] for row in turns}), 1)
        read = native_reader.status(finished["id"])
        self.assertEqual(read["test_report"], finished["test_report"])
        self.assertTrue(read["report"]["path"].endswith("outcome.json"))

    def test_native_declared_evidence_is_verified_and_changed_or_legacy_files_are_not(self):
        source = self.folder / "native-source"
        source.mkdir()
        (source / "test_frozen.py").write_text("import unittest\nclass Native(unittest.TestCase):\n def test_result(self): self.assertEqual(2 + 2, 4)\n", encoding="utf-8")
        asset = native_assets.import_asset(self.pid, source, "selenium-unittest")
        finished = native_run.execute(native_run.enqueue(self.pid, asset["id"])["id"])
        self.assertEqual(finished["status"], "Pass")
        self.assertEqual(self.rows(self.workbook(finished["test_report"]))[0]["本次结果"], "通过")
        run = db.one("SELECT * FROM runs WHERE id = ?", (finished["id"],))
        data = task_report_data.run_snapshot(run)
        self.assertTrue(data["cases"][0]["evidence_valid"])
        folder = data_refs.resolve(data["cases"][0]["evidence_directory"]) / "native"
        outcome_path = folder / "outcome.json"
        outcome = json.loads(outcome_path.read_bytes())
        report = next(reference for reference in outcome["evidence"] if reference["path"] == "unittest.json")
        original = (folder / report["path"]).read_bytes()
        (folder / report["path"]).write_bytes(original + b" ")
        changed = task_report_data.run_snapshot(run)
        self.assertFalse(changed["cases"][0]["evidence_valid"])
        self.assertEqual(task_report_data.case_verdict(changed["cases"][0]), "Fail")
        (folder / report["path"]).write_bytes(original)
        outcome["evidence"] = [reference["path"] for reference in outcome["evidence"]]
        outcome_path.write_text(json.dumps(outcome), encoding="utf-8")
        self.assertFalse(task_report_data.run_snapshot(run)["cases"][0]["evidence_valid"])

    def test_same_task_in_two_sessions_executes_again_and_does_not_modify_first_report(self):
        arguments = ("run", self.pid, "--case", self.case["id"])
        code, first = self.cli("--session", "任务 A", *arguments)
        self.assertEqual(code, 1)
        first_report = first["test_report"]
        original = Path(first_report["excel"]).read_bytes()
        ReportHandler.priority = "high"
        code, second = self.cli("--session", "任务 B", *arguments)
        self.assertEqual(code, 0)
        self.assertNotEqual(first_report["task_id"], second["test_report"]["task_id"])
        self.assertNotEqual(first_report["excel"], second["test_report"]["excel"])
        self.assertEqual(Path(first_report["excel"]).read_bytes(), original)
        self.assertEqual(self.rows(self.workbook(first_report))[0]["本次结果"], "失败")
        self.assertEqual(self.rows(self.workbook(second["test_report"]))[0]["本次结果"], "通过")
        self.assertEqual(db.one("SELECT COUNT(*) n FROM runs")["n"], 2)

    def test_running_status_does_not_wait_for_an_independent_executor_guard(self):
        asset = self.native_asset()
        run = native_run.enqueue(self.pid, asset["id"])
        held = self.folder / "held"
        release = self.folder / "release"
        script = """import sys,time
from pathlib import Path
from autoskill import deletion
with deletion.execution_guard(sys.argv[1]):
    Path(sys.argv[2]).write_text('held')
    while not Path(sys.argv[3]).exists(): time.sleep(0.02)
"""
        child = subprocess.Popen([sys.executable, "-c", script, run["id"], str(held), str(release)],
                                 env={**os.environ, "PYTHONPATH": str(APP)}, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while not held.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(held.exists())
            db.update("runs", run["id"], {"status": "Running", "pid": child.pid, "owner_identity": json.dumps(runtime_identity.current(child.pid))})
            with ThreadPoolExecutor(1) as pool:
                future = pool.submit(native_reader.status, run["id"])
                try:
                    self.assertEqual(future.result(timeout=2)["test_report"]["status"], "pending")
                    with TestClient(create_app(start_worker=False), base_url="http://127.0.0.1") as client:
                        response = pool.submit(client.get, "/api/runs/" + run["id"]).result(timeout=2)
                        self.assertEqual(response.json()["test_report"]["status"], "pending")
                        db.update("runs", run["id"], {"delete_requested": 1})
                        response = pool.submit(client.get, "/api/runs/" + run["id"]).result(timeout=2)
                        self.assertEqual(response.json()["test_report"]["status"], "skipped")
                finally:
                    release.touch()
        finally:
            release.touch()
            child.communicate(timeout=5)

    def claim(self, identity):
        return workflow.next_task(identity, idempotency_key=uuid.uuid4().hex)["task"]

    def submit(self, identity, task, payload):
        return workflow.submit(identity, task["id"], lease_token=task["lease_token"], attempt_id=task["attempt_id"],
                               idempotency_key=uuid.uuid4().hex, payload=payload)

    def workflow_candidate(self):
        identity = workflow.create(self.pid, {"title": "Only the priority requirement", "sources": [{"id": "source", "text": "priority must be high"}]},
                                   idempotency_key=uuid.uuid4().hex)["workflow_id"]
        self.submit(identity, self.claim(identity), {"requirements": [{"id": "req", "text": "priority high", "source_ids": ["source"]}]})
        self.submit(identity, self.claim(identity), {"scenarios": [{"id": "scenario", "title": "Priority", "requirement_ids": ["req"], "oracle_ids": ["oracle"]}],
                                                     "oracles": [{"id": "oracle", "kind": "api", "requirement_ids": ["req"], "checks": self.case["assertions"]}]})
        self.submit(identity, self.claim(identity), {"cases": [self.case], "bindings": {"scenario": [self.case["id"]]}})
        self.assertIsNone(self.claim(identity))
        return identity

    def test_workflow_report_keeps_its_sources_both_attempts_and_deleted_evidence_state(self):
        with patch.object(runner, "spawn", return_value=None), patch.object(jobs, "ensure_worker", return_value={"status": "running"}):
            identity = self.workflow_candidate()
            first = next(item["run_id"] for item in workflow.status(identity)["items"] if item["phase"] == "execute")
            runner.execute(first)
            task = self.claim(identity)
            context = workflow.diagnostic_context(identity)
            self.assertEqual(context["manifest_hash"], task["manifest_hash"])
            self.submit(identity, task, {"manifest_hash": context["manifest_hash"], "diagnoses": [
                {"target_id": target["target_id"], "disposition": "retry", "reason": "Target fix is now available",
                 "evidence_refs": target["evidence_refs"]} for target in context["target_contexts"]]})
            self.assertIsNone(self.claim(identity))
            second = [item["run_id"] for item in workflow.status(identity)["items"] if item["phase"] == "execute"][-1]
            ReportHandler.priority = "high"
            runner.execute(second)
            self.submit(identity, self.claim(identity), {"entries": [{"id": "knowledge", "text": "Retain initial defect", "evidence_refs": [first, second]}]})
            final = workflow.status(identity)
            task_reports.finish_workflow_report(final["test_report"]["report_id"])
            final = workflow.status(identity)
        self.assertTrue(final["complete"])
        self.assertEqual(final["conclusion"], "mixed")
        book = self.workbook(final["test_report"])
        rows = self.rows(book)
        self.assertEqual([row["本次结果"] for row in rows], ["失败", "通过"])
        self.assertEqual({row["用例编号"] for row in rows}, {self.case["id"]})
        self.assertEqual({row["关联场景"] for row in rows}, {"scenario"})
        source_text = json.dumps(self.rows(book, "来源与冻结定义"), ensure_ascii=False)
        if "长文本" in book:
            source_text += "".join(row["全文片段"] for row in self.rows(book, "长文本") if row["原单元格"].startswith("来源与冻结定义!"))
        self.assertIn("priority must be high", source_text)
        report_file = data_refs.resolve(final["test_report"]["excel"])
        frozen_workbook = report_file.read_bytes()
        deletion.delete_runs([second])
        self.assertEqual(task_reports.get(final["test_report"]["report_id"])["status"], "failed")
        refreshed = workflow.status(identity)["test_report"]
        self.assertEqual(refreshed["status"], "failed")
        self.assertEqual(report_file.read_bytes(), frozen_workbook)

    def test_http_download_and_run_deletion_remove_report_without_resurrection(self):
        result = self.execute()
        receipt = result["test_report"]
        with TestClient(create_app(start_worker=False), base_url="http://127.0.0.1") as client:
            detail = client.get("/api/runs/" + result["id"]).json()
            self.assertEqual(detail["test_report"], receipt)
            download = client.get(receipt["download_url"])
            self.assertEqual(download.status_code, 200)
            book = load_workbook(io.BytesIO(download.content))
            self.assertEqual(self.rows(book)[0]["本次结果"], "失败")
            book.close()
            deleted = client.delete("/api/runs/" + result["id"]).json()
            self.assertEqual(deleted["errors"], [])
            self.assertFalse(Path(receipt["excel"]).exists())
            self.assertEqual(task_reports.for_run(result["id"])["status"], "skipped")
            self.assertGreaterEqual(client.get(receipt["download_url"]).status_code, 400)
            self.assertFalse(Path(receipt["excel"]).parent.exists())

    def test_record_stop_delivers_unpublished_candidates_and_queues_validation(self):
        from autoskill.recorder import session

        rec = {"project_id": self.pid, "started_at": db.now(), "session_id": "recording", "rand": "captured", "start_url": "/priority",
               "steps": [{"action": "click", "selector": "#save"}]}
        session._save(rec)
        capture = {"source": "cdp_network", "entries": [], "available": False}
        with patch.object(session.agent, "call", return_value=None), patch.object(session.capture, "stop", return_value=capture), patch.object(jobs, "ensure_worker", return_value={}):
            result = session.stop("Saved recording")
        rows = self.rows(self.workbook(result["test_report"]))
        self.assertEqual([row["用例编号"] for row in rows], [result["ui_case"]["id"]])
        self.assertEqual(rows[0]["本次结果"], "未执行")
        self.assertTrue(result["candidate_id"])
        self.assertEqual(db.one("SELECT COUNT(*) n FROM runs")["n"], len(result["run_ids"]))
        self.assertFalse(any(case["id"] == result["ui_case"]["id"] for case in projects.cases(self.pid)))
        self.assertEqual(db.one("SELECT COUNT(*) n FROM results")["n"], 0)

    def test_long_source_text_is_readable_in_chunks_without_becoming_a_formula(self):
        case = copy.deepcopy(self.case)
        case["title"] = "=" + "来源" * 18000
        recording = {"started_at": db.now(), "session_id": "long", "rand": "long"}
        book = self.workbook(task_reports.for_recording(self.pid, recording, [case]))
        reference = "用例与结果!R2C4"
        chunks = [row for row in self.rows(book, "长文本") if row["原单元格"] == reference]
        self.assertEqual("".join(row["全文片段"] for row in chunks), case["title"])
        self.assertFalse(any(cell.data_type == "f" for sheet in book for row in sheet for cell in row))


if __name__ == "__main__":
    unittest.main()
