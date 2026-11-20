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

from autoskill import case_folders, case_plans, cli, db, execution_inputs, plan, projects, runner, task_context, task_reports
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

    def wait_run(self, identity, *, publication=False):
        args = cli.parser().parse_args(["run-status", identity, "--brief"])
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            result, code = cli.dispatch(args)
            self.assertEqual(code, 0, result)
            if (result["status"] not in ("Queued", "Running") and result["test_report"]["status"] == "ready"
                    and (not publication or result["publication"]["complete"])):
                return result
            time.sleep(0.1)
        self.fail(f"The actual replay did not deliver its report or publication receipt: {result}")
        return None

    def replay_from_library(self, identity):
        self.open("cases?range=all")
        row = self.page.get_by_role("row").filter(has=self.page.get_by_role("checkbox", name="选择 " + identity, exact=True))
        row.get_by_role("button", name="运行", exact=True).click()
        expect(self.page).to_have_url(re.compile(r"/#/runs/run-"))
        return self.wait_run(self.page.url.split("/runs/")[1].split("?")[0])

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
        design = self.folder / "session-design.json"
        design.write_text(json.dumps([document], ensure_ascii=False), encoding="utf-8")
        with task_context.use_session(task):
            written, code = cli.dispatch(cli.parser().parse_args(["plan", "write", self.pid, "--file", str(design)]))
            self.assertEqual(code, 0, written)
            initial_book = Path(written["initial_excel"])
            initial_hash = hashlib.sha256(initial_book.read_bytes()).hexdigest()
            try:
                self.assertTrue(session.start(self.pid, login=False, url="/ui")["recording"])
                session.browser(["click", "[data-testid=check]"])
                self.assertTrue(session.check("text", "testid=result", "通过")["passed"])
                recording = session.stop("记录当前会话校验", plan_case=identity)
            finally:
                session.cancel()
        case_id = recording["ui_case"]["id"]
        validation = self.wait_run(recording["run_id"], publication=True)
        self.assertEqual(validation["status"], "Pass", validation)
        self.assertIn(case_id, validation["publication"]["published_ids"], validation["publication"])
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
        evidence = self.design_row(identity, task=task)
        self.assertEqual(evidence["status"], "Pass", evidence)
        self.assertTrue(evidence["evidence"]["binding_current"], evidence)
        self.assertEqual(evidence["evidence"]["run_id"], validation["run_id"])
        completed = self.replay_from_library(case_id)
        self.assertEqual(completed["status"], "Pass", completed)
        self.assertNotEqual(completed["run_id"], validation["run_id"])
        for run_id in (validation["run_id"], completed["run_id"]):
            frozen = execution_inputs.load(db.one("SELECT * FROM runs WHERE id = ?", (run_id,)))
            self.assertEqual(frozen["cases"][case_id]["extensions"]["test_case_document"], declared)
        self.assertEqual(hashlib.sha256(initial_book.read_bytes()).hexdigest(), initial_hash)
        self.open(f"case-plans?project={self.pid}&session={task}")
        expect(self.page.get_by_role("heading", name="测试用例表", exact=True)).to_be_visible()
        row = self.page.locator(f'[data-case="{identity}"]')
        expect(row.get_by_role("heading", name=document["description"], exact=True)).to_be_visible()
        row.locator("summary").click()
        expect(row.get_by_text(document["steps"], exact=True)).to_be_visible()
        expect(row.get_by_text(document["expected"], exact=True)).to_be_visible()
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
        base = f"/api/case-plans/{self.pid}"
        params = {"session": "功能设计会话"}
        current = self.client.get(base, params=params).json()
        imported_sheet = self.client.post(base + "/import-cases", params=params, json={
            "case_ids": ["TC-SLOW", "TC-CHANGING", "TC-UI"], "expected_revision": current["revision"]})
        self.assertEqual(imported_sheet.status_code, 200, imported_sheet.text)
        imported = self.design_row("TC-SLOW")
        self.assertEqual(imported["source"], source)
        self.assertEqual((imported["status"], imported["evidence"]["run_id"]), ("Pass", passed["id"]))
        self.assertEqual(self.design_row("TC-CHANGING")["status"], "Fail")
        self.assertIn("待确认", imported["steps"])
        self.assertIn("待确认", self.design_row("TC-UI")["steps"])
        self.assertNotIn("GET /slow", imported["steps"])
        human = {"description": "验证服务完成请求后返回业务就绪标记。", "preconditions": "本地合成就绪检查服务已启动。",
                 "steps": "1. 查询服务就绪状态。\n2. 查看响应中的就绪标记。", "expected": "2. 就绪标记 ok 为 true。",
                 "author": "集成验收脚本（合成）", "remarks": "保留声明的 true 判据及原始结构化来源。"}
        saved = self.client.patch(base + "/rows/TC-SLOW", params=params, json={
            "row": human, "expected_revision": imported_sheet.json()["revision"]})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(self.design_row("TC-SLOW")["source"], source)
        archive = self.client.get(base + "/history/" + str(imported_sheet.json()["version"]), params=params)
        self.assertEqual(archive.status_code, 200, archive.text)
        original = next(row for row in archive.json()["rows"] if row["id"] == "TC-SLOW")
        self.assertEqual((original["source"], original["steps"]), (source, imported["steps"]))
        response = self.client.post(base + "/rows/TC-SLOW/apply", params=params, json={"expected_revision": saved.json()["revision"]})
        self.assertEqual(response.status_code, 200, response.text)
        applied = projects.case(self.pid, "TC-SLOW")
        self.assertGreater(applied["revision"], old_case["revision"])
        self.assertEqual(applied["steps"], human["steps"].splitlines())
        self.assertEqual(applied["expected"], human["expected"])
        self.assertEqual(applied["extensions"]["test_case_document"]["source"], source)
        for field in ("description", "preconditions", "author", "remarks"):
            self.assertEqual(applied["extensions"]["test_case_document"][field], human[field])
        self.assertEqual((applied["method"], applied["path"], applied["assertions"]), (old_case["method"], old_case["path"], old_case["assertions"]))
        self.assertFalse(execution_inputs.result_current(self.pid, applied, old_result))
        self.assertEqual(self.design_row("TC-SLOW")["status"], "NotRun")
        completed = self.replay_from_library("TC-SLOW")
        run_id = completed["run_id"]
        detail = db.one("SELECT detail FROM results WHERE run_id = ? AND case_id = ?", (run_id, "TC-SLOW"))
        self.assertEqual(json.loads(detail["detail"])["business_status"], "Unverified")
        updated = self.design_row("TC-SLOW")
        self.assertEqual((updated["status"], updated["evidence"]["run_id"]), ("Pass", run_id))
        self.assertEqual(completed["test_report"]["status"], "ready")
        self.assertEqual(db.one("SELECT status FROM runs WHERE id = ?", (failed["id"],))["status"], "Fail")
        self.assertEqual(hashlib.sha256(old_report.read_bytes()).hexdigest(), old_report_hash)
        self.open(f"case-plans?project={self.pid}&session=功能设计会话")
        row = self.page.locator('[data-case="TC-SLOW"]')
        expect(row.get_by_role("heading", name=human["description"], exact=True)).to_be_visible()
        row.locator("summary").click()
        expect(row.get_by_text(human["steps"], exact=True)).to_be_visible()
        expect(row.get_by_text(source["text"], exact=True)).to_be_visible()
        self.evidence("updated-functional-design-unverified.png")
