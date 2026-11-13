"""Separate native runs share process budgets but never a replay login-state lock."""
import contextlib
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill import builder, db, native_assets, native_run, paths, projects, runner
from autoskill.server import jobs


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        with self.server.guard:
            self.server.active += 1
            self.server.calls += 1
            self.server.maximum = max(self.server.maximum, self.server.active)
            self.server.entered.set()
            if self.server.active == 2:
                self.server.overlapped.set()
        try:
            self.server.release.wait(12)
            data = b'{"answer":42,"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(data)
        finally:
            with self.server.guard:
                self.server.active -= 1


class NativeParallelQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": self.tmp.name, "AUTOSKILL_HEADED": "0", "AUTOSKILL_MAX_PARALLEL": "2"})
        self.environment.start()
        self.children = []
        self.site = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.site.guard = threading.Lock()
        self.site.entered, self.site.overlapped, self.site.release = threading.Event(), threading.Event(), threading.Event()
        self.site.active = self.site.maximum = self.site.calls = 0
        threading.Thread(target=self.site.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.site.server_port}"
        self.pid = projects.create("Same project independent tasks", self.url, project_id="parallel-native")["id"]
        file = self.root / "spec.json"
        file.write_text(json.dumps({"endpoint": {"url": self.url + "/ask"}, "samples": [{"id": "answer", "question": "value", "expected": 42}]}))
        self.asset = native_assets.import_asset(self.pid, str(file), "ai-eval")
        case = builder.api_case("TC-API-001", "Replay with shared login scope", "POST", "/replay", {"status": 200, "json_equals": {"/ok": True}})
        projects.save_cases(self.pid, [case])

    def tearDown(self):
        self.site.release.set()
        for child in self.children:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=10)
        runner.reap_children()
        self.site.shutdown()
        self.site.server_close()
        self.environment.stop()
        self.tmp.cleanup()

    def spawn(self, run):
        pid = runner.spawn(run["id"])
        self.assertIsNotNone(pid)
        self.children.append(runner._children[pid])

    def finished(self, *runs):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            jobs.sweep()
            rows = [db.one("SELECT * FROM runs WHERE id = ?", (run["id"],)) for run in runs]
            if all(row["status"] in db.FINISHED for row in rows):
                self.assertEqual([row["status"] for row in rows], ["Pass"] * len(rows), [row["reason"] for row in rows])
                return rows
            time.sleep(0.02)
        raise AssertionError("Run did not complete")

    def native_pair(self):
        return [native_run.enqueue(self.pid, self.asset["id"], session_id=session) for session in ("native-session-a", "native-session-b")]

    def test_two_native_sessions_in_one_project_have_simultaneous_real_http_requests(self):
        first, second = self.native_pair()
        self.spawn(first)
        self.spawn(second)
        self.assertTrue(self.site.overlapped.wait(10), "The second native session was serialized behind the first")
        self.assertEqual(len(db.rows("SELECT * FROM runs WHERE project_id = ? AND status = 'Running'", (self.pid,))), 2)
        self.site.release.set()
        finished = self.finished(first, second)
        self.assertEqual({row["session_id"] for row in finished}, {"native-session-a", "native-session-b"})
        self.assertEqual((self.site.calls, self.site.maximum), (2, 2))
        self.assertEqual(len(db.rows("SELECT * FROM results")), 2)
        for run in (first, second):
            self.assertTrue((paths.local() / "runs" / run["id"] / "native/input/spec.json").is_file())
            self.assertTrue(native_run.get(run["id"])["results"][0]["detail"]["business_verified"])

    def test_replay_sessions_in_one_project_still_serialize_shared_login_state(self):
        first, second = [runner.create(self.pid, session_id=session) for session in ("replay-session-a", "replay-session-b")]
        self.spawn(first)
        self.assertTrue(self.site.entered.wait(10))
        self.assertIsNone(runner.spawn(second["id"]))
        self.assertEqual(db.one("SELECT status FROM runs WHERE id = ?", (second["id"],))["status"], "Queued")
        self.site.release.set()
        self.finished(first)
        self.spawn(second)
        self.finished(second)
        self.assertEqual((self.site.calls, self.site.maximum), (2, 1))

    def test_native_parallelism_still_respects_the_configured_global_budget(self):
        first, second = self.native_pair()
        with patch.dict(os.environ, {"AUTOSKILL_MAX_PARALLEL": "1"}):
            self.spawn(first)
            self.assertTrue(self.site.entered.wait(10))
            self.assertIsNone(runner.spawn(second["id"]))
            self.assertEqual(db.one("SELECT status FROM runs WHERE id = ?", (second["id"],))["status"], "Queued")
            self.site.release.set()
            self.finished(first)
            self.spawn(second)
            self.finished(second)
        self.assertEqual((self.site.calls, self.site.maximum), (2, 1))


if __name__ == "__main__":
    unittest.main()
