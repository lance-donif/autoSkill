"""Real CLI and HTTP postconditions fill the original sheet and reject borrowed evidence."""
import copy
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import test_api_http_lifecycle as lifecycle
import test_public_reuse_flow as reuse
from openpyxl import load_workbook

from autoskill import candidate_publication, case_plans, data_refs, db, execution_inputs, projects, runner


class PlanPostconditionTests(unittest.TestCase):
    command = reuse.PublicReuseFlowTests.command

    def setUp(self):
        lifecycle.ApiHttpLifecycleTests.setUp(self)
        environment = patch.dict(os.environ, {"AUTOSKILL_CASE_SKILL": str(self.folder / "sheet"), "AUTOSKILL_SESSION": "postcondition-plan",
                                              "AUTOSKILL_WORKFLOW_BINDING": ""})
        environment.start()
        self.addCleanup(environment.stop)
        self.inputs = self.folder / "inputs.json"
        self.inputs.write_text(json.dumps({"payload": {"value": 3}, "updated": {"value": 5}}), encoding="utf-8")
        self.rows = [{"id": "DESIGN-DELETE", "title": "删除本次创建的记录", "preconditions": "已创建本次记录。",
                      "steps": ["1. 删除本次创建的记录。", "2. 查询刚才删除的记录。"], "expected": "2. 查询返回空记录。"},
                     {"id": "DESIGN-ABSENT", "title": "核对删除后的实际状态", "preconditions": "已发起删除。",
                      "steps": ["1. 查询同一记录。"], "expected": "1. 返回空记录。"},
                     {"id": "DESIGN-UNRUN", "title": "另一个业务范围", "steps": ["1. 核对另一个业务。"], "expected": "1. 满足独立要求。"}]
        source = self.folder / "design.json"
        source.write_text(json.dumps(self.rows), encoding="utf-8")
        self.written = self.command("plan", "write", self.project, "--file", str(source))
        shown = self.command("plan", "show")
        cases = lifecycle.ApiHttpLifecycleTests.cases(self)
        cases[-2]["extensions"]["postconditions"] = [cases[-1]["id"]]
        for case, row in zip(cases[-2:], shown["cases"][:2]):
            case["extensions"]["test_case_document"] = {"description": row["title"], "preconditions": row["preconditions"],
                                                        "steps": row["steps"], "expected": row["expected"], "source": row["execution_source"]}
        self.cases = cases
        self.staged = candidate_publication.stage(self.project, cases, {}, {"fixture": "postcondition-plan"})

    def run_candidate(self, *, fails=False):
        result = self.command("run", self.project, "--candidate", self.staged["id"], "--case", "API-DELETE",
                              "--api-only", "--inputs", str(self.inputs), code=1 if fails else 0)
        run = db.one("SELECT * FROM runs WHERE id = ?", (result["run_id"],))
        self.assertEqual((run["total"], run["done"]), (1, 1))
        self.assertEqual(execution_inputs.load(run)["selected"], ["API-DELETE"])
        return result, run

    def workbook_rows(self, file):
        with Path(file).open("rb") as stream:
            book = load_workbook(stream)
            try:
                self.assertEqual(book["功能测试用例"].max_column, 14)
                return {row[3]: row for row in book["功能测试用例"].values if isinstance(row[0], str) and row[0].startswith("FT-")}
            finally:
                book.close()

    def test_failed_then_passed_postcondition_updates_same_complete_design(self):
        initial = Path(self.written["initial_excel"]).read_bytes()
        self.server.ignore_delete = True
        failed, first_run = self.run_candidate(fails=True)
        self.assertNotIn("API-DELETE", failed["published_ids"])
        self.assertNotIn("API-ABSENT", failed["published_ids"])
        first = self.command("plan", "show")
        self.assertEqual((first["task_id"], first["delivery_excel"]), (self.written["task_id"], self.written["delivery_excel"]))
        self.assertEqual([row["status"] for row in first["cases"]], ["失败", "失败", "未执行"])
        first_report = Path(first["test_report"]["excel"])
        first_bytes = first_report.read_bytes()
        self.server.ignore_delete = False
        passed, second_run = self.run_candidate()
        self.assertIn("API-DELETE", passed["published_ids"])
        self.assertIn("API-ABSENT", passed["published_ids"])
        shown = self.command("plan", "show")
        self.assertEqual(shown["test_report"]["status"], "ready")
        self.assertEqual(shown["delivery_excel"], self.written["delivery_excel"])
        view = case_plans.get(self.project, "postcondition-plan")
        for row in view["rows"][:2]:
            self.assertEqual(row["evidence"]["latest_execution"]["status"], "Pass")
            self.assertTrue(row["evidence"]["has_historical_failure"])
            self.assertEqual([item["run_id"] for item in row["evidence"]["execution_history"]], [first_run["id"], second_run["id"]])
        rows = self.workbook_rows(shown["delivery_excel"])
        for original in self.rows[:2]:
            self.assertEqual(rows[original["title"]][9], "失败（曾失败，后续通过）")
            self.assertEqual(rows[original["title"]][6], original["expected"])
        self.assertEqual(rows[self.rows[2]["title"]][9], "未执行")
        self.assertEqual(first_report.read_bytes(), first_bytes)
        self.assertEqual(Path(self.written["initial_excel"]).read_bytes(), initial)
        self.assertEqual(len(self.server.requests), 12)
        self.assertTrue(all(case["id"] in {saved["id"] for saved in projects.cases(self.project)} for case in self.cases))

    def test_derived_receipt_rejects_other_run_or_changed_target_even_when_outer_hash_is_recomputed(self):
        _first, first_run = self.run_candidate()
        _second, second_run = self.run_candidate()
        original = db.one("SELECT * FROM results WHERE run_id = ? AND case_id = 'API-DELETE'", (first_run["id"],))
        other = db.one("SELECT * FROM results WHERE run_id = ? AND case_id = 'API-ABSENT'", (second_run["id"],))
        self.assertTrue(execution_inputs.result_evidence(first_run, original))
        detail = json.loads(original["detail"])
        reference = detail["attempts"][-1]
        file = data_refs.resolve(reference["path"])
        original_bytes = file.read_bytes()
        receipt = json.loads(original_bytes)
        frozen = execution_inputs.load(first_run)["cases"]["API-DELETE"]
        for corruption in ("another_run", "target_check", "source_references", "execution_order"):
            altered = copy.deepcopy(receipt)
            source = altered["outcome"]["postcondition_source"]
            targets = altered["outcome"]["postcondition_results"]
            restored = []
            if corruption == "another_run":
                targets[0] = other
            elif corruption == "target_check":
                target_detail = json.loads(targets[0]["detail"])
                target_detail["assertions"][0]["actual"] = "invented"
                targets[0]["detail"] = json.dumps(target_detail)
            elif corruption == "source_references":
                source["attempts"] = []
            else:
                target_detail = json.loads(targets[0]["detail"])
                target_reference = target_detail["attempts"][0]
                target_file = data_refs.resolve(target_reference["path"])
                restored.append((target_file, target_file.read_bytes()))
                target_receipt = json.loads(restored[-1][1])
                target_receipt.update(started_at="2000-01-01T00:00:00+00:00", finished_at="2000-01-01T00:00:01+00:00")
                execution_inputs.write_json(target_file, target_receipt)
                target_reference["sha256"] = execution_inputs.content_hash(target_receipt)
                targets[0]["detail"] = json.dumps(target_detail)
                self.assertTrue(execution_inputs.result_evidence(first_run, targets[0]))
            altered["outcome"] = runner.postcondition_outcome(frozen, source, targets)
            execution_inputs.write_json(file, altered)
            changed_detail = {**copy.deepcopy(detail), **altered["outcome"]}
            changed_detail["attempts"][-1]["sha256"] = execution_inputs.content_hash(altered)
            changed_result = {**original, "status": altered["outcome"]["status"], "detail": json.dumps(changed_detail)}
            with self.subTest(corruption=corruption):
                self.assertFalse(execution_inputs.result_evidence(first_run, changed_result))
            file.write_bytes(original_bytes)
            for restored_file, original_content in restored:
                restored_file.write_bytes(original_content)
        self.assertTrue(execution_inputs.result_evidence(first_run, original))


if __name__ == "__main__":
    unittest.main()
