"""Copied and recycled process identifiers cannot own, cancel or hold another data copy open."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill import builder, db, deletion, native_assets, native_run, paths, projects, runner, runtime_identity
from autoskill.engines import process


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.root / "data"),
                                              "AUTOSKILL_CASE_SKILL": str(self.root / "data/.local/case-plans/default/autoskill-cases"),
                                              "AUTOSKILL_HEADED": "0", "AUTOSKILL_SESSION": ""})
        environment.start()
        self.addCleanup(environment.stop)
        self.project = projects.create("Ownership", "http://127.0.0.1:1", project_id="ownership")["id"]
        projects.save_cases(self.project, [builder.api_case("TC-API-001", "No request may be replayed", "GET", "/", {"status": 200, "json_equals": {"/ok": True}})])

    def queued(self):
        return runner.create(self.project)

    def native(self):
        source = self.root / "native.py"
        source.write_text("import unittest\nclass Checks(unittest.TestCase):\n def test_ok(self): self.assertTrue(True)\n")
        asset = native_assets.import_asset(self.project, str(source), "selenium-unittest")
        return runner.enqueue(native_run.prepare(self.project, asset["id"]))

    def mark(self, run, owner):
        db.update("runs", run["id"], {"status": "Running", "pid": owner.get("pid", os.getpid()), "owner_identity": owner})

    def sleeper(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop, child)
        return child

    @staticmethod
    def stop(child):
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=10)

    def marker(self, run, owner):
        folder = paths.local() / "runs" / run["id"] / "native"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "supervisor.json").write_text(json.dumps({"pid": owner.get("pid", os.getpid()), "owner_identity": owner}))
        (folder / "process-spec.json").write_text(json.dumps({"owner_pid": owner.get("pid", os.getpid()), "owner_identity": owner}))
        return folder

    def test_real_local_owner_survives_sweep_but_old_birth_and_foreign_roots_do_not(self):
        run = self.queued()
        self.assertTrue(db.claim_run(run["id"], os.getpid(), 2))
        runner.recover_runs()
        self.assertEqual(db.one("SELECT status FROM runs WHERE id = ?", (run["id"],))["status"], "Running")
        for field in ("machine_id", "location_id", "process_started_at"):
            with self.subTest(field=field):
                owner = {**runtime_identity.current(), field: "copied-or-recycled"}
                self.mark(run, owner)
                runner.recover_runs()
                row = db.one("SELECT * FROM runs WHERE id = ?", (run["id"],))
                self.assertEqual(row["status"], "Blocked")
                self.assertEqual(row["done"], 0)
                self.assertEqual(db.rows("SELECT * FROM results WHERE run_id = ?", (run["id"],)), [])
        self.mark(run, {})
        runner.recover_runs()
        self.assertEqual(db.one("SELECT status FROM runs WHERE id = ?", (run["id"],))["status"], "Blocked")

    def test_sweep_compare_and_set_preserves_new_owner_even_when_numeric_pid_is_reused(self):
        run = self.queued()
        self.mark(run, {**runtime_identity.current(), "process_started_at": "old-birth"})

        def replaced(_pid, _owner):
            db.update("runs", run["id"], {"owner_identity": runtime_identity.current()})
            return False

        with patch.object(runner.runtime_identity, "owns", side_effect=replaced):
            runner.recover_runs()
        row = db.one("SELECT * FROM runs WHERE id = ?", (run["id"],))
        self.assertEqual(row["status"], "Running")
        self.assertTrue(runtime_identity.owns(row["pid"], row["owner_identity"]))

    def test_spawn_reservation_hands_both_pid_and_birth_identity_to_the_real_child(self):
        run = self.queued()
        child = self.sleeper()
        try:
            with patch.object(runner.subprocess, "Popen", return_value=child):
                self.assertEqual(runner.spawn(run["id"]), child.pid)
            row = db.one("SELECT * FROM runs WHERE id = ?", (run["id"],))
            self.assertTrue(row["claim_token"])
            self.assertTrue(runtime_identity.owns(child.pid, row["owner_identity"]))
            runner.recover_runs()
            self.assertEqual(db.one("SELECT status FROM runs WHERE id = ?", (run["id"],))["status"], "Running")
        finally:
            self.stop(child)
            runner.reap_children()

    def test_cancelling_foreign_native_marker_never_signals_unrelated_live_process(self):
        child = self.sleeper()
        run = self.native()
        foreign = {**runtime_identity.current(child.pid), "location_id": "another-data-copy"}
        self.mark(run, foreign)
        folder = self.marker(run, foreign)
        before = {file.name: file.read_bytes() for file in folder.iterdir()}
        requested = native_run.cancel(run["id"])
        self.assertTrue(requested["cancel"])
        self.assertFalse((folder / "cancel").exists())
        self.assertEqual({file.name: file.read_bytes() for file in folder.iterdir()}, before)
        runner.recover_runs()
        self.assertEqual(db.one("SELECT status FROM runs WHERE id = ?", (run["id"],))["status"], "Cancelled")
        self.assertIsNone(child.poll())

    def test_deleting_foreign_running_record_does_not_wait_for_or_kill_same_pid(self):
        child = self.sleeper()
        run = self.native()
        foreign = {**runtime_identity.current(child.pid), "machine_id": "another-machine"}
        self.mark(run, foreign)
        self.marker(run, foreign)
        outcome = deletion.delete_runs([run["id"]], start_worker=False)
        self.assertFalse(outcome["pending"], outcome)
        self.assertFalse(outcome["errors"], outcome)
        self.assertIsNotNone(db.one("SELECT * FROM deleted_runs WHERE id = ?", (run["id"],)))
        self.assertIsNone(child.poll())

    def test_copied_spec_cannot_start_a_business_process_or_rewrite_old_evidence(self):
        folder = self.root / "old-evidence"
        folder.mkdir()
        target = folder / "business-action"
        spec = {"owner_pid": os.getpid(), "owner_identity": {**runtime_identity.current(), "location_id": "another-data-copy"},
                "evidence": str(folder), "cwd": str(folder), "argv": [sys.executable, "-c", f"open({str(target)!r}, 'w').write('changed')"]}
        file = folder / "process-spec.json"
        file.write_text(json.dumps(spec))
        (folder / "process-result.json").write_text('{"state":"completed","exit_code":1}')
        before = {item.name: item.read_bytes() for item in folder.iterdir()}
        process.worker(str(file))
        self.assertEqual({item.name: item.read_bytes() for item in folder.iterdir()}, before)
        self.assertFalse(target.exists())

    def test_exited_supervisor_with_live_caller_does_not_block_cleanup_forever(self):
        run = self.native()
        folder = self.marker(run, runtime_identity.current())
        (folder / "supervisor.json").write_text(json.dumps({"pid": 99999999, "owner_identity": runtime_identity.current(99999999)}))
        self.assertFalse(deletion._native_active(run["id"]))
        self.assertFalse((folder / "cancel").exists())

    def test_windows_launch_ready_does_not_bypass_owner_identity_check(self):
        folder = self.root / "old-launch"
        folder.mkdir()
        (folder / "launch-ready").touch()
        spec = folder / "process-spec.json"
        spec.write_text(json.dumps({"launcher_owner_pid": os.getpid(), "launcher_owner_identity": {**runtime_identity.current(), "process_started_at": "old"},
                                    "evidence": str(folder), "argv": [sys.executable, "-c", "raise SystemExit(0)"]}))
        with patch.object(process.subprocess, "call") as execute:
            self.assertEqual(process.windows_launch(str(spec)), 2)
        execute.assert_not_called()

    def test_owned_native_child_writes_identity_and_cancel_still_reaps_it(self):
        run = self.native()
        folder = paths.local() / "runs" / run["id"] / "native"
        spec = {"argv": [sys.executable, "-c", "import os,time;from pathlib import Path;Path('pid').write_text(str(os.getpid()));time.sleep(60)"],
                "cwd": str(folder), "evidence": str(folder)}
        owned = process.Process(spec)
        self.addCleanup(owned.cleanup)
        deadline = time.monotonic() + 10
        while not (folder / "heartbeat.json").is_file() and time.monotonic() < deadline:
            time.sleep(0.03)
        heartbeat = json.loads((folder / "heartbeat.json").read_bytes())
        self.assertTrue(runtime_identity.owns(heartbeat["pid"], heartbeat["owner_identity"]))
        self.assertTrue(deletion._native_active(run["id"]))
        self.assertEqual(owned.wait()["state"], "cancelled")
        self.assertFalse(runner.process_alive(heartbeat["pid"]))
        self.assertFalse(deletion._native_active(run["id"]))


if __name__ == "__main__":
    unittest.main()
