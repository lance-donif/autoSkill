"""Regression checks for resident locks, native bindings, and dashboard supervision."""
from __future__ import annotations

import ctypes
import gc
import os
import socket
import subprocess
import sys
import tempfile
import time
import tracemalloc
import unittest
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/autoskill/app"))

from autoskill import db, locking, maintenance, paths, runner, runtime_identity, secrets, task_state
from autoskill.server.jobs import Worker


class Passes:
    """Give a real worker a fixed number of passes without waiting between them."""

    def __init__(self, count):
        self.remaining = count

    def is_set(self):
        return self.remaining <= 0

    def wait(self, _seconds):
        self.remaining -= 1
        return self.is_set()


def windows_handles():
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.GetProcessHandleCount.argtypes = (wintypes.HANDLE, wintypes.LPDWORD)
    kernel.GetProcessHandleCount.restype = wintypes.BOOL
    count = wintypes.DWORD()
    if not kernel.GetProcessHandleCount(kernel.GetCurrentProcess(), ctypes.byref(count)):
        raise ctypes.WinError(ctypes.get_last_error())
    return count.value


class RuntimeResources(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory(prefix="autoskill-resources-"))
        self.enterContext(patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": folder, "AUTOSKILL_SYNC_DIR": ""}))
        self.root = paths.workspace()
        db.init()

    def assert_stable(self, operation, rounds=400, allowance=128 * 1024):
        for _ in range(10):
            operation()
        gc.collect()
        was_tracing = tracemalloc.is_tracing()
        if not was_tracing:
            tracemalloc.start()
        try:
            pointer_types = len(ctypes._pointer_type_cache)
            # CPython's intern table may allocate a larger slab early in a traced run.
            for _ in range(rounds):
                operation()
            gc.collect()
            before = tracemalloc.get_traced_memory()[0]
            handles = windows_handles() if os.name == "nt" else None
            for _ in range(rounds):
                operation()
            gc.collect()
            self.assertEqual(len(ctypes._pointer_type_cache), pointer_types)
            self.assertLess(tracemalloc.get_traced_memory()[0] - before, allowance)
            if handles is not None:
                self.assertLessEqual(windows_handles(), handles + 2)
        finally:
            if not was_tracing:
                tracemalloc.stop()

    def test_shared_access_and_sqlite_connections_do_not_retain_resources(self):
        def query():
            with maintenance.access(self.root), db.connect() as conn:
                self.assertEqual(conn.execute("SELECT 1").fetchone()[0], 1)

        self.assert_stable(query, rounds=1000)

    @unittest.skipUnless(os.name == "nt", "requires real DPAPI and Windows process handles")
    def test_legacy_dpapi_and_process_queries_use_stable_bindings(self):
        from autoskill.engines.process_job import ProcessJob

        def operation():
            self.assertEqual(secrets.unseal(secrets.seal("示例凭据")), "示例凭据")
            self.assertTrue(runner.process_alive(os.getpid()))
            self.assertIsNotNone(runtime_identity.process_birth(os.getpid()))
            job = ProcessJob()
            job.close()

        self.assert_stable(operation, rounds=300)

    def test_worker_does_not_retain_one_type_or_handle_per_pass(self):
        def operation():
            worker = Worker()
            worker.stop = Passes(10)
            worker.loop()

        self.assert_stable(operation, rounds=45, allowance=256 * 1024)

    def test_queue_batch_reuses_sequential_transactions_and_holds_migration_gate(self):
        def try_migration():
            with self.assertRaises(ValueError), maintenance.exclusive(self.root):
                self.fail("Exclusive migration entered an active database batch")

        with ThreadPoolExecutor(1) as thread, db.batch():
            with db.connect() as first:
                first.execute("CREATE TABLE batch_probe (value)")
                first.execute("INSERT INTO batch_probe VALUES (1)")
            thread.submit(try_migration).result(timeout=5)
            with self.assertRaises(RuntimeError), db.connect() as reused:
                self.assertIs(reused, first)
                with db.connect() as nested:
                    self.assertIsNot(nested, first)
                    self.assertEqual(nested.execute("SELECT value FROM batch_probe").fetchone()[0], 1)
                reused.execute("INSERT INTO batch_probe VALUES (2)")
                raise RuntimeError("rollback only this operation")
            self.assertEqual(db.rows("SELECT value FROM batch_probe"), [{"value": 1}])
        with maintenance.exclusive(self.root):
            pass

    def test_dashboard_idle_does_not_import_feature_dependencies(self):
        roots = ["onnxruntime", "numpy", "PIL", "playwright", "selenium", "locust", "openpyxl", "jsonschema", "cryptography", "httpx"]
        check = ("import sys; from autoskill.server.app import create_app; create_app(start_worker=False); "
                 f"loaded = set({roots!r}) & set(sys.modules); assert not loaded, sorted(loaded)")
        subprocess.run([sys.executable, "-c", check], env=paths.child_env(), cwd=paths.APP, check=True, timeout=15)

    def test_inactive_named_lock_objects_are_reclaimed(self):
        baseline = len(locking._threads)
        for index in range(120):
            with locking.named_lock("resources-test", str(index)), locking.named_lock("resources-test", str(index)):
                self.assertEqual(len(locking._threads), baseline + 1)
        self.assertEqual(len(locking._threads), baseline)

    def test_saturated_watch_set_is_bounded_and_still_invalidates(self):
        with patch.object(task_state, "_watched", OrderedDict()), patch.object(task_state, "_WATCHED_FILES", 3):
            for index in range(100):
                task_state._watch(self.root / str(index))
            self.assertEqual(len(task_state._watched), 1)
            self.assertIsNone(task_state._watched[self.root])
            with patch.object(task_state.time, "monotonic_ns", side_effect=[0, 500_000_000]):
                self.assertNotEqual(task_state.revision(), task_state.revision())

    def test_dashboard_shuts_down_when_its_host_pipe_closes(self):
        with socket.socket() as port_probe:
            port_probe.bind(("127.0.0.1", 0))
            port = port_probe.getsockname()[1]
        env = {**paths.child_env(), "AUTOSKILL_HOST_PIPE": "1", "PYTHONDONTWRITEBYTECODE": "1"}
        with subprocess.Popen([sys.executable, "-m", "autoskill", "dashboard", "--port", str(port)], cwd=paths.APP, env=env,
                              stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as child:
            try:
                deadline = time.monotonic() + 15
                while True:
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                            break
                    except OSError:
                        if child.poll() is not None or time.monotonic() >= deadline:
                            self.fail("Dashboard did not listen before its host closed the pipe")
                        time.sleep(0.05)
                child.stdin.close()
                self.assertEqual(child.wait(timeout=10), 0)
                self.assertFalse(paths.service_file().exists())
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)
