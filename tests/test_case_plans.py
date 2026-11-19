"""Online design sheets isolate sessions and preserve real evidence through editable revisions."""
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))
from fastapi.testclient import TestClient

from autoskill import builder, case_plans, db, execution_inputs, plan, project_materials, projects, runner, task_workbook
from autoskill.server.app import create_app


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        return

    def do_GET(self):
        data = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class CasePlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": self.tmp.name, "AUTOSKILL_HEADED": "0"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.site = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.site.serve_forever, kwargs={"poll_interval": .02}, daemon=True).start()
        self.addCleanup(self.site.server_close)
        self.addCleanup(self.site.shutdown)
        self.pid = projects.create("设计表项目", f"http://127.0.0.1:{self.site.server_port}", project_id="design")["id"]
        self.document = {"description": "验证服务返回真实健康状态", "preconditions": "服务已经运行，健康检查数据已固定",
                         "steps": ["1. 查询服务健康状态", "2. 检查健康检查结果"], "expected": "2. 服务状态为健康，ok 标记为 true。",
                         "source": {"kind": "synthetic_requirement", "text": "本地健康服务契约：ok 为 true 表示健康", "version": 1},
                         "module": "运维/健康", "business_path": ["设计表项目", "运维", "健康"], "priority": "P0", "prefix": "JK"}
        projects.save_cases(self.pid, [builder.api_case("API-001", "健康检查", "GET", "/health", {"status": 200, "json_equals": {"/ok": True}}, test_case_document=self.document)])
        self.row = {"id": "PLAN-001", "title": "健康检查", "module": "运维/健康", "type": "接口", "priority": "P1", "preconditions": "服务已运行",
                    "steps": "1. 请求健康接口", "expected": "1. ok 为 true", "author": "测试人员", "description": "验证服务健康", "requirement_ids": ["legacy/REQ-RECORDED"]}

    def save(self, row=None, *, session=""):
        current = case_plans.get(self.pid, session)
        return case_plans.save(self.pid, row or self.row, session=session, expected_revision=current["revision"])

    def test_draft_cannot_be_marked_green_and_edit_invalidates_old_binding_but_keeps_history(self):
        created = self.save()
        self.assertEqual(created["rows"][0]["status"], "NotRun")
        with plan.use_folder(created["folder"]):
            plan.mark("PLAN-001", "通过", "manual green")
        current = case_plans.get(self.pid)
        self.assertEqual(current["rows"][0]["status"], "NotRun")
        linked = case_plans.link(self.pid, "PLAN-001", "API-001", expected_revision=current["revision"])
        with patch("autoskill.server.jobs.ensure_worker", return_value={"status": "test"}):
            run = case_plans.run(self.pid, ["PLAN-001"])
        self.assertEqual(runner.execute(run["id"])["status"], "Pass")
        synced = case_plans.sync(self.pid, expected_revision=case_plans.get(self.pid)["revision"])
        self.assertEqual(synced["rows"][0]["status"], "Pass")
        self.assertIn("覆盖关系待确认", synced["rows"][0]["evidence"]["reason"])
        changed = case_plans.save(self.pid, {"expected": "1. 返回新需求要求的值"}, case_id="PLAN-001", expected_revision=synced["revision"])
        self.assertEqual(changed["rows"][0]["status"], "NotRun")
        self.assertFalse(changed["rows"][0]["evidence"]["binding_current"])
        with plan.use_folder(changed["folder"]):
            plan.sync()
            self.assertEqual(plan.show()["cases"][0]["status"], "未执行")
        old = case_plans.history(self.pid, linked["version"])
        self.assertEqual(old["rows"][0]["status"], "Pass")
        self.assertEqual(len(projects.cases(self.pid)), 1)
        with self.assertRaisesRegex(ValueError, "草稿不能直接执行"):
            case_plans.run(self.pid, ["PLAN-001"])
        exported = case_plans.export(self.pid)
        book = load_workbook(exported)
        self.addCleanup(book.close)
        self.assertEqual([cell.value for cell in book["功能测试用例"][1]], task_workbook.HEADERS)
        conclusions = [row[9].value for row in book["功能测试用例"].iter_rows(min_row=2) if row[1].value]
        self.assertEqual(conclusions, ["未执行"])

    def test_project_and_named_sessions_do_not_share_designs_or_execution(self):
        self.save(session="任务甲")
        self.assertFalse(case_plans.get(self.pid, "任务乙")["exists"])
        other = projects.create("另一个项目", "http://127.0.0.1:9", project_id="other")["id"]
        self.assertFalse(case_plans.get(other, "任务甲")["exists"])
        self.assertEqual(case_plans.get(self.pid, "任务甲")["rows"][0]["id"], "PLAN-001")
        self.assertEqual(len(projects.cases(self.pid)), 1)
        self.assertEqual(projects.cases(other), [])

    def test_cli_sheet_is_visible_without_manual_adoption_or_rewriting_bytes(self):
        source = Path(self.tmp.name) / "old-design.json"
        source.write_text('[{"id":"OLD-001","title":"既有设计","steps":"1. 检查","expected":"1. 符合需求"}]', encoding="utf-8")
        made = plan.write(self.pid, str(source))
        self.assertIn("内部编辑表", made["excel_role"])
        delivery = load_workbook(made["delivery_excel"])
        self.addCleanup(delivery.close)
        self.assertEqual([cell.value for cell in delivery["功能测试用例"][1]], task_workbook.HEADERS)
        self.assertEqual([row[9].value for row in delivery["功能测试用例"].iter_rows(min_row=2) if row[1].value], ["未执行"])
        plan.mark("OLD-001", "通过", "legacy manual result")
        original = Path(made["excel"]).read_bytes()
        view = case_plans.get(self.pid)
        self.assertTrue(view["exists"])
        self.assertIsNone(view["legacy"])
        self.assertEqual(view["rows"][0]["id"], "OLD-001")
        self.assertEqual(view["rows"][0]["status"], "NotRun")
        self.assertEqual(view["delivery_excel"], made["delivery_excel"])
        self.assertEqual(Path(made["excel"]).read_bytes(), original)

    def test_imported_existing_case_uses_real_current_evidence_and_http_result_fields_are_read_only(self):
        run = runner.create(self.pid, ["API-001"])
        self.assertEqual(runner.execute(run["id"])["status"], "Pass")
        current = case_plans.get(self.pid)
        imported = case_plans.import_cases(self.pid, ["API-001"], expected_revision=current["revision"])
        self.assertEqual(imported["rows"][0]["status"], "Pass")
        client = TestClient(create_app(start_worker=False), base_url="http://127.0.0.1:8787")
        self.addCleanup(client.close)
        response = client.patch(f"/api/case-plans/{self.pid}/rows/API-001", json={"expected_revision": imported["revision"], "row": {"status": "Pass"}})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(client.get(f"/api/case-plans/{self.pid}?session=another").json()["rows"], [])
        response = client.get(f"/api/case-plans/{self.pid}/excel")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b"PK"))
        with self.assertRaisesRegex(ValueError, "设计表已变化"):
            case_plans.save(self.pid, {"title": "stale"}, case_id="API-001", expected_revision=current["revision"])

    def test_functional_design_is_saved_on_actual_case_revision_and_old_pass_is_not_reused(self):
        document = {"description": "验证服务返回真实健康状态", "preconditions": "服务已经运行，健康检查数据已固定",
                    "steps": ["1. 查询服务健康状态", "2. 检查健康检查结果"], "expected": "2. 服务状态为健康，ok 标记为 true。",
                    "source": "本地合成健康服务需求：ok 为 true 表示健康", "priority": "P0", "module": "运维/健康",
                    "author": "需求验证人员", "remarks": "保留原健康状态判据，不按错误响应改写预期。", "business_path": ["设计表项目", "运维", "健康"], "prefix": "JK"}
        case = projects.case(self.pid, "API-001")
        case["extensions"]["test_case_document"] = document
        projects.save_cases(self.pid, [case])
        original = projects.case(self.pid, "API-001")
        first = runner.create(self.pid, ["API-001"])
        runner.execute(first["id"])
        current = case_plans.get(self.pid)
        imported = case_plans.import_cases(self.pid, ["API-001"], expected_revision=current["revision"])
        row = imported["rows"][0]
        self.assertEqual(row["steps"].splitlines(), document["steps"])
        self.assertEqual((row["author"], row["source"], row["remarks"]), (document["author"], document["source"], document["remarks"]))
        self.assertEqual(row["business_path"], document["business_path"])
        applied = case_plans.apply_to_case(self.pid, "API-001", expected_revision=imported["revision"])
        version = projects.case(self.pid, "API-001")
        self.assertGreater(version["revision"], original["revision"])
        self.assertEqual(version["steps"], document["steps"])
        self.assertEqual(version["preconditions"], document["preconditions"])
        self.assertEqual(version["expected"], document["expected"])
        for key in ("method", "path", "assertions"):
            self.assertEqual(version[key], original[key])
        self.assertEqual(applied["rows"][0]["status"], "NotRun")
        old_result = db.one("SELECT * FROM results WHERE run_id = ?", (first["id"],))
        self.assertFalse(execution_inputs.result_current(self.pid, version, old_result))
        with patch("autoskill.server.jobs.ensure_worker", return_value={"status": "test"}):
            second = case_plans.run(self.pid, ["API-001"])
        self.assertEqual(runner.execute(second["id"])["status"], "Pass")
        self.assertEqual(case_plans.get(self.pid)["rows"][0]["status"], "Fail")
        outcome = json.loads(db.one("SELECT detail FROM results WHERE run_id = ?", (second["id"],))["detail"])
        self.assertEqual(outcome["business_status"], "Unverified")
        frozen = execution_inputs.load(db.one("SELECT * FROM runs WHERE id = ?", (second["id"],)))
        self.assertEqual(frozen["cases"]["API-001"]["steps"], document["steps"])
        with plan.use_folder(applied["folder"]):
            planned = plan.show()["cases"][0]
            self.assertEqual((planned["description"], planned["source"]), (document["description"], document["source"]))

    def test_legacy_raw_request_import_remains_missing_design_and_cannot_publish_it(self):
        legacy = projects.case(self.pid, "API-001")
        legacy["extensions"].pop("test_case_document", None)
        legacy["steps"] = ["GET /health"]
        legacy["expected"] = "HTTP 200，并满足录制到的字段与结构"
        projects.save_cases(self.pid, [legacy])
        current = case_plans.get(self.pid)
        imported = case_plans.import_cases(self.pid, ["API-001"], expected_revision=current["revision"])
        row = imported["rows"][0]
        self.assertNotIn("GET /health", row["steps"])
        self.assertIn("待确认", row["steps"])
        self.assertIn("待确认", row["expected"])
        before = projects.case(self.pid, "API-001")
        with self.assertRaisesRegex(ValueError, "待补充"):
            case_plans.apply_to_case(self.pid, "API-001", expected_revision=imported["revision"])
        self.assertEqual(projects.case(self.pid, "API-001"), before)

    def test_legacy_binding_without_mapping_proof_keeps_history_without_current_business_pass(self):
        first = runner.create(self.pid, ["API-001"])
        runner.execute(first["id"])
        imported = case_plans.import_cases(self.pid, ["API-001"], expected_revision=case_plans.get(self.pid)["revision"])
        self.assertEqual(imported["rows"][0]["status"], "Pass")
        metadata = Path(imported["folder"]) / "design.json"
        original = json.loads(metadata.read_bytes())
        original["rows"]["API-001"]["binding"].pop("design_mapped")
        metadata.write_text(json.dumps(original), encoding="utf-8")
        self.assertEqual(case_plans.get(self.pid)["rows"][0]["status"], "Pass")
        self.assertEqual(db.one("SELECT status FROM runs WHERE id = ?", (first["id"],))["status"], "Pass")

    def test_project_export_waits_for_a_complete_workbook_and_metadata_revision(self):
        current = self.save()
        workbook_saved, release, exported = threading.Event(), threading.Event(), threading.Event()
        errors, bundle = [], []
        original = plan._save

        def paused_save(book):
            result = original(book)
            workbook_saved.set()
            if not release.wait(10):
                raise RuntimeError("Writer was not released")
            return result

        def write():
            try:
                case_plans.save(self.pid, {"expected": "1. 新预期", "author": "新编制人"}, case_id="PLAN-001", expected_revision=current["revision"])
            except Exception as error:  # noqa: BLE001  # Propagate worker failures into the owning test thread.
                errors.append(error)

        def export():
            try:
                bundle.append(project_materials.export_project(self.pid))
            except Exception as error:  # noqa: BLE001  # Propagate worker failures into the owning test thread.
                errors.append(error)
            finally:
                exported.set()

        with patch.object(plan, "_save", side_effect=paused_save):
            writer = threading.Thread(target=write)
            reader = threading.Thread(target=export)
            writer.start()
            self.assertTrue(workbook_saved.wait(10))
            reader.start()
            self.assertFalse(exported.wait(.15))
            release.set()
            writer.join(10)
            reader.join(10)
        self.assertFalse(writer.is_alive())
        self.assertFalse(reader.is_alive())
        self.assertEqual(errors, [])
        file = project_materials.export_file(bundle[0]["export_id"])
        with zipfile.ZipFile(file) as package:
            prefix = self.pid + "/testcases/design-plans/default/"
            meta = json.loads(package.read(prefix + "design.json"))
            self.assertEqual(meta["rows"]["PLAN-001"]["author"], "新编制人")
            self.assertEqual(meta["version"], current["version"] + 1)
            book = load_workbook(io.BytesIO(package.read(prefix + "cases.xlsx")))
            self.addCleanup(book.close)
            self.assertEqual(book["用例"]["H2"].value, "1. 新预期")


if __name__ == "__main__":
    unittest.main()
