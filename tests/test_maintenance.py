"""Whole-data transfer gates retain concurrent ordinary access and fence writes across processes."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "skills/autoskill/app"
sys.path.insert(0, str(APP))

from autoskill import maintenance


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "data"
        self.home = self.root / "machine-control"
        home = patch.object(Path, "home", return_value=self.home)
        home.start()
        self.addCleanup(home.stop)

    def child(self, code, *, workspace=None):
        prelude = ("import os; from pathlib import Path; from unittest.mock import patch; "
                   "patch.object(Path, 'home', return_value=Path(os.environ['FIXTURE_CONTROL_HOME'])).start(); ")
        env = {**os.environ, "PYTHONPATH": str(APP), "AUTOSKILL_WORKSPACE": str(workspace or self.workspace),
               "AUTOSKILL_RUNTIME_CONTEXT": "", "AUTOSKILL_CASE_SKILL": "", "FIXTURE_CONTROL_HOME": str(self.home)}
        return subprocess.run([sys.executable, "-B", "-c", prelude + code], env=env, cwd=self.root,
                              capture_output=True, text=True, encoding="utf-8", timeout=20, check=False)

    def test_shared_readers_overlap_and_exclusive_transfer_rejects_an_active_reader(self):
        with maintenance.access(self.workspace):
            result = self.child("from autoskill import maintenance,paths; maintenance.check(paths.workspace())")
            self.assertEqual(result.returncode, 0, result.stderr)
            result = self.child("from autoskill import maintenance,paths; gate=maintenance.exclusive(paths.workspace()); gate.__enter__()")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("数据目录正在使用", result.stderr)
        with maintenance.exclusive(self.workspace):
            result = self.child("from autoskill import maintenance,paths; maintenance.check(paths.workspace())")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("数据目录正在使用", result.stderr)
        result = self.child("from autoskill import maintenance,paths; maintenance.check(paths.workspace())")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_exclusive_transfer_blocks_database_and_settings_without_creating_user_data(self):
        with maintenance.exclusive(self.workspace):
            for command in ("from autoskill import db; db.init()", "from autoskill import paths; paths.save_settings(browser='bundled')"):
                with self.subTest(command=command):
                    result = self.child(command)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("数据目录正在使用", result.stderr)
        self.assertFalse(self.workspace.exists())

    def test_shared_external_sheet_uses_the_same_transfer_gate(self):
        sheet = self.root / "shared sheet"
        command = ("from autoskill import locking; "
                   "gate=locking.file_lock(Path(os.environ['AUTOSKILL_WORKSPACE'])/'.autoskill-plan.lock'); gate.__enter__()")
        with maintenance.exclusive(sheet):
            result = self.child(command, workspace=sheet)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("数据目录正在使用", result.stderr)
        self.assertFalse(sheet.exists())

    def test_reentrant_access_and_exclusive_owner_can_write_but_cannot_promote_shared_access(self):
        with maintenance.exclusive(self.workspace), maintenance.exclusive(self.workspace), maintenance.access(self.workspace):
            maintenance.check(self.workspace)
        with (maintenance.access(self.workspace), maintenance.access(self.workspace),
              self.assertRaisesRegex(ValueError, "不能同时进行整目录传输"), maintenance.exclusive(self.workspace)):
            pass
        with maintenance.exclusive(self.workspace):
            maintenance.check(self.workspace)

    def test_original_io_errors_escape_and_release_the_gate(self):
        with self.assertRaisesRegex(PermissionError, "synthetic I/O"), maintenance.access(self.workspace):
            raise PermissionError("synthetic I/O")
        with maintenance.exclusive(self.workspace):
            maintenance.check(self.workspace)
        with self.assertRaisesRegex(PermissionError, "synthetic I/O"), maintenance.exclusive(self.workspace):
            raise PermissionError("synthetic I/O")
        result = self.child("from autoskill import maintenance,paths; maintenance.check(paths.workspace())")
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
