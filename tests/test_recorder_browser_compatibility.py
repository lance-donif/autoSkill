"""Real recording keeps its original Chrome connection through capture and replay."""
import json
import os
import re
import shutil
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
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill import builder, conversation_capture, db, execution_inputs, paths, projects, runner, runtime_identity, task_context
from autoskill.recorder import agent, session
from autoskill.replay import codegen

PAGE = b"""<!doctype html><label>Name<input id="name"></label><button id="submit">Greet</button>
<p id="result">ready</p><script>
document.querySelector('#submit').onclick = async () => {
 const response = await fetch('/greet?name=' + encodeURIComponent(document.querySelector('#name').value));
 document.querySelector('#result').textContent = (await response.json()).message;
};</script>"""


class RecordingTarget(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path)
        if path.path == "/greet":
            name = parse_qs(path.query).get("name", [""])[0]
            body = json.dumps({"message": "Hello " + name}).encode()
            mime = "application/json"
        else:
            body, mime = PAGE, "text/html"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class AmbiguousTarget(RecordingTarget):
    def do_GET(self):
        body = '''<!doctype html><title>同名操作</title><table>
<tr><td>第一行</td><td><button id="first" onclick="document.querySelector('#result').textContent='第一行已处理'">处理</button></td></tr>
<tr><td>第二行</td><td><button id="second" onclick="document.querySelector('#result').textContent='第二行已处理'">处理</button></td></tr>
</table><output id="result">等待操作</output>'''.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ReuseTarget(RecordingTarget):
    def do_GET(self):
        if self.path == "/prepare-click":
            self.server.preparation_clicks += 1
            self.send_response(204)
            self.end_headers()
            return
        body = b'''<!doctype html><button id="prepare">Prepare</button><button id="next" disabled>Continue</button>
<p id="result">@@INITIAL@@</p><script>
  const result = document.getElementById('result'), next = document.getElementById('next');
  document.getElementById('prepare').onclick = async () => {
    await fetch('/prepare-click');
    next.disabled = result.textContent !== 'new document';
    result.textContent = next.disabled ? 'preparation failed' : 'prepared';
  };
  next.onclick = () => { result.textContent = 'finished'; };
</script>'''.replace(b'@@INITIAL@@', b'new document' if self.server.can_prepare else b'unavailable')
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)


