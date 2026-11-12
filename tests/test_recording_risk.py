"""Risk labels must survive alternative input paths that do not use click/fill."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill import projects
from autoskill.recorder import compile as compiler


class RecordingRiskTests(unittest.TestCase):
    def test_compiled_mutations_cannot_bypass_write_label_without_click_or_fill(self):
        mutations = [
            {"action": "keyboard", "operation": operation, "value": "Enter" if operation in ("press", "down", "up") else "text"}
            for operation in ("type", "inserttext", "press", "down", "up")
        ] + [
            {"action": "mouse", "operation": "down", "button": "left"},
            {"action": "mouse", "operation": "up", "button": "left"},
            {"action": "eval", "script": "() => document.body.dataset.saved = 'yes'"},
            {"action": "upload", "selector": "#upload", "files": ["files/uploads/document.txt"]},
            {"action": "download", "selector": "#export", "filename": "export.txt"},
            {"action": "drag", "selector": "#from", "target_locators": [{"by": "css", "value": "#to"}]},
            {"action": "clipboard", "operation": "write", "text": "copied"},
            {"action": "dialog", "mode": "accept"},
            {"action": "native_step", "command": "set", "args": ["device", "phone"], "reason": "not portable"},
            {"action": "browser_command", "command": "storage", "args": ["local", "set", "saved", "yes"]},
            {"action": "browser_command", "command": "cookies", "args": ["clear"]},
            {"action": "browser_command", "command": "set", "args": ["offline", "on"]},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            previous = os.environ.get("AUTOSKILL_WORKSPACE")
            os.environ["AUTOSKILL_WORKSPACE"] = temporary
            try:
                pid = projects.create("Risk classification", "https://example.test", project_id="risk")["id"]
                for index, step in enumerate(mutations):
                    with self.subTest(action=step):
                        rec = {"start_url": "/", "steps": [step, {"assertion": "title", "expected": "saved"}], "secrets": []}
                        summary = compiler.build(pid, rec, {"log": {"entries": []}}, None, f"Alternative input {index}")
                        case = next(case for case in projects.cases(pid) if case["id"] == summary["ui_case"]["id"])
                        self.assertEqual(case["risk"], "write")
                        self.assertEqual(case["extensions"]["automation"]["actions"], [step])
            finally:
                if previous is None:
                    os.environ.pop("AUTOSKILL_WORKSPACE", None)
                else:
                    os.environ["AUTOSKILL_WORKSPACE"] = previous

    def test_navigation_and_observation_keep_existing_read_semantics(self):
        observations = [
            {"action": "goto", "url": "/"}, {"action": "back"}, {"action": "forward"}, {"action": "reload"},
            {"action": "hover", "selector": "#menu"}, {"action": "focus", "selector": "#input"},
            {"action": "mouse", "operation": "move", "x": 100, "y": 200},
            {"action": "mouse", "operation": "wheel", "delta_y": 100},
            {"action": "scroll", "direction": "down", "pixels": 100},
            {"action": "scroll_into_view", "selector": "#result"},
            {"action": "frame", "selector": "#child"}, {"action": "frame", "selector": "main"},
            {"action": "wait", "selector": "#done", "timeout_seconds": 600},
            {"action": "tab_new", "tab": "t2", "url": "/help"},
            {"action": "tab_switch", "tab": "t1"},
            {"assertion": "title", "expected": "Read view"},
        ]
        self.assertEqual(compiler.ui_risk(observations), "read")
        self.assertEqual(compiler.ui_risk([]), "read")

    def test_native_observation_subcommands_are_distinct_from_mutations(self):
        for command, observed, changed in (
            ("storage", ["local", "item"], ["local", "clear"]),
            ("cookies", ["get"], ["set", "item", "value"]),
            ("network", ["requests"], ["route", "*/api/*", "--abort"]),
            ("clipboard", ["read"], ["write", "value"]),
            ("state", ["save", "snapshot.json"], ["load", "snapshot.json"]),
        ):
            with self.subTest(command=command):
                self.assertEqual(compiler.ui_risk([{"action": "browser_command", "command": command, "args": observed}]), "read")
                self.assertEqual(compiler.ui_risk([{"action": "browser_command", "command": command, "args": changed}]), "write")

    def test_held_mouse_move_and_new_opaque_actions_do_not_lose_the_write_risk(self):
        self.assertEqual(compiler.ui_risk([
            {"action": "mouse", "operation": "down", "button": "left"},
            {"action": "mouse", "operation": "move", "x": 200, "y": 200},
        ]), "write")
        self.assertEqual(compiler.ui_risk([{"action": "new_native_input"}]), "write")
        self.assertEqual(compiler.ui_risk([{"action": "browser_command", "command": "unknown", "args": []}]), "write")
        self.assertEqual(compiler.ui_risk([{"action": "native_step", "command": "unknown", "args": [], "reason": "opaque"}]), "write")

    def test_legacy_click_and_keyboard_paths_keep_the_write_label(self):
        for action in ("fill", "type", "click", "dblclick", "press", "check", "uncheck", "select", "captcha"):
            with self.subTest(action=action):
                self.assertEqual(compiler.ui_risk([{"action": action}]), "write")


if __name__ == "__main__":
    unittest.main()
