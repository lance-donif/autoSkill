"""Chromium edits designs, runs their linked cases and downloads real current/historical evidence."""
import io
import re
import time

from openpyxl import load_workbook
from playwright.sync_api import expect
from test_load_dashboard import DashboardWorkspace

from autoskill import case_document, projects, task_workbook


class CasePlanDashboardTests(DashboardWorkspace):
    def open_plan(self, session="设计甲"):
        self.open(f"case-plans?range=all&project={self.pid}&session={session}")
        expect(self.page.get_by_role("heading", name="设计用例表", exact=True)).to_be_visible()

    def download(self, locator):
        with self.page.expect_download() as saved:
            locator.click()
        return saved.value.path().read_bytes()

    def conclusions(self, content):
        book = load_workbook(io.BytesIO(content))
        self.addCleanup(book.close)
        self.assertEqual([cell.value for cell in book["功能测试用例"][1]], task_workbook.HEADERS)
        return [row[9].value for row in book["功能测试用例"].iter_rows(min_row=2) if row[1].value]

    def test_design_edit_requires_new_binding_and_real_run_preserves_prior_version(self):
        case = projects.case(self.pid, "TC-SLOW")
        case_document.apply(case, {"description": "验证查询服务就绪状态后返回真实就绪标记", "preconditions": "本地合成就绪检查服务已启动",
                                   "steps": ["1. 查询服务就绪状态。", "2. 查看响应中的就绪标记。"], "expected": "2. 就绪标记 ok 为 true。",
                                   "source": {"kind": "synthetic_requirement", "text": "服务以 ok=true 表示就绪，查询不修改数据。"},
                                   "business_path": ["负载验证", "服务检查", "就绪状态"], "module": "服务检查"})
        projects.save_cases(self.pid, [case])
        self.open_plan()
        self.page.get_by_role("button", name="从已有用例导入", exact=True).click()
        dialog = self.page.get_by_role("dialog", name="从已有可执行用例导入设计", exact=True)
        dialog.get_by_role("checkbox", name="导入 TC-SLOW", exact=True).check()
        dialog.get_by_role("button", name="导入为设计", exact=True).click()
        expect(self.page.locator("#view table tbody tr")).to_have_count(1)
        self.assertEqual(self.client.get(f"/api/case-plans/{self.pid}?session=设计甲").json()["rows"][0]["status"], "NotRun")
        self.page.get_by_role("button", name="编辑设计", exact=True).click()
        dialog = self.page.get_by_role("dialog", name="编辑设计：TC-SLOW", exact=True)
        dialog.get_by_label("用例编制人", exact=True).fill("验收人员")
        dialog.get_by_role("button", name="保存设计", exact=True).click()
        expect(dialog).not_to_be_visible()
        expect(self.page.get_by_role("button", name="回放", exact=True)).to_be_visible()
        self.page.get_by_role("button", name="回放", exact=True).click()
        expect(self.page).to_have_url(re.compile(r"/#/runs/run-"))
        identity = self.page.url.split("/runs/")[1].split("?")[0]
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            run = self.client.get("/api/runs/" + identity).json()
            if run["status"] not in ("Queued", "Running"):
                break
            time.sleep(.1)
        self.assertEqual(run["status"], "Pass", run)
        self.open_plan()
        self.page.get_by_role("button", name="同步真实结果", exact=True).click()
        expect(self.page.get_by_role("link", name="查看真实执行证据", exact=True)).to_be_visible()
        current = self.client.get(f"/api/case-plans/{self.pid}?session=设计甲").json()
        self.assertEqual(current["rows"][0]["status"], "Pass")
        self.evidence("case-plans-real-result.png")
        passed_version = current["version"]
        readonly = self.client.patch(f"/api/case-plans/{self.pid}/rows/TC-SLOW?session=设计甲", json={"expected_revision": current["revision"], "row": {"conclusion": "通过"}})
        self.assertEqual(readonly.status_code, 400)
        self.assertEqual(self.conclusions(self.download(self.page.get_by_role("link", name="下载 14 列 Excel", exact=True))), ["通过"])
        self.page.get_by_role("button", name="编辑设计", exact=True).click()
        dialog = self.page.get_by_role("dialog", name="编辑设计：TC-SLOW", exact=True)
        dialog.get_by_label("预期结果", exact=True).fill("1. 新版本需求，尚未回归验证")
        dialog.get_by_role("button", name="保存设计", exact=True).click()
        expect(self.page.locator("#view")).to_contain_text("设计或关联用例已修改")
        self.assertEqual(self.conclusions(self.download(self.page.get_by_role("link", name="下载 14 列 Excel", exact=True))), ["未执行"])
        self.page.get_by_role("button", name="历史版本", exact=True).click()
        history = self.page.get_by_role("dialog", name="设计历史版本", exact=True)
        old = history.locator('.row').filter(has=self.page.get_by_role("button", name=f"查看版本 {passed_version}", exact=True))
        self.assertEqual(self.conclusions(self.download(old.get_by_role("link", name="下载历史 Excel", exact=True))), ["通过"])
        history.get_by_role("button", name=f"查看版本 {passed_version}", exact=True).click()
        expect(history).to_contain_text("不代表当前设计验证")
        history.get_by_role("button", name="关闭", exact=True).last.click()
        self.page.get_by_role("textbox", name="设计表会话", exact=True).fill("设计乙")
        self.page.get_by_role("button", name="切换会话", exact=True).click()
        expect(self.page.locator("#view")).to_contain_text("这份设计表还没有设计用例")
        self.assertEqual(self.client.get(f"/api/case-plans/{self.pid}?session=设计甲").json()["rows"][0]["status"], "NotRun")
        other = self.client.post("/api/projects", json={"name": "隔离项目", "url": "http://127.0.0.1:9"}).json()
        self.open(f"case-plans?range=all&project={other['id']}&session=设计甲")
        expect(self.page.locator("#view")).to_contain_text("这份设计表还没有设计用例")
        self.evidence("case-plans-session-isolation.png")