@unittest.skipUnless(paths.agent_browser() and paths.chrome(), "requires the actual recording browser")
class RecorderBrowserCompatibilityTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "linux", "Linux Chromium temporary socket locations")
    def test_long_tmpdir_keeps_real_recording_and_both_replay_engines_operational(self):
        if not paths.chromedriver():
            self.skipTest("requires matching ChromeDriver")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            temporary = root / ("测试 临时" * 12)
            temporary.mkdir()
            self.enterContext(patch.dict(os.environ, {"TMPDIR": str(temporary), "AUTOSKILL_WORKSPACE": str(root / "data"),
                                                      "AUTOSKILL_BRIDGE": "off", "AUTOSKILL_HEADED": "0"}))
            self.enterContext(task_context.use_session("long-temp"))
            site = ThreadingHTTPServer(("127.0.0.1", 0), ReuseTarget)
            self.addCleanup(site.server_close)
            site.can_prepare, site.preparation_clicks = True, 0
            thread = threading.Thread(target=site.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
            thread.start()
            self.addCleanup(site.shutdown)
            project = projects.create("临时目录中的订单", f"http://127.0.0.1:{site.server_port}")["id"]
            session.start(project, login=False)
            try:
                session.browser(["click", "#prepare"])
                self.assertTrue(session.check("text", "#result", "prepared")["passed"])
            finally:
                session.cancel()
            case = builder.ui_case("TC-UI-001", "提交订单", {"start_url": "/", "actions": [{"action": "click", "selector": "#prepare"}],
                                   "assertions": [{"assertion": "text", "selector": "#result", "expected": "prepared"}]})
            projects.save_cases(project, [case])
            for engine in ("playwright", "selenium"):
                with self.subTest(engine=engine):
                    result = runner.execute(runner.create(project, [case["id"]], engine=engine)["id"])
                    self.assertEqual(result["status"], "Pass", result)
            self.assertEqual(site.preparation_clicks, 3)
            self.assertEqual(os.environ["TMPDIR"], str(temporary))

    def test_saved_setup_stays_on_the_page_and_preserves_the_selected_source_revision(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
                "AUTOSKILL_WORKSPACE": folder, "AUTOSKILL_BRIDGE": "off", "AUTOSKILL_HEADED": "0"}), task_context.use_session("reuse"):
            site = ThreadingHTTPServer(("127.0.0.1", 0), ReuseTarget)
            site.can_prepare = True
            site.preparation_clicks = 0
            thread = threading.Thread(target=site.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{site.server_port}"
                project = projects.create("Same-tab preparation", base)["id"]
                original = builder.ui_case("TC-UI-001", "Prepare the page", {
                    "start_url": "/", "actions": [
                        {"assertion": "text", "selector": "#result", "expected": "new document", "timeout_seconds": 0.4},
                        {"action": "click", "selector": "#prepare"},
                    ],
                    "assertions": [{"assertion": "text", "selector": "#result", "expected": "prepared", "timeout_seconds": 0.4}],
                })
                projects.save_cases(project, [original])
                started = session.start(project, login=False, reuse=original["id"])
                self.assertEqual(started["setup"]["status"], "ready")
                self.assertIn('button "Continue"', started["snapshot"])
                self.assertEqual(session.status()["steps"], [])
                session.browser(["click", "#next"])
                self.assertTrue(session.check("text", "#result", "finished")["passed"])
                with patch("autoskill.server.jobs.ensure_worker"):
                    saved = session.stop("Continue on the prepared page")
                newer = {**original, "revision": 2, "title": "Later revision"}
                projects.save_cases(project, [newer])
                run = runner.execute(saved["run_id"])
                self.assertEqual(run["status"], "Pass", run)
                stored = projects.case(project, saved["ui_case"]["id"])
                flow = stored["extensions"]["automation"]
                self.assertEqual([step["action"] for step in flow["actions"]], ["click"])
                self.assertEqual(flow["setup_cases"][0]["revision"], 1)
                self.assertEqual(flow["setup_cases"][0]["content_hash"], execution_inputs.content_hash(original))
                chain, separate_login = session._reuse_stages(project, stored["id"], original["id"], True)
                self.assertEqual([stage["case_id"] for stage in chain], [original["id"], stored["id"]])
                self.assertIsNone(separate_login)
                row = db.one("SELECT * FROM results WHERE run_id = ?", (run["id"],))
                detail = json.loads(row["detail"])
                self.assertEqual([check["expected"] for check in detail["assertions"]], ["finished"])
                self.assertEqual([check["expected"] for check in detail["setup_cases"][0]["assertions"]], ["new document", "prepared"])
                self.assertTrue(execution_inputs.result_evidence(run, row))
                detail["setup_cases"][0]["revision"] = 2
                self.assertFalse(execution_inputs.result_evidence(run, {**row, "detail": detail}))
                self.assert_standalone_reuses_page(stored, base, Path(folder))
                with (patch.object(session.capture, "start", side_effect=RuntimeError("test capture interrupted")),
                      self.assertRaisesRegex(RuntimeError, "test capture interrupted")):
                    session.start(project, login=False, reuse=original["id"])
                interrupted = session.status()
                self.assertEqual(interrupted["setup"]["status"], "interrupted")
                self.assertEqual(interrupted["setup"]["cases"][0]["status"], "Pass")
                clicks = site.preparation_clicks
                with self.assertRaisesRegex(session.RecordError, "上次录制尚未就绪"):
                    session.start(project, login=False, reuse=original["id"])
                self.assertEqual(site.preparation_clicks, clicks)
                session.cancel()
                site.can_prepare = False
                clicks = site.preparation_clicks
                blocked = runner.execute(runner.create(project, [stored["id"]])["id"])
                self.assertEqual(blocked["status"], "Blocked", blocked)
                failure = db.one("SELECT category, detail FROM results WHERE run_id = ?", (blocked["id"],))
                self.assertEqual(failure["category"], "prerequisite_failed")
                trace = json.loads(failure["detail"])
                self.assertEqual(trace["setup_cases"][0]["status"], "Fail")
                self.assertEqual(len(trace["steps"]), 1)
                with self.assertRaisesRegex(session.RecordError, "前置用例 TC-UI-001 未通过"):
                    session.start(project, login=False, reuse=original["id"])
                self.assertFalse(session.status()["recording"])
                self.assertEqual(site.preparation_clicks, clicks)
            finally:
                session.cancel()
                site.shutdown()
                site.server_close()
                thread.join(timeout=3)

    def assert_standalone_reuses_page(self, case, base_url, folder):
        engines = ("playwright", "selenium") if paths.chromedriver() else ("playwright",)
        for engine in engines:
            with self.subTest(export=engine):
                script = folder / (engine + ".py")
                script.write_text(codegen.script([case], base_url, engine), encoding="utf-8")
                env = {**os.environ, "HEADED": "0", "CHROME_BINARY": paths.chrome(), "TIMEOUT": "5",
                       "NO_PROXY": "localhost,127.0.0.1,::1", "no_proxy": "localhost,127.0.0.1,::1"}
                if engine == "selenium":
                    env["CHROMEDRIVER"] = paths.chromedriver()
                result = subprocess.run([sys.executable, "-I", str(script)], env=env, capture_output=True,
                                        text=True, timeout=45, check=False)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_snapshot_hints_distinguish_same_name_buttons_and_preserve_replay(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
                "AUTOSKILL_WORKSPACE": folder, "AUTOSKILL_BRIDGE": "off", "AUTOSKILL_HEADED": "0"}):
            site = ThreadingHTTPServer(("127.0.0.1", 0), AmbiguousTarget)
            thread = threading.Thread(target=site.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
            thread.start()
            with task_context.use_session("snapshot-hints"):
                try:
                    project = projects.create("同名控件提示", f"http://127.0.0.1:{site.server_port}")["id"]
                    started = session.start(project, login=False)
                    hinted = [line for line in started["snapshot"].splitlines() if 'hint="' in line and "id:second" in line]
                    self.assertEqual(len(hinted), 1, started["snapshot"])
                    reference = re.search(r"\[ref=(e\d+)\]", hinted[0]).group(1)
                    self.assertEqual(session.current()["refs"][reference]["name"], "处理")
                    session.browser(["click", "@" + reference])
                    self.assertTrue(session.check("text", "#result", "第二行已处理")["passed"])
                    with patch("autoskill.server.jobs.ensure_worker"):
                        saved = session.stop("用快照提示选择第二行操作")
                    self.assertEqual(saved["test_report"]["status"], "ready")
                    self.assertEqual(runner.execute(saved["run_id"])["status"], "Pass")
                finally:
                    session.cancel()
                    site.shutdown()
                    site.server_close()
                    thread.join(timeout=3)

    @unittest.skipUnless(sys.platform == "linux" and os.environ.get("DISPLAY") and shutil.which("xwininfo") and shutil.which("xprop"),
                         "requires a real X11 display and window inspection tools")
    def test_default_and_explicit_recording_create_mapped_owned_windows(self):
        site = ThreadingHTTPServer(("127.0.0.1", 0), RecordingTarget)
        thread = threading.Thread(target=site.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        try:
            for explicit in (False, True):
                with (self.subTest(explicit=explicit), tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
                        "AUTOSKILL_WORKSPACE": folder, "AUTOSKILL_BRIDGE": "off"}), task_context.use_session("visible-recording")):
                    os.environ.pop("AUTOSKILL_HEADED", None)
                    if explicit:
                        paths.save_settings(dashboard={"headed": False})
                        os.environ["AUTOSKILL_HEADED"] = "1"
                    original_settings = paths.settings()
                    try:
                        project = projects.create("Visible recording", f"http://127.0.0.1:{site.server_port}")["id"]
                        started = session.start(project, login=False)
                        self.assertTrue(started["recording"])
                        self.assertTrue(started["headed"])
                        self.assertTrue(session.current()["headed"])
                        self.assertEqual(paths.settings(), original_settings)
                        self.assert_recording_window()
                        self.assertTrue(session.check("text", "#result", "ready")["passed"])
                    finally:
                        session.cancel()
        finally:
            site.shutdown()
            site.server_close()
            thread.join(timeout=3)

    def assert_recording_window(self):
        name = session.session_name()
        title = "autoskill-visible-" + uuid.uuid4().hex
        agent.call(name, ["eval", "--stdin"], stdin_text="document.title = " + json.dumps(title))
        deadline, own_window = time.monotonic() + 4, ""
        while not own_window and time.monotonic() < deadline:
            tree = subprocess.run(["xwininfo", "-root", "-tree"], capture_output=True, text=True, timeout=5, check=True).stdout
            own_window = next((line for line in tree.splitlines() if title in line), "")
            if not own_window:
                time.sleep(0.05)
        self.assertTrue(own_window, "The recording page has no mapped X11 browser window")
        window = re.search(r"0x[0-9a-fA-F]+", own_window).group()
        details = subprocess.run(["xwininfo", "-id", window], capture_output=True, text=True, timeout=5, check=True).stdout
        self.assertIn("Map State: IsViewable", details)
        properties = subprocess.run(["xprop", "-id", window, "_NET_WM_PID"], capture_output=True, text=True, timeout=5, check=True).stdout
        browser = int(properties.rsplit("=", 1)[1].strip())
        process = Path(f"/proc/{browser}")
        parent = int(process.joinpath("stat").read_text().rsplit(") ", 1)[1].split()[1])
        self.assertEqual(parent, agent._controller_pid(name))
        self.assertIsNotNone(runtime_identity.process_birth(browser))
        arguments = process.joinpath("cmdline").read_bytes().split(b"\0")
        self.assertFalse(any(value.startswith(b"--headless") for value in arguments))
        self.assertIn(b"--start-maximized", arguments)

    def test_fresh_recorders_keep_the_same_browser_and_save_replayable_business_evidence(self):
        import websocket

        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
                "AUTOSKILL_WORKSPACE": folder, "AUTOSKILL_BRIDGE": "off", "AUTOSKILL_HEADED": "0"}):
            site = ThreadingHTTPServer(("127.0.0.1", 0), RecordingTarget)
            thread = threading.Thread(target=site.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{site.server_port}"
                project = projects.create("Chrome startup compatibility", base)["id"]
                for index in range(2):
                    with self.subTest(recording=index), task_context.use_session(f"fresh-recording-{index}"):
                        connection = None
                        try:
                            self.assertTrue(session.start(project, login=False)["recording"])
                            name = session.session_name()
                            controller = agent._controller_pid(name)
                            owner = json.loads((paths.browser_control() / (name + ".owner.json")).read_text())
                            self.assertTrue(runtime_identity.owns(controller, owner))
                            endpoint = agent.call(name, ["get", "cdp-url"])["cdpUrl"]
                            connection = websocket.create_connection(endpoint, timeout=2, suppress_origin=True)
                            for identity in range(1, 7):
                                connection.send(json.dumps({"id": identity, "method": "Browser.getVersion"}))
                                reply = json.loads(connection.recv())
                                self.assertEqual(reply["id"], identity)
                                self.assertIn("Chrome/", reply["result"]["product"])
                                time.sleep(0.5)
                            session.browser(["fill", "#name", "recorder"])
                            session.browser(["click", "#submit"])
                            self.assertTrue(session.check("text", "#result", "Hello recorder")["passed"])
                            self.assertEqual(agent.call(name, ["get", "cdp-url"])["cdpUrl"], endpoint)
                            self.assertEqual(agent._controller_pid(name), controller)
                            self.assertTrue(runtime_identity.owns(controller, owner))
                            with patch("autoskill.server.jobs.ensure_worker"):
                                saved = session.stop("Recorded greeting " + str(index))
                            source = conversation_capture.get(project, saved["conversation_captures"]["id"])
                            requests = [row for row in source["requests"] if "/greet?" in row["request"]["url"]]
                            self.assertEqual(len(requests), 1)
                            self.assertEqual(json.loads(requests[0]["response"]["content"]["text"]), {"message": "Hello recorder"})
                            case = saved["ui_case"]["id"]
                            self.assertEqual(runner.execute(saved["run_id"])["status"], "Pass")
                            result = db.one("SELECT status, detail FROM results WHERE run_id = ? AND case_id = ?",
                                            (saved["run_id"], case))
                            self.assertEqual(result["status"], "Pass")
                            self.assertEqual(json.loads(result["detail"])["business_status"], "Pass")
                        finally:
                            if connection is not None:
                                connection.close()
                            session.cancel()
            finally:
                site.shutdown()
                site.server_close()
                thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
