"""Chromium fixtures exercise result navigation and fault receipts, without remote services."""
import copy
import json
import unittest
from urllib.parse import parse_qs, urlsplit

import test_html_report as html_fixture
from playwright.sync_api import expect


class ResultsUxTests(unittest.TestCase):
    make_run = staticmethod(html_fixture.HtmlReportTests.make_run)
    setUpClass = classmethod(html_fixture.HtmlReportTests.setUpClass.__func__)
    tearDown = html_fixture.HtmlReportTests.tearDown
    open = html_fixture.HtmlReportTests.open

    def setUp(self):
        html_fixture.HtmlReportTests.setUp(self)
        self.post_count = 0
        self.post_status = 200
        self.post_body = {"id": "run-next", "total": 1, "dropped": 2}
        self.revision = "results-ux-1"
        self.native = self.native_fixture()
        self.native_requests = []
        self.next_status = 'Fail'

    def native_fixture(self):
        rows = []
        for number in range(1, 71):
            failed = number == 70
            rows.append({"id": number, "case_id": "duplicate" if number >= 69 else f"case-{number}",
                         "case_title": f"Native result {number}", "duration_ms": 1, "turn_count": 0,
                         "state": {"lifecycle": "completed", "phase": "settled", "outcome": "fail" if failed else "pass",
                                   "checks": {"business": "pass", "performance": "fail" if failed else "not_applicable", "quality": "not_applicable"},
                                   "reason": "响应超过目标" if failed else "", "evidence": "valid", "report": "ready", "scope": {}}})
        return {"id": "native-70", "family": "native", "project_id": "example", "project_name": "Native fixture", "status": "Fail",
                "engine": "selenium-unittest", "asset_id": "asset-1", "asset_version": 1, "created_at": "2026-11-16T00:00:00Z",
                "total": 70, "done": 70, "passed": 69, "failed": 1, "blocked": 0, "state": copy.deepcopy(rows[-1]["state"]),
                "outcome": {}, "results": rows, "report": {"available": False}}

    def api(self, path):
        if path == "/api/state-revision":
            return {"revision": self.revision}
        if path == "/api/results":
            row = {**self.native["results"][-1], "run_id": "native-70", "project_name": "Native fixture", "kind": "native"}
            return {"items": [row], "total": 70}
        if path == "/api/runs/native-70":
            return self.native
        if path == "/api/runs/run-next":
            result = copy.deepcopy(html_fixture.HtmlReportTests.api(self, "/api/runs/run-report"))
            result["id"] = "run-next"
            result["status"] = self.next_status
            return result
        return html_fixture.HtmlReportTests.api(self, path)

    def route(self, request):
        url = urlsplit(request.request.url)
        if request.request.method == "POST":
            self.post_count += 1
            request.fulfill(status=self.post_status, content_type="application/json", body=json.dumps(self.post_body))
        elif url.path == "/api/native-runs/native-70":
            query = parse_qs(url.query)
            self.native_requests.append(query)
            result = copy.deepcopy(self.native)
            focus = query.get("case", [None])[0]
            if focus:
                if focus == "duplicate":
                    request.fulfill(status=400, content_type="application/json", body=json.dumps({"detail": "本次运行存在相同用例编号；请用 result:<结果 id> 读取具体结果"}))
                    return
                row = next(row for row in result["results"] if str(row["id"]) == focus.removeprefix("result:"))
                result["results"] = [row]
                result["case"] = {"state": row["state"], "assertions": [{"name": "SLA", "passed": row["id"] != 70, "expected": "100 ms", "actual": "250 ms"}]}
                result["page"] = {"has_more": False}
            elif query.get("all") == ["1"]:
                for row in result["results"]:
                    row["detail"] = {"assertions": [{"name": "SLA", "expected": "100 ms", "actual": "250 ms",
                                                     "passed": row["id"] != 70}]}
                result["page"] = {"has_more": False}
            else:
                start = 50 if query.get("cursor") else 0
                limit = int(query.get("limit", [50])[0])
                result["results"] = result["results"][start:start + limit]
                result["page"] = {"has_more": start + limit < 70, "next_cursor": "second"}
            request.fulfill(content_type="application/json", body=json.dumps(result))
        else:
            html_fixture.HtmlReportTests.route(self, request)

    def redraw(self):
        self.revision += "x"
        self.page.evaluate("window.dispatchEvent(new Event('focus'))")

    def test_keyboard_result_link_reaches_70th_and_returns_to_filters(self):
        original = "#/runs?view=results&outcome=fail&offset=50&range=all"
        self.page.goto("http://127.0.0.1:8787/" + original)
        link = self.page.get_by_role("link", name="Native result 70", exact=True)
        link.focus()
        link.press("Enter")
        target = self.page.get_by_role("region", name="定位的执行结果")
        expect(target).to_contain_text("SLA")
        expect(target).to_contain_text("250 ms")
        expect(self.page.locator(".report-summary .status").first).to_have_text("不通过")
        expect(target.locator(".state-details")).to_have_count(0)
        self.assertTrue(any(q.get("case") == ["result:70"] for q in self.native_requests))
        self.assertFalse(any("case" in q and "cursor" in q for q in self.native_requests))
        self.page.get_by_role("link", name="返回执行记录", exact=True).click()
        expect(self.page).to_have_url("http://127.0.0.1:8787/" + original)
        expect(self.page.get_by_role("link", name="Native result 70", exact=True)).to_be_focused()

    def test_native_html_contains_every_page_and_its_evidence(self):
        self.page.goto("http://127.0.0.1:8787/#/native-runs/native-70")
        summary = self.page.locator(".report-summary")
        expect(summary.get_by_role("button")).to_have_count(1)
        expect(summary.locator("details")).to_have_count(0)
        expect(summary.locator(".stats")).to_be_visible()
        expect(self.page.locator(".result")).to_have_count(50)
        with self.page.expect_download() as download:
            summary.get_by_role("button", name="下载 HTML 报告", exact=True).click()
        offline = self.browser.new_context(offline=True)
        self.addCleanup(offline.close)
        reader = offline.new_page()
        reader.set_content(download.value.path().read_text(encoding="utf-8"))
        expect(reader.locator(".result")).to_have_count(70)
        final = reader.locator(".result").last
        expect(final.locator(".result-head")).to_contain_text("Native result 70")
        expect(final.locator(".result-head .status")).to_have_count(1)
        expect(final.locator(".result-body .checks")).to_contain_text("250 ms")
        expect(reader.locator(".result-body .state-details")).to_have_count(0)
        for document in (self.page, reader):
            expect(document.get_by_text("本次报告", exact=True)).to_have_count(0)
        self.assertEqual(self.post_count, 0)

    def test_later_problem_and_duplicate_legacy_link_are_explicit(self):
        self.page.goto("http://127.0.0.1:8787/#/native-runs/native-70")
        self.page.get_by_role("link", name="下一页", exact=True).click()
        expect(self.page.locator(".result-list")).to_contain_text("Native result 70")
        self.page.goto("http://127.0.0.1:8787/#/native-runs/native-70?case=duplicate&cursor=second")
        expect(self.page.get_by_role("link", name="查看结果 69", exact=False)).to_be_visible()
        expect(self.page.get_by_role("link", name="查看结果 70", exact=False)).to_be_visible()
        expect(self.page.get_by_role("region", name="定位的执行结果")).to_have_count(0)
        self.assertFalse(any("case" in q and "cursor" in q for q in self.native_requests))

    def test_html_export_still_works_when_excel_generation_failed(self):
        self.run["test_report"] = {"status": "failed", "error": "原工作簿校验失败"}
        self.open()
        report = self.page.locator(".report-summary")
        expect(report.get_by_role("link")).to_have_count(0)
        expect(report.get_by_role("button")).to_have_count(1)
        with self.page.expect_download() as download:
            report.get_by_role("button", name="下载 HTML 报告", exact=True).click()
        self.assertIn("Later failed attempt", download.value.path().read_text(encoding="utf-8"))
        self.assertEqual(self.post_count, 0)

    def failed_case(self):
        return self.page.locator(".result[data-result='2']")

    def test_case_rerun_receipt_names_only_the_selected_case_and_new_run(self):
        self.open()
        with self.page.expect_request(lambda request: request.method == "POST" and request.url.endswith("/rerun")) as sent:
            self.failed_case().get_by_role("button", name="只重跑这一条", exact=True).click()
        self.assertEqual(sent.value.post_data_json["case_id"], "TC-001")
        expect(self.page).to_have_url("http://127.0.0.1:8787/#/runs/run-next")
        expect(self.page.locator("#view")).to_contain_text("已使用当前用例定义提交重新运行，运行编号 run-next")
        self.assertEqual(self.post_count, 1)

    def test_redraw_cannot_duplicate_an_inflight_rerun(self):
        pending = []

        def hold(request):
            self.post_count += 1
            pending.append(request)

        self.page.route('**/api/runs/run-report/rerun', hold)
        self.open()
        self.failed_case().get_by_role('button', name='只重跑这一条', exact=True).click()
        expect(self.page.get_by_role('button', name='正在提交…', exact=True)).to_be_disabled()
        self.redraw()
        self.page.get_by_role('button', name='正在提交…', exact=True).dispatch_event('click')
        self.assertEqual(self.post_count, 1)
        pending[0].fulfill(content_type='application/json', body=json.dumps(self.post_body))
        expect(self.page).to_have_url('http://127.0.0.1:8787/#/runs/run-next')
        self.assertEqual(self.post_count, 1)

    def test_candidate_publication_receipt_remains_readable_without_starting_another_run(self):
        self.run["publication"] = {"candidate_id": "candidate-local", "complete": True, "published_ids": [],
                                   "unpublished": {"API-1": "缺少登录凭据"}}
        self.open()
        receipt = self.page.locator('[aria-label="本批候选入库结果"]')
        receipt.get_by_text("查看未入库原因", exact=True).click()
        expect(receipt).to_contain_text("API-1：缺少登录凭据")
        expect(self.page.get_by_role("button", name="按本次候选版本再次验证", exact=True)).to_have_count(0)
        self.assertEqual(self.post_count, 0)

    def test_workflow_result_returns_to_its_task_without_detached_rerun(self):
        self.run['workflow_id'] = 'workflow-original'
        self.run['publication'] = {'candidate_id': 'candidate-local'}
        self.open()
        expect(self.failed_case().get_by_role('link', name='返回原测试任务', exact=True)).to_have_attribute('href', '#/workflows/workflow-original')
        expect(self.page.get_by_role('button', name='按本次候选版本再次验证', exact=True)).to_have_count(0)
        expect(self.page.get_by_role('button', name='按当前用例重新运行', exact=True)).to_have_count(0)
        self.assertEqual(self.post_count, 0)
