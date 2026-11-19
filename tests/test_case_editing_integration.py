"""Real saves preserve old evidence and require new manual browser/API execution."""
import copy
import hashlib
import json
import time
from pathlib import Path

import openpyxl
from locator_acceptance_fixture import LocatorSite
from playwright.sync_api import expect
from test_load_dashboard import DashboardWorkspace

from autoskill import builder, case_document, data_refs, db, execution_inputs, projects, task_reports


class CaseEditingIntegrationTests(DashboardWorkspace):
    def setUp(self):
        self.locator_site = LocatorSite().__enter__()
        self.addCleanup(self.locator_site.__exit__, None, None, None)
        super().setUp()

    def definition(self, project_id, case_id):
        response = self.client.get(f"/api/case-definitions/{project_id}/{case_id}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    @staticmethod
    def draft(definition):
        case = copy.deepcopy(definition["case"])
        for action in definition["editing"]["actions"]:
            case["extensions"]["automation"]["actions"][action["index"]]["_edit_id"] = action["id"]
        return case

    def save(self, project_id, definition, case):
        return self.client.put(f"/api/projects/{project_id}/cases/{case['id']}", json={
            "expected_content_revision": definition["content_revision"], "case": case,
        })

    def manual_run(self, project_id, case_id, engine="playwright"):
        response = self.client.post("/api/runs", json={"project_id": project_id, "case_ids": [case_id], "engine": engine, "headed": False})
        self.assertEqual(response.status_code, 200, response.text)
        identity = response.json()["runs"][0]["id"]
        deadline = time.monotonic() + 50
        while time.monotonic() < deadline:
            run = self.client.get("/api/runs/" + identity).json()
            if run["status"] not in ("Queued", "Running") and (run.get("test_report") or {}).get("status") == "ready":
                return run
            time.sleep(0.1)
        self.fail("Actual manually requested run/report did not finish: " + identity)
        return None

    def listing(self, project_id, case_id):
        response = self.client.get("/api/cases", params={"projects": project_id})
        self.assertEqual(response.status_code, 200, response.text)
        return next(item for item in response.json()["items"] if item["case_id"] == case_id)

    def frozen(self, run):
        receipt = task_reports.get(run["test_report"]["report_id"])
        folder = Path(receipt["excel"]).parent
        files = [file for file in folder.rglob("*") if file.is_file()]
        row = db.one("SELECT * FROM runs WHERE id = ?", (run["id"],))
        files.append(data_refs.resolve(row["input_manifest"]))
        result = db.one("SELECT * FROM results WHERE run_id = ?", (run["id"],))
        self.assertTrue(execution_inputs.result_evidence(row, result))
        return {file: hashlib.sha256(file.read_bytes()).hexdigest() for file in files}, result

    def unchanged_history(self, snapshot):
        files, result = snapshot
        for file, digest in files.items():
            self.assertEqual(hashlib.sha256(file.read_bytes()).hexdigest(), digest, str(file))
        self.assertEqual(db.one("SELECT * FROM results WHERE id = ?", (result["id"],)), result)

    def test_context_edit_requires_new_manual_dual_engine_runs_and_preserves_old_pass_and_cas(self):
        site, identity = self.locator_site, "TC-EDIT-CONTEXT"
        project_id = projects.create("编辑与定位集成", site.base_url, project_id="editing-context")["id"]
        projects.update(project_id, timeout_seconds=3, page_load_seconds=10)
        document = {"description": "验证保存项目名称后显示本次输入的名称。", "preconditions": "合成项目表单可以访问。",
                    "steps": ["1. 在【项目名称】中填写名称。", "2. 点击【保存】并查看保存结果。"],
                    "expected": "2. 保存成功并显示本次输入的项目名称。",
                    "source": {"kind": "synthetic_requirement", "id": "SAVE-NAME", "facts": ["按本次输入保存"]},
                    "business_path": ["项目管理", "项目设置", "名称维护"], "author": "集成验收脚本（合成）"}
        case = builder.ui_case(identity, "保存项目名称", site.automation("初始项目", mixed=False), test_case_document=document)
        case["extensions"]["unshown_metadata"] = {"nullable": None, "nested": [{"source_text": "不可丢失"}]}
        other = builder.api_case("TC-UNSELECTED", "独立旁例保持原样", "GET", "/hit", {"status": 200, "json_equals": {"/saved": True}})
        projects.save_cases(project_id, [case, other])
        other_before = copy.deepcopy(projects.case(project_id, other["id"]))
        original = self.manual_run(project_id, identity)
        self.assertEqual(original["status"], "Pass", original)
        old = self.frozen(original)
        self.assertEqual(self.listing(project_id, identity)["current_status"], "Pass")
        first = self.definition(project_id, identity)
        second = self.definition(project_id, identity)
        changed = self.draft(first)
        changed["title"] = "在嵌套表单保存项目名称"
        automation = site.automation("新版项目", force_xpath=True)
        for action, row in zip(automation["actions"], first["editing"]["actions"]):
            action["_edit_id"] = row["id"]
        automation["actions"].append({"assertion": "count", "context": site.context,
                                      "locators": [{"by": "css", "value": "#present"}], "expected": 1})
        changed["extensions"]["automation"] = automation
        runs_before = db.one("SELECT COUNT(*) n FROM runs")["n"]
        saved = self.save(project_id, first, changed)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertTrue(saved.json()["changed"])
        self.assertEqual(saved.json()["case"]["revision"], first["case"]["revision"] + 1)
        self.assertEqual(saved.json()["verification"]["status"], "NotRun")
        self.assertEqual(db.one("SELECT COUNT(*) n FROM runs")["n"], runs_before)
        self.assertEqual(site.hits, [{"where": "plain", "value": "初始项目"}])
        stored = projects.case(project_id, identity)
        self.assertEqual(stored["extensions"]["automation"], {**automation, "actions": [
            {key: value for key, value in step.items() if key != "_edit_id"} for step in automation["actions"]]})
        self.assertEqual(stored["extensions"]["unshown_metadata"], case["extensions"]["unshown_metadata"])
        self.assertEqual(stored["extensions"]["test_case_document"], document)
        self.assertEqual(projects.case(project_id, other["id"]), other_before)
        current = self.listing(project_id, identity)
        self.assertEqual(current["last_status"], "Pass")
        self.assertIsNone(current["current_status"])
        self.assertFalse(current["current"])
        self.assertFalse(execution_inputs.result_current(project_id, stored, old[1]))
        stale = self.draft(second)
        stale["title"] = "过期会话不应覆盖"
        conflict = self.save(project_id, second, stale)
        self.assertEqual(conflict.status_code, 409, conflict.text)
        self.assertEqual(conflict.json()["code"], "revision_conflict")
        self.assertEqual(projects.case(project_id, identity), stored)
        self.assertEqual(db.one("SELECT COUNT(*) n FROM runs")["n"], runs_before)
        self.open("cases?range=all")
        row = self.page.locator("#view tbody tr").filter(has_text=identity)
        expect(row.locator("td").nth(4)).to_contain_text("未执行")
        self.evidence("edited-context-awaits-manual-run.png")
        for engine in ("selenium", "playwright"):
            with self.subTest(engine=engine):
                completed = self.manual_run(project_id, identity, engine)
                self.assertEqual(completed["status"], "Pass", completed)
                result = db.one("SELECT * FROM results WHERE run_id = ?", (completed["id"],))
                detail = json.loads(result["detail"])
                self.assertGreaterEqual(detail["fallbacks"], 3)
                self.assertEqual(detail["business_status"], "Pass")
                self.assertTrue(execution_inputs.result_current(project_id, stored, result))
                frozen = execution_inputs.load(db.one("SELECT * FROM runs WHERE id = ?", (completed["id"],)))
                self.assertEqual(frozen["cases"][identity], stored)
                self.unchanged_history(old)
        self.assertEqual(site.hits, [{"where": "plain", "value": "初始项目"},
                                    {"where": "leaf", "value": "新版项目"}, {"where": "leaf", "value": "新版项目"}])

    def test_form_expected_edit_preserves_opaque_fields_and_keeps_old_checks_unverified(self):
        identity = "TC-SLOW"
        case = projects.case(self.pid, identity)
        source = {"kind": "synthetic_requirement", "id": "READY", "nested": {"facts": ["就绪标记为true"], "empty": None}}
        document = {"description": "验证服务完成查询后报告就绪状态。", "preconditions": "本地合成服务可访问。",
                    "steps": ["1. 查询服务就绪状态。", "2. 查看就绪标记。"], "expected": "2. 就绪标记为true。",
                    "source": source, "business_path": ["服务管理", "服务检查", "就绪状态"], "author": "集成验收脚本（合成）"}
        case_document.apply(case, document)
        case["extensions"]["unshown_metadata"] = {"nullable": None, "nested": [{"original": "保留此字段"}]}
        projects.save_cases(self.pid, [case])
        failure = self.manual_run(self.pid, "TC-CHANGING")
        self.assertEqual(failure["status"], "Fail", failure)
        failed_history = self.frozen(failure)
        original = self.manual_run(self.pid, identity)
        self.assertEqual(original["status"], "Pass", original)
        passed_history = self.frozen(original)
        before = self.definition(self.pid, identity)["case"]
        runs_before = db.one("SELECT COUNT(*) n FROM runs")["n"]
        self.open("cases?range=all")
        row = self.page.locator("#view tbody tr").filter(has_text=identity)
        row.get_by_role("button", name=f"编辑用例 {identity}", exact=True).click()
        dialog = self.page.get_by_role("dialog", name="编辑用例", exact=True)
        dialog.get_by_label("功能描述", exact=True).fill("验证服务就绪状态及新增的就绪说明。")
        changed_expected = "2. 就绪标记为true，且包含完整的就绪说明。"
        dialog.get_by_label("预期结果", exact=True).fill(changed_expected)
        with self.page.expect_response(lambda response: response.request.method == "PUT" and response.url.endswith("/cases/" + identity)) as response:
            dialog.get_by_role("button", name="保存修改", exact=True).click()
        self.assertEqual(response.value.status, 200, response.value.text())
        expect(dialog).not_to_be_visible()
        expect(row.locator("td").nth(4)).to_contain_text("未执行")
        self.assertEqual(db.one("SELECT COUNT(*) n FROM runs")["n"], runs_before)
        after = projects.case(self.pid, identity)
        self.assertEqual(after["expected"], changed_expected)
        self.assertEqual(after["extensions"]["test_case_document"]["source"], source)
        self.assertEqual(after["extensions"]["unshown_metadata"], before["extensions"]["unshown_metadata"])
        for key in ("method", "path", "assertions", "preconditions", "steps"):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(after["extensions"]["functional_design"]["state"], "unmapped")
        self.assertIsNone(self.listing(self.pid, identity)["current_status"])
        completed = self.manual_run(self.pid, identity)
        self.assertEqual(completed["status"], "Pass", completed)
        result = db.one("SELECT * FROM results WHERE run_id = ?", (completed["id"],))
        self.assertEqual(json.loads(result["detail"])["business_status"], "Unverified")
        self.assertEqual(self.listing(self.pid, identity)["current_status"], "Pass")
        receipt = task_reports.get(completed["test_report"]["report_id"])
        book = openpyxl.load_workbook(receipt["excel"], read_only=True, data_only=True)
        try:
            alias = next(row[0] for row in book["编号映射"].iter_rows(min_row=2, values_only=True) if row[1] == identity)
            exported = next(row for row in book["功能测试用例"].iter_rows(min_row=2, values_only=True) if row[0] == alias)
            self.assertEqual(exported[3:7], ("验证服务就绪状态及新增的就绪说明。", document["preconditions"],
                                            "\n".join(document["steps"]), changed_expected))
            self.assertEqual(exported[9], "通过")
        finally:
            book.close()
        self.unchanged_history(passed_history)
        self.unchanged_history(failed_history)
        # A direct API run is external to the open inspector; read fresh display data.
        self.page.reload()
        expect(self.page.locator("#view h1")).to_be_visible()
        row = self.page.locator("#view tbody tr").filter(has_text=identity)
        expect(row.locator("td").nth(4)).to_contain_text("通过")
        self.evidence("edited-functional-expected-remains-unverified.png")
