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
        self.page.get_by_role("button", name="移动用例 TC-SLOW", exact=True).click()
        dialog.get_by_label(f"「{self.pid}」目标文件夹", exact=True).select_option(label="不放入文件夹")
        dialog.get_by_role("button", name="移动用例", exact=True).click()
        expect(dialog).not_to_be_visible()
        self.page.set_viewport_size({"width": 390, "height": 844})
        self.page.reload()
        folders = self.page.get_by_role("navigation", name="页面压测验收文件夹")
        expect(folders.get_by_text("根目录", exact=True)).to_have_count(0)
        expect(self.page.get_by_role("row").filter(has_text="TC-SLOW")).to_be_visible()
        child_button = folders.get_by_role("button", name="接口回归，1 条用例", exact=True)
        expect(child_button).to_be_hidden()
        expand = folders.get_by_role("button", name="展开订单测试", exact=True)
        expand.focus()
        expand.press("Enter")
        expect(child_button).to_be_visible()
        collapse_all = folders.get_by_role("button", name="收起全部用例", exact=True)
        collapse_all.press("Space")
        expect(child_button).to_be_hidden()
        folders.get_by_role("button", name="展开全部用例", exact=True).press("Space")
        expect(child_button).to_be_visible()
        search = self.page.get_by_role("searchbox", name="搜索用例")
        search.fill("订单测试/接口回归")
        row = self.page.get_by_role("row").filter(has_text="TC-CHECK")
        expect(row).to_contain_text("订单测试 / 接口回归")
        expect(self.page.get_by_role("checkbox", name="选择 TC-SLOW", exact=True)).to_have_count(0)
        row.get_by_text("接口会话校验", exact=True).click()
        details = self.page.get_by_role("dialog", name="接口会话校验", exact=True)
        expect(details).to_contain_text("所在目录　订单测试 / 接口回归")
        expect(details.get_by_role("button", name="运行这条用例", exact=True)).to_be_enabled()
        details.get_by_role("button", name="关闭", exact=True).last.click()
        search.fill("")
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

    def test_workflow_page_observes_host_maintenance_and_preserves_history(self):
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
        for label in ("新建工作流", "需求迭代", "审阅并处理下一阶段", "交给 AI 处理"):
            expect(self.page.get_by_role("button", name=label, exact=True)).to_have_count(0)
        expect(self.page.locator("#view")).to_contain_text("DSH 对话")
        expect(self.page.get_by_text("前20项规则原版本", exact=True)).to_be_visible()
        self.assertEqual(self.page.locator("#view textarea").count(), 0)
        payload = {"sources": [{"id": "changed", "text": "前20项规则调整为新版本，其余规则保持不变。"}]}
        analyzed = self.client.post(f"/api/workflow-dashboard/{identity}/impact", json=payload)
        self.assertEqual(analyzed.status_code, 200, analyzed.text)
        analysis = analyzed.json()
        self.assertEqual(analysis["direct_case_ids"], [f"CASE-{number:03}" for number in range(20)])
        self.assertEqual(len(analysis["unaffected_case_ids"]), 80)
        maintained = self.client.post(f"/api/workflow-dashboard/{identity}/maintenance", json={
            **payload, "analysis_hash": analysis["analysis_hash"], "case_ids": analysis["direct_case_ids"], "idempotency_key": uuid.uuid4().hex})
        self.assertEqual(maintained.status_code, 200, maintained.text)
        made = maintained.json()
        self.assertEqual(made["selected_case_ids"], analysis["direct_case_ids"])
        self.open("workflows/" + made["workflow_id"])
        expect(self.page.get_by_text("需求维护：已固定 20 条用例", exact=True)).to_be_visible()
        self.assertEqual({case["id"]: execution_inputs.canonical(case) for case in projects.cases(project)}, baseline)
        self.page.get_by_role("button", name="暂停", exact=True).click()
        expect(self.page.get_by_role("button", name="恢复", exact=True)).to_be_visible()
        self.page.get_by_role("button", name="恢复", exact=True).click()
        expect(self.page.get_by_role("button", name="暂停", exact=True)).to_be_visible()
        maintenance_id = made["workflow_id"]
        self.submit_stage(maintenance_id, {"requirements": [
            {"id": "REQ-NEW", "text": "前20项按新版业务规则核验ok为true", "source_ids": ["changed"]}]})
        self.submit_stage(maintenance_id, {"scenarios": [
            {"id": "SC-NEW", "title": "新版业务规则验证", "requirement_ids": ["REQ-NEW"], "oracle_ids": ["O-NEW"]}],
            "oracles": [{"id": "O-NEW", "kind": "api", "requirement_ids": ["REQ-NEW"],
                         "checks": {"status": 200, "json_equals": {"/ok": True}}}]})
        candidates = []
        for number in range(20):
            candidate = projects.case(project, f"CASE-{number:03}")
            candidate.update(revision=2, title=candidate["title"] + " V2", requirement_ids=["REQ-NEW"])
            candidates.append(candidate)
        self.submit_stage(maintenance_id, {"cases": candidates, "bindings": {"SC-NEW": analysis["direct_case_ids"]}})
        self.assertEqual({case["id"]: execution_inputs.canonical(case) for case in projects.cases(project)}, baseline)
        self.client.post(f"/api/workflows/{maintenance_id}/recover", json={"idempotency_key": uuid.uuid4().hex}).raise_for_status()
        self.page.get_by_role("button", name="刷新状态", exact=True).click()
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
        self.client.post(f"/api/workflows/{maintenance_id}/recover", json={"idempotency_key": uuid.uuid4().hex}).raise_for_status()
        self.page.get_by_role("button", name="刷新状态", exact=True).click()
        expect(self.page.get_by_text("已入库 20 条", exact=True).first).to_be_visible()
        for number in range(100):
            current = projects.case(project, f"CASE-{number:03}")
            if number < 20:
                self.assertTrue(current["title"].endswith(" V2"))
            else:
                self.assertEqual(execution_inputs.canonical(current), baseline[current["id"]])
        self.evidence("workflow-published.png")
        expect(self.page.get_by_role("link", name="查看项目测试用例", exact=True)).to_be_visible()
        self.assertEqual(self.page.locator("#view textarea").count(), 0)

    def test_workflow_partial_publication_shows_real_counts_reasons_and_source_identity(self):
        source = self.folder / "订单需求.md"
        source.write_text("查询服务返回 ok=true；错误的独立预期必须失败并保留证据。", encoding="utf-8")
        identity = workflow.create(self.pid, {"title": "入库结果查阅", "sources": [{"id": "SOURCE", "file": str(source)}]},
                                   idempotency_key=uuid.uuid4().hex)["workflow_id"]
        self.submit_stage(identity, {"requirements": [{"id": "REQ", "text": "按独立预期核验返回值", "source_ids": ["SOURCE"]}]})
        self.submit_stage(identity, {
            "scenarios": [{"id": "SC-" + suffix, "title": suffix, "requirement_ids": ["REQ"], "oracle_ids": ["O-" + suffix]}
                          for suffix in ("PASS", "FAIL")],
            "oracles": [{"id": "O-" + suffix, "kind": "api", "requirement_ids": ["REQ"],
                         "checks": {"status": 200, "json_equals": {"/ok": value}}} for suffix, value in (("PASS", True), ("FAIL", False))]})
        candidates = []
        for suffix, expected in (("PASS", True), ("FAIL", False)):
            case = builder.api_case("PARTIAL-" + suffix, suffix, "GET", "/slow", {"status": 200, "json_equals": {"/ok": expected}})
            case["requirement_ids"] = ["REQ"]
            candidates.append(case)
        self.submit_stage(identity, {"cases": candidates, "bindings": {"SC-PASS": ["PARTIAL-PASS"], "SC-FAIL": ["PARTIAL-FAIL"]}})
        deadline = time.monotonic() + 30
        receipt = None
        while time.monotonic() < deadline:
            self.client.post(f"/api/workflows/{identity}/recover", json={"idempotency_key": uuid.uuid4().hex}).raise_for_status()
            current = self.client.get("/api/workflow-dashboard/" + identity).json()
            receipt = next((item["context"].get("publication_receipt") for item in current["items"]
                            if item["context"].get("publication") == "partially_published"), None)
            if receipt:
                break
            time.sleep(0.1)
        self.assertIsNotNone(receipt, current)
        self.assertEqual(receipt["published_ids"], ["PARTIAL-PASS"])
        self.assertEqual(set(receipt["unpublished"]), {"PARTIAL-FAIL"})
        self.assertIn("PARTIAL-PASS", {case["id"] for case in projects.cases(self.pid)})
        self.assertNotIn("PARTIAL-FAIL", {case["id"] for case in projects.cases(self.pid)})
        self.open("workflows/" + identity)
        expect(self.page.get_by_text("已入库 1 条，未入库 1 条", exact=True)).to_be_visible()
        expect(self.page.get_by_text("PARTIAL-FAIL 未入库：" + receipt["unpublished"]["PARTIAL-FAIL"], exact=True)).to_be_visible()
        expect(self.page.get_by_text("原始文件：订单需求.md", exact=True)).to_be_visible()
        expect(self.page.get_by_text("来源标识：", exact=False)).to_contain_text("workflows/" + identity + "/artifacts/")
        expect(self.page.get_by_text("候选已发布", exact=True)).to_have_count(0)
        run_id = next(item["run_id"] for item in current["items"] if item["phase"] == "execute" and item["run_id"])
        self.open("runs/" + run_id)
        expect(self.page.get_by_role("button", name="只重跑这一条", exact=True)).to_have_count(0)
        expect(self.page.get_by_role("link", name="返回原测试任务", exact=True)).to_have_attribute("href", "#/workflows/" + identity)
        destination = self.origin + '/?embed=1#/workflows/' + identity
        self.page.goto(self.origin + '/api/status')
        self.page.set_viewport_size({'width': 700, 'height': 700})
        self.page.set_content('<iframe name="host" style="width:390px;height:500px;border:0" src="' + destination + '"></iframe>')
        reader = self.page.frame(name='host')
        expect(reader.get_by_role('heading', name='入库结果查阅', exact=True)).to_be_visible()
        expect(reader.get_by_role('link', name='查看项目测试用例', exact=True)).to_be_in_viewport(ratio=1)
        self.evidence('f7-workflow-first-screen.png')
        oracle = reader.locator('[data-reading-id="source:oracles"] summary')
        oracle.focus()
        oracle.press('Enter')
        evidence = reader.get_by_role('link', name='查看真实执行', exact=True).first
        for _attempt in range(3):
            evidence.focus()
            reader.evaluate('scrollTo(0, 888)')
            before = reader.evaluate('scrollY')
            evidence.press('Enter')
            expect(reader.locator('#view')).to_contain_text('本次综合失败')
            reader.evaluate('history.back()')
            expect(reader.get_by_role('heading', name='入库结果查阅', exact=True)).to_be_visible()
            expect(evidence).to_be_focused()
            self.assertTrue(oracle.locator('..').evaluate('node => node.open'))
            reader.wait_for_function('expected => Math.abs(scrollY - expected) < 2', arg=before)
        evidence.press('Enter')
        expect(reader.locator('#view')).to_contain_text('本次综合失败')
        old_cycle = workflow.status(identity)['cycle_id']
        revised = workflow.revise(identity, {'title': '第二周期服务查询', 'sources': [{'id': 'SOURCE', 'text': '新周期独立需求：查询服务仍应为 true。'}]},
                                  idempotency_key=uuid.uuid4().hex)
        self.assertNotEqual(revised['cycle_id'], old_cycle)
        reader.evaluate('history.back()')
        expect(reader.get_by_role('heading', name='第二周期服务查询', exact=True)).to_be_visible()
        self.assertFalse(reader.locator('[data-reading-id="source:oracles"]').evaluate('node => node.open'))
        reader.wait_for_function('scrollY === 0')
        current_progress = reader.get_by_role('region', name='当前周期进度', exact=True)
        expect(current_progress).to_have_attribute('data-cycle', revised['cycle_id'])
        expect(current_progress.get_by_role('link', name='查看真实执行', exact=True)).to_have_count(0)
        old_progress = reader.locator('.workflow-cycle[data-cycle="' + old_cycle + '"]')
        self.assertFalse(old_progress.evaluate('node => node.open'))
        expect(old_progress.locator(':scope > summary')).to_contain_text('历史周期 1 · 1 条失败结果')
        old_progress.locator(':scope > summary').focus()
        old_progress.locator(':scope > summary').press('Enter')
        expect(old_progress).to_contain_text('PARTIAL-FAIL 未入库：' + receipt['unpublished']['PARTIAL-FAIL'])
        history_link = old_progress.get_by_role('link', name='查看真实执行', exact=True).first
        history_link.focus()
        before = reader.evaluate('scrollY')
        history_link.press('Enter')
        expect(reader.locator('#view')).to_contain_text('本次综合失败')
        reader.evaluate('history.back()')
        expect(reader.get_by_role('heading', name='第二周期服务查询', exact=True)).to_be_visible()
        expect(history_link).to_be_focused()
        self.assertTrue(old_progress.evaluate('node => node.open'))
        reader.wait_for_function('expected => Math.abs(scrollY - expected) < 2', arg=before)
        self.assertEqual(reader.evaluate('document.documentElement.scrollWidth'), 390)
        self.evidence('f6-workflow-previous-cycle.png')

    @staticmethod
    def submit_stage(identity, payload):
        task = workflow.next_task(identity, idempotency_key=uuid.uuid4().hex)["task"]
        return workflow.submit(identity, task["item_id"], lease_token=task["lease_token"], attempt_id=task["attempt_id"],
                               idempotency_key=uuid.uuid4().hex, payload=payload)
