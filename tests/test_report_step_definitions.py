"""Check incomplete UI reports against the definition frozen for that run."""
import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/autoskill/app"))

from autoskill import execution_inputs
from autoskill.server.app import _supplement_ui_steps


class ReportStepDefinitionTests(unittest.TestCase):
    def test_navigation_timeout_and_definition_only_rows_share_only_the_frozen_plan(self):
        case = {"extensions": {"automation": {"start_url": "/start", "actions": [{"action": "goto", "url": "/next"}],
                                              "assertions": [{"name": "页面标题", "assertion": "title", "expected": "示例"}]}}}
        failed = {"kind": "ui", "case_id": "example", "case_content_hash": execution_inputs.content_hash(case),
                  "state": {"evidence": "valid"}, "detail": {"steps": [{"text": "打开 /start", "status": "failed", "error": "导航超时"}]}}
        missing = {**failed, "detail": {}}
        mismatched = {**failed, "case_content_hash": "other-version", "detail": {}}
        unverified = {**copy.deepcopy(failed), "state": {"evidence": "invalid"}}
        rows = [failed, missing, mismatched, unverified]
        with patch.object(execution_inputs, "load", return_value={"cases": {"example": case}}) as load:
            _supplement_ui_steps({"input_manifest": "example"}, rows)
        load.assert_called_once()
        self.assertEqual(len(failed["detail"]["steps"]), 1)
        pending = failed["detail"]["pending_steps"]
        self.assertIn("/next", pending[0]["text"])
        self.assertEqual(pending[1], {"text": "页面标题", "check": True})
        self.assertEqual(missing["detail"], {"definition_steps": [{"text": "打开 /start"}, *pending]})
        self.assertFalse(mismatched["detail"])
        self.assertNotIn("pending_steps", unverified["detail"])
