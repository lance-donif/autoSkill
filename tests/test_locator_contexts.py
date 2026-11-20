"""Real dual-engine XPath and context tests with independent HTTP side-effect receipts."""
from __future__ import annotations

import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))
os.environ.setdefault("AUTOSKILL_HEADED", "0")
from locator_acceptance_fixture import LocatorSite

from autoskill import paths
from autoskill.replay import ir, ui
from autoskill.replay.playwright_driver import DOM_JS, PlaywrightDriver
from autoskill.replay.selenium_driver import SeleniumDriver


class LocatorModelTests(unittest.TestCase):
    def test_xpath_context_and_secondary_targets_are_validated(self):
        context = [{"kind": "shadow", "locators": [{"by": "css", "value": "#host"}]}]
        locator = {"by": "xpath", "value": ".//input", "xpath_anchor": "#form"}
        step = {"assertion": "value", "locators": [locator], "expected": "new", "context": context,
                "transition_from": {"assertion": "value", "expected": "old"}}
        ir.validate({"start_url": "/", "actions": [{"action": "captcha", "locators": [locator], "context": context,
                                                   "image": [{"by": "xpath", "value": "//img"}], "image_context": []}],
                     "assertions": [step]})
        for changes in ({"context": {}}, {"context": [{"kind": "closed", "locators": [locator]}]},
                        {"context": [{"kind": "frame", "locators": []}]},
                        {"locators": [{"by": "xpath", "value": "//input", "xpath_anchor": "#form"}]},
                        {"locators": [{"by": "role", "role": "checkbox", "name": "通知", "unique": True, "nth": 1}]},
                        {"locators": [{"by": "role", "role": "checkbox", "name": "通知", "unique": True, "anchor": {"by": "text", "value": "通知"}}]},
                        {"locators": [{"by": "css", "value": "#name", "context": context}]}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                ir.validate({"start_url": "/", "assertions": [{**step, **changes}]})
        self.assertEqual(ir.locators({"selector": "xpath=//input"}), [{"by": "xpath", "value": "//input"}])


class ContextBrowserMixin:
    driver_class = PlaywrightDriver

    @classmethod
    def setUpClass(cls):
        cls.site = LocatorSite().__enter__()
        cls.temp = tempfile.TemporaryDirectory()
        cls.driver = cls.driver_class(cls.site.config(), headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.driver.close()
        cls.temp.cleanup()
        cls.site.__exit__()

    def setUp(self):
        self.driver.reset()

    def script(self, source, context=None):
        context = self.site.context if context is None else context
        if isinstance(self.driver, PlaywrightDriver):
            _, root = self.driver._root(context)
            try:
                return root.evaluate("root => { const dom = (" + DOM_JS + ")(root); " + source + " }")
            finally:
                root.dispose()
        host = self.driver._root(context)
        return self.driver.wd.execute_script("const root = arguments[0] ? arguments[0].shadowRoot : document; const dom = ("
                                             + DOM_JS + ")(root); " + source, host)

    def open_mixed(self):
        self.driver.goto(self.site.base_url + "/mixed")
        checked = ui.check(self.driver, {"assertion": "visible", "selector": "#present", "context": self.site.context}, 3)
        self.assertTrue(checked["passed"], checked)

    def test_shared_label_reorder_keeps_the_recorded_role_and_rejects_ambiguous_controls(self):
        self.driver.goto(self.site.base_url)
        recorded = self.script("""
root.body.innerHTML = '<label><input type="checkbox">通知</label><button role="switch" aria-label="通知" aria-checked="false">状态</button>';
const toggle = root.querySelector('button');
toggle.onclick = () => toggle.setAttribute('aria-checked', 'true');
const field = root.querySelector('input');
const locators = dom.describe(field).locators;
root.body.prepend(toggle);
return locators;
""", [])
        old_xpath = "".join(f"/*[local-name()='{tag}'][1]" for tag in ("html", "body", "label", "input"))
        legacy = [{"by": "label", "value": "通知"}, {"by": "role", "role": "checkbox", "name": "通知"},
                  {"by": "css", "value": "label > input"}, {"by": "xpath", "value": old_xpath, "fragile": True}]
        for locators in (recorded, legacy):
            self.script("root.querySelector('input').checked = false;", [])
            ui.perform(self.driver, {"action": "check", "locators": locators}, "", [], 1)
            self.assertTrue(self.driver.read({"by": "role", "role": "checkbox", "name": "通知"}, "checked"))
            self.assertFalse(self.driver.read({"by": "role", "role": "switch", "name": "通知"}, "checked"))
        self.script("root.querySelector('input').checked=false; const row=root.querySelector('label'); root.body.append(row.cloneNode(true));", [])
        for locators in (recorded, legacy):
            with self.assertRaisesRegex(ui.StepError, "业务目标不唯一"):
                ui.perform(self.driver, {"action": "check", "locators": locators}, "", [], 0)
            self.assertFalse(ui.check(self.driver, {"assertion": "hidden", "locators": locators}, 0)["passed"])
        self.assertEqual(self.script("return root.querySelectorAll('input:checked').length;", []), 0)
        ui.perform(self.driver, {"action": "check", "locators": [{"by": "label", "value": "通知", "nth": 2}]}, "", [], 0)
        self.assertEqual(self.script("return [...root.querySelectorAll('input')].map(el => el.checked);", []), [False, True])

    def test_mixed_cross_origin_xpath_fallback_changes_only_leaf(self):
        before = len(self.site.hits)
        automation = self.site.automation("真实修改", force_xpath=True)
        case = {"extensions": {"automation": automation}}
        result = ui.run_case(self.driver, case, self.site.config(), Path(self.temp.name), "mixed.png")
        self.assertEqual(result["status"], "Pass", result)
        self.assertEqual(result["fallbacks"], 3)
        self.assertEqual(self.site.hits[before:], [{"where": "leaf", "value": "真实修改"}])
        self.assertEqual(self.driver.read({"by": "css", "value": "#name"}, "value"), "原名称")

    def test_missing_or_closed_context_cannot_satisfy_negative_assertions(self):
        self.open_mixed()
        paths_to_check = [[{"kind": "frame", "locators": [{"by": "css", "value": "#gone"}]}],
                          [{"kind": "shadow", "locators": [{"by": "css", "value": "#closed-shell"}]}]]
        for context in paths_to_check:
            for assertion in ("hidden", "count"):
                with self.subTest(context=context, assertion=assertion):
                    result = ui.check(self.driver, {"assertion": assertion, "selector": "#absent", "expected": 0,
                                                   "context": context}, 0)
                    self.assertFalse(result["passed"], result)
                    self.assertIn("上下文" if context[0]["kind"] == "frame" else "closed", result["actual"])
            with self.assertRaises(ui.StepError):
                ui.perform(self.driver, {"action": "wait_hidden", "selector": "#absent", "context": context}, "", [], 0)
        valid = ui.check(self.driver, {"assertion": "hidden", "selector": "#absent", "context": self.site.context}, 0)
        self.assertTrue(valid["passed"], valid)

    def test_auto_xpath_quotes_duplicates_and_dynamic_ids_are_exact(self):
        self.open_mixed()
        rows = self.script("""
const quoted = root.querySelector('#quoted');
quoted.setAttribute('data-testid', `both 'single' and "double"`);
const duplicates = Array.from(root.querySelectorAll('.duplicate'));
duplicates[1].id = 'react-123456789';
return [quoted, ...duplicates].map(el => {
 const candidates = dom.describe(el).locators;
 const xpath = candidates.at(-1);
 return {xpath, exact: dom.find(xpath).length === 1 && dom.find(xpath)[0] === el};
});
""")
        self.assertTrue(all(row["exact"] for row in rows), rows)
        self.assertTrue(all(row["xpath"]["by"] == "xpath" for row in rows), rows)
        self.assertIn("concat(", rows[0]["xpath"]["value"])
        self.assertNotIn("react-123456789", str(rows[2]["xpath"]))

    def test_invalid_xpath_anchor_and_escaping_paths_do_not_pass(self):
        self.open_mixed()
        for locator in ({"by": "xpath", "value": ".//input"},
                        {"by": "xpath", "value": ".//input", "xpath_anchor": "#missing"},
                        {"by": "xpath", "value": ".//input", "xpath_anchor": "button"},
                        {"by": "xpath", "value": "../*", "xpath_anchor": "#content"},
                        {"by": "xpath", "value": ".//@id", "xpath_anchor": "#content"},
                        {"by": "xpath", "value": ".//*[", "xpath_anchor": "#content"}):
            with self.subTest(locator=locator):
                result = ui.check(self.driver, {"assertion": "hidden", "locators": [locator], "context": self.site.context}, 0)
                self.assertFalse(result["passed"], result)
                self.assertIn("读取失败", result["actual"])

    def test_replaced_frames_and_shadow_hosts_are_resolved_again(self):
        self.open_mixed()
        self.script("""
const host=root.querySelector('#leaf-shell'), html=host.shadowRoot.innerHTML;
const replacement=host.cloneNode(); host.replaceWith(replacement);
replacement.attachShadow({mode:'open'}).innerHTML=html;
""", self.site.context[:-1])
        step = {"action": "fill", "selector": "#name", "value": "重建后的元素", "context": self.site.context}
        ui.perform(self.driver, step, "", [], 3)
        self.assertEqual(self.driver.read({"by": "css", "value": "#name", "context": self.site.context}, "value"), step["value"])
        self.script("const frame=root.querySelector('#remote-frame'); frame.replaceWith(frame.cloneNode());", self.site.context[:1])
        step["value"] = "重新加载的框架"
        ui.perform(self.driver, step, "", [], 3)
        self.assertEqual(self.driver.read({"by": "css", "value": "#name", "context": self.site.context}, "value"), step["value"])

    def test_transition_and_captcha_keep_their_contexts(self):
        self.open_mixed()
        assertion = {"assertion": "value", "selector": "#name", "context": self.site.context, "expected": "原名称",
                     "transition_from": {"assertion": "value", "expected": "原名称"}}
        result = ui.check(self.driver, assertion, 0.2)
        self.assertTrue(result["passed"], result)
        self.assertTrue(result["source_observation"]["observed"])
        self.script("root.querySelector('#content').insertAdjacentHTML('beforeend','<canvas id=picture width=60 height=20></canvas>');")
        image = [{"by": "css", "value": "#picture"}]
        step = {"action": "captcha", "selector": "#name", "context": self.site.context, "image": image}
        with patch("autoskill.captcha.solve", return_value="6789") as solve:
            ui.perform(self.driver, step, "", [], 1)
            self.assertTrue(solve.call_args.args[0].startswith(b"\x89PNG"))
        self.assertEqual(self.driver.read({"by": "css", "value": "#name", "context": self.site.context}, "value"), "6789")
        other = copy.deepcopy(step)
        other["image_context"] = []
        with self.assertRaises(ui.StepError):
            ui.perform(self.driver, other, "", [], 0)


class PlaywrightContextTests(ContextBrowserMixin, unittest.TestCase):
    driver_class = PlaywrightDriver


@unittest.skipUnless(paths.chrome() and paths.chromedriver(), "matching browser and chromedriver required")
class SeleniumContextTests(ContextBrowserMixin, unittest.TestCase):
    driver_class = SeleniumDriver


if __name__ == "__main__":
    unittest.main()
