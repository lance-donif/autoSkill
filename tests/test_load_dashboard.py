"""Use Chromium and real HTTP services to verify saved-case load entry points and retained failures."""
import contextlib
import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx
import openpyxl
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "skills/autoskill/app"
sys.path.insert(0, str(APP))

from autoskill import builder, db, paths, projects, runtime_identity, sessions, task_report_data, workflow
from autoskill.server import jobs, scheduler


class LoadTarget(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return

    def send(self, body, *, cookie=None, html=False):
        raw = body.encode() if html else json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8" if html else "application/json")
        self.send_header("Content-Length", str(len(raw)))
        if cookie is not None:
            self.send_header("Set-Cookie", "marker=" + cookie + "; Path=/")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query)
        state = self.server.fixture
        if parsed.path == "/ui":
            marker = uuid.uuid4().hex
            with state["lock"]:
                state["ui_initial_cookies"].append(self.headers.get("Cookie", ""))
            page = """<!doctype html><html lang="zh-CN"><body>
<button data-testid="check">验证会话</button><p data-testid="result">待执行</p>
<script>document.querySelector('button').onclick = async () => {
  const response = await fetch('/ui-work'); const result = await response.json();
  document.querySelector('p').textContent = result.ok ? '通过' : '失败';
};</script></body></html>"""
            self.send(page, cookie=marker, html=True)
            return
        if parsed.path == "/begin":
            marker = query.get("rand", [""])[0]
            with state["lock"]:
                state["api_initial_cookies"].append(self.headers.get("Cookie", ""))
                state["api_tokens"].append(marker)
            self.send({"token": marker, "ok": True}, cookie=marker)
            return
        if parsed.path == "/changing":
            with state["lock"]:
                state["changing_calls"] += 1
                current = state["changing_calls"]
            self.send({"ok": current > 1})
            return
        if parsed.path in ("/check", "/slow", "/ui-work"):
            with state["lock"]:
                state["active"] += 1
                if parsed.path == "/slow":
                    state["slow_started"] += 1
                state["maximum"] = max(state["maximum"], state["active"])
                if parsed.path == "/ui-work":
                    state["ui_tokens"].append(self.headers.get("Cookie", ""))
            try:
                time.sleep(0.25)
                marker = query.get("token", [""])[0]
                ok = (self.headers.get("Cookie", "") == "marker=" + marker) if parsed.path == "/check" else True
                self.send({"ok": ok})
            finally:
                with state["lock"]:
                    state["active"] -= 1
            return
        self.send_error(404)


