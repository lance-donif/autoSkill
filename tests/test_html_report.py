"""Exercise standalone report downloads in Chromium, including offline details and images."""
import base64
import json
import mimetypes
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "skills/autoskill/app/ui"
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill import paths

PIXEL = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl1sAAAAASUVORK5CYII=")
PAYLOAD = '</script><script>window.reportInjected=true</script></style><style>body{display:none}</style><img src=x onerror="window.reportInjected=true">'


class HtmlReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        executable = paths.chrome()
        if not executable:
            raise unittest.SkipTest("HTML report tests need an installed or bundled browser")
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        cls.browser = cls.playwright.chromium.launch(executable_path=executable, headless=True, args=["--no-sandbox"])
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.context = self.browser.new_context(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.image_status = 200
        self.run = self.make_run()
        self.page.route("**/*", self.route)

    def tearDown(self):
        self.assertEqual(self.errors, [])

    @staticmethod
    def make_run():
        first = {"id": 1, "case_id": "TC-001", "case_title": "Passed case", "kind": "ui", "status": "Pass",
                 "prerequisite": 0, "duration_ms": 1234, "actual": "First attempt evidence", "category": "", "reason": "",
                 "detail": {"steps": [{"text": "Open the editor", "status": "done", "ms": 20},
                                      {"text": "Save the value", "status": "done", "ms": 30}],
                            "assertions": [{"name": "Long value", "passed": True,
                                            "expected": "long " * 80 + "EXPECTED TAIL", "actual": PAYLOAD}],
                            "screenshots": ["evidence.png"]}}
        second = {"id": 2, "case_id": "TC-001", "case_title": "Later failed attempt", "kind": "api", "status": "Fail",
                  "prerequisite": 0, "duration_ms": 54, "actual": "Later evidence", "category": "assertion_failed", "reason": "Wrong answer",
                  "detail": {"request": {"method": "POST", "url": "https://example.invalid/chat", "headers": {"X-Test": "Header value"},
                                         "params": {"page": 2}, "json": {"question": PAYLOAD}},
                             "response": {"status": 200, "bytes": 33, "body": "Response body " + PAYLOAD},
                             "assertions": [{"name": "The independent oracle", "passed": False, "expected": 42, "actual": 41}]}}
        third = {"id": 3, "case_id": "TC-003", "case_title": "Blocked case", "kind": "api", "status": "Blocked", "prerequisite": 0,
                 "duration_ms": 0, "actual": "", "reason": "Missing fixture credential", "category": "missing_secret", "detail": {}}
        return {"id": "run-report", "project_id": "example", "project_name": "Report fixture " + PAYLOAD,
                "status": "Fail", "total": 3, "passed": 1, "failed": 1, "blocked": 1, "done": 3,
                "engine": "playwright", "trigger": "manual", "created_at": "2026-11-13T00:00:00Z",
                "started_at": "2026-11-13T00:00:00Z", "finished_at": "2026-11-13T00:00:02Z", "delete_requested": 0,
                "deletion_error": "", "results": [first, second, third],
                "test_report": {"status": "ready", "download_url": "/api/task-reports/r/excel",
                                "markdown_download_url": "/api/task-reports/r/markdown"}}

    def route(self, route):
        path = urlsplit(route.request.url).path
        if path == "/api/runs/run-report/files/evidence.png":
            route.fulfill(status=self.image_status, content_type="image/png", body=PIXEL if self.image_status == 200 else b"missing")
        elif path.startswith("/api/"):
            route.fulfill(content_type="application/json", body=json.dumps(self.api(path)))
        else:
            file = UI / (path.lstrip("/") or "index.html")
            if file.is_file():
                content_type = "text/javascript" if file.suffix == ".js" else mimetypes.guess_type(file)[0]
                route.fulfill(content_type=content_type or "text/plain", body=file.read_bytes())
            else:
                route.fulfill(status=404, body="Not found")

    def api(self, path):
        if path == "/api/state-revision":
            return {"revision": "html-report-fixture"}
        if path == "/api/projects":
            return [{"id": "example", "name": "Report fixture", "category": "", "base_url": "https://example.invalid",
                     "cases": {"total": 3, "ui": 1, "api": 2}, "login": {"ui_case": None, "api_case": None}, "missing_secrets": []}]
        if path == "/api/settings":
            return {"data_id": "fixture-data", "dashboard": {"theme": "dark"}, "legacy_dashboard_import_eligible": False}
        if path == "/api/status":
            return {"browser": {}, "runtime": {}, "skill_path": "/synthetic/skill"}
        if path == "/api/runs":
            return {"items": [], "total": 0}
        if path == "/api/runs/run-report":
            for entity in [self.run, *self.run["results"]]:
                status = entity["status"]
                entity["state"] = {
                    "lifecycle": "active" if status == "Running" else "completed",
                    "phase": "running" if status == "Running" else "blocked" if status == "Blocked" else "settled",
                    "outcome": {"Running": "pending", "NotRun": "not_run", "Blocked": "not_run"}.get(status, status.lower()),
                    "checks": {"business": "unverified" if status in ("Running", "Blocked") else status.lower(),
                               "performance": "not_applicable", "quality": "not_applicable"},
                    "evidence": "unverified" if status == "Running" else "valid", "report": "pending" if status == "Running" else "ready",
                    "delivery": "not_applicable", "scope": {},
                }
            self.run["test_report"]["bugs_download_url"] = "/api/task-reports/r/bugs"
            return self.run
        raise AssertionError("Unexpected API request: " + path)

    def open(self):
        self.page.add_init_script("localStorage.setItem('autoskill.theme', 'dark')")
        self.page.goto("http://127.0.0.1:8787/#/runs/run-report")
        expect(self.page.locator("#view h1")).to_have_text(self.run["project_name"])

    def download(self):
        with self.page.expect_download() as waiting:
            self.page.get_by_role("button", name="下载 HTML 报告", exact=True).click()
        download = waiting.value
        self.assertEqual(download.suggested_filename, "autoSkill-run-report.html")
        file = self.folder / download.suggested_filename
        download.save_as(file)
        return file

    def offline(self, file):
        context = self.browser.new_context(viewport={"width": 1440, "height": 1000}, offline=True)
        self.addCleanup(context.close)
        page = context.new_page()
        requests = []
        page.on("request", lambda request: requests.append(request.url))
        page.on("pageerror", lambda error: self.errors.append(str(error)))
        page.set_content(file.read_text(encoding="utf-8"))
        expect(page.locator("h1")).to_have_text(self.run["project_name"])
        return page, requests

    @staticmethod
    def report_style(page):
        return page.evaluate("() => { const s = getComputedStyle(document.querySelector('.run-summary')); "
                             "return [s.backgroundColor, s.borderRadius, s.boxShadow, s.fontFamily, s.color, s.fontSize]; }")

    def test_download_has_same_styles_all_details_and_no_online_dependencies(self):
        self.open()
        summary = self.page.locator(".run-summary")
        expect(summary.get_by_role("button")).to_have_count(1)
        expect(summary.get_by_role("link")).to_have_count(0)
        expect(summary.get_by_role("button", name="下载 HTML 报告", exact=True)).to_be_visible()
        expect(summary.get_by_text("执行与技术详情", exact=True)).to_have_count(0)
        expect(summary.locator(".stats")).to_be_visible()
        expect(summary.locator(".stat-label")).to_have_text(["通过", "失败", "未执行", "用例", "总耗时"])
        expect(summary.locator(".stat-value").nth(0)).to_have_text("1")
        expect(summary.locator(".stat-value").nth(1)).to_have_text("1")
        expect(self.page.locator(".report-heading")).to_be_visible()
        width = self.page.evaluate("document.querySelector('.report-heading').getBoundingClientRect().width")
        self.assertGreaterEqual(width, 280)
        expect(self.page.locator(".result[data-result='1'] .result-body")).to_have_count(0)
        online_style = self.report_style(self.page)
        file = self.download()
        page, requests = self.offline(file)
        for document in (self.page, page):
            expect(document.locator(".result-body .state-details")).to_have_count(0)
            expect(document.locator(".result[data-result='2'] .result-head .status")).to_have_count(1)
        self.assertEqual(self.report_style(page), online_style)
        expect(page.locator(".result-body")).to_have_count(3)
        expect(page.locator(".result[data-result='1'] .steps")).to_contain_text("Save the value")
        expect(page.locator(".result[data-result='2'] .checks")).to_contain_text("The independent oracle")
        expect(page.locator(".result[data-result='2'] .io")).to_contain_text("Response body " + PAYLOAD)
        expect(page.locator(".result[data-result='3'] .banner")).to_contain_text("Missing fixture credential")
        expect(page.get_by_role("button", name="只重跑这一条", exact=True)).to_have_count(0)
        expect(page.get_by_role("button", name="删除运行", exact=True)).to_have_count(0)
        self.assertFalse(page.evaluate("Boolean(window.reportInjected)"))
        expect(page.locator(".result[data-result='1'] .checks td:nth-child(3) details pre")).to_contain_text("EXPECTED TAIL")
        page.get_by_role("button", name="全部收起", exact=True).click()
        expect(page.locator(".result-body:visible")).to_have_count(0)
        page.locator(".result[data-result='1'] .result-head").click()
        expect(page.locator(".result[data-result='1'] .result-body")).to_be_visible()
        expect(page.locator(".result[data-result='2'] .result-body")).to_be_hidden()
        page.get_by_role("button", name="全部展开", exact=True).click()
        expect(page.locator(".result-body:visible")).to_have_count(3)
        expect(page.locator(".result[data-result='1'] .checks details pre").first).to_be_visible()
        page.locator("a.shot").click()
        expect(page.get_by_role("dialog", name="执行截图原图", exact=True)).to_be_visible()
        self.assertEqual(page.locator(".report-image img").evaluate("img => img.naturalWidth"), 1)
        expect(page.locator(".report-image a")).to_have_attribute("download", "evidence.png")
        page.get_by_role("button", name="关闭", exact=True).click()
        expect(page.get_by_role("dialog", name="执行截图原图", exact=True)).to_be_hidden()
        self.assertTrue(all(url.startswith(("file:", "data:")) for url in requests), requests)
        self.assertNotIn('src="/api/', file.read_text(encoding="utf-8"))

    def test_summary_badge_distinguishes_pass_failure_and_unexecuted_runs(self):
        for status, passed, failed, not_run, label in (("Pass", 3, 0, 0, "通过"), ("Fail", 1, 1, 1, "不通过"),
                                                      ("Running", 0, 1, 0, "执行中"), ("NotRun", 0, 0, 3, "未执行")):
            with self.subTest(status=status):
                self.run.update(status=status, passed=passed, failed=failed, not_run=not_run)
                self.page.goto(f"http://127.0.0.1:8787/?snapshot={status}#/runs/run-report")
                expect(self.page.locator(".run-summary .status").first).to_have_text(label)

    def test_missing_screenshot_is_disclosed_in_download_and_other_details_survive(self):
        self.image_status = 404
        self.open()
        file = self.download()
        expect(self.page.locator("#toasts")).to_contain_text("1 张截图未能嵌入")
        page, requests = self.offline(file)
        expect(page.locator(".result-body")).to_have_count(3)
        expect(page.locator("#view > .banner")).to_contain_text("1 张截图未能嵌入")
        expect(page.locator(".result[data-result='1'] .banner")).to_contain_text("evidence.png 未能嵌入：HTTP 404")
        expect(page.locator("a.shot")).to_have_count(0)
        self.assertTrue(all(url.startswith("file:") for url in requests), requests)

    def test_empty_running_report_exports_progress_without_claiming_final_results(self):
        self.run.update(status="Running", engine="ai-eval", results=[], done=0, passed=0, failed=0, blocked=0, finished_at=None)
        self.open()
        file = self.download()
        page, requests = self.offline(file)
        expect(page.locator("header")).to_contain_text("导出时任务尚未完成")
        expect(page.locator(".run-summary")).to_contain_text("执行中")
        expect(page.locator(".run-summary .stats")).to_be_visible()
        expect(page.locator("#view")).to_contain_text("正在准备")
        expect(page.get_by_role("button", name="取消运行", exact=True)).to_have_count(0)
        self.assertTrue(all(url.startswith("file:") for url in requests), requests)


if __name__ == "__main__":
    unittest.main()
