"""Expected child closure must preserve parent assertions without hiding unrelated target loss."""
import copy
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from test_tabs import BrowserSiteServer, Site

from autoskill.replay import ui
from autoskill.replay.playwright_driver import PlaywrightDriver


class ClosingWaitDriver(PlaywrightDriver):
    """Pump the real page after dispatch to consistently exercise a closed-target acknowledgement."""

    def act(self, locator, op, value):
        super().act(locator, op, value)
        if locator.get("value") == "#send":
            self.page.wait_for_timeout(10)


class BrowserExitDriver(PlaywrightDriver):
    def act(self, locator, op, value):
        if locator.get("value") == "#send":
            self.browser.close()
        super().act(locator, op, value)


class PlaywrightTabCloseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = BrowserSiteServer(("127.0.0.1", 0), Site)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.previous = os.environ.get("AUTOSKILL_WORKSPACE")
        os.environ["AUTOSKILL_WORKSPACE"] = self.temporary.name
        self.config = {"base_url": self.base, "timeout_seconds": 2, "page_load_seconds": 5}
        self.case = {"id": "SELF-CLOSE", "kind": "ui", "extensions": {"automation": {
            "start_url": "/", "actions": [
                {"action": "click", "selector": "#popup", "tabs": {
                    "opened": [{"id": "t2", "url": "/child?source=popup"}], "closed": [], "active": "t2"}},
                {"action": "fill", "selector": "#note", "value": "result from child"},
                {"action": "click", "selector": "#send", "tabs": {"opened": [], "closed": ["t2"], "active": "t1"}},
            ], "assertions": [{"assertion": "text", "selector": "#result", "expected": "result from child"}],
        }}}

    def tearDown(self):
        if self.previous is None:
            os.environ.pop("AUTOSKILL_WORKSPACE", None)
        else:
            os.environ["AUTOSKILL_WORKSPACE"] = self.previous
        self.temporary.cleanup()

    def replay(self, case, driver_type=ClosingWaitDriver):
        driver = driver_type(self.config)
        try:
            driver.reset()
            return ui.run_case(driver, case, self.config, Path(self.temporary.name), "result.png")
        finally:
            driver.close()

    def test_expected_child_close_continues_parent_business_assertion(self):
        outcome = self.replay(self.case)
        self.assertEqual(outcome["status"], "Pass", outcome)
        self.assertEqual(outcome["business_status"], "Pass", outcome)
        self.assertEqual(outcome["assertions"][0]["actual"], "result from child")

    def test_parent_oracle_still_fails_after_expected_close(self):
        self.case["extensions"]["automation"]["assertions"][0]["expected"] = "independent expected result"
        self.case["extensions"]["automation"]["assertions"][0]["timeout_seconds"] = 0.3
        outcome = self.replay(self.case)
        self.assertEqual(outcome["status"], "Fail", outcome)
        self.assertEqual(outcome["category"], "assertion_failed", outcome)
        self.assertEqual(outcome["assertions"][0]["expected"], "independent expected result")
        self.assertEqual(outcome["assertions"][0]["actual"], "result from child")

    def test_unrecorded_child_close_is_not_action_success(self):
        case = copy.deepcopy(self.case)
        del case["extensions"]["automation"]["actions"][-1]["tabs"]
        case["extensions"]["automation"]["assertions"] = []
        outcome = self.replay(case)
        self.assertEqual(outcome["status"], "Fail", outcome)
        self.assertEqual(outcome["category"], "environment", outcome)
        self.assertIn("TargetClosedError", outcome["reason"])

    def test_browser_exit_is_not_expected_child_close(self):
        outcome = self.replay(self.case, BrowserExitDriver)
        self.assertEqual(outcome["status"], "Fail", outcome)
        self.assertEqual(outcome["category"], "environment", outcome)
        self.assertIn("TargetClosedError", outcome["reason"])

    def test_missing_recorded_destination_is_not_accepted(self):
        self.case["extensions"]["automation"]["actions"][-1]["tabs"]["active"] = "t3"
        outcome = self.replay(self.case)
        self.assertEqual(outcome["status"], "Fail", outcome)
        self.assertEqual(outcome["category"], "environment", outcome)
        self.assertIn("TargetClosedError", outcome["reason"])


if __name__ == "__main__":
    unittest.main()
