"""Import failures preserve both the local project and its SQLite index."""
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill import builder, db, paths, projects, secrets, share

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


if __name__ == "__main__":
    unittest.main()
