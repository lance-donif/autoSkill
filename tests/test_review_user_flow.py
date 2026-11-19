"""Independent, opt-in loopback acceptance for the design-to-evidence user journey.

Run lightweight rule tests with unittest. Set AUTOSKILL_REVIEW_USER_FLOW=1 and
AUTOSKILL_CHROMIUM to an installed Chromium to enable real HTTP/browser/Excel checks.
All fixtures stay under TMPDIR; AUTOSKILL_REVIEW_TEMP_ROOT can select a shorter private
path for Chromium's Unix socket. AUTOSKILL_REVIEW_EVIDENCE retains observations.
The fixture does not exercise a DSH model conversation or native host file delivery.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib.util
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))
SPEC = importlib.util.spec_from_file_location("review_user_flow", ROOT / "scripts/review_rules/user_flow.py")
RULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RULE)


class ReadOnlyRuleTests(unittest.TestCase):
    def rule(self, modules):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, source in modules.items():
                file = root / RULE.PACKAGE / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text(source, encoding="utf-8")
            return RULE.run(root)

    def test_follows_aliased_helper_to_execution_mutation(self):
        findings = self.rule({"plan.py": "from .helpers import inspect as inspect_task\ndef show():\n return inspect_task()\n",
                              "helpers.py": "from .runner import enqueue as commit\ndef inspect():\n return commit({})\n"})
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["path"], "skills/autoskill/app/autoskill/helpers.py")
        self.assertEqual(findings[0]["line"], 3)
        self.assertIn("autoskill.runner.enqueue", findings[0]["message"])

    def test_status_cannot_render_or_retry_reports(self):
        findings = self.rule({"task_reports.py": "def get(identity):\n return helper(identity)\ndef helper(identity):\n return _render(identity)\ndef _render(identity):\n return identity\n"})
        self.assertEqual([item["rule"] for item in findings], ["user_flow.read_only_status"])

    def test_unreachable_explicit_action_and_unrelated_method_are_allowed(self):
        self.assertEqual(self.rule({"plan.py": "from . import runner\ndef show():\n return {'status': 'pending'}\n"
                                              "def execute():\n return runner.enqueue({})\n",
                                    "workflow.py": "from . import runner\ndef status(service):\n def explicit_action():\n  return runner.enqueue({})\n return service.retry()\n"}), [])


class BusinessFixture(BaseHTTPRequestHandler):
    requests = 0

    def log_message(self, *_args):
        return

    def do_GET(self):
        route = urlparse(self.path)
        if route.path.startswith("/api/"):
            type(self).requests += 1
            body = {"approved": False} if "/approval/" in route.path else {"name": "订单-乙" if route.path.endswith("002") else "订单-甲", "state": "待处理"}
            self.reply(json.dumps(body, ensure_ascii=False), "application/json")
        elif route.path == "/host":
            address = parse_qs(route.query)["dashboard"][0]
            self.reply('<!doctype html><meta charset="utf-8"><h1>业务工作台</h1>'
                       f'<iframe data-testid="dashboard" style="width:100%;height:900px;border:0" src="{address}"></iframe>')
        elif route.path == "/orders":
            self.reply('<!doctype html><meta charset="utf-8"><h1>订单中心</h1><iframe id="orders-frame" src="/order-list"></iframe>')
        elif route.path == "/order-list":
            self.reply('<!doctype html><meta charset="utf-8"><button id="query" onclick="this.nextElementSibling.hidden=false">查询订单</button>'
                       '<ul id="orders" hidden><li class="order">订单-甲</li><li class="order">订单-乙</li></ul>')
        else:
            self.send_error(404)

    def reply(self, body, content_type="text/html"):
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def digest(file):
    return hashlib.sha256(Path(file).read_bytes()).hexdigest()


def source_fingerprint():
    folder = ROOT / "skills/autoskill/app"
    files = [(file.relative_to(folder).as_posix(), digest(file)) for file in sorted(folder.rglob("*"))
             if file.is_file() and file.suffix in {".py", ".js", ".css", ".html"}]
    return hashlib.sha256(json.dumps(files).encode()).hexdigest()


def workbook_rows(file):
    from openpyxl import load_workbook

    with Path(file).open("rb") as stream:
        book = load_workbook(stream, data_only=False)
        sheet = book["功能测试用例"]
        headers = [cell.value for cell in sheet[1]]
        rows = [list(row) for row in sheet.iter_rows(values_only=True) if str(row[0] or "").startswith("FT-")]
        fonts = {cell.font.name for row in sheet for cell in row if cell.value is not None}
        book.close()
    return headers, rows, fonts


@unittest.skipUnless(os.environ.get("AUTOSKILL_REVIEW_USER_FLOW") == "1", "explicit opt-in required for isolated Chromium/HTTP/Excel acceptance")
class UserFlowAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import uvicorn

        temporary_root = os.environ.get("AUTOSKILL_REVIEW_TEMP_ROOT")
        if temporary_root:
            Path(temporary_root).mkdir(parents=True, exist_ok=True)
        cls.temporary = tempfile.TemporaryDirectory(prefix="uf-", dir=temporary_root)
        cls.directory = Path(cls.temporary.name)
        cls.evidence = Path(os.environ.get("AUTOSKILL_REVIEW_EVIDENCE", str(cls.directory / "observations")))
        cls.evidence.mkdir(parents=True, exist_ok=True)
        chromium = os.environ.get("AUTOSKILL_CHROMIUM") or shutil.which("chromium")
        if not chromium:
            raise RuntimeError("Acceptance requires an installed Chromium; no browser is downloaded")
        cls.environment = patch.dict(os.environ, {
            "AUTOSKILL_WORKSPACE": str(cls.directory / "data"), "AUTOSKILL_SESSION": "user-flow-acceptance",
            "AUTOSKILL_CASE_SKILL": "", "AUTOSKILL_HEADED": "0", "AUTOSKILL_BRIDGE": "off",
            "AUTOSKILL_RUNTIME_CONTEXT": "", "AUTOSKILL_WORKFLOW_BINDING": "", "AUTOSKILL_CHROMIUM": chromium,
            "TMPDIR": str(cls.directory), "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD": "1", "PIP_NO_INDEX": "1",
            "NO_PROXY": "127.0.0.1,localhost,::1", "no_proxy": "127.0.0.1,localhost,::1",
        })
        cls.environment.start()
        cls.chromium = chromium
        cls.fixture = ThreadingHTTPServer(("127.0.0.1", 0), BusinessFixture)
        cls.business = f"http://127.0.0.1:{cls.fixture.server_port}"
        cls.fixture_thread = threading.Thread(target=cls.fixture.serve_forever, daemon=True)
        cls.fixture_thread.start()
        from autoskill.server.app import create_app

        with socket.socket() as port_socket:
            port_socket.bind(("127.0.0.1", 0))
            port = port_socket.getsockname()[1]
        cls.address = f"http://127.0.0.1:{port}"
        cls.server = uvicorn.Server(uvicorn.Config(create_app(port=port, start_worker=False), host="127.0.0.1", port=port, log_level="error"))
        cls.server_thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.server_thread.start()
        deadline = time.monotonic() + 10
        while not cls.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("The isolated dashboard did not start")
            time.sleep(0.02)
        cls.observations = {"source_before": source_fingerprint(), "main_js_sha256": digest(ROOT / "skills/autoskill/app/ui/js/main.js"), "limitations": [
            "loopback fixtures; no production capacity claim", "no DSH model conversation or native file-card delivery"]}

    @classmethod
    def tearDownClass(cls):
        cls.observations["source_after"] = source_fingerprint()
        unchanged = cls.observations["source_after"] == cls.observations["source_before"]
        cls.observations["source_unchanged"] = unchanged
        (cls.evidence / "observations.json").write_text(json.dumps(cls.observations, ensure_ascii=False, indent=2), encoding="utf-8")
        cls.server.should_exit = True
        cls.server_thread.join(10)
        cls.fixture.shutdown()
        cls.fixture.server_close()
        cls.fixture_thread.join(5)
        cls.environment.stop()
        cls.temporary.cleanup()
        if not unchanged:
            raise AssertionError("Product source changed during acceptance; these observations are not a final snapshot verdict")

    def project(self):
        from autoskill import projects

        project = projects.create("订单中心", self.business)["id"]
        projects.sync_requirement_records(project, [{"id": "REQ-ORDER", "module": "订单查询", "title": "订单查询与审批",
            "source": "独立验收业务需求：订单甲乙均待处理，审批应通过。"}])
        return project

    def design(self, project, *, title="订单查询"):
        from autoskill import plan

        rows = [
            {"id": "TC-001", "title": "查询订单甲", "expected": "1. 返回订单-甲的待处理状态。"},
            {"id": "TC-002", "title": "查看审批结果", "expected": "1. 审批结果为已通过。"},
            {"id": "TC-003", "title": "查看订单列表", "expected": "1. 展示订单-甲和订单-乙两条记录。", "steps": "1. 点击【查询订单】。"},
            {"id": "TC-004", "title": "查询订单乙", "expected": "1. 返回订单-乙的待处理状态。"},
        ]
        for row in rows:
            row.update(module=title, business_path=["订单中心", title], source="独立验收业务需求：订单甲乙均待处理，审批应通过。",
                       description="验证" + row["title"], preconditions="已准备订单-甲和订单-乙。", author="【待确认：实际编制人】")
            row.setdefault("steps", "1. 查询对应订单的业务状态。")
        source = self.directory / "design.json"
        source.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        receipt = plan.write(project, str(source))
        self.assertEqual(receipt["test_report"]["status"], "ready", receipt)
        headers, written, fonts = workbook_rows(receipt["delivery_excel"])
        self.assertEqual(len(headers), 14)
        self.assertEqual(len(written), 4)
        self.assertEqual(fonts, {"宋体"})
        self.assertEqual([item[6] for item in written], [item["expected"] for item in rows])
        self.assertTrue(all(item[1] == "订单中心-" + title and item[2] == title for item in written))
        self.assertTrue(all(not item[8] and item[9] in (None, "未执行") for item in written))
        return receipt

    def candidate(self, design_id, identity, *, ui_case=False):
        from autoskill import plan

        row = next(item for item in plan.show()["cases"] if item["id"] == design_id)
        document = {key: row[key] for key in ("business_path", "description", "preconditions", "steps", "expected")}
        document["source"] = row["execution_source"]
        case = {"schema_version": "1.1.0", "id": identity, "kind": "ui" if ui_case else "api", "revision": 1, "lifecycle": "active",
                "requirement_ids": ["REQ-ORDER"], "module": row["module"], "title": row["title"], "priority": "P1",
                "preconditions": row["preconditions"], "steps": [row["steps"]], "test_data": {}, "expected": row["expected"],
                "source": "独立验收业务需求", "risk": "read", "tags": [],
                "extensions": {"oracle": {"source": "explicit"}, "test_case_document": document}}
        if ui_case:
            frame = [{"kind": "frame", "locators": [{"by": "css", "value": "#orders-frame"}]}]
            case["extensions"]["automation"] = {"start_url": self.business + "/orders",
                "actions": [{"action": "click", "selector": "#query", "context": frame}],
                "assertions": [{"assertion": "visible", "selector": "ul", "context": frame},
                               {"assertion": "count", "selector": ".order", "context": frame, "expected": 2},
                               {"assertion": "contains", "selector": "ul", "context": frame, "expected": "订单-乙"}]}
        else:
            approval = design_id == "TC-002"
            name = "订单-乙" if design_id == "TC-004" else "订单-甲"
            case.update(api_source="source-and-official-docs", source_ref="独立验收业务需求", method="GET",
                        path="/api/approval/A-001" if approval else "/api/order/A-002" if design_id == "TC-004" else "/api/order/A-001",
                        assertions={"status": 200, "json_equals": {"/approved": True} if approval else {"/name": name, "/state": "待处理"}})
        return case

    def queue(self, project, cases, identity):
        from autoskill import candidate_publication, projects

        task = candidate_publication.stage(project, cases, projects.auth(project), {"acceptance": identity})
        result = candidate_publication.queue(task["id"], start=False)
        self.assertTrue(result["run_ids"], result)
        self.assertEqual(result["unpublished"], {}, result)
        self.assertFalse(result["published_ids"])
        return result

    def execute(self, queued):
        from autoskill import candidate_publication, runner

        for run_id in queued["run_ids"]:
            runner.execute(run_id)
        publication = candidate_publication.read(queued["candidate_id"])["batches"][-1]["publication"]
        self.assertTrue(publication["complete"], publication)
        return publication

    def test_complete_sheet_real_execution_and_late_original_delivery(self):
        from autoskill import case_plans, plan, projects, task_context, task_reports

        with task_context.use_session("main-journey"):
            project = self.project()
            first = self.design(project)
            initial_hash = digest(first["initial_excel"])
            queued = self.queue(project, [self.candidate("TC-001", "TC-API-001"), self.candidate("TC-002", "TC-API-002"),
                                          self.candidate("TC-003", "TC-UI-001", ui_case=True)], "first-task")
            publication = self.execute(queued)
            self.assertEqual(set(publication["published_ids"]), {"TC-API-001", "TC-UI-001"}, publication)
            self.assertIn("TC-API-002", publication["unpublished"])
            current = plan.show()
            self.assertEqual(current["delivery_excel"], first["delivery_excel"])
            self.assertEqual(digest(first["initial_excel"]), initial_hash)
            rows = workbook_rows(current["delivery_excel"])[1]
            self.assertEqual([row[9] for row in rows[:3]], ["通过", "失败", "通过"])
            self.assertIn(rows[3][9], (None, "未执行"))
            self.assertEqual(rows[1][6], "1. 审批结果为已通过。")
            frozen = current["test_report"]
            frozen_paths = [Path(frozen[key]) for key in ("excel", "markdown", "bugs")]
            frozen_hashes = [digest(file) for file in frozen_paths]
            late = self.queue(project, [self.candidate("TC-004", "TC-API-003")], "late-original-task")
            replacement = self.design(project, title="新任务订单查询")
            replacement_hash = digest(replacement["delivery_excel"])
            self.assertNotEqual(replacement["task_id"], first["task_id"])
            self.assertEqual(self.execute(late)["published_ids"], ["TC-API-003"])
            self.assertEqual(digest(replacement["delivery_excel"]), replacement_hash)
            self.assertEqual([digest(file) for file in frozen_paths], frozen_hashes)
            self.assertEqual(task_reports.get(frozen["report_id"])["status"], "ready")
            self.assertEqual([row[9] for row in workbook_rows(first["delivery_excel"])[1]], ["通过", "失败", "通过", "通过"])
            viewed = case_plans.get(project, "main-journey")
            self.assertTrue(all(row["status"] == "NotRun" for row in viewed["rows"]), viewed["rows"])
            self.assertNotIn("TC-API-002", {case["id"] for case in projects.cases(project)})
            self.assertEqual(viewed["task_id"], replacement["task_id"])
            self.observations["main_journey"] = {"published_ids": publication["published_ids"], "unpublished": publication["unpublished"],
                "old_task": first["task_id"], "new_task": replacement["task_id"], "old_frozen_hashes": frozen_hashes,
                "new_delivery_sha256": replacement_hash, "initial_excel_sha256": initial_hash}
            for label, file in (("initial", first["initial_excel"]), ("updated-original", first["delivery_excel"]), ("new-task", replacement["delivery_excel"])):
                shutil.copyfile(file, self.evidence / (label + ".xlsx"))
            self.verify_hash_reads_and_retry(frozen)

    def verify_hash_reads_and_retry(self, report):
        from autoskill import db, task_reports, task_state

        file = Path(report["markdown"])
        original = file.read_bytes()
        file_stat = file.stat()
        replacement = (b"X" if original[:1] != b"X" else b"Y") + original[1:]
        file.write_bytes(replacement)
        os.utime(file, ns=(file_stat.st_atime_ns, file_stat.st_mtime_ns))
        before_requests = BusinessFixture.requests
        before_runs = db.one("SELECT COUNT(*) AS total FROM runs")["total"]
        before = {item.name: digest(item) for item in file.parent.iterdir() if item.is_file()}
        for _ in range(3):
            self.assertEqual(task_reports.get(report["report_id"])["status"], "failed")
            self.assertEqual(task_state.report(report["report_id"])["status"], "failed")
        self.assertEqual({item.name: digest(item) for item in file.parent.iterdir() if item.is_file()}, before)
        retried = task_reports.retry(report["report_id"])
        self.assertEqual(retried["status"], "ready", retried)
        self.assertEqual(BusinessFixture.requests, before_requests)
        self.assertEqual(db.one("SELECT COUNT(*) AS total FROM runs")["total"], before_runs)
        self.assertEqual(workbook_rows(retried["excel"])[1][1][6], "1. 审批结果为已通过。")
        self.observations["same_size_same_mtime_hash_check"] = {"read_verdict": "failed", "retry": "ready", "additional_runs": 0, "additional_business_requests": 0}

    async def browser(self, playwright, name):
        from autoskill import paths

        return await playwright.chromium.launch_persistent_context(str(self.directory / ("profile-" + name)), executable_path=self.chromium,
            headless=True, viewport={"width": 1280, "height": 900}, args=[*paths.QUIET_CHROME_ARGS, "--no-sandbox"])

    async def cold_pages(self):
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import async_playwright

        results = []
        async with async_playwright() as playwright:
            for endpoint in ("/settings", "/status", "/state-revision"):
                context = await self.browser(playwright, endpoint[1:])
                page = await context.new_page()

                async def delay(route):
                    await asyncio.sleep(6)
                    with contextlib.suppress(PlaywrightError):
                        await route.continue_()

                await page.route("**/api" + endpoint, delay)
                started = time.monotonic()
                try:
                    await page.goto(self.address + "/#/case-plans", wait_until="domcontentloaded")
                    budget = max(1, round((5 - (time.monotonic() - started)) * 1000))
                    await page.locator("main h1").wait_for(timeout=budget)
                    elapsed = time.monotonic() - started
                    self.assertLessEqual(elapsed, 5.0)
                    self.assertEqual(await page.locator("main h1").inner_text(), "测试用例表")
                    results.append({"delayed_endpoint": endpoint, "first_content_seconds": elapsed, "threshold_seconds": 5})
                finally:
                    await page.screenshot(path=str(self.evidence / ("cold-" + endpoint[1:] + ".png")))
                    await context.close()
        self.observations["cold_pages"] = results

    def test_first_content_with_slow_optional_metadata(self):
        asyncio.run(self.cold_pages())

    async def live_sheet(self):
        from playwright.async_api import async_playwright

        from autoskill import task_context

        with task_context.use_session("visible-sheet"):
            project = self.project()
            self.design(project)
            async with async_playwright() as playwright:
                context = await self.browser(playwright, "visible-sheet")
                page = await context.new_page()
                target = self.address + "/#/case-plans?project=" + project + "&session=visible-sheet"
                try:
                    await page.goto(self.business + "/host?dashboard=" + quote(target, safe=""), wait_until="domcontentloaded")
                    frame = page.frame_locator('iframe[data-testid="dashboard"]')
                    await frame.locator("main h1").wait_for(timeout=5000)
                    self.assertEqual(await frame.locator("main h1").count(), 1)
                    self.assertEqual(await page.locator("h1").inner_text(), "业务工作台")
                    row = frame.locator('[data-case="TC-001"]')
                    self.assertIn("查询订单甲", await row.inner_text())
                    propagation = await self.observe_state_commit(row)
                    self.assertLessEqual(propagation, 1.0)
                    await page.screenshot(path=str(self.evidence / "embedded-desktop.png"), full_page=True)
                    await page.set_viewport_size({"width": 390, "height": 844})
                    await page.screenshot(path=str(self.evidence / "embedded-mobile.png"), full_page=True)
                    overflow = await frame.locator("html").evaluate("e => e.scrollWidth > e.clientWidth + 1")
                    self.assertFalse(overflow)
                    self.observations["embedded_state"] = {"state_propagation_seconds": propagation, "threshold_seconds": 1, "horizontal_overflow": overflow}
                finally:
                    await context.close()

    async def observe_state_commit(self, row):
        from autoskill import plan

        committed = []
        save = plan._save

        def save_and_timestamp(*args, **kwargs):
            result = save(*args, **kwargs)
            if not committed:
                committed.append(time.monotonic())
            return result

        with patch.object(plan, "_save", side_effect=save_and_timestamp):
            mutation = asyncio.create_task(asyncio.to_thread(plan.mark, "TC-001", "阻塞", "独立验收：订单凭据尚未提供。"))
            try:
                while not committed and not mutation.done():
                    await asyncio.sleep(0.005)
                if not committed:
                    await mutation
                    self.fail("The mutation did not commit a workbook")
                budget = max(1, round((1 - (time.monotonic() - committed[0])) * 1000))
                await row.locator(".status").filter(has_text="阻塞").wait_for(timeout=budget)
                return time.monotonic() - committed[0]
            finally:
                await mutation

    def test_embedded_identity_layout_and_one_second_state(self):
        asyncio.run(self.live_sheet())

    def test_http_reads_preserve_report_bytes_and_run_count(self):
        from autoskill import db, task_context

        with task_context.use_session("http-read-only"):
            project = self.project()
            design = self.design(project)
            receipt = design["test_report"]
            files = [Path(receipt[key]) for key in ("excel", "markdown", "bugs")]
            before = [digest(file) for file in files]
            count = db.one("SELECT COUNT(*) AS total FROM runs")["total"]
            for route in (f"/api/case-plans/{project}?session=http-read-only", "/api/state-revision", "/api/dashboard", "/api/runs?limit=5"):
                with urlopen(self.address + route, timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    json.load(response)
            self.assertEqual([digest(file) for file in files], before)
            self.assertEqual(db.one("SELECT COUNT(*) AS total FROM runs")["total"], count)


if __name__ == "__main__":
    unittest.main()
