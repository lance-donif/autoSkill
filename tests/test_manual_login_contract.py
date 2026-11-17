"""Independent contracts for manual login continuity and later browser preferences."""
import json
import os
import sys
import tempfile
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill import builder, db, paths, projects, runner, task_context
from autoskill.recorder import agent, capture, session
from autoskill.server import project_settings

LOGIN = b"""<!doctype html><title>Manual login fixture</title>
<label>Fixture password<input id="password" type="password"></label>
<button id="login">Sign in manually</button><script>
document.querySelector('#login').onclick = async () => {
 await fetch('/login', {method: 'POST'});
 location.href = '/workspace';
};</script>"""
WORKSPACE = b"""<!doctype html><title>Authenticated workspace</title>
<h1>Signed in</h1><button id="business">Complete business action</button>
<output id="result">Ready</output><script>
document.querySelector('#business').onclick = () => {
 document.querySelector('#result').textContent = 'Business action complete';
};</script>"""


def evidence(name, value):
    """Write only fixture observations when an explicit evidence folder is selected."""
    selected = os.environ.get("AUTOSKILL_MANUAL_LOGIN_EVIDENCE")
    if selected:
        folder = Path(selected)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / (name + ".json")).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


class ManualLoginTarget(BaseHTTPRequestHandler):
    def reply(self, body, *, cookie=False):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if cookie:
            self.send_header("Set-Cookie", "fixture_login=yes; Path=/; HttpOnly; SameSite=Lax")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        authorized = "fixture_login=yes" in self.headers.get("Cookie", "")
        self.reply(WORKSPACE if urlsplit(self.path).path == "/workspace" and authorized else LOGIN)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.reply(b"ok", cookie=True)

    def log_message(self, *_args):
        pass


class IsolatedContract(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="manual-login-contract-")
        self.addCleanup(temporary.cleanup)
        environment = patch.dict(os.environ, {
            "AUTOSKILL_WORKSPACE": temporary.name, "AUTOSKILL_BRIDGE": "off",
            "AUTOSKILL_SESSION": "", "AUTOSKILL_CASE_SKILL": "", "AUTOSKILL_HEADED": "0"})
        environment.start()
        self.addCleanup(environment.stop)
        scope = task_context.use_session("manual-login-independent-audit")
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)

    def save_preference(self, headed):
        file = paths.local() / "settings.json"
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(json.dumps({"dashboard": {"headed": headed}}), encoding="utf-8")


