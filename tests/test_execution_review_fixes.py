"""Real process regressions for run ownership, concurrent projects and durable queue recovery."""
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

from autoskill import builder, db, deletion, paths, projects, runner, runtime_identity, sessions, task_context
from autoskill.server import jobs

SESSION_EXECUTOR = """
import sys
from autoskill import runner, task_context

class CheckedSession(runner.Session):
    def __init__(self, run, force_login=False):
        if task_context.session_id() != (run['session_id'] or None):
            raise ValueError('任务会话没有恢复')
        super().__init__(run, force_login)

runner.Session = CheckedSession
result = runner.execute(sys.argv[1])
raise SystemExit(0 if result['status'] == 'Pass' else 2)
"""

RESERVED_EXECUTOR = """
import json
import sys
import time
from pathlib import Path
from autoskill import db, runner

run_id, role, signal, gate, output = sys.argv[1:]
signal, gate, output = Path(signal), Path(gate), Path(output)
if role == 'join':
    claim = db.claim_run
    def observed(*args, **kwargs):
        acquired = claim(*args, **kwargs)
        if not acquired:
            signal.touch()
        return acquired
    db.claim_run = observed
else:
    signal.touch()
    deadline = time.monotonic() + 10
    while not gate.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError('Test did not release the reserved executor')
        time.sleep(0.01)
result = runner.execute(run_id, claim_token=None if role == 'join' else role)
output.write_text(json.dumps(result))
"""


