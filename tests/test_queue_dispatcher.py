"""An independent dispatcher drains durable asynchronous work after the submitting CLI exits."""
import datetime
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from test_execution_review_fixes import Handler, Site

from autoskill import builder, db, paths, projects, runner
from autoskill.server import jobs, scheduler

ENSURE = "import json; from autoskill.server import jobs; print(json.dumps(jobs.ensure_worker()))"


class DispatcherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sites = [Site(("127.0.0.1", 0), Handler) for _ in range(3)]
        for site in cls.sites:
            site.reset()
            threading.Thread(target=site.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        for site in cls.sites:
            site.release.set()
            site.shutdown()
            site.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": self.tmp.name, "AUTOSKILL_HEADED": "0",
                                                   "AUTOSKILL_QUEUE_IDLE_SECONDS": "0.2", "AUTOSKILL_MAX_PARALLEL": "1"})
        self.environment.start()
        self.dispatcher_pids = set()
        self.children = []
        for site in self.sites:
            site.reset()
        db.init()

    def tearDown(self):
        for site in self.sites:
            site.release.set()
        dispatcher_pids = (self.dispatcher_pids | set(jobs._dispatchers)) - {os.getpid()}
        for pid in dispatcher_pids:
            if runner.process_alive(pid):
                os.kill(pid, signal.SIGTERM)
        for child in [*self.children, *jobs._dispatchers.values()]:
            if child.poll() is None:
                child.terminate()
            child.communicate(timeout=5)
        jobs._dispatchers.clear()
        self.wait(lambda: not any(runner.process_alive(pid) for pid in dispatcher_pids))
        # Terminal runs can still be writing reports; stop every workspace writer before removal.
        executor_pids = {row["pid"] for row in db.rows("SELECT DISTINCT pid FROM runs WHERE pid IS NOT NULL")} - {os.getpid()}
        deadline = time.monotonic() + 10
        while any(runner.process_alive(pid) for pid in executor_pids) and time.monotonic() < deadline:
            time.sleep(0.02)
        for pid in executor_pids:
            if runner.process_alive(pid):
                os.kill(pid, signal.SIGTERM)
        self.wait(lambda: not any(runner.process_alive(pid) for pid in executor_pids))
        self.environment.stop()
        self.tmp.cleanup()

    def queued(self, index):
        pid = "dispatcher-" + str(index)
        projects.create(pid, f"http://127.0.0.1:{self.sites[index].server_port}", project_id=pid)
        case = builder.api_case("TC-API-001", "Queued effect", "POST", "/effect", {"status": 200, "json_equals": {"/ok": True}})
        projects.save_cases(pid, [case])
        return runner.create(pid)

    def wait(self, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        raise AssertionError("Background queue did not reach the expected state")

    def ensure(self):
        result = jobs.ensure_worker()
        self.assertIn(result["status"], ("started", "running"), result)
        self.dispatcher_pids.add(result["pid"])
        return result

    def test_concurrent_cli_submitters_share_worker_and_queue_drains_without_dashboard(self):
        runs = [self.queued(index) for index in range(3)]
        for _ in range(3):
            child = subprocess.Popen([sys.executable, "-c", ENSURE], cwd=paths.APP, env=paths.child_env(),
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.children.append(child)
        replies = []
        for child in self.children:
            output, error = child.communicate(timeout=10)
            self.assertEqual(child.returncode, 0, error.decode(errors="replace"))
            reply = json.loads(output)
            replies.append(reply)
            self.dispatcher_pids.add(reply["pid"])
        self.assertEqual(len({reply["pid"] for reply in replies}), 1)
        self.assertTrue(all(reply["status"] in ("started", "running") for reply in replies))
        self.assertTrue(self.sites[0].entered.wait(10))
        self.assertEqual(len(db.rows("SELECT * FROM runs WHERE status = 'Running'")), 1)
        self.assertEqual(len(db.rows("SELECT * FROM runs WHERE status = 'Queued'")), 2)
        for site in self.sites:
            site.release.set()
        self.wait(lambda: all(db.one("SELECT status FROM runs WHERE id = ?", (run["id"],))["status"] == "Pass" for run in runs))
        self.assertEqual([site.requests for site in self.sites], [1, 1, 1])
        self.assertEqual(len(db.rows("SELECT * FROM results")), 3)
        self.wait(lambda: not runner.process_alive(replies[0]["pid"]))
        self.assertFalse((paths.local() / "queue-worker.json").exists())

    def test_spawn_failure_keeps_original_queued_run_available_for_retry(self):
        run = self.queued(0)
        with patch.object(jobs.subprocess, "Popen", side_effect=OSError("synthetic resource failure")):
            result = jobs.ensure_worker()
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(db.one("SELECT status FROM runs WHERE id = ?", (run["id"],))["status"], "Queued")
        self.sites[0].release.set()
        self.ensure()
        self.wait(lambda: db.one("SELECT status FROM runs WHERE id = ?", (run["id"],))["status"] == "Pass")
        self.assertEqual(self.sites[0].requests, 1)

    def test_cancelled_waiter_does_not_execute_when_a_slot_opens(self):
        first, second = self.queued(0), self.queued(1)
        self.ensure()
        self.assertTrue(self.sites[0].entered.wait(10))
        jobs.cancel(second["id"])
        self.sites[0].release.set()
        self.wait(lambda: db.one("SELECT status FROM runs WHERE id = ?", (first["id"],))["status"] == "Pass")
        self.assertEqual(db.one("SELECT status FROM runs WHERE id = ?", (second["id"],))["status"], "Cancelled")
        self.assertFalse(self.sites[1].entered.is_set())

    def test_a_new_enqueue_restarts_after_idle_shutdown(self):
        first = self.queued(0)
        self.sites[0].release.set()
        previous = self.ensure()["pid"]
        self.wait(lambda: db.one("SELECT status FROM runs WHERE id = ?", (first["id"],))["status"] == "Pass")
        self.wait(lambda: not runner.process_alive(previous))
        second = self.queued(1)
        self.sites[1].release.set()
        current = self.ensure()["pid"]
        self.assertNotEqual(previous, current)
        self.wait(lambda: db.one("SELECT status FROM runs WHERE id = ?", (second["id"],))["status"] == "Pass")
        self.assertEqual([site.requests for site in self.sites[:2]], [1, 1])

    def test_replacement_dispatcher_respects_a_surviving_executor_after_worker_crash(self):
        first, second = self.queued(0), self.queued(1)
        old_pid = self.ensure()["pid"]
        self.assertTrue(self.sites[0].entered.wait(10))
        os.kill(old_pid, signal.SIGTERM)
        self.wait(lambda: not runner.process_alive(old_pid))
        new_pid = self.ensure()["pid"]
        self.assertNotEqual(old_pid, new_pid)
        self.assertFalse(self.sites[1].entered.is_set())
        self.sites[0].release.set()
        self.sites[1].release.set()
        self.wait(lambda: all(db.one("SELECT status FROM runs WHERE id = ?", (run["id"],))["status"] == "Pass" for run in (first, second)))
        self.assertEqual([site.requests for site in self.sites[:2]], [1, 1])

    def test_future_schedule_fires_after_submitter_exits_and_idle_timeout_passes(self):
        setup = self.queued(0)
        jobs.cancel(setup["id"])
        due = datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=1.4)
        task = scheduler.create({"project_id": setup["project_id"], "title": "Future execution", "case_ids": ["TC-API-001"],
                                 "rule": {"kind": "at", "at": due.isoformat()}})
        child = subprocess.Popen([sys.executable, "-c", ENSURE], cwd=paths.APP, env=paths.child_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.children.append(child)
        output, error = child.communicate(timeout=10)
        self.assertEqual(child.returncode, 0, error.decode(errors="replace"))
        dispatcher = json.loads(output)
        self.assertIn(dispatcher["status"], ("started", "running"))
        self.dispatcher_pids.add(dispatcher["pid"])
        self.sites[0].release.set()
        self.wait(lambda: scheduler.get(task["id"])["last_status"] == "Pass")
        self.assertEqual(self.sites[0].requests, 1)
        self.assertEqual(len(db.rows("SELECT * FROM schedule_occurrences WHERE task_id = ?", (task["id"],))), 1)
        self.wait(lambda: not runner.process_alive(dispatcher["pid"]))

    def test_enabled_future_schedule_keeps_worker_until_paused(self):
        setup = self.queued(0)
        jobs.cancel(setup["id"])
        task = scheduler.create({"project_id": setup["project_id"], "title": "Recurring execution", "case_ids": ["TC-API-001"],
                                 "rule": {"kind": "interval", "minutes": 1}})
        dispatcher = self.ensure()
        threading.Event().wait(0.7)
        self.assertTrue(runner.process_alive(dispatcher["pid"]))
        self.assertEqual(self.sites[0].requests, 0)
        scheduler.set_enabled(task["id"], False)
        self.wait(lambda: not runner.process_alive(dispatcher["pid"]))

    def test_future_maintenance_creates_one_durable_host_delivery(self):
        setup = self.queued(0)
        jobs.cancel(setup["id"])
        due = datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=1.2)
        task = scheduler.create({"project_id": setup["project_id"], "title": "Future maintenance", "kind": "maintenance",
                                 "workflow_id": "synthetic-workflow", "rule": {"kind": "at", "at": due.isoformat()}})
        self.ensure()
        self.wait(lambda: bool(db.one("SELECT id FROM host_outbox WHERE task_id = ?", (task["id"],))))
        self.assertEqual(len(db.rows("SELECT * FROM host_outbox WHERE task_id = ?", (task["id"],))), 1)
        self.assertEqual(db.one("SELECT state FROM host_outbox WHERE task_id = ?", (task["id"],))["state"], "pending")
        self.assertEqual(self.sites[0].requests, 0)


if __name__ == "__main__":
    unittest.main()
