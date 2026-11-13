"""Portable source trees, immutable domain history and archive integrity."""
import hashlib
import json
import os
import sys
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill import builder, db, domain, paths, projects, share

PID = "artifact-test"


class ArtifactShareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.root / "source")})
        environment.start()
        self.addCleanup(environment.stop)
        projects.create("原生资产", "https://artifact.invalid", project_id=PID)
        projects.save_cases(PID, [builder.api_case("TC-API-001", "fixture", "GET", "/fixture", {"status": 204})])
        self.folder = projects.path(PID)

    def create_assets(self):
        source = {"scripts/pages/login.py": b"class LoginPage:\n    pass\n", "input/plan.jmx": b"<jmeterTestPlan/>",
                  "input/fixture.bin": b"\x00\xff\x80fixture", "input/README": b"extensionless source", "scripts/run.cmd": b"@echo fixture\r\n",
                  "input/large.bin": b"large fixture\0" * 410_000}
        for name, data in source.items():
            file = self.folder / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(data)
        (self.folder / "scripts/pages/login.py").chmod(0o755)
        first = domain.commit(PID, {"sources/requirement.json": {"title": "Imported requirement", "origin": "external"},
                                    "native-assets/model/files/model.bin": b"\x00model\xff"}, None)
        second = domain.commit(PID, {"knowledge/history.json": {"status": "historical", "result": "Pass", "origin": "external"}}, first["head"])
        return source, first["head"], second["head"]

    def rewrite(self, package, changes, *, repair_manifest=False, duplicate=None):
        target = self.root / "changed.zip"
        with zipfile.ZipFile(package) as archive:
            entries = {item.filename: (item, archive.read(item)) for item in archive.infolist()}
        for name, data in changes.items():
            if data is None:
                entries.pop(name, None)
            else:
                entries[name] = (entries[name][0] if name in entries else zipfile.ZipInfo(name), data)
        if repair_manifest:
            manifest = json.loads(entries[share.MANIFEST][1])
            manifest["files"] = {name: {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
                                 for name, (_, data) in entries.items() if name.startswith("projects/")}
            entries[share.MANIFEST] = (entries[share.MANIFEST][0], json.dumps(manifest).encode())
        with zipfile.ZipFile(target, "w") as archive:
            for member, data in entries.values():
                archive.writestr(member, data)
            if duplicate:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    archive.writestr(duplicate, b"duplicate")
        return target

    def test_source_binary_and_history_roundtrip_without_local_pass(self):
        source, first, second = self.create_assets()
        package = share.pack([PID], str(self.root / "source.zip"))["file"]
        with zipfile.ZipFile(package) as archive:
            manifest = json.loads(archive.read(share.MANIFEST))
            self.assertEqual(manifest["format"], 2)
            self.assertFalse(manifest["includes_local_verification"])
            self.assertTrue(any("/versions/" in name for name in archive.namelist()))
            for name, data in source.items():
                self.assertEqual(archive.read(f"projects/{PID}/{name}"), data)
        with patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.root / "receiver")}):
            imported = share.import_pack(package)
            self.assertEqual([item["id"] for item in imported["imported"]], [PID])
            for name, data in source.items():
                self.assertEqual((projects.path(PID) / name).read_bytes(), data)
            self.assertEqual(domain.read(PID)["head"], second)
            self.assertEqual(domain.read_file(PID, "native-assets/model/files/model.bin", first), b"\x00model\xff")
            self.assertEqual(domain.read_document(PID, "knowledge/history.json")["status"], "historical")
            self.assertEqual(db.rows("SELECT id FROM runs"), [])
            self.assertEqual(db.rows("SELECT run_id FROM results"), [])
            self.assertEqual((projects.path(PID) / "scripts/pages/login.py").stat().st_mode & 0o777, 0o755)

    def test_tampered_missing_or_unlisted_files_preserve_local_project(self):
        self.create_assets()
        package = share.pack([PID], str(self.root / "source.zip"))["file"]
        projects.update(PID, name="Local version")
        before = {file.relative_to(self.folder).as_posix(): file.read_bytes() for file in self.folder.rglob("*") if file.is_file()}
        name = f"projects/{PID}/input/plan.jmx"
        for changes in ({name: b"<tampered/>"}, {name: None}, {f"projects/{PID}/new.py": b"unlisted"}):
            with self.subTest(changes=list(changes)):
                result = share.import_pack(str(self.rewrite(package, changes)), overwrite=True)
                self.assertEqual(result["imported"], [])
                self.assertEqual(projects.get(PID)["name"], "Local version")
                self.assertEqual({file.relative_to(self.folder).as_posix(): file.read_bytes() for file in self.folder.rglob("*") if file.is_file()}, before)

    def test_repaired_archive_hash_cannot_disguise_corrupt_domain_object(self):
        self.create_assets()
        package = share.pack([PID], str(self.root / "source.zip"))["file"]
        head = domain.read(PID)["head"]
        ref = domain.file_ref(PID, "native-assets/model/files/model.bin")
        name = f"projects/{PID}/domain/objects/{ref['sha256']}"
        altered = self.rewrite(package, {name: b"tampered domain object"}, repair_manifest=True)
        result = share.import_pack(str(altered), overwrite=True)
        self.assertEqual(result["imported"], [])
        self.assertIn("哈希", result["skipped"][0]["reason"])
        self.assertEqual(domain.read(PID)["head"], head)
        self.assertEqual(domain.read_file(PID, "native-assets/model/files/model.bin"), b"\x00model\xff")

    def test_duplicate_archive_names_are_rejected(self):
        package = share.pack([PID], str(self.root / "source.zip"))["file"]
        altered = self.rewrite(package, {}, duplicate=f"projects/{PID}/project.json")
        with self.assertRaisesRegex(ValueError, "重复路径"):
            share.import_pack(str(altered), overwrite=True)

    def test_legacy_format_imports_source_files_without_suffix_or_size_limit(self):
        self.create_assets()
        package = share.pack([PID], str(self.root / "source.zip"))["file"]
        with zipfile.ZipFile(package) as archive:
            manifest = json.loads(archive.read(share.MANIFEST))
        manifest["format"] = 1
        manifest.pop("files")
        altered = self.rewrite(package, {share.MANIFEST: json.dumps(manifest).encode()})
        with patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.root / "legacy-receiver")}):
            result = share.import_pack(str(altered))
            self.assertEqual(len(result["imported"]), 1)
            self.assertGreater((projects.path(PID) / "input/large.bin").stat().st_size, 5 * 1024 * 1024)
            self.assertTrue((projects.path(PID) / "scripts/run.cmd").is_file())

    def test_pack_inside_project_does_not_include_itself(self):
        package = share.pack([PID], str(self.folder / "shared.zip"))["file"]
        with zipfile.ZipFile(package) as archive:
            self.assertNotIn(f"projects/{PID}/shared.zip", archive.namelist())

    def test_full_pack_includes_native_assets_and_domain_objects(self):
        self.create_assets()
        skill = self.root / "skill"
        skill.mkdir()
        (skill / "SKILL.md").write_text("fixture", encoding="utf-8")
        with patch.object(paths, "SKILL", skill), patch.object(paths, "VENDOR", skill / "vendor"):
            package = share.pack_full(str(self.root / "full.zip"))["file"]
        with zipfile.ZipFile(package) as archive:
            prefix = f"autoskill/workspace/projects/{PID}/"
            self.assertEqual(archive.read(prefix + "input/plan.jmx"), b"<jmeterTestPlan/>")
            self.assertEqual(archive.read(prefix + "scripts/pages/login.py"), b"class LoginPage:\n    pass\n")
            self.assertTrue(any(name.startswith(prefix + "domain/objects/") for name in archive.namelist()))


if __name__ == "__main__":
    unittest.main()
