"""Manual login and explicit visibility survive independent commands in one recording."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "skills/autoskill/app"
sys.path.insert(0, str(APP))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from autoskill import db, paths, projects, task_context
from autoskill.recorder import agent, session
from autoskill.recorder.context import AttachedPage
from autoskill.server import project_settings


class RecordingVisibilityContracts(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        environment = patch.dict(os.environ, {
            "AUTOSKILL_WORKSPACE": temporary.name, "AUTOSKILL_SESSION": "visibility-contract",
            "AUTOSKILL_WORKFLOW_BINDING": "", "AUTOSKILL_CASE_SKILL": "", "AUTOSKILL_RUNTIME_CONTEXT": "",
            "AUTOSKILL_BRIDGE": "off", "PYTHONPATH": str(APP), "DISPLAY": ":99"})
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("AUTOSKILL_HEADED", None)
        paths.save_settings(dashboard={"headed": False})

    def test_login_service_explicit_environment_overrides_saved_hidden_choice(self):
        app = FastAPI()
        project_settings.register(app)
        with patch.dict(os.environ, {"AUTOSKILL_HEADED": "1"}), \
                patch.object(projects, "auth", return_value={"ui_case": "LOGIN"}), \
                patch.object(project_settings.runner, "create", return_value={"id": "run", "status": "Queued", "headed": 1}) as create, \
                patch.object(project_settings.jobs, "ensure_worker", return_value={}), TestClient(app) as client:
            self.assertEqual(client.post("/api/projects/fixture/login", json={}).status_code, 200)
            self.assertTrue(create.call_args.kwargs["headed"])
            self.assertEqual(client.post("/api/projects/fixture/login", json={"headed": False}).status_code, 200)
            self.assertFalse(create.call_args.kwargs["headed"])
        self.assertFalse(paths.settings()["dashboard"]["headed"])

    def test_stop_validation_keeps_recorded_mode_despite_parent_environment(self):
        project = projects.create("Recording visibility", "http://127.0.0.1:9")["id"]
        for recorded, inherited, saved, expected in ((True, None, False, True), (False, None, True, False),
                                                    (True, "0", False, True), (False, "1", False, False),
                                                    (None, None, True, True), (None, None, False, False),
                                                    (None, "0", True, False), (None, "1", False, True)):
            with self.subTest(recorded=recorded, inherited=inherited, saved=saved), patch.dict(os.environ):
                paths.save_settings(dashboard={"headed": saved})
                os.environ.pop("AUTOSKILL_HEADED", None)
                if inherited is not None:
                    os.environ["AUTOSKILL_HEADED"] = inherited
                rec = {"project_id": project, "session_id": task_context.session_id(), "started_at": db.now(),
                       "start_url": "/", "steps": [{"assertion": "url", "expected": "/"}], "refs": {},
                       "secrets": [], "used_login": False, "as_login": False, "network_capture": None}
                if recorded is not None:
                    rec["headed"] = recorded
                session._save(rec)
                with patch.object(agent, "call", return_value=None), \
                        patch.object(session.capture, "stop", return_value={}), \
                        patch("autoskill.task_reports.for_recording", return_value={}), \
                        patch("autoskill.server.jobs.ensure_worker"):
                    result = session.stop("Validate recorded visibility")
                self.assertTrue(result["run_ids"], result)
                for run_id in result["run_ids"]:
                    self.assertEqual(bool(db.one("SELECT headed FROM runs WHERE id = ?", (run_id,))["headed"]), expected)
                self.assertEqual(paths.settings()["dashboard"]["headed"], saved)


class ManualLoginTarget(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/login":
            body = b"""<!doctype html><title>Manual login</title><form action='/signed-in'>
<label>User<input name='username'></label><label>Password<input name='password' type='password'></label>
<button>Sign in</button></form>"""
        elif self.path.startswith("/signed-in"):
            self.send_response(303)
            self.send_header("Set-Cookie", "fixture_login=accepted; Path=/; HttpOnly; SameSite=Lax")
            self.send_header("Location", "/account")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        elif "fixture_login=accepted" in self.headers.get("Cookie", ""):
            body = b"""<!doctype html><title>Signed in</title><h1>Account ready</h1>
