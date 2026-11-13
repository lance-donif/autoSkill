"""Exercise nested case folders and local diagnostics against the running dashboard."""
import io
import json
import time
import uuid
import zipfile
from pathlib import Path

import openpyxl
from dashboard_revision_fixture import change_project_and_wait_for_ui
from playwright.sync_api import expect
from test_load_dashboard import DashboardWorkspace

from autoskill import builder, execution_inputs, projects, runner, workflow
from autoskill.core import store


class FeatureDashboardTests(DashboardWorkspace):
    def create_folder(self, name, parent=None):
        self.page.get_by_role("button", name="新建文件夹", exact=True).first.click()
        dialog = self.page.get_by_role("dialog", name="新建文件夹", exact=True)
        dialog.get_by_label("文件夹名称", exact=True).fill(name)
        if parent:
            dialog.get_by_label("上级文件夹", exact=True).select_option(parent)
        with self.page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/folders")) as response:
            dialog.get_by_role("button", name="创建文件夹", exact=True).click()
        self.assertEqual(response.value.status, 200, response.value.text())
        expect(dialog).not_to_be_visible()
        tree = self.client.get(f"/api/projects/{self.pid}/folders").json()
        return next(folder["id"] for folder in tree["folders"] if folder["name"] == name)

    def test_nested_folders_move_and_recursive_excel_export_survive_reload(self):
        original = self.client.get(f"/api/projects/{self.pid}/cases/TC-CHECK").json()
        self.open("cases?range=all")
        parent = self.create_folder("订单测试")
        child = self.create_folder("接口回归", parent)
        self.page.get_by_role("button", name="移动用例 TC-CHECK", exact=True).click()
        dialog = self.page.get_by_role("dialog", name="移动用例", exact=True)
        dialog.get_by_label(f"「{self.pid}」目标文件夹", exact=True).select_option(child)
        dialog.get_by_role("button", name="移动用例", exact=True).click()
        expect(dialog).not_to_be_visible()
        tree = self.client.get(f"/api/projects/{self.pid}/folders").json()
        self.assertEqual(tree["assignments"]["TC-CHECK"], child)
        self.assertEqual(next(folder["parent_id"] for folder in tree["folders"] if folder["id"] == child), parent)
        self.page.reload()
        expect(self.page.get_by_role("button", name="接口回归，1 条用例", exact=True)).to_be_visible()
        self.page.get_by_role("button", name="接口回归，1 条用例", exact=True).click()
        expect(self.page.get_by_text("接口会话校验", exact=True)).to_be_visible()
        expect(self.page.get_by_text("可取消慢请求", exact=True)).not_to_be_visible()
        self.evidence("nested-folders.png")
        self.assertEqual(self.client.get(f"/api/projects/{self.pid}/cases/TC-CHECK").json(), original)
        self.page.get_by_role("button", name="订单测试，0 条用例", exact=True).click()
        with self.page.expect_download() as downloaded:
            self.page.get_by_role("link", name="导出文件夹 Excel", exact=True).click()
        workbook = openpyxl.load_workbook(io.BytesIO(Path(downloaded.value.path()).read_bytes()))
        self.addCleanup(workbook.close)
        mapped = list(workbook["编号映射"].values)
        self.assertEqual({row[1] for row in mapped[1:]}, {"TC-CHECK"})
        with self.page.expect_download() as downloaded_all:
            self.page.get_by_role("link", name="导出 Excel", exact=True).click()
        all_book = openpyxl.load_workbook(io.BytesIO(Path(downloaded_all.value.path()).read_bytes()))
        self.addCleanup(all_book.close)
        all_mapped = list(all_book["编号映射"].values)
        self.assertEqual({row[1] for row in all_mapped[1:]}, {"TC-BEGIN", "TC-CHECK", "TC-SLOW", "TC-CHANGING", "TC-UI"})

    def test_diagnostics_run_real_checks_and_show_missing_runtime_honestly(self):
        self.open("diagnostics")
        self.page.get_by_role("button", name="检查运行环境", exact=True).click()
        expect(self.page.get_by_role("table", name="Python 依赖状态", exact=True)).to_be_visible(timeout=15000)
        expect(self.page.get_by_role("table", name="Python 依赖状态", exact=True)).to_contain_text("playwright")
        self.page.get_by_label("选择执行器", exact=True).select_option("ai-eval")
        self.page.get_by_role("button", name="检查执行器", exact=True).click()
        table = self.page.get_by_role("table", name="执行器检查结果", exact=True)
        expect(table).to_contain_text("ai-eval", timeout=15000)
        expect(table).to_contain_text("可用")
        self.page.get_by_role("button", name="校验离线包文件", exact=True).click()
        expect(self.page.get_by_role("alert").filter(has_text="运行时清单")).to_be_visible(timeout=15000)
        expect(self.page.get_by_role("button", name="校验离线包文件", exact=True)).to_be_enabled()
        self.evidence("diagnostics.png")

    def test_knowledge_materials_download_and_uploaded_share_use_real_files(self):
        self.open("project-materials?project=" + self.pid)
        self.page.get_by_role("button", name="新建知识", exact=True).first.click()
        dialog = self.page.get_by_role("dialog", name="新建知识", exact=True)
        dialog.get_by_label("文件名", exact=True).fill("订单/退款规则.md")
        dialog.get_by_label("内容", exact=True).fill("退款按原支付渠道退回。")
        dialog.get_by_role("button", name="保存", exact=True).click()
        expect(dialog).not_to_be_visible()
        knowledge = projects.path(self.pid) / "knowledge/订单/退款规则.md"
        self.assertEqual(knowledge.read_text(), "退款按原支付渠道退回。")
        self.page.get_by_role("row").filter(has_text="订单/退款规则.md").get_by_role("button", name="查看 / 编辑", exact=True).click()
        edit = self.page.get_by_role("dialog", name="查看与编辑知识", exact=True)
        expect(edit.get_by_label("内容", exact=True)).to_have_value("退款按原支付渠道退回。")
        edit.get_by_label("内容", exact=True).fill("退款按原支付渠道退回，保留订单号。")
        edit.get_by_role("button", name="保存", exact=True).click()
        expect(edit).not_to_be_visible()
        self.assertEqual(knowledge.read_text(), "退款按原支付渠道退回，保留订单号。")
        run = runner.create(self.pid, ["TC-SLOW"], headed=False)
        completed = runner.execute(run["id"])
        self.assertEqual(completed["status"], "Pass", completed)
        self.assertTrue(Path(completed["test_report"]["excel"]).is_relative_to(projects.path(self.pid) / "reports"))
        self.page.get_by_role("button", name="导出全部项目资料", exact=True).click()
        with self.page.expect_download() as exported:
            self.page.get_by_role("link", name="下载项目资料 ZIP", exact=True).click()
        with zipfile.ZipFile(exported.value.path()) as archive:
            self.assertEqual(archive.read(self.pid + "/knowledge/订单/退款规则.md").decode(), knowledge.read_text())
            manifest = json.loads(archive.read("materials-manifest.json"))
            self.assertTrue(any("/testcases/" in name for name in manifest["files"]))
            self.assertTrue(any("/reports/" in name for name in manifest["files"]))
        self.page.get_by_role("button", name="分享与导入", exact=True).click()
        self.page.get_by_role("button", name="生成分享包", exact=True).click()
        receipt = self.page.get_by_role('link', name='下载分享包', exact=True).element_handle()
        with self.page.expect_download() as shared:
            self.page.get_by_role("link", name="下载分享包", exact=True).click()
        package = Path(shared.value.path()).read_bytes()
        self.assertTrue(zipfile.is_zipfile(io.BytesIO(package)))
        self.page.get_by_label("从这台设备选择 ZIP", exact=True).set_input_files({"name": "project-share.zip", "mimeType": "application/zip", "buffer": package})
        category = change_project_and_wait_for_ui(self, self.pid)
        self.assertTrue(receipt.evaluate('node => node.isConnected'))
        expect(self.page.get_by_role('link', name='下载分享包', exact=True)).to_be_visible()
        self.assertEqual(self.page.get_by_label('从这台设备选择 ZIP', exact=True).evaluate(
            'node => [...node.files].map(file => ({name: file.name, size: file.size}))'),
            [{'name': 'project-share.zip', 'size': len(package)}])
        self.page.get_by_role("button", name="导入项目分享包", exact=True).click()
        expect(self.page.get_by_text("已导入 0 个项目 · 1 个未导入", exact=True)).to_be_visible()
        self.assertEqual(self.client.get('/api/projects/' + self.pid).json()['category'], category)
        self.evidence("project-materials.png")

    def test_workflow_page_explains_twenty_of_hundred_and_creates_scoped_maintenance(self):
        project = projects.create("需求变更页面验收", f"http://localhost:{self.target.server_port}", project_id="workflow-dashboard")["id"]
        store.write(projects.path(project) / "analysis/requirements.json", [
            {"id": identity, "module": "业务", "title": "固定需求", "source": "独立需求"} for identity in ("REQ-CHANGED", "REQ-STABLE")])
        cases = []
        for number in range(100):
            case = builder.api_case(f"CASE-{number:03}", f"业务规则 {number}", "GET", "/slow", {"status": 200, "json_equals": {"/ok": True}})
            case["requirement_ids"] = ["REQ-CHANGED" if number < 20 else "REQ-STABLE"]
            cases.append(case)
        projects.save_cases(project, cases)
        baseline = {case["id"]: execution_inputs.canonical(case) for case in projects.cases(project)}
        identity = workflow.create(project, {"title": "原始需求", "sources": [{"id": "changed", "text": "前20项规则原版本"},
                                                                                  {"id": "stable", "text": "其余80项保持原规则"}]},
                                   idempotency_key=uuid.uuid4().hex)["workflow_id"]
        self.submit_stage(identity, {"requirements": [
            {"id": "REQ-CHANGED", "text": "前20项规则", "source_ids": ["changed"]},
            {"id": "REQ-STABLE", "text": "其余80项规则", "source_ids": ["stable"]}]})
        self.submit_stage(identity, {"scenarios": [
            {"id": "SC-CHANGED", "title": "变更规则场景", "requirement_ids": ["REQ-CHANGED"], "oracle_ids": ["O-CHANGED"]},
            {"id": "SC-STABLE", "title": "稳定规则场景", "requirement_ids": ["REQ-STABLE"], "oracle_ids": ["O-STABLE"]}],
            "oracles": [{"id": "O-" + suffix, "kind": "api", "requirement_ids": ["REQ-" + suffix],
                         "checks": {"status": 200, "json_equals": {"/ok": True}}} for suffix in ("CHANGED", "STABLE")]})
        self.open("workflows/" + identity)
        self.page.get_by_role("button", name="需求迭代", exact=True).click()
        dialog = self.page.get_by_role("dialog", name="需求迭代与影响分析", exact=True)
        dialog.get_by_label("修改的需求来源", exact=True).select_option("changed")
        dialog.get_by_label("新需求正文", exact=True).fill("前20项规则调整为新版本，其余规则保持不变。")
        with self.page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/impact")) as analyzed:
            dialog.get_by_role("button", name="分析影响", exact=True).click()
        analysis = analyzed.value.json()
        self.assertEqual(analyzed.value.status, 200, analysis)
        self.assertEqual(analysis["direct_case_ids"], [f"CASE-{number:03}" for number in range(20)])
        self.assertEqual(len(analysis["unaffected_case_ids"]), 80)
        expect(dialog.get_by_role("heading", name="直接影响 20 条 · 依赖波及 0 条", exact=True)).to_be_visible()
        self.assertEqual(sum(dialog.get_by_role("checkbox").evaluate_all("nodes => nodes.map(node => node.checked)")), 20)
        expect(dialog).to_contain_text("需求关联：REQ-CHANGED")
        self.evidence("workflow-impact.png")
        with self.page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/maintenance")) as maintained:
            dialog.get_by_role("button", name="维护所选用例", exact=True).click()
        made = maintained.value.json()
        self.assertEqual(maintained.value.status, 200, made)
        self.assertEqual(made["selected_case_ids"], analysis["direct_case_ids"])
        expect(self.page.get_by_text("需求维护：已固定 20 条用例", exact=True)).to_be_visible()
        self.assertEqual({case["id"]: execution_inputs.canonical(case) for case in projects.cases(project)}, baseline)
        self.page.get_by_role("button", name="暂停", exact=True).click()
        expect(self.page.get_by_role("button", name="恢复", exact=True)).to_be_visible()
        self.page.get_by_role("button", name="恢复", exact=True).click()
        expect(self.page.get_by_role("button", name="暂停", exact=True)).to_be_visible()
        self.page.get_by_role("button", name="交给 AI 处理", exact=True).click()
        dispatch = self.page.get_by_role("dialog", name="交给 AI 处理", exact=True)
        dispatch.get_by_role("button", name="投递给 AI", exact=True).click()
        expect(dispatch).not_to_be_visible()
        expect(self.page.get_by_text("pending", exact=False).first).to_be_visible()
        maintenance_id = made["workflow_id"]
        self.review_stage("需求拆分", {"requirements": [
            {"id": "REQ-NEW", "text": "前20项按新版业务规则核验ok为true", "source_ids": ["changed"]}]})
        self.review_stage("用例设计", {"scenarios": [
            {"id": "SC-NEW", "title": "新版业务规则验证", "requirement_ids": ["REQ-NEW"], "oracle_ids": ["O-NEW"]}],
            "oracles": [{"id": "O-NEW", "kind": "api", "requirement_ids": ["REQ-NEW"],
                         "checks": {"status": 200, "json_equals": {"/ok": True}}}]})
        candidates = []
        for number in range(20):
            candidate = projects.case(project, f"CASE-{number:03}")
            candidate.update(revision=2, title=candidate["title"] + " V2", requirement_ids=["REQ-NEW"])
            candidates.append(candidate)
        self.review_stage("候选用例", {"cases": candidates, "bindings": {"SC-NEW": analysis["direct_case_ids"]}})
        self.assertEqual({case["id"]: execution_inputs.canonical(case) for case in projects.cases(project)}, baseline)
        self.page.get_by_role("button", name="刷新执行与恢复状态", exact=True).click()
        deadline = time.monotonic() + 30
        run_id = None
        while time.monotonic() < deadline:
            current = self.client.get("/api/workflow-dashboard/" + maintenance_id).json()
            run_id = next((item["run_id"] for item in current["items"] if item["phase"] == "execute" and item["run_id"]), None)
            if run_id and self.client.get("/api/runs/" + run_id).json()["status"] not in ("Queued", "Running"):
                break
            time.sleep(0.1)
        self.assertIsNotNone(run_id)
        result = self.client.get("/api/runs/" + run_id).json()
        self.assertEqual((result["status"], result["total"], result["passed"]), ("Pass", 20, 20), result)
        self.page.get_by_role("button", name="刷新执行与恢复状态", exact=True).click()
        expect(self.page.get_by_text("候选已发布", exact=True).first).to_be_visible()
        for number in range(100):
            current = projects.case(project, f"CASE-{number:03}")
            if number < 20:
                self.assertTrue(current["title"].endswith(" V2"))
            else:
                self.assertEqual(execution_inputs.canonical(current), baseline[current["id"]])
        self.evidence("workflow-published.png")
        self.review_stage("知识整理", {"entries": [{"id": "validated", "text": "本次20项新版规则真实回归通过", "evidence_refs": [run_id]}]})
        self.assertTrue(self.client.get("/api/workflow-dashboard/" + maintenance_id).json()["workflow"]["complete"])

    @staticmethod
    def submit_stage(identity, payload):
        task = workflow.next_task(identity, idempotency_key=uuid.uuid4().hex)["task"]
        return workflow.submit(identity, task["item_id"], lease_token=task["lease_token"], attempt_id=task["attempt_id"],
                               idempotency_key=uuid.uuid4().hex, payload=payload)

    def review_stage(self, label, payload):
        self.page.get_by_role("button", name="审阅并处理下一阶段", exact=True).click()
        dialog = self.page.get_by_role("dialog", name="审阅：" + label, exact=True)
        dialog.get_by_label("阶段结果（JSON）", exact=True).fill(json.dumps(payload, ensure_ascii=False))
        with self.page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/submit")) as submitted:
            dialog.get_by_role("button", name="提交审阅结果", exact=True).click()
        self.assertEqual(submitted.value.status, 200, submitted.value.text())
        expect(dialog).not_to_be_visible()
