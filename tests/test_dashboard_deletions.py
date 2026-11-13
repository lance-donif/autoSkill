"""Exercise deletion controls in Chromium with isolated HTTP API contract fixtures."""
import json
import mimetypes
import re
import sys
import unittest
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "skills/autoskill/app/ui"
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill import paths


class DashboardDeletionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        executable = paths.chrome()
        if not executable:
            raise unittest.SkipTest("Dashboard tests need an installed or bundled browser")
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        cls.browser = cls.playwright.chromium.launch(executable_path=executable, headless=True, args=["--no-sandbox"])
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 1440, "height": 1000})
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.cases = [self.case("one", "TC-001", "Alpha case"), self.case("one", "TC-002", "Beta case"),
                      self.case("two", "TC-003", "Other project case")]
        self.runs = [self.make_run("run-1"), self.make_run("run-2")]
        self.mutations = []
        self.project_reads = 0
        self.case_errors = set()
        self.case_warnings = {}
        self.run_errors = set()
        self.offline_runs = set()
        self.finish_deleting = set()
        self.page.route("**/*", self.handle_request)

    def tearDown(self):
        self.assertEqual(self.errors, [])

    @staticmethod
    def case(project_id, case_id, title):
        return {"project_id": project_id, "case_id": case_id, "title": title, "kind": "api", "module": "Example",
                "depends_on": "[]", "last_status": None, "last_run_at": None}

    @staticmethod
    def make_run(run_id, status="Pass"):
        return {"id": run_id, "project_id": "one", "project_name": "Project one", "status": status,
                "total": 1, "passed": int(status == "Pass"), "failed": 0, "blocked": 0, "done": int(status == "Pass"),
                "engine": "playwright", "trigger": "manual", "created_at": "2026-11-13T00:00:00Z",
                "started_at": "2026-11-13T00:00:00Z", "finished_at": None, "delete_requested": 0,
                "deletion_error": "", "results": []}

    def projects(self):
        return [{"id": project_id, "name": "Project " + project_id, "category": "", "base_url": "http://example.invalid",
                 "cases": {"total": sum(item["project_id"] == project_id for item in self.cases), "ui": 0,
                           "api": sum(item["project_id"] == project_id for item in self.cases)},
                 "login": {"ui_case": None, "api_case": None}, "missing_secrets": []} for project_id in ("one", "two")]

    def delete_cases(self, items):
        result = {"deleted": [], "pending": [], "errors": []}
        for item in items:
            if item["case_id"] in self.case_errors:
                result["errors"].append({**item, "code": "case_in_use", "message": "另一条用例仍引用此用例"})
            else:
                self.cases = [case for case in self.cases
                              if (case["project_id"], case["case_id"]) != (item["project_id"], item["case_id"])]
                result["deleted"].append({**item, "warnings": self.case_warnings.get(item["case_id"], [])})
        return result

    def delete_runs(self, run_ids):
        result = {"deleted": [], "pending": [], "errors": []}
        for run_id in run_ids:
            run = next((run for run in self.runs if run["id"] == run_id), None)
            if run_id in self.run_errors:
                result["errors"].append({"run_id": run_id, "code": "delete_failed", "message": "附件被占用，请重试删除"})
            elif run and run["status"] in ("Queued", "Running"):
                run["delete_requested"] = 1
                result["pending"].append({"run_id": run_id, "status": "cancelling", "message": "等待执行器完整退出"})
            else:
                self.runs = [item for item in self.runs if item["id"] != run_id]
                result["deleted"].append({"run_id": run_id, "already_deleted": run is None})
        return result

    def run_response(self, request, path, query):
        if path == "/api/runs/delete":
            return 200, self.delete_runs(request.post_data_json["run_ids"])
        if path == "/api/runs":
            items = self.runs
            status = query.get("status", [""])[0]
            if status == "active":
                items = [run for run in items if run["status"] in ("Queued", "Running")]
            elif status:
                items = [run for run in items if run["status"] == status]
            offset, limit = int(query.get("offset", [0])[0]), int(query.get("limit", [50])[0])
            return 200, {"items": items[offset:offset + limit], "total": len(items)}
        run_id = unquote(path.removeprefix("/api/runs/"))
        if request.method == "DELETE":
            return 200, self.delete_runs([run_id])
        if run_id in self.finish_deleting:
            self.runs = [run for run in self.runs if run["id"] != run_id]
        run = next((run for run in self.runs if run["id"] == run_id), None)
        return (200, run) if run else (404, {"detail": "运行不存在"})

    def case_response(self, request, path):
        if path == "/api/cases/delete":
            return 200, self.delete_cases(request.post_data_json["items"])
        if path == "/api/cases":
            return 200, {"items": self.cases, "total": len(self.cases)}
        parts = path.split("/")
        project_id, case_id = unquote(parts[3]), unquote(parts[5])
        if request.method == "DELETE":
            return 200, self.delete_cases([{"project_id": project_id, "case_id": case_id}])
        case = next(item for item in self.cases if (item["project_id"], item["case_id"]) == (project_id, case_id))
        return 200, {**case, "id": case_id, "depends_on": [], "revision": 1, "replayable": True,
                     "request": {"method": "GET", "path": "/example"}, "assertions": {"status": 200}}

    def api_response(self, request, path, query):
        if request.method in ("DELETE", "POST"):
            self.mutations.append((request.method, path, request.post_data_json if request.post_data else None))
        if path == "/api/projects":
            self.project_reads += 1
            return 200, self.projects()
        if path == "/api/settings":
            return 200, {"data_id": "fixture-data", "dashboard": {}, "legacy_dashboard_import_eligible": False}
        if path == "/api/status":
            return 200, {"browser": {}, "runtime": {}, "skill_path": "/synthetic/skill"}
        if path.endswith("/folders"):
            project_id = path.split("/")[3]
            return 200, {"project_id": project_id, "revision": "fixture-folders", "folders": [],
                         "assignments": {item["case_id"]: None for item in self.cases if item["project_id"] == project_id}}
        if path == "/api/state-revision":
            return 200, {"revision": "fixture"}
        if path.startswith("/api/runs"):
            return self.run_response(request, path, query)
        if path.startswith("/api/cases") or "/cases/" in path:
            return self.case_response(request, path)
        raise AssertionError("Unexpected API request: " + path)

    def handle_request(self, route):
        url = urlsplit(route.request.url)
        if route.request.method == "GET" and url.path.removeprefix("/api/runs/") in self.offline_runs:
            route.abort("connectionfailed")
            return
        if url.path.startswith("/api/"):
            status, data = self.api_response(route.request, url.path, parse_qs(url.query))
            route.fulfill(status=status, content_type="application/json", body=json.dumps(data))
            return
        file = UI / (url.path.lstrip("/") or "index.html")
        if file.is_file():
            media_type = "text/javascript" if file.suffix == ".js" else mimetypes.guess_type(str(file))[0] or "text/plain"
            route.fulfill(status=200, content_type=media_type, body=file.read_bytes())
        else:
            route.fulfill(status=404, body="Not found")

    def open(self, path):
        self.page.goto("http://127.0.0.1:8787/#/" + path)
        expect(self.page.locator("#view h1")).to_be_visible()

    def confirm(self, action):
        self.page.get_by_role("alertdialog").get_by_role("button", name=action, exact=True).click()

    def test_case_row_cancel_and_detail_delete_preserve_scope_and_other_cases(self):
        self.open("cases?projects=one&range=all")
        self.page.get_by_role("button", name="删除用例 TC-001", exact=True).click()
        self.confirm("取消")
        self.assertEqual(self.mutations, [])
        self.page.get_by_text("Alpha case", exact=True).click()
        self.page.get_by_role("dialog", name="Alpha case", exact=True).get_by_role("button", name="删除用例", exact=True).click()
        self.confirm("取消")
        expect(self.page.get_by_role("dialog", name="Alpha case", exact=True)).to_be_visible()
        self.assertEqual(self.mutations, [])
        self.page.get_by_role("dialog", name="Alpha case", exact=True).get_by_role("button", name="删除用例", exact=True).click()
        self.confirm("删除用例")
        expect(self.page.get_by_text("Alpha case", exact=True)).to_have_count(0)
        expect(self.page.get_by_text("Beta case", exact=True)).to_be_visible()
        self.assertEqual(self.mutations, [("DELETE", "/api/projects/one/cases/TC-001", None)])
        self.assertEqual({case["case_id"] for case in self.cases}, {"TC-002", "TC-003"})
        self.assertIn("projects=one&range=all", self.page.url)
        self.assertGreater(self.project_reads, 1)

    def test_case_batch_partial_failure_clears_selection_and_retains_error(self):
        self.case_errors.add("TC-002")
        self.case_warnings["TC-001"] = ["用例已删除；用例表被占用，派生展示刷新未完成"]
        self.open("cases?range=all")
        for case_id in ("TC-001", "TC-002", "TC-003"):
            self.page.get_by_role("checkbox", name="选择 " + case_id, exact=True).check()
        self.page.get_by_role("button", name="删除所选", exact=True).click()
        self.confirm("删除所选")
        expect(self.page.get_by_role("alert")).to_contain_text("TC-002：另一条用例仍引用此用例")
        expect(self.page.locator("#view").get_by_role("status")).to_contain_text("TC-001：用例已删除；用例表被占用")
        expect(self.page.locator("#view .select-bar")).to_be_hidden()
        expect(self.page.get_by_role("checkbox", name="选择 TC-002", exact=True)).not_to_be_checked()
        self.assertEqual([case["case_id"] for case in self.cases], ["TC-002"])
        self.assertEqual(self.mutations[0][1], "/api/cases/delete")
        self.assertEqual(len(self.mutations[0][2]["items"]), 3)
        self.assertGreater(self.project_reads, 1)

    def test_case_row_delete_keeps_search_and_other_project(self):
        self.open("cases?range=all")
        self.page.get_by_role("searchbox", name="搜索用例", exact=True).fill("Alpha")
        self.page.get_by_role("button", name="删除用例 TC-001", exact=True).click()
        self.confirm("删除用例")
        expect(self.page.get_by_role("searchbox", name="搜索用例", exact=True)).to_have_value("Alpha")
        expect(self.page.get_by_text("Alpha case", exact=True)).to_have_count(0)
        self.assertEqual({case["case_id"] for case in self.cases}, {"TC-002", "TC-003"})
        self.assertEqual(self.mutations, [("DELETE", "/api/projects/one/cases/TC-001", None)])

    def test_run_batch_delete_returns_to_previous_page_and_keeps_filters(self):
        self.runs = [self.make_run(f"run-{number:03}") for number in range(1, 103)]
        self.open("runs?projects=one&range=all&status=Pass&offset=100")
        self.page.get_by_role("checkbox", name="选择本页全部运行", exact=True).check()
        self.page.get_by_role("button", name="删除所选", exact=True).click()
        self.confirm("取消")
        self.assertEqual(self.mutations, [])
        self.page.get_by_role("button", name="删除所选", exact=True).click()
        self.confirm("删除运行")
        expect(self.page).to_have_url(re.compile(r"[?&]offset=50(?:&|$)"))
        expect(self.page.locator("#view .table tbody tr")).to_have_count(50)
        expect(self.page.get_by_text("51–100 / 100", exact=True)).to_be_visible()
        expect(self.page.locator("#view .select-bar")).to_be_hidden()
        query = parse_qs(urlsplit(self.page.url).fragment.partition("?")[2])
        self.assertEqual((query["projects"], query["range"], query["status"]), (["one"], ["all"], ["Pass"]))
        self.assertEqual(self.mutations, [("POST", "/api/runs/delete", {"run_ids": ["run-101", "run-102"]})])

    def test_completed_run_detail_delete_returns_to_filtered_history(self):
        self.open("runs?projects=one&range=all&status=Pass")
        self.page.locator("#view .table tbody tr").first.click()
        expect(self.page.locator("#view h1")).to_have_text("Project one")
        self.page.get_by_role("button", name="删除运行", exact=True).click()
        self.confirm("删除运行")
        expect(self.page.locator("#view h1")).to_have_text("执行记录")
        expect(self.page.locator("#view .table tbody tr")).to_have_count(1)
        self.assertEqual(self.mutations, [("DELETE", "/api/runs/run-1", None)])
        self.assertIn("status=Pass", self.page.url)
        self.assertGreater(self.project_reads, 1)

    def test_running_delete_remains_pending_through_disconnect_until_confirmed_missing(self):
        self.runs[0] = self.make_run("run-1", "Running")
        self.offline_runs.add("run-1")
        self.open("runs?range=all")
        self.page.get_by_role("button", name="取消并删除 run-1", exact=True).click()
        self.confirm("取消并删除")
        expect(self.page.locator("#view").get_by_role("status")).to_contain_text("正在取消并删除")
        expect(self.page.locator("#view").get_by_role("status")).to_contain_text("暂时无法确认删除结果", timeout=6000)
        expect(self.page.get_by_role("checkbox", name="选择运行 run-1", exact=True)).to_be_disabled()
        expect(self.page.locator("#toasts")).not_to_contain_text("已删除")
        expect(self.page.locator("#view .table tbody tr")).to_have_count(2)
        self.offline_runs.clear()
        self.finish_deleting.add("run-1")
        expect(self.page.get_by_role("checkbox", name="选择运行 run-1", exact=True)).to_have_count(0, timeout=6000)
        expect(self.page.locator("#toasts")).to_contain_text("已删除 1 次运行")
        expect(self.page.get_by_role("checkbox", name="选择运行 run-2", exact=True)).to_be_visible()
        self.assertEqual(self.mutations, [("DELETE", "/api/runs/run-1", None)])

    def test_run_batch_mixed_outcomes_and_cleanup_error_allow_retry(self):
        self.runs.append(self.make_run("run-3", "Running"))
        self.run_errors.add("run-2")
        self.open("runs?range=all")
        self.page.get_by_role("checkbox", name="选择本页全部运行", exact=True).check()
        self.page.get_by_role("button", name="取消并删除所选", exact=True).click()
        self.confirm("取消并删除")
        expect(self.page.get_by_role("alert")).to_contain_text("run-2：附件被占用")
        expect(self.page.locator("#view").get_by_role("status")).to_contain_text("1 次运行正在取消并删除")
        expect(self.page.locator("#toasts")).to_contain_text("已删除 1 次运行，1 次正在取消并删除，1 次未删除")
        expect(self.page.locator("#view .select-bar")).to_be_hidden()
        pending = next(run for run in self.runs if run["id"] == "run-3")
        pending.update(status="Cancelled", deletion_error="合成清理错误")
        expect(self.page.get_by_role("button", name="重试删除 run-3", exact=True)).to_be_visible(timeout=6000)
        expect(self.page.get_by_role("alert")).to_contain_text("run-3：合成清理错误")
        self.page.get_by_role("button", name="重试删除 run-3", exact=True).click()
        self.confirm("删除运行")
        expect(self.page.get_by_role("checkbox", name="选择运行 run-3", exact=True)).to_have_count(0)
        expect(self.page.get_by_role("checkbox", name="选择运行 run-2", exact=True)).to_be_visible()

    def test_running_report_waits_for_removal_before_returning_to_history(self):
        self.runs[0] = self.make_run("run-1", "Running")
        self.open("runs/run-1?projects=one&range=all")
        self.page.get_by_role("button", name="取消并删除", exact=True).click()
        self.confirm("取消")
        self.assertEqual(self.mutations, [])
        self.page.get_by_role("button", name="取消并删除", exact=True).click()
        self.confirm("取消并删除")
        expect(self.page.get_by_role("button", name="正在取消并删除", exact=True)).to_be_disabled()
        expect(self.page.locator("#view h1")).to_have_text("Project one")
        expect(self.page.locator("#toasts")).not_to_contain_text("已删除")
        self.finish_deleting.add("run-1")
        expect(self.page.locator("#view h1")).to_have_text("执行记录", timeout=6000)
        expect(self.page.locator("#view .table tbody tr")).to_have_count(1)
        self.assertIn("projects=one&range=all", self.page.url)

    def test_reloaded_pending_run_resumes_observation_without_new_delete_request(self):
        self.runs[0].update(delete_requested=1, status="Cancelled")
        self.open("runs?range=all")
        expect(self.page.locator("#view").get_by_role("status")).to_contain_text("正在取消并删除")
        self.finish_deleting.add("run-1")
        expect(self.page.get_by_role("checkbox", name="选择运行 run-1", exact=True)).to_have_count(0, timeout=6000)
        expect(self.page.locator("#toasts")).to_contain_text("已删除 1 次运行")
        self.assertEqual(self.mutations, [])


if __name__ == "__main__":
    unittest.main()
