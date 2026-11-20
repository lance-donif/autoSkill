"""Public cancellation interrupts real browser checks without inventing or erasing business failures."""
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

from autoskill import builder, candidate_publication, data_refs, db, execution_inputs, plan, projects, runner, task_reports
from autoskill.recorder import session
from autoskill.server.app import create_app


class PendingPage(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return

    def send(self, body, mime):
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        body = '''<!doctype html><meta charset="utf-8"><div data-testid="priority">medium</div>
<div data-testid="state">待处理</div><button data-testid="complete">完成</button>
<script>document.querySelector('button').onclick=async()=>{
const response=await fetch('/complete',{method:'POST'});const value=await response.json();
document.querySelector('[data-testid="state"]').textContent=value.state;};</script>'''.encode()
        self.send(body, "text/html; charset=utf-8")

    def do_POST(self):
        self.server.calls += 1
        body = json.dumps({"state": "已完成" if self.server.ready else "待处理"}).encode()
        self.send(body, "application/json")
        self.server.received.set()


class UiCancelTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.root / "data"),
                                              "AUTOSKILL_SESSION": "ui-cancel", "AUTOSKILL_CASE_SKILL": str(self.root / "sheet"),
                                              "AUTOSKILL_HEADED": "0", "AUTOSKILL_BRIDGE": "off", "AUTOSKILL_WORKFLOW_BINDING": "",
                                              "AUTOSKILL_RUNTIME_CONTEXT": ""})
        environment.start()
        self.addCleanup(environment.stop)
        self.target = ThreadingHTTPServer(("127.0.0.1", 0), PendingPage)
        self.target.ready, self.target.calls = True, 0
        self.target.received = threading.Event()
        thread = threading.Thread(target=self.target.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.target.server_close)
        self.addCleanup(self.target.shutdown)
        self.pid = projects.create("取消传播验证", f"http://127.0.0.1:{self.target.server_port}", project_id="ui-cancel")["id"]
        worker = patch("autoskill.server.jobs.ensure_worker", return_value={"status": "test-queued"})
        worker.start()
        self.addCleanup(worker.stop)
        self.client = TestClient(create_app(start_worker=False), base_url="http://127.0.0.1:8787")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def write_plan(self, expected):
        body = [{"id": "DESIGN-CANCEL", "module": "工单", "title": "完成工单并核对状态", "preconditions": "测试服务已运行。",
                 "steps": ["1. 点击完成。", "2. 核对工单状态。"], "expected": expected,
                 "source": {"kind": "synthetic_requirement", "text": expected}}]
        source = self.root / "design.json"
        source.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
        return plan.write(self.pid, str(source))

    def cancel_running(self, run_id):
        self.target.ready = False
        self.target.received.clear()
        finished, errors = [], []

        def execute():
            try:
                finished.append(runner.execute(run_id))
            except Exception as error:  # noqa: BLE001  # Surface thread failures in the test thread.
                errors.append(error)

        thread = threading.Thread(target=execute)
        thread.start()
        def close_execution():
            if thread.is_alive():
                db.execute("UPDATE runs SET cancel = 1 WHERE id = ?", (run_id,))
            thread.join(105)
            self.assertFalse(thread.is_alive(), "The test's execution thread must stop before its workspace is removed")
        self.addCleanup(close_execution)
        self.assertTrue(self.target.received.wait(15),
                        "The real browser must reach the HTTP action before cancellation: "
                        + repr([(run.get("status"), run.get("reason")) for run in finished]) + repr(errors))
        cancel_time = time.monotonic()
        response = self.client.post(f"/api/runs/{run_id}/cancel")
        self.assertEqual(response.status_code, 200, response.text)
        thread.join(8)
        self.assertFalse(thread.is_alive(), "Cancellation must interrupt the 40-second assertion, not wait for its timeout")
        self.assertEqual(errors, [])
        self.assertLess(time.monotonic() - cancel_time, 8)
        self.assertEqual(finished[0]["status"], "Cancelled")
        rows = db.rows("SELECT * FROM results WHERE run_id = ?", (run_id,))
        self.assertEqual(len(rows), 1)
        result, detail = rows[0], json.loads(rows[0]["detail"])
        self.assertEqual(detail["execution_status"], "cancelled")
        receipt = json.loads(data_refs.resolve(detail["attempts"][-1]["path"]).read_bytes())
        self.assertEqual(receipt["state"], "completed")
        self.assertEqual(receipt["outcome"]["execution_status"], "cancelled")
        self.assertTrue(execution_inputs.result_evidence(finished[0], result))
        return finished[0], result, detail

    def test_cancelled_long_check_fails_the_started_case_in_its_original_sheet_and_report(self):
        written = self.write_plan("2. 工单状态变为已完成。")
        session.start(self.pid, login=False, url="/")
        session.browser(["click", "testid=complete"])
        observed = session.check("text", "testid=state", "已完成", timeout_seconds=40)
        self.assertTrue(observed["passed"])
        recorded = session.stop("完成工单并核对状态", plan_case="DESIGN-CANCEL")
        self.assertEqual(recorded["ui_case"]["recording_status"], "Pass")
        run_id = recorded["run_ids"][0]
        for identity in recorded["run_ids"][1:]:
            self.assertEqual(self.client.post(f"/api/runs/{identity}/cancel").status_code, 200)
        initial = Path(written["initial_excel"]).read_bytes()
        _run, result, detail = self.cancel_running(run_id)
        self.assertEqual(result["status"], "Cancelled")
        self.assertTrue(detail["target_started"])
        self.assertIn("取消", result["reason"])
        self.assertEqual(detail["business_status"], "Unverified")
        self.assertFalse(any(row.get("passed") is False for row in detail.get("assertions", [])))
        self.assertEqual(projects.cases(self.pid), [])
        shown = plan.show()
        row = shown["cases"][0]
        self.assertEqual((row["id"], row["status"]), ("DESIGN-CANCEL", "失败"))
        self.assertIn(result["reason"], row["note"])
        self.assertEqual(shown["summary"]["失败"], 1)
        self.assertEqual(shown["test_report"]["summary_counts"]["Fail"], 1)
        self.assertEqual(shown["test_report"]["bugs_counts"]["business"], 0)
        self.assertEqual(Path(written["initial_excel"]).read_bytes(), initial)
        report = task_reports.for_run(run_id)
        self.assertEqual(report["summary_counts"]["Fail"], 1)
        self.assertEqual(report["bugs_counts"]["business"], 0)

    def test_failure_observed_before_cancel_remains_a_business_failure(self):
        self.write_plan("优先级应为 high；完成操作后状态应为已完成。")
        shown = plan.show()["cases"][0]
        case = builder.ui_case("UI-EARLY-FAIL", "保留取消前已确认的业务失败", {
            "start_url": "/", "actions": [
                {"assertion": "text", "selector": "[data-testid=priority]", "expected": "high", "mode": "always", "timeout_seconds": 1},
                {"action": "click", "selector": "[data-testid=complete]"}],
            "assertions": [{"assertion": "text", "selector": "[data-testid=state]", "expected": "已完成", "timeout_seconds": 40}]},
            test_case_document={"description": shown["title"], "preconditions": shown["preconditions"], "steps": shown["steps"],
                                "expected": shown["expected"], "source": shown["execution_source"]})
        candidate = candidate_publication.stage(self.pid, [case], {}, {"fixture": "early-failure-cancel"})
        queued = candidate_publication.queue(candidate["id"], start=False)
        _run, result, detail = self.cancel_running(queued["run_id"])
        self.assertEqual(result["status"], "Fail")
        self.assertEqual(detail["business_status"], "Fail")
        self.assertEqual(detail["execution_blocker"]["category"], "cancelled")
        self.assertEqual(len(detail["assertions"]), 1)
        self.assertEqual((detail["assertions"][0]["expected"], detail["assertions"][0]["actual"]), ("high", "medium"))
        self.assertEqual(projects.cases(self.pid), [])
        self.assertEqual(plan.show()["cases"][0]["status"], "失败")
        report = task_reports.for_run(queued["run_id"])
        self.assertEqual(report["summary_counts"]["Fail"], 1)
        self.assertEqual(report["bugs_counts"]["business"], 1)


if __name__ == "__main__":
    unittest.main()
