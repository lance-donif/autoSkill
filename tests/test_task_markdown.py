"""Short Markdown shares frozen facts with Excel and never changes execution to repair delivery."""
import copy
import hashlib
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

from fastapi.testclient import TestClient
from openpyxl import Workbook

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

import test_task_reports as fixtures

from autoskill import db, deletion, execution_inputs, projects, reporting, runner, task_context, task_markdown, task_report_data, task_reports, workflow
from autoskill.server.app import create_app


def snapshot():
    return {"schema": 1, "kind": "run", "task_id": "run-summary", "project_id": "fixture", "project": {"name": "Summary project"},
            "title": "Frozen task", "status": "Fail", "run_ids": ["run-summary"], "cases": [], "results": [],
            "sources": {"private_definition": "DO_NOT_DUMP_SOURCE"}, "notes": [], "environment": ["http://fixture.test"],
            "metadata": {"started_at": "2026-11-13T10:00:00+00:00", "finished_at": "2026-11-13T10:01:00+00:00"}}


def observation(identity, status, *, valid=True, reason="", recorded=None):
    definition = {"id": identity, "title": "Case " + identity, "kind": "api", "assertions": {"private": "DO_NOT_DUMP_ASSERTIONS"},
                  "steps": ["DO_NOT_DUMP_STEPS"], "turns": [{"answer": "DO_NOT_DUMP_DIALOGUE"}]}
    result = {"status": status, "reason": reason, "detail": {"business_status": "Pass" if status == "Pass" else status}}
    return {"definition": definition, "result": result if status is not None else None, "evidence_valid": valid,
            "run_id": "run-summary", "recording_result": recorded}


class TaskMarkdownSummaryTests(unittest.TestCase):
    def test_summary_is_short_keeps_all_verdicts_and_retains_the_earlier_failure(self):
        value = snapshot()
        value["environment"] = ["test-environment-" + str(index) for index in range(200)]
        value["cases"] = [observation("A", "Fail", reason="Observed wrong priority"), observation("A", "Pass"),
                          observation("B", "Blocked"), observation("C", None), observation("D", "unexpected"),
                          observation("E", "Pass", valid=False), observation("F", "Cancelled")]
        text, summary = task_markdown.render(value, excel_link="cases.xlsx")
        self.assertEqual((summary["cases"], summary["execution_rows"]), (6, 7))
        self.assertEqual(summary["counts"], {"Pass": 0, "Fail": 1, "Blocked": 1, "Cancelled": 1, "Unverified": 1, "NotRun": 1, "Unknown": 1})
        for label in ("失败", "阻塞", "未执行", "未知", "业务未验证", "已取消"):
            self.assertIn("| " + label + " | 1 |", text)
        self.assertIn("另有 1 项问题", text)
        self.assertIn("Observed wrong priority", text)
        self.assertIn("[查看完整 Excel](cases.xlsx)", text)
        self.assertNotIn("DO_NOT_DUMP", text)
        self.assertLess(len(text), 1800)

    def test_recording_failure_is_visible_but_never_becomes_a_replay_pass(self):
        value = snapshot()
        value.update(kind="recording", status="NotRun", run_ids=[])
        value["cases"] = [observation("recorded", None, recorded={"status": "Fail", "actual": "DO_NOT_DUMP_OBSERVATION"})]
        text, summary = task_markdown.render(value, excel_link="cases.xlsx")
        self.assertEqual((summary["counts"]["Pass"], summary["counts"]["NotRun"]), (0, 1))
        self.assertIn("录制时已观察到断言失败", text)
        self.assertIn("尚未回放", text)
        self.assertNotIn("DO_NOT_DUMP", text)

    def test_empty_or_missing_facts_stay_unknown_and_user_text_stays_literal(self):
        value = snapshot()
        value["project"]["name"] = '<script>alert(1)</script> [pretend](javascript:example)'
        value["metadata"], value["environment"] = {}, []
        text, summary = task_markdown.render(value, excel_link="cases.xlsx")
        self.assertEqual(summary["counts"]["Pass"], 0)
        self.assertIn("环境：未记录", text)
        self.assertIn("时间：未记录", text)
        self.assertNotIn("<script>", text)
        self.assertNotIn("[pretend]", text)
        self.assertIn("不代表通过", text)


class TaskMarkdownDeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.TaskReportTests.setUpClass.__func__(cls)

    def setUp(self):
        fixtures.TaskReportTests.setUp(self)
        # These delivery tests finish frozen reports explicitly. A separate
        # dispatcher would race fixture cleanup and steal that rendering work.
        worker = patch("autoskill.server.jobs.ensure_worker", return_value={"status": "running"})
        worker.start()
        self.addCleanup(worker.stop)

    def execute(self, case_id="TC-API-001"):
        return runner.execute(runner.create(self.pid, [case_id])["id"])

    def legacy(self, report):
        saved = copy.deepcopy(report)
        Path(saved["markdown"]).unlink()
        for key in list(saved):
            if key.startswith(("markdown", "summary_")):
                saved.pop(key)
        saved["validation"].pop("markdown_readable", None)
        execution_inputs.write_json(task_reports._folder(saved["report_id"]) / "receipt.json", saved)
        return saved

    def test_ready_receipt_and_http_download_validate_both_actual_files(self):
        finished = self.execute()
        report = finished["test_report"]
        self.assertEqual(report["status"], "ready", report)
        self.assertEqual(report["summary_counts"]["Fail"], 1)
        self.assertTrue(report["validation"]["workbook_readable"])
        self.assertTrue(report["validation"]["markdown_readable"])
        markdown = Path(report["markdown"]).read_bytes()
        self.assertEqual(hashlib.sha256(markdown).hexdigest(), report["markdown_sha256"])
        self.assertEqual(len(markdown), report["markdown_size"])
        self.assertIn(quote(Path(report["excel"]).name, safe=""), markdown.decode())
        self.assertEqual(task_reports.download(report["report_id"]), Path(report["excel"]))
        self.assertEqual(task_reports.download(report["report_id"], format="markdown"), Path(report["markdown"]))
        with self.assertRaisesRegex(ValueError, "格式"):
            task_reports.download(report["report_id"], format="html")
        with TestClient(create_app(start_worker=False), base_url="http://127.0.0.1") as client:
            response = client.get(report["markdown_download_url"])
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.headers["content-type"].startswith("text/markdown"))
            self.assertIn("attachment", response.headers["content-disposition"])
            self.assertEqual(response.content, markdown)
            self.assertEqual(client.get(report["download_url"]).status_code, 200)
            Path(report["markdown"]).write_text("changed", encoding="utf-8")
            self.assertEqual(task_reports.get(report["report_id"])["status"], "failed")
            self.assertGreaterEqual(client.get(report["markdown_download_url"]).status_code, 400)
            self.assertGreaterEqual(client.get(report["download_url"]).status_code, 400)

    def test_markdown_readback_failure_is_retryable_without_rerunning(self):
        original = Path.read_text

        def unreadable(file, *args, **kwargs):
            if file.name.startswith(".测试简报-") and file.suffix == ".tmp":
                return "wrong readback"
            return original(file, *args, **kwargs)

        with patch.object(Path, "read_text", unreadable):
            finished = self.execute()
        self.assertEqual(finished["status"], "Fail")
        failed = finished["test_report"]
        self.assertEqual(failed["status"], "failed", failed)
        self.assertNotIn("markdown", failed)
        self.assertNotIn("excel", failed)
        before = db.one("SELECT COUNT(*) n FROM runs")["n"]
        with patch.object(task_report_data, "run_snapshot", side_effect=AssertionError("must use frozen snapshot")), \
                patch.object(runner, "execute", side_effect=AssertionError("report repair must not execute")):
            fixed = task_reports.retry(failed["report_id"])
        self.assertEqual(fixed["status"], "ready", fixed)
        self.assertEqual(db.one("SELECT COUNT(*) n FROM runs")["n"], before)

    def test_a_renderer_claim_cannot_make_invalid_utf8_a_ready_report(self):
        def invalid(_snapshot, destination, **_kwargs):
            destination.write_bytes(b"\xff\xfe")
            return {"markdown_validation": {"markdown_readable": True}}

        with patch.object(task_markdown, "write_task", side_effect=invalid):
            finished = self.execute()
        self.assertEqual(finished["test_report"]["status"], "failed")
        self.assertNotIn("markdown_download_url", finished["test_report"])
        self.assertEqual(task_reports.retry(finished["test_report"]["report_id"])["status"], "ready")

    def test_legacy_excel_migration_keeps_its_bytes_and_frozen_history(self):
        report = self.legacy(self.execute()["test_report"])
        excel = Path(report["excel"]).read_bytes()
        changed = projects.case(self.pid, self.case["id"])
        changed["title"] = "Later live case"
        changed["assertions"]["json_equals"]["/priority"] = "medium"
        projects.save_cases(self.pid, [changed])
        projects.update(self.pid, name="Later live project")
        with patch.object(task_report_data, "run_snapshot", side_effect=AssertionError("must not recapture live history")):
            restored = task_reports.retry(report["report_id"])
        self.assertEqual(restored["status"], "ready", restored)
        self.assertEqual(Path(restored["excel"]).read_bytes(), excel)
        self.assertEqual((restored["sha256"], restored["source_hash"], restored["template_id"]),
                         (report["sha256"], report["source_hash"], report["template_id"]))
        text = Path(restored["markdown"]).read_text(encoding="utf-8")
        self.assertIn("Task Excel", text)
        self.assertNotIn("Later live", text)
        self.assertEqual(restored["summary_counts"]["Fail"], 1)
        self.assertEqual(db.one("SELECT COUNT(*) n FROM runs")["n"], 1)

    def test_legacy_report_is_automatically_completed_from_its_snapshot_on_ensure(self):
        finished = self.execute()
        old = self.legacy(finished["test_report"])
        with patch.object(task_report_data, "run_snapshot", side_effect=AssertionError("must not recapture")):
            ready = task_reports.for_run(finished["id"])
        self.assertEqual(ready["status"], "ready", ready)
        self.assertEqual(ready["sha256"], old["sha256"])
        self.assertTrue(Path(ready["markdown"]).is_file())

    def test_repeated_markdown_migration_failures_never_rebuild_a_valid_legacy_workbook(self):
        finished = self.execute()
        legacy = self.legacy(finished["test_report"])
        book = Workbook()
        book.active.title = "用例与结果"
        book.active.append(["用例编号", "用例标题", "本次结果"])
        book.active.append([self.case["id"], self.case["title"], "失败"])
        book.save(legacy["excel"])
        book.close()
        original = Path(legacy["excel"]).read_bytes()
        legacy.update(renderer="builtin-project-columns", template_id="builtin-project-columns",
                      sha256=hashlib.sha256(original).hexdigest(), size=len(original))
        execution_inputs.write_json(task_reports._folder(legacy["report_id"]) / "receipt.json", legacy)
        with patch.object(reporting, "write_task", side_effect=AssertionError("must preserve the legacy workbook")) as renderer, \
                patch.object(task_report_data, "run_snapshot", side_effect=AssertionError("must use the frozen history")) as capture:
            with patch.object(task_markdown, "write_task", side_effect=OSError("synthetic markdown failure")):
                failed = task_reports.for_run(finished["id"])
                for attempt in (failed, task_reports.retry(legacy["report_id"])):
                    self.assertEqual(attempt["status"], "failed")
                    self.assertNotIn("excel", attempt)
                    self.assertNotIn("_retained_excel", attempt)
                    self.assertNotIn("_retained_excel", task_reports.get(legacy["report_id"]))
                    self.assertEqual(Path(legacy["excel"]).read_bytes(), original)
            fixed = task_reports.retry(legacy["report_id"])
        renderer.assert_not_called()
        capture.assert_not_called()
        self.assertEqual(fixed["status"], "ready", fixed)
        self.assertEqual((fixed["template_id"], fixed["sha256"]), ("builtin-project-columns", legacy["sha256"]))
        self.assertEqual(Path(fixed["excel"]).read_bytes(), original)
        self.assertTrue(Path(fixed["markdown"]).is_file())

    def test_missing_or_modified_historical_snapshot_never_falls_back_to_live_data_on_retry(self):
        for damage in ("missing", "changed"):
            with self.subTest(damage=damage):
                report = self.legacy(self.execute()["test_report"])
                file = task_reports._folder(report["report_id"]) / "snapshot.json"
                if damage == "missing":
                    file.unlink()
                else:
                    value = json.loads(file.read_bytes())
                    value["project"]["name"] = "foreign history"
                    execution_inputs.write_json(file, value)
                with patch.object(task_report_data, "run_snapshot", side_effect=AssertionError("must not substitute live data")):
                    for _ in range(2):
                        failed = task_reports.retry(report["report_id"])
                        self.assertEqual(failed["status"], "failed")
                        self.assertEqual(failed["source_hash"], report["source_hash"])
                        self.assertNotIn("markdown", failed)

    def test_a_previous_workflow_cycle_can_add_markdown_without_using_the_new_cycle(self):
        created = workflow.create(self.pid, {"title": "Original cycle", "sources": [{"id": "source", "text": "Old expected priority"}]},
                                  idempotency_key="create-cycle")
        stopped = workflow.cancel(created["workflow_id"], idempotency_key="stop-cycle")
        ready = task_reports.finish_workflow_report(stopped["test_report"]["report_id"])
        self.assertEqual(ready["status"], "ready", ready)
        legacy = self.legacy(ready)
        frozen = json.loads((task_reports._folder(legacy["report_id"]) / "snapshot.json").read_bytes())
        original = Path(legacy["excel"]).read_bytes()
        workflow.revise(created["workflow_id"], {"title": "New cycle", "sources": [{"id": "source", "text": "New requirement"}]},
                        idempotency_key="next-cycle")
        with patch.object(task_report_data, "workflow_snapshot", side_effect=AssertionError("must not use the new cycle")) as capture:
            ready = task_reports.retry(legacy["report_id"])
        capture.assert_not_called()
        self.assertEqual(ready["status"], "ready", ready)
        self.assertEqual(Path(ready["excel"]).read_bytes(), original)
        text = Path(ready["markdown"]).read_text(encoding="utf-8")
        self.assertIn("Original cycle", text)
        self.assertIn(frozen["cycle_id"], text)
        self.assertNotIn("New cycle", text)

    def test_sessions_keep_distinct_summaries_and_deletion_removes_both_artifacts(self):
        with task_context.use_session("one"):
            first = self.execute()
        with task_context.use_session("two"):
            second = self.execute("TC-API-002")
        one, two = first["test_report"], second["test_report"]
        self.assertNotEqual(one["markdown"], two["markdown"])
        first_text, second_text = (Path(receipt["markdown"]).read_text(encoding="utf-8") for receipt in (one, two))
        self.assertIn(first["id"], first_text)
        self.assertNotIn(second["id"], first_text)
        self.assertNotIn(first["id"], second_text)
        self.assertEqual((one["summary_counts"]["Fail"], two["summary_counts"]["Pass"]), (1, 1))
        deletion.delete_runs([first["id"]])
        self.assertFalse(Path(one["excel"]).exists())
        self.assertFalse(Path(one["markdown"]).exists())
        with self.assertRaises(ValueError):
            task_reports.download(one["report_id"], format="markdown")
        self.assertTrue(Path(two["markdown"]).exists())


if __name__ == "__main__":
    unittest.main()
