"""Shared replay contracts that do not require a browser or an external site."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill.replay import ir, ui
from autoskill.replay.primitives import DialogPolicy, SeleniumDialogs, output_file, project_asset
from autoskill.replay.selenium_driver import SeleniumDriver


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class StreamPage:
    """A stream may show a plausible answer before its final completion signal."""

    def __init__(self, clock, text):
        self.clock, self.text = clock, text

    def count(self, locator):
        return 1

    def read(self, locator, what, arg=None):
        return self.text(self.clock.now)


class DriverCapabilityContracts(unittest.TestCase):
    def test_assertion_budget_can_extend_past_project_default(self):
        clock = Clock()
        page = StreamPage(clock, lambda now: "done" if now >= 7 else "thinking")
        step = {"assertion": "text", "selector": "#done", "expected": "done", "timeout_seconds": 12}
        with patch.object(ui.time, "monotonic", clock.monotonic), patch.object(ui.time, "sleep", clock.sleep):
            result = ui.check(page, step, timeout=2)
        self.assertTrue(result["passed"])
        self.assertGreaterEqual(clock.now, 7)

    def test_stability_restarts_after_transient_first_completion(self):
        clock = Clock()
        page = StreamPage(clock, lambda now: "done" if now < 0.3 or now >= 1 else "thinking")
        step = {"assertion": "text", "selector": "#done", "expected": "done", "stable_for_seconds": 0.5}
        with patch.object(ui.time, "monotonic", clock.monotonic), patch.object(ui.time, "sleep", clock.sleep):
            result = ui.check(page, step, timeout=3)
        self.assertTrue(result["passed"])
        self.assertGreaterEqual(clock.now, 1.5)

    def test_a_read_finishing_after_the_deadline_cannot_report_success(self):
        clock = Clock()

        def slow_read(now):
            clock.sleep(1)
            return "done"

        page = StreamPage(clock, slow_read)
        step = {"assertion": "text", "selector": "#done", "expected": "done"}
        with patch.object(ui.time, "monotonic", clock.monotonic), patch.object(ui.time, "sleep", clock.sleep):
            result = ui.check(page, step, timeout=0.5)
        self.assertFalse(result["passed"])

    def test_secret_expectation_matches_without_persisting_the_actual_secret(self):
        clock = Clock()
        page = StreamPage(clock, lambda now: "confidential-result")
        step = {"assertion": "text", "selector": "#answer", "expected": "${ENV:REPLAY_SECRET}"}
        with patch.object(ui.secrets, "get_secret", return_value="confidential-result"):
            result = ui.check(page, step, timeout=1)
        self.assertTrue(result["passed"])
        self.assertNotIn("confidential-result", json.dumps(result))
        self.assertEqual(result["actual"], "<redacted>")

    def test_resolved_secret_script_still_expands_unique_run_value(self):
        ir.set_rand("UNIQUE123")
        try:
            with patch.object(ui.secrets, "get_secret", return_value="() => 'item-{{RAND}}'"):
                self.assertEqual(ui._resolve("${ENV:REPLAY_SCRIPT}"), "() => 'item-UNIQUE123'")
        finally:
            ir.set_rand("")

    def test_native_mutation_blocks_before_the_first_navigation(self):
        class UnopenedPage:
            def goto(self, url):
                raise AssertionError("unsupported cases must be checked before navigating")

        case = {"extensions": {"automation": {"start_url": "https://example.test/", "actions": [
            {"action": "native_step", "command": "network", "args": ["unsupported"], "reason": "not portable"}]}}}
        with tempfile.TemporaryDirectory() as temporary:
            result = ui.run_case(UnopenedPage(), case, {}, Path(temporary), "unused.png")
        self.assertEqual(result["status"], "Blocked")
        self.assertEqual(result["category"], ui.UNSUPPORTED)
        self.assertEqual(result["screenshots"], [])

    def test_dialog_choice_is_prepared_consumed_once_and_never_overwritten(self):
        policy = DialogPolicy()
        policy.arm("accept", "prompt result")
        with self.assertRaises(ValueError):
            policy.arm("dismiss")
        self.assertEqual(policy.take(), ("accept", "prompt result"))
        self.assertEqual(policy.take(), ("accept", None))
        policy.arm("dismiss")
        self.assertEqual(policy.take(), ("dismiss", None))
        self.assertFalse(policy.pending())

    def test_additional_browser_permissions_preserve_explicit_test_microphone(self):
        import threading

        events = SeleniumDialogs.__new__(SeleniumDialogs)
        events._lock = threading.RLock()
        events._permissions = set()
        grants = []
        events.command = lambda method, params: grants.append((method, params))
        events.grant_permissions(["audioCapture"])
        events.grant_permissions(["geolocation"])
        events.grant_permissions(["clipboardReadWrite", "clipboardSanitizedWrite"])
        self.assertEqual(grants[-1], ("Browser.grantPermissions", {"permissions": [
            "audioCapture", "clipboardReadWrite", "clipboardSanitizedWrite", "geolocation"]}))

    def test_selenium_auth_state_labels_each_frame_origin_and_restores_scope(self):
        class Switch:
            def __init__(self, browser):
                self.browser = browser

            def window(self, handle):
                self.browser.document = "main"

            def default_content(self):
                self.browser.document = "main"

            def frame(self, node):
                self.browser.document = "child"

            def parent_frame(self):
                self.browser.document = "main"

        class Browser:
            window_handles = ["tab1"]
            current_window_handle = "tab1"

            def __init__(self):
                self.document = "child"
                self.switch_to = Switch(self)

            def execute_cdp_cmd(self, method, params):
                return {"cookies": []}

            def execute_script(self, script):
                return {"origin": "https://" + self.document + ".example", "localStorage": [
                    {"name": "login", "value": self.document + "-value"}]}

            def find_elements(self, by, selector):
                return [object()] if self.document == "main" else []

        driver = SeleniumDriver.__new__(SeleniumDriver)
        driver.wd = Browser()
        driver._frame_path = ["#child"]
        driver._origins = set()
        state = driver.storage_state()
        self.assertEqual({entry["origin"]: entry["localStorage"][0]["value"] for entry in state["origins"]}, {
            "https://main.example": "main-value", "https://child.example": "child-value"})
        self.assertEqual(driver.wd.document, "child")
        self.assertEqual(driver._frame_path, ["#child"])

    def test_selenium_key_names_and_chords_preserve_explicit_held_modifiers(self):
        from selenium.webdriver.common.keys import Keys

        calls = []
        class Chain:
            def __init__(self, driver):
                pass
            def key_down(self, value):
                calls.append(("down", value)); return self
            def key_up(self, value):
                calls.append(("up", value)); return self
            def send_keys(self, value):
                calls.append(("press", value)); return self
            def perform(self):
                pass

        driver = SeleniumDriver.__new__(SeleniumDriver)
        driver.wd = object()
        driver._held_modifiers = {"Shift"}
        with patch("selenium.webdriver.common.action_chains.ActionChains", Chain):
            driver.press(None, "Shift+F1")
            self.assertEqual(calls, [("press", Keys.F1)])
            self.assertEqual(driver._held_modifiers, {"Shift"})
            calls.clear()
            driver.press(None, "ControlOrMeta+Insert")
            self.assertEqual(calls, [("down", Keys.CONTROL), ("press", Keys.INSERT), ("up", Keys.CONTROL)])
            calls.clear()
            driver.press(None, "+")
            self.assertEqual(calls, [("press", "+")])
            calls.clear()
            driver.press(None, "Shift")
            self.assertEqual(calls, [("press", Keys.SHIFT)])
            self.assertEqual(driver._held_modifiers, set())

    def test_portable_paths_reject_host_paths_traversal_and_link_escape(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "root"
            root.mkdir()
            (root / "input.txt").write_text("upload", encoding="utf-8")
            outside = Path(temporary) / "outside.txt"
            outside.write_text("outside", encoding="utf-8")
            (root / "escape.txt").symlink_to(outside)
            self.assertEqual(project_asset(root, "input.txt"), root / "input.txt")
            for invalid in ("../outside.txt", "/tmp/source.txt", "C:/source.txt", "dir\\file", "./input.txt", "escape.txt"):
                with self.subTest(path=invalid), self.assertRaises(ValueError):
                    project_asset(root, invalid)
            for invalid in ("../output.txt", "/tmp/output.txt", "C:/output.txt", "dir\\file", "nested/../file", "escape.txt"):
                with self.subTest(output=invalid), self.assertRaises(ValueError):
                    output_file(root, invalid)
            destination = output_file(root, "nested/result.txt")
            destination.write_text("evidence", encoding="utf-8")
            alternate = output_file(root, "nested/result.txt")
            self.assertEqual(alternate, root / "nested/result (1).txt")
            alternate.write_text("second evidence", encoding="utf-8")
            self.assertEqual(output_file(root, "nested/result.txt"), root / "nested/result (2).txt")
            self.assertEqual(destination.read_text(encoding="utf-8"), "evidence")


if __name__ == "__main__":
    unittest.main()
