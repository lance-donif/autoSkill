"""Human functional designs survive real page editing, recording and new execution evidence."""
import hashlib
import io
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import openpyxl
from playwright.sync_api import expect
from test_load_dashboard import DashboardWorkspace

from autoskill import case_folders, case_plans, db, execution_inputs, plan, projects, runner, task_context, task_reports
from autoskill.recorder import session
from autoskill.server import jobs


class FunctionalDesignDashboardTests(DashboardWorkspace):
    def test_explicit_recording_subproject_keeps_the_published_design_binding_and_folder(self):
        task, identity = "验证目录", "DESIGN-PATH"
        row = {"id": identity, "title": "查看会话状态", "description": "验证初始会话状态", "source": "合成需求",
               "preconditions": "会话页面可用", "steps": "1. 进入会话页面。", "expected": "1. 页面显示待执行。",
               "business_path": ["业务", "功能", "会话状态"], "module": "未分组", "priority": "P1"}
        revision = case_plans.get(self.pid, task)["revision"]
        saved = case_plans.save(self.pid, row, session=task, expected_revision=revision)
        with task_context.use_session(task), plan.use_folder(saved["folder"]):
            session._save({"project_id": self.pid, "start_url": "/ui", "steps": [
                {"assertion": "text", "locators": [{"by": "testid", "value": "result"}], "expected": "待执行",
                 "oracle": {"source": "explicit"}}], "secrets": [], "used_login": False, "as_login": False})
            with patch.object(session.agent, "call", return_value=None), patch.object(session.capture, "stop", return_value={}), \
                    patch.object(jobs, "ensure_worker", return_value={}):
                recording = session.stop(row["title"], module="会话服务", plan_case=identity)
            with ThreadPoolExecutor(max_workers=1) as worker:
                replay = worker.submit(runner.execute, recording["run_id"]).result(timeout=45)
            self.assertEqual(replay["status"], "Pass", replay)
            case_id = recording["ui_case"]["id"]
            self.assertEqual(replay["publication"]["published_ids"], [case_id])
            case = projects.case(self.pid, case_id)
            self.assertEqual(case["module"], "会话服务")
            self.assertEqual(case["extensions"]["test_case_document"]["module"], row["module"])
            self.assertEqual(case["extensions"]["test_case_document"]["business_path"], row["business_path"])
            self.assertNotIn("error", plan.link(identity, self.pid, case_id, 1))
            binding = json.loads((Path(saved["folder"]) / "design.json").read_bytes())["rows"][identity]["binding"]
            self.assertTrue(binding["design_mapped"])
            self.assertEqual(binding["case_hash"], execution_inputs.content_hash(case))
        folders = case_folders.list_folders(self.pid)
        leaf = next(item for item in folders["folders"] if item["name"] == "会话状态")
        self.assertEqual(folders["assignments"][case_id], leaf["id"])
        self.assertEqual(execution_inputs.content_hash(projects.case(self.pid, case_id)), binding["case_hash"])

    def design_row(self, identity, *, task="功能设计会话"):
        response = self.client.get(f"/api/case-plans/{self.pid}", params={"session": task})
        self.assertEqual(response.status_code, 200, response.text)
        return next(item for item in response.json()["rows"] if item["id"] == identity)

    def import_rows(self, identities, *, task="功能设计会话"):
        self.open(f"case-plans?project={self.pid}&session={task}")
        self.page.get_by_role("button", name="从已有用例导入", exact=True).click()
        dialog = self.page.get_by_role("dialog", name="从已有可执行用例导入设计", exact=True)
        for identity in identities:
            dialog.get_by_role("checkbox", name="导入 " + identity, exact=True).check()
        dialog.get_by_role("button", name="导入为设计", exact=True).click()
        expect(dialog).not_to_be_visible()
        expect(self.page.locator("#view table tbody tr")).to_have_count(len(identities))

    def wait_run(self, identity):
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            result = self.client.get("/api/runs/" + identity).json()
            if result["status"] not in ("Queued", "Running") and (result.get("test_report") or {}).get("status") == "ready":
                return result
            time.sleep(0.1)
        self.fail("The actual replay did not finish")
        return None

    def workbook_row(self, data, identity):
        book = openpyxl.load_workbook(io.BytesIO(data))
        self.addCleanup(book.close)
        alias = next(row[0] for row in book["编号映射"].iter_rows(min_row=2, values_only=True) if row[1] == identity)
        return next(row for row in book["功能测试用例"].iter_rows(min_row=2, values_only=True) if row[0] == alias)

    def test_actual_record_plan_carries_the_authored_functional_design_into_frozen_replay(self):
        task, identity = "真实录制设计", "DESIGN-SESSION"
        source = {"kind": "synthetic_requirement", "id": "SESSION-READY", "text": "点击验证会话后，当前浏览器会话校验成功时显示通过。"}
        document = {"id": identity, "title": "验证当前会话校验结果", "module": "浏览器会话/当前会话校验",
                    "description": "验证点击会话校验后展示当前会话的校验结果。", "preconditions": "本地合成页面可访问，当前浏览器已接收会话Cookie。",
                    "steps": "1. 进入合成会话校验页面。\n2. 点击【验证会话】。\n3. 查看本次会话校验结果。",
                    "expected": "3. 当前会话校验成功，页面显示“通过”。", "source": source,
                    "author": "集成验收脚本（合成）", "remarks": "预期来自合成需求声明，不从现场结果反推。"}
        current = self.client.get(f"/api/case-plans/{self.pid}", params={"session": task}).json()
        saved = self.client.post(f"/api/case-plans/{self.pid}/rows", params={"session": task},
                                 json={"row": document, "expected_revision": current["revision"]})
        self.assertEqual(saved.status_code, 200, saved.text)
        with task_context.use_session(task), plan.use_folder(saved.json()["folder"]):
            try:
                self.assertTrue(session.start(self.pid, login=False, url="/ui")["recording"])
                session.browser(["click", "[data-testid=check]"])
                self.assertTrue(session.check("text", "testid=result", "通过")["passed"])
                recording = session.stop("记录当前会话校验", plan_case=identity)
            finally:
                session.cancel()
        case_id = recording["ui_case"]["id"]
        case = projects.case(self.pid, case_id)
        declared = case["extensions"]["test_case_document"]
        self.assertEqual(case["steps"], document["steps"].splitlines())
        self.assertEqual(case["expected"], document["expected"])
        self.assertEqual(declared["description"], document["description"])
        self.assertEqual(declared["source"]["case_id"], identity)
        self.assertEqual(declared["source"]["upstream"], source)
        self.assertEqual(declared["business_path"], self.design_row(identity, task=task)["business_path"])
        recording_report = task_reports.get(recording["test_report"]["report_id"])
        exported = self.workbook_row(Path(recording_report["excel"]).read_bytes(), case_id)
        self.assertEqual(exported[3:7], (document["description"], document["preconditions"], document["steps"], document["expected"]))
        self.assertNotIn("testid=", exported[5])
        before = self.design_row(identity, task=task)
        self.assertNotEqual(before["status"], "Pass")
        self.assertTrue(before["evidence"]["binding_current"], before)
        self.open(f"case-plans?project={self.pid}&session={task}")
        self.page.get_by_role("button", name="回放", exact=True).click()
        expect(self.page).to_have_url(re.compile(r"/#/runs/run-"))
        run_id = self.page.url.split("/runs/")[1].split("?")[0]
        completed = self.wait_run(run_id)
        self.assertEqual(completed["status"], "Pass", completed)
        evidence = self.design_row(identity, task=task)
        self.assertEqual(evidence["status"], "Pass", evidence)
        frozen = execution_inputs.load(db.one("SELECT * FROM runs WHERE id = ?", (run_id,)))
        self.assertEqual(frozen["cases"][case_id]["extensions"]["test_case_document"], declared)
        self.open(f"case-plans?project={self.pid}&session={task}")
        expect(self.page.get_by_role("heading", name="设计用例表", exact=True)).to_be_visible()
        expect(self.page.locator("#view")).to_contain_text(document["description"])
        expect(self.page.locator("#view")).to_contain_text(document["steps"])
        self.evidence("recorded-functional-design-pass.png")

    def test_legacy_design_edit_preserves_structured_source_and_invalidates_old_pass(self):
        source = {"kind": "synthetic_requirement", "id": "READY-TRUE", "text": "请求完成后，服务以 ok=true 表示就绪。"}
        legacy = projects.case(self.pid, "TC-SLOW")
        legacy["steps"], legacy["expected"] = ["GET /slow"], "HTTP 200"
        legacy["extensions"]["test_case_document"] = {"description": "验证服务就绪标记", "source": source,
                                                       "business_path": ["负载验证", "服务检查", "就绪状态"], "prefix": "JXZT"}
        projects.save_cases(self.pid, [legacy])
        failed = runner.create(self.pid, ["TC-CHANGING"])
        self.assertEqual(runner.execute(failed["id"])["status"], "Fail")
        passed = runner.create(self.pid, ["TC-SLOW"])
        passed_run = runner.execute(passed["id"])
        self.assertEqual(passed_run["status"], "Pass")
        old_report = Path(passed_run["test_report"]["excel"])
        old_report_hash = hashlib.sha256(old_report.read_bytes()).hexdigest()
        old_case = projects.case(self.pid, "TC-SLOW")
        old_result = db.one("SELECT * FROM results WHERE run_id = ?", (passed["id"],))
        self.import_rows(["TC-SLOW", "TC-CHANGING", "TC-UI"])
        imported = self.design_row("TC-SLOW")
        self.assertEqual(imported["source"], source)
        self.assertEqual(imported["status"], "Unverified")
        self.assertEqual(self.design_row("TC-CHANGING")["status"], "Fail")
        self.assertIn("待确认", imported["steps"])
        self.assertIn("待确认", self.design_row("TC-UI")["steps"])
        self.assertNotIn("GET /slow", imported["steps"])
        row = self.page.locator("#view table tbody tr").filter(has_text="TC-SLOW")
        row.get_by_role("button", name="编辑设计", exact=True).click()
        dialog = self.page.get_by_role("dialog", name="编辑设计：TC-SLOW", exact=True)
        self.assertEqual(json.loads(dialog.get_by_label("设计来源", exact=True).input_value()), source)
        human = {"测试说明": "验证服务完成请求后返回业务就绪标记。", "前置条件": "本地合成就绪检查服务已启动。",
                 "操作步骤": "1. 查询服务就绪状态。\n2. 查看响应中的就绪标记。", "预期结果": "2. 就绪标记 ok 为 true。",
                 "用例编制人": "集成验收脚本（合成）", "设计备注": "保留声明的 true 判据及原始结构化来源。"}
        for label, value in human.items():
            dialog.get_by_label(label, exact=True).fill(value)
        dialog.get_by_role("button", name="保存设计", exact=True).click()
        expect(dialog).not_to_be_visible()
        self.assertEqual(self.design_row("TC-SLOW")["source"], source)
        with self.page.expect_response(lambda response: response.request.method == "POST" and "/rows/TC-SLOW/apply" in response.url) as response:
            row.get_by_role("button", name="应用设计到用例", exact=True).click()
        self.assertEqual(response.value.status, 200, response.value.text())
        expect(self.page.locator("#view")).to_contain_text(human["测试说明"])
        applied = projects.case(self.pid, "TC-SLOW")
        self.assertGreater(applied["revision"], old_case["revision"])
        self.assertEqual(applied["steps"], human["操作步骤"].splitlines())
        self.assertEqual(applied["expected"], human["预期结果"])
        self.assertEqual(applied["extensions"]["test_case_document"]["source"], source)
        self.assertEqual((applied["method"], applied["path"], applied["assertions"]), (old_case["method"], old_case["path"], old_case["assertions"]))
        self.assertFalse(execution_inputs.result_current(self.pid, applied, old_result))
        self.assertNotEqual(self.design_row("TC-SLOW")["status"], "Pass")
        row.get_by_role("button", name="回放", exact=True).click()
        expect(self.page).to_have_url(re.compile(r"/#/runs/run-"))
        run_id = self.page.url.split("/runs/")[1].split("?")[0]
        completed = self.wait_run(run_id)
        detail = db.one("SELECT detail FROM results WHERE run_id = ? AND case_id = ?", (run_id, "TC-SLOW"))
        self.assertEqual(json.loads(detail["detail"])["business_status"], "Unverified")
        self.assertNotEqual(self.design_row("TC-SLOW")["status"], "Pass")
        self.assertEqual(completed["test_report"]["status"], "ready")
        self.assertEqual(db.one("SELECT status FROM runs WHERE id = ?", (failed["id"],))["status"], "Fail")
        self.assertEqual(hashlib.sha256(old_report.read_bytes()).hexdigest(), old_report_hash)
        self.open(f"case-plans?project={self.pid}&session=功能设计会话")
        expect(self.page.get_by_role("heading", name="设计用例表", exact=True)).to_be_visible()
        self.evidence("updated-functional-design-unverified.png")
