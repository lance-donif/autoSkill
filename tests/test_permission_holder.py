"""Real detached-worker/socket lifetimes against a local fake CDP peer."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))
from autoskill import paths
from autoskill.recorder import native, permissions
from autoskill.recorder.agent import AgentError
from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
import uvicorn


class PermissionHolderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.calls, cls.connections, cls.close_peer = [], 0, False

        async def cdp(peer):
            await peer.accept()
            cls.connections += 1
            connection = uuid.uuid4().hex
            try:
                while True:
                    request = await peer.receive_json()
                    cls.calls.append({**request, "connection": connection})
                    if cls.close_peer:
                        await peer.close()
                        return
                    method = request["method"]
                    if method == "SystemInfo.getProcessInfo":
                        result = {"processInfo": [{"type": "browser", "id": os.getpid()}]}
                    elif method == "Target.getTargets":
                        result = {"targetInfos": [{"type": "page", "targetId": "default"},
                                                  {"type": "page", "targetId": "isolated", "browserContextId": "context-1"}]}
                    elif method == "Browser.grantPermissions":
                        result = {}
                    else:
                        result = {}
                    await peer.send_json({"id": request["id"], "result": result})
            except Exception:
                pass
            finally:
                cls.connections -= 1

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        app = Starlette(routes=[WebSocketRoute("/devtools/browser/test", cdp)])
        cls.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, loop="asyncio", http="h11", ws="wsproto",
                                                 lifespan="off", log_level="critical"))
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        cls.endpoint = f"ws://127.0.0.1:{port}/devtools/browser/test"
        deadline = time.monotonic() + 10
        while not cls.server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        if not cls.server.started:
            raise RuntimeError("Local fake CDP peer did not start")

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.thread.join(5)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, AUTOSKILL_WORKSPACE=self.temp.name)
        self.env.start()
        self.rec = {"project_id": "permissions", "native_permissions": ["geolocation"]}
        self.session = "permission-test-" + uuid.uuid4().hex[:12]
        self.recording = paths.local() / "recording.json"
        self.recording.write_text(json.dumps(self.rec))
        self.calls_start = len(type(self).calls)
        type(self).close_peer = False

    def tearDown(self):
        try:
            native.stop_permission_holder(self.rec)
        finally:
            type(self).close_peer = False
            self.env.stop()
            self.temp.cleanup()

    def save(self):
        self.recording.write_text(json.dumps(self.rec))

    def wait_dead(self, pid):
        deadline = time.monotonic() + 6
        while permissions._alive(pid) and time.monotonic() < deadline:
            time.sleep(0.04)
        self.assertFalse(permissions._alive(pid), "permission worker did not exit")

    def test_socket_survives_the_launching_cli_and_next_cli_reuses_it(self):
        script = """import json,sys
from pathlib import Path
from autoskill.recorder import native
p=Path(sys.argv[1]);rec=json.loads(p.read_text())
native.ensure_permissions(sys.argv[2],sys.argv[3],rec)
p.write_text(json.dumps(rec))
"""
        run = subprocess.run([sys.executable, "-c", script, str(self.recording), self.session, self.endpoint],
                             env=paths.child_env(), capture_output=True, text=True, timeout=20)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.rec = json.loads(self.recording.read_text())
        pid = self.rec["permission_holder"]["pid"]
        self.assertTrue(permissions._alive(pid))
        self.assertGreaterEqual(type(self).connections, 1)
        native.ensure_permissions(self.session, self.endpoint, self.rec)
        self.save()
        self.assertEqual(self.rec["permission_holder"]["pid"], pid)
        calls = type(self).calls[self.calls_start:]
        grants = [item for item in calls if item["method"] == "Browser.grantPermissions"]
        self.assertEqual(len({item["connection"] for item in grants}), 1)
        self.assertEqual({item["params"].get("browserContextId") for item in grants}, {None, "context-1"})
        folder = permissions._folder(self.rec["permission_holder"])
        self.assertEqual((folder / "control.json").stat().st_mode & 0o777, 0o600)
        native.stop_permission_holder(self.rec)
        self.assertNotIn("permission_holder", self.rec)
        self.assertFalse(folder.exists())
        self.wait_dead(pid)

    def test_updates_are_acknowledged_on_the_same_live_connection(self):
        native.ensure_permissions(self.session, self.endpoint, self.rec)
        self.save()
        previous = dict(self.rec["permission_holder"])
        self.rec["native_permissions"].append("microphone")
        native.ensure_permissions(self.session, self.endpoint, self.rec)
        self.save()
        self.assertEqual(previous["pid"], self.rec["permission_holder"]["pid"])
        self.assertNotEqual(previous["generation"], self.rec["permission_holder"]["generation"])
        grants = [item for item in type(self).calls[self.calls_start:] if item["method"] == "Browser.grantPermissions"]
        self.assertEqual(grants[-1]["params"]["permissions"], ["geolocation", "microphone"])
        self.assertEqual(len({item["connection"] for item in grants}), 1)
        self.assertEqual(self.rec["permission_holder"]["owner_pid"], os.getpid())

    def test_browser_socket_disconnect_exits_without_waiting_for_another_cli(self):
        native.ensure_permissions(self.session, self.endpoint, self.rec)
        self.save()
        pid = self.rec["permission_holder"]["pid"]
        type(self).close_peer = True
        self.wait_dead(pid)

    def test_daemon_pid_sidecar_removal_exits_while_browser_peer_stays_alive(self):
        home = Path(self.temp.name) / "home"
        daemon = home / ".agent-browser" / (self.session + ".pid")
        daemon.parent.mkdir(parents=True)
        daemon.write_text(str(os.getpid()))
        with patch.dict(os.environ, USERPROFILE=str(home)):
            native.ensure_permissions(self.session, self.endpoint, self.rec)
        self.save()
        pid = self.rec["permission_holder"]["pid"]
        daemon.unlink()
        self.wait_dead(pid)

    def test_clearing_permissions_stops_the_holder(self):
        native.ensure_permissions(self.session, self.endpoint, self.rec)
        self.save()
        pid = self.rec["permission_holder"]["pid"]
        self.rec["native_permissions"] = []
        native.ensure_permissions(self.session, self.endpoint, self.rec)
        self.assertNotIn("permission_holder", self.rec)
        self.wait_dead(pid)

    def test_invalid_private_token_cannot_target_another_directory(self):
        self.rec["permission_holder"] = {"token": "../outside", "pid": os.getpid()}
        with self.assertRaises(AgentError):
            native.stop_permission_holder(self.rec)
        self.rec.pop("permission_holder")


if __name__ == "__main__":
    unittest.main()
