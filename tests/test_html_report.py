"""Exercise standalone report downloads in Chromium, including offline details and images."""
import base64
import copy
import json
import mimetypes
import re
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
        if path in {"/api/runs/run-report/files/evidence.png", "/api/runs/run-report/files/retried.png"}:
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
            case = document.locator(".result[data-result='2']")
            http = case.locator("details").filter(has=document.get_by_text("请求与响应详情", exact=True))
            expect(http).to_have_count(1)
            line = case.locator(".req-line")
            expect(line).to_have_text(re.compile(r"POST.*https://example.invalid/chat.*HTTP 200.*54 ms.*33 B"))
            self.assertTrue(line.evaluate("node => Boolean(node.compareDocumentPosition(node.closest('.result-body').querySelector('.checks')) & Node.DOCUMENT_POSITION_FOLLOWING)"))
            http.evaluate("node => { node.open = false; }")
            expect(line).to_be_visible()
            http.evaluate("node => { node.open = true; }")
            expect(http).to_contain_text("Header value")
            expect(http).to_contain_text('"page": 2')
            expect(http).to_contain_text("Response body " + PAYLOAD)
            self.assertEqual(case.inner_text().count("https://example.invalid/chat"), 1)
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

    def test_retry_requests_remain_separate_in_one_http_panel_online_and_offline(self):
        http = self.run["results"][1]["detail"]
        attempts = []
        for number in (1, 2):
            request = {"method": "POST", "url": "https://example.invalid/retry", "json": {"input": f"input-{number}"}}
            response = {"status": 500 if number == 1 else 200, "body": f"response-{number}", "bytes": 30 + number}
            attempts.append({"id": f"attempt-{number}", "outcome": "fail" if number == 1 else "pass",
                             "kind": "case" if number == 1 else "relogin_retry", "request": request, "response": response,
                             "sha256": "internal-receipt-hash", "duration_ms": number})
        http.update(attempts=attempts, request=copy.deepcopy(attempts[1]["request"]), response=copy.deepcopy(attempts[1]["response"]))
        self.run["results"][2]["detail"].update(request={}, response={}, attempts=[
            {"id": "did-not-start", "outcome": "not_run", "kind": "not_executed", "request": {}, "response": {}}])
        self.open()
        offline, _ = self.offline(self.download())
        for document in (self.page, offline):
            with self.subTest(online=document == self.page):
                for removed in ("本次报告", "执行尝试", "详见本次尝试的执行记录", "查看本次记录"):
                    expect(document.get_by_text(removed, exact=True)).to_have_count(0)
                case = document.locator("[data-result='2']")
                expect(case).not_to_contain_text("internal-receipt-hash")
                lines = case.locator(".req-line")
                expect(lines).to_have_count(2)
                expect(lines.nth(0)).to_have_text(re.compile(r"第 1 次.*POST.*retry.*HTTP 500.*1 ms.*31 B"))
                expect(lines.nth(1)).to_have_text(re.compile(r"第 2 次.*POST.*retry.*HTTP 200.*2 ms.*32 B"))
                panel = case.locator("details").filter(has=document.get_by_text("请求与响应详情", exact=True))
                expect(panel).to_have_count(1)
                expect(panel).to_contain_text(re.compile(r"第 1 次.*input-1.*response-1.*第 2 次.*input-2.*response-2", re.DOTALL))
                for token in ("input-1", "response-1", "input-2", "response-2"):
                    self.assertEqual(panel.text_content().count(token), 1, token)
                no_request = document.locator("[data-result='3']")
                expect(no_request).to_contain_text("第 1 次")
                expect(no_request.get_by_text("请求与响应详情", exact=True)).to_have_count(0)

    def test_ui_steps_distinguish_recorded_pending_and_definitions_in_both_reports(self):
        case = self.run["results"][0]
        case.update(status="Fail", reason="Timeout at navigation")
        case["detail"]["steps"][1].update(status="failed", error="Timeout at navigation", ms=25)
        case["detail"]["pending_steps"] = [{"text": "Check the saved value", "check": True}]
        definition = self.run["results"][2]["detail"]
        definition["definition_steps"] = [{"text": "Read archived definition", "check": True}]
        self.open()
        offline, _ = self.offline(self.download())
        for document in (self.page, offline):
            with self.subTest(online=document == self.page):
                result = document.locator("[data-result='1']")
                expect(result.locator("[data-attempt]")).to_have_count(0)
                timeline = result.get_by_role("list", name="操作步骤")
                steps = timeline.get_by_role("listitem")
                expect(steps).to_have_count(3)
                expect(steps.nth(0)).to_have_text(re.compile(r"Open the editor.*已完成.*20 ms"))
                expect(steps.nth(1)).to_have_text(re.compile(r"Save the value.*失败.*25 ms.*Timeout at navigation"))
                expect(steps.nth(2)).to_have_text(re.compile(r"断言.*Check the saved value.*未执行"))
                expect(steps.nth(2)).not_to_contain_text("ms")
                self.assertTrue(result.locator(".checks").evaluate(
                    "checks => Boolean(checks.compareDocumentPosition(checks.parentElement.querySelector('.steps')) & Node.DOCUMENT_POSITION_FOLLOWING)"))
                self.assertTrue(timeline.evaluate(
                    "steps => Boolean(steps.compareDocumentPosition(steps.closest('.result').querySelector('.shot')) & Node.DOCUMENT_POSITION_FOLLOWING)"))
                archive = document.locator("[data-result='3']")
                expect(archive.get_by_role("heading", name="用例定义步骤")).to_be_visible()
                expect(archive.get_by_role("list", name="用例定义步骤")).to_have_text("断言Read archived definition")
                expect(archive.get_by_role("list", name="操作步骤")).to_have_count(0)

    def test_retried_ui_keeps_each_timeline_check_and_image_with_its_receipt(self):
        case = self.run["results"][0]
        case.update(status="Fail", reason="Priority was medium")
        captures = []
        for passed, value, name in [(False, "medium", "evidence.png"), (True, "high", "retried.png")]:
            check = {"name": "Priority", "passed": passed, "actual": value, "expected": "high"}
            if not passed:
                check["screenshot"] = name
            captures.append({"kind": "case", "outcome": "pass" if passed else "fail", "ui": {
                "steps": [{"text": "Inspect priority", "status": "done" if passed else "failed"}],
                "assertions": [check], "screenshots": [name]}})
        case["detail"].update(attempts=captures, steps=captures[1]["ui"]["steps"], assertions=captures[0]["ui"]["assertions"],
                              screenshots=["evidence.png", "retried.png"])
        archive = self.run["results"][2]
        archive["kind"] = "ui"
        archive["detail"].update(attempts=[{"kind": "case", "outcome": "fail", "ui": {"steps": [{"text": "Known navigation", "status": "failed"}]}},
                                           {"kind": "case", "outcome": "pass"}],
                                 steps=[{"text": "Archived navigation", "status": "done"}])
        self.open()
        offline, _ = self.offline(self.download())
        for document in (self.page, offline):
            with self.subTest(online=document == self.page):
                result = document.locator("[data-result='1']")
                for idx, (state, value, filename) in enumerate([("failed", "medium", "evidence.png"), ("done", "high", "retried.png")], 1):
                    section = result.get_by_role("region", name=f"第 {idx} 次", exact=True)
                    expect(section.locator(f".timeline li.{state}")).to_have_text(re.compile("Inspect priority"))
                    expect(section.locator(".checks tbody tr td").last).to_have_text(value)
                    expect(section.locator(".shot img")).to_have_count(1)
                    if document == self.page:
                        expect(section.locator(".shot")).to_have_attribute("href", re.compile(re.escape(filename) + "$"))
                    else:
                        expect(section.locator(".shot")).to_have_attribute("data-filename", filename)
                        expect(section.locator(".shot img")).to_have_attribute("src", re.compile("^data:image/png"))
                expect(result.locator(".shot")).to_have_count(2)
                expect(result.get_by_text("第 1 次 · 断言「Priority」失败时的截图（密码输入框已遮盖）", exact=True)).to_be_visible()
                older = document.locator("[data-result='3']")
                expect(older.locator("[data-attempt]")).to_have_count(1)
                expect(older.locator("[data-attempt]")).not_to_contain_text("Archived navigation")
                expect(older.get_by_role("region", name="用例结果")).to_contain_text("Archived navigation")

    def test_status_only_empty_body_and_interrupted_http_attempts(self):
        detail = self.run["results"][1]["detail"]
        request = {"method": "GET", "url": "https://example.invalid/resource"}
        detail.update(request=request, response=None, attempts=[
            {"request": request, "response": {"status": 200}},
            {"request": request, "response": {"status": 204, "body": "", "bytes": 0}, "duration_ms": 0},
            {"request": request, "duration_ms": 7}])
        self.open()
        offline, _ = self.offline(self.download())
        for document in (self.page, offline):
            with self.subTest(online=document == self.page):
                result = document.locator("[data-result='2']")
                lines = result.locator(".req-line")
                expect(lines).to_have_count(3)
                expect(lines.nth(0)).to_contain_text("HTTP 200")
                expect(lines.nth(0)).not_to_contain_text("ms")
                expect(lines.nth(1)).to_have_text(re.compile(r"HTTP 204.*0 ms.*0 B"))
                expect(lines.nth(2)).to_contain_text("7 ms")
                expect(lines.nth(2)).not_to_contain_text("HTTP")
                expect(lines.nth(2)).not_to_contain_text(" B")
                panel = result.locator("details").filter(has=document.get_by_text("请求与响应详情", exact=True))
                expect(panel).to_contain_text(re.compile(r"第 1 次.*响应状态HTTP 200.*第 2 次.*响应体（空）", re.DOTALL))
                expect(panel).not_to_contain_text("第 3 次")

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
