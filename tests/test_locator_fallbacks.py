"""Absence assertions must inspect available fallback locators before passing."""
import itertools
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill.replay import ui

LOCATORS = [{"by": "testid", "value": "old-id"}, {"by": "css", "value": "#error"}]


class VisibleFallback:
    def count(self, locator):
        return int(locator == LOCATORS[1])

    def read(self, _locator, what):
        return True if what == "visible" else "ready"


class LocatorFallbackTests(unittest.TestCase):
    def test_absence_assertions_fail_when_the_fallback_is_visible(self):
        for assertion in ({"assertion": "hidden"}, {"assertion": "count", "expected": 0}):
            with self.subTest(assertion=assertion):
                result = ui.check(VisibleFallback(), {**assertion, "locators": LOCATORS}, 0)
                self.assertFalse(result["passed"])
                self.assertTrue(result["fallback"])

    def test_positive_assertions_use_the_matching_fallback(self):
        result = ui.check(VisibleFallback(), {"assertion": "visible", "locators": LOCATORS}, 0)
        self.assertTrue(result["passed"])
        self.assertTrue(result["fallback"])

    def test_assertion_reselects_after_the_fallback_appears(self):
        class Appearing(VisibleFallback):
            attempts = 0

            def count(self, locator):
                if locator != LOCATORS[1]:
                    return 0
                self.attempts += 1
                return int(self.attempts > 1)

        with patch.object(ui.time, "monotonic", side_effect=itertools.count(step=0.01)), patch.object(ui.time, "sleep"):
            result = ui.check(Appearing(), {"assertion": "text", "expected": "ready", "locators": LOCATORS}, 1)
        self.assertTrue(result["passed"])
        self.assertTrue(result["fallback"])

    def test_wait_hidden_does_not_ignore_a_visible_fallback(self):
        with self.assertRaises(ui.StepError) as caught:
            ui.perform(VisibleFallback(), {"action": "wait_hidden", "locators": LOCATORS}, "https://example.test", [], 0)
        self.assertEqual(caught.exception.category, ui.TIMEOUT)

    def test_absence_can_pass_when_none_of_the_locators_match(self):
        with patch.object(VisibleFallback, "count", return_value=0):
            result = ui.check(VisibleFallback(), {"assertion": "hidden", "locators": LOCATORS}, 0)
        self.assertTrue(result["passed"])


if __name__ == "__main__":
    unittest.main()
