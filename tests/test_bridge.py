"""A sandboxed caller hands its command to the local service, and every failure is answered in JSON."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "skills/autoskill/app"
sys.path.insert(0, str(APP))
__import__("os").environ.setdefault("AUTOSKILL_HEADED", "0")  # tests never open browser windows
from fastapi.testclient import TestClient

from autoskill import paths
from autoskill.server.app import create_app

KEYS = ("AUTOSKILL_WORKSPACE", "USERPROFILE", "AUTOSKILL_BRIDGE", "AUTOSKILL_BRIDGE_SERVER")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Sandbox(unittest.TestCase):
    """A workspace, plus a home folder in which the browser controller's folder cannot be created."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.workspace = Path(self.tmp.name) / "ws"
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        (self.home / ".agent-browser").write_text("", encoding="ascii")  # a file where the folder should be
        self.saved = {key: os.environ.get(key) for key in KEYS}
        os.environ["AUTOSKILL_WORKSPACE"] = str(self.workspace)
        self.server = None

    def tearDown(self):
        if self.server:
            self.server.terminate()
            self.server.wait(20)
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def env(self, **extra) -> dict:
        env = {k: v for k, v in os.environ.items() if not k.startswith("AUTOSKILL_")}
        env.update(PYTHONPATH=str(APP), PYTHONUTF8="1", AUTOSKILL_WORKSPACE=str(self.workspace))
        env.update(extra)
        return env

    def cli(self, *argv, sandboxed=True, **extra):
        home = {"USERPROFILE": str(self.home)} if sandboxed else {}
        done = subprocess.run([sys.executable, "-m", "autoskill", *argv], env=self.env(**home, **extra), capture_output=True,
                              text=True, encoding="utf-8", stdin=subprocess.DEVNULL, timeout=120)
        return done.returncode, done.stdout, done.stderr

    def start_service(self) -> int:
        port = free_port()
        self.server = subprocess.Popen([sys.executable, "-m", "autoskill", "dashboard", "--port", str(port)], env=self.env(),
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(80):
            try:
                with opener.open(f"http://127.0.0.1:{port}/api/status", timeout=1) as reply:
                    if json.loads(reply.read())["bridge"] and paths.service_file().is_file():
                        return port
            except (OSError, ValueError, KeyError):
                pass
            time.sleep(0.25)
        self.fail("the service did not start")


class RestrictedProcessTests(Sandbox):
    @unittest.skipUnless(paths.agent_browser() and paths.chrome(), "browser runtime required")
    def test_recording_through_service_keeps_its_output_files(self):
        from test_replay_ui import serve

        site, origin = serve()
        try:
            self.start_service()
            code, out, err = self.cli("project", "create", "--name", "bridge recording", "--url", origin, "--id", "bridge-recording")
            self.assertEqual(code, 0, out + err)
            code, out, err = self.cli("record", "start", "bridge-recording")
            self.assertEqual(code, 0, out + err)
            self.assertIn("snapshot", json.loads(out))
            code, out, err = self.cli("record", "cancel")
            self.assertEqual(code, 0, out + err)
        finally:
            site.shutdown()
            site.server_close()

    def blocked_workspace(self) -> str:
        blocked = Path(self.tmp.name) / "blocked"
        blocked.write_text("", encoding="ascii")
        return str(blocked / "ws")

    def test_a_process_that_cannot_keep_data_or_start_a_browser_is_recognised(self):
        self.assertEqual(paths.restricted(), "")
        os.environ["USERPROFILE"] = str(self.home)
        self.assertIn("浏览器控制目录不可写", paths.restricted())
        os.environ["AUTOSKILL_WORKSPACE"] = self.blocked_workspace()
        self.assertIn("数据目录不可写", paths.restricted())

    def test_without_a_service_the_answer_is_json_that_says_what_to_do(self):
        code, out, _ = self.cli("project", "list")
        self.assertEqual(code, 2)
        error = json.loads(out)["error"]
        for expected in ("受限沙箱", "后台服务", "dashboard", "完全访问权限"):
            self.assertIn(expected, error)
        self.assertFalse((self.workspace / "projects").exists())  # nothing was created half-way

    def test_a_failure_nobody_planned_for_is_still_one_json_object(self):
        code, out, err = self.cli("project", "list", sandboxed=False, AUTOSKILL_WORKSPACE=self.blocked_workspace(),
                                  AUTOSKILL_BRIDGE="off")
        self.assertEqual(code, 2)
        self.assertIn("读写 autoSkill 数据失败", json.loads(out)["error"])
        self.assertIn("Traceback", err)  # the detail goes to stderr, never into stdout


class BridgeTests(Sandbox):
    def test_a_sandboxed_command_runs_through_the_service_with_the_same_output(self):
        port = self.start_service()
        code, out, _ = self.cli("project", "create", "--name", "沙箱项目", "--url", "http://127.0.0.1:9/app", "--id", "boxed")
        self.assertEqual((code, json.loads(out)["created"]["id"]), (0, "boxed"))
        self.assertTrue((self.workspace / "projects" / "boxed" / "project.json").is_file())  # the service's data, not a copy
        code, out, _ = self.cli("project", "list")
        self.assertEqual([p["id"] for p in json.loads(out)["projects"]], ["boxed"])
        code, out, _ = self.cli("context", "missing")  # errors and exit codes come back unchanged
        self.assertEqual((code, json.loads(out)["error"]), (2, "项目 missing 不存在"))
        code, out, _ = self.cli("doctor")
        self.assertIn("后台服务", json.loads(out)["executed_by"])
        code, out, _ = self.cli("secret", "set", "BOXED_PASSWORD", "p@ss word")
        self.assertEqual((code, json.loads(out)["saved"]), (0, "BOXED_PASSWORD"))
        listed = json.loads(self.cli("secret", "list", sandboxed=False)[1])
        self.assertIn("BOXED_PASSWORD", json.dumps(listed))
        self.assertNotIn("p@ss word", json.dumps(listed))
        self.assertEqual(json.loads(paths.service_file().read_text(encoding="utf-8"))["port"], port)

    def test_what_the_service_refuses_to_do_for_someone_else(self):
        self.start_service()
        self.cli("project", "create", "--name", "p", "--url", "http://127.0.0.1:9", "--id", "boxed")
        outside = str(Path(self.tmp.name) / "outside")
        for argv in (["report", "boxed", "--out", outside], ["export", "scripts", "boxed", "--out", outside],
                     ["pack", "--out", outside], ["pack", "--full"], ["pack", "--include-secrets", "--passphrase", "x"]):
            code, out, _ = self.cli(*argv)
            self.assertEqual(code, 2, argv)
            self.assertIn("error", json.loads(out), argv)
        self.assertFalse(Path(outside).exists())
        code, out, _ = self.cli("project", "list", AUTOSKILL_WORKSPACE=str(Path(self.tmp.name) / "elsewhere"))
        self.assertEqual(code, 2)  # a caller that moved its data folder is not silently served from another one
        self.assertIn("error", json.loads(out))

    def test_web_pages_cannot_reach_the_command_endpoint(self):
        body = {"argv": ["project", "list"]}
        marked = {"X-Autoskill-Bridge": "1"}
        with TestClient(create_app(8787, start_worker=False), base_url="http://127.0.0.1:8787") as client:
            post = client.post
            self.assertEqual(post("/api/bridge", json=body).status_code, 403)  # no marker header
            self.assertEqual(post("/api/bridge", json=body, headers={**marked, "Origin": "http://127.0.0.1:8787"}).status_code, 403)
            self.assertEqual(post("/api/bridge", json=body, headers={**marked, "Host": "evil.test"}).status_code, 403)
            self.assertEqual(post("/api/bridge", json={"argv": "project list"}, headers=marked).status_code, 400)
            self.assertEqual(post("/api/bridge", json={"argv": ["dashboard"]}, headers=marked).status_code, 400)
            os.environ["AUTOSKILL_BRIDGE_SERVER"] = "off"
            self.assertEqual(post("/api/bridge", json=body, headers=marked).status_code, 403)
            self.assertFalse(client.get("/api/status").json()["bridge"])


if __name__ == "__main__":
    unittest.main()
