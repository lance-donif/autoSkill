"""Complete data imports preserve source bytes, path identity and atomic publication boundaries."""
import contextlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/autoskill/app"))

from autoskill import command_input, data_refs, data_runtime, data_store, data_transfer, db, paths, runtime_identity


class DataStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.root = self.folder / "selected"
        environment = {key: value for key, value in os.environ.items() if not key.startswith("AUTOSKILL_")}
        environment["AUTOSKILL_WORKSPACE"] = str(self.root)
        self.environment = patch.dict(os.environ, environment, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def put(self, root, name, value=b"synthetic data"):
        file = root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(value)
        return file

    def source(self, name="old"):
        root = self.folder / name
        self.put(root, "projects/p/testcases/TC-API-001.json", b'{"id":"TC-API-001","assertions":{"status":200}}')
        self.put(root, "projects/p/domain/objects/immutable.bin", b"\x00\xfforiginal source")
        self.put(root, ".local/credentials.json", b'{"synthetic":"fixture-only"}')
        self.put(root, ".local/sessions/p.json", b'{"cookies":[{"name":"fixture","value":"synthetic"}]}')
        self.put(root, ".local/runs/old-run/report.bin", b"original failed report")
        self.put(root, ".local/workflows/history.json", b'{"status":"failed","attempt":1}')
        self.put(root, "projects/p/native/original.py", b"assert 1 == 1\n")
        return root

    def database(self, root):
        file = self.put(root, ".local/autoskill.db", b"")
        return sqlite3.connect(file)

    def test_status_does_not_create_a_root_or_database(self):
        status = data_store.status()
        self.assertFalse(status["initialized"])
        self.assertFalse(self.root.exists())

    def test_complete_copy_and_export_keep_history_and_map_to_this_copy(self):
        source = self.source()
        evidence = source / ".local/runs/old-run/report.bin"
        self.put(source, ".local/runs/old-run/receipt.json", json.dumps({"path": str(evidence)}).encode())
        original = {file.relative_to(source).as_posix(): file.read_bytes() for file in source.rglob("*") if file.is_file()}
        imported = data_store.import_data(source)
        self.assertEqual(imported["status"], "imported")
        self.assertEqual(data_refs.resolve(str(evidence)), self.root / evidence.relative_to(source))
        self.assertEqual(runtime_identity.location_id(), data_transfer.read_json(self.root / ".local/runtime-location.json")["location_id"])
        exported = data_store.export_data(self.folder / "complete.zip")
        self.assertEqual(data_store.download_file(exported["export_id"]), self.folder / "complete.zip")
        restored = self.folder / "restored"
        data_store.import_data(exported["archive"], restored)
        for name, value in original.items():
            self.assertEqual((source / name).read_bytes(), value, name)
            self.assertEqual((restored / name).read_bytes(), value, name)
        self.assertEqual(data_refs.resolve(str(evidence), restored), restored / evidence.relative_to(source))
        self.assertTrue(source.is_dir())

    def test_target_file_created_after_final_check_survives(self):
        source = self.source()
        target = self.folder / "target"
        target.mkdir()
        check = data_store._empty_target
        calls = 0

        def interleave(folder):
            nonlocal calls
            check(folder)
            calls += 1
            if calls == 2:
                self.put(target, "new-user-file", b"must survive")

        with patch.object(data_store, "_empty_target", side_effect=interleave), self.assertRaises(data_store.DataError) as error:
            data_store.import_data(source, target)
        self.assertEqual(error.exception.code, "target_not_empty")
        self.assertEqual((target / "new-user-file").read_bytes(), b"must survive")
        self.assertEqual(list(target.iterdir()), [target / "new-user-file"])

    def test_new_target_directory_after_removal_is_not_overwritten(self):
        source = self.source()
        target = self.folder / "target"
        target.mkdir()
        publish = data_transfer.publish_directory

        def interleave(stage, destination):
            destination.mkdir()
            self.put(destination, "concurrent-file", b"new owner")
            publish(stage, destination)

        with patch.object(data_transfer, "publish_directory", side_effect=interleave), self.assertRaises(data_store.DataError):
            data_store.import_data(source, target)
        self.assertEqual((target / "concurrent-file").read_bytes(), b"new owner")
        self.assertFalse((target / "projects").exists())

    def test_publisher_also_preserves_a_concurrently_created_empty_directory(self):
        stage = self.folder / "stage"
        stage.mkdir()
        self.put(stage, "payload")
        target = self.folder / "target"
        target.mkdir()
        with self.assertRaises(FileExistsError):
            data_transfer.publish_directory(stage, target)
        self.assertTrue((stage / "payload").is_file())
        self.assertEqual(list(target.iterdir()), [])

    def test_bridge_installation_maps_sibling_sheet_and_custom_sheet_to_original_paths(self):
        original = self.folder / "caller-install"
        workspace = original / "autoskill/workspace"
        self.put(workspace, "projects/p/config.json", b"{}")
        sibling = self.put(original, "autoskill-cases/用例.xlsx", b"default sheet")
        custom = self.put(original, "custom-cases/用例.xlsx", b"custom sheet")
        with self.database(workspace) as connection:
            connection.execute("CREATE TABLE tasks(id TEXT, plan_path TEXT)")
            connection.execute("INSERT INTO tasks VALUES(?, ?)", ("task", str(custom.parent)))
        attachment = command_input._attachment(str(original))
        mapping = command_input.materialize_attachments([attachment], self.folder / "transport")
        source = Path(mapping[str(original)])
        shutil.rmtree(original)
        data_store.import_data(source, original_source=original)
        metadata = data_transfer.read_json(self.root / "data-root.json")
        self.assertIn(str(workspace), metadata["legacy_roots"])
        self.assertEqual({item["source"] for item in metadata["path_mappings"]}, {str(sibling.parent), str(custom.parent)})
        self.assertEqual(data_refs.resolve(str(sibling)).read_bytes(), b"default sheet")
        self.assertEqual(data_refs.resolve(str(custom)).read_bytes(), b"custom sheet")
        self.assertFalse(any(str(self.folder / "transport") in item["source"] for item in metadata["path_mappings"]))

    def test_plan_dependencies_come_from_the_same_frozen_database(self):
        source = self.source()
        plan_a = self.put(self.folder, "plan-a/用例.xlsx", b"A")
        plan_b = self.put(self.folder, "plan-b/用例.xlsx", b"B")
        with self.database(source) as connection:
            connection.execute("CREATE TABLE tasks(id TEXT, plan_path TEXT)")
            connection.execute("INSERT INTO tasks VALUES(?, ?)", ("task", str(plan_a.parent)))
        collect = data_store._collect_plans

        def interleave(frozen, destination, metadata, skill):
            with sqlite3.connect(source / ".local/autoskill.db") as connection:
                connection.execute("UPDATE tasks SET plan_path=?", (str(plan_b.parent),))
            return collect(frozen, destination, metadata, skill)

        with patch.object(data_store, "_collect_plans", side_effect=interleave):
            data_store.import_data(source)
        with sqlite3.connect(self.root / ".local/autoskill.db") as connection:
            original_path = connection.execute("SELECT plan_path FROM tasks").fetchone()[0]
        self.assertEqual(original_path, str(plan_a.parent))
        self.assertEqual((data_refs.resolve(original_path) / "用例.xlsx").read_bytes(), b"A")

    def test_sqlite_wal_is_backed_up_but_immutable_sqlite_is_byte_preserved(self):
        source = self.source()
        immutable = self.put(source, "projects/p/domain/objects/sqlite-asset", b"")
        with sqlite3.connect(immutable) as connection:
            connection.execute("CREATE TABLE payload(value TEXT)")
            connection.execute("INSERT INTO payload VALUES('immutable fixture')")
        original = immutable.read_bytes()
        with contextlib.closing(self.database(source)) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA wal_autocheckpoint=0")
            connection.execute("CREATE TABLE history(value TEXT)")
            connection.execute("INSERT INTO history VALUES('committed WAL')")
            connection.commit()
            manifest = data_transfer.snapshot(source, self.folder / "snapshot")
        self.assertEqual(manifest["sqlite"], [".local/autoskill.db"])
        self.assertEqual((self.folder / "snapshot/projects/p/domain/objects/sqlite-asset").read_bytes(), original)
        with sqlite3.connect(self.folder / "snapshot/.local/autoskill.db") as connection:
            self.assertEqual(connection.execute("SELECT value FROM history").fetchone()[0], "committed WAL")

    def test_changed_source_during_copy_is_not_published(self):
        source = self.source()
        copy = shutil.copy2
        changed = False

        def interleave(old, new, *args, **kwargs):
            nonlocal changed
            result = copy(old, new, *args, **kwargs)
            if not changed:
                changed = True
                self.put(source, "projects/p/changed-during-copy", b"new revision")
            return result

        with patch.object(shutil, "copy2", side_effect=interleave), self.assertRaisesRegex(ValueError, "发生变化"):
            data_store.import_data(source)
        self.assertFalse(self.root.exists())
        self.assertTrue((source / "projects/p/changed-during-copy").is_file())

    def test_live_owned_run_prevents_snapshot_without_changing_its_state(self):
        db.init()
        db.insert("runs", {"id": "active-fixture", "project_id": "p", "engine": "http", "status": "Running", "pid": os.getpid(),
                           "owner_identity": json.dumps(runtime_identity.current()), "created_at": "2026-11-13T00:00:00Z"})
        target = self.folder / "target"
        with self.assertRaises(data_store.DataError) as error:
            data_store.import_data(self.root, target)
        self.assertEqual(error.exception.code, "data_busy")
        self.assertTrue(error.exception.details["activities"])
        self.assertEqual(db.one("SELECT status FROM runs WHERE id='active-fixture'")["status"], "Running")
        self.assertFalse(target.exists())

    def test_manual_whole_folder_zip_flat_and_single_top_directory(self):
        source = self.source()
        data_transfer.write_json(source / "data-root.json", {"schema_version": 1, "data_id": "portable-fixture", "locations": [str(source)]})
        (source / "projects/p/empty").mkdir()
        for prefix in ("", "whole-data/"):
            with self.subTest(prefix=prefix):
                archive = self.folder / ("flat.zip" if not prefix else "enclosed.zip")
                with zipfile.ZipFile(archive, "w") as package:
                    for file in source.rglob("*"):
                        package.write(file, prefix + file.relative_to(source).as_posix())
                target = self.folder / ("flat" if not prefix else "enclosed")
                result = data_store.import_data(archive, target)
                self.assertEqual(result["data_id"], "portable-fixture")
                self.assertTrue((target / "projects/p/empty").is_dir())
                self.assertEqual((target / ".local/credentials.json").read_bytes(), (source / ".local/credentials.json").read_bytes())

    @unittest.skipIf(os.name == "nt", "POSIX filenames and modes require a POSIX filesystem")
    def test_archive_restores_legal_posix_names_executable_modes_and_empty_directories(self):
        source = self.source()
        script = self.put(source, "projects/p/native/run.sh", b"#!/bin/sh\nexit 0\n")
        script.chmod(0o700)
        self.put(source, "projects/p/knowledge/10:30-notes.md", b"legal filename")
        self.put(source, "projects/p/knowledge/back\\slash.md", b"literal backslash")
        empty = source / "projects/p/empty"
        empty.mkdir()
        empty.chmod(0o750)
        data_store.import_data(source)
        receipt = data_store.export_data(self.folder / "modes.zip")
        target = self.folder / "restored"
        data_store.import_data(receipt["archive"], target)
        self.assertEqual(stat.S_IMODE((target / script.relative_to(source)).stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((target / empty.relative_to(source)).stat().st_mode), 0o750)
        self.assertEqual((target / "projects/p/knowledge/10:30-notes.md").read_bytes(), b"legal filename")
        self.assertEqual((target / "projects/p/knowledge/back\\slash.md").read_bytes(), b"literal backslash")

    def test_windows_unrepresentable_names_fail_during_preflight(self):
        with patch.object(data_transfer.os, "name", "nt"):
            for name in ("projects/p/10:30.md", "projects/p/AUX.txt", "projects/p/trailing.", "projects/p/part\\file"):
                with self.subTest(name=name), self.assertRaisesRegex(ValueError, "Windows"):
                    data_transfer.relative_name(name)

    def test_archive_rejects_traversal_absolute_links_and_unlisted_files(self):
        for index, name in enumerate(("../escape", "/absolute", "C:/absolute", "data/../../escape", "data//double")):
            with self.subTest(name=name):
                archive = self.folder / f"invalid-{index}.zip"
                with zipfile.ZipFile(archive, "w") as package:
                    package.writestr(name, b"invalid")
                with self.assertRaises(ValueError):
                    data_store.import_data(archive, self.folder / f"target-{index}")
                self.assertFalse((self.folder / f"target-{index}").exists())
        archive = self.folder / "symlink.zip"
        with zipfile.ZipFile(archive, "w") as package:
            info = zipfile.ZipInfo("link")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            package.writestr(info, "../escape")
        with self.assertRaisesRegex(ValueError, "链接"):
            data_store.import_data(archive, self.folder / "links")
        snapshot = self.folder / "snapshot"
        manifest = {"format": "autoskill-data", "schema_version": 1, **data_transfer.snapshot(self.source(), snapshot)}
        archive = self.folder / "extra.zip"
        data_transfer.archive(snapshot, archive, manifest)
        with zipfile.ZipFile(archive, "a") as package:
            package.writestr("data/unlisted", b"not listed")
        with self.assertRaisesRegex(ValueError, "清单不一致"):
            data_store.import_data(archive, self.folder / "unlisted")

    def test_export_download_rejects_later_file_mutation(self):
        data_store.import_data(self.source())
        result = data_store.export_data(self.folder / "download.zip")
        Path(result["archive"]).write_bytes(b"replacement")
        with self.assertRaisesRegex(data_store.DataError, "已变化"):
            data_store.download_file(result["export_id"])

    def test_manual_zip_crc_failure_never_publishes_a_partial_target(self):
        archive = self.folder / "corrupt.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as package:
            package.writestr("data-root.json", '{"schema_version":1,"data_id":"crc-fixture"}')
            package.writestr("projects/p/source.bin", b"unique-payload-for-crc")
        archive.write_bytes(archive.read_bytes().replace(b"unique-payload-for-crc", b"xnique-payload-for-crc"))
        with self.assertRaises(data_store.DataError) as error:
            data_store.import_data(archive)
        self.assertEqual(error.exception.code, "invalid_data")
        self.assertFalse(self.root.exists())

    def test_old_manifest_without_modes_remains_importable_and_hash_mismatch_fails(self):
        snapshot = self.folder / "snapshot"
        manifest = {"format": "autoskill-data", "schema_version": 1, **data_transfer.snapshot(self.source(), snapshot)}
        manifest.pop("modes")
        manifest.pop("directories")
        archive = self.folder / "old-format.zip"
        data_transfer.archive(snapshot, archive, manifest)
        data_store.import_data(archive)
        self.assertEqual((self.root / ".local/runs/old-run/report.bin").read_bytes(), b"original failed report")
        manifest["files"][".local/runs/old-run/report.bin"]["sha256"] = "0" * 64
        corrupt = self.folder / "hash-mismatch.zip"
        data_transfer.archive(snapshot, corrupt, manifest)
        target = self.folder / "bad-hash"
        with self.assertRaisesRegex(ValueError, "校验失败"):
            data_store.import_data(corrupt, target)
        self.assertFalse(target.exists())

    def test_bootstrap_finishes_pending_relocation_even_after_local_marker_was_written(self):
        data_store.ensure_initialized()
        write = data_runtime._durable_json

        def interrupted(file, value, root):
            write(file, value, root)
            if file.name == "runtime-location.json":
                raise OSError("synthetic interruption after marker")

        with patch.object(data_runtime, "_durable_json", side_effect=interrupted), self.assertRaises(OSError):
            data_runtime.attach(self.root, force=True)
        pending = data_transfer.read_json(self.root / ".local/relocation-pending.json")
        result = data_store.ensure_initialized()
        self.assertEqual(result["event_id"], pending["event_id"])
        self.assertFalse((self.root / ".local/relocation-pending.json").exists())

    def test_default_bootstrap_imports_old_workspace_sheet_and_runtime_configuration(self):
        install = self.folder / "installation"
        skill = install / "autoskill"
        source = skill / "workspace"
        self.put(source, "projects/p/config.json", b"{}")
        plan = self.put(install, "autoskill-cases/用例.xlsx", b"sheet")
        self.put(skill, "vendor/linux-x64/runtime-settings.json", b'{"browser":"existing"}')
        environment = {key: value for key, value in os.environ.items() if key != "AUTOSKILL_WORKSPACE"}
        with (patch.dict(os.environ, environment, clear=True), patch.object(paths, "SKILL", skill),
              patch.object(paths, "default_workspace", return_value=self.root)):
            result = data_store.ensure_initialized(self.root)
        self.assertEqual(result["status"], "imported")
        self.assertEqual((self.root / ".local/runtime-settings/linux-x64.json").read_bytes(), b'{"browser":"existing"}')
        self.assertEqual(data_refs.resolve(str(plan)).read_bytes(), b"sheet")
        self.assertTrue(source.is_dir())

    def test_existing_new_runtime_configuration_wins_over_legacy_vendor_copy(self):
        install = self.folder / "installation"
        source = install / "autoskill/workspace"
        self.put(source, ".local/runtime-settings/linux-x64.json", b'{"browser":"saved"}')
        self.put(install, "autoskill/vendor/linux-x64/runtime-settings.json", b'{"browser":"old"}')
        data_store.import_data(install)
        self.assertEqual((self.root / ".local/runtime-settings/linux-x64.json").read_bytes(), b'{"browser":"saved"}')


if __name__ == "__main__":
    unittest.main()
