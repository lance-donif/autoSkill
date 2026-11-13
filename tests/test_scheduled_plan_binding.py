"""Future scheduled runs retain each caller's explicit case sheet across shared workers."""
import datetime
import json
import os
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

from autoskill import builder, data_refs, db, plan, projects, runner, task_context
from autoskill.server import jobs, scheduler


class ScheduledPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": self.tmp.name, "AUTOSKILL_HEADED": "0",
                                                   "AUTOSKILL_QUEUE_IDLE_SECONDS": "0.1"})
        self.environment.start()
        self.site = Site(("127.0.0.1", 0), Handler)
        self.site.reset()
        self.site.release.set()
        threading.Thread(target=self.site.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.worker = None
        self.pid = projects.create("Scheduled sheets", f"http://127.0.0.1:{self.site.server_port}", project_id="sheets")["id"]
        self.case = builder.api_case("TC-API-001", "Business result", "POST", "/effect", {"status": 200, "json_equals": {"/ok": True}})
        projects.save_cases(self.pid, [self.case])

    def tearDown(self):
        db.execute("UPDATE tasks SET enabled = 0")
        if self.worker is not None:
            try:
                self.worker.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.worker.terminate()
                self.worker.wait(timeout=5)
            jobs._dispatchers.pop(self.worker.pid, None)
        self.site.shutdown()
        self.site.server_close()
        self.environment.stop()
        self.tmp.cleanup()

    def sheet(self, folder):
        source = self.root / "design.json"
        source.write_text(json.dumps([{"id": "PLAN-001", "title": "Requirement"}]))
        with patch.dict(os.environ, {"AUTOSKILL_CASE_SKILL": str(folder)}):
            plan.write(self.pid, str(source))
            plan.link("PLAN-001", self.pid, self.case["id"], 1)

    def test_second_schedule_keeps_its_sheet_in_a_worker_started_by_first_session(self):
        first, second = self.root / "session-a-plan", self.root / "session-b-plan"
        self.sheet(first)
        self.sheet(second)
        with patch.dict(os.environ, {"AUTOSKILL_CASE_SKILL": str(first)}), task_context.use_session("session-a"):
            holding = scheduler.create({"project_id": self.pid, "title": "Keep first worker", "rule": {"kind": "interval", "minutes": 1}})
            delivery = jobs.ensure_worker()
            self.assertIn(delivery["status"], ("started", "running"))
            self.worker = jobs._dispatchers[delivery["pid"]]
        due = datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=1)
        with patch.dict(os.environ, {"AUTOSKILL_CASE_SKILL": str(second)}), task_context.use_session("session-b"):
            task = scheduler.create({"project_id": self.pid, "title": "Second session", "case_ids": [self.case["id"]],
                                     "rule": {"kind": "at", "at": due.isoformat()}})
        self.assertEqual((holding["plan_path"], task["plan_path"]), (str(first), str(second)))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and scheduler.get(task["id"])["last_status"] != "Pass":
            time.sleep(0.02)
        final_task = scheduler.get(task["id"])
        self.assertEqual(final_task["last_status"], "Pass")
        run = db.one("SELECT * FROM runs WHERE id = ?", (final_task["last_run_id"],))
        self.assertEqual((data_refs.resolve(run["plan_path"]), run["session_id"]), (second, "session-b"))
        self.assertFalse(data_refs.is_absolute(run["plan_path"]))
        self.assertEqual(runner.public_run(run)["plan_path"], str(second))
        self.worker.poll()
        with patch.dict(os.environ, {"AUTOSKILL_CASE_SKILL": str(second)}):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and plan.show()["summary"]["通过"] != 1:
                time.sleep(0.02)
            self.assertEqual(plan.show()["summary"]["通过"], 1)
        with patch.dict(os.environ, {"AUTOSKILL_CASE_SKILL": str(first)}):
            self.assertEqual(plan.show()["summary"]["通过"], 0)
        self.assertEqual(self.site.requests, 1)

    def test_prepare_explicit_plan_path_does_not_change_the_process_environment(self):
        with patch.dict(os.environ, {"AUTOSKILL_CASE_SKILL": str(self.root / "environment-plan")}):
            prepared = runner.prepare(self.pid, plan_path=self.root / "requested-plan")
            self.assertEqual(prepared["plan_path"], str(self.root / "requested-plan"))
            self.assertEqual(os.environ["AUTOSKILL_CASE_SKILL"], str(self.root / "environment-plan"))


if __name__ == "__main__":
    unittest.main()
