"""Independent processes merge credentials and saved login state without torn JSON files."""
import json
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

from autoskill import paths, secrets, sessions


def write_shared(kind, workspace, number, rounds, barrier):
    os.environ["AUTOSKILL_WORKSPACE"] = workspace
    for iteration in range(rounds):
        barrier.wait(timeout=15)
        if kind == "credentials":
            secrets.set_secret(f"WRITER_{number}_{iteration}", f"synthetic-{number}-{iteration}")
        elif number == 0:
            sessions.save("shared", ui={"cookies": [], "origins": [], "iteration": iteration})
        else:
            sessions.save("shared", variables={f"writer_{number}_{iteration}": iteration})


def replace_json_stores(workspace, started, finished):
    os.environ["AUTOSKILL_WORKSPACE"] = workspace
    if not started.wait(timeout=15):
        raise RuntimeError("JSON reader did not start")
    try:
        for iteration in range(24):
            value = f"synthetic-{iteration}-" + "x" * (128 * 1024)
            secrets.set_secret("ATOMIC_FIXTURE", value)
            sessions.save("shared", variables={"atomic_fixture": value})
    finally:
        finished.set()


def observe_json_stores(workspace, started, finished, results):
    os.environ["AUTOSKILL_WORKSPACE"] = workspace
    files = [paths.local() / "credentials.json", paths.local() / "sessions/shared.json"]
    failures, reads = [], 0
    started.set()
    while not finished.is_set():
        for file in files:
            try:
                json.loads(file.read_text(encoding="utf-8"))
            except (ValueError, OSError) as error:
                failures.append(type(error).__name__)
            reads += 1
    results.put((reads, failures))


class SharedLocalWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": self.tmp.name})
        environment.start()
        self.addCleanup(environment.stop)
        self.context = multiprocessing.get_context("spawn")

    def run_processes(self, processes):
        try:
            for process in processes:
                process.start()
            deadline = time.monotonic() + 30
            for process in processes:
                process.join(timeout=max(0, deadline - time.monotonic()))
            self.assertEqual([process.exitcode for process in processes], [0] * len(processes))
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)

    def concurrent_writers(self, kind, count=5, rounds=10):
        barrier = self.context.Barrier(count)
        self.run_processes([self.context.Process(target=write_shared, args=(kind, self.tmp.name, number, rounds, barrier))
                            for number in range(count)])

    def test_different_credentials_survive_simultaneous_process_updates(self):
        secrets.set_secret("EXISTING_FIXTURE", "synthetic-existing")
        self.concurrent_writers("credentials")
        expected = {f"WRITER_{number}_{iteration}": f"synthetic-{number}-{iteration}"
                    for number in range(5) for iteration in range(10)}
        expected["EXISTING_FIXTURE"] = "synthetic-existing"
        self.assertEqual(secrets._load(), expected)

    def test_browser_state_and_all_variable_updates_are_merged(self):
        sessions.save("shared", variables={"existing": "synthetic-existing"})
        self.concurrent_writers("sessions")
        saved = sessions.load("shared")
        expected = {f"writer_{number}_{iteration}": iteration for number in range(1, 5) for iteration in range(10)}
        expected["existing"] = "synthetic-existing"
        self.assertEqual(saved["variables"], expected)
        self.assertEqual(saved["ui"], {"cookies": [], "origins": [], "iteration": 9})

    def test_unlocked_readers_only_observe_complete_json_during_replacement(self):
        secrets.set_secret("ATOMIC_FIXTURE", "synthetic-initial")
        sessions.save("shared", variables={"atomic_fixture": "synthetic-initial"})
        started, finished = self.context.Event(), self.context.Event()
        results = self.context.Queue()
        try:
            self.run_processes([
                self.context.Process(target=observe_json_stores, args=(self.tmp.name, started, finished, results)),
                self.context.Process(target=replace_json_stores, args=(self.tmp.name, started, finished)),
            ])
            reads, failures = results.get(timeout=5)
            self.assertGreater(reads, 0)
            self.assertEqual(failures, [])
        finally:
            results.close()
            results.join_thread()

    def test_failed_atomic_replacement_keeps_previous_values(self):
        secrets.set_secret("EXISTING_FIXTURE", "synthetic-existing")
        sessions.save("shared", variables={"existing": "synthetic-existing"})
        original_session = sessions.load("shared")
        with patch.object(Path, "replace", side_effect=OSError("fixture replacement failure")):
            with self.assertRaisesRegex(OSError, "fixture replacement"):
                secrets.set_secret("NEW_FIXTURE", "synthetic-new")
            with self.assertRaisesRegex(OSError, "fixture replacement"):
                sessions.save("shared", variables={"new": "synthetic-new"})
        self.assertEqual(secrets.names(), ["EXISTING_FIXTURE"])
        self.assertEqual(sessions.load("shared"), original_session)


if __name__ == "__main__":
    unittest.main()