class DashboardWorkspace(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        executable = paths.chrome()
        if not executable:
            raise unittest.SkipTest("Load dashboard tests need an installed or bundled browser")
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        cls.browser = cls.playwright.chromium.launch(executable_path=executable, headless=True, args=["--no-sandbox"])
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.folder / "workspace"), "AUTOSKILL_HEADED": "0",
                                               "AUTOSKILL_SESSION": "", "AUTOSKILL_RUNTIME_CONTEXT": "",
                                               "AUTOSKILL_WORKFLOW_BINDING": "", "AUTOSKILL_BRIDGE": "off",
                                               "AUTOSKILL_QUEUE_IDLE_SECONDS": "0.1", "PYTHONPATH": str(APP)})
        environment.start()
        self.addCleanup(environment.stop)
        self.target = ThreadingHTTPServer(("127.0.0.1", 0), LoadTarget)
        self.target.fixture = {"lock": threading.Lock(), "active": 0, "maximum": 0, "changing_calls": 0, "slow_started": 0,
                               "api_tokens": [], "api_initial_cookies": [], "ui_tokens": [], "ui_initial_cookies": []}
        threading.Thread(target=self.target.serve_forever, daemon=True).start()
        self.addCleanup(self.target.server_close)
        self.addCleanup(self.target.shutdown)
        project = projects.create("页面压测验收", f"http://127.0.0.1:{self.target.server_port}", project_id="load-dashboard")
        self.pid = project["id"]
        projects.save_cases(self.pid, [
            builder.api_case("TC-BEGIN", "独立登录前置", "GET", "/begin", {"status": 200, "json_equals": {"/ok": True}},
                             params={"rand": "{{RAND}}"}, extract={"token": "/token"}),
            builder.api_case("TC-CHECK", "接口会话校验", "GET", "/check", {"status": 200, "json_equals": {"/ok": True}},
                             params={"token": "{{token}}"}, depends_on=["TC-BEGIN"]),
            builder.api_case("TC-SLOW", "可取消慢请求", "GET", "/slow", {"status": 200, "json_equals": {"/ok": True}}),
            builder.api_case("TC-CHANGING", "原始断言先失败后通过", "GET", "/changing", {"status": 200, "json_equals": {"/ok": True}}),
            builder.ui_case("TC-UI", "页面独立浏览器会话", {"start_url": "/ui", "actions": [
                {"action": "click", "locators": [{"by": "testid", "value": "check"}]}], "assertions": [
                {"assertion": "text", "locators": [{"by": "testid", "value": "result"}], "expected": "通过"}]}),
        ])
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.origin = f"http://127.0.0.1:{port}"
        self.log = self.folder / "dashboard.log"
        with self.log.open("wb") as output:
            self.process = subprocess.Popen([sys.executable, "-m", "autoskill", "dashboard", "--port", str(port), "--headless"],
                                            env=os.environ.copy(), stdout=output, stderr=output)
        self.addCleanup(self.stop_process)
        self.client = httpx.Client(base_url=self.origin, trust_env=False, timeout=10)
        self.addCleanup(self.client.close)
        self.wait_for_server()
        settings = self.client.get("/api/settings").json()
        self.client.put("/api/settings", json={"dashboard": {"headed": False}, "expected_data_id": settings["data_id"]}).raise_for_status()
        self.context = self.browser.new_context(viewport={"width": 1440, "height": 1100}, accept_downloads=True)
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))

    def stop_process(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        # Release fixture-owned host leases so the independent worker can retire.
        for task in scheduler.list_all():
            if task["enabled"]:
                scheduler.update(task["id"], {"enabled": False})
        if db.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_items'"):
            leases = db.rows("SELECT i.id,i.workflow_id,a.id AS attempt_id,a.lease_token,a.checkpoint FROM workflow_items i "
                             "JOIN workflow_attempts a ON a.id=i.attempt_id JOIN workflow_runtime f ON f.id=i.workflow_id "
                             "WHERE i.state='leased' AND f.lifecycle='active'")
            for lease in leases:
                with contextlib.suppress(workflow.WorkflowError):
                    workflow.release(lease["workflow_id"], lease["id"], lease_token=lease["lease_token"],
                                     attempt_id=lease["attempt_id"], idempotency_key=uuid.uuid4().hex,
                                     checkpoint=json.loads(lease["checkpoint"]))
        for run in db.rows("SELECT id FROM runs WHERE status IN ('Queued','Running')"):
            jobs.cancel(run["id"])
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            # Synchronous execution in this test process has already finished writing.
            active = any(row["pid"] != os.getpid() and runtime_identity.owns(row["pid"], row["owner_identity"])
                         for row in db.rows("SELECT pid,owner_identity FROM runs WHERE pid IS NOT NULL"))
            if not active and not (self.folder / "workspace/.local/queue-worker.json").exists():
                return
            time.sleep(0.05)
        self.fail("Fixture writers did not stop before temporary workspace cleanup")

    def wait_for_server(self):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.fail("Dashboard exited:\n" + self.log.read_text(encoding="utf-8"))
            with contextlib.suppress(httpx.HTTPError):
                if self.client.get("/api/status").status_code == 200:
                    return
            time.sleep(0.05)
        self.fail("Dashboard did not become ready:\n" + self.log.read_text(encoding="utf-8"))

    def tearDown(self):
        with contextlib.suppress(httpx.HTTPError):
            for run in self.client.get("/api/load-tests").json().get("items", []):
                if run["status"] in ("Queued", "Running"):
                    self.client.post("/api/load-tests/" + run["id"] + "/cancel", json={})
        self.assertEqual(self.errors, [])

    def open(self, route):
        self.page.goto(self.origin + "/#/" + route)
        expect(self.page.locator("#view h1")).to_be_visible()

    def editor_from_case(self, case_id):
        self.open("cases?range=all")
        self.page.get_by_role("checkbox", name="选择 " + case_id, exact=True).check()
        self.page.get_by_role("button", name="压测所选", exact=True).click()
        return self.page.get_by_role("dialog", name="新建压测", exact=True)

    def start(self, dialog, *, concurrency=2, target_requests=4):
        dialog.get_by_label("并发数", exact=True).fill(str(concurrency))
        dialog.get_by_label("目标请求数", exact=True).fill(str(target_requests))
        dialog.get_by_label("持续时间（秒）", exact=True).fill("")
        return self.submit(dialog, "performance")

    def submit(self, dialog, mode):
        with self.page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/api/load-tests")) as response:
            dialog.get_by_role("button", name="开始压测", exact=True).click()
        self.assertEqual(response.value.status, 200, response.value.text())
        payload = response.value.request.post_data_json
        self.assertEqual(payload["mode"], mode)
        self.assertNotIn("engine", payload)
        self.assertNotIn("headed", payload)
        run_id = response.value.json()["runs"][0]["id"]
        expect(self.page).to_have_url(re.compile(r"/#/load-tests/" + run_id))
        return run_id

    def wait_finished(self, run_id):
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            run = self.client.get("/api/load-tests/" + run_id).json()
            if run["status"] not in ("Queued", "Running"):
                return run
            time.sleep(0.1)
        self.fail("Load did not finish:\n" + self.log.read_text(encoding="utf-8"))
        return None

    def statistic_cells(self, document, label="总计"):
        table = document.get_by_role("table", name="统计", exact=True)
        return table.get_by_role("row").filter(has=document.get_by_role("rowheader", name=label, exact=True)).locator("th, td")

    def evidence(self, name):
        destination = os.environ.get("AUTOSKILL_VALIDATION_OUTPUT")
        if destination:
            folder = Path(destination)
            folder.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(folder / name), full_page=True)


