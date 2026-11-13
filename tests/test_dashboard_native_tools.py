"""Native dashboard downloads preserve evidence and original asset versions."""
from unittest.mock import patch

from fastapi.testclient import TestClient
from test_native_engines import Workspace

from autoskill import native_assets, native_run
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