<button id='continue' onclick="document.querySelector('output').textContent='Continued after login'">Continue</button>
<output>Waiting</output>"""
        else:
            self.send_response(303)
            self.send_header("Location", "/login")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@unittest.skipUnless(paths.agent_browser() and paths.chrome(), "requires the actual recording browser")
class ManualLoginContinuationTests(unittest.TestCase):
    def test_manual_login_resumes_original_browser_after_cli_process_exits(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
                "AUTOSKILL_WORKSPACE": folder, "AUTOSKILL_SESSION": "manual-login-continuation",
                "AUTOSKILL_WORKFLOW_BINDING": "", "AUTOSKILL_CASE_SKILL": "", "AUTOSKILL_RUNTIME_CONTEXT": "",
                "AUTOSKILL_HEADED": "0", "AUTOSKILL_BRIDGE": "off", "PYTHONPATH": str(APP)}):
            site = ThreadingHTTPServer(("127.0.0.1", 0), ManualLoginTarget)
            thread = threading.Thread(target=site.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
            thread.start()
            self.addCleanup(thread.join, 3)
            self.addCleanup(site.server_close)
            self.addCleanup(site.shutdown)
            project = projects.create("Manual login continuation", f"http://127.0.0.1:{site.server_port}/account")["id"]
            try:
                started = self.command("record", "start", project, "--no-login")
                self.assertIn("needs_login", started)
                self.assertNotIn("record cancel", started["needs_login"])
                self.assertIn("browser snapshot", started["needs_login"])
                self.assertEqual(started["session_id"], "manual-login-continuation")
                self.assertFalse(started["headed"])
                original = session.current()
                name = session.session_name()
                controller = agent._controller_pid(name)
                endpoint = agent.call(name, ["get", "cdp-url"])["cdpUrl"]
                self.assertEqual(started["browser_session"], name)
                with AttachedPage(name, original) as attached:
                    attached.page.get_by_label("User").fill("fixture-user")
                    attached.page.get_by_label("Password").fill("fixture-password")
                    attached.page.get_by_role("button", name="Sign in").click()
                    attached.page.wait_for_url("**/account")
                for result in (self.command("browser", "wait", "1"), self.command("browser", "wait", "#continue"),
                               self.command("browser", "wait", "role=button:Continue"), self.command("browser", "snapshot")):
                    self.assertEqual(result["session_id"], started["session_id"])
                    self.assertEqual(result["browser_session"], started["browser_session"])
                    self.assertEqual(result["tab"], started["tab"])
                    self.assertTrue(result["url"].endswith("/account"), result)
                self.assertIn("Continue", result["snapshot"])
                self.command("browser", "click", "#continue")
                self.assertTrue(self.command("check", "text", "output", "Continued after login")["passed"])
                self.assertEqual(agent._controller_pid(name), controller)
                self.assertEqual(agent.call(name, ["get", "cdp-url"])["cdpUrl"], endpoint)
                self.assertEqual(session.current()["active_tab"], original["active_tab"])
                self.assertEqual(session.current()["started_at"], original["started_at"])
                status = self.command("record", "status")
                self.assertEqual(status["browser_session"], started["browser_session"])
                self.assertFalse(status["headed"])
                refused = self.command("record", "start", project, "--headed", exit_code=2)
                self.assertIn("已有进行中的录制", refused["error"])
                self.assertEqual(agent._controller_pid(name), controller)
                self.assertEqual(session.current()["active_tab"], original["active_tab"])
            finally:
                session.cancel()

    def command(self, *arguments, exit_code=0):
        completed = subprocess.run([sys.executable, "-m", "autoskill", *arguments], env=os.environ.copy(),
                                   capture_output=True, text=True, timeout=90, check=False)
        self.assertEqual(completed.returncode, exit_code, (completed.stdout, completed.stderr))
        return json.loads(completed.stdout)


if __name__ == "__main__":
    unittest.main()
