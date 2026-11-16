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
        self.download_status = 200
        self.report_status = "ready"
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
        if path == "/api/task-reports/r":
            return {**self.run["test_report"], "report_id": "r", "status": self.report_status, "error": "报告文件校验失败"}
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
            else:
                start = 50 if query.get("cursor") else 0
                limit = int(query.get("limit", [50])[0])
                result["results"] = result["results"][start:start + limit]
                result["page"] = {"has_more": start + limit < 70, "next_cursor": "second"}
            request.fulfill(content_type="application/json", body=json.dumps(result))
        elif url.path in ("/api/task-reports/r/markdown", "/api/task-reports/r/bugs"):
            request.fulfill(status=self.download_status, content_type="text/markdown" if self.download_status == 200 else "application/json",
                            body="# 本次结果\n\n业务失败仍保留" if self.download_status == 200 else json.dumps({"detail": "报告文件校验失败"}))
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
        expect(self.page.locator("#view")).to_contain_text("性能目标未通过")
        self.assertTrue(any(q.get("case") == ["result:70"] for q in self.native_requests))
        self.assertFalse(any("case" in q and "cursor" in q for q in self.native_requests))
        self.page.get_by_role("link", name="返回执行记录", exact=True).click()
        expect(self.page).to_have_url("http://127.0.0.1:8787/" + original)
        expect(self.page.get_by_role("link", name="Native result 70", exact=True)).to_be_focused()

    def test_later_problem_and_duplicate_legacy_link_are_explicit(self):
        self.page.goto("http://127.0.0.1:8787/#/native-runs/native-70")
        self.page.get_by_role("button", name="查看本次所有未通过结果", exact=True).click()
        issues = self.page.locator('[aria-label="本次未通过结果"]')
        expect(issues).to_contain_text("Native result 70")
        expect(issues).not_to_contain_text("Native result 69")
        self.page.goto("http://127.0.0.1:8787/#/native-runs/native-70?case=duplicate&cursor=second")
        expect(self.page.get_by_role("link", name="查看结果 69", exact=False)).to_be_visible()
        expect(self.page.get_by_role("link", name="查看结果 70", exact=False)).to_be_visible()
        expect(self.page.get_by_role("region", name="定位的执行结果")).to_have_count(0)
        self.assertFalse(any("case" in q and "cursor" in q for q in self.native_requests))

    def test_cancel_rejection_and_unknown_never_claim_success(self):
        self.run.update(status="Running", cancel=0)
        self.post_status = 409
        self.post_body = {"detail": "执行器拒绝取消"}
        self.open()
        self.page.get_by_role("button", name="取消运行", exact=True).click()
        expect(self.page.get_by_role("alertdialog")).to_have_count(0)
        expect(self.page.locator("#view")).to_contain_text("请求被拒绝：执行器拒绝取消")
        expect(self.page.locator("#view")).not_to_contain_text("取消已申请")
        self.post_status = 500
        self.post_body = {"detail": "连接已中断"}
        self.page.get_by_role("button", name="取消运行", exact=True).click()
        expect(self.page.locator("#view")).to_contain_text("操作可能已经生效")
        expect(self.page.get_by_role("button", name="取消运行", exact=True)).to_be_enabled()
        self.redraw()
        expect(self.page.locator("#view")).to_contain_text("操作可能已经生效")
        self.assertEqual(self.post_count, 2)

    def test_cancel_waiting_and_already_finished_are_distinct(self):
        self.run.update(status="Running", cancel=0)
        self.post_body = {"id": "run-report", "status": "Running", "cancel": 1}
        self.open()
        self.page.get_by_role("button", name="取消运行", exact=True).click()
        expect(self.page.locator("#view")).to_contain_text("取消已申请，正在等待")
        expect(self.page.get_by_role("button", name="等待取消结果", exact=True)).to_be_disabled()
        self.run.update(status="Cancelled", cancel=1)
        self.redraw()
        expect(self.page.locator("#view")).to_contain_text("本次运行已取消；已产生的结果仍然保留")
        expect(self.page.locator("#view")).not_to_contain_text("取消已申请，正在等待")
        self.run.update(status="Running", cancel=0)
        self.page.reload()
        self.post_body = {"id": "run-report", "status": "Fail", "cancel": 0}
        self.page.get_by_role("button", name="取消运行", exact=True).click()
        expect(self.page.locator("#view")).to_contain_text("已经结束，取消没有改变")

    def test_download_failure_persists_and_retry_returns_real_file(self):
        self.run["test_report"]["report_id"] = "r"
        self.download_status = 404
        self.open()
        self.page.get_by_role("link", name="下载 Markdown 汇总", exact=True).click()
        expect(self.page.locator('[aria-label="本次报告"]')).to_contain_text("下载失败：报告文件校验失败")
        self.redraw()
        expect(self.page.locator('[aria-label="本次报告"]')).to_contain_text("下载失败：报告文件校验失败")
        self.download_status = 200
        with self.page.expect_download() as download:
            self.page.get_by_role("link", name="下载 Markdown 汇总", exact=True).click()
        path = self.folder / 'report.md'
        download.value.save_as(path)
        self.assertIn("业务失败仍保留", path.read_text())
        self.download_status = 404
        self.page.get_by_role("link", name="下载 Bug 清单", exact=True).click()
        expect(self.page.locator('[aria-label="本次报告"]')).to_contain_text("下载失败：报告文件校验失败")
        expect(self.page.locator('[aria-label="本次报告"]')).not_to_contain_text("文件已取回")
        self.assertEqual(self.post_count, 0)

    def test_rerun_receipt_names_current_scope_and_dropped_count(self):
        self.open()
        self.page.get_by_role("button", name="按当前用例重新运行", exact=True).click()
        expect(self.page).to_have_url("http://127.0.0.1:8787/#/runs/run-next")
        expect(self.page.locator("#view")).to_contain_text("有 2 条用例已不存在，本次跳过")
        self.assertEqual(self.post_count, 1)

    def test_redraw_cannot_duplicate_an_inflight_rerun(self):
        pending = []

        def hold(request):
            self.post_count += 1
            pending.append(request)

        self.page.route('**/api/runs/run-report/rerun', hold)
        self.open()
        self.page.get_by_role('button', name='按当前用例重新运行', exact=True).click()
        expect(self.page.get_by_role('button', name='正在提交…', exact=True)).to_be_disabled()
        self.redraw()
        self.page.get_by_role('button', name='正在提交…', exact=True).dispatch_event('click')
        self.assertEqual(self.post_count, 1)
        pending[0].fulfill(content_type='application/json', body=json.dumps(self.post_body))
        expect(self.page).to_have_url('http://127.0.0.1:8787/#/runs/run-next')
        self.assertEqual(self.post_count, 1)

    def test_candidate_zero_prepared_keeps_reason_and_does_not_claim_a_run(self):
        self.run['publication'] = {'candidate_id': 'candidate-local'}
        self.post_body = {'id': None, 'total': 0, 'candidate_id': 'candidate-local', 'run_ids': [],
                          'publication': {'complete': True, 'status': 'complete', 'published_ids': [],
                                          'unpublished': {'API-1': '缺少登录凭据'}}}
        self.open()
        self.page.get_by_role('button', name='按本次候选版本再次验证', exact=True).click()
        expect(self.page.locator('#view')).to_contain_text('本次没有候选加入执行队列')
        expect(self.page.locator('#view')).to_contain_text('API-1：缺少登录凭据')
        expect(self.page.locator('#view')).not_to_contain_text('已加入执行队列')
        expect(self.page).to_have_url('http://127.0.0.1:8787/#/runs/run-report')
        self.redraw()
        expect(self.page.locator('#view')).to_contain_text('缺少登录凭据')
        expect(self.page.get_by_role('button', name='按本次候选版本再次验证', exact=True)).to_be_enabled()
        self.assertEqual(self.post_count, 1)

    def test_candidate_partial_preparation_lists_real_runs_and_missing_targets(self):
        self.run['publication'] = {'candidate_id': 'candidate-local'}
        self.next_status = 'Queued'
        self.post_body = {'id': 'run-next', 'total': 2, 'candidate_id': 'candidate-local', 'run_ids': ['run-next', 'run-other'],
                          'publication': {'complete': False, 'status': 'pending', 'published_ids': [], 'unpublished': {}},
                          'preparation_errors': {'API-3': '前置用例不存在'}}
        self.open()
        self.page.get_by_role('button', name='按本次候选版本再次验证', exact=True).click()
        expect(self.page).to_have_url('http://127.0.0.1:8787/#/runs/run-next')
        expect(self.page.locator('#view')).to_contain_text('使用本次候选版本')
        expect(self.page.locator('#view')).to_contain_text('已加入执行队列')
        expect(self.page.locator('#view')).to_contain_text('API-3：前置用例不存在')
        expect(self.page.get_by_role('link', name='查看运行 run-other', exact=True)).to_have_attribute('href', '#/runs/run-other')
        self.next_status = 'Running'
        self.redraw()
        expect(self.page.locator('#view')).to_contain_text('本次运行正在执行')
        expect(self.page.locator('#view')).not_to_contain_text('已加入执行队列')
        self.next_status = 'Pass'
        self.redraw()
        expect(self.page.locator('#view')).to_contain_text('本次运行已结束')
        expect(self.page.locator('#view')).not_to_contain_text('已加入执行队列')
        expect(self.page.locator('#view')).to_contain_text('API-3：前置用例不存在')
        expect(self.page.get_by_role('link', name='查看运行 run-other', exact=True)).to_be_visible()
        self.assertEqual(self.post_count, 1)

    def test_workflow_result_returns_to_its_task_without_detached_rerun(self):
        self.run['workflow_id'] = 'workflow-original'
        self.run['publication'] = {'candidate_id': 'candidate-local'}
        self.open()
        expect(self.page.get_by_role('link', name='返回原测试任务', exact=True)).to_have_attribute('href', '#/workflows/workflow-original')
        expect(self.page.get_by_role('button', name='按本次候选版本再次验证', exact=True)).to_have_count(0)
        expect(self.page.get_by_role('button', name='按当前用例重新运行', exact=True)).to_have_count(0)
        self.assertEqual(self.post_count, 0)
