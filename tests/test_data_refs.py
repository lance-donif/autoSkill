"""Portable data references preserve original evidence bytes after complete directory relocation."""
import copy
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PureWindowsPath
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill import builder, data_refs, db, deletion, domain, execution_inputs, native_assets, native_run, paths, projects, runner, runtime_identity, task_report_data


def metadata(root, *, legacy=(), locations=(), mappings=()):
    root.mkdir(parents=True, exist_ok=True)
    file = root / "data-root.json"
    value = json.loads(file.read_bytes()) if file.is_file() else {"schema_version": 1, "data_id": "fixture-data"}
    value.update(legacy_roots=list(legacy), locations=list(locations), path_mappings=list(mappings))
    file.write_text(json.dumps(value), encoding="utf-8")


class ReferenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "new root"
        self.old = Path(temporary.name) / "old root"
        metadata(self.root, legacy=[str(self.old)])

    def test_relative_round_trip_is_rooted_without_cwd_or_file_creation(self):
        relative = ".local/runs/run-1/attempts/result.json"
        resolved = data_refs.resolve(relative, self.root)
        self.assertEqual(resolved, self.root / relative)
        self.assertEqual(data_refs.store(resolved, self.root), relative)
        self.assertEqual(data_refs.store(relative, self.root), relative)
        self.assertFalse(resolved.parent.exists())

    def test_explicit_old_root_is_rebased_even_when_original_file_still_exists(self):
        self.old.mkdir()
        (self.old / "file.json").write_text("original")
        (self.root / "file.json").write_text("current copy")
        self.assertEqual(data_refs.resolve(self.old / "file.json", self.root).read_text(), "current copy")
        (self.root / "file.json").unlink()
        self.assertFalse(data_refs.resolve(self.old / "file.json", self.root).exists())
        self.assertEqual((self.old / "file.json").read_text(), "original")

    def test_windows_drive_unc_and_case_insensitive_old_roots_work_on_posix(self):
        metadata(self.root, legacy=[r"C:\Users\Original\autoSkill-data", r"\\original-server\share\tests"])
        expected = self.root / ".local/runs/run-1/evidence.png"
        for value in (r"c:\users\original\autoSkill-data\.local\runs\run-1\evidence.png",
                      r"\\ORIGINAL-SERVER\share\tests\.local\runs\run-1\evidence.png"):
            self.assertEqual(data_refs.resolve(value, self.root), expected)
            self.assertEqual(data_refs.store(value, self.root), ".local/runs/run-1/evidence.png")

    def test_longest_explicit_mapping_and_current_root_take_precedence(self):
        parent = self.root.parent
        metadata(self.root, legacy=[str(parent)], mappings=[
            {"source": str(self.old), "destination": "general"},
            {"source": str(self.old / "autoskill-cases"), "destination": ".local/case-plans/default/autoskill-cases"}])
        value = self.old / "autoskill-cases/cases.xlsx"
        self.assertEqual(data_refs.resolve(value, self.root), self.root / ".local/case-plans/default/autoskill-cases/cases.xlsx")
        self.assertEqual(data_refs.resolve(self.root / "own.json", self.root), self.root / "own.json")

    def test_location_alias_does_not_require_a_prior_migration(self):
        metadata(self.root, locations=[str(self.old)])
        self.assertEqual(data_refs.resolve(self.old / "projects/p/project.json", self.root), self.root / "projects/p/project.json")

    def test_external_sheet_fallback_does_not_hide_corrupt_or_escaping_mappings(self):
        external = self.old / "autoskill-cases"
        with patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.root)}):
            metadata(self.root)
            self.assertEqual(runner.plan_folder(external), external)
            (self.root / "data-root.json").write_text("invalid JSON")
            with self.assertRaises(ValueError):
                runner.plan_folder(external)
            (self.root / "data-root.json").unlink()
            metadata(self.root, mappings=[{"source": str(self.old), "destination": "../outside"}])
            with self.assertRaises(ValueError):
                runner.plan_folder(external)

    def test_unmapped_paths_traversal_and_link_escape_are_not_guessed(self):
        (self.root / "same-name.json").write_text("does not identify an external file")
        for value in (str(self.old.parent / "another/same-name.json"), str(self.old) + "-other/same-name.json", "../same-name.json", "C:relative.json"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                data_refs.resolve(value, self.root)
        outside = self.root.parent / "external"
        outside.mkdir()
        (self.root / "link").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            data_refs.resolve("link/file.json", self.root)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        self.reply()

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.reply()

    def reply(self):
        self.server.calls += 1
        body = b'{"ok":true,"priority":"medium","answer":42}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class EvidenceRelocationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.site.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.site.shutdown()
        cls.site.server_close()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.a, self.b = self.parent / "source-data", self.parent / "copied-data"
        environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.a), "AUTOSKILL_CASE_SKILL": str(self.a / ".local/case-plans/default/autoskill-cases"),
                                              "AUTOSKILL_HEADED": "0", "AUTOSKILL_SESSION": ""})
        environment.start()
        self.addCleanup(environment.stop)
        self.site.calls = 0
        self.pid = projects.create("Portable evidence", f"http://127.0.0.1:{self.site.server_port}", project_id="portable")["id"]
        self.case = builder.api_case("TC-API-001", "Preserve a real business failure", "GET", "/case",
                                     {"status": 200, "json_equals": {"/priority": "high"}})
        projects.save_cases(self.pid, [self.case])
        domain.commit(self.pid, {"sources/requirement.txt": b"The expected priority is high."}, expected_head=None)

    def completed(self):
        prepared = runner.prepare(self.pid)
        self.assertTrue(Path(prepared["input_manifest"]).is_absolute())
        self.assertTrue(Path(prepared["input_manifest"]).is_file())
        queued = runner.enqueue(prepared)
        final = runner.execute(queued["id"])
        self.assertTrue(Path(final["input_manifest"]).is_absolute())
        self.assertEqual(final["status"], "Fail")
        return db.one("SELECT * FROM runs WHERE id = ?", (final["id"],))

    def legacy(self, run, windows=False):
        legacy_root = r"C:\Users\Original\autoSkill-data" if windows else str(self.a)
        def absolute(value):
            relative = data_refs.store(value, self.a)
            return str(PureWindowsPath(legacy_root).joinpath(*Path(relative).parts)) if windows else str(self.a / relative)
        result = db.one("SELECT * FROM results WHERE run_id = ?", (run["id"],))
        detail = json.loads(result["detail"])
        for reference in detail["attempts"]:
            reference["path"] = absolute(reference["path"])
        db.update("results", result["id"], {"detail": detail})
        db.update("runs", run["id"], {"input_manifest": absolute(run["input_manifest"]), "plan_path": absolute(run["plan_path"])})
        metadata(self.a, legacy=[legacy_root], locations=[str(self.a)])
        return legacy_root

    def copy_tree(self):
        shutil.copytree(self.a, self.b)
        os.environ["AUTOSKILL_WORKSPACE"] = str(self.b)
        os.environ["AUTOSKILL_CASE_SKILL"] = str(self.b / ".local/case-plans/default/autoskill-cases")

    def evidence_bytes(self, root):
        folders = [root / ".local/execution-inputs", root / ".local/native-inputs", root / ".local/runs"]
        return {file.relative_to(root).as_posix(): file.read_bytes() for folder in folders for file in folder.rglob("*") if file.is_file()}

    def test_new_durable_refs_are_relative_while_public_run_locations_remain_accessible(self):
        run = self.completed()
        self.assertFalse(data_refs.is_absolute(run["input_manifest"]))
        self.assertFalse(data_refs.is_absolute(run["plan_path"]))
        result = db.one("SELECT * FROM results WHERE run_id = ?", (run["id"],))
        attempt = json.loads(result["detail"])["attempts"][0]
        self.assertFalse(data_refs.is_absolute(attempt["path"]))
        self.assertTrue(data_refs.resolve(attempt["path"]).is_file())
        snapshot = task_report_data.run_snapshot(run)
        self.assertFalse(data_refs.is_absolute(snapshot["cases"][0]["evidence_directory"]))
        self.assertFalse(data_refs.is_absolute(snapshot["metadata"]["input_manifest"]))
        self.assertTrue(execution_inputs.result_evidence(run, result))

    def test_old_absolute_evidence_resolves_in_copy_with_original_present_then_absent(self):
        run = self.completed()
        self.legacy(run)
        expected = self.evidence_bytes(self.a)
        self.copy_tree()
        copied = db.one("SELECT * FROM runs WHERE id = ?", (run["id"],))
        result = db.one("SELECT * FROM results WHERE run_id = ?", (run["id"],))
        original_input = self.a / data_refs.store(copied["input_manifest"])
        original_input.write_bytes(b"the old location is no longer authoritative")
        for original_present in (True, False):
            with self.subTest(original_present=original_present):
                if not original_present:
                    shutil.rmtree(self.a)
                self.assertTrue(execution_inputs.result_evidence(copied, result))
                self.assertEqual(execution_inputs.load(copied)["cases"][self.case["id"]]["assertions"]["json_equals"]["/priority"], "high")
                self.assertTrue(Path(runner.public_run(copied)["input_manifest"]).is_relative_to(self.b))
                summary = task_report_data.task_summary(task_report_data.run_snapshot(copied))
                self.assertEqual(summary["counts"]["Fail"], 1)
        self.assertEqual(self.evidence_bytes(self.b), expected)
        self.assertEqual(self.site.calls, 1)

    def test_windows_absolute_receipts_keep_their_hashes_and_validate_on_linux(self):
        run = self.completed()
        self.legacy(run, windows=True)
        expected = self.evidence_bytes(self.a)
        self.copy_tree()
        shutil.rmtree(self.a)
        copied = db.one("SELECT * FROM runs WHERE id = ?", (run["id"],))
        result = db.one("SELECT * FROM results WHERE run_id = ?", (run["id"],))
        self.assertTrue(execution_inputs.result_evidence(copied, result))
        self.assertEqual(copied["input_hash"], run["input_hash"])
        self.assertEqual(self.evidence_bytes(self.b), expected)
        file = data_refs.resolve(json.loads(result["detail"])["attempts"][0]["path"])
        file.write_bytes(b"corrupt copied evidence")
        self.assertFalse(execution_inputs.result_evidence(copied, result))

    def test_interrupted_foreign_executor_preserves_earlier_real_failure_and_terminal_history(self):
        run = self.completed()
        result = db.one("SELECT * FROM results WHERE run_id = ?", (run["id"],))
        original = {key: value for key, value in self.evidence_bytes(self.a).items() if "/test-report/" not in key}
        runner.recover_runs()
        self.assertEqual(db.one("SELECT * FROM runs WHERE id = ?", (run["id"],)), run)
        db.update("runs", run["id"], {"status": "Running", "pid": os.getpid(),
                                    "owner_identity": {**runtime_identity.current(), "location_id": "former-data-location"}})
        runner.recover_runs()
        recovered = db.one("SELECT * FROM runs WHERE id = ?", (run["id"],))
        self.assertEqual(recovered["status"], "Blocked")
        self.assertEqual(db.one("SELECT * FROM results WHERE run_id = ?", (run["id"],)), result)
        self.assertTrue(execution_inputs.result_evidence(recovered, result))
        self.assertEqual(task_report_data.task_summary(task_report_data.run_snapshot(recovered))["counts"]["Fail"], 1)
        self.assertEqual({key: value for key, value in self.evidence_bytes(self.a).items() if "/test-report/" not in key}, original)
        self.assertEqual(self.site.calls, 1)

    def test_deleting_the_copy_never_deletes_original_inputs_or_reuses_unresolved_paths(self):
        run = self.completed()
        self.legacy(run)
        original = self.evidence_bytes(self.a)
        self.copy_tree()
        outcome = deletion.delete_runs([run["id"]], start_worker=False)
        self.assertFalse(outcome["errors"], outcome)
        self.assertFalse((self.b / ".local/runs" / run["id"]).exists())
        self.assertEqual(self.evidence_bytes(self.a), original)
        self.assertIsNotNone(db.one("SELECT id FROM deleted_runs WHERE id = ?", (run["id"],)))

    def test_shared_manifest_with_absolute_and_relative_db_refs_is_retained(self):
        first = self.completed()
        second = {key: first[key] for key in ("project_id", "engine", "status", "case_ids", "total", "created_at", "family", "input_manifest", "input_hash")}
        second.update(id=db.new_id("run"), input_manifest=str(data_refs.resolve(first["input_manifest"])))
        db.insert("runs", second)
        file = data_refs.resolve(first["input_manifest"])
        outcome = deletion.delete_runs([first["id"]], start_worker=False)
        self.assertFalse(outcome["errors"], outcome)
        self.assertTrue(file.is_file())
        self.assertEqual(execution_inputs.load(second)["project_id"], self.pid)

    def test_native_frozen_assets_and_legacy_absolute_evidence_survive_tree_copy(self):
        spec = self.parent / "conversation.json"
        spec.write_text(json.dumps({"endpoint": {"url": f"http://127.0.0.1:{self.site.server_port}/ask"},
                                    "samples": [{"id": "answer", "question": "Value?", "expected": 42}]}), encoding="utf-8")
        asset = native_assets.import_asset(self.pid, str(spec), "ai-eval")
        prepared = native_run.prepare(self.pid, asset["id"])
        queued = runner.enqueue(prepared)
        self.assertEqual(native_run.execute(queued["id"])["status"], "Pass")
        run = db.one("SELECT * FROM runs WHERE id = ?", (queued["id"],))
        db.update("runs", run["id"], {"input_manifest": str(data_refs.resolve(run["input_manifest"]))})
        folder = paths.local() / "runs" / run["id"] / "native"
        outcome_file = folder / "outcome.json"
        outcome = json.loads(outcome_file.read_bytes())
        for ref in outcome["evidence"]:
            ref["path"] = str(folder / ref["path"])
        outcome_file.write_text(json.dumps(outcome), encoding="utf-8")
        for result in db.rows("SELECT * FROM results WHERE run_id = ?", (run["id"],)):
            detail = json.loads(result["detail"])
            detail["evidence"] = copy.deepcopy(outcome["evidence"])
            db.update("results", result["id"], {"detail": detail})
        metadata(self.a, legacy=[str(self.a)])
        expected = self.evidence_bytes(self.a)
        original_asset = domain.read_file(self.pid, f"native-assets/{asset['id']}/asset.json")
        self.copy_tree()
        shutil.rmtree(self.a)
        copied = db.one("SELECT * FROM runs WHERE id = ?", (run["id"],))
        snapshot = task_report_data.run_snapshot(copied)
        self.assertTrue(snapshot["cases"][0]["evidence_valid"])
        self.assertEqual(task_report_data.task_summary(snapshot)["counts"]["Pass"], 1)
        self.assertEqual(hashlib.sha256(domain.read_file(self.pid, f"native-assets/{asset['id']}/asset.json")).digest(), hashlib.sha256(original_asset).digest())
        self.assertEqual(self.evidence_bytes(self.b), expected)
        self.assertEqual(self.site.calls, 1)


if __name__ == "__main__":
    unittest.main()
