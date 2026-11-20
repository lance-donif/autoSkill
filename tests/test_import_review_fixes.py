"""Import failures preserve both the local project and its SQLite index."""
import errno
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

import test_task_reports as delivery_fixtures
from fastapi.testclient import TestClient

from autoskill import builder, cli, db, execution_inputs, paths, projects, runner, secrets, share, task_reports
from autoskill.server.app import create_app

PID = "review-import"
PASSPHRASE = "fixture-import-passphrase"


class ImportRollbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.root / "workspace")})
        environment.start()
        self.addCleanup(environment.stop)
        projects.create("Incoming project", "https://example.invalid", project_id=PID)
        self.case = builder.api_case("TC-API-001", "Incoming case", "GET", "/ok", {"status": 204},
                                     headers={"X-Api-Key": "${ENV:IMPORT_REVIEW_TOKEN}"})
        projects.save_cases(PID, [self.case])
        secrets.set_secret("IMPORT_REVIEW_TOKEN", "synthetic-pack-value")
        self.pack = share.pack([PID], str(self.root / "incoming.zip"), PASSPHRASE)["file"]
        projects.update(PID, name="Local project")
        projects.save_cases(PID, [{**self.case, "title": "Local case"}])
        self.folder = projects.path(PID)
        (self.folder / "knowledge/local-only.md").write_text("Keep the local edits.", encoding="utf-8")
        secrets.set_secret("IMPORT_REVIEW_TOKEN", "synthetic-local-value")
        self.before_files = self.files()
        self.before_index = self.index()

    def files(self):
        return {file.relative_to(self.folder).as_posix(): file.read_bytes()
                for file in self.folder.rglob("*") if file.is_file()}

    def index(self):
        return {table: db.rows(f"SELECT * FROM {table} WHERE " + ("id" if table == "projects" else "project_id") + " = ? ORDER BY 1, 2", (PID,))
                for table in ("projects", "cases")}

    def modified_pack(self, changes):
        target = self.root / "changed.zip"
        with zipfile.ZipFile(self.pack) as source, zipfile.ZipFile(target, "w") as archive:
            for member in source.infolist():
                data = changes.get(member.filename, source.read(member))
                if data is not None:
                    archive.writestr(member, data)
        return str(target)

    def assert_original_preserved(self):
        self.assertEqual(self.files(), self.before_files)
        self.assertEqual(self.index(), self.before_index)
        self.assertEqual(projects.get(PID)["name"], "Local project")

    def fail_index_on_incoming_case(self):
        with db.connect() as connection:
            connection.execute(
                "CREATE TRIGGER reject_incoming_case BEFORE INSERT ON cases "
                "WHEN NEW.project_id = 'review-import' AND NEW.title = 'Incoming case' "
                "BEGIN SELECT RAISE(ABORT, 'fixture index failure'); END")

    def test_missing_config_and_invalid_config_or_auth_preserve_local_data(self):
        for name, content in (("qa-config.json", None), ("qa-config.json", "[]"), ("auth.json", "[]")):
            with self.subTest(name=name, content=content):
                package = self.modified_pack({f"projects/{PID}/{name}": content})
                result = share.import_pack(package, overwrite=True)
                self.assertEqual(result["imported"], [])
                self.assertIn(name, result["skipped"][0]["reason"])
                self.assert_original_preserved()

    def test_index_failure_rolls_back_directory_and_all_index_rows(self):
        self.fail_index_on_incoming_case()
        result = share.import_pack(self.pack, overwrite=True)
        self.assertEqual(result["imported"], [])
        self.assertIn("fixture index failure", result["skipped"][0]["reason"])
        self.assert_original_preserved()

    def test_failed_new_project_leaves_no_directory_or_index(self):
        projects.delete(PID)
        self.fail_index_on_incoming_case()
        result = share.import_pack(self.pack)
        self.assertEqual(result["imported"], [])
        self.assertFalse(self.folder.exists())
        self.assertEqual(self.index(), {"projects": [], "cases": []})

    def test_install_rename_failure_restores_original_directory(self):
        replace = Path.replace

        def fail_staging_move(source, target):
            if source.name == "incoming":
                raise OSError("fixture install failure")
            return replace(source, target)

        with patch.object(Path, "replace", fail_staging_move):
            result = share.import_pack(self.pack, overwrite=True)
        self.assertEqual(result["imported"], [])
        self.assertIn("fixture install failure", result["skipped"][0]["reason"])
        self.assert_original_preserved()

    def test_cli_and_dashboard_distinguish_normal_skips_from_failed_and_partial_imports(self):
        def command(file, *options):
            output = io.StringIO()
            with patch.dict(os.environ, {"AUTOSKILL_BRIDGE": "off"}), redirect_stdout(output):
                status = cli.main(["import", str(file), *options])
            return status, json.loads(output.getvalue())

        code, skipped = command(self.pack)
        self.assertEqual((code, skipped["errors"], [item["id"] for item in skipped["skipped"]]), (0, [], [PID]))
        with patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.root / "sender")}):
            share.import_pack(self.pack)
            projects.create("Another project", "https://example.invalid/other", project_id="other-project")
            package = share.pack([PID, "other-project"], str(self.root / "multiple.zip"))["file"]
        replace = Path.replace

        def denied(source, target):
            if source.name == "incoming" and target.name == PID:
                raise PermissionError(errno.EACCES, "fixture denied the installation rename")
            return replace(source, target)

        with patch.object(Path, "replace", denied):
            code, failed = command(self.pack, "--overwrite")
            self.assertEqual((code, failed["imported"], failed["errors"]), (2, [], failed["skipped"]))
            self.assertEqual([item["id"] for item in failed["errors"]], [PID])
            self.assertIn("重试", failed["next"])
            self.assert_original_preserved()
            code, partial = command(package, "--overwrite")
            self.assertEqual((code, [p["id"] for p in partial["imported"]], [p["id"] for p in partial["errors"]]), (2, ["other-project"], [PID]))
            with TestClient(create_app(start_worker=False), base_url="http://127.0.0.1") as client:
                response = client.post("/api/shares/import", json={"source": package, "overwrite": True})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["errors"], partial["errors"])
            self.assert_original_preserved()
        code, repaired = command(package, "--overwrite")
        self.assertEqual((code, repaired["skipped"], repaired["errors"]), (0, [], []))
        self.assertEqual({item["id"] for item in repaired["imported"]}, {PID, "other-project"})

    def test_rollback_failure_retains_recoverable_backup(self):
        self.fail_index_on_incoming_case()
        replace = Path.replace

        def fail_backup_restore(source, target):
            if source.name == "previous":
                raise OSError("fixture rollback failure")
            return replace(source, target)

        with patch.object(Path, "replace", fail_backup_restore):
            result = share.import_pack(self.pack, overwrite=True)
        self.assertEqual(result["imported"], [])
        self.assertIn("数据保留在", result["skipped"][0]["reason"])
        backups = list((paths.local() / "import").glob("*/previous"))
        self.assertEqual(len(backups), 1)
        self.assertEqual({file.relative_to(backups[0]).as_posix(): file.read_bytes()
                          for file in backups[0].rglob("*") if file.is_file()}, self.before_files)
        self.assertEqual(self.index(), self.before_index)

    def test_wrong_passphrase_fails_before_replacing_any_project(self):
        with self.assertRaisesRegex(ValueError, "口令"):
            share.import_pack(self.pack, passphrase="wrong-fixture-passphrase", overwrite=True)
        self.assert_original_preserved()

    def test_invalid_manifest_fails_before_replacing_any_project(self):
        package = self.modified_pack({share.MANIFEST: json.dumps([])})
        with self.assertRaisesRegex(ValueError, "清单"):
            share.import_pack(package, overwrite=True)
        self.assert_original_preserved()

    def test_successful_overwrite_updates_files_and_index(self):
        result = share.import_pack(self.pack, passphrase=PASSPHRASE, overwrite=True)
        self.assertEqual([item["id"] for item in result["imported"]], [PID])
        self.assertEqual(result["skipped"], [])
        self.assertEqual(result["missing_secrets"], [])
        self.assertEqual(projects.get(PID)["name"], "Incoming project")
        self.assertEqual(projects.case(PID, self.case["id"])["title"], "Incoming case")
        self.assertEqual(self.index()["projects"][0]["name"], "Incoming project")
        self.assertEqual(self.index()["cases"][0]["title"], "Incoming case")
        self.assertFalse((self.folder / "knowledge/local-only.md").exists())
        self.assertEqual(list((paths.local() / "import").iterdir()), [])

    def test_skipped_project_still_restores_requested_shared_credentials(self):
        result = share.import_pack(self.pack, passphrase=PASSPHRASE)
        self.assertEqual(result["imported"], [])
        self.assertEqual(result["secrets_restored"], ["IMPORT_REVIEW_TOKEN"])
        self.assertEqual(secrets.get_secret("IMPORT_REVIEW_TOKEN"), "synthetic-pack-value")
        self.assert_original_preserved()


class LocalReportImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        delivery_fixtures.TaskReportTests.setUpClass.__func__(cls)

    def setUp(self):
        delivery_fixtures.TaskReportTests.setUp(self)

    def test_replacing_project_assets_preserves_only_its_own_indexed_run_report(self):
        def files(folder):
            return {file.relative_to(folder): file.read_bytes() for file in folder.rglob("*") if file.is_file()}

        original = runner.execute(runner.create(self.pid, ["TC-API-002"])["id"])
        report = original["test_report"]
        identity = report["report_id"]
        report_dir = task_reports._folder(identity)
        before = files(report_dir)
        run_dir = paths.local() / "runs" / original["id"]
        before_run = files(run_dir)
        peer = projects.create("Other local project", "https://example.invalid/peer", project_id="other-owner")["id"]
        attachment = projects.path(peer) / "reports" / identity / "note.md"
        attachment.parent.mkdir(parents=True)
        attachment.write_text("local attachment", encoding="utf-8")
        with patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.folder / "sender")}):
            projects.create("Incoming name", "https://example.invalid/incoming", project_id=self.pid)
            projects.create("Other incoming project", "https://example.invalid/subsite", project_id=peer)
            candidate = builder.api_case("INCOMING-API", "Incoming case", "GET", "/order", {"status": 200})
            projects.save_cases(self.pid, [candidate])
            remote = task_reports.for_recording(self.pid, {"started_at": db.now(), "session_id": "sender"}, [candidate])
            remote_originals = files(task_reports._folder(remote["report_id"]))
            for project in (self.pid, peer):
                folder = projects.path(project) / "reports" / identity
                folder.mkdir(parents=True)
                (folder / "note.md").write_text("incoming attachment", encoding="utf-8")
            other = projects.path(self.pid) / "reports" / "additional" / "note.md"
            other.parent.mkdir()
            other.write_text("new report attachment", encoding="utf-8")
            package = share.pack([self.pid, peer], str(self.folder / "normal-transfer.zip"))["file"]
        for _ in range(2):
            imported = share.import_pack(package, overwrite=True)
            self.assertEqual(imported["errors"], [])
            self.assertEqual({p["id"] for p in imported["imported"]}, {self.pid, peer})
            self.assertEqual(projects.get(self.pid)["name"], "Incoming name")
            self.assertEqual([case["id"] for case in projects.cases(self.pid)], ["INCOMING-API"])
            self.assertEqual((projects.path(self.pid) / "reports/additional/note.md").read_text(), "new report attachment")
            self.assertEqual(files(projects.path(self.pid) / "reports" / remote["report_id"]), remote_originals)
            with self.assertRaisesRegex(ValueError, "测试报告不存在"):
                task_reports.get(remote["report_id"])
            self.assertEqual(attachment.read_text(), "incoming attachment")
            self.assertEqual(files(report_dir), before)
            self.assertEqual(files(run_dir), before_run)
            self.assertEqual(task_reports.get(identity), report)
        retry = task_reports.retry(identity)
        for key in ("source_hash", "sha256", "markdown_sha256", "bugs_sha256"):
            self.assertEqual(retry[key], report[key])
        run = db.one("SELECT * FROM runs WHERE id=?", (original["id"],))
        result = db.one("SELECT * FROM results WHERE run_id=?", (original["id"],))
        result["detail"] = json.loads(result["detail"])
        self.assertEqual((run["status"], result["status"]), ("Pass", "Pass"))
        self.assertTrue(execution_inputs.result_evidence(run, result))
        self.assertEqual(db.one("SELECT COUNT(*) n FROM runs")["n"], 1)


if __name__ == "__main__":
    unittest.main()
