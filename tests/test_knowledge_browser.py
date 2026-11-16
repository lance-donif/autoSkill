"""Real browser interruption preserves unknown effects and prevents automatic replay."""
import os
import subprocess
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

from autoskill import builder, db, failure_context, paths, projects, runner, task_context, workflow
from autoskill.recorder import session


class CommitTarget(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'''<!doctype html><form action="/commit" method="post">
<button id="submit">Submit</button></form><output id="state">ready</output>'''
        self._send(body)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.server.commits += 1
        self._send(b'<!doctype html><output id="state">saved</output>')
        self.server.committed.set()

    def _send(self, body):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


CHILD = r'''
import os, signal, sys
from pathlib import Path
from autoskill.recorder import session

project, boundary, marker = sys.argv[1:]
session.start(project, login=False)
original_intent, original_receipt = session._intent, session._receipt
armed = False

def stop(intent):
    Path(marker).write_text(intent["operation_id"], encoding="utf-8")
    signal.pause()

def intent(rec, kind, payload, *, side_effect=True):
    global armed
    saved = original_intent(rec, kind, payload, side_effect=side_effect)
    if payload.get("command") == "click":
        armed = True
        if boundary == "before_dispatch":
            stop(saved)
    return saved

def receipt(saved, observation, *, status="succeeded"):
    if armed and boundary == "before_receipt":
        stop(saved)
    return original_receipt(saved, observation, status=status)

session._intent, session._receipt = intent, receipt
session.browser(["click", "#submit"])
raise RuntimeError("The injected boundary was not reached")
'''


@unittest.skipUnless(sys.platform == "linux" and paths.agent_browser() and paths.chrome(), "requires Linux and the actual recording browser")
class KnowledgeBrowserTests(unittest.TestCase):
    def test_real_missing_control_before_assertion_has_execution_step_target(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                "AUTOSKILL_WORKSPACE": directory, "AUTOSKILL_CASE_SKILL": directory + "/plan", "AUTOSKILL_HEADED": "0"}):
            site = ThreadingHTTPServer(("127.0.0.1", 0), CommitTarget)
            thread = threading.Thread(target=site.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
            thread.start()
            try:
                project = projects.create("操作失败证据", f"http://127.0.0.1:{site.server_port}")["id"]
                case = builder.ui_case("TC-UI-001", "缺失的提交控件", {"start_url": "/", "actions": [
                    {"action": "click", "locators": [{"by": "css", "value": "#absent"}], "timeout_seconds": 0.1}],
                    "assertions": [{"assertion": "text", "locators": [{"by": "css", "value": "#state"}], "expected": "saved"}]})
                projects.save_cases(project, [case])
                run = runner.create(project, [case["id"]])
                result = runner.execute(run["id"])
                self.assertNotEqual(result["status"], "Pass")
                manifest = failure_context.build_manifest(project, [{"run_id": run["id"]}])
                self.assertEqual(len(manifest["diagnostic_targets"]), 1)
                target = manifest["diagnostic_targets"][0]
                self.assertEqual((target["type"], target["pointer"]), ("execution_error", "/outcome/steps/1"))
                context = failure_context.read(project, run_id=run["id"], execution_attempt_id=target["execution_attempt_id"], case_id=case["id"])
                self.assertTrue(all(row["name"] == "全部动作可以完成" for row in context["assertions"]))
                self.assertEqual(context["outcome"]["business_status"], "Unverified")
                self.assertEqual(context["failed_steps"][0]["pointer"], target["pointer"])
                self.assertTrue(context["evidence_refs"])
            finally:
                site.shutdown()
                site.server_close()
                thread.join(timeout=3)

    def _interrupt(self, boundary, expected_commits):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                "AUTOSKILL_WORKSPACE": directory, "AUTOSKILL_CASE_SKILL": directory + "/plan",
                "AUTOSKILL_BRIDGE": "off", "AUTOSKILL_HEADED": "0", "AUTOSKILL_SESSION": "interrupt-" + uuid.uuid4().hex}), \
                patch("autoskill.server.jobs.ensure_worker", return_value={"status": "running", "pid": None}):
            site = ThreadingHTTPServer(("127.0.0.1", 0), CommitTarget)
            site.commits, site.committed = 0, threading.Event()
            thread = threading.Thread(target=site.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
            thread.start()
            child = None
            with task_context.use_session(os.environ["AUTOSKILL_SESSION"]):
                try:
                    project = projects.create("中断证据", f"http://127.0.0.1:{site.server_port}")["id"]
                    identity = workflow.create(project, {"title": "中断恢复", "sources": [{"id": "s1", "text": "提交只能执行一次"}]},
                                               idempotency_key="create")["workflow_id"]
                    task = workflow.next_task(identity, idempotency_key="first")["task"]
                    marker = Path(directory) / "boundary.txt"
                    environment = {**os.environ, "PYTHONPATH": str(ROOT / "skills/autoskill/app"),
                                   "AUTOSKILL_WORKFLOW_BINDING": task["binding_token"]}
                    child = subprocess.Popen([sys.executable, "-c", CHILD, project, boundary, str(marker)], env=environment,
                                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    deadline = time.monotonic() + 50
                    while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.05)
                    if not marker.exists():
                        child.kill()
                        output = child.communicate(timeout=5)
                        self.fail("Browser did not reach the interruption boundary: " + repr(output))
                    if expected_commits:
                        self.assertTrue(site.committed.wait(10), "The business write must precede process termination")
                    child.kill()
                    child.communicate(timeout=5)
                    operation_id = marker.read_text()
                    operation = db.one("SELECT * FROM workflow_operations WHERE id = ?", (operation_id,))
                    self.assertEqual((operation["state"], operation["observation"]), ("intended", None))
                    self.assertEqual(site.commits, expected_commits)
                    manifest = failure_context.build_manifest(project, [], workflow_id=identity, operations=[operation])
                    self.assertEqual(len(manifest["diagnostic_targets"]), 1)
                    self.assertEqual(manifest["diagnostic_targets"][0]["type"], "interruption")
                    context = failure_context.workflow_context(project, manifest)
                    self.assertTrue(context["target_contexts"][0]["evidence_available"])
                    db.execute("UPDATE workflow_attempts SET lease_until = '2000-01-01' WHERE id = ?", (task["attempt_id"],))
                    investigation = workflow.next_task(identity, idempotency_key="investigate")["task"]
                    self.assertEqual(investigation["phase"], "investigate")
                    public_context = workflow.diagnostic_context(identity)
                    self.assertEqual(public_context["manifest_hash"], investigation["manifest_hash"])
                    self.assertEqual(public_context["diagnostic_targets"][0]["operation_id"], operation_id)
                    self.assertEqual(site.commits, expected_commits)
                    with workflow.use_binding(investigation["binding_token"]):
                        self.assertTrue(session.check("text", "#state", "saved" if expected_commits else "ready")["passed"])
                    observation = db.one("SELECT id FROM workflow_operations WHERE attempt_id = ? AND kind = 'check'",
                                         (investigation["attempt_id"],))
                    result = workflow.submit(identity, investigation["id"], lease_token=investigation["lease_token"],
                                             attempt_id=investigation["attempt_id"], idempotency_key="incomplete",
                                             payload={"resolution": "incomplete", "observation_operation_ids": [observation["id"]]})
                    self.assertEqual(result["resolution"], "incomplete")
                    self.assertEqual(result["workflow"]["conclusion"], "incomplete")
                    self.assertEqual(db.one("SELECT state FROM workflow_items WHERE id = ?", (task["id"],))["state"], "interrupted")
                    self.assertEqual(db.one("SELECT state FROM workflow_operations WHERE id = ?", (operation_id,))["state"], "intended")
                    self.assertEqual(workflow.next_task(identity, idempotency_key="knowledge")["task"]["phase"], "knowledge")
                    self.assertEqual(site.commits, expected_commits)
                finally:
                    if child is not None and child.poll() is None:
                        child.kill()
                        child.communicate(timeout=5)
                    session.cancel()
                    site.shutdown()
                    site.server_close()
                    thread.join(timeout=3)

    def test_kill_after_intent_before_dispatch_keeps_zero_writes(self):
        self._interrupt("before_dispatch", 0)

    def test_kill_after_business_commit_before_receipt_keeps_one_write(self):
        self._interrupt("before_receipt", 1)


if __name__ == "__main__":
    unittest.main()
