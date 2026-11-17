"""Host tasks honor the selected data root's browser preference unless explicitly overridden."""
import base64
import contextlib
import io
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

from autoskill import bridge, builder, cli, db, paths, projects, runner
from autoskill.recorder import agent


class BrowserVisibilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        env = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.folder / "data"), "AUTOSKILL_BRIDGE": "off",
                                      "AUTOSKILL_RUNTIME_CONTEXT": "", "DISPLAY": ":99", "PYTHONPATH": str(APP)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("AUTOSKILL_HEADED", None)
        paths.save_settings(dashboard={"headed": False})

    def test_implicit_runner_record_and_cli_use_saved_preference_and_explicit_flags_win(self):
        pid = projects.create("Visibility fixture", "http://127.0.0.1:9")["id"]
        case = builder.api_case("TC-API-001", "Mode selection", "GET", "/", {"status": 200, "json_equals": {"/ok": True}})
        projects.save_cases(pid, [case])
        self.assertFalse(paths.headed())
        self.assertEqual(runner.prepare(pid)["headed"], 0)
        launch = agent.launch_args(["http://127.0.0.1:9"])
        self.assertNotIn("--headed", launch)
        self.assertNotIn("--start-maximized", " ".join(launch))
        for arguments, wanted in (([], False), (["--headed"], True), (["--headless"], False)):
            with self.subTest(arguments=arguments), patch.dict(os.environ):
                os.environ.pop("AUTOSKILL_HEADED", None)
                output = io.StringIO()
                with patch.object(cli, "dispatch", side_effect=lambda args: ({"headed": paths.headed()}, 0)), contextlib.redirect_stdout(output):
                    self.assertEqual(cli.main(["run", pid, *arguments]), 0)
                self.assertEqual(json.loads(output.getvalue())["headed"], wanted)
        with patch.dict(os.environ, {"AUTOSKILL_HEADED": "1"}):
            self.assertTrue(paths.headed())
            self.assertIn("--headed", agent.launch_args(["http://127.0.0.1:9"]))

    def test_runtime_context_reads_its_own_data_root_preference(self):
        paths.save_settings(dashboard={"headed": True})
        other = self.folder / "other-data"
        with patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(other)}):
            paths.save_settings(dashboard={"headed": False})
        token = base64.urlsafe_b64encode(json.dumps({"version": 1, "workspace": str(other), "port": 8787, "host": "dsh"}).encode()).decode().rstrip("=")
        output = io.StringIO()
        with patch.object(cli, "dispatch", side_effect=lambda args: ({"headed": paths.headed(), "workspace": str(paths.workspace())}, 0)), contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(["--runtime-context=" + token, "project", "list"]), 0)
        actual = json.loads(output.getvalue())
        self.assertEqual(actual, {"headed": False, "workspace": str(other)})

    def test_bridge_boolean_false_overrides_service_environment_and_none_does_not_force_true(self):
        captured = []

        def child(argv, stdin, out, err, extra):
            captured.append(extra)
            return {"exit": 0, "stdout": "{}", "stderr": ""}

        with patch.dict(os.environ, {"AUTOSKILL_HEADED": "1"}), patch.object(bridge, "_execute_child", side_effect=child):
            bridge.execute(["project", "list"], headed=False)
            bridge.execute(["project", "list"], headed=None)
        self.assertEqual(captured[0]["AUTOSKILL_HEADED"], "0")
        self.assertNotIn("AUTOSKILL_HEADED", captured[1])
        with self.assertRaisesRegex(ValueError, "布尔值"):
            bridge.execute(["project", "list"], headed="false")

    @unittest.skipUnless(sys.platform == "linux", "Linux requires a desktop to show Chromium")
    def test_default_replay_and_recording_report_when_desktop_is_missing(self):
        with patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.folder / "first-use")}):
            os.environ.pop("DISPLAY", None)
            os.environ.pop("WAYLAND_DISPLAY", None)
            self.assertEqual(paths.settings(), {})
            self.assertTrue(paths.headed())
            pid = projects.create("No desktop", "http://127.0.0.1:9")["id"]
            case = builder.ui_case("TC-UI-001", "Desktop requirement", {"start_url": "/", "actions": [],
                "assertions": [{"assertion": "title", "expected": "Ready"}]})
            projects.save_cases(pid, [case])
            projects.set_auth(pid, {"ui_case": case["id"]})
            for command in (["run", pid], ["run", pid, "--engine", "selenium"], ["login", pid, "--force"]):
                with self.subTest(command=command):
                    completed = subprocess.run([sys.executable, "-m", "autoskill", *command], capture_output=True, text=True,
                                               timeout=30, check=False)
                    result = json.loads(completed.stdout)
                    self.assertEqual(completed.returncode, 2, result)
                    self.assertEqual(result["status"], "Blocked", result)
                    self.assertIn("需要图形桌面", result["results"][0]["detail"])
                    self.assertEqual(db.one("SELECT headed FROM runs WHERE id = ?", (result["run_id"],))["headed"], 1)
            if paths.agent_browser():
                completed = subprocess.run([sys.executable, "-m", "autoskill", "record", "start", pid, "--no-login"],
                                           capture_output=True, text=True, timeout=30, check=False)
                self.assertEqual(completed.returncode, 2, completed.stdout)
                self.assertIn("需要图形桌面", json.loads(completed.stdout)["error"])

    def test_real_explicitly_saved_hidden_preference_applies_without_desktop(self):
        if not paths.chrome():
            self.skipTest("Actual implicit browser preference acceptance requires Chromium")
        os.environ.pop("DISPLAY", None)
        os.environ.pop("WAYLAND_DISPLAY", None)
        user_agents = []

        class Site(BaseHTTPRequestHandler):
            def do_GET(self):
                user_agents.append(self.headers.get("User-Agent", ""))
                body = b'<main data-testid="state">ready</main>'
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                return

        site = ThreadingHTTPServer(("127.0.0.1", 0), Site)
        threading.Thread(target=site.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.addCleanup(site.server_close)
        self.addCleanup(site.shutdown)
        pid = projects.create("Actual implicit browser", f"http://127.0.0.1:{site.server_port}")["id"]
        case = builder.ui_case("TC-UI-001", "Actual browser fixture", {"start_url": "/", "actions": [],
            "assertions": [{"assertion": "text", "locators": [{"by": "testid", "value": "state"}], "expected": "ready"}]})
        projects.save_cases(pid, [case])
        projects.set_auth(pid, {"ui_case": case["id"]})
        for command in (["run", pid], ["login", pid, "--force"]):
            with self.subTest(command=command):
                completed = subprocess.run([sys.executable, "-m", "autoskill", *command], env=os.environ.copy(),
                                           capture_output=True, text=True, timeout=30, check=False)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                result = json.loads(completed.stdout)
                self.assertEqual(result["status"], "Pass", result)
                self.assertEqual(db.one("SELECT headed FROM runs WHERE id = ?", (result["run_id"],))["headed"], 0)
        if paths.agent_browser():
            try:
                recording = subprocess.run([sys.executable, "-m", "autoskill", "record", "start", pid, "--no-login"], env=os.environ.copy(),
                                           capture_output=True, text=True, timeout=60, check=False)
                self.assertEqual(recording.returncode, 0, (recording.stdout, recording.stderr))
                self.assertTrue(json.loads(recording.stdout)["recording"])
            finally:
                subprocess.run([sys.executable, "-m", "autoskill", "record", "cancel"], env=os.environ.copy(),
                               capture_output=True, text=True, timeout=30, check=False)
        self.assertGreaterEqual(len(user_agents), 2)
        self.assertTrue(all("HeadlessChrome" in value for value in user_agents), user_agents)


if __name__ == "__main__":
    unittest.main()
