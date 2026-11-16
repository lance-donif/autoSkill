"""Chromium inspects host-maintained designs and downloads real current and historical evidence."""
import io
import json
import os
from pathlib import Path
from unittest.mock import patch

from openpyxl import load_workbook
from playwright.sync_api import expect
from test_load_dashboard import DashboardWorkspace

from autoskill import builder, case_document, execution_inputs, plan, projects, runner, task_workbook


class CasePlanDashboardTests(DashboardWorkspace):
    def open_options(self, reader=None):
        options = (reader or self.page).locator('.case-plan-options')
        if not options.evaluate('node => node.open'):
            options.locator('summary').focus()
            options.locator('summary').press('Enter')

    def open_plan(self, session="设计甲"):
        self.open(f"case-plans?range=all&project={self.pid}&session={session}")
        expect(self.page.get_by_role("heading", name="测试用例表", exact=True)).to_be_visible()

    def download(self, locator):
        with self.page.expect_download() as saved:
            locator.click()
        return Path(saved.value.path()).read_bytes()

    def conclusions(self, content):
        book = load_workbook(io.BytesIO(content))
        self.addCleanup(book.close)
        self.assertEqual([cell.value for cell in book["功能测试用例"][1]], task_workbook.HEADERS)
        return [row[9].value for row in book["功能测试用例"].iter_rows(min_row=2) if row[1].value]

    def test_host_changes_require_real_evidence_and_browser_keeps_prior_excel(self):
        case = projects.case(self.pid, "TC-SLOW")
        case_document.apply(case, {"description": "验证查询服务就绪状态后返回真实就绪标记", "preconditions": "本地合成就绪检查服务已启动",
                                   "steps": ["1. 查询服务就绪状态。", "2. 查看响应中的就绪标记。"], "expected": "2. 就绪标记 ok 为 true。",
                                   "source": {"kind": "synthetic_requirement", "text": "服务以 ok=true 表示就绪，查询不修改数据。"},
                                   "business_path": ["负载验证", "服务检查", "就绪状态"], "module": "服务检查"})
        projects.save_cases(self.pid, [case])
        base = f"/api/case-plans/{self.pid}"
        initial = self.client.get(base + "?session=设计甲").json()
        imported = self.client.post(base + "/import-cases?session=设计甲", json={"case_ids": ["TC-SLOW"], "expected_revision": initial["revision"]})
        self.assertEqual(imported.status_code, 200, imported.text)
        self.open_plan()
        expect(self.page.locator("#view .case-plan-row")).to_have_count(1)
        for label in ("新建设计", "从已有用例导入", "编辑设计", "关联用例", "应用设计到用例"):
            expect(self.page.get_by_role("button", name=label, exact=True)).to_have_count(0)
        expect(self.page.get_by_role("textbox", name="设计表会话", exact=True)).to_have_count(0)
        current = self.client.get(base + "?session=设计甲").json()
        self.assertEqual(current["rows"][0]["status"], "NotRun")
        updated = self.client.patch(base + "/rows/TC-SLOW?session=设计甲", json={"expected_revision": current["revision"], "row": {"author": "验收人员"}})
        self.assertEqual(updated.status_code, 200, updated.text)
        run = runner.create(self.pid, ["TC-SLOW"], headed=False)
        self.assertEqual(runner.execute(run["id"])["status"], "Pass")
        current = self.client.get(base + "?session=设计甲").json()
        synced = self.client.post(base + "/sync?session=设计甲", json={"expected_revision": current["revision"]})
        self.assertEqual(synced.status_code, 200, synced.text)
        self.open_plan()
        expect(self.page.get_by_role("link", name="查看真实执行证据", exact=True)).to_be_visible()
        current = self.client.get(f"/api/case-plans/{self.pid}?session=设计甲").json()
        self.assertEqual(current["rows"][0]["status"], "Pass")
        self.evidence("case-plans-real-result.png")
        passed_version = current["version"]
        readonly = self.client.patch(f"/api/case-plans/{self.pid}/rows/TC-SLOW?session=设计甲", json={"expected_revision": current["revision"], "row": {"conclusion": "通过"}})
        self.assertEqual(readonly.status_code, 400)
        self.assertEqual(self.conclusions(self.download(self.page.get_by_role("link", name="下载 14 列 Excel", exact=True))), ["通过"])
        changed = self.client.patch(base + "/rows/TC-SLOW?session=设计甲", json={"expected_revision": current["revision"], "row": {"expected": "1. 新版本需求，尚未回归验证"}})
        self.assertEqual(changed.status_code, 200, changed.text)
        self.open_options()
        self.page.get_by_role("button", name="刷新结果", exact=True).click()
        expect(self.page.locator("#view")).to_contain_text("设计或关联用例已修改")
        self.assertEqual(self.conclusions(self.download(self.page.get_by_role("link", name="下载 14 列 Excel", exact=True))), ["未执行"])
        self.open_options()
        self.page.get_by_role("button", name="历史版本", exact=True).click()
        history = self.page.get_by_role("dialog", name="设计历史版本", exact=True)
        old = history.locator('.row').filter(has=self.page.get_by_role("button", name=f"查看版本 {passed_version}", exact=True))
        self.assertEqual(self.conclusions(self.download(old.get_by_role("link", name="下载历史 Excel", exact=True))), ["通过"])
        history.get_by_role("button", name=f"查看版本 {passed_version}", exact=True).click()
        expect(history).to_contain_text("不代表当前设计验证")
        history.get_by_role("button", name="关闭", exact=True).last.click()
        self.open_plan("设计乙")
        expect(self.page.locator("#view")).to_contain_text("这份用例表还没有设计用例")
        self.assertEqual(self.client.get(f"/api/case-plans/{self.pid}?session=设计甲").json()["rows"][0]["status"], "NotRun")
        other = self.client.post("/api/projects", json={"name": "隔离项目", "url": "http://127.0.0.1:9"}).json()
        self.open(f"case-plans?range=all&project={other['id']}&session=设计甲")
        expect(self.page.locator("#view")).to_contain_text("这份用例表还没有设计用例")
        self.evidence("case-plans-session-isolation.png")

    def test_excel_only_project_groups_real_outcomes_and_preserves_full_download_in_iframe(self):
        project = projects.create("订单用例表验收", f"http://localhost:{self.target.server_port}", project_id="excel-only")["id"]
        rows = [
            {"id": "DESIGN-PASS", "module": "订单查询", "title": "查询就绪订单", "steps": ["1. 查询订单服务。"],
             "expected": "1. 就绪标记应为 true。", "source": "需求：就绪查询返回 true。"},
            {"id": "DESIGN-FAIL", "module": "订单查询", "title": "核对错误预期", "steps": ["1. 再次查询订单服务。"],
             "expected": "1. 就绪标记应为 false（独立失败对照）。", "source": "需求：保留失败断言证据。"},
            {"id": "DESIGN-PENDING", "module": "支付", "title": "尚待执行的支付场景", "steps": ["1. 检查支付结果。"],
             "expected": "1. 支付结果符合需求。"},
        ]
        source = self.folder / "readable-design.json"
        source.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        with patch.dict(os.environ, {"AUTOSKILL_CASE_SKILL": str(self.folder / "sheet"), "AUTOSKILL_SESSION": "完整订单用例表"}):
            plan.write(project, str(source))
            for identity, expected in (("PASS", True), ("FAIL", False)):
                candidate = builder.api_case("API-" + identity, identity, "GET", "/slow", {"status": 200, "json_equals": {"/ok": expected}})
                run = runner.create(project, [candidate["id"]], candidate_cases=[candidate], api_only=True)
                frozen = execution_inputs.load(run)["cases"][candidate["id"]]
                bound = plan.bind_execution("DESIGN-" + identity, project, frozen, run["id"], candidate_id="fixture-candidate")
                self.assertNotIn("error", bound, bound)
                self.assertEqual(runner.execute(run["id"])["status"], "Pass" if expected else "Fail")
                plan.sync(run_id=run["id"])
        self.assertEqual(projects.cases(project), [])
        self.open("case-plans?range=all")
        self.open_options()
        self.page.get_by_role('combobox', name='用例表项目', exact=True).select_option(project)
        expect(self.page.locator("#view .case-plan-row")).to_have_count(3)
        for important in (self.page.locator('.case-plan-row h3').first, self.page.locator('.case-plan-row .status').first,
                          self.page.get_by_role('link', name='下载 14 列 Excel', exact=True)):
            expect(important).to_be_in_viewport(ratio=1)
        destination = self.page.url.replace("/#/", "/?embed=1#/")
        self.page.goto(self.origin + "/api/status")
        self.page.set_viewport_size({"width": 700, "height": 700})
        self.page.set_content('<iframe name="host" style="width:390px;height:500px;border:0" src="' + destination + '"></iframe>')
        reader = self.page.frame(name="host")
        expect(reader.get_by_role("heading", name="测试用例表", exact=True)).to_be_visible()
        expect(reader.locator('#scope')).to_be_hidden()
        first = reader.locator('.case-plan-row').first
        for important in (first.locator('h3').first, first.locator('.status').first,
                          reader.get_by_role('link', name='下载 14 列 Excel', exact=True)):
            expect(important).to_be_in_viewport(ratio=1)
        self.evidence('f6-case-plan-first-screen.png')
        if output := os.environ.get('AUTOSKILL_VALIDATION_OUTPUT'):
            self.page.locator('iframe[name="host"]').screenshot(path=str(Path(output) / 'f6-case-plan-iframe390x500.png'))
        expect(reader.get_by_role("region", name="业务模块：订单查询", exact=True).locator(".case-plan-row")).to_have_count(2)
        expect(reader.get_by_role("region", name="业务模块：支付", exact=True).locator(".case-plan-row")).to_have_count(1)
        failed = reader.locator('[data-case="DESIGN-FAIL"]')
        expect(failed.locator(".status")).to_have_text("失败")
        expect(failed.get_by_role("link", name="查看真实执行证据", exact=True)).to_be_visible()
        expect(failed.get_by_text(rows[1]["expected"], exact=True)).not_to_be_visible()
        summary = failed.locator("summary")
        summary.focus()
        summary.press("Enter")
        expect(failed.get_by_text(rows[1]["expected"], exact=True)).to_be_visible()
        expect(failed.get_by_text(rows[1]["steps"][0], exact=True)).to_be_visible()
        expect(failed.get_by_text(rows[1]["source"], exact=True)).to_be_visible()
        expect(failed.get_by_text("实际结果", exact=True)).to_be_visible()
        expect(reader.locator("#view")).not_to_contain_text("设计人：未记录")
        with self.page.expect_response(lambda response: '/api/case-plans/' + project in response.url):
            self.client.patch('/api/projects/' + project, json={'category': '外部真实更新'}).raise_for_status()
        expect(reader.locator('#view')).not_to_have_attribute('aria-busy', 'true')
        expect(summary).to_be_focused()
        self.assertTrue(summary.locator('..').evaluate('node => node.open'))
        self.assertEqual(reader.evaluate("document.documentElement.scrollWidth"), 390)
        self.evidence("f1-case-plans-iframe390.png")
        self.assertEqual(self.conclusions(self.download(reader.get_by_role("link", name="下载 14 列 Excel", exact=True))), ["通过", "失败", "未执行"])
        evidence = failed.get_by_role("link", name="查看真实执行证据", exact=True)
        for _attempt in range(2):
            evidence.focus()
            before = reader.evaluate("scrollY")
            self.assertGreater(before, 0)
            evidence.press("Enter")
            expect(reader.get_by_text("本次综合失败", exact=False)).to_be_visible()
            expect(reader.get_by_role("link", name="下载本次用例 Excel", exact=True)).to_be_visible()
            self.assertFalse(reader.locator(".report-technical").evaluate('node => node.open'))
            reader.evaluate("history.back()")
            expect(reader.get_by_role("heading", name="测试用例表", exact=True)).to_be_visible()
            expect(evidence).to_be_focused()
            self.assertTrue(summary.locator('..').evaluate('node => node.open'))
            reader.wait_for_function("expected => Math.abs(scrollY - expected) < 2", arg=before)
            self.assertEqual(reader.evaluate("document.documentElement.scrollWidth"), 390)
            evidence.press("Tab")
            expect(summary).to_be_focused()
        self.evidence("f2-case-plans-evidence-return.png")
        evidence.press("Enter")
        expect(reader.get_by_text("本次综合失败", exact=False)).to_be_visible()
        with patch.dict(os.environ, {"AUTOSKILL_CASE_SKILL": str(self.folder / "sheet"), "AUTOSKILL_SESSION": "完整订单用例表"}):
            prior = plan.show()["task_id"]
            replacement = plan.write(project, str(source))
            self.assertNotEqual(replacement["task_id"], prior)
        reader.evaluate("history.back()")
        expect(reader.get_by_role("heading", name="测试用例表", exact=True)).to_be_visible()
        expect(reader.locator(".case-plan-details[open]")).to_have_count(0)
        expect(failed.get_by_role("link", name="查看真实执行证据", exact=True)).to_have_count(0)
        reader.wait_for_function("scrollY === 0")
        for identity in ('DESIGN-FAIL', 'DESIGN-PENDING'):
            opened = reader.locator(f'[data-case="{identity}"] summary')
            opened.focus()
            opened.press('Enter')
        self.assertGreater(reader.evaluate('scrollY'), 0)
        changed_rows = [{**row, 'title': row['title'] + ' 新任务', 'source': '新一轮任务来源'} for row in rows]
        source.write_text(json.dumps(changed_rows, ensure_ascii=False), encoding='utf-8')
        with (self.page.expect_response(lambda response: '/api/case-plans/' + project in response.url),
              patch.dict(os.environ, {"AUTOSKILL_CASE_SKILL": str(self.folder / "sheet"), "AUTOSKILL_SESSION": "完整订单用例表"})):
            newest = plan.write(project, str(source))
            self.assertNotEqual(newest['task_id'], replacement['task_id'])
        expect(reader.locator('#view')).to_contain_text('已切换到新的用例表任务')
        expect(reader.locator('.case-plan-details[open]')).to_have_count(0)
        expect(reader.get_by_role('heading', name='测试用例表', exact=True)).to_be_focused()
        expect(reader.get_by_role('heading', name='核对错误预期 新任务', exact=True)).to_be_visible()
        reader.wait_for_function('scrollY === 0')
        with self.page.expect_response(lambda response: '/api/case-plans/' + project in response.url):
            self.client.patch('/api/projects/' + project, json={'category': '新任务之后的外部更新'}).raise_for_status()
        expect(reader.locator('#view')).not_to_have_attribute('aria-busy', 'true')
        expect(reader.get_by_role('heading', name='测试用例表', exact=True)).to_be_focused()
        expect(reader.locator('#view')).to_contain_text('已切换到新的用例表任务')
        for important in (reader.locator('.case-plan-row h3').first, reader.locator('.case-plan-row .status').first,
                          reader.get_by_role('link', name='下载 14 列 Excel', exact=True)):
            expect(important).to_be_in_viewport(ratio=1)
        with (self.page.expect_response(lambda response: '/api/case-plans/' + project in response.url),
              patch.dict(os.environ, {'AUTOSKILL_CASE_SKILL': str(self.folder / 'alternative-sheet'), 'AUTOSKILL_SESSION': '备用订单用例表'})):
            plan.write(project, str(source))
        sheets = reader.get_by_role('combobox', name='已有用例表', exact=True)
        expect(reader.locator('select[aria-label="已有用例表"] option[value="备用订单用例表"]')).to_have_count(1)
        self.open_options(reader)
        sheets.select_option('完整订单用例表')
        expect(reader.locator('#view h1')).to_be_visible()
        self.open_options(reader)
        expect(sheets).to_have_value('完整订单用例表')
        reader.get_by_role('button', name='回放设置', exact=True).click()
        preference = reader.get_by_role('switch', name='显示浏览器窗口', exact=True)
        preference.focus()
        original = preference.element_handle()
        with self.page.expect_response(lambda response: '/api/case-plans/' + project in response.url):
            self.client.patch('/api/projects/' + project, json={'category': '设置打开期间的外部更新'}).raise_for_status()
        self.assertTrue(original.evaluate('node => node.isConnected && node === document.activeElement'))
        preference.press('Escape')
        reader.get_by_role('combobox', name='用例表项目', exact=True).select_option(self.pid)
        expect(reader.locator('#view')).to_contain_text('这份用例表还没有设计用例')
        self.assertEqual(reader.evaluate('new URLSearchParams(location.hash.split("?")[1]).get("projects")'), self.pid)
