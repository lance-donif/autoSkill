"""Real HTTP outbox and CLI bridge callbacks work with a programmed host, not a live DSH model."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import uvicorn

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "skills/autoskill/app"
sys.path.insert(0, str(APP))

from autoskill import dialogue_judge
from autoskill.server import scheduler
from autoskill.server.app import create_app


class HostOutboxLoopTests(unittest.TestCase):
    def test_two_task_sessions_use_real_judge_callbacks_and_delivery_receipts(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": directory, "AUTOSKILL_BRIDGE": "off"}):
            requests = []
            for session, answer in (("甲", "medium"), ("乙", "high")):
                context = {"run_id": "run-" + session, "session_id": "task-" + session, "host": "dsh", "parameters": {"host_session_id": "host-" + session}}
                requests.append(dialogue_judge.submit_request(context, {"id": "case-1"}, 1, "优先级必须为 high", [], answer,
                                                             {"rule": "核对原始需求"}, {"expected": "high"}, idempotency_key="request"))
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen(128)
            port = listener.getsockname()[1]
            server = uvicorn.Server(uvicorn.Config(create_app(start_worker=False), log_level="error"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(server.started)
                process = subprocess.run(["node", str(ROOT / "tests/host-outbox-fixture.mjs"), f"http://127.0.0.1:{port}", directory, sys.executable, str(APP)],
                                         cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=40, check=False)
                self.assertEqual(process.returncode, 0, process.stderr)
                calls = json.loads(process.stdout)["calls"]
                self.assertEqual({call["task_session"] for call in calls}, {"task-甲", "task-乙"})
                self.assertEqual({call["session_id"] for call in calls}, {"host-甲", "host-乙"})
                self.assertEqual({call["verdict"] for call in calls}, {"pass", "fail"})
                for request in requests:
                    self.assertEqual(dialogue_judge.get(request["judge_id"])["status"], "completed")
                self.assertEqual(scheduler.pending_deliveries("dsh")["total"], 0)
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                listener.close()
            self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
