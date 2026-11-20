"""Real recording keeps frame/shadow scopes and XPath declarations through replay."""
import contextlib
import copy
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill import paths, projects, runner
from autoskill.recorder import session
from autoskill.recorder.context import AttachedPage


@contextlib.contextmanager
def context_site():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format, *_args):
            pass

        def do_GET(self):
            port = self.server.server_address[1]
            if self.path == "/":
                html = f'''<!doctype html><title>定位录制</title>
                <input id="name" data-testid="entry" aria-label="姓名"><output id="result">主页面</output>
                <div id="outer"></div><div id="closed"></div>
                <iframe id="same" src="/leaf"></iframe>
                <script>
                document.querySelector('#outer').attachShadow({{mode:'open'}}).innerHTML =
                  '<section id="panel"><iframe id="inner" src="http://localhost:{port}/middle"></iframe></section>';
                document.querySelector('#closed').attachShadow({{mode:'closed'}}).innerHTML = '<button>封闭按钮</button>';
                </script>'''
            elif self.path == "/notifications":
                html = '''<!doctype html><meta charset="utf-8">
                <span id="name">订单通知</span>
                <input type="checkbox" aria-labelledby="name" aria-checked="false" checked>
                <button role="switch" aria-labelledby="name" aria-checked="false">通知开关</button>
                <output aria-label="变更次数">0</output>
                <script>
                const toggle = document.querySelector('button'), count = document.querySelector('output');
                toggle.onclick = () => {
                    toggle.setAttribute('aria-checked', String(toggle.getAttribute('aria-checked') !== 'true'));
                    count.textContent = String(Number(count.textContent) + 1);
                };
                </script>'''
            elif self.path == "/middle":
                html = f'''<!doctype html><title>跨域页面</title><input id="name" data-testid="entry"><div id="card"></div>
                <script>document.querySelector('#card').attachShadow({{mode:'open'}}).innerHTML =
                  '<div id="nested"></div><iframe id="leaf" src="http://127.0.0.1:{port}/leaf"></iframe>';
                document.querySelector('#card').shadowRoot.querySelector('#nested').attachShadow({{mode:'open'}}).innerHTML =
                  '<button data-testid="deep">深层按钮</button>';
                </script>'''
            else:
                html = '''<!doctype html><title>表单</title>
                <label for="name">姓名</label><input id="name" data-testid="entry">
                <button id="save" onclick="document.querySelector('#result').textContent='已保存：'+document.querySelector('#name').value">保存</button>
                <output id="result">尚未保存</output>'''
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_address[1])
    finally:
        server.shutdown()
        worker.join()
        server.server_close()


class ExplicitXPathTests(unittest.TestCase):
    def test_xpath_is_not_css_and_assertion_cardinality_is_preserved(self):
        rec = {"refs": {}}
        locator = {"by": "xpath", "value": "//button[@id='save']"}
        self.assertEqual(session._spec(rec, "xpath=//button[@id='save']", True), {"locator": locator})
        self.assertEqual(session._assertion_locators(rec, "count", "xpath=//button[@id='save']"), [locator])
        self.assertEqual(session._assertion_locators(rec, "hidden", "xpath=//button[@id='save']"), [locator])
        with patch.object(session, "_describe", return_value={"locators": [{"by": "css", "value": "#save"}, locator]}):
            self.assertEqual(session._target(rec, "xpath=//button[@id='save']", True)["locators"][0], locator)


class RecorderContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not paths.agent_browser() or not paths.chrome(bundled=True):
            raise unittest.SkipTest("需要 AUTOSKILL_AGENT_BROWSER 和 AUTOSKILL_CHROMIUM")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": self.tmp.name, "AUTOSKILL_HEADED": "0"})
        environment.start()
        self.addCleanup(environment.stop)
        self.site = context_site()
        self.base = self.site.__enter__()
        self.addCleanup(self.site.__exit__, None, None, None)
        self.project = projects.create("上下文录制", self.base, project_id="contexts")["id"]
        session.start(self.project, login=False)
        self.addCleanup(session.cancel)

    def enter_leaf(self):
        for command, target in (("shadow", "#outer"), ("frame", "#inner"), ("shadow", "#card"), ("frame", "#leaf")):
            session.browser([command, target])

    def test_checked_false_fails_on_an_enabled_aria_switch(self):
        session.browser(["open", "/notifications"])
        target = "role=switch:订单通知"
        session.browser(["click", target])
        wrong = session.check("checked", target, "false", timeout_seconds=0)
        self.assertEqual((wrong["passed"], wrong["expected"], wrong["actual"]), (False, False, True))

    def test_aria_switch_state_and_idempotent_actions_match_recording_and_replay(self):
        session.browser(["open", "/notifications"])
        target = "role=switch:订单通知"
        ref = next(key for key, item in session.current()["refs"].items() if item.get("role") == "switch")
        session.browser(["check", "@" + ref])
        self.assertTrue(session.browser(["is", "checked", target])["result"]["checked"])
        for action, expected, changes in (("check", "true", "1"), ("uncheck", "false", "2"), ("uncheck", "false", "2")):
            session.browser([action, target])
            self.assertTrue(session.check("checked", target, expected, timeout_seconds=0)["passed"])
            self.assertTrue(session.check("text", "label=变更次数", changes, timeout_seconds=0)["passed"])
        self.assertTrue(session.check("checked", "role=checkbox:订单通知", "true", timeout_seconds=0)["passed"])
        with patch("autoskill.server.jobs.ensure_worker"):
            saved = session.stop("反复设置通知开关")
        self.assertEqual(runner.execute(saved["run_id"])["status"], "Pass")
        if paths.chromedriver():
            run = runner.create(self.project, [saved["ui_case"]["id"]], "selenium")
            self.assertEqual(runner.execute(run["id"])["status"], "Pass")

    def test_record_save_and_replay_cross_origin_mixed_context_without_a_second_browser(self):
        session.browser(["fill", "xpath=//input[@id='name']", "主文档"])
        original_tabs = session.browser(["tab", "list"])["tabs"]
        self.enter_leaf()
        described = session.browser(["describe", "#name"])
        self.assertEqual(len(described["context"]), 4)
        self.assertEqual(described["locator_definitions"][-1]["by"], "xpath")
        self.assertEqual(len(session.browser(["tab", "list"])["tabs"]), len(original_tabs))
        refs = session.current()["refs"]
        reference = next(key for key, value in refs.items() if value.get("name") == "姓名")
        session.browser(["fill", "@" + reference, "张三"])
        self.assertEqual(session.browser(["get", "value", "#name"])["result"], {"value": "张三"})
        self.assertEqual(session.check("value", "#name", "张三")["status"], "Pass")
        session.browser(["click", "xpath=//button[@id='save']"])
        self.assertEqual(session.check("text", "#result", "已保存：张三")["status"], "Pass")
        rec = session.current()
        self.assertEqual([hop["kind"] for hop in rec["steps"][-1]["context"]], ["shadow", "frame", "shadow", "frame"])
        self.assertEqual(rec["steps"][-2]["locators"][0]["by"], "xpath")
        session.browser(["context", "top"])
        self.assertEqual(session.check("value", "#name", "主文档")["status"], "Pass")
        with patch("autoskill.server.jobs.ensure_worker"):
            summary = session.stop("在嵌套表单填写姓名并保存")
        case_id = summary["ui_case"]["id"]
        case = next(item for item in summary["candidates"] if item["id"] == case_id)
        self.assertIn("context", json.dumps(case))
        for engine in ("playwright", "selenium"):
            if engine == "selenium" and not paths.chromedriver():
                continue
            with self.subTest(engine=engine):
                run = {"id": summary["run_id"]} if engine == "playwright" else runner.create(self.project, [case_id], engine)
                completed = runner.execute(run["id"])
                self.assertEqual(completed["status"], "Pass", completed)

    def test_context_missing_is_not_a_hidden_pass_and_closed_shadow_is_explicit(self):
        before = session.current()
        with self.assertRaisesRegex(Exception, "closed|封闭|开放"):
            session.browser(["shadow", "#closed"])
        self.assertEqual(session.current().get("context", []), before.get("context", []))
        session.browser(["frame", "#same"])
        rec = session.current()
        with AttachedPage(session.session_name(), {**rec, "context": []}) as attached:
            attached.page.evaluate("document.querySelector('#same').remove()")
        outcome = session.check("hidden", "#name", None, timeout_seconds=0)
        self.assertFalse(outcome["passed"])
        self.assertNotEqual(outcome["actual"], "不可见")
        self.assertEqual(session.current()["steps"][-1]["context"], rec["context"])

    def test_nested_open_shadow_references_and_frame_reload_re_resolve(self):
        session.browser(["shadow", "#outer"])
        session.browser(["frame", "#inner"])
        session.browser(["shadow", "#card"])
        session.browser(["shadow", "#nested"])
        record = session.current()
        ref = next(iter(record["refs"]))
        described = session.browser(["describe", "@" + ref])
        self.assertEqual(described["name"], "深层按钮")
        xpath = described["locator_definitions"][-1]
        self.assertEqual(xpath["by"], "xpath")
        self.assertIn("xpath_anchor", xpath)
        with AttachedPage(session.session_name(), record) as attached:
            self.assertEqual(attached.read(xpath, "text"), "深层按钮")
        session.browser(["context", "top"])
        session.browser(["frame", "#same"])
        saved = copy.deepcopy(session.current()["context"])
        session.browser(["reload"])
        self.assertEqual(session.current()["context"], saved)
        session.browser(["fill", "#name", "重载后"])
        self.assertEqual(session.check("value", "#name", "重载后")["status"], "Pass")


if __name__ == "__main__":
    unittest.main()
