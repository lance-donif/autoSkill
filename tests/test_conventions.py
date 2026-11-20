"""Project conventions a general linter cannot see. Standard library only, so it runs anywhere."""
import argparse
import io
import re
import sys
import tempfile
import tokenize
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "skills/autoskill/app"
UI = APP / "ui"
sys.path.insert(0, str(APP))
sys.path.insert(0, str(ROOT / "scripts"))

from review_rules import compact_navigation

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


class CompactNavigationTests(unittest.TestCase):
    before = """.side { height: 100vh; }
:root[data-embed="true"] .shell {
  grid-template: auto 1fr / minmax(0, 1fr);
}
:root[data-embed="true"] .side { height: auto; }
@media (max-width: 1100px) {
  .shell {
    grid-template: auto 1fr / minmax(0, 1fr);
  }
  .side { height: auto; /* min-height: 49px; */ }
  .side-panel { min-height: 49px; }
}
"""

    def test_live_dashboard_and_pages_entries(self):
        findings = compact_navigation.run(ROOT)
        self.assertFalse(findings, "\n".join(f"{f['path']}:{f['line']}: {f['message']}" for f in findings))

    def test_unreserved_rows_point_to_their_declarations(self):
        sheets = [("base.css", self.before)]
        self.assertEqual(compact_navigation.unreserved_rows(sheets), [("base.css", 3), ("base.css", 8)])
        scoped = "@media (max-width: 1100px) { .side { block-size: 3rem; } }"
        self.assertEqual(compact_navigation.unreserved_rows([*sheets, ("phone.css", scoped)]), [("base.css", 3)])
        for invalid in ("0px", "auto", "min-content"):
            with self.subTest(invalid=invalid):
                self.assertEqual(compact_navigation.unreserved_rows([*sheets, ("override.css", f".side {{ min-height: {invalid}; }}")]),
                                 [("base.css", 3), ("base.css", 8)])

    def test_explicit_side_sizes_and_reserved_grid_tracks(self):
        for source in (self.before + ".side { min-block-size: calc(2rem + 1px); }",
                       self.before + "#side { height: 49px; }",
                       self.before.replace("auto 1fr /", "minmax(3rem, auto) 1fr /"),
                       self.before.replace("auto 1fr /", "49px 1fr /")):
            with self.subTest(source=source):
                self.assertEqual(compact_navigation.unreserved_rows([("base.css", source)]), [])

    def test_matching_ancestors_specificity_order_and_important(self):
        sheets = [("base.css", self.before)]
        bad = [("base.css", 3), ("base.css", 8)]
        alternatives = (
            ("#app > #side { min-block-size: 49px; }", []),
            ("aside.side { min-block-size: 49px; }", []),
            (".side { min-block-size: 49px !important; }", []),
            ("#app > aside.side:empty { min-block-size: 49px; }", []),
            ("aside.side { min-height: 49px; } .side { min-height: 0; }", []),
            (".side { min-height: 49px !important; } #side { min-height: 0; }", []),
            ("#other > #side { min-height: 49px; } div.side { min-height: 49px; }", bad),
            (".side { min-height: 49px; } .side { min-height: 0; }", bad),
            (".side { min-height: 0 !important; } #app > #side { min-height: 49px; }", bad),
        )
        for css, expected in alternatives:
            with self.subTest(css=css):
                self.assertEqual(compact_navigation.unreserved_rows([*sheets, ("overrides.css", css)]), expected)

    def test_only_entries_with_an_empty_navigation_slot_require_the_css_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ui = root / compact_navigation.UI
            ui.mkdir(parents=True)
            stylesheet = ui / "shell.css"
            stylesheet.write_text(self.before, encoding="utf-8")
            html = '<link rel="stylesheet" href="/shell.css"><div id="app" class="shell"><aside id="side" class="side"><button>Menu</button></aside></div>'
            (ui / "index.html").write_text(html, encoding="utf-8")
            self.assertEqual(compact_navigation.run(root), [])
            preview = root / "preview"
            preview.mkdir()
            entry = preview / "cases.html"
            empty = html.replace('href="/shell.css"', f'href="../{stylesheet.relative_to(root).as_posix()}"').replace("<button>Menu</button>", "<!-- nav -->")
            entry.write_text(empty, encoding="utf-8")
            self.assertEqual([(f["path"], f["line"]) for f in compact_navigation.run(root)],
                             [(stylesheet.relative_to(root).as_posix(), line) for line in (3, 8)])
            (preview / "override.css").write_text(".side { min-height: 49px; }", encoding="utf-8")
            entry.write_text(empty + '<link rel="stylesheet" href="override.css">', encoding="utf-8")
            self.assertEqual(compact_navigation.run(root), [])
            entry.write_text(empty + '<link rel="stylesheet" href="override.css" media="print">', encoding="utf-8")
            self.assertEqual([f["line"] for f in compact_navigation.run(root)], [3, 8])
            entry.write_text(empty + '<style>aside.side { min-height: 49px; }</style>', encoding="utf-8")
            self.assertEqual(compact_navigation.run(root), [])
            entry.write_text(empty.replace('id="side"', 'id="side" style="min-height:49px"'), encoding="utf-8")
            self.assertEqual(compact_navigation.run(root), [])


if __name__ == "__main__":
    unittest.main()
