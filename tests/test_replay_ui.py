"""Real-browser replay tests: the same case must be judged identically by Playwright and Selenium."""
import copy
import functools
import json
import sys
import tempfile
import threading
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))
__import__("os").environ.setdefault("AUTOSKILL_HEADED", "0")  # tests never open browser windows
from autoskill import paths
from autoskill.replay import ir, ui


class Quiet(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def log_message(self, *args):
        pass

    def handle(self):
        try:
            super().handle()
        except ConnectionError:  # the browser closed a keep-alive socket
            return


def serve():
    handler = functools.partial(Quiet, directory=str(ROOT / "skills/autoskill/examples/todomvc-site"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


class StepModelTests(unittest.TestCase):
    def test_only_whitelisted_steps_are_accepted(self):
        good = {"start_url": "/", "actions": [{"action": "click", "locators": [{"by": "testid", "value": "save", "nth": 1}]}],
                "assertions": [{"assertion": "url_contains", "expected": "/done"}]}
        ir.validate(good)
        for bad in ({"action": "eval", "value": "alert(1)"}, {"action": "click"},
                    {"action": "click", "locators": [{"by": "xpath", "value": "//a"}]},
                    {"action": "fill", "selector": "input"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ir.validate({"start_url": "/", "actions": [bad], "assertions": []})

    def test_origin_errors_never_echo_credentials(self):
        with self.assertRaises(PermissionError) as caught:
            ir.check_origin("http://user:hunter2@10.0.0.5:8080/x", ["http://127.0.0.1"])
        self.assertNotIn("hunter2", str(caught.exception))
        self.assertIn("10.0.0.5:8080", str(caught.exception))


class BrowserReplayMixin:
    engine = ""

    @classmethod
    def setUpClass(cls):
        cls.site, cls.base = serve()
        cls.other, cls.other_base = serve()
        cls.config = {"base_url": cls.base, "allowed_origins": [cls.base], "timeout_seconds": 4}
        cases = json.loads((ROOT / "tests/fixtures/todomvc-ui-cases.json").read_text(encoding="utf-8"))
        cls.case = next(c for c in cases if c["id"] == "TC-TODO-UI-002")
        cls.case["extensions"]["automation"]["start_url"] = "/"
        cls.tmp = tempfile.TemporaryDirectory()
        cls.driver = cls.make_driver()

    @classmethod
    def tearDownClass(cls):
        cls.driver.close()
        for server in (cls.site, cls.other):
            server.shutdown()
            server.server_close()
        cls.tmp.cleanup()

    def run_variant(self, edit=None):
        case = copy.deepcopy(self.case)
        if edit:
            edit(case["extensions"]["automation"])
        # Upstream initializes its route/count in window.load, after DOMContentLoaded.
        # Establish that real application precondition before typing; retain the original
        # actions and business assertions, including the intentionally wrong expectation.
        case["extensions"]["automation"]["actions"].insert(0, {
            "assertion": "contains", "selector": ".todo-count", "expected": "0",
        })
        self.driver.reset()
        return ui.run_case(self.driver, case, self.config, Path(self.tmp.name), self.engine + ".png")

    def test_recorded_case_passes_and_leaves_a_screenshot(self):
        result = self.run_variant()
        self.assertEqual(result["status"], "Pass", result["reason"])
        self.assertTrue((Path(self.tmp.name) / result["screenshots"][0]).stat().st_size > 1000)
        self.assertTrue(all(step["status"] == "done" for step in result["steps"]))

    def test_wrong_expectation_fails_with_the_actual_value(self):
        result = self.run_variant(lambda a: a["assertions"][1].update(expected="something else"))
        self.assertEqual((result["status"], result["category"]), ("Fail", ui.ASSERTION))
        self.assertIn("autoSkill A", result["reason"])

    def test_missing_element_fails_as_locator_not_found(self):
        result = self.run_variant(lambda a: a["actions"].append({"action": "click", "selector": "#nope"}))
        self.assertEqual((result["status"], result["category"]), ("Fail", ui.LOCATOR))

    def test_other_origin_can_be_navigated_and_replayed(self):
        result = self.run_variant(lambda a: a.update(start_url=self.other_base + "/"))
        self.assertEqual(result["status"], "Pass", result["reason"])
        self.assertTrue(result["screenshots"])

    def test_fallback_locator_is_used_and_reported(self):
        def edit(automation):
            step = automation["actions"][0]
            step.pop("selector")
            step["locators"] = [{"by": "testid", "value": "gone"}, {"by": "placeholder", "value": "What needs to be done?"}]

        result = self.run_variant(edit)
        self.assertEqual((result["status"], result["fallbacks"]), ("Pass", 1))

    def test_page_can_fetch_and_reload_outside_the_legacy_allow_list(self):
        self.driver.reset()
        self.driver.goto(self.base + "/")
        self.assertEqual(self.fetch_from_page(self.other_base + "/index.html"), "reached 200")
        self.driver.goto(self.other_base + "/index.html")
        self.driver.reload()
        self.assertIn(self.other_base, self.driver.url())


class PlaywrightReplayTests(BrowserReplayMixin, unittest.TestCase):
    engine = "playwright"

    @classmethod
    def make_driver(cls):
        from autoskill.replay.playwright_driver import PlaywrightDriver

        return PlaywrightDriver(cls.config)

    def fetch_from_page(self, url):
        return self.driver.page.evaluate("u => fetch(u).then(r => 'reached ' + r.status).catch(() => 'blocked')", url)


@unittest.skipUnless(paths.chrome() and paths.chromedriver(), "set AUTOSKILL_CHROMIUM and AUTOSKILL_CHROMEDRIVER")
class SeleniumReplayTests(BrowserReplayMixin, unittest.TestCase):
    engine = "selenium"

    @classmethod
    def make_driver(cls):
        from autoskill.replay.selenium_driver import SeleniumDriver

        return SeleniumDriver(cls.config)

    def fetch_from_page(self, url):
        script = "const done = arguments[arguments.length - 1]; fetch(arguments[0]).then(r => done('reached ' + r.status)).catch(() => done('blocked'));"
        return self.driver.wd.execute_async_script(script, url)


if __name__ == "__main__":
    unittest.main()
