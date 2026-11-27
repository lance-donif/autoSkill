"""Standalone exports retain the shared XPath/context semantics without importing the application."""
import ast
import copy
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))
os.environ.setdefault("AUTOSKILL_HEADED", "0")

from locator_acceptance_fixture import LocatorSite

from autoskill import paths
from autoskill.replay import codegen


def make_case(actions, assertions):
    return {"id": "TC-EXPORT-LOCATORS", "title": "编辑后的定位与检查", "kind": "ui", "extensions": {
        "automation": {"start_url": "/", "actions": actions, "assertions": assertions}}}


class ExportLocatorContractTests(unittest.TestCase):
    def test_exports_embed_all_candidates_and_context_without_application_imports(self):
        context = [{"kind": "frame", "locators": [{"by": "xpath", "value": "//iframe[@id='outer']"}]},
                   {"kind": "shadow", "locators": [{"by": "css", "value": "#component"}]}]
        locator = {"by": "xpath", "value": ".//input[@name='customer']", "xpath_anchor": "#form", "nth": 1}
        case = make_case([{"action": "fill", "context": context,
                           "locators": [{"by": "testid", "value": "outdated"}, locator], "value": "客户-{{RAND}}"}],
                         [{"assertion": "value", "context": context, "locators": [locator], "expected": "客户-{{RAND}}",
                           "transition_from": {"assertion": "value", "expected": ""}}])
        before = copy.deepcopy(case)
        for engine in ("playwright", "selenium"):
            with self.subTest(engine=engine):
                source = codegen.script([case], "http://localhost", engine)
                tree = ast.parse(source)
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom):
                        self.assertEqual(node.level, 0)
                        self.assertFalse((node.module or "").startswith("autoskill"))
                    if isinstance(node, ast.Import):
                        self.assertFalse(any(item.name.startswith("autoskill") for item in node.names))
                self.assertIn("'xpath_anchor':", source)
                self.assertIn("'context':", source)
                self.assertIn('"outdated"', source)
                self.assertIn('"客户-" + RAND', source)
                self.assertIn("def _root(", source)
                self.assertIn("def perform(", source)
        self.assertEqual(case, before)

    def test_legacy_locator_aliases_are_normalized_once_and_source_state_keeps_its_target(self):
        step = {"assertion": "text", "selector": "xpath=//div[@id='result']", "expected": "完成",
                "transition_from": {"assertion": "text", "selector": "#source", "expected": "等待"}}
        for emit in (codegen.pw_step, codegen.se_step):
            source = emit(step)
            self.assertNotIn("'selector':", source)
            self.assertIn("'by': \"xpath\"", source)
            self.assertIn("#source", source)
            self.assertIn("transition_from", source)

    def test_edited_multiline_values_and_quoted_titles_remain_data_in_both_exports(self):
        value = "第一行\n第二行含 '单引号' 与 \"双引号\""
        case = make_case([{"action": "fill", "selector": "textarea", "value": value}], [])
        case["title"] = '修改说明\n包含 """ 三引号'
        for engine in ("playwright", "selenium"):
            source = codegen.script([case], "http://localhost", engine)
            tree = ast.parse(source)
            self.assertIn(case["title"], ast.get_docstring(tree))
            self.assertTrue(any(isinstance(node, ast.Constant) and node.value == value for node in ast.walk(tree)))

    def test_embedded_runtime_support_executes_perform_and_prepare_assertion_without_name_errors(self):
        class _StubDriver:
            def __init__(self):
                self.actions = []

            def count(self, _locator):
                return 1

            def read(self, _locator, kind, *_args):
                return "done" if kind == "text" else ""

            def act(self, locator, op, value):
                self.actions.append((op, locator, value))

        case = make_case([{"action": "fill", "selector": "#name", "value": "alice"}],
                         [{"assertion": "text", "selector": "#result", "expected": "done"}])
        for engine in ("playwright", "selenium"):
            with self.subTest(engine=engine):
                source = codegen.script([case], "http://localhost", engine)
                self.assertIn("def _require_resolved(", source)
                self.assertIn("MISSING_VARIABLE", source)
                import importlib.util

                with tempfile.TemporaryDirectory() as directory:
                    module_file = Path(directory) / f"support_{engine}.py"
                    preamble = ("import os\nimport time\nfrom urllib.parse import urljoin, urlsplit\n"
                                "BASE_URL = 'http://localhost'\nTIMEOUT = 0.5\nRAND = 'r1'\n")
                    module_file.write_text(preamble + codegen._runtime_support(engine), encoding="utf-8")
                    spec = importlib.util.spec_from_file_location(f"support_{engine}", module_file)
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                driver = _StubDriver()
                module.perform(driver, {"action": "fill", "locators": [{"by": "css", "value": "#name"}], "value": "alice"},
                               "http://localhost", [], 0.5)
                self.assertEqual(driver.actions, [("fill", {"by": "css", "value": "#name"}, "alice")])
                prepared = module._prepare_assertion({"assertion": "text", "locators": [{"by": "css", "value": "#result"}], "expected": "done"})
                self.assertEqual(prepared["expected"], "done")
                checked = module.check(driver, {"assertion": "text", "locators": [{"by": "css", "value": "#result"}], "expected": "done"}, 0.5)
                self.assertTrue(checked["passed"])


