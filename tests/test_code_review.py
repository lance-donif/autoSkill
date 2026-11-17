"""Review tooling reports missing inputs, failed plugins and failed subprocesses honestly."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import code_review
import validate_review
from review_rules import repository


class ReviewToolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "source"
        self.root.mkdir()
        (self.root / "one.py").write_text("alpha  \nbeta\nalpha\n", encoding="utf-8")
        (self.root / "reports").mkdir()
        (self.root / "reports/capture.txt").write_text("alpha-private", encoding="utf-8")
        (self.root / ".env").write_text("alpha-private", encoding="utf-8")
        self.rules = self.root / "scripts/review_rules"
        self.rules.mkdir(parents=True)

    def invoke(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = code_review.main(["--root", str(self.root), *args])
        return code, json.loads(output.getvalue())

    def test_without_git_or_rg_search_remains_scoped_and_preserves_lines(self):
        with patch.object(code_review.shutil, "which", return_value=None):
            result = code_review.search(self.root, "alpha", [], 1)
            self.assertEqual(result, {"engine": "python", "total": 2, "matches": [{"path": "one.py", "line": 1, "text": "alpha  "}]})
            self.assertEqual(code_review.inventory(self.root, ["*.py"]), ["one.py"])

    def test_rg_and_python_have_same_line_results_including_hidden_source(self):
        if not shutil.which("rg"):
            self.skipTest("rg is optional; fallback behavior is tested separately")
        hidden = self.root / ".github"
        hidden.mkdir()
        (hidden / "ci.yml").write_text("alpha\n", encoding="utf-8")
        actual = code_review.search(self.root, "alpha", [], 10)
        with patch.object(code_review.shutil, "which", return_value=None):
            expected = code_review.search(self.root, "alpha", [], 10)
        self.assertEqual(actual["matches"], expected["matches"])
        self.assertEqual(actual["total"], 3)

    def test_no_matches_are_valid_but_invalid_pattern_is_failure(self):
        code, result = self.invoke("search", "no-such-value")
        self.assertEqual((code, result["total"]), (0, 0))
        for executable in (None, shutil.which("rg")):
            with self.subTest(executable=executable), patch.object(code_review.shutil, "which", side_effect=lambda name, executable=executable: executable if name == "rg" else None):
                code, result = self.invoke("search", "[")
                self.assertEqual(code, 2)
                self.assertFalse(result["ok"])

    def test_view_rejects_escape_symlink_and_invalid_ranges(self):
        private = self.root.parent / "private.txt"
        private.write_text("never-read", encoding="utf-8")
        for name in ("../private.txt", str(private)):
            code, result = self.invoke("view", name)
            self.assertEqual(code, 2)
            self.assertNotIn("never-read", json.dumps(result))
        if os.name == "posix":
            (self.root / "link.txt").symlink_to(private)
            self.assertEqual(self.invoke("view", "link.txt")[0], 2)
            self.assertNotIn("link.txt", code_review.inventory(self.root, []))
        self.assertEqual(self.invoke("view", "one.py", "--start", "0")[0], 2)
        self.assertEqual(self.invoke("files", "--limit", "-1")[0], 2)
        self.assertEqual(self.invoke("view", "absent.py")[0], 2)

    def test_module_exception_and_invalid_findings_do_not_hide_other_rules(self):
        (self.rules / "broken.py").write_text("def run(root):\n    raise RuntimeError('fixture-error')\n", encoding="utf-8")
        (self.rules / "malformed.py").write_text("def run(root):\n    return [{'severity': 'error'}]\n", encoding="utf-8")
        (self.rules / "good.py").write_text(
            "def run(root):\n    print('diagnostic must not corrupt JSON')\n"
            "    return [{'rule': 'good', 'severity': 'error', 'path': 'one.py', 'line': 2, 'message': 'fixture finding'}]\n", encoding="utf-8")
        code, result = self.invoke("check")
        self.assertEqual(code, 2)
        self.assertEqual([item["rule"] for item in result["rule_errors"]], ["broken", "malformed"])
        self.assertEqual(len(result["findings"]), 1)
        self.assertFalse(result["ok"])
        self.assertEqual(self.invoke("check", "--rule", "good")[0], 1)
        self.assertEqual(self.invoke("check", "--rule", "missing")[0], 2)

    def test_system_exit_is_a_rule_failure_and_warning_is_not(self):
        (self.rules / "exit.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
        (self.rules / "warning.py").write_text(
            "def run(root):\n    return [{'rule': 'warning', 'severity': 'warning', 'path': 'one.py', 'line': 1, 'message': 'note'}]\n", encoding="utf-8")
        self.assertEqual(self.invoke("check", "--rule", "exit")[0], 2)
        self.assertEqual(self.invoke("check", "--rule", "warning")[0], 0)


class RepositoryRuleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.files = {
            "package.json": '{"version":"1.2.3","exports":{".":"./plugin/index.mjs"}}',
            "plugin/index.mjs": "import { value } from './support.mjs';\n",
            "plugin/support.mjs": "export const value = 1;\n",
            "skills/autoskill/app/autoskill/__init__.py": '__version__ = "1.2.3"\n',
            "skills/autoskill/app/autoskill/service.py": 'from . import support\n',
            "skills/autoskill/app/autoskill/support.py": 'VALUE = 1\n',
            ".github/workflows/lint.yml": 'run: python tests/test_contract.py\nnames = {"httpx"}\n',
            "tests/test_contract.py": '"""Contract fixture."""\n',
            "requirements.txt": 'httpx[socks]==0.28.1\n',
            "requirements-linux.txt": '',
        }
        for name, source in self.files.items():
            self.write(name, source)

    def write(self, name, source):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")

    def test_complete_source_tree_passes_without_importing_product(self):
        self.assertEqual(repository.run(self.root), [])

    def test_unsubmitted_local_modules_and_package_version_mismatch_fail(self):
        (self.root / "plugin/support.mjs").unlink()
        (self.root / "skills/autoskill/app/autoskill/support.py").unlink()
        self.write("package.json", '{"version":"9.9.9","exports":{".":"./plugin/index.mjs"}}')
        findings = repository.run(self.root)
        self.assertEqual({item["path"] for item in findings}, {"plugin/index.mjs", "skills/autoskill/app/autoskill/service.py", "skills/autoskill/app/autoskill/__init__.py"})
        self.assertTrue(all(item["line"] >= 1 for item in findings))

    def test_ci_references_and_unique_project_pins_are_checked(self):
        (self.root / "tests/test_contract.py").unlink()
        self.write("requirements-linux.txt", 'httpx[socks]==0.28.1\n')
        findings = repository.run(self.root)
        self.assertEqual(len(findings), 2)
        self.assertTrue(all(item["path"] == ".github/workflows/lint.yml" for item in findings))

    def test_named_ci_suite_must_resolve_to_present_tests(self):
        self.write("scripts/validate_review.py", 'PYTHON_SUITES = {"workflow": ["test_missing.py"]}\nNODE_SUITES = {}\n')
        self.write(".github/workflows/lint.yml", 'run: python scripts/validate_review.py --suite workflow --suite nonexistent\n')
        findings = repository.run(self.root)
        self.assertEqual(len(findings), 2)
        self.assertTrue(any("test_missing.py" in item["message"] for item in findings))
        self.assertTrue(any("nonexistent" in item["message"] for item in findings))


class ValidationRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "source with spaces"
        (self.root / "tests").mkdir(parents=True)
        self.output = Path(self.temporary.name) / "outside-output"

    def test_selected_tests_run_outside_checkout_and_keep_failures(self):
        (self.root / "tests/test_failed.py").write_text(
            "import unittest\nclass Failed(unittest.TestCase):\n    def test_failure(self):\n        self.assertEqual(1, 2)\n", encoding="utf-8")
        (self.root / "tests/test_ok.py").write_text(
            "import unittest\nclass OK(unittest.TestCase):\n    def test_success(self):\n        self.assertTrue(True)\n", encoding="utf-8")
        commands = validate_review.build_commands(self.root, [], ["test_failed.py", "test_ok.py"], [], sys.executable)
        result = validate_review.run_commands(self.root, commands, self.output, 15, sys.executable)
        self.assertEqual([item["exit_code"] for item in result["results"]], [1, 0])
        self.assertEqual(result["exit_code"], 1)
        self.assertFalse(result["ok"])
        self.assertTrue((self.output / "summary.json").is_file())
        self.assertIn("AssertionError", Path(result["results"][0]["log"]).read_text())
        self.assertFalse((self.root / "tests/__pycache__").exists())
        with self.assertRaises(ValueError):
            validate_review.run_commands(self.root, commands, self.output, 15, sys.executable)

    def test_cli_show_logs_keeps_full_success_and_failure_output_and_exit_code(self):
        for name, check in (("success", "self.assertTrue(True)"), ("failure", "self.fail('fixture failure')")):
            source = ("import sys, unittest\nclass Fixture(unittest.TestCase):\n    def test_result(self):\n"
                      f"        print('{name} stdout first\\n{name} stdout last')\n"
                      f"        print('{name} stderr first\\n{name} stderr last', file=sys.stderr)\n"
                      f"        {check}\n")
            self.root.joinpath(f"tests/test_{name}.py").write_text(source, encoding="utf-8")
        summaries = []
        for show_logs in (False, True):
            with self.subTest(show_logs=show_logs):
                output = self.output / ("shown" if show_logs else "json-only")
                command = [sys.executable, str(ROOT / "scripts/validate_review.py"), "--root", str(self.root),
                           "--python-test", "test_success.py", "--python-test", "test_failure.py", "--output-dir", str(output)]
                completed = subprocess.run([*command, *(["--show-logs"] if show_logs else [])],
                                           env={**os.environ, "GITHUB_ACTIONS": "true"},
                                           capture_output=True, text=True, timeout=30, shell=False, check=False)
                self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
                stored_summary = (output / "summary.json").read_text(encoding="utf-8")
                summary = json.loads(stored_summary)
                summaries.append([item["exit_code"] for item in summary["results"]])
                if not show_logs:
                    self.assertEqual(json.loads(completed.stdout), summary)
                    self.assertNotIn("success stdout first", completed.stdout)
                    self.assertNotIn("failure stderr last", completed.stdout)
                    continue
                self.assertTrue(completed.stdout.endswith(stored_summary))
                for result in summary["results"]:
                    self.assertIn(Path(result["log"]).read_text(encoding="utf-8"), completed.stdout)
                self.assertLess(completed.stdout.index("success stdout first"), completed.stdout.index("failure stdout first"))
                self.assertIn("success stderr last", completed.stdout)
                self.assertIn("failure stderr last", completed.stdout)
                self.assertIn("AssertionError: fixture failure", completed.stdout)
                annotations = [line for line in completed.stdout.splitlines() if line.startswith("::error::")]
                self.assertEqual(len(annotations), 1)
                self.assertIn("python-test_failure.py", annotations[0])
                self.assertIn("AssertionError: fixture failure", annotations[0])
        self.assertEqual(summaries, [[0, 1], [0, 1]])

    def test_github_annotation_escapes_failed_check_and_limits_tail_preserving_exit_codes(self):
        diagnostic = "Traceback: 100% failed\r\nexpected %0A literal\nAssertionError: final failure\n"
        commands = [{"name": "failed%check", "argv": [sys.executable, "-c",
                     f"import sys; sys.stdout.buffer.write({('omitted-prefix' + 'x' * 12000 + diagnostic).encode()!r}); sys.exit(7)"]},
                    {"name": "successful-check", "argv": [sys.executable, "-c", "print('SUCCESS')"]}]
        output = io.StringIO()
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}), contextlib.redirect_stdout(output):
            result = validate_review.run_commands(self.root, commands, self.output, 15, sys.executable, show_logs=True)
        annotations = [line for line in output.getvalue().splitlines() if line.startswith("::error::")]
        self.assertEqual(len(annotations), 1)
        self.assertTrue(annotations[0].startswith("::error::检查失败：failed%25check（退出码 7，跳过 0）%0A"))
        self.assertTrue(annotations[0].endswith("Traceback: 100%25 failed%0D%0Aexpected %250A literal%0AAssertionError: final failure%0A"))
        self.assertNotIn("omitted-prefix", annotations[0])
        self.assertNotIn("successful-check", annotations[0])
        self.assertLess(len(annotations[0].encode("utf-8")), 3000)
        self.assertEqual(result["exit_code"], 7)
        self.assertEqual([item["exit_code"] for item in result["results"]], [7, 0])
        self.assertEqual(json.loads((self.output / "summary.json").read_text(encoding="utf-8")), result)
        self.assertTrue(Path(result["results"][0]["log"]).read_bytes().endswith(diagnostic.encode()))

    def test_github_annotations_require_explicit_logs_and_actions(self):
        commands = [{"name": "failed", "argv": [sys.executable, "-c", "print('FAILURE'); raise SystemExit(3)"]}]
        for show_logs, actions in ((False, "true"), (True, "false"), (True, "")):
            with self.subTest(show_logs=show_logs, actions=actions):
                output = io.StringIO()
                with patch.dict(os.environ, {"GITHUB_ACTIONS": actions}), contextlib.redirect_stdout(output):
                    result = validate_review.run_commands(self.root, commands, self.output / f"{show_logs}-{actions}",
                                                          15, sys.executable, show_logs=show_logs)
                self.assertNotIn("::error::", output.getvalue())
                self.assertEqual(result["exit_code"], 3)

    def test_show_logs_includes_failed_version_probe_and_continues(self):
        commands = [{"name": "version-failed", "argv": [sys.executable, "-c", "print('MUST_NOT_EXECUTE')"],
                     "version_argv": [sys.executable, "-c", "print('VERSION_PROBE_FAILED'); raise SystemExit(7)"]},
                    {"name": "next-check", "argv": [sys.executable, "-c", "print('NEXT_CHECK_PASSED')"]}]
        output = io.StringIO()
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}), contextlib.redirect_stdout(output):
            result = validate_review.run_commands(self.root, commands, self.output, 15, sys.executable, show_logs=True)
        annotations = [line for line in output.getvalue().splitlines() if line.startswith("::error::")]
        self.assertEqual(len(annotations), 1)
        self.assertIn("version-failed", annotations[0])
        self.assertIn("VERSION_PROBE_FAILED", annotations[0])
        self.assertEqual(result["exit_code"], 7)
        self.assertEqual([item["exit_code"] for item in result["results"]], [7, 0])
        self.assertNotIn("MUST_NOT_EXECUTE", output.getvalue())
        for item in result["results"]:
            self.assertIn(Path(item["log"]).read_text(encoding="utf-8"), output.getvalue())
        self.assertLess(output.getvalue().index("VERSION_PROBE_FAILED"), output.getvalue().index("NEXT_CHECK_PASSED"))

    def test_empty_and_missing_test_selection_cannot_pass(self):
        (self.root / "tests/test_empty.py").write_text('"""No tests here."""\n', encoding="utf-8")
        commands = validate_review.build_commands(self.root, [], ["test_empty.py", "test_absent.py"], [], sys.executable)
        result = validate_review.run_commands(self.root, commands, self.output, 15, sys.executable)
        self.assertEqual([item["exit_code"] for item in result["results"]], [1, 1])
        self.assertFalse(result["ok"])

    def test_missing_tool_and_timeout_are_retained_failures(self):
        commands = [{"name": "missing", "argv": [], "error": "not installed"},
                    {"name": "timeout", "argv": [sys.executable, "-c", "import time; time.sleep(30)"]},
                    {"name": "exit-seven", "argv": [sys.executable, "-c", "raise SystemExit(7)"]}]
        result = validate_review.run_commands(self.root, commands, self.output, 0.15, sys.executable)
        self.assertEqual([item["exit_code"] for item in result["results"]], [127, 124, 7])
        self.assertEqual(result["exit_code"], 127)

    def test_skipped_python_tests_are_visible_and_can_fail_acceptance(self):
        self.root.joinpath("tests/test_skipped.py").write_text(
            "import unittest\nclass Skipped(unittest.TestCase):\n    @unittest.skip('runtime absent')\n"
            "    def test_not_executed(self):\n        self.fail('must not claim execution')\n", encoding="utf-8")
        commands = validate_review.build_commands(self.root, [], ["test_skipped.py"], [], sys.executable)
        output = io.StringIO()
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}), contextlib.redirect_stdout(output):
            result = validate_review.run_commands(self.root, commands, self.output, 15, sys.executable, fail_on_skip=True, show_logs=True)
        annotations = [line for line in output.getvalue().splitlines() if line.startswith("::error::")]
        self.assertEqual(len(annotations), 1)
        self.assertIn("跳过 1", annotations[0])
        self.assertIn("runtime absent", annotations[0])
        self.assertEqual(result["results"][0]["exit_code"], 0)
        self.assertEqual(result["results"][0]["skipped"], 1)
        self.assertFalse(result["results"][0]["coverage_ok"])
        self.assertEqual(result["exit_code"], 1)
        self.assertFalse(result["ok"])

    def test_node_skip_summary_is_not_lost_behind_successful_exit(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is optional for the Python-only tooling suite")
        self.root.joinpath("tests/skipped.test.mjs").write_text(
            "import test from 'node:test';\ntest.skip('SDK absent', () => {});\n", encoding="utf-8")
        commands = validate_review.build_commands(self.root, [], [], ["tests/skipped.test.mjs"], sys.executable, node=node)
        result = validate_review.run_commands(self.root, commands, self.output, 15, sys.executable, fail_on_skip=True)
        self.assertEqual(result["results"][0]["exit_code"], 0)
        self.assertEqual(result["results"][0]["skipped"], 1)
        self.assertFalse(result["ok"])

    def test_no_implicit_install_or_suite_and_runtime_state_is_isolated(self):
        with patch.object(validate_review.shutil, "which", return_value=None):
            command = validate_review.build_commands(self.root, ["biome"], [], [], sys.executable)[0]
        self.assertIn("error", command)
        self.assertEqual(command["argv"], [])
        with patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": "/do-not-read", "AUTOSKILL_SESSION": "private-session"}):
            env = validate_review.validation_environment(self.output, sys.executable)
        self.assertEqual(env["AUTOSKILL_WORKSPACE"], str(self.output / "workspace"))
        self.assertNotIn("AUTOSKILL_SESSION", env)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = validate_review.main(["--root", str(self.root), "--output-dir", str(self.output)])
        self.assertEqual(result, 2)
        self.assertFalse(json.loads(output.getvalue())["ok"])


if __name__ == "__main__":
    unittest.main()