class Site(ThreadingHTTPServer):
    daemon_threads = True

    def reset(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.requests = 0
        self.lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        with self.server.lock:
            self.server.requests += 1
        self.server.entered.set()
        self.server.release.wait(15)
        data = b'{"ok":true,"test_id":"new"}'
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            return  # The crash recovery tests intentionally kill clients waiting for a response.


class ExecutionReviewTests(unittest.TestCase):
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
                                                   "AUTOSKILL_MAX_PARALLEL": "2"})
        self.environment.start()
        self.children = []
        for site in self.sites:
            site.reset()
        db.init()

    def tearDown(self):
        for site in self.sites:
            site.release.set()
        for child in [*self.children, *runner._children.values()]:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)
        runner.reap_children()
        self.environment.stop()
        self.tmp.cleanup()

    def queued(self, index=0, *, force_login=False):
        project_id = f"project-{index}"
        if not db.one("SELECT id FROM projects WHERE id = ?", (project_id,)):
            projects.create(project_id, f"http://127.0.0.1:{self.sites[index].server_port}", project_id=project_id)
            case = builder.api_case("TC-API-001", "合成请求", "POST", "/effect", {"status": 200, "json_equals": {"/ok": True}})
            projects.save_cases(project_id, [case])
        return runner.create(project_id, force_login=force_login)

    def launch_executor(self, run_id, token=None, *, check_session=False):
        command = ([sys.executable, "-c", SESSION_EXECUTOR, run_id] if check_session
                   else [sys.executable, "-m", "autoskill.runner", run_id])
        if token is not None:
            command += ["--claim-token", token]
        env = paths.child_env({"AUTOSKILL_SESSION": "unrelated-parent-session"} if check_session else None)
        child = subprocess.Popen(command, cwd=paths.APP, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.children.append(child)
        return child

    def assert_exited(self, child):
        stdout, stderr = child.communicate(timeout=10)
        self.assertEqual(child.returncode, 0, (stdout + stderr).decode(errors="replace"))

    def final(self, run_id):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            jobs.sweep()
            result = db.one("SELECT * FROM runs WHERE id = ?", (run_id,))
            if result["status"] in db.FINISHED:
                return result
            time.sleep(0.02)
        raise AssertionError("Run did not finish")

    def test_two_direct_processes_execute_one_run_once(self):
        run = self.queued()
        first = self.launch_executor(run["id"])
        second = self.launch_executor(run["id"])
        self.assertTrue(self.sites[0].entered.wait(10))
        self.sites[0].release.set()
        self.assert_exited(first)
        self.assert_exited(second)
        self.assertEqual(self.sites[0].requests, 1)
        result = runner.execute(run["id"])
        self.assertEqual((result["status"], result["done"], result["total"]), ("Pass", 1, 1))
        self.assertEqual(len(db.rows("SELECT * FROM results")), 1)

    def test_worker_reservation_and_cli_process_share_one_run(self):
        run = self.queued()
        jobs.start_queued()
        self.assertTrue(self.sites[0].entered.wait(10))
        contender = self.launch_executor(run["id"])
        self.sites[0].release.set()
        self.assert_exited(contender)
        self.assertEqual(self.final(run["id"])["status"], "Pass")
        self.assertEqual(self.sites[0].requests, 1)
        self.assertEqual(len(db.rows("SELECT * FROM results")), 1)

    def reserved_join(self, run_id):
        """Start the joiner before the designated executor can enter its execution guard."""
        token = "designated-reservation"
        self.assertTrue(db.claim_run(run_id, os.getpid(), 2, token))
        root = Path(self.tmp.name)
        gate = root / "owner-release"
        owner_signal, join_signal = root / "owner-ready", root / "join-attempted"
        owner_output, join_output = root / "owner-result.json", root / "join-result.json"
        def launch(role, signal, output):
            child = subprocess.Popen([sys.executable, "-c", RESERVED_EXECUTOR, run_id, role, str(signal), str(gate), str(output)],
                                     cwd=paths.APP, env=paths.child_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.children.append(child)
            return child
        owner = launch(token, owner_signal, owner_output)
        self.wait_for_file(owner_signal)
        with db.connect() as connection:
            changed = connection.execute("UPDATE runs SET pid = ?, owner_identity = ? WHERE id = ? AND claim_token = ?",
                                         (owner.pid, json.dumps(runtime_identity.current(owner.pid)), run_id, token)).rowcount
        self.assertEqual(changed, 1)
        joiner = launch("join", join_signal, join_output)
        self.wait_for_file(join_signal)
        self.assertFalse(owner_output.exists())
        self.assertFalse(join_output.exists())
        return owner, joiner, gate, owner_output, join_output

    def wait_for_file(self, file):
        deadline = time.monotonic() + 5
        while not file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(file.exists(), "The child did not reach its controlled reservation barrier")

    def test_direct_join_releases_guard_before_waiting_for_designated_executor(self):
        run = self.queued()
        owner, joiner, gate, owner_output, join_output = self.reserved_join(run["id"])
        gate.touch()
        self.assertTrue(self.sites[0].entered.wait(5), "A joining process must not hold the guard needed by the designated executor")
        active = db.one("SELECT pid,claim_token FROM runs WHERE id = ?", (run["id"],))
        self.assertEqual((active["pid"], active["claim_token"]), (owner.pid, ""))
        with deletion._try_execution(run["id"]) as acquired:
            self.assertFalse(acquired)
        self.assertFalse(join_output.exists())
        self.assertEqual(self.sites[0].requests, 1)
        self.sites[0].release.set()
        self.assert_exited(owner)
        self.assert_exited(joiner)
        results = [json.loads(file.read_text()) for file in (owner_output, join_output)]
        self.assertEqual([(row["status"], row["done"]) for row in results], [("Pass", 1), ("Pass", 1)])
        self.assertEqual(self.sites[0].requests, 1)
        self.assertEqual(len(db.rows("SELECT * FROM results WHERE run_id = ?", (run["id"],))), 1)

    def test_direct_join_of_cancelled_reservation_does_not_execute_or_block_owner(self):
        run = self.queued()
        owner, joiner, gate, owner_output, join_output = self.reserved_join(run["id"])
        jobs.cancel(run["id"])
        gate.touch()
        self.assert_exited(owner)
        self.assert_exited(joiner)
        results = [json.loads(file.read_text()) for file in (owner_output, join_output)]
        self.assertEqual([row["status"] for row in results], ["Cancelled", "Cancelled"])
        self.assertEqual(self.sites[0].requests, 0)
        self.assertEqual(db.rows("SELECT * FROM results WHERE run_id = ?", (run["id"],)), [])

    def test_reservation_token_is_consumed_once_and_rejects_other_children(self):
        run = self.queued()
        self.assertTrue(db.claim_run(run["id"], os.getpid(), 2, "reservation"))
        self.assertEqual(runner.execute(run["id"], claim_token="wrong")["status"], "Running")
        self.assertEqual(self.sites[0].requests, 0)
        first = self.launch_executor(run["id"], "reservation")
        second = self.launch_executor(run["id"], "reservation")
        self.assertTrue(self.sites[0].entered.wait(10))
        self.sites[0].release.set()
        self.assert_exited(first)
        self.assert_exited(second)
        self.assertEqual(self.sites[0].requests, 1)
        self.assertEqual(self.final(run["id"])["status"], "Pass")

    def test_independent_projects_actually_execute_concurrently(self):
        runs = [self.queued(index) for index in range(2)]
        jobs.start_queued()
        for site in self.sites[:2]:
            self.assertTrue(site.entered.wait(10), "Both projects must enter HTTP before either response is released")
        active = db.rows("SELECT pid FROM runs WHERE status = 'Running'")
        self.assertEqual(len({row["pid"] for row in active}), 2)
        for site in self.sites[:2]:
            site.release.set()
        self.assertEqual([self.final(run["id"])["status"] for run in runs], ["Pass", "Pass"])

    def test_dispatch_limits_claims_and_keeps_the_oldest_eligible_replay(self):
        first, waiting, last = [self.queued() for _ in range(3)]
        other = self.queued(1)
        with patch.object(db, "claim_run", wraps=db.claim_run) as claim:
            jobs.start_queued()
            self.assertTrue(all(site.entered.wait(10) for site in self.sites[:2]))
            jobs.start_queued()
            self.assertEqual([call.args[0] for call in claim.call_args_list], [first["id"], other["id"]])
            self.sites[0].release.set()
            self.assertEqual(self.final(first["id"])["status"], "Pass")
            jobs.start_queued()
            self.assertEqual(claim.call_args_list[-1].args[0], waiting["id"])
            self.assertEqual(self.final(waiting["id"])["status"], "Pass")
            self.assertEqual(db.one("SELECT status FROM runs WHERE id = ?", (last["id"],))["status"], "Queued")
            self.sites[1].release.set()
            self.assertEqual(self.final(other["id"])["status"], "Pass")

    def test_resource_budget_can_be_increased_for_three_projects(self):
        os.environ["AUTOSKILL_MAX_PARALLEL"] = "3"
        runs = [self.queued(index) for index in range(3)]
        jobs.start_queued()
        for site in self.sites:
            self.assertTrue(site.entered.wait(10))
        self.assertEqual(len(db.rows("SELECT * FROM runs WHERE status = 'Running'")), 3)
        for site in self.sites:
            site.release.set()
        self.assertEqual([self.final(run["id"])["status"] for run in runs], ["Pass"] * 3)

    def test_two_task_sessions_restore_their_identity_and_execute_concurrently(self):
        with task_context.use_session("任务甲"):
            first = self.queued(0)
        with task_context.use_session("任务乙"):
            second = self.queued(1)
        self.assertEqual([first["session_id"], second["session_id"]], ["任务甲", "任务乙"])
        children = [self.launch_executor(run["id"], check_session=True) for run in (first, second)]
        for site in self.sites[:2]:
            self.assertTrue(site.entered.wait(10), "Both task sessions must enter HTTP before either response is released")
        for site in self.sites[:2]:
            site.release.set()
        for child in children:
            self.assert_exited(child)
        self.assertEqual([self.final(run["id"])["status"] for run in (first, second)], ["Pass", "Pass"])

    def test_default_run_does_not_inherit_worker_session(self):
        with task_context.use_session(None):
            run = self.queued()
        self.assertEqual(run["session_id"], "")
        self.sites[0].release.set()
        self.assert_exited(self.launch_executor(run["id"], check_session=True))
        self.assertEqual(self.final(run["id"])["status"], "Pass")

    def test_shared_project_login_stays_serialized(self):
        first, second = self.queued(), self.queued()
        jobs.start_queued()
        self.assertTrue(self.sites[0].entered.wait(10))
        self.assertEqual(db.one("SELECT status FROM runs WHERE id = ?", (second["id"],))["status"], "Queued")
        self.sites[0].release.set()
        self.assertEqual(self.final(first["id"])["status"], "Pass")
        jobs.start_queued()
        self.assertEqual(self.final(second["id"])["status"], "Pass")
        self.assertEqual(self.sites[0].requests, 2)

    def test_two_crashed_children_are_reaped_and_free_queue_slots(self):
        runs = [self.queued(index) for index in range(3)]
        jobs.start_queued()
        for site in self.sites[:2]:
            self.assertTrue(site.entered.wait(10))
        crashed = list(runner._children.values())
        self.assertEqual(len(crashed), 2)
        for child in crashed:
            child.kill()
        if sys.platform.startswith("linux"):
            for child in crashed:
                os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)
                self.assertFalse(jobs.alive(child.pid))  # Still unreaped: kill(pid, 0) alone would return True.
        else:
            for child in crashed:
                child.wait(timeout=5)
        jobs.sweep()
        self.assertTrue(all(child.returncode is not None for child in crashed))
        self.assertFalse(runner._children)
        self.assertEqual([self.final(run["id"])["status"] for run in runs[:2]], ["Blocked", "Blocked"])
        jobs.start_queued()
        self.assertTrue(self.sites[2].entered.wait(10))
        self.sites[2].release.set()
        self.assertEqual(self.final(runs[2]["id"])["status"], "Pass")

    def test_spawn_failure_finishes_run_and_does_not_hold_project_slot(self):
        run = self.queued()
        with patch.object(runner.subprocess, "Popen", side_effect=OSError("process limit")):
            jobs.start_queued()
        result = self.final(run["id"])
        self.assertEqual(result["status"], "Blocked")
        self.assertIsNone(result["pid"])
        self.assertTrue(result["finished_at"])
        self.assertIn("启动", result["reason"])
        retry = self.queued()
        self.sites[0].release.set()
        jobs.start_queued()
        self.assertEqual(self.final(retry["id"])["status"], "Pass")

    def test_successful_child_handle_is_reaped_by_sweep(self):
        run = self.queued()
        self.sites[0].release.set()
        pid = runner.spawn(run["id"])
        child = runner._children[pid]
        if sys.platform.startswith("linux"):
            os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT)
            self.assertIsNone(child.returncode)
        else:
            child.wait(timeout=10)
        jobs.sweep()
        self.assertEqual(child.returncode, 0)
        self.assertNotIn(pid, runner._children)
        self.assertEqual(self.final(run["id"])["status"], "Pass")

    def test_cancel_during_final_case_remains_cancelled(self):
        run = self.queued()
        jobs.start_queued()
        self.assertTrue(self.sites[0].entered.wait(10))
        jobs.cancel(run["id"])
        self.sites[0].release.set()
        result = self.final(run["id"])
        self.assertEqual((result["status"], result["done"]), ("Cancelled", 1))
        self.assertEqual(self.sites[0].requests, 1)
        self.assertEqual(runner.execute(run["id"])["status"], "Cancelled")
        self.assertEqual(self.sites[0].requests, 1)

    def test_cancel_before_child_activation_executes_no_case(self):
        run = self.queued()
        self.assertTrue(db.claim_run(run["id"], os.getpid(), 2, "reservation"))
        jobs.cancel(run["id"])
        child = self.launch_executor(run["id"], "reservation")
        self.assert_exited(child)
        self.assertEqual(self.final(run["id"])["status"], "Cancelled")
        self.assertEqual(self.sites[0].requests, 0)

    def test_cancel_during_spawn_failure_wins(self):
        run = self.queued()

        def fail(*args, **kwargs):
            jobs.cancel(run["id"])
            raise OSError("process limit")

        with patch.object(runner.subprocess, "Popen", side_effect=fail):
            jobs.start_queued()
        self.assertEqual(self.final(run["id"])["status"], "Cancelled")

    def test_sweep_cannot_overwrite_a_newly_activated_child(self):
        run = self.queued()
        db.claim_run(run["id"], 999999, 2, "reservation")

        def accept_reservation(pid):
            self.assertTrue(db.activate_run(run["id"], os.getpid(), "reservation"))
            return False

        with patch.object(runner, "process_alive", side_effect=accept_reservation):
            jobs.sweep()
        result = db.one("SELECT * FROM runs WHERE id = ?", (run["id"],))
        self.assertEqual((result["status"], result["pid"]), ("Running", os.getpid()))

    def test_force_login_is_durable_when_worker_executes_cli_request(self):
        run = self.queued()
        jobs.cancel(run["id"])
        case = projects.case(run["project_id"], "TC-API-001")
        case["extensions"]["extract"] = {"test_id": "/test_id"}
        projects.save_cases(run["project_id"], [case])
        projects.set_auth(run["project_id"], {"api_case": case["id"]})
        sessions.save(run["project_id"], variables={"test_id": "old"})
        run = runner.create(run["project_id"], force_login=True)
        self.sites[0].release.set()
        jobs.start_queued()
        self.assertEqual(self.final(run["id"])["status"], "Pass")
        self.assertEqual(self.sites[0].requests, 1)
        details = json.loads(db.one("SELECT detail FROM results WHERE run_id = ?", (run["id"],))["detail"])
        self.assertFalse(details.get("reused"))

    def test_concurrent_startups_migrate_old_runs_without_losing_them(self):
        run = self.queued()
        with db.connect() as conn:
            conn.execute("DROP INDEX runs_by_session")
            # Old databases predate revision triggers; remove modern metadata before constructing that fixture.
            for trigger in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='runs'").fetchall():
                conn.execute('DROP TRIGGER "' + trigger["name"].replace('"', '""') + '"')
            for column in ("claim_token", "force_login", "session_id", "headed", "owner_identity"):
                conn.execute(f"ALTER TABLE runs DROP COLUMN {column}")
        for _ in range(2):
            child = subprocess.Popen([sys.executable, "-c", "from autoskill import db; db.init()"], cwd=paths.APP,
                                     env=paths.child_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.children.append(child)
        for child in self.children:
            self.assert_exited(child)
        row = db.one("SELECT * FROM runs WHERE id = ?", (run["id"],))
        self.assertEqual((row["status"], row["claim_token"], row["force_login"], row["session_id"], row["headed"], row["owner_identity"]),
                         ("Queued", "", 0, "", 0, "{}"))
        revision = db.one("SELECT revision FROM state_revision WHERE id=1")["revision"]
        db.update("runs", run["id"], {"claim_token": ""})
        self.assertEqual(db.one("SELECT revision FROM state_revision WHERE id=1")["revision"], revision)
        db.update("runs", run["id"], {"claim_token": "migrated"})
        self.assertGreater(db.one("SELECT revision FROM state_revision WHERE id=1")["revision"], revision)


if __name__ == "__main__":
    unittest.main()
