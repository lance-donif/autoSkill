"""Shared Excel plans serialize whole updates across named tasks and distinct workspaces."""
import json
import multiprocessing
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill import builder, db, execution_inputs, plan, projects, runner


class PlanHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        self.send_response(204 if self.path == "/ok" else 404)
        self.end_headers()


def configure_task(workspace, folder, session):
    os.environ.update(AUTOSKILL_WORKSPACE=workspace, AUTOSKILL_CASE_SKILL=folder, AUTOSKILL_SESSION=session)


def apply_operation(operation):
    kind, value = operation
    if kind == "mark":
        return plan.mark(value, "通过", f"Completed {value}")
    if kind == "failed_mark":
        with patch.object(Path, "replace", side_effect=PermissionError("fixture workbook is busy")):
            try:
                plan.mark(value, "通过", "Temporary write")
            except ValueError as error:
                if "用例表正被其他程序打开" in str(error):
                    return {"failed_write": True}
                raise
        raise AssertionError("Expected the atomic publication to fail")
    if kind == "link":
        return plan.link(value, "shared", "TC-API-001", 1)
    if kind == "sync":
        return plan.sync()
    if kind == "write":
        return plan.write("shared", value)
    return plan.show()


def paused_operation(workspace, folder, operation, opened, release, after_save):
    configure_task(workspace, folder, "task-a")
    save, lock = plan._save, plan.file_lock

    def pause(book):
        result = save(book) if after_save else None
        opened.set()
        if not release.wait(timeout=15):
            raise RuntimeError("Paused plan operation was not released")
        return result if after_save else save(book)

    @contextmanager
    def held(path):
        with lock(path), patch.object(plan, "_save", pause):
            yield

    with patch.object(plan, "file_lock", held):
        result = apply_operation(operation)
    if result and result.get("error"):
        raise RuntimeError(result["error"])


def second_operation(workspace, folder, operation, started, finished, results):
    configure_task(workspace, folder, "task-b")
    lock = plan.file_lock

    @contextmanager
    def entering(path):
        started.set()
        with lock(path):
            yield

    with patch.object(plan, "file_lock", entering):
        result = apply_operation(operation)
    if result and result.get("error"):
        raise RuntimeError(result["error"])
    results.put(result)
    finished.set()


class SharedPlanWriteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = ThreadingHTTPServer(("127.0.0.1", 0), PlanHandler)
        cls.thread = threading.Thread(target=cls.site.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.site.shutdown()
        cls.thread.join(timeout=5)
        cls.site.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = str(self.root / "workspace-a")
        self.other_workspace = str(self.root / "workspace-b")
        self.folder = self.root / "shared-plan"
        environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": self.workspace,
                                              "AUTOSKILL_CASE_SKILL": str(self.folder), "AUTOSKILL_SESSION": "fixture"})
        environment.start()
        self.addCleanup(environment.stop)
        self.context = multiprocessing.get_context("spawn")
        projects.create("Shared plan", f"http://127.0.0.1:{self.site.server_port}", project_id="shared")
        projects.save_cases("shared", [builder.api_case("TC-API-001", "Replay case", "GET", "/ok", {"status": 204})])
        self.source = self.root / "cases.json"
        self.source.write_text(json.dumps([{"id": "TC-001", "title": "First row"},
                                           {"id": "TC-002", "title": "Second row"}]), encoding="utf-8")
        self.excel = Path(plan.write("shared", str(self.source))["excel"])

    def finish_processes(self, processes):
        try:
            deadline = time.monotonic() + 30
            for process in processes:
                process.join(timeout=max(0, deadline - time.monotonic()))
            self.assertEqual([process.exitcode for process in processes], [0] * len(processes))
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)

    def overlapping_operations(self, first, second, second_folder=None, should_wait=True, after_save=False):
        opened, release = self.context.Event(), self.context.Event()
        started, finished = self.context.Event(), self.context.Event()
        results = self.context.Queue()
        self.addCleanup(results.join_thread)
        self.addCleanup(results.close)
        writer = self.context.Process(target=paused_operation,
                                      args=(self.workspace, str(self.folder), first, opened, release, after_save))
        follower = self.context.Process(target=second_operation,
                                        args=(self.other_workspace, str(second_folder or self.folder), second, started, finished, results))
        processes = [writer]
        writer.start()
        try:
            self.assertTrue(opened.wait(timeout=10))
            follower.start()
            processes.append(follower)
            self.assertTrue(started.wait(timeout=10))
            if should_wait:
                self.assertFalse(finished.wait(timeout=0.2))
            else:
                self.assertTrue(finished.wait(timeout=10))
        finally:
            release.set()
            self.finish_processes(processes)
        return results.get(timeout=5)

    def rows(self):
        return {row["id"]: row for row in plan.show()["cases"]}

    def cli_show(self, folder, code):
        env = {**os.environ, "PYTHONPATH": str(ROOT / "skills/autoskill/app"), "AUTOSKILL_CASE_SKILL": str(folder),
               "AUTOSKILL_BRIDGE": "off", "PYTHONUTF8": "1"}
        result = subprocess.run([sys.executable, "-B", "-m", "autoskill", "plan", "show"], cwd=self.root, env=env,
                                capture_output=True, text=True, encoding="utf-8", timeout=30, check=False)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_named_tasks_in_different_workspaces_keep_both_marks(self):
        self.overlapping_operations(("mark", "TC-001"), ("mark", "TC-002"))
        self.assertEqual([(row["status"], row["note"]) for row in self.rows().values()],
                         [("通过", "Completed TC-001"), ("通过", "Completed TC-002")])

    def test_link_does_not_overwrite_a_concurrent_mark(self):
        self.overlapping_operations(("mark", "TC-001"), ("link", "TC-002"))
        rows = self.rows()
        self.assertEqual(rows["TC-001"]["status"], "通过")
        self.assertEqual(rows["TC-001"]["note"], "Completed TC-001")
        self.assertEqual(rows["TC-002"]["replay"], "shared/TC-API-001")

    def test_sync_and_manual_mark_preserve_each_other(self):
        run = runner.execute(runner.create("shared")["id"])
        result = db.one("SELECT * FROM results WHERE run_id = ?", (run["id"],))
        self.assertEqual(run["status"], "Pass")
        self.assertTrue(execution_inputs.result_evidence(run, result))
        plan.link("TC-001", "shared", "TC-API-001", 1)
        self.assertEqual(self.rows()["TC-001"]["status"], "未执行")
        self.overlapping_operations(("sync", ""), ("mark", "TC-002"))
        rows = self.rows()
        self.assertEqual([row["status"] for row in rows.values()], ["通过", "通过"])
        self.assertEqual(rows["TC-002"]["note"], "Completed TC-002")

    def test_show_reads_the_new_workbook_and_its_matching_task_after_a_writer_finishes(self):
        previous = plan.show()
        self.source.write_text(json.dumps([{"id": "TC-NEW", "title": "Replacement", "description": "New source"}]), encoding="utf-8")
        shown = self.overlapping_operations(("write", str(self.source)), ("show", ""), after_save=True)
        current = plan.show()
        self.assertNotEqual(shown["task_id"], previous["task_id"])
        self.assertEqual(shown["task_id"], current["task_id"])
        self.assertEqual(shown["initial_report_id"], current["initial_report_id"])
        self.assertEqual([(row["id"], row["description"]) for row in shown["cases"]], [("TC-NEW", "New source")])

    def test_write_and_file_selection_are_inside_the_shared_lock(self):
        self.source.write_text(json.dumps([{"id": "TC-NEW", "title": "Replacement row"}]), encoding="utf-8")
        self.overlapping_operations(("write", str(self.source)), ("mark", "TC-NEW"))
        self.assertEqual([(row["id"], row["title"], row["status"]) for row in plan.show()["cases"]],
                         [("TC-NEW", "Replacement row", "通过")])

    def test_different_plan_directories_do_not_block_each_other(self):
        other = self.root / "other-plan"
        with patch.dict(os.environ, {"AUTOSKILL_CASE_SKILL": str(other)}):
            plan.write("shared", str(self.source))
        self.overlapping_operations(("mark", "TC-001"), ("mark", "TC-002"), second_folder=other, should_wait=False)
        self.assertEqual(self.rows()["TC-001"]["status"], "通过")
        with patch.dict(os.environ, {"AUTOSKILL_CASE_SKILL": str(other)}):
            self.assertEqual(self.rows()["TC-002"]["status"], "通过")

    def test_cli_show_reads_a_mapped_copy_of_an_external_read_only_sheet(self):
        (self.folder / ".autoskill-plan.lock").unlink()
        before = {file.relative_to(self.folder): file.read_bytes() for file in self.folder.rglob("*") if file.is_file()}
        folder_mode = stat.S_IMODE(self.folder.stat().st_mode)
        excel_mode = stat.S_IMODE(self.excel.stat().st_mode)
        self.excel.chmod(0o444)
        self.folder.chmod(0o555)
        try:
            shown = self.cli_show(self.folder, 0)
        finally:
            self.folder.chmod(folder_mode)
            self.excel.chmod(excel_mode)
        self.assertEqual([row["id"] for row in shown["cases"]], ["TC-001", "TC-002"])
        self.assertTrue(Path(shown["excel"]).is_relative_to(Path(self.workspace)))
        self.assertEqual(Path(shown["excel"]).read_bytes(), before[self.excel.relative_to(self.folder)])
        self.assertEqual({file.relative_to(self.folder): file.read_bytes() for file in self.folder.rglob("*") if file.is_file()}, before)

    def test_show_of_a_missing_plan_does_not_create_its_directory(self):
        missing = self.root / "missing-plan"
        with patch.dict(os.environ, {"AUTOSKILL_CASE_SKILL": str(missing)}), \
                patch.object(plan, "file_lock", side_effect=AssertionError("Read-only show must not acquire a write lock")), \
                self.assertRaisesRegex(ValueError, "还没有用例表"):
            plan.show()
        error = self.cli_show(missing, 2)
        self.assertIn(str(missing), error["error"])
        self.assertEqual(error["code"], "validation_error")
        self.assertFalse(missing.exists())

    def test_failed_writer_releases_the_lock_for_a_waiter_and_retry(self):
        self.overlapping_operations(("failed_mark", "TC-001"), ("mark", "TC-002"))
        rows = self.rows()
        self.assertEqual((rows["TC-001"]["status"], rows["TC-001"]["note"]), ("未执行", ""))
        self.assertEqual((rows["TC-002"]["status"], rows["TC-002"]["note"]), ("通过", "Completed TC-002"))
        self.assertEqual(list(self.folder.glob(".autoskill-plan-*.tmp")), [])
        plan.mark("TC-001", "通过", "Retried mark")
        self.assertEqual([(row["status"], row["note"]) for row in self.rows().values()],
                         [("通过", "Retried mark"), ("通过", "Completed TC-002")])

    def test_failed_replace_preserves_original_book_and_ignores_temporary_file(self):
        before = self.excel.read_bytes()

        def refuse_replace(source, target):
            self.assertEqual(Path(target), self.excel)
            self.assertEqual(plan._file(), self.excel)
            self.assertNotEqual(source.suffix, ".xlsx")
            raise PermissionError("fixture workbook is open")

        with patch.object(Path, "replace", refuse_replace), self.assertRaisesRegex(ValueError, "用例表正被其他程序打开"):
            plan.mark("TC-001", "通过")
        self.assertEqual(self.excel.read_bytes(), before)
        self.assertEqual(list(self.folder.glob(".autoskill-plan-*.tmp")), [])

    def test_failed_workbook_serialization_keeps_the_original_book(self):
        before = self.excel.read_bytes()

        def partial_save(book, target):
            Path(target).write_bytes(b"incomplete fixture workbook")
            raise OSError("fixture disk failure")

        with patch.object(Workbook, "save", partial_save), self.assertRaisesRegex(OSError, "fixture disk failure"):
            plan.mark("TC-001", "通过")
        self.assertEqual(self.excel.read_bytes(), before)
        self.assertEqual(list(self.folder.glob(".autoskill-plan-*.tmp")), [])

    @unittest.skipIf(os.name == "nt", "POSIX workbook sharing permissions")
    def test_atomic_save_preserves_existing_workbook_permissions(self):
        self.excel.chmod(0o640)
        plan.mark("TC-001", "通过")
        self.assertEqual(stat.S_IMODE(self.excel.stat().st_mode), 0o640)

    def test_lock_permission_errors_preserve_best_effort_link_and_sync(self):
        with patch.object(plan, "file_lock", side_effect=PermissionError("fixture lock permission")):
            self.assertIn("用例表正被其他程序打开", plan.link("TC-001", "shared", "TC-API-001", 1)["error"])
            self.assertIn("用例表正被其他程序打开", plan.sync(quiet=True)["error"])


if __name__ == "__main__":
    unittest.main()
