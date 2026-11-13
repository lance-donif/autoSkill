"""Large native runs stay bounded across CLI and HTTP while explicit reads retain evidence."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "skills/autoskill/app"
sys.path.insert(0, str(APP))

from autoskill import db, native_assets, native_reader, native_run, paging, paths, projects
from autoskill.engines.process import write_json
from autoskill.server.app import create_app


class NativeReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.root / "workspace"), "AUTOSKILL_BRIDGE": "off",
                                               "AUTOSKILL_RUNTIME_CONTEXT": "", "AUTOSKILL_SESSION": ""})
        environment.start()
        self.addCleanup(environment.stop)
        self.pid = projects.create("原生结果", "http://127.0.0.1:9", project_id="native-reader")["id"]
        source = self.root / "test_native.py"
        source.write_text("import unittest\nclass Original(unittest.TestCase):\n def test_saved(self): self.assertTrue(True)\n", encoding="utf-8")
        self.asset = native_assets.import_asset(self.pid, str(source), "selenium-unittest")

    def command(self, *argv):
        result = subprocess.run([sys.executable, "-m", "autoskill", *argv], env={**os.environ, "PYTHONPATH": str(APP)}, cwd=self.root,
                                capture_output=True, text=True, encoding="utf-8", check=False, timeout=30)
        return result.returncode, json.loads(result.stdout)

    def large_run(self):
        run = native_run.enqueue(self.pid, self.asset["id"])
        cases = [{"id": f"sample-{number}", "title": f"多轮问数 {number}", "status": "Fail" if number == 1 else "Pass",
                  "business_status": "Fail" if number == 1 else "Pass", "assertions": [{"expected": 100, "actual": 90 if number == 1 else 100,
                                                                                         "passed": number != 1}],
                  "turns": [{"question": "第一问", "answer": "正文" * 3000}, {"question": "延续上轮", "answer": "第二轮"}]}
                 for number in range(67)]
        outcome = {"status": "Fail", "business_status": "Fail", "metrics": {"planned_cases": 67}, "cases": cases,
                   "events": [{"case_id": case["id"], "event": "completed"} for case in cases],
                   "evidence": [{"path": f"report-{number}.json", "sha256": str(number), "size": 123} for number in range(500)]}
        folder = paths.local() / "runs" / run["id"] / "native"
        folder.mkdir(parents=True)
        write_json(folder / "outcome.json", outcome)
        native_run._record(run, outcome)
        db.update("runs", run["id"], {"status": "Fail"})
        return run["id"]

    def test_default_projection_is_bounded_and_explicit_case_and_all_are_complete(self):
        run_id = self.large_run()
        first = native_reader.status(run_id, limit=20)
        self.assertEqual((len(first["results"]), first["page"]["total"]), (20, 67))
        self.assertEqual(first["outcome"]["business_status"], "Fail")
        self.assertNotIn("cases", first["outcome"])
        self.assertNotIn("events", first["outcome"])
        self.assertEqual(first["outcome"]["evidence_ref"]["count"], 500)
        self.assertLess(len(json.dumps(first)), 20000)
        report = Path(first["report"]["path"])
        self.assertEqual(first["report"]["sha256"], hashlib.sha256(report.read_bytes()).hexdigest())
        second = native_reader.status(run_id, limit=50, cursor=first["page"]["next_cursor"])
        self.assertEqual(len({row["id"] for row in first["results"] + second["results"]}), 67)
        detail = native_reader.status(run_id, case_id="sample-1")
        self.assertEqual(len(detail["case"]["turns"]), 2)
        self.assertEqual(detail["case"]["assertions"][0]["expected"], 100)
        self.assertNotIn("evidence", detail["case"])
        full = native_reader.status(run_id, all_items=True)
        self.assertEqual(len(full["results"]), 67)
        self.assertEqual(len(full["outcome"]["cases"]), 67)
        self.assertEqual(len(native_run.get(run_id)["results"]), 67)
        db.update("runs", run_id, {"reason": "执行状态已变化"})
        with self.assertRaises(paging.PageError) as error:
            native_reader.status(run_id, cursor=first["page"]["next_cursor"])
        self.assertEqual(error.exception.code, "cursor_expired")

    def test_cli_and_http_share_pages_and_single_case_reads(self):
        run_id = self.large_run()
        code, result = self.command("native", "status", run_id, "--limit", "3")
        self.assertEqual((code, len(result["results"]), result["page"]["total"]), (0, 3, 67))
        with TestClient(create_app(start_worker=False), base_url="http://127.0.0.1:8787") as client:
            response = client.get(f"/api/native-runs/{run_id}?limit=3")
            self.assertEqual(response.json(), result)
            detail = client.get(f"/api/native-runs/{run_id}", params={"case": "sample-1"}).json()
            self.assertEqual(detail["case"]["business_status"], "Fail")
        self.assertEqual(len(self.command("native", "status", run_id, "--all")[1]["outcome"]["cases"]), 67)

    def test_real_native_wait_returns_summary_and_case_evidence_remains_readable(self):
        code, result = self.command("native", "run", self.pid, self.asset["id"], "--wait")
        self.assertEqual((code, result["status"], result["page"]["total"]), (0, "Pass", 1))
        self.assertNotIn("cases", result["outcome"])
        detail = self.command("native", "status", result["id"], "--case", result["results"][0]["case_id"])[1]
        self.assertEqual(detail["case"]["status"], "Pass")


if __name__ == "__main__":
    unittest.main()
