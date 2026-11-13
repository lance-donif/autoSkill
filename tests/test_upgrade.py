"""Synthetic offline installations exercise data retention and interruption recovery without user data."""
import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "skills/autoskill/app"
sys.path.insert(0, str(APP))

from autoskill import db, locking, maintenance, paths, upgrade


@unittest.skipUnless(sys.platform == "linux" and os.uname().machine == "x86_64", "Synthetic launchers exercise Linux x64 only")
class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.target = self.root / "old installation"
        self.package = self.root / "new package"
        self.workspace = self.target / "autoskill/workspace"
        self.environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": "", "AUTOSKILL_CASE_SKILL": "", "AUTOSKILL_RUNTIME_CONTEXT": "", "AUTOSKILL_BRIDGE": "off"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.make_package(self.target, "1.0.0")
        self.make_package(self.package, "2.0.0")
        self.put(self.workspace / "projects/project/knowledge/note.md", "项目知识")
        self.put(self.workspace / ".local/credentials.json", '{"fixture":"synthetic-credential"}')
        self.put(self.workspace / ".local/sessions/project.json", '{"cookies":[{"name":"synthetic","value":"fixture"}]}')
        self.put(self.workspace / ".local/runs/old/evidence.txt", "旧运行记录")
        self.put(self.target / "cordis.patch.yml", "fixture-custom-config: true\n")

    def put(self, file, value):
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(value, encoding="utf-8")

    def manifest(self, folder):
        files = {file.relative_to(folder).as_posix(): hashlib.sha256(file.read_bytes()).hexdigest()
                 for file in folder.rglob("*") if file.is_file() and file.name != "package-manifest.json"}
        self.put(folder / "package-manifest.json", json.dumps({"format": 1, "version": json.loads((folder / "package.json").read_text())["version"], "files": files}))

    def make_package(self, folder, version):
        self.put(folder / "package.json", json.dumps({"version": version, "files": ["autoskill"]}))
        self.put(folder / "autoskill/SKILL.md", "合成离线包\n")
        app = folder / "autoskill/app/autoskill"
        self.put(app / "__init__.py", "")
        self.put(app / "__main__.py", 'print(\'{"ok":true,"synthetic_cli":true}\')\n')
        shutil.copy2(APP / "autoskill/db.py", app / "db.py")
        shutil.copy2(APP / "autoskill/maintenance.py", app / "maintenance.py")
        self.put(app / "paths.py", "import os\nfrom pathlib import Path\ndef workspace():\n return Path(os.environ['AUTOSKILL_WORKSPACE'])\n"
                 "def local():\n p=workspace()/'.local'\n p.mkdir(parents=True,exist_ok=True)\n return p\n")
        shutil.copy2(APP / "autoskill/upgrade.py", folder / "autoskill-upgrade.py")
        binary = folder / "autoskill/vendor/linux-x64/python/bin/python3"
        self.put(binary, f"#!{sys.executable}\nimport os,sys\nos.execv(sys.executable,[sys.executable,*sys.argv[1:]])\n")
        binary.chmod(0o755)
        self.manifest(folder)

    def prepare(self, **options):
        return upgrade.prepare(str(self.package), str(self.target), **options)["journal"]

    def record(self, journal):
        return json.loads(Path(journal).read_text(encoding="utf-8"))

    def version(self):
        return json.loads((self.target / "package.json").read_text())["version"]

    def test_full_workspace_wal_immutable_assets_modes_and_real_schema_migration_copy(self):
        database = self.workspace / ".local/autoskill.db"
        connection = sqlite3.connect(database)
        self.addCleanup(connection.close)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript(db.SCHEMA)
        connection.execute("INSERT INTO projects(id,name,created_at,updated_at) VALUES('old','preserved','t','t')")
        connection.execute("INSERT INTO tasks(id,project_id,title,engine,rule,created_at,updated_at) VALUES('scheduled','old','task','playwright','{}','t','t')")
        connection.commit()
        database.chmod(0o600)
        self.workspace.chmod(0o700)
        asset = self.root / "asset.sqlite"
        with sqlite3.connect(asset) as asset_db:
            asset_db.execute("CREATE TABLE fixture(value)")
            asset_db.execute("INSERT INTO fixture VALUES('immutable')")
        digest = hashlib.sha256(asset.read_bytes()).hexdigest()
        immutable = self.workspace / "projects/project/domain/objects" / digest
        immutable.parent.mkdir(parents=True)
        shutil.copy2(asset, immutable)
        (self.workspace / "projects/project/empty-fixture").mkdir()
        journal = self.prepare()
        backup = next(item for item in self.record(journal)["backups"][0]["workspaces"] if item["source"] == str(self.workspace))
        backup_db = Path(backup["backup"]) / ".local/autoskill.db"
        with sqlite3.connect(backup_db) as copied:
            self.assertEqual(copied.execute("SELECT name FROM projects").fetchone()[0], "preserved")
        self.assertEqual(backup_db.stat().st_mode & 0o777, 0o600)
        self.assertEqual((Path(backup["backup"]) / immutable.relative_to(self.workspace)).read_bytes(), asset.read_bytes())
        connection.close()
        result = upgrade.apply(journal)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(self.version(), "2.0.0")
        self.assertEqual(self.workspace.stat().st_mode & 0o777, 0o700)
        self.assertTrue((self.workspace / "projects/project/empty-fixture").is_dir())
        self.assertEqual(immutable.read_bytes(), asset.read_bytes())
        with sqlite3.connect(database) as original:
            self.assertNotIn("family", {row[1] for row in original.execute("PRAGMA table_info(runs)")})
            self.assertEqual(original.execute("SELECT count(*) FROM tasks").fetchone()[0], 1)
        probes = list(Path(journal).parent.glob("probe-data-*/.local/autoskill.db"))
        self.assertTrue(probes)
        with sqlite3.connect(probes[0]) as migrated:
            self.assertIn("family", {row[1] for row in migrated.execute("PRAGMA table_info(runs)")})
        self.assertTrue(all(item["ok"] for item in result["checks"]))

    def test_external_custom_case_plan_configs_and_relative_symlink_preserved(self):
        external = self.root / "external workspace"
        custom = self.target / "autoskill/custom-data"
        caseplans = self.root / "external plans"
        config = self.root / "external config.yml"
        for folder in (external, custom, caseplans):
            self.put(folder / "keep.txt", str(folder.name))
        self.put(config, "custom: yes")
        link = self.workspace / "linked"
        link.symlink_to("../../../external workspace", target_is_directory=True)
        with patch.dict(os.environ, {"AUTOSKILL_CASE_SKILL": str(caseplans)}):
            journal = self.prepare(workspaces=[str(external), str(custom)], config_paths=[str(config)])
        upgrade.apply(journal)
        self.assertEqual((custom / "keep.txt").read_text(), "custom-data")
        self.assertTrue(link.is_symlink())
        self.assertEqual((link / "keep.txt").read_text(), "external workspace")
        self.assertEqual(config.read_text(), "custom: yes")
        self.assertEqual((caseplans / "keep.txt").read_text(), "external plans")
        snapshots = self.record(journal)["backups"][-1]["workspaces"]
        external_backup = next(item for item in snapshots if item["source"] == str(external))
        self.assertEqual((Path(external_backup["backup"]) / "keep.txt").read_text(), "external workspace")

    def test_complete_inventory_and_tampered_package_fail_before_replacement(self):
        self.put(self.package / "autoskill/app/autoskill/unlisted.py", "unexpected = True\n")
        with self.assertRaisesRegex(ValueError, "完整性清单"):
            self.prepare()
        self.assertEqual(self.version(), "1.0.0")

    def test_handoff_checks_runtime_and_helper_before_launching(self):
        journal = self.prepare()
        helper = Path(journal).with_name("autoskill-upgrade.py")
        helper.write_text("raise RuntimeError('synthetic tamper')\n", encoding="utf-8")
        with patch.object(upgrade.subprocess, "Popen") as launch, self.assertRaisesRegex(ValueError, "升级器与冻结"):
            upgrade.handoff(journal)
        launch.assert_not_called()

    def test_new_public_write_during_stage_sync_is_fenced_before_acknowledgement(self):
        journal = self.prepare()
        stage = Path(self.record(journal)["stage"])
        original = upgrade._sync_tree

        def concurrent_write(path):
            if path == stage:
                writer = subprocess.run([sys.executable, "-c", "from autoskill.secrets import set_secret; set_secret('SYNTHETIC_UPGRADE_WRITE','fixture')"],
                                        cwd=self.root, env={**os.environ, "PYTHONPATH": str(APP), "AUTOSKILL_WORKSPACE": str(self.workspace)},
                                        capture_output=True, timeout=10, check=False)
                self.assertNotEqual(writer.returncode, 0)
                self.assertIn("正在升级", writer.stderr.decode())
            return original(path)

        with patch.object(upgrade, "_sync_tree", side_effect=concurrent_write):
            self.assertEqual(upgrade.apply(journal)["state"], "completed")
        self.assertNotIn("SYNTHETIC_UPGRADE_WRITE", json.loads((self.workspace / ".local/credentials.json").read_text()))

    def test_noncooperating_writer_is_detected_after_sync_before_directory_switch(self):
        journal = self.prepare()
        stage = Path(self.record(journal)["stage"])
        original = upgrade._sync_tree

        def concurrent_write(path):
            if path == stage:
                self.put(self.workspace / "noncooperating.json", '{"retained":true}')
            return original(path)

        with patch.object(upgrade, "_sync_tree", side_effect=concurrent_write), self.assertRaisesRegex(ValueError, "仍有写入"):
            upgrade.apply(journal)
        self.assertEqual(self.version(), "1.0.0")
        self.assertTrue((self.workspace / "noncooperating.json").is_file())

    def test_shared_access_allows_concurrency_but_blocks_upgrade_until_writer_finishes(self):
        with maintenance.access(self.workspace):
            process = subprocess.run([sys.executable, "-c", ("from autoskill import maintenance; from pathlib import Path; "
                                      "maintenance.check(Path(__import__('os').environ['AUTOSKILL_WORKSPACE']))")],
                                     env={**os.environ, "PYTHONPATH": str(APP), "AUTOSKILL_WORKSPACE": str(self.workspace)},
                                     capture_output=True, check=False)
            self.assertEqual(process.returncode, 0, process.stderr)
            with self.assertRaisesRegex(ValueError, "持有锁"), upgrade._workspace_gate(self.workspace):
                pass
        with upgrade._workspace_gate(self.workspace), self.assertRaisesRegex(ValueError, "正在升级"):
            maintenance.check(self.workspace)

    def test_gate_covers_database_settings_and_shared_sheet_without_hiding_io_errors(self):
        with patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.workspace)}), upgrade._workspace_gate(self.workspace):
            with self.assertRaisesRegex(ValueError, "正在升级"):
                db.init()
            with self.assertRaisesRegex(ValueError, "正在升级"):
                paths.save_settings(browser="bundled")
        sheet = self.root / "shared sheet"
        with upgrade._workspace_gate(sheet), self.assertRaisesRegex(ValueError, "正在升级"), locking.file_lock(sheet / ".autoskill-plan.lock"):
            pass
        self.assertFalse(sheet.exists())
        with self.assertRaises(PermissionError), maintenance.access(self.workspace):
            raise PermissionError("synthetic real I/O error")

    def test_occupied_directory_rename_failure_keeps_old_data_active(self):
        journal = self.prepare()
        original = upgrade._rename

        def occupied(source, destination):
            if source == self.target:
                raise PermissionError("synthetic Windows directory in use")
            return original(source, destination)

        with patch.object(upgrade, "_rename", side_effect=occupied), self.assertRaises(PermissionError):
            upgrade.apply(journal)
        self.assertEqual(self.version(), "1.0.0")
        self.assertEqual(upgrade.status(journal)["last_action"]["state"], "failed")
        self.assertEqual(upgrade.recover(journal)["state"], "prepared")

    def test_only_machine_lock_files_are_omitted_and_exclusion_is_recorded(self):
        self.put(self.workspace / ".local/locks/known.lock", "fixture")
        self.put(self.workspace / "projects/project/native/user.lock", "user-asset")
        journal = self.prepare()
        item = next(item for item in self.record(journal)["backups"][0]["workspaces"] if item["source"] == str(self.workspace))
        inventory = json.loads(Path(item["manifest"]).read_text())
        self.assertIn(".local/locks/known.lock", inventory["omitted_transient_locks"])
        self.assertFalse((Path(item["backup"]) / ".local/locks/known.lock").exists())
        self.assertEqual((Path(item["backup"]) / "projects/project/native/user.lock").read_text(), "user-asset")

    def test_verification_failure_restores_entire_old_installation(self):
        journal = self.prepare()
        with patch.object(upgrade, "_validate_installation", side_effect=ValueError("synthetic verification failure")), self.assertRaises(ValueError):
            upgrade.apply(journal)
        self.assertEqual(upgrade.status(journal)["state"], "rolled_back")
        self.assertEqual(self.version(), "1.0.0")
        self.assertTrue((self.workspace / ".local/credentials.json").is_file())

    def test_rename_interruption_can_recover_and_repeat_without_losing_data(self):
        journal = self.prepare(job_dir=str(self.root / "durable-job"))
        self.assertEqual(self.prepare(job_dir=str(self.root / "durable-job")), journal)
        original = upgrade._state

        def crash(file, record, state, **changes):
            if state == "old_moved":
                raise KeyboardInterrupt("synthetic process crash")
            return original(file, record, state, **changes)

        with patch.object(upgrade, "_state", side_effect=crash), self.assertRaises(KeyboardInterrupt):
            upgrade.apply(journal)
        self.assertFalse(self.target.exists())
        self.assertEqual(upgrade.recover(journal)["state"], "rolled_back")
        self.assertEqual(upgrade.recover(journal)["state"], "rolled_back")
        self.assertEqual(self.version(), "1.0.0")
        self.assertTrue((self.workspace / ".local/sessions/project.json").is_file())

    def test_rollback_keeps_new_data_and_deleted_workspace_config_tombstones(self):
        journal = self.prepare()
        upgrade.apply(journal)
        self.put(self.workspace / "new.txt", "new-data")
        upgrade.rollback(journal)
        self.assertEqual((self.workspace / "new.txt").read_text(), "new-data")
        self.assertEqual(upgrade.rollback(journal)["state"], "rolled_back")
        second = self.prepare()
        upgrade.apply(second)
        shutil.rmtree(self.workspace)
        (self.target / "cordis.patch.yml").unlink()
        upgrade.rollback(second)
        self.assertFalse(self.workspace.exists())
        self.assertFalse((self.target / "cordis.patch.yml").exists())
        self.assertTrue(Path(self.record(second)["old"]).is_dir())

    def test_rollback_staging_crash_does_not_mutate_old_copy_and_recovery_preserves_current_data(self):
        journal = self.prepare()
        upgrade.apply(journal)
        self.put(self.workspace / "new.txt", "after-upgrade")
        with patch.object(upgrade, "_snapshot", side_effect=KeyboardInterrupt("synthetic crash")), self.assertRaises(KeyboardInterrupt):
            upgrade.rollback(journal)
        self.assertEqual(upgrade.status(journal)["state"], "rollback_staging")
        self.assertEqual(upgrade.recover(journal)["state"], "rolled_back")
        self.assertEqual((self.workspace / "new.txt").read_text(), "after-upgrade")

    def test_target_lock_prevents_concurrent_apply(self):
        journal = self.prepare()
        with upgrade._target_lock(self.target), self.assertRaisesRegex(ValueError, "持有锁"):
            upgrade.apply(journal)
        self.assertEqual(upgrade.status(journal)["state"], "prepared")

    def test_recovery_refreshes_data_written_after_interrupted_rollback(self):
        journal = self.prepare()
        upgrade.apply(journal)
        with patch.object(upgrade, "_restore", side_effect=KeyboardInterrupt("before directory switch")), self.assertRaises(KeyboardInterrupt):
            upgrade.rollback(journal)
        self.assertEqual(upgrade.status(journal)["state"], "rollback_ready")
        self.put(self.workspace / "after-crash.json", '{"retained":true}')
        upgrade.recover(journal)
        self.assertEqual(json.loads((self.workspace / "after-crash.json").read_text()), {"retained": True})

    def test_stopping_owned_dispatcher_keeps_schedules_and_retains_receipt(self):
        app = self.target / "autoskill/app"
        self.put(app / "autoskill/server/__init__.py", "")
        self.put(app / "autoskill/server/jobs.py", "import time\nwhile True:\n time.sleep(0.1)\n")
        child = subprocess.Popen([sys.executable, "-m", "autoskill.server.jobs"], cwd=app,
                                 env={**os.environ, "PYTHONPATH": str(app), "PYTHONDONTWRITEBYTECODE": "1"})
        self.addCleanup(lambda: child.poll() is None and child.terminate())
        self.addCleanup(lambda: child.poll() is not None or child.wait(timeout=5))
        self.put(self.workspace / ".local/queue-worker.json", json.dumps({"pid": child.pid, "token": "synthetic"}))
        self.put(self.workspace / "schedule.json", '{"enabled":true}')
        result = subprocess.run([sys.executable, "-B", str(self.package / "autoskill-upgrade.py"), "prepare", str(self.package),
                                 "--target", str(self.target), "--job-dir", str(self.root / "stable-job"), "--stop-services"],
                                cwd=self.root, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True, text=True, timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["state"], "launched")
        import time

        deadline = time.monotonic() + 10
        while upgrade.status(receipt["journal"])["state"] == "awaiting_stop" and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(upgrade.status(receipt["journal"])["state"], "prepared", Path(receipt["journal"]).with_name("handoff.log").read_text())
        child.wait(timeout=5)
        self.assertEqual(json.loads((self.workspace / "schedule.json").read_text()), {"enabled": True})

    def test_neighbor_installation_is_not_stopped_and_active_capture_is_blocked(self):
        neighbor = self.target.with_name(self.target.name + "-neighbor")
        process = {"pid": 12345, "exe": str(neighbor / "autoskill/vendor/python"), "cwd": str(neighbor / "autoskill/app"),
                   "argv": ["python", "-m", "autoskill", "dashboard"]}
        with patch.object(upgrade, "_processes", return_value=[process]), patch.object(upgrade.os, "kill") as kill:
            self.assertEqual(upgrade._process_blockers(self.target, [self.workspace]), [])
            upgrade._stop_services(self.target, [self.workspace])
            kill.assert_not_called()
        process.update(exe=sys.executable, cwd=str(self.target / "autoskill/app"), argv=["python", "-m", "autoskill.recorder.capture", "--folder", str(self.workspace / ".local/network-fixture")])
        with patch.object(upgrade, "_processes", return_value=[process]), self.assertRaisesRegex(ValueError, "录制"):
            upgrade._stop_services(self.target, [self.workspace])

    def test_mid_backup_write_aborts_and_workspace_overlap_is_detected(self):
        original = upgrade._fingerprint
        calls = 0

        def changed(source):
            nonlocal calls
            value = original(source)
            if source == self.workspace:
                calls += 1
                if calls == 1:
                    self.put(self.workspace / "during.txt", "concurrent")
            return value

        with patch.object(upgrade, "_fingerprint", side_effect=changed), self.assertRaisesRegex(ValueError, "备份期间发生变化"):
            self.prepare()
        self.assertEqual(self.version(), "1.0.0")
        journal = self.prepare(workspaces=[str(self.target / "autoskill/app")])
        with self.assertRaisesRegex(ValueError, "程序文件重叠"):
            upgrade.apply(journal)
        self.assertEqual(self.version(), "1.0.0")

    def test_git_checkout_is_reported_without_replacement(self):
        (self.target / ".git").mkdir()
        with self.assertRaisesRegex(ValueError, "Git 源码安装"):
            self.prepare()

    def test_builder_emits_complete_manifest_and_standalone_help(self):
        spec = importlib.util.spec_from_file_location("upgrade_bundle_builder", ROOT / "scripts/build-windows-bundle.py")
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        builder.write_package_manifest(self.package)
        upgrade._verify_package(self.package, complete=True)
        result = subprocess.run([sys.executable, str(self.package / "autoskill-upgrade.py"), "--help"], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("prepare", result.stdout)


if __name__ == "__main__":
    unittest.main()
