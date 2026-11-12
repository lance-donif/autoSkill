"""Legacy API runs enforce every supported response assertion without saving response values."""
import contextlib
import datetime
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill import builder
from autoskill.core import store


class LegacyAssertionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name) / "project"
        environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": self.tmp.name})
        environment.start()
        self.addCleanup(environment.stop)
        store.init(self.folder)
        store.write(self.folder / "analysis/requirements.json",
                    [{"id": "REQ-RECORDED", "module": "Regression", "title": "HTTP assertions", "source": "fixture"}])
        store.write(self.folder / "qa-config.json",
                    {"mode": "auto", "execution_authorized": True, "base_url": "https://example.invalid"})
        self.case = builder.api_case("TC-API-001", "Response contract", "GET", "/check", {
            "status": 200, "body_contains": ["service ready", "completed"],
            "header_contains": {"X-Result": "ready"}, "max_ms": 50,
        })
        store.save_cases(self.folder, [self.case])

    def response(self, *, status=200, text="service ready and completed", headers=None, elapsed_ms=25):
        response = httpx.Response(status, text=text, headers={"x-result": "READY; complete"} if headers is None else headers,
                                  request=httpx.Request("GET", "https://example.invalid/check"))
        response.elapsed = datetime.timedelta(milliseconds=elapsed_ms)
        return response

    def run_response(self, response):
        with patch.object(httpx.Client, "request", return_value=response), contextlib.redirect_stdout(io.StringIO()):
            result = store.run_api(self.folder)[0]
        self.assertEqual(store.validate(self.folder), [])
        return result

    def test_matching_body_headers_and_timing_pass_with_recorded_checks(self):
        result = self.run_response(self.response())
        self.assertEqual(result["status"], "Pass")
        self.assertEqual(len(result["assertions"]), 5)
        self.assertTrue(all(check["passed"] for check in result["assertions"]))
        evidence = store.read(self.folder / result["evidence"][0])
        self.assertEqual(evidence["assertions"], result["assertions"])

    def test_each_mismatched_response_contract_fails_the_run(self):
        for changed in ({"text": "service ready but unfinished"}, {"headers": {"X-Result": "wrong"}},
                        {"headers": {}}, {"elapsed_ms": 1000}):
            with self.subTest(changed=changed):
                result = self.run_response(self.response(**changed))
                self.assertEqual(result["status"], "Fail")
                self.assertEqual(sum(not check["passed"] for check in result["assertions"]), 1)

    def test_timing_limit_is_inclusive_without_truncating_milliseconds(self):
        self.assertEqual(self.run_response(self.response(elapsed_ms=50))["status"], "Pass")
        self.assertEqual(self.run_response(self.response(elapsed_ms=50.001))["status"], "Fail")

    def test_body_comparison_remains_case_sensitive(self):
        result = self.run_response(self.response(text="SERVICE READY and completed"))
        self.assertEqual(result["status"], "Fail")

    def test_expected_environment_values_are_checked_without_persisting_them(self):
        self.case["assertions"].update(body_contains=["${ENV:LEGACY_REVIEW_EXPECTED}"],
                                       header_contains={"X-Result": "${ENV:LEGACY_REVIEW_EXPECTED}"})
        store.save_cases(self.folder, [self.case])
        marker = "synthetic-response-only-marker"
        with patch.dict(os.environ, {"LEGACY_REVIEW_EXPECTED": marker}):
            result = self.run_response(self.response(text=marker, headers={"X-Result": marker}))
        self.assertEqual(result["status"], "Pass")
        for file in (self.folder / "execution").rglob("*.json"):
            self.assertNotIn(marker, file.read_text(encoding="utf-8"))

    def test_missing_expected_environment_value_blocks_instead_of_passing(self):
        self.case["assertions"]["body_contains"] = ["${ENV:LEGACY_REVIEW_MISSING}"]
        store.save_cases(self.folder, [self.case])
        os.environ.pop("LEGACY_REVIEW_MISSING", None)
        result = self.run_response(self.response())
        self.assertEqual(result["status"], "Blocked")
        self.assertIn("LEGACY_REVIEW_MISSING", result["reason"])


if __name__ == "__main__":
    unittest.main()
