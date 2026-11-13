"""A host-like CLI/HTTP loop preserves a real business defect through completion."""
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill import builder, cli, command_input, paths, projects
from autoskill.server.app import create_app


class ActualPriority(BaseHTTPRequestHandler):
    def do_GET(self):
        content = b'{"priority":"medium"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, *args):
        return


class HostCommandLoopTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.root / "workspace"), "AUTOSKILL_HEADED": "0",
                                                   "AUTOSKILL_BRIDGE": "off", "AUTOSKILL_QUEUE_IDLE_SECONDS": "0.15",
                                                   "AUTOSKILL_CASE_SKILL": str(self.root / "sheet")})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ActualPriority)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.pid = projects.create("真实缺陷闭环", f"http://127.0.0.1:{self.server.server_port}", project_id="host-loop")["id"]
        self.client = TestClient(create_app(start_worker=False), base_url="http://127.0.0.1:8787")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.addCleanup(self.wait_dispatcher)

    def wait_dispatcher(self):
        deadline = time.monotonic() + 10
        while (paths.local() / "queue-worker.json").exists() and time.monotonic() < deadline:
            time.sleep(0.1)

    def invoke(self, *argv, payload=None):
        argv = ["--session=host-simulation", *argv]
        args = cli.parser().parse_args(argv)
        with patch("sys.stdin", io.StringIO(json.dumps(payload, ensure_ascii=False) if payload is not None else "")):
            forwarded, stdin, attachments = command_input.for_bridge(args, argv)
        response = self.client.post("/api/bridge", headers={"X-Autoskill-Bridge": "1"}, json={
            "argv": forwarded, "stdin": stdin, "attachments": attachments, "caller_cwd": str(self.root), "session_id": "host-simulation"})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["exit"], 0, result)
        return json.loads(result["stdout"])

    def next(self, workflow_id):
        return self.invoke("workflow", "next", workflow_id, "--idempotency-key", uuid.uuid4().hex)["task"]

    def submit(self, workflow_id, task, payload):
        return self.invoke("workflow", "submit", workflow_id, task["id"], "--attempt-id", task["attempt_id"],
                           "--lease-token", task["lease_token"], "--idempotency-key", uuid.uuid4().hex, "--file", "-", payload=payload)

    def test_prompt_to_actual_fail_to_complete_keeps_original_expectation(self):
        source = self.root / "需求.md"
        source.write_text("用户需求：优先级必须为 high；不得按当前返回值降低期望。", encoding="utf-8")
        spec = self.root / "workflow.json"
        spec.write_text(json.dumps({"title": "优先级完整验证", "sources": [{"id": "s1", "file": source.name}]}), encoding="utf-8")
        created = self.invoke("workflow", "start", self.pid, "--file=" + str(spec), "--idempotency-key", "same-create")
        duplicate = self.invoke("workflow", "start", self.pid, "--file", str(spec), "--idempotency-key", "same-create")
        workflow_id = created["workflow_id"]
        self.assertEqual(duplicate["workflow_id"], workflow_id)
        sources = self.invoke("workflow", "read", workflow_id, "--kind", "sources")
        self.assertIn("high", sources["items"][0]["text"])
        task = self.next(workflow_id)
        self.assertEqual(task["phase"], "requirements")
        self.submit(workflow_id, task, {"requirements": [{"id": "r1", "text": "优先级必须为 high", "source_ids": ["s1"]}]})
        task = self.next(workflow_id)
        self.assertEqual(task["phase"], "design")
        self.submit(workflow_id, task, {"scenarios": [{"id": "s1", "title": "核对优先级", "requirement_ids": ["r1"], "oracle_ids": ["o1"]}],
                                        "oracles": [{"id": "o1", "kind": "api", "requirement_ids": ["r1"],
                                                     "checks": {"status": 200, "json_equals": {"/priority": "high"}}}]})
        task = self.next(workflow_id)
        self.assertEqual(task["phase"], "materialize")
        case = builder.api_case("TC-API-001", "核对优先级", "GET", "/priority", {"status": 200, "json_equals": {"/priority": "high"}})
        self.submit(workflow_id, task, {"cases": [case], "bindings": {"s1": [case["id"]]}})
        deadline, task = time.monotonic() + 20, None
        while task is None and time.monotonic() < deadline:
            task = self.next(workflow_id)
            if task is None:
                time.sleep(0.1)
        self.assertIsNotNone(task)
        self.assertEqual(task["phase"], "diagnose", task)
        self.assertEqual(task["context"]["execution"]["conclusion"], "violated")
        run_id = task["context"]["execution"].get("run_id")
        self.submit(workflow_id, task, {"disposition": "business_defect", "reason": "独立预期 high，真实接口返回 medium", "evidence_refs": [run_id] if run_id else []})
        task = self.next(workflow_id)
        self.assertEqual(task["phase"], "knowledge")
        self.submit(workflow_id, task, {"entries": [{"id": "defect-priority", "text": "优先级不符合 high 的需求", "evidence_refs": [run_id] if run_id else []}]})
        final = self.invoke("workflow", "status", workflow_id)
        self.assertTrue(final["complete"])
        self.assertEqual(final["conclusion"], "violated")
        saved = self.invoke("cases", self.pid, "--case", case["id"])
        self.assertEqual(saved["case"]["assertions"]["json_equals"]["/priority"], "high")


if __name__ == "__main__":
    unittest.main()
