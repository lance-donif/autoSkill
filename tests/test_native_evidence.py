"""Native completion binds artifact bytes once while retaining the adapter's original verdicts."""
import copy
import hashlib
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from test_native_engines import Workspace

from autoskill import db, native_assets, native_run


class NativeEvidenceTests(Workspace):
    def queued(self):
        source = self.fixture("import unittest\nclass Original(unittest.TestCase):\n def test_ok(self): self.assertEqual(2 + 2, 4)\n")
        asset = native_assets.import_asset(self.pid, str(source), "selenium-unittest")
        return native_run.enqueue(self.pid, asset["id"])

    def report(self, run):
        folder = native_run._evidence_folder(run)
        folder.mkdir(parents=True, exist_ok=True)
        file = folder / "report.json"
        file.write_bytes(b'{"cases": [{"id": "one", "status": "Pass"}]}')
        return folder, file

    def outcome(self, evidence, status="Pass"):
        return {"status": status, "business_status": status, "cases": [
            {"id": "one", "title": "Original test", "status": status, "verdict_scope": "native_test",
             "assertions": [{"name": "Original assertion", "passed": status == "Pass"}]}], "evidence": evidence}

    def test_real_unittest_preserves_pass_fail_and_binds_all_report_bytes(self):
        source = self.fixture("import unittest\nclass Original(unittest.TestCase):\n"
                              " def test_pass(self): self.assertEqual(2 + 2, 4)\n"
                              " def test_fail(self): self.assertEqual('actual', 'expected')\n")
        result = self.execute(source)
        self.assertEqual(result["status"], "Fail")
        self.assertEqual(result["outcome"]["business_status"], "Fail")
        self.assertEqual({row["status"] for row in result["results"]}, {"Pass", "Fail"})
        references = result["outcome"]["evidence"]
        self.assertTrue(any(ref["path"] == "unittest.json" for ref in references))
        self.assertEqual(result["outcome"]["evidence_status"], "bound")
        folder = native_run._evidence_folder(result)
        for ref in references:
            self.assertFalse(Path(ref["path"]).is_absolute())
            data = (folder / ref["path"]).read_bytes()
            self.assertEqual(ref["sha256"], hashlib.sha256(data).hexdigest())
            self.assertEqual(ref["size"], len(data))
        saved = json.loads((folder / "outcome.json").read_bytes())
        self.assertEqual(saved["evidence"], references)
        for row in result["results"]:
            self.assertEqual(row["detail"]["evidence"], references)
            self.assertEqual(row["detail"]["evidence_status"], "bound")

    def test_absolute_prehashed_reference_is_validated_and_normalized_without_promoting_verdict(self):
        run = self.queued()
        _folder, file = self.report(run)
        data = file.read_bytes()
        raw = self.outcome([{"path": str(file), "sha256": hashlib.sha256(data).hexdigest(), "kind": "native-report"}])
        raw["business_status"] = "Unverified"
        raw["cases"][0]["assertions"] = []
        frozen = native_run._bind_evidence(run, raw)
        self.assertEqual(frozen["status"], "Pass")
        self.assertEqual(frozen["business_status"], "Unverified")
        self.assertEqual(frozen["cases"], raw["cases"])
        self.assertEqual(frozen["evidence"], [{"path": "report.json", "sha256": hashlib.sha256(data).hexdigest(),
                                               "size": len(data), "kind": "native-report"}])

    def test_existing_hash_conflict_is_durable_and_retains_original_cases(self):
        run = self.queued()
        _folder, file = self.report(run)
        raw = self.outcome([{"path": str(file), "sha256": "0" * 64, "size": file.stat().st_size}])
        with mock.patch.object(native_run, "_run_adapter", return_value=copy.deepcopy(raw)):
            finished = native_run.execute(run["id"])
        self.assertEqual(finished["status"], "Blocked")
        result = native_run.get(run["id"])
        outcome = result["outcome"]
        self.assertEqual(outcome["reported_status"], "Pass")
        self.assertEqual(outcome["business_status"], "Pass")
        self.assertEqual(outcome["cases"], raw["cases"])
        self.assertEqual(outcome["reported_evidence"], raw["evidence"])
        self.assertEqual(outcome["evidence"], [])
        self.assertEqual(outcome["evidence_status"], "invalid")
        self.assertIn("摘要", outcome["evidence_error"])
        self.assertEqual(result["results"][0]["status"], "Pass")
        self.assertEqual(result["results"][0]["detail"]["evidence_status"], "invalid")
        self.assertEqual(result["results"][0]["detail"]["assertions"], raw["cases"][0]["assertions"])

    def test_integrity_failure_never_erases_an_observed_business_failure(self):
        run = self.queued()
        _folder, file = self.report(run)
        raw = self.outcome([{"path": str(file), "size": file.stat().st_size + 1}], "Fail")
        frozen = native_run._bind_evidence(run, raw)
        self.assertEqual(frozen["status"], "Fail")
        self.assertEqual(frozen["business_status"], "Fail")
        self.assertEqual(frozen["cases"], raw["cases"])
        self.assertEqual(frozen["evidence_status"], "invalid")
        self.assertIn("大小", frozen["evidence_error"])

    def test_missing_or_non_file_evidence_invalidates_the_whole_reference_set(self):
        run = self.queued()
        folder, _file = self.report(run)
        for missing in ("missing.json", str(folder)):
            with self.subTest(path=missing):
                frozen = native_run._bind_evidence(run, self.outcome(["report.json", missing]))
                self.assertEqual(frozen["status"], "Blocked")
                self.assertEqual(frozen["evidence"], [])
                self.assertEqual(frozen["evidence_status"], "invalid")

    @unittest.skipIf(os.name == "nt", "This fixture requires unprivileged symbolic link creation")
    def test_outside_traversal_symlink_and_self_references_are_never_hashed(self):
        run = self.queued()
        folder, _file = self.report(run)
        outside = self.root / "outside.json"
        outside.write_text("outside fixture")
        sibling = folder.parent.parent / "another-run" / "native" / "report.json"
        sibling.parent.mkdir(parents=True)
        sibling.write_text("another run")
        link = folder / "link.json"
        link.symlink_to(outside)
        loop = folder / "loop.json"
        loop.symlink_to(loop)
        (folder / "outcome.json").write_text("reserved summary")
        for name in (str(outside), str(sibling), "../native/report.json", "link.json", "loop.json", "outcome.json"):
            with self.subTest(path=name), mock.patch.object(native_run.hashlib, "file_digest", side_effect=AssertionError("must not read rejected evidence")):
                frozen = native_run._bind_evidence(run, self.outcome([name]))
                self.assertEqual(frozen["status"], "Blocked")
                self.assertEqual(frozen["evidence_status"], "invalid")

    @unittest.skipIf(os.name == "nt", "This fixture requires unprivileged symbolic link creation")
    def test_redirected_run_folder_does_not_start_adapter_or_write_outside(self):
        run = self.queued()
        folder = native_run._evidence_folder(run)
        folder.parent.mkdir(parents=True)
        outside = self.root / "outside-run"
        outside.mkdir()
        folder.symlink_to(outside, target_is_directory=True)
        with mock.patch.object(native_run, "_run_adapter", side_effect=AssertionError("adapter must not start")) as adapter:
            result = native_run.execute(run["id"])
        adapter.assert_not_called()
        self.assertEqual(result["status"], "Blocked")
        self.assertFalse((outside / "outcome.json").exists())
        self.assertEqual(list(outside.iterdir()), [])

    def test_mutation_during_hashing_is_not_bound_as_completed_evidence(self):
        run = self.queued()
        _folder, file = self.report(run)
        digest = hashlib.file_digest

        def mutate(stream, algorithm):
            result = digest(stream, algorithm)
            file.write_bytes(file.read_bytes() + b"changed")
            return result

        with mock.patch.object(native_run.hashlib, "file_digest", side_effect=mutate):
            frozen = native_run._bind_evidence(run, self.outcome(["report.json"]))
        self.assertEqual(frozen["status"], "Blocked")
        self.assertIn("绑定期间发生变化", frozen["evidence_error"])

    def test_reading_legacy_string_evidence_never_rebinds_current_files(self):
        run = self.queued()
        native_run.execute(run["id"])
        folder = native_run._evidence_folder(run)
        file = folder / "outcome.json"
        outcome = json.loads(file.read_bytes())
        outcome["evidence"] = [ref["path"] for ref in outcome["evidence"]]
        outcome.pop("evidence_status")
        file.write_text(json.dumps(outcome))
        for row in db.rows("SELECT id, detail FROM results WHERE run_id = ?", (run["id"],)):
            detail = json.loads(row["detail"])
            detail["evidence"] = outcome["evidence"]
            detail.pop("evidence_status")
            db.execute("UPDATE results SET detail = ? WHERE id = ?", (json.dumps(detail), row["id"]))
        before = file.read_bytes()
        result = native_run.get(run["id"])
        self.assertEqual(result["outcome"]["evidence"], outcome["evidence"])
        self.assertEqual(result["results"][0]["detail"]["evidence"], outcome["evidence"])
        self.assertEqual(file.read_bytes(), before)

    def test_missing_success_evidence_is_explicit_without_changing_case_assertions(self):
        run = self.queued()
        raw = self.outcome([])
        frozen = native_run._bind_evidence(run, raw)
        self.assertEqual(frozen["status"], "Blocked")
        self.assertEqual(frozen["cases"], raw["cases"])
        self.assertEqual(frozen["reported_status"], "Pass")
        self.assertEqual(frozen["evidence_status"], "invalid")
