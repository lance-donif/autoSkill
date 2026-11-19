"""Exercise the published Pages assets and the HTML downloads using only a local static server."""
import ast
import base64
import copy
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import unittest
from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import expect, sync_playwright

ROOT = Path(os.environ.get("AUTOSKILL_PAGES_ROOT", Path(__file__).resolve().parents[1]))
WIDTHS = (1360, 390)


class StaticFiles(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


class PagesWorkspace(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        public_url = os.environ.get("AUTOSKILL_PAGES_URL")
        if public_url:
            cls.preview = public_url.rstrip("/") + "/"
        else:
            server = ThreadingHTTPServer(("127.0.0.1", 0), partial(StaticFiles, directory=ROOT))
            cls.addClassCleanup(server.server_close)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            cls.addClassCleanup(server.shutdown)
            cls.preview = f"http://127.0.0.1:{server.server_port}/preview/"
        cls.site_root = urljoin(cls.preview, "../")
        playwright = sync_playwright().start()
        cls.addClassCleanup(playwright.stop)
        cls.browser = playwright.chromium.launch(executable_path=os.environ.get("AUTOSKILL_CHROMIUM"))
        cls.addClassCleanup(cls.browser.close)

    @contextmanager
    def session(self, width, sample=""):
        self.viewport = {"width": width, "height": 860}
        context = self.browser.new_context(viewport=self.viewport)
        self.page = context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))

        def guard(route):
            url = route.request.url
            if url.startswith(self.site_root) and "/api/" not in url:
                route.continue_()
            else:
                self.errors.append(f"External or server API request: {url}")
                route.abort()

        context.route("**/*", guard)
        context.tracing.start(screenshots=True, snapshots=True, sources=True)
        with tempfile.TemporaryDirectory() as scratch:
            self.scratch = Path(scratch)
            output = os.environ.get("AUTOSKILL_VALIDATION_OUTPUT")
            name = "-".join(filter(None, [self._testMethodName, str(width), sample]))
            self.artifacts = Path(output) / name if output else self.scratch
            self.artifacts.mkdir(parents=True, exist_ok=True)
            try:
                yield
                self.assertEqual(self.errors, [])
            except Exception as error:
                if not output:
                    self.artifacts = Path(tempfile.mkdtemp(prefix=name + "-"))
                    shutil.copytree(self.scratch, self.artifacts, dirs_exist_ok=True)
                self.page.screenshot(path=self.artifacts / "failure.png", timeout=5000)
                context.tracing.stop(path=self.artifacts / "trace.zip")
                error.add_note(f"Pages browser failure artifacts: {self.artifacts}")
                raise
            else:
                context.tracing.stop()
            finally:
                context.close()

    def open(self, fragment):
        self.page.goto(self.preview + "#/" + fragment)

    @contextmanager
    def offline(self, run_id, *, report_selector=".run-summary"):
        with self.page.expect_download() as event:
            self.page.get_by_role("button", name="下载 HTML 报告", exact=True).click()
        target = self.artifacts / (run_id + ".html")
        event.value.save_as(target)
        self.assertGreater(target.stat().st_size, 1000)
        context = self.browser.new_context(viewport=self.viewport, offline=True)
        context.tracing.start(screenshots=True, snapshots=True)
        reader = context.new_page()
        reader.on("pageerror", lambda error: self.errors.append(str(error)))
        reader.on("request", lambda request: self.errors.append(request.url) if request.url.startswith(("http:", "https:")) else None)
        try:
            reader.set_content(target.read_text(encoding="utf-8"))
            expect(reader.locator(report_selector)).to_be_visible()
            assets = reader.locator('script[src], img[src], link[rel="stylesheet"][href]').evaluate_all(
                "nodes => nodes.map(node => node.getAttribute('src') || node.getAttribute('href'))")
            self.assertTrue(all(url.startswith("data:") for url in assets), assets)
            yield reader
        except Exception:
            reader.screenshot(path=self.artifacts / (run_id + "-offline.png"))
            context.tracing.stop(path=self.artifacts / (run_id + "-offline-trace.zip"))
            raise
        else:
            context.tracing.stop()
        finally:
            context.close()


