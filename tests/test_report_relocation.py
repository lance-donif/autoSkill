"""Moving complete data preserves immutable evidence while public report paths follow the selected copy."""
import hashlib
import json
import os
import shutil
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

import test_task_reports as fixtures

from autoskill import data_refs, db, execution_inputs, native_run, paths, runner, task_report_data, task_reports
from autoskill.server.app import create_app


class ReportRelocationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.TaskReportTests.setUpClass.__func__(cls)

    def setUp(self):
        fixtures.TaskReportTests.setUp(self)

    def _run(self):
        return runner.execute(runner.create(self.pid, [self.case["id"]])["id"])

    @staticmethod
    def _files(report):
        return {name: Path(report[name]).read_bytes() for name in ("excel", "markdown", "bugs")}

    def _assert_reports(self, report, content, destination):
        observed = task_reports.get(report["report_id"])
        self.assertEqual(observed["status"], "ready", observed)
        for artifact, raw in content.items():
            file = Path(observed[artifact])
            self.assertTrue(file.is_relative_to(destination))
            self.assertEqual(file.read_bytes(), raw)
            self.assertEqual(task_reports.download(report["report_id"], artifact), file)
        return observed

    def test_relative_reports_survive_moving_data_with_original_tree_still_present(self):
        run = self._run()
        report = run["test_report"]
        content = self._files(report)
        original = paths.workspace()
        snapshot = (task_reports._folder(report["report_id"]) / "snapshot.json").read_bytes()
        receipt = json.loads((task_reports._folder(report["report_id"]) / "receipt.json").read_bytes())
        self.assertTrue(all(not data_refs.is_absolute(receipt[key]) for key in content))
        destination = self.folder / "moved data 资料"
        shutil.copytree(original, destination)
        with patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(destination)}):
            observed = self._assert_reports(report, content, destination)
            raw_run = db.one("SELECT * FROM runs WHERE id=?", (run["id"],))
            result = db.one("SELECT * FROM results WHERE run_id=?", (run["id"],))
            result["detail"] = json.loads(result["detail"])
            self.assertTrue(execution_inputs.result_evidence(raw_run, result))
            self.assertEqual((task_reports._folder(report["report_id"]) / "snapshot.json").read_bytes(), snapshot)
            with TestClient(create_app(start_worker=False), base_url="http://127.0.0.1") as client:
                view = client.get("/api/runs/" + run["id"]).json()
                self.assertTrue(Path(view["input_manifest"]).is_relative_to(destination))
                self.assertTrue(Path(view["results"][0]["detail"]["attempts"][0]["path"]).is_relative_to(destination))
                for artifact, url in (("excel", "download_url"), ("markdown", "markdown_download_url"), ("bugs", "bugs_download_url")):
                    response = client.get(observed[url])
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.content, content[artifact])
        self.assertEqual(self._files(report), content)

    def test_legacy_absolute_receipt_and_snapshot_are_not_rewritten_or_rehashed(self):
        run = self._run()
        report = run["test_report"]
        original = paths.workspace()
        folder = task_reports._folder(report["report_id"])
        receipt = json.loads((folder / "receipt.json").read_bytes())
        for key in ("excel", "markdown", "bugs"):
            receipt[key] = report[key]
        snapshot = json.loads((folder / "snapshot.json").read_bytes())
        snapshot["metadata"]["input_manifest"] = run["input_manifest"]
        for item in snapshot["cases"]:
            item["evidence_directory"] = str(original / ".local" / "runs" / run["id"])
        receipt["source_hash"] = execution_inputs.content_hash(snapshot)
        (folder / "snapshot.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=4), encoding="utf-8")
        (folder / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=4), encoding="utf-8")
        originals = {file.name: file.read_bytes() for file in folder.iterdir() if file.is_file()}
        content = self._files(report)
        destination = self.folder / "legacy copy"
        shutil.copytree(original, destination)
        (destination / "data-root.json").write_text(json.dumps({"schema_version": 1, "data_id": "relocation-test",
                                                               "legacy_roots": [str(original)], "locations": [str(destination)]}), encoding="utf-8")
        with patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(destination)}):
            self._assert_reports(report, content, destination)
            copied = task_reports._folder(report["report_id"])
            for name, raw in originals.items():
                self.assertEqual((copied / name).read_bytes(), raw)
            with patch.object(task_report_data, "run_snapshot", side_effect=AssertionError("Do not recapture historical facts")):
                repaired = task_reports.retry(report["report_id"])
            self.assertEqual(repaired["status"], "ready", repaired)
            self.assertEqual(repaired["source_hash"], receipt["source_hash"])
            for name, raw in originals.items():
                if name != "receipt.json":
                    self.assertEqual((copied / name).read_bytes(), raw)
        shutil.rmtree(original)
        with patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(destination)}):
            self._assert_reports(report, content, destination)

    def test_native_multiturn_evidence_follows_data_copy_without_touching_original_outcome(self):
        asset = fixtures.TaskReportTests.native_asset(self)
        run = native_run.execute(native_run.enqueue(self.pid, asset["id"])["id"])
        self.assertEqual(run["status"], "Fail")
        report = run["test_report"]
        content = self._files(report)
        root = paths.workspace()
        outcome = paths.local() / "runs" / run["id"] / "native" / "outcome.json"
        digest = hashlib.sha256(outcome.read_bytes()).hexdigest()
        destination = self.folder / "native copy"
        shutil.copytree(root, destination)
        with patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(destination)}):
            self._assert_reports(report, content, destination)
            snapshot = task_report_data.run_snapshot(db.one("SELECT * FROM runs WHERE id=?", (run["id"],)))
            self.assertTrue(snapshot["cases"][0]["evidence_valid"])
            moved = paths.local() / "runs" / run["id"] / "native" / "outcome.json"
            self.assertEqual(hashlib.sha256(moved.read_bytes()).hexdigest(), digest)


if __name__ == "__main__":
    unittest.main()
