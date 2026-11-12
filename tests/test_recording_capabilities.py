"""Recorder contracts that do not require a running browser."""
import base64
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill.recorder import actions, session
from autoskill.replay import ir


class RecordingCapabilityContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.rec = {"project_id": "demo", "steps": [], "refs": {}, "secrets": [], "rand": "abc123"}
        ir.set_rand(self.rec["rand"])
        self.config = {"base_url": "http://localhost", "allowed_origins": [], "page_load_seconds": 1}
        self.enterContext(patch.object(session, "_file", return_value=Path(self.temp.name) / "recording.json"))
        self.enterContext(patch.object(session.projects, "config", return_value=self.config))
        self.enterContext(patch.object(session, "_sync_tabs"))
        self.enterContext(patch.object(session, "_observe_tabs", return_value=None))
        self.enterContext(patch.object(session, "snapshot", return_value="page"))
        self.enterContext(patch.object(session, "_after", side_effect=lambda rec, result: result))
        self.enterContext(patch.object(session, "_wait_ready", return_value={}))
        self.call = self.enterContext(patch.object(session.agent, "call", return_value={}))
        self.act = self.enterContext(patch.object(session, "_act", return_value=None))
        self.target = self.enterContext(patch.object(session, "_target", side_effect=self.found))
        self.secret_values = {}
        self.enterContext(patch.object(session.secrets, "get_secret", side_effect=self.secret_values.get))
        self.enterContext(patch.object(session.secrets, "set_secret", side_effect=self.secret_values.__setitem__))

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def found(rec, token, rich=False):
        return {"locators": [{"by": "css", "value": token}], "tag": "input", "editable": True}

    def run_command(self, *argv):
        result = session._browser(self.rec, list(argv))
        step = self.rec["steps"][-1]
        ir.validate({"start_url": "/", "actions": [step], "assertions": []})
        return step, result

    def test_navigation_focus_drag_scroll_and_wait_are_recorded(self):
        cases = [
            (("back",), {"action": "back"}),
            (("forward",), {"action": "forward"}),
            (("focus", "#name"), {"action": "focus", "locators": [{"by": "css", "value": "#name"}]}),
            (("drag", "#a", "#b"), {"action": "drag", "locators": [{"by": "css", "value": "#a"}],
                                     "target_locators": [{"by": "css", "value": "#b"}]}),
            (("scrollintoview", "#footer"), {"action": "scroll_into_view", "locators": [{"by": "css", "value": "#footer"}]}),
            (("scroll", "left", "0"), {"action": "scroll", "direction": "left", "pixels": 0}),
            (("wait", "25"), {"action": "wait", "milliseconds": 25}),
            (("wait", "#gone", "--state", "detached"), {"action": "wait", "selector": "#gone", "state": "detached"}),
        ]
        for argv, expected in cases:
            with self.subTest(argv=argv):
                step, _ = self.run_command(*argv)
                self.assertEqual(expected, step)

    def test_detached_wait_does_not_require_a_live_target(self):
        self.run_command("wait", "#missing", "--state", "detached")
        self.target.assert_not_called()
        self.call.assert_called_once_with(session.session_name(), ["wait", "#missing", "--state", "detached"])

    def test_raw_keyboard_keeps_sensitive_value_out_of_recording(self):
        with patch.object(session, "_describe", return_value={"secret": True}):
            step, _ = self.run_command("keyboard", "inserttext", "a-password")
        self.assertEqual("${ENV:DEMO_PASSWORD}", step["value"])
        self.assertNotIn("a-password", json.dumps(self.rec))
        self.assertEqual("a-password", self.secret_values["DEMO_PASSWORD"])
        self.act.assert_called_once_with(self.rec, ["keyboard", "inserttext", "a-password"])

    def test_mouse_coordinates_and_zero_wheel_deltas(self):
        step, _ = self.run_command("mouse", "move", "12.5", "2")
        self.assertEqual({"action": "mouse", "operation": "move", "x": 12.5, "y": 2}, step)
        step, _ = self.run_command("mouse", "wheel", "0", "0")
        self.assertEqual(0, step["delta_y"])
        self.assertEqual(0, step["delta_x"])
        before = len(self.rec["steps"])
        with self.assertRaises(ValueError):
            self.run_command("mouse", "move", "NaN", "0")
        self.assertEqual(before, len(self.rec["steps"]))

    def test_eval_base64_payload_is_local_reference(self):
        script = "() => { window.token = 'sensitive-token'; return 42; }"
        encoded = base64.b64encode(script.encode()).decode()
        step, _ = self.run_command("eval", "-b", encoded)
        self.assertEqual("${ENV:DEMO_EVAL_SCRIPT}", step["script"])
        self.assertNotIn("sensitive-token", json.dumps(self.rec))
        self.assertEqual(script, self.secret_values["DEMO_EVAL_SCRIPT"])
        self.assertIn(script, self.call.call_args.kwargs["stdin_text"])

    def test_dialog_preparation_persists_before_trigger_and_undo_disarms(self):
        result = session._dialog(self.rec, ["accept", "prompt-secret"])
        self.assertEqual(1, result["recorded"])
        self.assertEqual({"mode": "accept", "text": "${ENV:DEMO_DIALOG_TEXT}"}, self.rec["dialog_policy"])
        self.assertNotIn("prompt-secret", json.dumps(self.rec))
        self.assertEqual("dialog", self.rec["steps"][0]["action"])
        with patch.object(session, "current", return_value=self.rec):
            session.undo()
        self.assertNotIn("dialog_policy", self.rec)

    def test_nonportable_options_go_to_native_without_fake_portable_step(self):
        with patch.object(session, "_native", return_value={"replayable": False}) as native:
            self.assertEqual({"replayable": False}, session._browser(self.rec, ["click", "#link", "--new-tab"]))
            self.assertEqual({"replayable": False}, session._browser(self.rec, ["type", "#name", "--delay", "20", "value"]))
            self.assertEqual({"replayable": False}, session._browser(self.rec, ["wait", "--text", "Saved"]))
        self.assertEqual(3, native.call_count)
        self.assertEqual([], self.rec["steps"])

    def test_flag_looking_input_remains_literal(self):
        step, _ = self.run_command("fill", "#name", "--", "--clear")
        self.assertEqual("--clear", step["value"])
        self.act.assert_called_once_with(self.rec, ["fill", "#name", "--", "--clear"])

    def test_frame_keeps_nested_path_and_main_resets(self):
        self.target.side_effect = lambda rec, token, rich=False: {"locators": [{"by": "css", "value": token}], "tag": "iframe"}
        self.run_command("frame", "#outer")
        self.run_command("frame", "#inner")
        self.assertEqual(["#outer", "#inner"], self.rec["frame_path"])
        self.run_command("frame", "main")
        self.assertEqual([], self.rec["frame_path"])

    def test_native_find_in_nested_frame_fails_before_main_frame_mutation(self):
        self.rec["frame_path"] = ["#outer", "#inner"]
        with self.assertRaisesRegex(session.RecordError, "操作尚未执行"):
            session._native(self.rec, ["find", "text", "Delete", "click"])
        self.call.assert_not_called()
        self.assertEqual([], self.rec["steps"])

    def test_single_character_press_in_password_becomes_secret_reference(self):
        with patch.object(session, "_describe", return_value={"secret": True}):
            step, _ = self.run_command("press", "x")
        self.assertEqual("keyboard", step["action"])
        self.assertEqual("${ENV:DEMO_PASSWORD}", step["value"])
        self.assertEqual("x", self.secret_values["DEMO_PASSWORD"])


class NativeActionNumbers(unittest.TestCase):
    def test_finite_numeric_contract(self):
        for invalid in ("nan", "inf", "-inf", "not-a-number"):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                actions.number(invalid, "coordinate")
        self.assertEqual(0, actions.number("0", "coordinate"))
        self.assertEqual(1.5, actions.number("1.5", "coordinate"))


if __name__ == "__main__":
    unittest.main()