@unittest.skipUnless(paths.chrome() and paths.chromedriver(), "matching Chromium and chromedriver required")
class ExportLocatorBrowserTests(unittest.TestCase):
    def run_export(self, site, automation, engine):
        case = make_case(automation.get("actions", []), automation.get("assertions", []))
        case["extensions"]["automation"]["start_url"] = automation["start_url"]
        source = codegen.script([case], site.base_url, engine)
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / (engine + ".py")
            file.write_text(source, encoding="utf-8")
            env = {**os.environ, "PYTHONPATH": "", "HEADED": "0", "TIMEOUT": "3", "CHROME_BINARY": paths.chrome(),
                   "CHROMEDRIVER": paths.chromedriver(), "NO_PROXY": "localhost,127.0.0.1,::1", "no_proxy": "localhost,127.0.0.1,::1"}
            return subprocess.run([sys.executable, "-I", str(file)], cwd=directory, env=env,
                                  capture_output=True, text=True, timeout=60, check=False)

    def test_both_standalone_scripts_really_use_xpath_after_primary_failure_in_mixed_context(self):
        with LocatorSite() as site:
            site.other_url = site.other_url.replace("127.0.0.1", "localhost")
            for engine in ("playwright", "selenium"):
                with self.subTest(engine=engine):
                    value = "导出修改后的名称-" + engine
                    before = len(site.hits)
                    result = self.run_export(site, site.automation(value, force_xpath=True), engine)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(result.stdout.count("已使用备选定位"), 2, result.stdout)
                    self.assertEqual(site.hits[before:], [{"where": "leaf", "value": value}])

    def test_document_xpath_and_legacy_selector_keep_working_after_navigation(self):
        with LocatorSite() as site:
            for engine in ("playwright", "selenium"):
                with self.subTest(engine=engine):
                    before = len(site.hits)
                    automation = {"start_url": "/plain", "actions": [
                        {"action": "fill", "selector": "#name", "value": "首次输入"},
                        {"action": "reload"},
                        {"action": "fill", "locators": [{"by": "xpath", "value": "//input[@id='name']"}], "value": "页面重载后"},
                        {"action": "click", "selector": "xpath=//button[@id='save']"},
                    ], "assertions": [{"assertion": "text", "selector": "#result", "expected": "保存成功：页面重载后"}]}
                    result = self.run_export(site, automation, engine)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(site.hits[before:], [{"where": "plain", "value": "页面重载后"}])

    def test_missing_context_never_passes_hidden_or_zero_count(self):
        with LocatorSite() as site:
            for engine in ("playwright", "selenium"):
                for assertion in ("hidden", "count"):
                    with self.subTest(engine=engine, assertion=assertion):
                        context = site.context
                        context[1]["locators"] = [{"by": "css", "value": "#missing-frame"}]
                        automation = {"start_url": "/mixed", "actions": [], "assertions": [{
                            "assertion": assertion, "expected": 0, "context": context,
                            "locators": [{"by": "xpath", "value": ".//p[@id='present']", "xpath_anchor": "#content"}],
                            "timeout_seconds": 0}]}
                        result = self.run_export(site, automation, engine)
                        self.assertNotEqual(result.returncode, 0, result.stdout)
                        self.assertIn("上下文未找到", result.stderr)
                        self.assertNotIn("PASS TC-EXPORT-LOCATORS", result.stdout)

    def test_closed_shadow_reports_unsupported_in_each_standalone_script(self):
        with LocatorSite() as site:
            for engine in ("playwright", "selenium"):
                with self.subTest(engine=engine):
                    automation = {"start_url": "/mixed", "actions": [], "assertions": [{
                        "assertion": "hidden", "context": [{"kind": "shadow", "locators": [{"by": "css", "value": "#closed-shell"}]}],
                        "locators": [{"by": "css", "value": "button"}], "timeout_seconds": 0}]}
                    result = self.run_export(site, automation, engine)
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertIn("不支持 closed 模式", result.stderr)


if __name__ == "__main__":
    unittest.main()
