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

from autoskill import conversation_capture, db, paths, projects, runner, runtime_identity, task_context
from autoskill.recorder import agent, session

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


@unittest.skipUnless(paths.agent_browser() and paths.chrome(), "requires the actual recording browser")
class RecorderBrowserCompatibilityTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "linux" and os.environ.get("DISPLAY") and shutil.which("xwininfo") and shutil.which("xprop"),
                         "requires a real X11 display and window inspection tools")
    def test_explicitly_visible_recording_creates_a_mapped_owned_window(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
                "AUTOSKILL_WORKSPACE": folder, "AUTOSKILL_BRIDGE": "off", "AUTOSKILL_HEADED": "1"}):
            site = ThreadingHTTPServer(("127.0.0.1", 0), RecordingTarget)
            thread = threading.Thread(target=site.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
            thread.start()
            with task_context.use_session("visible-recording"):
                try:
                    project = projects.create("Visible recording", f"http://127.0.0.1:{site.server_port}")["id"]
                    self.assertTrue(session.start(project, login=False)["recording"])
                    name = session.session_name()
                    title = "autoskill-visible-" + uuid.uuid4().hex
                    agent.call(name, ["eval", "--stdin"], stdin_text="document.title = " + json.dumps(title))
                    deadline, own_window = time.monotonic() + 4, ""
                    while not own_window and time.monotonic() < deadline:
                        tree = subprocess.run(["xwininfo", "-root", "-tree"], capture_output=True, text=True,
                                              timeout=5, check=True).stdout
                        own_window = next((line for line in tree.splitlines() if title in line), "")
                        if not own_window:
                            time.sleep(0.05)
                    self.assertTrue(own_window, "The recording page has no mapped X11 browser window")
                    window = re.search(r"0x[0-9a-fA-F]+", own_window).group()
                    details = subprocess.run(["xwininfo", "-id", window], capture_output=True, text=True,
                                             timeout=5, check=True).stdout
                    self.assertIn("Map State: IsViewable", details)
                    properties = subprocess.run(["xprop", "-id", window, "_NET_WM_PID"], capture_output=True, text=True,
                                                timeout=5, check=True).stdout
                    browser = int(properties.rsplit("=", 1)[1].strip())
                    process = Path(f"/proc/{browser}")
                    parent = int(process.joinpath("stat").read_text().rsplit(") ", 1)[1].split()[1])
                    self.assertEqual(parent, agent._controller_pid(name))
                    self.assertIsNotNone(runtime_identity.process_birth(browser))
                    arguments = process.joinpath("cmdline").read_bytes().split(b"\0")
                    self.assertFalse(any(value.startswith(b"--headless") for value in arguments))
                    self.assertIn(b"--start-maximized", arguments)
                    self.assertTrue(session.check("text", "#result", "ready")["passed"])
                finally:
                    session.cancel()
                    site.shutdown()
                    site.server_close()
                    thread.join(timeout=3)

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
                            saved = session.stop("Recorded greeting " + str(index))
                            source = conversation_capture.get(project, saved["conversation_captures"]["id"])
                            requests = [row for row in source["requests"] if "/greet?" in row["request"]["url"]]
                            self.assertEqual(len(requests), 1)
                            self.assertEqual(json.loads(requests[0]["response"]["content"]["text"]), {"message": "Hello recorder"})
                            case = saved["ui_case"]["id"]
                            run = runner.create(project, [case])
                            self.assertEqual(runner.execute(run["id"])["status"], "Pass")
                            result = db.one("SELECT status, detail FROM results WHERE run_id = ? AND case_id = ?",
                                            (run["id"], case))
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