@unittest.skipUnless(paths.agent_browser() and paths.chrome(), "requires actual agent-browser and Chromium")
class ManualLoginBrowserContract(IsolatedContract):
    def setUp(self):
        super().setUp()
        site = ThreadingHTTPServer(("127.0.0.1", 0), ManualLoginTarget)
        thread = threading.Thread(target=site.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(site.server_close)
        self.addCleanup(site.shutdown)
        self.addCleanup(session.cancel)
        self.base = f"http://127.0.0.1:{site.server_port}"
        self.project = projects.create("人工登录独立验收", self.base + "/login")["id"]
        self.marker = "same-tab-" + uuid.uuid4().hex

    def identity(self):
        name = session.session_name()
        tabs = agent.call(name, ["tab", "list"])["tabs"]
        endpoint = agent.call(name, ["get", "cdp-url"])["cdpUrl"]
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(endpoint)
            connection = browser.new_browser_cdp_session()
            processes = connection.send("SystemInfo.getProcessInfo")["processInfo"]
            browser_pid = next(row["id"] for row in processes if row["type"] == "browser")
            targets = connection.send("Target.getTargets")["targetInfos"]
            page_targets = sorted(row["targetId"] for row in targets if row["type"] == "page")
        return {"session": name, "controller_pid": agent._controller_pid(name), "browser_pid": browser_pid,
                "cdp_url": endpoint, "page_target_ids": page_targets, "tabs": [tab["tabId"] for tab in tabs],
                "active_tab": next(tab["tabId"] for tab in tabs if tab.get("active"))}

    def user_login(self, endpoint):
        # A separate CDP client represents user input outside the recorder's commands.
        # It exercises a real headless browser, not a visible desktop or a Windows device.
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(endpoint)
            page = next(page for context in browser.contexts for page in context.pages if page.url.startswith(self.base))
            page.evaluate("value => sessionStorage.setItem('manual_login_marker', value)", self.marker)
            page.locator("#password").fill("synthetic-fixture-password")
            page.locator("#login").click()
            page.wait_for_url(self.base + "/workspace")
            self.assertEqual(page.locator("h1").inner_text(), "Signed in")

    def repeat_start_without_replacing_browser(self):
        try:
            resumed = session.start(self.project)
        except session.RecordError as error:
            self.assertIn("snapshot", str(error))
            self.restart_handling = "refused-existing-recording-with-continuation-hint"
        else:
            self.assertTrue(resumed["recording"])
            self.restart_handling = "reused-existing-recording"

    def assert_continuity(self, before):
        after = self.identity()
        self.assertEqual(after, before)
        marker = agent.call(session.session_name(), ["eval", "--stdin"],
                            stdin_text="sessionStorage.getItem('manual_login_marker')")["result"]
        self.assertEqual(marker, self.marker)
        self.assertIn("Signed in", session.browser(["snapshot"])["snapshot"])
        session.browser(["click", "#business"])
        self.assertTrue(session.check("text", "#result", "Business action complete")["passed"])
        evidence(self._testMethodName, {"runtime": "Linux headless Chromium", "before": before, "after": after,
                                       "session_storage_preserved": True, "business_action": "Pass",
                                       "repeat_start": getattr(self, "restart_handling", "not-requested")})

    def test_manual_login_then_snapshot_and_business_action_use_original_tab(self):
        self.assertTrue(session.start(self.project, login=False)["recording"])
        before = self.identity()
        self.user_login(before["cdp_url"])
        self.assert_continuity(before)

    def test_repeated_start_after_manual_login_preserves_authenticated_tab(self):
        session.start(self.project, login=False)
        before = self.identity()
        started_at = session.current()["started_at"]
        self.user_login(before["cdp_url"])
        self.repeat_start_without_replacing_browser()
        self.assertEqual(session.current()["started_at"], started_at)
        self.assert_continuity(before)

    def test_repeated_start_while_user_is_typing_preserves_login_form(self):
        session.start(self.project, login=False)
        before = self.identity()
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(before["cdp_url"])
            page = next(page for context in browser.contexts for page in context.pages if page.url.startswith(self.base))
            page.locator("#password").fill("synthetic-in-progress")
            page.evaluate("value => sessionStorage.setItem('manual_login_marker', value)", self.marker)
        self.repeat_start_without_replacing_browser()
        self.assertEqual(self.identity(), before)
        self.assertEqual(session.browser(["get", "value", "#password"])["result"]["value"], "synthetic-in-progress")
        evidence(self._testMethodName, {"runtime": "Linux headless Chromium", "identity": before,
                                       "partially_entered_login_preserved": True, "repeat_start": self.restart_handling})


class ManualLoginPreferenceContract(IsolatedContract):
    def fake_recording(self, project):
        page = {"url": projects.get(project)["start_url"], "state": "complete", "password": True, "seconds": 0}
        for target, value in (("_open", page), ("_page_state", page), ("_settled_snapshot", "password input")):
            mocked = patch.object(session, target, return_value=value)
            mocked.start()
            self.addCleanup(mocked.stop)
        for target, value in (("_observe_tabs", None), ("_fit_window", None)):
            mocked = patch.object(session, target, return_value=value)
            mocked.start()
            self.addCleanup(mocked.stop)
        for owner, target, value in ((agent, "call", {}), (capture, "start", None), (capture, "stop", {"available": False})):
            mocked = patch.object(owner, target, return_value=value)
            mocked.start()
            self.addCleanup(mocked.stop)
        return session.start(project, login=False)

    def test_login_hint_keeps_the_open_recording_for_manual_login(self):
        project = projects.create("登录引导契约", "http://127.0.0.1:9/login")["id"]
        result = self.fake_recording(project)
        hint = result.get("needs_login", "")
        self.assertTrue(hint)
        self.assertNotIn("record cancel", hint)
        self.assertNotIn("--as-login", hint)
        self.assertIn("snapshot", hint)
        self.assertTrue(session.current())

    def test_windows_default_and_saved_choice_reach_launch_arguments(self):
        os.environ.pop("AUTOSKILL_HEADED", None)
        with patch.object(paths.sys, "platform", "win32"), patch.object(paths, "chrome", return_value="fixture-chrome"):
            self.assertTrue(paths.headed())
            for saved in (False, True):
                self.save_preference(saved)
                for explicit in (None, "0", "1"):
                    with self.subTest(saved=saved, explicit=explicit), patch.dict(os.environ):
                        if explicit is not None:
                            os.environ["AUTOSKILL_HEADED"] = explicit
                        expected = saved if explicit is None else explicit == "1"
                        flags = agent.launch_args([])
                        self.assertEqual("--headed" in flags, expected)
                        self.assertEqual("--start-maximized" in ",".join(flags), expected)
        evidence(self._testMethodName, {"scope": "launch argument contract only", "mapped_window_verified": False})

    def test_login_endpoint_explicit_environment_overrides_saved_preference(self):
        app = FastAPI()
        project_settings.register(app)
        for saved in (False, True):
            self.save_preference(saved)
            for explicit, request_choice in ((not saved, None), (not saved, saved)):
                with self.subTest(saved=saved, explicit=explicit, request_choice=request_choice):
                    os.environ["AUTOSKILL_HEADED"] = "1" if explicit else "0"
                    chosen = explicit if request_choice is None else request_choice
                    with patch.object(projects, "auth", return_value={"ui_case": "LOGIN"}), \
                            patch.object(runner, "create", return_value={"id": "fixture", "status": "Queued", "headed": int(chosen)}) as create, \
                            patch.object(project_settings.jobs, "ensure_worker", return_value={}), TestClient(app) as client:
                        body = {} if request_choice is None else {"headed": request_choice}
                        response = client.post("/api/projects/fixture/login", json=body)
                        self.assertEqual(response.status_code, 200)
                        self.assertIs(create.call_args.kwargs["headed"], chosen)

    def test_record_stop_validation_preserves_record_start_explicit_choice(self):
        project = projects.create("后续验证窗口契约", "http://127.0.0.1:9/login")["id"]
        for selected in (False, True):
            for inherited in (None, "0" if selected else "1"):
                with self.subTest(selected=selected, inherited=inherited):
                    self.save_preference(not selected)
                    os.environ["AUTOSKILL_HEADED"] = "1" if selected else "0"
                    self.fake_recording(project)
                    recording = session.current()
                    recording["steps"] = [{"assertion": "text", "locators": [{"by": "css", "value": "#result"}], "expected": "Ready"}]
                    session._save(recording)
                    # A later CLI invocation may inherit a stale host value or only the saved default.
                    os.environ.pop("AUTOSKILL_HEADED", None)
                    if inherited is not None:
                        os.environ["AUTOSKILL_HEADED"] = inherited
                    with patch("autoskill.server.jobs.ensure_worker"):
                        stopped = session.stop("窗口选择在自动校验中保留")
                    self.assertTrue(stopped["run_ids"], stopped)
                    for identity in stopped["run_ids"]:
                        row = db.one("SELECT headed FROM runs WHERE id = ?", (identity,))
                        self.assertEqual(row["headed"], int(selected))

    def test_runner_without_explicit_choice_uses_saved_dashboard_preference(self):
        project = projects.create("自动运行窗口默认值", "http://127.0.0.1:9")["id"]
        case = builder.ui_case("PREF-UI", "窗口默认值", {"start_url": "/", "steps": [{"action": "open", "url": "/"}]})
        projects.save_cases(project, [case])
        os.environ.pop("AUTOSKILL_HEADED", None)
        for selected in (False, True):
            self.save_preference(selected)
            self.assertEqual(runner.prepare(project)["headed"], int(selected))


if __name__ == "__main__":
    unittest.main()
