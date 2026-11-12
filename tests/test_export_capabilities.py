"""Portable export contracts; browser integration lives in test_browser_capabilities."""
import ast
import json
import re
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill.replay import codegen, ir  # noqa: E402


class ExportCapabilitiesTests(unittest.TestCase):
    def case(self, actions):
        return {"id": "TC-EXPORT", "title": "Portable actions", "kind": "ui", "extensions": {
            "automation": {"start_url": "/", "actions": actions, "assertions": []}}}

    def test_native_mutation_is_valid_visible_and_cannot_export(self):
        case = self.case([{"action": "native_step", "command": "state", "args": ["load", "saved.json"],
                           "reason": "State import has no portable implementation", "replayable": False}])
        ir.validate(case["extensions"]["automation"])
        self.assertFalse(ir.is_replayable(case))
        self.assertIn("State import", ir.replay_blockers(case)[0])
        for engine in ("playwright", "selenium"):
            with self.subTest(engine=engine), self.assertRaisesRegex(ValueError, "state"):
                codegen.script([case], "http://example.test", engine)

    def test_files_cannot_escape_project_or_output(self):
        for name in ("/etc/passwd", "../secret", "files/../secret", "C:/secret", r"C:\secret", "files//secret", "${ENV:FILE}"):
            for action in ({"action": "upload", "selector": "#file", "files": [name]},
                           {"action": "download", "selector": "#link", "filename": name}):
                with self.subTest(action=action), self.assertRaises(ValueError):
                    ir.validate(self.case([action])["extensions"]["automation"])

    def test_all_portable_commands_generate_independent_python(self):
        actions = [
            {"action": "back"}, {"action": "forward"}, {"action": "focus", "selector": "#field"},
            {"action": "drag", "selector": "#source", "target_locators": [{"by": "text", "value": "Target"}]},
            {"action": "frame", "selector": "#outer"}, {"action": "frame", "selector": "#inner"},
            {"action": "eval", "script": "${ENV:SCRIPT}"}, {"action": "frame", "selector": "main"},
            {"action": "dialog", "mode": "accept", "text": "${ENV:PROMPT}"}, {"action": "click", "selector": "#prompt"},
            *[{"action": "keyboard", "operation": operation, "value": value} for operation, value in
              (("type", "text"), ("inserttext", "字"), ("down", "Shift"), ("up", "Shift"), ("press", "Control+A"))],
            {"action": "mouse", "operation": "move", "x": 0.25, "y": 3.5},
            {"action": "mouse", "operation": "down", "button": "left"},
            {"action": "mouse", "operation": "up", "button": "left"},
            {"action": "mouse", "operation": "wheel", "delta_x": 0, "delta_y": 0},
            {"action": "scroll", "direction": "down", "pixels": 2.5, "selector": "#list"},
            {"action": "scroll_into_view", "selector": "#last"}, {"action": "wait", "milliseconds": 1},
            {"action": "wait", "selector": "#done", "state": "visible", "timeout_seconds": 180, "stable_for_seconds": 2},
            {"action": "upload", "selector": "#file", "files": ["files/uploads/asset.txt"]},
            {"action": "download", "selector": "#link", "filename": "report.csv"},
            {"action": "clipboard", "operation": "write", "text": "value"},
            {"action": "browser_command", "command": "storage", "args": ["local", "set", "key", "value"]},
            {"action": "browser_command", "command": "set", "args": ["headers", "${ENV:HEADERS}"]},
            {"assertion": "min_count", "selector": "li", "expected": 0, "timeout_seconds": 180},
        ]
        case = self.case(actions)
        ir.validate(case["extensions"]["automation"])
        for engine in ("playwright", "selenium"):
            with self.subTest(engine=engine):
                source = codegen.script([case], "http://example.test", engine)
                ast.parse(source)
                self.assertNotIn("from autoskill", source)
                self.assertIn('os.environ["SCRIPT"].replace("{{RAND}}", RAND)', source)
                self.assertIn('"timeout_seconds": 180', source)
                self.assertIn('"stable_for_seconds": 2', source)

    def test_unknown_command_cannot_hide_behind_a_secret_reference(self):
        with self.assertRaises(ValueError):
            ir.validate(self.case([{"action": "browser_command", "command": "destroy", "args": ["${ENV:VALUE}"]}])[
                "extensions"]["automation"])

    def test_export_stages_assets_with_relative_names(self):
        case = self.case([{"action": "upload", "selector": "#file", "files": ["files/uploads/input.txt"]}])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            asset = root / "files/uploads/input.txt"
            asset.parent.mkdir(parents=True)
            asset.write_text("portable upload", encoding="utf-8")
            out = Path(temporary) / "moved scripts"
            with patch.object(codegen.projects, "cases", return_value=[case]), \
                    patch.object(codegen.projects, "config", return_value={"base_url": "http://example.test"}), \
                    patch.object(codegen.projects, "path", return_value=root), \
                    patch.object(codegen.runner, "build_plan", return_value=[(case["id"], "selected")]):
                result = codegen.export("project", str(out))
            self.assertEqual(len(result["scripts"]), 2, result)
            self.assertEqual((out / "assets/files/uploads/input.txt").read_text(), "portable upload")
            for name in result["scripts"]:
                source = (out / name).read_text()
                self.assertNotIn(str(root), source)
                ast.parse(source)

    def test_schema_and_ir_agree_on_new_step_shapes(self):
        from jsonschema import Draft202012Validator

        schema = json.loads((ROOT / "skills/autoskill/references/case.schema.json").read_text())
        validator = Draft202012Validator({"$defs": schema["$defs"], "$ref": "#/$defs/automation"})
        automation = self.case([{"action": "drag", "selector": "#a", "target_locators": [{"by": "role", "role": "button"}]},
                                {"action": "wait", "selector": "#done", "stable_for_seconds": 2, "timeout_seconds": 180}])[
            "extensions"]["automation"]
        self.assertFalse(list(validator.iter_errors(automation)))
        for field in ("../escape", "/absolute", "C:/drive", "dir/./file"):
            invalid = self.case([{"action": "download", "selector": "a", "filename": field}])["extensions"]["automation"]
            self.assertTrue(list(validator.iter_errors(invalid)), field)

    def test_fake_microphone_asset_is_copied_and_launch_is_portable(self):
        case = self.case([])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            audio = root / "files/media/test.wav"
            audio.parent.mkdir(parents=True)
            with wave.open(str(audio), "wb") as stream:
                stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                stream.writeframes(b"\x00\x00" * 800)
            config = {"base_url": "http://example.test", "media": {"audio_input_file": "files/media/test.wav"}}
            out = Path(temporary) / "export"
            with patch.object(codegen.projects, "cases", return_value=[case]), \
                    patch.object(codegen.projects, "config", return_value=config), \
                    patch.object(codegen.projects, "path", return_value=root), \
                    patch.object(codegen.runner, "build_plan", return_value=[(case["id"], "selected")]):
                result = codegen.export("project", str(out))
            self.assertEqual(len(result["scripts"]), 2, result)
            self.assertEqual((out / "assets/files/media/test.wav").read_bytes(), audio.read_bytes())
            for name in result["scripts"]:
                source = (out / name).read_text()
                self.assertNotIn(str(root), source)
                self.assertIn("--use-file-for-fake-audio-capture=", source)
                self.assertIn("chrome_args(LAUNCH_CONFIG, PROJECT_ASSETS)", source)
                ast.parse(source)

    def test_special_navigation_urls_remain_absolute(self):
        self.assertEqual(codegen.url_expr("about:blank"), '"about:blank"')
        self.assertEqual(codegen.url_expr("data:text/plain,hi"), '"data:text/plain,hi"')

    def test_imported_metadata_cannot_insert_python_statements(self):
        case = self.case([])
        case["id"] = '../../escape\n    raise RuntimeError("injected")\\'
        case["title"] = 'Title\r    raise RuntimeError("injected")'
        for engine in ("playwright", "selenium"):
            with self.subTest(engine=engine):
                tree = ast.parse(codegen.script([case], "http://example.test", engine))
                generated = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name.startswith("case_")]
                self.assertEqual(len(generated), 1)
                self.assertTrue(all(isinstance(node, ast.Expr) for node in generated[0].body))

    def test_nonportable_revision_removes_previous_scripts(self):
        case = self.case([{"action": "native_step", "command": "state", "args": ["load", "saved.json"],
                           "reason": "No portable state import"}])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            out = root / "scripts"
            out.mkdir()
            for engine in ("playwright", "selenium"):
                (out / ("tc-export_" + engine + ".py")).write_text('print("stale PASS")')
            with patch.object(codegen.projects, "cases", return_value=[case]), \
                    patch.object(codegen.projects, "config", return_value={"base_url": "http://example.test"}), \
                    patch.object(codegen.projects, "path", return_value=root), \
                    patch.object(codegen.runner, "build_plan", return_value=[(case["id"], "selected")]):
                result = codegen.export("project", str(out))
            self.assertFalse(result["scripts"])
            self.assertEqual(len(result["notes"]), 2)
            self.assertFalse(list(out.glob("*.py")))

    def test_api_prerequisite_blocks_incomplete_ui_export(self):
        case = self.case([])
        api = {"id": "TC-API", "kind": "api"}
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            for engine in ("playwright", "selenium"):
                (out / ("tc-export_" + engine + ".py")).write_text('print("stale PASS")')
            with patch.object(codegen.projects, "cases", return_value=[api, case]), \
                    patch.object(codegen.projects, "config", return_value={"base_url": "http://example.test"}), \
                    patch.object(codegen.projects, "path", return_value=out), \
                    patch.object(codegen.runner, "build_plan", return_value=[("TC-API", "prerequisite"), (case["id"], "selected")]):
                result = codegen.export("project", str(out))
            self.assertFalse(result["scripts"])
            self.assertIn("TC-API", result["notes"][0])
            self.assertFalse(list(out.glob("*.py")))

    def selenium_runtime(self):
        """Load the actual exported classes against recording WebDriver boundaries."""
        tree = ast.parse(codegen.script([self.case([])], "http://example.test", "selenium"))
        exported = [node for node in tree.body if isinstance(node, ast.ClassDef)
                    and node.name in ("Standalone", "StandaloneSelenium")]
        keys = SimpleNamespace(**{name: "KEY_" + name for name in (
            "ALT", "CONTROL", "META", "SHIFT", "ENTER", "ESCAPE", "TAB", "BACKSPACE", "DELETE", "SPACE",
            "ARROW_UP", "ARROW_DOWN", "ARROW_LEFT", "ARROW_RIGHT", "HOME", "END", "PAGE_UP", "PAGE_DOWN", "INSERT",
            *["F" + str(number) for number in range(1, 13)])})

        class Driver:
            def __init__(self):
                self.events, self.actions, self.held, self.reject_actions = [], [], set(), False

            def execute_cdp_cmd(self, method, params):
                self.events.append((method, dict(params)))

        class Actions:
            def __init__(self, driver):
                self.driver, self.pending = driver, []

            def key_down(self, key):
                self.pending.append(("down", key))
                return self

            def key_up(self, key):
                self.pending.append(("up", key))
                return self

            def send_keys(self, key):
                self.pending.append(("send", key))
                return self

            def perform(self):
                if self.driver.reject_actions:
                    raise RuntimeError("WebDriver rejected the key action")
                for operation, key in self.pending:
                    self.driver.actions.append((operation, key))
                    if operation == "down":
                        self.driver.held.add(key)
                    else:
                        # ActionChains.send_keys issues down and up for each key.
                        self.driver.held.discard(key)

        class Events:
            def __init__(self, *args):
                pass

            def check(self):
                pass

        namespace = {"re": re, "Keys": keys, "ActionChains": Actions, "SeleniumDialogs": Events,
                     "SeleniumCommands": lambda *args: None, "permissions": lambda config: [],
                     "LAUNCH_CONFIG": {}, "TIMEOUT": 5}
        exec(compile(ast.Module(body=exported, type_ignores=[]), "generated_selenium_runtime", "exec"), namespace)
        driver = Driver()
        return namespace["StandaloneSelenium"](driver, None), driver, keys

    def test_exported_selenium_mouse_receives_held_modifier_mask(self):
        runtime, driver, _ = self.selenium_runtime()
        for key in ("Shift", "Ctrl"):
            runtime.perform({"action": "keyboard", "operation": "down", "value": key})
        for operation in ("move", "down", "wheel", "up"):
            runtime.perform({"action": "mouse", "operation": operation, "x": 12.5, "y": 2,
                             "delta_x": 0, "delta_y": 0})
        runtime.perform({"action": "keyboard", "operation": "up", "value": "ControlOrMeta"})
        runtime.perform({"action": "mouse", "operation": "move", "x": 1, "y": 2})
        runtime.perform({"action": "keyboard", "operation": "up", "value": "Shift"})
        runtime.perform({"action": "mouse", "operation": "down"})
        mouse = [params for method, params in driver.events if method == "Input.dispatchMouseEvent"]
        self.assertEqual([event["modifiers"] for event in mouse], [10, 10, 10, 10, 8, 0])
        self.assertEqual([event["buttons"] for event in mouse[:4]], [0, 1, 1, 0])
        self.assertEqual((mouse[2]["deltaX"], mouse[2]["deltaY"]), (0, 0))

    def test_exported_press_chord_preserves_previously_held_modifiers(self):
        runtime, driver, keys = self.selenium_runtime()
        runtime.perform({"action": "keyboard", "operation": "down", "value": "Shift"})
        runtime.perform({"action": "keyboard", "operation": "press", "value": "Shift+ArrowLeft"})
        runtime.perform({"action": "mouse", "operation": "down"})
        self.assertEqual(driver.events[-1][1]["modifiers"], 8)
        self.assertIn(keys.SHIFT, driver.held)
        self.assertNotIn(("up", keys.SHIFT), driver.actions)
        runtime.perform({"action": "keyboard", "operation": "up", "value": "Shift"})
        runtime.perform({"action": "press", "key": "Ctrl+a"})
        runtime.perform({"action": "mouse", "operation": "up"})
        self.assertEqual(driver.events[-1][1]["modifiers"], 0)
        self.assertFalse(driver.held)

    def test_exported_modifier_aliases_literal_plus_and_failed_actions(self):
        runtime, driver, keys = self.selenium_runtime()
        for key in ("Command", "Alt"):
            runtime.perform({"action": "keyboard", "operation": "down", "value": key})
        runtime.perform({"action": "mouse", "operation": "move", "x": 0, "y": 0})
        self.assertEqual(driver.events[-1][1]["modifiers"], 5)
        runtime.perform({"action": "keyboard", "operation": "up", "value": "Cmd"})
        runtime.perform({"action": "keyboard", "operation": "up", "value": "Alt"})
        runtime.perform({"action": "keyboard", "operation": "press", "value": "+"})
        self.assertEqual(driver.actions[-1], ("send", "+"))
        self.assertEqual(runtime.key("Esc"), keys.ESCAPE)
        self.assertEqual(runtime.key("F12"), keys.F12)
        driver.reject_actions = True
        with self.assertRaises(RuntimeError):
            runtime.perform({"action": "keyboard", "operation": "down", "value": "Control"})
        driver.reject_actions = False
        runtime.perform({"action": "mouse", "operation": "move", "x": 0, "y": 0})
        self.assertEqual(driver.events[-1][1]["modifiers"], 0)

    def test_exported_terminal_modifier_press_clears_held_state(self):
        runtime, driver, _ = self.selenium_runtime()
        runtime.perform({"action": "keyboard", "operation": "down", "value": "Ctrl"})
        runtime.perform({"action": "keyboard", "operation": "press", "value": "Control"})
        runtime.perform({"action": "mouse", "operation": "move", "x": 0, "y": 0})
        self.assertEqual(driver.events[-1][1]["modifiers"], 0)
        self.assertFalse(driver.held)

    def playwright_input_runtime(self, blocked=False):
        tree = ast.parse(codegen.script([self.case([])], "http://example.test", "playwright"))
        exported = [node for node in tree.body if isinstance(node, ast.ClassDef)
                    and node.name in ("Standalone", "StandalonePlaywright")]
        namespace = {"input": lambda message: "captcha-value"}
        exec(compile(ast.Module(body=exported, type_ignores=[]), "generated_playwright_runtime", "exec"), namespace)
        calls = []

        class Node:
            def click(self, **options):
                calls.append(("trial", options))
                if blocked:
                    raise TimeoutError("A modal still intercepts input")

            def fill(self, value):
                calls.append(("fill", value))

            def press_sequentially(self, value):
                calls.append(("type", value))

            def select_option(self, **options):
                calls.append(("select", options))

            def focus(self):
                calls.append(("focus", None))

            def press(self, key):
                calls.append(("press", key))

        runtime = namespace["StandalonePlaywright"].__new__(namespace["StandalonePlaywright"])
        runtime.timeout = 7
        node = Node()
        runtime.choose = lambda candidates: candidates[0]
        runtime.one = lambda locator: node
        return runtime, calls

    def test_exported_playwright_input_waits_for_actionability_before_one_mutation(self):
        for operation in ("fill", "type", "select", "focus", "press", "captcha"):
            with self.subTest(operation=operation):
                runtime, calls = self.playwright_input_runtime()
                runtime.perform({"action": operation, "locators": [{"by": "css", "value": "#input"}],
                                 "value": "text", "key": "Enter"})
                self.assertEqual(calls[0], ("trial", {"trial": True, "timeout": 7000}))
                self.assertEqual(len(calls), 2)
                self.assertEqual(calls[1][0], "fill" if operation == "captcha" else operation)

    def test_exported_playwright_modal_timeout_never_dispatches_input(self):
        for operation in ("fill", "type", "select", "focus", "press", "captcha"):
            with self.subTest(operation=operation):
                runtime, calls = self.playwright_input_runtime(blocked=True)
                with self.assertRaises(TimeoutError):
                    runtime.perform({"action": operation, "locators": [{"by": "css", "value": "#input"}],
                                     "value": "text", "key": "Enter"})
                self.assertEqual(calls, [("trial", {"trial": True, "timeout": 7000})])


if __name__ == "__main__":
    unittest.main(verbosity=2)