class PagesBrowserTests(PagesWorkspace):
    def drawer(self, project, folder, title):
        self.open(f"cases?range=all&projects={project}")
        self.page.get_by_role("button", name=re.compile("^" + folder + "，")).click()
        self.page.get_by_role("button", name=title, exact=True).click()
        drawer = self.page.get_by_role("dialog", name=title, exact=True)
        expect(drawer.locator(".drawer-foot button")).to_have_text(["关闭", "运行这条用例"])
        expect(drawer.get_by_role("button", name="编辑用例")).to_have_count(0)
        expect(drawer.get_by_role("button", name="删除用例")).to_have_count(0)
        return drawer

    def test_case_library_ui_and_api(self):
        for width in WIDTHS:
            with self.subTest(width=width), self.session(width):
                title = "读者从首页进入Python3基础语法章节"
                drawer = self.drawer("runoob", "公开页面", title)
                steps = drawer.locator("ol.steps li .step-text")
                expect(steps).to_have_count(9)
                expect(steps).to_contain_text(["打开", "学习 Python", "/python3/python3-tutorial.html", "#leftcolumn",
                                              "点击 link「Python3 基础语法」", "heading", "编码", "标识符", "Python 保留关键字"])
                for engine in ("Playwright", "Selenium"):
                    with self.page.expect_download() as saved:
                        drawer.get_by_role("link", name=engine + " .py", exact=True).click()
                    self.assertTrue(saved.value.suggested_filename.endswith(".py"))
                    code = saved.value.path().read_text(encoding="utf-8")
                    ast.parse(code)
                    for snippet in (engine.lower(), "TC-UI-004", "/python3/python3-tutorial.html"):
                        self.assertIn(snippet, code)
                drawer.locator(".drawer-foot").get_by_role("button", name="关闭").click()
                expect(self.page.get_by_role("button", name=title, exact=True)).to_be_visible()

                drawer = self.drawer("stock-room", "账户", "登录库存系统")
                expect(drawer.locator(".req-line")).to_contain_text("POST")
                expect(drawer.locator(".req-line")).to_contain_text("/api/login")
                expect(drawer.get_by_text("请求体", exact=True)).to_be_visible()
                expect(drawer.locator("pre").filter(has_text="demo-viewer")).to_be_visible()
                checks = drawer.locator("table").filter(has_text="/role")
                expect(checks.locator("tr")).to_contain_text(["状态码200", "字段 /role\"viewer\""])
                drawer.locator(".drawer-foot").get_by_role("button", name="关闭").click()
                self.page.get_by_role("button", name="编辑用例 ST-API-006", exact=True).click()
                editor = self.page.get_by_role("dialog", name="编辑用例")
                expect(editor.get_by_label("功能描述", exact=True)).to_be_visible()
                editor.get_by_label("功能描述", exact=True).fill("核对登录用户的角色")
                editor.get_by_role("button", name="保存修改", exact=True).click()
                expect(editor).to_have_count(0)
                self.page.get_by_role("button", name="编辑用例 ST-API-006", exact=True).click()
                expect(editor.get_by_label("功能描述", exact=True)).to_have_value("核对登录用户的角色")
                editor.get_by_role("button", name="取消", exact=True).click()
                self.page.get_by_role("button", name="删除用例 ST-API-006", exact=True).click()
                confirm = self.page.get_by_role("alertdialog")
                expect(confirm).to_contain_text("登录库存系统")
                confirm.get_by_role("button", name="删除用例", exact=True).click()
                expect(self.page.get_by_role("button", name="登录库存系统", exact=True)).to_have_count(0)

    def open_result(self, run_id, case_id):
        self.open("runs?range=all")
        self.page.locator(f'[data-history-id="run:{run_id}"]').click()
        result = self.page.locator(f'.result[data-case="{case_id}"]')
        head = result.locator(".result-head")
        expect(head).to_be_visible()
        if head.get_attribute("aria-expanded") != "true":
            head.click()
        return result

    def check_ui(self, result, timed_out):
        steps = result.get_by_role("list", name="操作步骤", exact=True).get_by_role("listitem")
        if timed_out:
            expect(steps).to_have_count(2)
            expect(steps.first).to_contain_text("打开")
            expect(steps.first).to_contain_text("失败")
            expect(steps.first).to_contain_text("页面没有在限定时间内加载完成")
            expect(steps.last).to_contain_text("未执行")
            expect(steps.last).to_contain_text("Python3")
        else:
            expect(steps).to_have_count(5)
            expect(steps).to_contain_text(["打开", "学习 Python", "/python3/python3-tutorial.html", "#leftcolumn", "Python3 基础语法"])
            for step in steps.all():
                expect(step).to_contain_text(re.compile("完成|通过"))
            evidence = result.locator(".checks")
            expect(evidence).to_contain_text("Python 保留关键字")

    def check_historical_screenshot(self, result, *, offline=False):
        shot = result.locator(".shot")
        shot.scroll_into_view_if_needed()
        image = shot.locator("img")
        expect(image).to_be_visible()
        image.scroll_into_view_if_needed()
        expect(image).to_be_in_viewport()
        dimensions = image.evaluate("async img => { await img.decode(); return [img.naturalWidth, img.naturalHeight] }")
        self.assertEqual(dimensions, [1920, 11328])
        if offline:
            encoding, data = image.get_attribute("src").split(",", 1)
            self.assertEqual(encoding, "data:image/png;base64")
            digest = hashlib.sha256(base64.b64decode(data, validate=True)).hexdigest()
            self.assertEqual(digest, "7b5077ec9bdc3926e768806ebbf1605920cc2cc0a9580f5af4b481a8d9ac90ed")

    def test_successful_and_timed_out_ui_reports(self):
        for width in WIDTHS:
            for run_id, case, timed_out in [("run-83be44ec7c93", "TC-UI-004", False),
                                            ("run-72801c4e4083", "RN-HOME-PYTHON3", True)]:
                with self.subTest(width=width, run=run_id), self.session(width, run_id):
                    result = self.open_result(run_id, case)
                    self.check_ui(result, timed_out)
                    if not timed_out:
                        self.check_historical_screenshot(result)
                    with self.offline(run_id) as reader:
                        saved = reader.locator(f'.result[data-case="{case}"]')
                        self.check_ui(saved, timed_out)
                        if not timed_out:
                            self.check_historical_screenshot(saved, offline=True)

    def check_failed_api(self, result):
        line = result.locator(".req-line")
        expect(line).to_have_count(1)
        expect(line).to_contain_text("GET")
        expect(line).to_contain_text("https://stock.example.invalid/api/stock")
        expect(line).to_contain_text("HTTP 200")
        self.assertFalse(line.evaluate("node => Boolean(node.closest('details'))"))
        checks = result.locator(".checks")
        self.assertTrue(line.evaluate("(node, checks) => !!(node.compareDocumentPosition(checks) & Node.DOCUMENT_POSITION_FOLLOWING)", checks.element_handle()))
        expect(checks.locator("tbody tr")).to_contain_text(["状态码200200", "字段 /threshold2010"])
        panel = result.locator("details").filter(has=result.page.get_by_text("请求与响应详情", exact=True))
        if not panel.evaluate("node => node.open"):
            panel.locator("summary").click()
        expect(panel).to_contain_text('"threshold": 10')

    def test_failed_api_report(self):
        for width in WIDTHS:
            with self.subTest(width=width), self.session(width):
                run_id = "run-demo-07"
                case = "ST-API-002"
                self.check_failed_api(self.open_result(run_id, case))
                with self.offline(run_id) as reader:
                    self.check_failed_api(reader.locator(f'.result[data-case="{case}"]'))

    def import_retry(self):
        self.open("data")
        with self.page.expect_download() as download:
            self.page.get_by_role("button", name="导出全部数据", exact=True).click()
        backup = json.loads(download.value.path().read_text(encoding="utf-8"))
        source = next(run for run in backup["data"]["runs"] if run["id"] == "run-demo-07")
        run = copy.deepcopy(source)
        run_id = "run-e2e-http-attempts"
        case = "ST-API-002"
        row = next(item for item in run["results"] if item["case_id"] == case)
        run.update(id=run_id, results=[row], case_ids=[case], total=1, done=1, passed=0, failed=1)
        row.update(id=9999999, run_id=run_id)
        row["detail"]["attempts"] = [
            {"kind": "target", "status": "Fail", "outcome": "fail", "duration_ms": number * 10,
             "request": {"method": "POST", "url": f"https://stock.example.invalid/attempt/{number}",
                         "headers": {"X-Request": f"request-{number}"}, "json": {"sku": f"input-{number}"}},
             "response": {"status": status, "body": f"recorded-response-{number}", "bytes": number * 10}}
            for number, status in [(1, 503), (2, 409)]
        ]
        backup["data"]["runs"].insert(0, run)
        self.page.locator('input[type="file"]').set_input_files(
            {"name": "e2e-retry.json", "mimeType": "application/json", "buffer": json.dumps(backup).encode()})
        self.page.get_by_role("button", name="恢复备份", exact=True).click()
        self.page.get_by_role("alertdialog").get_by_role("button", name="恢复", exact=True).click()
        expect(self.page.get_by_text("已恢复 e2e-retry.json", exact=True)).to_be_visible()
        return run_id, case

    def check_retry(self, result):
        lines = result.locator(".req-line")
        expect(lines).to_have_count(2)
        panel = result.locator("details").filter(has=result.page.get_by_text("请求与响应详情", exact=True))
        if not panel.evaluate("node => node.open"):
            panel.locator("summary").click()
        exchanges = panel.locator(":scope > div")
        expect(exchanges).to_have_count(2)
        for number, status in [(1, 503), (2, 409)]:
            line = lines.nth(number - 1)
            expect(line).to_be_visible()
            self.assertFalse(line.evaluate("node => Boolean(node.closest('details'))"))
            for value in (f"第 {number} 次", "POST", f"/attempt/{number}", f"HTTP {status}"):
                expect(line).to_contain_text(value)
            fields = exchanges.nth(number - 1)
            for value in (f"第 {number} 次", f"request-{number}", f"input-{number}", f"recorded-response-{number}"):
                expect(fields).to_contain_text(value)
            expect(fields).not_to_contain_text(f"recorded-response-{3 - number}")

    def test_retry_preserves_each_http_exchange(self):
        for width in WIDTHS:
            with self.subTest(width=width), self.session(width):
                run_id, case = self.import_retry()
                self.check_retry(self.open_result(run_id, case))
                with self.offline(run_id) as reader:
                    self.check_retry(reader.locator(f'.result[data-case="{case}"]'))
