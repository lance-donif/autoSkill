"""Project conventions a general linter cannot see. Standard library only, so it runs anywhere."""
import argparse
import io
import re
import sys
import tokenize
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "skills/autoskill/app"
UI = APP / "ui"
sys.path.insert(0, str(APP))

from autoskill import cli
from autoskill.replay import codegen, ir

COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(")
CSS_VALUE = re.compile(r":\s*([^;{}]+)[;}]")
CJK = re.compile(r"[一-鿿]")
LOCATOR = {"by": "css", "value": "#a"}
SAMPLE = {"url": "/x", "tab": "t1", "active": "t1", "tabs": {"active": "t1", "opened": [], "closed": []}, "value": "v",
          "key": "Enter", "image": [LOCATOR], "expected": 1, "attribute": "class"}


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def sample(kind: str, table: dict, key: str) -> dict:
    needs_target, required = table[kind]
    step = {key: kind, **{field: SAMPLE[field] for field in required}}
    if needs_target:
        step["locators"] = [LOCATOR]
    return step


class ConventionTests(unittest.TestCase):
    def test_colours_are_defined_in_tokens_css_only(self):
        for sheet in sorted((UI / "css").glob("*.css")):
            if sheet.name != "tokens.css":
                found = [value.strip() for value in CSS_VALUE.findall(read(sheet)) if COLOUR.search(value)]
                self.assertEqual(found, [], f"{sheet.name}: use a var(--token) from tokens.css instead of a colour literal")
        for script in sorted((UI / "js").rglob("*.js")):
            self.assertIsNone(re.search(r"""['"]#[0-9a-fA-F]{3,8}['"]|\brgba?\(""", read(script)), f"{script.name}: colour literal")

    def test_every_css_variable_in_use_is_defined(self):
        defined = set(re.findall(r"(--[a-z0-9-]+)\s*:", read(UI / "css/tokens.css")))
        for file in [*sorted((UI / "css").glob("*.css")), *sorted((UI / "js").rglob("*.js"))]:
            unknown = set(re.findall(r"var\((--[a-z0-9-]+)", read(file))) - defined
            self.assertEqual(unknown, set(), f"{file.name} uses tokens that tokens.css does not define")

    def test_every_step_kind_is_named_and_reaches_both_exported_scripts(self):
        for table, names, key in ((ir.ACTIONS, ir.ACTION_NAMES, "action"), (ir.ASSERTIONS, ir.ASSERTION_NAMES, "assertion")):
            for kind in table:
                with self.subTest(kind=kind):
                    self.assertIn(kind, names)
                    step = sample(kind, table, key)
                    self.assertTrue(ir.describe(step))
                    for build in (codegen.pw_step, codegen.se_step):
                        compile(build(step), kind, "exec")

    def test_every_command_is_documented_for_the_agent(self):
        commands = next(a for a in cli.parser()._actions if isinstance(a, argparse._SubParsersAction)).choices
        docs = read(ROOT / "skills/autoskill/SKILL.md") + read(ROOT / "skills/autoskill/references/commands.md")
        missing = [name for name in commands if f"`{name}" not in docs and f"autoskill {name}" not in docs]
        self.assertEqual(missing, [], "add these commands to SKILL.md or references/commands.md")

    def test_product_rules_are_not_reintroduced(self):
        scripts = "".join(read(file) for file in sorted((UI / "js").rglob("*.js")))
        self.assertNotIn("approvals", scripts)          # no risk approvals: the user's cases run as written
        self.assertNotIn("type: 'password'", scripts)   # credentials are shown as plain text
        self.assertNotIn("approvals", read(APP / "autoskill/replay/api.py"))

    def test_source_files_are_clean_text(self):
        for folder in (APP, ROOT / "plugin", ROOT / "scripts", ROOT / "tests"):
            for file in sorted(folder.rglob("*")):
                if file.suffix in (".py", ".js", ".mjs", ".css", ".html") and "__pycache__" not in file.parts:
                    text = file.read_bytes().decode("utf-8")
                    self.assertNotIn("\r", text, f"{file}: CRLF line endings")
                    self.assertNotIn("\t", text, f"{file}: tab character")
                    self.assertTrue(text.endswith("\n"), f"{file}: no final newline")
                    self.assertFalse([n for n, line in enumerate(text.split("\n"), 1) if line != line.rstrip()], f"{file}: trailing spaces")

    def test_code_comments_are_english(self):
        for file in sorted((APP / "autoskill").rglob("*.py")):
            for token in tokenize.generate_tokens(io.StringIO(read(file)).readline):
                if token.type == tokenize.COMMENT:
                    self.assertIsNone(CJK.search(token.string), f"{file.name}:{token.start[0]}: comments are written in English")


if __name__ == "__main__":
    unittest.main()
