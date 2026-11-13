"""Scheduled browser choices survive persistence and reach a real Chromium launch."""
import contextlib
import datetime
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill import builder, db, paths, projects, runner
from autoskill.replay.playwright_driver import PlaywrightDriver
from autoskill.server import scheduler


class SiteHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/slow":
            time.sleep(2)
        body = b'<main data-testid="state">ready</main>'
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


class ScheduledBrowserModeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        env = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": temp.name, "AUTOSKILL_HEADED": "1", "DISPLAY": ":99",
                                      "AUTOSKILL_BRIDGE": "off", "PYTHONPATH": str(ROOT / "skills/autoskill/app")})
        env.start()
        self.addCleanup(env.stop)
        self.site = ThreadingHTTPServer(("127.0.0.1", 0), SiteHandler)
        threading.Thread(target=self.site.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.addCleanup(self.site.server_close)
        self.addCleanup(self.site.shutdown)
        self.pid = projects.create("Scheduled browser", f"http://127.0.0.1:{self.site.server_port}")["id"]
        case = builder.ui_case("TC-UI-001", "Browser mode fixture", {
            "start_url": "/", "actions": [],
            "assertions": [{"assertion": "text", "locators": [{"by": "testid", "value": "state"}], "expected": "ready"}]})
        projects.save_cases(self.pid, [case])
        self.spec = {"project_id": self.pid, "title": "Hidden scheduled browser", "case_ids": [case["id"]],
                     "headed": False, "rule": {"kind": "interval", "minutes": 5}}

    def launch_and_check(self, run):
        if not paths.chrome():
            self.skipTest("Real scheduled browser mode acceptance requires Chromium")
        commands = []

        class ObservedDriver(PlaywrightDriver):
            def __init__(self, config, headless=True):
                super().__init__(config, headless=headless)
                page = self.browser.new_page()
                page.goto("chrome://version")
                commands.append(page.locator("#command_line").text_content().split())
                page.close()

        with patch("autoskill.replay.playwright_driver.PlaywrightDriver", ObservedDriver):
            result = runner.execute(run["id"])
        self.assertEqual(result["status"], "Pass", db.rows("SELECT reason, detail FROM results WHERE run_id = ?", (run["id"],)))
        self.assertEqual(len(commands), 1)
        self.assertTrue(any(argument.startswith("--headless") for argument in commands[0]), commands[0])
        self.assertNotIn("--start-maximized", commands[0])

    def test_immediate_run_keeps_explicit_hidden_choice_after_edit_and_fresh_process(self):
        paths.save_settings(dashboard={"headed": True})
        task = scheduler.create(self.spec)
        updated = scheduler.update(task["id"], {"title": "Renamed", "expected_revision": task["revision"]})
        self.assertIs(updated["headed"], False)
        code = "from autoskill.server import scheduler; import json,sys; print(json.dumps(scheduler.run_now(sys.argv[1])))"
        completed = subprocess.run([sys.executable, "-c", code, task["id"]], capture_output=True, text=True, check=True)
        run = json.loads(completed.stdout)
        self.assertEqual(run["headed"], 0)
        self.launch_and_check(run)

    def test_due_task_keeps_hidden_choice_after_database_restart(self):
        due = datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=30)
        task = scheduler.create({**self.spec, "rule": {"kind": "at", "at": due.isoformat()}})
        db.init()
        runs = scheduler.tick(due + datetime.timedelta(seconds=1))
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["headed"], 0)
        self.assertFalse(scheduler.get(task["id"])["enabled"])
        self.launch_and_check(runs[0])

    def test_legacy_tasks_follow_saved_preference_and_unrelated_edits_do_not_pin_it(self):
        paths.save_settings(dashboard={"headed": False})
        spec = {key: value for key, value in self.spec.items() if key != "headed"}
        task = scheduler.create(spec)
        self.assertIsNone(task["headed"])
        self.assertIs(task["effective_headed"], False)
        updated = scheduler.update(task["id"], {"title": "Legacy renamed"})
        self.assertIsNone(updated["headed"])
        self.assertEqual(scheduler._prepare(updated, "manual")["headed"], 0)
        paths.save_settings(dashboard={"headed": True})
        self.assertTrue(scheduler.get(task["id"])["effective_headed"])
        self.assertEqual(scheduler._prepare(scheduler.get(task["id"]), "manual")["headed"], 1)

    def test_schema_adoption_preserves_old_task_and_rejects_non_boolean_choices(self):
        task = scheduler.create(self.spec)
        db.execute("ALTER TABLE tasks DROP COLUMN headed")
        db.init()
        paths.save_settings(dashboard={"headed": False})
        migrated = scheduler.get(task["id"])
        self.assertIsNone(migrated["headed"])
        self.assertFalse(migrated["effective_headed"])
        with self.assertRaisesRegex(ValueError, "布尔值"):
            scheduler.update(task["id"], {"headed": "false"})

    @unittest.skipUnless(sys.platform == "linux", "The real worker process observation uses Linux /proc")
    def test_actual_worker_child_launches_headless_chromium_for_hidden_task(self):
        if not paths.chrome():
            self.skipTest("Real worker browser acceptance requires Chromium")
        case = projects.case(self.pid, "TC-UI-001")
        case["extensions"]["automation"]["start_url"] = "/slow"
        projects.save_cases(self.pid, [case])
        paths.save_settings(dashboard={"headed": True})
        task = scheduler.create(self.spec)
        run = scheduler.run_now(task["id"])
        self.assertEqual(run["headed"], 0)
        worker_pid = runner.spawn(run["id"])
        self.assertIsNotNone(worker_pid)
        child = runner._children[worker_pid]
        self.addCleanup(self.finish_worker, child, run["id"])
        commands = []
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            commands.extend(self.browser_descendants(worker_pid))
            current = db.one("SELECT * FROM runs WHERE id = ?", (run["id"],))
            if current["status"] in db.FINISHED:
                break
            time.sleep(0.03)
        self.assertEqual(current["status"], "Pass", current)
        self.assertTrue(commands, "No Chromium command line was observed under the actual runner worker")
        self.assertTrue(all(any(argument.startswith("--headless") for argument in command) for command in commands), commands)
        self.assertTrue(all("--start-maximized" not in command for command in commands), commands)
        self.assertTrue(all(row["status"] == "Pass" for row in db.rows("SELECT status FROM results WHERE run_id = ?", (run["id"],))))

    @staticmethod
    def browser_descendants(worker_pid):
        parents = {}
        commands = {}
        for directory in Path("/proc").iterdir():
            if not directory.name.isdigit():
                continue
            with contextlib.suppress(OSError, ValueError, IndexError):
                fields = (directory / "stat").read_text().rsplit(")", 1)[1].split()
                parents[int(directory.name)] = int(fields[1])
                commands[int(directory.name)] = (directory / "cmdline").read_bytes().decode(errors="replace").split("\0")
        descendants = {worker_pid}
        while True:
            expanded = descendants | {pid for pid, parent in parents.items() if parent in descendants}
            if descendants == expanded:
                break
            descendants = expanded
        return [command for pid, command in commands.items() if pid in descendants and command
                and Path(command[0]).name in ("chrome", "chromium", "chromium-browser")
                and not any(argument.startswith("--type=") for argument in command)]

    @staticmethod
    def finish_worker(child, run_id):
        if child.poll() is None:
            db.execute("UPDATE runs SET cancel = 1 WHERE id = ?", (run_id,))
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.terminate()
                child.wait(timeout=5)
        runner.reap_children()


if __name__ == "__main__":
    unittest.main()
