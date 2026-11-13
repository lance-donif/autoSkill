"""Dashboard-native tools preserve pinned assets, bound evidence and asynchronous diagnostics."""
import json
import threading
import time
from unittest.mock import patch

from fastapi.testclient import TestClient
from test_native_engines import Workspace

from autoskill import domain, native_assets, native_run
from autoskill.server.app import create_app


class NativeDashboardTests(Workspace):
    def client(self):
        return TestClient(create_app(start_worker=False), base_url="http://127.0.0.1:8787")

    def test_downloads_only_bound_evidence_and_detects_changed_files(self):
        result = self.execute(self.fixture("import unittest\nclass Original(unittest.TestCase):\n def test_ok(self): self.assertEqual(2 + 2, 4)\n"))
        self.assertEqual(result["status"], "Pass", result)
        client = self.client()
        base = f"/api/native-runs/{result['id']}/files/"
        folder = native_run._evidence_folder(result)
        response = client.get(base + "unittest.json")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.content, (folder / "unittest.json").read_bytes())
        self.assertEqual(client.get(base + "outcome.json").status_code, 200)
        self.assertEqual(client.get(base + "input/test_fixture.py").status_code, 404)
        self.assertEqual(client.get(base + "process-spec.json").status_code, 404)
        self.assertEqual(client.get(base + "%2e%2e%2foutcome.json").status_code, 404)
        (folder / "unbound.txt").write_text("Not an execution artifact")
        self.assertEqual(client.get(base + "unbound.txt").status_code, 404)
        (folder / "unittest.json").write_text("changed report")
        self.assertEqual(client.get(base + "unittest.json").status_code, 409)

    def test_native_rerun_keeps_original_asset_version_and_parameters(self):
        source = self.fixture("import os,unittest\nclass Original(unittest.TestCase):\n def test_pin(self): self.assertEqual(os.environ['FROZEN'], 'old')\n")
        asset = native_assets.import_asset(self.pid, str(source), "selenium-unittest")
        parameters = {"env": {"FROZEN": "old"}}
        run = native_run.enqueue(self.pid, asset["id"], parameters=parameters)
        native_run.execute(run["id"])
        (source / "test_fixture.py").write_text("import unittest\nclass Changed(unittest.TestCase):\n def test_new(self): self.fail('changed revision')\n")
        changed = native_assets.import_asset(self.pid, str(source), "selenium-unittest", asset_id=asset["id"])
        self.assertNotEqual(changed["asset_version"], asset["asset_version"])
        with patch("autoskill.server.native_tools.jobs.ensure_worker", return_value={"status": "test-manual"}):
            response = self.client().post(f"/api/native-runs/{run['id']}/rerun", json={})
        self.assertEqual(response.status_code, 200, response.text)
        repeated = native_run.get(response.json()["id"])
        manifest = native_run._frozen(repeated)
        self.assertEqual((manifest["head"], manifest["asset_version"], manifest["parameters"]), (asset["head"], asset["asset_version"], parameters))
        self.assertEqual(native_run.execute(repeated["id"])["status"], "Pass")

    def test_diagnostics_does_not_block_request_and_retains_error(self):
        ready, release = threading.Event(), threading.Event()

        def delayed_doctor(launch):
            self.assertTrue(launch)
            ready.set()
            if not release.wait(timeout=5):
                raise RuntimeError("Test did not release the diagnostic")
            raise ValueError("真实浏览器不可用的测试原因")

        client = self.client()
        with patch("autoskill.server.native_tools.cli.doctor", side_effect=delayed_doctor):
            response = client.post("/api/diagnostics", json={"kind": "doctor", "launch": True})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["status"], "Running")
            self.assertTrue(ready.wait(timeout=1))
            release.set()
            result = self.await_diagnostic(client, response.json()["id"])
        self.assertEqual(result["status"], "Failed")
        self.assertIn("真实浏览器不可用", result["error"])
        self.assertIsNone(result["result"])

    def test_real_engine_probe_returns_actual_availability(self):
        client = self.client()
        response = client.post("/api/diagnostics", json={"kind": "engines", "engine": "ai-eval"})
        self.assertEqual(response.status_code, 200, response.text)
        result = self.await_diagnostic(client, response.json()["id"])
        self.assertEqual(result["status"], "Completed", result)
        self.assertEqual(result["result"]["engines"][0]["id"], "ai-eval")
        self.assertTrue(result["result"]["engines"][0]["available"])

    def await_diagnostic(self, client, identity):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = client.get("/api/diagnostics/" + identity).json()
            if result["status"] != "Running":
                return result
            time.sleep(0.01)
        raise AssertionError("Diagnostic did not complete")

    def test_uploaded_sources_are_data_and_preserve_exact_original_bytes(self):
        client = self.client()
        marker = self.root / "never-created"
        content = f"curl 'https://fixture.invalid/chat' --json '{{\"question\":\"中文 $(touch {marker})\"}}'\n"
        response = client.post(f"/api/conversations/{self.pid}/import-content", json={"format": "curl", "name": "原文", "content": content})
        self.assertEqual(response.status_code, 200, response.text)
        capture = client.get(f"/api/conversations/{self.pid}/{response.json()['id']}").json()
        self.assertEqual(domain.read_file(self.pid, capture["source"]["path"]), content.encode())
        self.assertFalse(marker.exists())
        har = {"log": {"entries": [{"_resourceType": "fetch", "request": {"url": "https://fixture.invalid/chat", "method": "POST", "headers": [],
                                  "postData": {"mimeType": "application/json", "text": '{"question":"next"}'}},
                                  "response": {"status": 200, "headers": [], "content": {"mimeType": "application/json", "text": '{"answer":"yes"}'}}}]}}
        original = json.dumps(har, ensure_ascii=False, indent=3) + "\n"
        response = client.post(f"/api/conversations/{self.pid}/import-content", json={"format": "har", "content": original})
        self.assertEqual(response.status_code, 200, response.text)
        capture = client.get(f"/api/conversations/{self.pid}/{response.json()['id']}").json()
        self.assertEqual(domain.read_file(self.pid, capture["source"]["path"]), original.encode())
        listed = client.get(f"/api/conversations/{self.pid}?limit=1").json()
        self.assertEqual((listed["total"], len(listed["items"]), listed["has_more"]), (2, 1, True))
        self.assertNotIn("requests", listed["items"][0].get("origin", {}))
        self.assertIsInstance(listed["items"][0]["requests"], int)