class LoadDashboardTests(DashboardWorkspace):
    def setUp(self):
        super().setUp()
        output = os.environ.get("AUTOSKILL_VALIDATION_OUTPUT")
        if output:
            diagnostics = Path(output) / self._testMethodName
            diagnostics.mkdir(parents=True, exist_ok=True)
            self.context.tracing.start(screenshots=True, snapshots=True)
            self.addCleanup(self.context.tracing.stop, path=diagnostics / "trace.zip")
            self.addCleanup(self.page.screenshot, path=diagnostics / "page.png", timeout=5000)

    def test_case_selection_loads_real_concurrent_requests_and_preserves_history(self):
        run_id = self.start(self.editor_from_case("TC-CHECK"))
        run = self.wait_finished(run_id)
        self.assertEqual((run["status"], run["done"], run["total"], run["passed"]), ("Pass", 4, 4, 4), run)
        self.assertEqual(run["engine"], "locust")
        self.assertEqual((run["metrics"]["request_count"], run["metrics"]["latency_samples"]), (4, 4))
        self.assertEqual([s["label"] for s in run["load_report"]["samples"]], ["TC-CHECK"] * 4)
        self.assertGreater(run["metrics"]["throughput_per_second"], 0)
        self.assertGreaterEqual(self.target.fixture["maximum"], 2)
        self.assertEqual(len(set(self.target.fixture["api_tokens"])), 4)
        self.assertEqual(self.target.fixture["api_initial_cookies"], [""] * 4)
        self.assertEqual(sessions.load(self.pid), {})
        targets = [row for row in run["results"] if not row["prerequisite"]]
        self.assertEqual([row["case_id"] for row in targets], ["TC-CHECK"] * 4)
        expect(self.statistic_cells(self.page).nth(1)).to_have_text("4", timeout=15000)
        expect(self.page.get_by_role("table", name="统计", exact=True)).to_contain_text("P95")
        self.evidence("api-result.png")
        self.page.get_by_role("link", name="压测记录", exact=True).click()
        expect(self.page.get_by_role("heading", name="压测记录", exact=True)).to_be_visible()
        self.page.reload()
        self.page.get_by_role("link", name="查看结果", exact=True).click()
        expect(self.statistic_cells(self.page).nth(1)).to_have_text("4")
        self.assertEqual(self.client.get("/api/cases").json()["items"][0]["last_status"], None)

    def test_pressure_tab_sends_staged_api_request_and_displays_measured_samples(self):
        dialog = self.editor_from_case("TC-SLOW")
        dialog.get_by_role("tab", name="压力测试", exact=True).click()
        expect(dialog.get_by_role("checkbox", name=re.compile("TC-UI"))).to_have_count(0)
        for field, value in (("初始并发数", "1"), ("每阶段增加并发数", "1"), ("最大并发数", "2"), ("每阶段持续时间（秒）", "0.8")):
            dialog.get_by_label(field, exact=True).fill(value)
        run_id = self.submit(dialog, "stress")
        run = self.wait_finished(run_id)
        self.assertEqual((run["status"], run["engine"], run["load"]["mode"]), ("Pass", "locust", "stress"), run)
        report = run["load_report"]
        self.assertEqual([stage["concurrency"] for stage in report["stages"]], [1, 2])
        self.assertGreaterEqual(self.target.fixture["maximum"], 2)
        self.assertEqual(self.target.fixture["slow_started"], len(report["samples"]))
        selector = self.page.get_by_role("combobox", name="阶段", exact=True)
        expect(selector.locator("option")).to_have_count(3, timeout=15000)
        for stage in (1, 2):
            count = sum(sample["stage"] == stage for sample in report["samples"])
            self.assertGreater(count, 0)
            selector.select_option(value=str(stage))
            expect(self.statistic_cells(self.page).nth(1)).to_have_text(str(count))

    def test_ui_case_keeps_normal_browser_replay_and_cannot_start_a_load_test(self):
        self.open("cases?range=all")
        ui = self.page.get_by_role("row").filter(has=self.page.get_by_role("checkbox", name="选择 TC-UI", exact=True))
        expect(ui.get_by_role("button", name="压测", exact=True)).to_have_count(0)
        ui.get_by_role("checkbox").check()
        expect(self.page.get_by_role("button", name="压测所选", exact=True).and_(self.page.locator(":enabled"))).to_have_count(0)
        options = self.client.get("/api/load-tests/options").json()
        self.assertEqual({item["kind"] for item in options["items"]}, {"api"})
        with self.page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/api/runs")) as launch:
            ui.get_by_role("button", name="运行", exact=True).click()
        run_id = launch.value.json()["runs"][0]["id"]
        expect(self.page.locator(".run-summary .status").first).to_have_text("通过", timeout=30000)
        self.assertEqual(self.client.get("/api/runs/" + run_id).json()["status"], "Pass")
        self.assertEqual(self.target.fixture["ui_initial_cookies"], [""])
        self.assertEqual(len(self.target.fixture["ui_tokens"]), 1)
        self.assertEqual(self.client.get("/api/load-tests").json()["total"], 0)

    def test_first_failure_survives_later_pass_and_export(self):
        original = projects.case(self.pid, "TC-CHANGING")
        run_id = self.start(self.editor_from_case("TC-CHANGING"), concurrency=1, target_requests=2)
        run = self.wait_finished(run_id)
        self.assertEqual((run["status"], run["done"], run["failed"], run["passed"]), ("Fail", 2, 1, 1), run)
        self.assertEqual([row["status"] for row in run["results"]], ["Fail", "Pass"])
        self.assertEqual(projects.case(self.pid, "TC-CHANGING"), original)
        stored = db.one("SELECT * FROM runs WHERE id = ?", (run_id,))
        snapshot = task_report_data.run_snapshot(stored)
        self.assertEqual(len(snapshot["cases"]), 2)
        self.assertTrue(all(item["evidence_valid"] for item in snapshot["cases"]))
        logical = task_report_data.logical_cases(snapshot)
        self.assertEqual((len(logical), logical[0]["status"], logical[0]["first_status"], logical[0]["final_status"]), (1, "Fail", "Fail", "Pass"))
        expect(self.statistic_cells(self.page).nth(1)).to_have_text("2", timeout=15000)
        with self.page.expect_download() as downloaded:
            self.page.get_by_role("button", name="下载 HTML 报告", exact=True).click()
        exported = Path(downloaded.value.path()).read_text(encoding="utf-8")
        self.assertIn("TC-CHANGING", exported)
        self.assertIn("失败", exported)
        self.assertIn("P95", exported)
        offline = self.browser.new_context(offline=True)
        self.addCleanup(offline.close)
        reader = offline.new_page()
        reader.set_content(exported)
        for document in (self.page, reader):
            for removed in ("本次报告", "执行尝试", "详见本次尝试的执行记录", "查看本次记录"):
                expect(document.get_by_text(removed, exact=True)).to_have_count(0)
            cells = self.statistic_cells(document, "TC-CHANGING")
            expect(cells.nth(1)).to_have_text("2")
            expect(cells.nth(2)).to_have_text("1")
            expect(document.get_by_role("table", name="错误汇总", exact=True)).to_contain_text("字段 /ok")
            expect(document.get_by_role("table", name="每个采样器的前五项错误", exact=True)).to_contain_text("TC-CHANGING")
        receipt = self.client.get("/api/runs/" + run_id).json()["test_report"]
        self.assertEqual(receipt["status"], "ready", receipt)
        workbook = openpyxl.load_workbook(io.BytesIO(self.client.get(receipt["download_url"]).content))
        self.addCleanup(workbook.close)
        rows = list(workbook["执行明细"].values)
        self.assertGreaterEqual(len(rows), 3)
        markdown = self.client.get(receipt["markdown_download_url"]).text
        self.assertIn("失败", markdown)
        bugs = self.client.get(receipt["bugs_download_url"]).text
        self.assertIn("TC-CHANGING", bugs)
        self.evidence("retained-failure.png")

    def test_cancel_retains_completed_results_and_start_errors_remain_actionable(self):
        dialog = self.editor_from_case("TC-SLOW")
        run_id = self.start(dialog, concurrency=2, target_requests=100)
        expect(self.page.get_by_role("button", name="停止压测", exact=True)).to_be_visible(timeout=10000)
        deadline = time.monotonic() + 15
        while self.client.get("/api/load-tests/" + run_id).json()["done"] == 0 and time.monotonic() < deadline:
            time.sleep(0.1)
        self.page.get_by_role("button", name="停止压测", exact=True).click()
        run = self.wait_finished(run_id)
        self.assertEqual(run["status"], "Cancelled", run)
        self.assertGreater(run["done"], 0)
        self.assertLess(run["done"], run["load"]["target_requests"])
        self.assertEqual(self.target.fixture["active"], 0)
        self.assertEqual(self.target.fixture["slow_started"], len(run["load_report"]["samples"]))
        expect(self.page.get_by_role("button", name="再次压测", exact=True)).to_be_visible(timeout=15000)
        self.assertEqual(self.client.get("/api/load-tests/" + run_id).json()["done"], run["done"])
        self.evidence("cancelled.png")
        self.page.get_by_role("button", name="再次压测", exact=True).click()
        dialog = self.page.get_by_role("dialog", name="新建压测", exact=True)
        expect(dialog.get_by_role("checkbox", name=re.compile("TC-SLOW"))).to_be_checked()
        original = projects.case(self.pid, "TC-SLOW")
        original["lifecycle"] = "archived"
        projects.save_cases(self.pid, [original])
        dialog.get_by_role("button", name="开始压测", exact=True).click()
        expect(dialog.get_by_role("alert")).to_contain_text("已归档")
        expect(dialog.get_by_role("button", name="开始压测", exact=True)).to_be_enabled()


if __name__ == "__main__":
    unittest.main()
