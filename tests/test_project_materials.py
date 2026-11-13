"""Project materials preserve original bytes, report receipts and portable root-relative references."""
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill import builder, data_refs, data_store, db, domain, project_materials, projects, report_locations, task_report_data, task_reports
from autoskill.server.app import create_app


class ProjectMaterialTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "data"
        environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.workspace), "AUTOSKILL_BRIDGE": "off",
                                             "AUTOSKILL_HEADED": "0", "AUTOSKILL_CASE_SKILL": str(self.root / "plan"),
                                             "AUTOSKILL_SESSION": "", "AUTOSKILL_WORKFLOW_BINDING": ""})
        environment.start()
        self.addCleanup(environment.stop)
        db.init()
        self.pid = projects.create("资料", "http://127.0.0.1:9761", project_id="materials")["id"]
        self.case = builder.api_case("API-001", "查询", "GET", "/items", {"status": 200, "json_equals": {"/ok": True}})
        projects.save_cases(self.pid, [self.case])

    def recording_report(self, identity="one"):
        recording = {"project_id": self.pid, "started_at": db.now(), "session_id": identity, "rand": identity, "start_url": "/"}
        report = task_reports.for_recording(self.pid, recording, [projects.case(self.pid, self.case["id"])])
        self.assertEqual(report["status"], "ready", report)
        return report

    def test_authoritative_knowledge_cas_and_path_validation(self):
        item = project_materials.write_knowledge(self.pid, "订单/规则.md", "必须退款")
        file = projects.path(self.pid) / "knowledge/订单/规则.md"
        self.assertEqual(file.read_text(), "必须退款")
        with self.assertRaisesRegex(ValueError, "变化"):
            project_materials.write_knowledge(self.pid, "订单/规则.md", "错误覆盖")
        with self.assertRaisesRegex(ValueError, "路径"):
            project_materials.write_knowledge(self.pid, "../qa-config.json", "escape")
        project_materials.write_knowledge(self.pid, "订单/规则.md", "必须原路退款", item["sha256"])
        self.assertEqual(file.read_text(), "必须原路退款")
        with self.assertRaisesRegex(ValueError, "变化"):
            project_materials.write_knowledge(self.pid, "订单/规则.md", "", item["sha256"], delete=True)

    def test_new_reports_are_under_project_and_receipt_hashes_are_retained(self):
        report = self.recording_report()
        folder = projects.path(self.pid) / "reports" / report["report_id"]
        self.assertEqual(Path(report["excel"]).parent, folder)
        snapshot = (folder / "snapshot.json").read_bytes()
        receipt = (folder / "receipt.json").read_bytes()
        read = task_reports.get(report["report_id"])
        self.assertEqual(read["sha256"], hashlib.sha256(Path(report["excel"]).read_bytes()).hexdigest())
        self.assertEqual((folder / "receipt.json").read_bytes(), receipt)
        self.assertEqual((folder / "snapshot.json").read_bytes(), snapshot)

    def test_legacy_reports_stay_in_place_without_rewriting_frozen_bytes(self):
        recording = {"project_id": self.pid, "started_at": db.now(), "rand": "legacy", "session_id": "legacy", "start_url": "/"}
        cases = [projects.case(self.pid, self.case["id"])]
        identity = task_report_data.recording_identity(self.pid, recording, cases)
        report_id = "recording_" + identity
        legacy = report_locations.folder(report_id)
        legacy.mkdir(parents=True)
        report = task_reports.for_recording(self.pid, recording, cases)
        self.assertEqual(report["status"], "ready", report)
        receipt = (legacy / "receipt.json").read_bytes()
        self.assertEqual(Path(task_reports.get(report_id)["excel"]).parent, legacy)
        self.assertEqual((legacy / "receipt.json").read_bytes(), receipt)
        self.assertFalse((projects.path(self.pid) / "reports" / report_id).exists())
        self.assertTrue(any(item["historical"] for item in project_materials.inventory(self.pid)["items"]))

    def test_materials_zip_keeps_cases_knowledge_reports_and_domain_versions_exactly(self):
        project_materials.write_knowledge(self.pid, "规则.md", "退款规则")
        report = self.recording_report()
        original = next(file for file in (projects.path(self.pid) / "testcases").rglob("*.json")
                        if any(case["id"] == self.case["id"] for case in json.loads(file.read_bytes())))
        case_bytes = original.read_bytes()
        generation = domain.read(self.pid)
        domain.commit(self.pid, {"case-folders.json": {"folders": [{"id": "f", "name": "迭代", "parent_id": None}],
                                                       "assignments": {self.case["id"]: "f"}},
                                 "workflows/review/definition.json": {"sources": [{"text": "退款需求第二版"}]}},
                      generation["head"], reason="保存资料来源")
        result = project_materials.export_project(self.pid)
        with zipfile.ZipFile(result["file"]) as archive:
            manifest = json.loads(archive.read("materials-manifest.json"))
            self.assertFalse(manifest["includes_local_verification"])
            self.assertEqual(archive.read(self.pid + "/" + original.relative_to(projects.path(self.pid)).as_posix()), case_bytes)
            self.assertEqual(archive.read(f"{self.pid}/knowledge/规则.md"), "退款规则".encode())
            report_name = f"{self.pid}/reports/{report['report_id']}/{Path(report['excel']).name}"
            self.assertEqual(archive.read(report_name), Path(report["excel"]).read_bytes())
            self.assertTrue(any("/domain/" in name for name in archive.namelist()))
            for name, expected in manifest["files"].items():
                self.assertEqual(hashlib.sha256(archive.read(name)).hexdigest(), expected["sha256"])
        self.assertEqual(original.read_bytes(), case_bytes)

    def test_full_data_move_preserves_new_report_locations(self):
        report = self.recording_report()
        before = Path(report["excel"]).read_bytes()
        data_store.export_data(str(self.root / "complete.zip"))
        target = self.root / "restored"
        data_store.import_data(str(self.root / "complete.zip"), target=str(target))
        with patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(target)}):
            restored = task_reports.get(report["report_id"])
            self.assertTrue(Path(restored["excel"]).is_relative_to(target))
            self.assertEqual(Path(restored["excel"]).read_bytes(), before)

    def test_http_download_share_upload_and_foreign_file_rejection(self):
        self.recording_report()
        item = project_materials.write_knowledge(self.pid, "规则.md", "业务规则")
        with TestClient(create_app(start_worker=False), base_url="http://127.0.0.1") as client:
            listing = client.get("/api/project-materials/" + self.pid).json()
            self.assertEqual(listing["directories"]["testcases"], str(projects.path(self.pid) / "testcases"))
            self.assertEqual(client.get(item["download_url"]).content, "业务规则".encode())
            self.assertEqual(client.get(f"/api/project-materials/{self.pid}/files/" + "0" * 64).status_code, 400)
            shared = client.post("/api/shares/export", json={"project_ids": [self.pid]}).json()
            package = client.get(shared["download_url"])
            self.assertEqual(package.status_code, 200)
            self.assertTrue(zipfile.is_zipfile(io.BytesIO(package.content)))
            imported = client.post("/api/shares/upload", content=package.content, headers={"Content-Type": "application/zip"}).json()
            self.assertEqual(imported["imported"], [])
            self.assertEqual(imported["skipped"][0]["id"], self.pid)
            self.assertFalse(list((self.workspace / ".local/share-imports").glob("*.zip")))
            self.assertEqual(data_refs.resolve(item["reference"]).read_text(), "业务规则")


if __name__ == "__main__":
    unittest.main()
