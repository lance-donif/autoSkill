"""Native completion binds artifact bytes once while retaining the adapter's original verdicts."""
import copy
import hashlib
import json
import os
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

from openpyxl import load_workbook
from test_native_engines import Workspace

from autoskill import db, native_assets, native_reader, native_run, task_report_data, task_reports
from autoskill.engines import ai_eval, reports


class NativeEvidenceTests(Workspace):
    def duplicate_run(self):
        source = self.fixture("import unittest\n"
                              "class Order(unittest.TestCase):\n"
                              " def __init__(self,index): super().__init__('runTest'); self.index=index\n"
                              " def id(self): return 'PAY-REPEAT' if self.index in (3,68) else 'PAY-%02d'%self.index\n"
                              " def __str__(self): return '付款核验第%02d条'%self.index\n"
                              " def runTest(self):\n"
                              "  if self.index==64: self.assertEqual('pending','paid')\n"
                              "  if self.index==68: self.assertEqual(1,2)\n"
                              "  if self.index==69: raise RuntimeError('账单服务暂不可用')\n"
                              "  self.assertTrue(True)\n"
                              "def load_tests(loader,tests,pattern): return unittest.TestSuite(Order(i) for i in range(70))\n")
        return self.execute(source)

    def test_duplicate_native_ids_keep_exact_proof_and_same_page_excel_markdown_verdicts(self):
        run = self.duplicate_run()
        self.assertEqual((run["passed"], run["failed"], run["not_run"]), (67, 3, 0))
        duplicates = [row for row in run["results"] if row["case_id"] == "PAY-REPEAT"]
        self.assertEqual([row["state"]["outcome"] for row in duplicates], ["pass", "fail"])
        self.assertEqual([row["state"]["evidence"] for row in duplicates], ["valid", "valid"])
        for row in duplicates:
            exact = native_reader.status(run["id"], case_id=f"result:{row['id']}")
            self.assertEqual(exact["case"]["status"], row["status"])
            self.assertEqual(exact["results"][0]["id"], row["id"])
        report = task_reports.get("run_" + run["id"])
        self.assertEqual(report["summary_counts"], {"Pass": 67, "Fail": 3, "NotRun": 0})
        book = load_workbook(report["excel"])
        try:
            conclusions = [row[9] for row in book["功能测试用例"].values if row[9] in ("通过", "失败", "未执行")]
        finally:
            book.close()
        self.assertEqual(Counter(conclusions), {"通过": 67, "失败": 3})
        markdown = Path(report["markdown"]).read_text(encoding="utf-8")
        self.assertIn("| 通过 | 67 |", markdown)
        self.assertIn("| 失败 | 3 |", markdown)
        self.assertIn("| 未执行 | 0 |", markdown)

    def test_one_damaged_native_occurrence_does_not_invalidate_other_rows(self):
        run = self.duplicate_run()
        first = run["results"][3]
        detail = {**first["detail"], "native_case_index": 68}
        db.execute("UPDATE results SET detail = ? WHERE id = ?", (json.dumps(detail), first["id"]))
        changed = native_run.get(run["id"])
        self.assertEqual((changed["passed"], changed["failed"]), (66, 4))
        snapshot = task_report_data.run_snapshot(db.one("SELECT * FROM runs WHERE id = ?", (run["id"],)))
        self.assertEqual(task_report_data.task_summary(snapshot)["counts"]["Fail"], 4)
        self.assertEqual(task_report_data.task_summary(snapshot)["counts"]["Pass"], 66)
        self.assertEqual(snapshot["cases"][68]["result"]["status"], "Fail")
        self.assertTrue(snapshot["cases"][68]["evidence_valid"])

    def test_legacy_duplicate_ids_match_only_unique_complete_native_content(self):
        run = self.duplicate_run()
        for row in run["results"]:
            detail = row["detail"]
            detail.pop("native_case_index")
            detail.pop("native_case_hash")
            db.execute("UPDATE results SET detail = ? WHERE id = ?", (json.dumps(detail), row["id"]))
        legacy = native_run.get(run["id"])
        self.assertEqual((legacy["passed"], legacy["failed"], legacy["not_run"]), (67, 3, 0))
        first = legacy["results"][3]
        ambiguous = copy.deepcopy(legacy["outcome"])
        ambiguous["cases"].append(copy.deepcopy(ambiguous["cases"][3]))
        self.assertFalse(task_report_data._native_row_evidence(legacy, ambiguous, first))

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

    def test_mock_native_outcomes_include_completed_unasserted_requests_and_target_start(self):
        run = self.queued()
        _folder, file = self.report(run)
        file.write_text(json.dumps({"run": {"executions": [
            {"item": {"id": "success"}, "response": {"code": 200, "responseTime": 2}},
            {"item": {"id": "started-error"}, "requestError": {"message": "timeout"}},
            {"item": {"id": "before-start"}, "scriptErrors": ["fixture setup failed"]},
        ]}}))
        cases = reports.newman(file)
        with mock.patch.object(native_run, "_run_adapter", return_value={"status": "Blocked", "cases": cases, "evidence": [str(file)]}):
            native_run.execute(run["id"])
        result = native_run.get(run["id"])
        self.assertEqual([row["status"] for row in result["results"]], ["Pass", "Fail", "NotRun"])
        self.assertEqual((result["passed"], result["failed"], result["not_run"]), (1, 1, 1))
        file.write_text("changed")
        self.assertEqual(native_run.get(run["id"])["results"][0]["status"], "Fail")

    def test_interrupted_unittest_retains_finished_and_started_cases(self):
        report = self.root / "unittest.json"
        events = [{"event": "start", "id": "one", "title": "one"},
                  {"event": "result", "row": {"id": "one", "title": "one", "status": "Pass", "target_started": True}},
                  {"event": "stop"}, {"event": "start", "id": "two", "title": "two"}]
        report.with_suffix(".jsonl").write_text("".join(json.dumps(event) + "\n" for event in events) + '{"event":')
        first, interrupted = reports.unittest_report(report, "运行已取消")
        self.assertEqual(first["status"], "Pass")
        self.assertTrue(interrupted["target_started"])
        self.assertEqual((interrupted["id"], interrupted["reason"]), ("two", "运行已取消"))

    def test_conversation_execution_only_pass_excludes_an_unresolved_judge(self):
        observed = {"status": "Pass", "business_status": "Unverified", "assertions": [], "reason": ""}
        case = ai_eval._case_result({}, "conversation", [observed], 0)
        self.assertTrue(case["target_started"])
        self.assertTrue(case["execution_only_pass"])
        pending = ai_eval._case_result({}, "conversation", [{**observed, "judgement": {"verdict": "uncertain"}}], 0)
        self.assertEqual(pending["status"], "Fail")
        self.assertFalse(pending["execution_only_pass"])

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
        self.assertEqual(result["results"][0]["status"], "Fail")
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
