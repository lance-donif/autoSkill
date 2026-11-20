"""Real CLI and bridge transfers preserve complete user data and historical path identity."""
import contextlib
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "skills/autoskill/app"
sys.path.insert(0, str(APP))

from autoskill import bridge, builder, cli, command_input, data_refs, db, projects


class DataCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "selected data"
        self.environment = {key: value for key, value in os.environ.items() if not key.startswith("AUTOSKILL_")}
        self.environment.update(PYTHONPATH=str(APP), AUTOSKILL_WORKSPACE=str(self.workspace), AUTOSKILL_BRIDGE="auto", PYTHONUTF8="1")

    def command(self, *argv, workspace=None, extra=None, code=0):
        env = {**self.environment, "AUTOSKILL_WORKSPACE": str(workspace or self.workspace), **(extra or {})}
        process = subprocess.run([sys.executable, "-B", "-m", "autoskill", *argv], cwd=self.root, env=env,
                                 capture_output=True, text=True, encoding="utf-8", timeout=40, check=False)
        self.assertEqual(process.returncode, code, process.stdout + process.stderr)
        return json.loads(process.stdout)

    def put(self, root, name, content):
        file = root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(content)
        return file

    def legacy(self, folder):
        with patch.dict(os.environ, {**self.environment, "AUTOSKILL_WORKSPACE": str(folder)}, clear=True):
            db.init()
            project = projects.create("合成迁移项目", "http://127.0.0.1:9", project_id="legacy")
            projects.save_cases(project["id"], [builder.api_case("TC-API-001", "保留原用例", "GET", "/", {"status": 200, "json_equals": {"/ok": True}})])
            db.insert("runs", {"id": "run-preserved", "project_id": "legacy", "engine": "http", "status": "Fail",
                               "case_ids": '["TC-API-001"]', "total": 1, "done": 1, "failed": 1, "created_at": "2026-11-13T00:00:00Z"})
        self.put(folder, ".local/credentials.json", b'{"SYNTHETIC":"fixture-only"}')
        self.put(folder, ".local/sessions/legacy.json", b'{"cookies":[{"name":"synthetic","value":"fixture"}]}')
        self.put(folder, "projects/legacy/knowledge/facts.md", "合成需求，不是用户数据".encode())
        evidence = self.put(folder, ".local/runs/run-preserved/evidence.bin", b"\x00\xfffrozen-evidence")
        self.put(folder, ".local/runs/run-preserved/test-report/receipt.json", json.dumps({"evidence": str(evidence)}).encode())
        return {file.relative_to(folder).as_posix(): hashlib.sha256(file.read_bytes()).hexdigest()
                for file in folder.rglob("*") if file.is_file() and not file.name.endswith(("-wal", "-shm", ".lock"))}

    def assert_legacy(self, folder, expected):
        for name, digest in expected.items():
            if name in (".local/autoskill.db", ".local/runtime-location.json"):
                continue
            self.assertEqual(hashlib.sha256((folder / name).read_bytes()).hexdigest(), digest, name)
        with sqlite3.connect(folder / ".local/autoskill.db") as connection:
            self.assertEqual(connection.execute("SELECT status,failed FROM runs WHERE id='run-preserved'").fetchone(), ("Fail", 1))
            self.assertEqual(connection.execute("SELECT case_id FROM cases WHERE project_id='legacy'").fetchone(), ("TC-API-001",))

    def test_light_cli_calls_and_embedded_parser_keep_their_import_contract(self):
        # A fresh process reveals which subsystems a read-only command actually imports.
        program = """
import contextlib, io, json, sys
from concurrent.futures import ThreadPoolExecutor
from importlib import import_module
from autoskill import cli

names = ('runner', 'context_service', 'task_state')
for _ in range(2):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        assert cli.main(['data', 'status']) == 0
    assert json.loads(output.getvalue())['data_id'] is None
assert not any('autoskill.' + name in sys.modules for name in names)
assert set(names) <= set(dir(cli))
assert not hasattr(cli, 'unknown_cli_attribute')
with ThreadPoolExecutor(max_workers=6) as workers:
    names = names * 4
    modules = list(workers.map(lambda name: getattr(cli, name), names))
assert all(module is import_module('autoskill.' + name) for name, module in zip(names, modules))
from autoskill.cli import *
assert (runner, context_service, task_state) == (cli.runner, cli.context_service, cli.task_state)
parser = cli.parser()
for engine in cli.runner.ENGINES:
    assert parser.parse_args(['run', 'example', '--engine', engine]).engine == engine
output = io.StringIO()
with contextlib.redirect_stdout(output):
    assert cli.main(['--session', 'example', 'run', 'project', '--engine', 'invalid']) == 2
assert json.loads(output.getvalue())['code'] == 'validation_error'
"""
        result = subprocess.run([sys.executable, "-B", "-c", program], env=self.environment, cwd=ROOT,
                                capture_output=True, text=True, timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_status_is_read_only_and_retired_upgrade_command_is_not_registered(self):
        status = self.command("data", "status")
        self.assertEqual(Path(status["root"]), self.workspace)
        self.assertFalse(self.workspace.exists())
        result = self.command("upgrade", "status", "old-journal.json", code=2)
        self.assertEqual(result["code"], "validation_error")
        self.assertFalse(self.workspace.exists())

    def test_data_io_failure_does_not_pollute_an_empty_retry_target(self):
        output, errors = io.StringIO(), io.StringIO()
        with (patch.dict(os.environ, self.environment, clear=True), patch.object(cli, "dispatch", side_effect=OSError("synthetic transfer I/O")),
              contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors)):
            result = cli.main(["data", "import", "old-data"])
        self.assertEqual(result, 2)
        self.assertIn("synthetic transfer I/O", json.loads(output.getvalue())["error"])
        self.assertFalse(self.workspace.exists())

    def test_default_target_import_and_complete_zip_roundtrip_keep_credentials_and_history(self):
        source = self.root / "old data"
        expected = self.legacy(source)
        original = {name: (source / name).read_bytes() for name in expected}
        imported = self.command("data", "import", source.name)
        self.assertEqual(Path(imported["root"]), self.workspace)
        self.assertTrue(imported["data_id"])
        self.assert_legacy(self.workspace, expected)
        exported = self.command("data", "export", "--out", "完整数据.zip")
        archive = self.root / "完整数据.zip"
        self.assertEqual(Path(exported["archive"]), archive)
        with zipfile.ZipFile(archive) as bundle:
            self.assertTrue(any(name.endswith(".local/credentials.json") for name in bundle.namelist()))
            self.assertTrue(any(name.endswith(".local/runs/run-preserved/evidence.bin") for name in bundle.namelist()))
            self.assertFalse(any(name.endswith("autoskill-upgrade.py") for name in bundle.namelist()))
        restored = self.root / "restored data"
        result = self.command("data", "import", archive.name, "--target", restored.name)
        self.assertEqual(Path(result["root"]), restored)
        self.assertTrue(result["requires_restart"])
        self.assert_legacy(restored, expected)
        for name, content in original.items():
            self.assertEqual((source / name).read_bytes(), content, name)
        receipt = json.loads((restored / ".local/runs/run-preserved/test-report/receipt.json").read_bytes())
        self.assertEqual(data_refs.resolve(receipt["evidence"], root=restored), restored / ".local/runs/run-preserved/evidence.bin")

    def test_bridge_legacy_attachment_uses_original_caller_root_not_transport_folder(self):
        caller = self.root / "caller"
        source = caller / "旧安装"
        legacy = source / "autoskill/workspace"
        expected = self.legacy(legacy)
        self.put(source, "autoskill/SKILL.md", b"synthetic legacy program")
        target = caller / "新数据"
        argv = ["data", "import", source.name, "--target", target.name]
        args = cli.parser().parse_args(["--input-base", str(caller), *argv])
        with patch.dict(os.environ, self.environment, clear=True):
            forwarded, stdin_text, attachments = command_input.for_bridge(args, argv)
            shutil.rmtree(source)
            result = bridge.execute(forwarded, stdin_text, caller_cwd=str(caller), attachments=attachments)
        self.assertEqual(result["exit"], 0, result)
        self.assertEqual(Path(json.loads(result["stdout"])["root"]), target)
        self.assert_legacy(target, expected)
        manifest = json.loads((target / "data-root.json").read_bytes())
        self.assertIn(str(legacy), manifest["legacy_roots"])
        self.assertFalse(any("bridge-io" in item for item in manifest["legacy_roots"]))
        receipt = json.loads((target / ".local/runs/run-preserved/test-report/receipt.json").read_bytes())
        resolved = data_refs.resolve(receipt["evidence"], root=target)
        self.assertEqual(resolved, target / ".local/runs/run-preserved/evidence.bin")
        self.assertEqual(resolved.read_bytes(), b"\x00\xfffrozen-evidence")

    def test_nonempty_import_target_is_preserved_and_permission_error_is_structured(self):
        source = self.root / "old data"
        self.legacy(source)
        sentinel = self.put(self.workspace, "keep.txt", b"do-not-overwrite")
        result = self.command("data", "import", source.name, code=2)
        self.assertTrue(result["error"])
        self.assertEqual(sentinel.read_bytes(), b"do-not-overwrite")
        output_parent = self.put(self.root, "not-a-directory", b"fixture")
        result = self.command("data", "export", "--out", str(output_parent / "archive.zip"), workspace=source, code=2)
        self.assertTrue(result["error"])


if __name__ == "__main__":
    unittest.main()
