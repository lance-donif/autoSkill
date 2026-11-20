"""Portable source trees, immutable domain history and archive integrity."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import warnings
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill import builder, db, domain, paths, projects, secrets, share

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
        source = {"scripts/pages/login.py": b"class LoginPage:\n    pass\n", "input/fixture.json": b'{"fixture":true}',
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
        name = f"projects/{PID}/input/fixture.json"
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

    def test_failed_project_pack_preserves_the_delivered_file_and_leaves_no_partial_output(self):
        deliveries = self.root / "交接"
        target = deliveries / "project.zip"
        share.pack([PID], str(target))
        original = target.read_bytes()
        for destination in (target, deliveries / "first-attempt.zip"):
            with self.subTest(destination=destination), self.assertRaisesRegex(ValueError, "不存在"):
                share.pack([PID, "deleted-project"], str(destination))
            self.assertEqual(list(deliveries.iterdir()), [target])
            self.assertEqual(target.read_bytes(), original)
        with patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.root / "receiver")}):
            self.assertEqual([p["id"] for p in share.import_pack(str(target))["imported"]], [PID])

    def test_exports_inside_a_project_include_attachments_and_exclude_output_and_staging(self):
        skill = self.root / "skill"
        skill.mkdir()
        self.enterContext(patch.object(paths, "SKILL", skill))
        for name in ("attachment.zip", ".autoskill-pack-notes.tmp"):
            (self.folder / name).write_bytes(b"keep")
        target = self.folder / "交接.zip"
        for export in (share.pack, share.pack_full):
            for _ in range(2):
                result = export(out=str(target))
                self.assertEqual(result["bytes"], target.stat().st_size)
                with zipfile.ZipFile(target) as archive:
                    self.assertIsNone(archive.testzip())
                    names = {Path(name).name for name in archive.namelist()}
                    self.assertNotIn(target.name, names)
                    self.assertIn("attachment.zip", names)
                    self.assertEqual({name for name in names if name.endswith(".tmp")}, {".autoskill-pack-notes.tmp"})

    def test_failed_full_pack_keeps_the_previous_archive_until_the_source_is_repaired(self):
        skill = self.root / "skill"
        skill.mkdir()
        (skill / "SKILL.md").write_bytes(b"skill")
        self.enterContext(patch.object(paths, "SKILL", skill))
        target = self.root / "交接" / "full.zip"
        domain.commit(PID, {"sources/order.txt": b"original"}, None)
        file = self.folder / "domain/objects" / domain.file_ref(PID, "sources/order.txt")["sha256"]
        share.pack_full(str(target))
        original = target.read_bytes()
        file.write_bytes(b"corrupt!")
        with self.assertRaisesRegex(ValueError, "哈希"):
            share.pack_full(str(target))
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(list(target.parent.iterdir()), [target])
        file.write_bytes(b"original")
        (skill / "SKILL.md").write_bytes(b"updated skill")
        share.pack_full(str(target))
        with zipfile.ZipFile(target) as archive:
            self.assertEqual(archive.read("autoskill/SKILL.md"), b"updated skill")

    def test_another_export_can_read_the_previous_delivery_while_replacement_is_in_progress(self):
        target = self.folder / "delivered.zip"
        share.pack([PID], str(target))
        original = target.read_bytes()
        (self.folder / "new.txt").write_bytes(b"new")
        started, resume = threading.Event(), threading.Event()
        archive_project = share._archive_project

        def pause(archive, project, entries, needed, destination):
            if destination == target:
                started.set()
                self.assertTrue(resume.wait(timeout=15))
            return archive_project(archive, project, entries, needed, destination)

        with patch.object(share, "_archive_project", pause), ThreadPoolExecutor(max_workers=1) as pool:
            writing = pool.submit(share.pack, [PID], str(target))
            try:
                self.assertTrue(started.wait(timeout=15))
                self.assertEqual(target.read_bytes(), original)
                other = share.pack([PID], str(self.root / "other.zip"))
                with zipfile.ZipFile(other["file"]) as archive:
                    self.assertEqual(archive.read(f"projects/{PID}/delivered.zip"), original)
                    self.assertFalse(any(name.endswith(".tmp") for name in archive.namelist()))
            finally:
                resume.set()
            writing.result(timeout=15)
        with zipfile.ZipFile(target) as archive:
            self.assertEqual(archive.read(f"projects/{PID}/new.txt"), b"new")

    @unittest.skipIf(os.name == "nt", "Unix file mode and symbolic links")
    def test_replacing_an_output_link_preserves_its_target_permissions(self):
        target = self.root / "delivered.zip"
        target.write_bytes(b"previous")
        target.chmod(0o640)
        link = self.root / "latest.zip"
        link.symlink_to(target)
        result = share.pack([PID], str(link))
        self.assertEqual(result["file"], str(link))
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.stat().st_mode & 0o777, 0o640)
        with zipfile.ZipFile(target) as archive:
            self.assertIn(share.MANIFEST, archive.namelist())

    @unittest.skipUnless(sys.platform == "linux" and shutil.which("setfacl"), "Linux POSIX ACL tools required")
    def test_replacement_preserves_named_access_and_excludes_unrelated_inherited_acl(self):
        folder = self.root / "deliveries"
        folder.mkdir()
        a, b = folder / "named.zip", folder / "plain.zip"
        for target in (a, b):
            share.pack([PID], str(target))
        set_named = subprocess.run(["setfacl", "-m", "u:60001:r", str(a)], capture_output=True, check=False)
        if set_named.returncode and b"not supported" in set_named.stderr.lower():
            self.skipTest("temporary filesystem does not support POSIX ACLs")
        set_named.check_returncode()
        acl = "system.posix_acl_access"
        original = os.getxattr(a, acl)
        subprocess.run(["setfacl", "-dm", "u:60002:r", str(folder)], check=True)
        for target in (a, b):
            share.pack([PID], str(target))
        self.assertEqual(os.getxattr(a, acl), original)
        self.assertNotIn(acl, os.listxattr(b))

    @unittest.skipUnless(os.name == "nt", "native Windows file DACL and file sharing required")
    def test_replacement_preserves_windows_dacl_and_open_readers(self):
        target = self.root / "delivered.zip"
        share.pack([PID], str(target))
        subprocess.run(["icacls", str(target), "/deny", "*S-1-5-32-546:(R)"], check=True)
        acl = subprocess.check_output(["icacls", str(target)])
        with target.open("rb") as previous:
            original = previous.read()
            with self.assertRaises(PermissionError):
                share.pack([PID], str(target))
            self.assertEqual(target.read_bytes(), original)
        share.pack([PID], str(target))
        self.assertEqual(subprocess.check_output(["icacls", str(target)]), acl)
        with zipfile.ZipFile(target) as archive:
            self.assertIn(share.MANIFEST, archive.namelist())

    def test_full_pack_includes_native_assets_and_domain_objects(self):
        self.create_assets()
        skill = self.root / "skill"
        skill.mkdir()
        (skill / "SKILL.md").write_text("fixture", encoding="utf-8")
        with patch.object(paths, "SKILL", skill), patch.object(paths, "VENDOR", skill / "vendor"):
            package = share.pack_full(str(self.root / "full.zip"))["file"]
        with zipfile.ZipFile(package) as archive:
            prefix = f"autoskill/workspace/projects/{PID}/"
            self.assertEqual(archive.read(prefix + "input/fixture.json"), b'{"fixture":true}')
            self.assertEqual(archive.read(prefix + "scripts/pages/login.py"), b"class LoginPage:\n    pass\n")
            self.assertTrue(any(name.startswith(prefix + "domain/objects/") for name in archive.namelist()))

    def test_pack_note_distinguishes_raw_source_auth_from_optional_encrypted_store(self):
        domain.attach(PID, "sources/request.curl", b"curl -H 'Authorization: Bearer synthetic-source-token' https://fixture.invalid", None)
        plain = share.pack([PID], str(self.root / "plain.zip"))
        self.assertIn("认证信息按原文保留", plain["note"])
        self.assertIn("未单独导出本机凭据库", plain["note"])
        secrets.set_secret("ARTIFACT_NOTE_TOKEN", "synthetic-store-token")
        projects.save_cases(PID, [builder.api_case("TC-API-001", "fixture", "GET", "/fixture", {"status": 204},
                                                   headers={"Authorization": "${ENV:ARTIFACT_NOTE_TOKEN}"})])
        encrypted = share.pack([PID], str(self.root / "encrypted.zip"), "fixture-passphrase")
        self.assertIn("认证信息按原文保留", encrypted["note"])
        self.assertIn("单独导出的本机凭据已用口令加密", encrypted["note"])


if __name__ == "__main__":
    unittest.main()
