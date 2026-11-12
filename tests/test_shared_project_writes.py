"""Shared project mutations remain coherent across independent processes and imports."""
import multiprocessing
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill import builder, db, paths, projects, share

PID = "shared"


def append_cases(workspace, number, barrier):
    os.environ["AUTOSKILL_WORKSPACE"] = workspace
    for iteration in range(4):
        case = builder.api_case(f"TC-API-{number * 10 + iteration + 1:03d}", f"Writer {number}, case {iteration}",
                                "GET", "/ok", {"status": 204})
        barrier.wait(timeout=15)
        projects.save_cases(PID, [case])


def update_field(workspace, field, value, barrier):
    os.environ["AUTOSKILL_WORKSPACE"] = workspace
    barrier.wait(timeout=15)
    projects.update(PID, **{field: value})


def create_project(workspace, number, explicit, barrier, results):
    os.environ["AUTOSKILL_WORKSPACE"] = workspace
    barrier.wait(timeout=15)
    info = projects.create(f"Writer {number}", f"https://shared.example.invalid/area-{number}",
                           project_id=f"explicit-{number}" if explicit else None)
    results.put((info["id"], info.get("reused", False)))


def import_project(workspace, package, barrier, results):
    os.environ["AUTOSKILL_WORKSPACE"] = workspace
    validate = share._validate_import

    def validate_together(staging):
        validate(staging)
        barrier.wait(timeout=15)

    with patch.object(share, "_validate_import", validate_together):
        result = share.import_pack(package)
    results.put(([item["name"] for item in result["imported"]], [item["id"] for item in result["skipped"]]))


def pause_import_after_backup(workspace, package, moved, release):
    os.environ["AUTOSKILL_WORKSPACE"] = workspace
    replace = Path.replace
    project = paths.projects_dir() / PID

    def pause_replace(source, target):
        result = replace(source, target)
        if source == project and target.name == "previous":
            moved.set()
            if not release.wait(timeout=15):
                raise RuntimeError("Project import was not released")
        return result

    with patch.object(Path, "replace", pause_replace):
        result = share.import_pack(package, overwrite=True)
    if not result["imported"]:
        raise RuntimeError("Project import did not finish")


def list_projects(workspace, started, finished, results):
    os.environ["AUTOSKILL_WORKSPACE"] = workspace
    started.set()
    results.put([(item["id"], item["name"]) for item in projects.list_all()])
    finished.set()


class SharedProjectWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = str(self.root / "workspace")
        environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": self.workspace})
        environment.start()
        self.addCleanup(environment.stop)
        self.context = multiprocessing.get_context("spawn")
        db.init()

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

    def run_processes(self, target, arguments):
        processes = [self.context.Process(target=target, args=arguments_for_process) for arguments_for_process in arguments]
        for process in processes:
            process.start()
        self.finish_processes(processes)

    def result_queue(self):
        results = self.context.Queue()
        self.addCleanup(results.join_thread)
        self.addCleanup(results.close)
        return results

    def package(self, name):
        with patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.root / f"source-{name}")}):
            projects.create(name, "https://shared.example.invalid", project_id=PID)
            case = builder.api_case("TC-API-001", name, "GET", "/ok", {"status": 204})
            projects.save_cases(PID, [case])
            return share.pack([PID], str(self.root / f"{name}.zip"))["file"]

    def test_distinct_case_updates_survive_concurrent_processes(self):
        projects.create("Shared project", "https://shared.example.invalid", project_id=PID)
        barrier = self.context.Barrier(4)
        self.run_processes(append_cases, [(self.workspace, number, barrier) for number in range(4)])
        expected = {f"TC-API-{number * 10 + iteration + 1:03d}" for number in range(4) for iteration in range(4)}
        self.assertEqual({item["id"] for item in projects.cases(PID)}, expected)
        self.assertEqual({row["case_id"] for row in db.rows("SELECT case_id FROM cases WHERE project_id = ?", (PID,))}, expected)

    def test_independent_metadata_and_config_updates_are_merged(self):
        projects.create("Shared project", "https://shared.example.invalid", project_id=PID)
        fields = {"name": "Updated project", "category": "Updated category", "timeout_seconds": 33, "page_load_seconds": 120}
        barrier = self.context.Barrier(len(fields))
        self.run_processes(update_field, [(self.workspace, field, value, barrier) for field, value in fields.items()])
        saved = projects.get(PID)
        self.assertEqual({field: saved[field] for field in fields}, fields)

    def test_simultaneous_automatic_creation_reuses_one_site(self):
        barrier, results = self.context.Barrier(4), self.result_queue()
        self.run_processes(create_project, [(self.workspace, number, False, barrier, results) for number in range(4)])
        created = [results.get(timeout=5) for _ in range(4)]
        self.assertEqual(len({project_id for project_id, _ in created}), 1)
        self.assertEqual(sum(not reused for _, reused in created), 1)
        self.assertEqual(len(projects.list_all()), 1)

    def test_explicit_ids_still_allow_distinct_projects_for_the_same_site(self):
        barrier, results = self.context.Barrier(4), self.result_queue()
        self.run_processes(create_project, [(self.workspace, number, True, barrier, results) for number in range(4)])
        created = [results.get(timeout=5) for _ in range(4)]
        self.assertEqual({project_id for project_id, _ in created}, {f"explicit-{number}" for number in range(4)})
        self.assertFalse(any(reused for _, reused in created))

    def test_concurrent_imports_recheck_existing_project_before_install(self):
        packages = [self.package("First"), self.package("Second")]
        barrier, results = self.context.Barrier(2), self.result_queue()
        self.run_processes(import_project, [(self.workspace, package, barrier, results) for package in packages])
        imported = [results.get(timeout=5) for _ in packages]
        winners = [name for names, _ in imported for name in names]
        skipped = [project_id for _, ids in imported for project_id in ids]
        self.assertEqual(len(winners), 1)
        self.assertEqual(skipped, [PID])
        self.assertEqual(projects.get(PID)["name"], winners[0])
        self.assertEqual(projects.case(PID, "TC-API-001")["title"], winners[0])
        self.assertEqual(db.one("SELECT name FROM projects WHERE id = ?", (PID,))["name"], winners[0])

    def test_directory_swap_does_not_make_listing_delete_the_project_index(self):
        package = self.package("Replacement")
        projects.create("Original", "https://shared.example.invalid", project_id=PID)
        moved, release = self.context.Event(), self.context.Event()
        started, finished = self.context.Event(), self.context.Event()
        results = self.result_queue()
        importer = self.context.Process(target=pause_import_after_backup, args=(self.workspace, package, moved, release))
        reader = self.context.Process(target=list_projects, args=(self.workspace, started, finished, results))
        processes = [importer]
        importer.start()
        try:
            self.assertTrue(moved.wait(timeout=10))
            reader.start()
            processes.append(reader)
            self.assertTrue(started.wait(timeout=10))
            self.assertFalse(finished.wait(timeout=0.2))
        finally:
            release.set()
            self.finish_processes(processes)
        self.assertEqual(results.get(timeout=5), [(PID, "Replacement")])
        self.assertEqual(db.one("SELECT name FROM projects WHERE id = ?", (PID,))["name"], "Replacement")
        self.assertEqual(db.one("SELECT COUNT(*) n FROM cases WHERE project_id = ?", (PID,))["n"], 1)


if __name__ == "__main__":
    unittest.main()
