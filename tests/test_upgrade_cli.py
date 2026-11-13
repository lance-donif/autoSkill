"""CLI and HTTP upgrades retain staged inputs after bridge cleanup and return durable handoff receipts."""
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "skills/autoskill/app"
sys.path.insert(0, str(APP))

from autoskill import bridge, cli, command_input, upgrade
from autoskill.server.app import create_app


class UpgradeCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.root / "external workspace"), "AUTOSKILL_BRIDGE": "off",
                                               "AUTOSKILL_INPUT_MAP": "", "AUTOSKILL_RUNTIME_CONTEXT": "", "AUTOSKILL_CALLER_CWD": ""})
        environment.start()
        self.addCleanup(environment.stop)
        self.target = self.root / "old installation"
        self.package = self.root / "new package"
        self.make_package(self.target, "1.0.0", runtime=False)
        self.make_package(self.package, "2.0.0", runtime=True)
        data = self.target / "autoskill/workspace/.local"
        data.mkdir(parents=True)
        (data / "credentials.json").write_text('{"synthetic":"fixture-value"}', encoding="utf-8")

    def make_package(self, folder, version, runtime):
        app = folder / "autoskill/app/autoskill"
        app.mkdir(parents=True)
        (folder / "package.json").write_text(json.dumps({"name": "synthetic-upgrade-fixture", "version": version}), encoding="utf-8")
        (folder / "autoskill/SKILL.md").write_text("合成升级验收包\n", encoding="utf-8")
        (app / "__init__.py").write_text("", encoding="utf-8")
        (app / "db.py").write_text("def init():\n    pass\n", encoding="utf-8")
        (app / "__main__.py").write_text('print(\'{"ok":true,"fixture":true}\')\n', encoding="utf-8")
        shutil.copy2(APP / "autoskill/upgrade.py", folder / "autoskill-upgrade.py")
        if runtime:
            binary = folder / "autoskill/vendor/linux-x64/python/bin/python3"
            binary.parent.mkdir(parents=True)
            binary.write_text(f"#!{sys.executable}\nimport os, sys\nos.execv(sys.executable, [sys.executable, *sys.argv[1:]])\n", encoding="utf-8")
            binary.chmod(0o755)
        files = {file.relative_to(folder).as_posix(): hashlib.sha256(file.read_bytes()).hexdigest() for file in folder.rglob("*") if file.is_file()}
        (folder / "package-manifest.json").write_text(json.dumps({"format": 1, "version": version, "files": files}), encoding="utf-8")

    def command(self, *argv):
        result = subprocess.run([sys.executable, "-m", "autoskill", *argv], cwd=self.root, env={**os.environ, "PYTHONPATH": str(APP)},
                                capture_output=True, text=True, encoding="utf-8", timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def wait_state(self, journal, expected):
        deadline = time.monotonic() + 15
        result = upgrade.status(journal)
        while (result["state"] not in (expected, "apply_failed", "prepare_failed") and result.get("last_action", {}).get("state") != "failed"
               and time.monotonic() < deadline):
            time.sleep(0.05)
            result = upgrade.status(journal)
        log = Path(journal).parent / "handoff.log"
        self.assertEqual(result["state"], expected, {"status": result, "handoff": log.read_text(encoding="utf-8") if log.is_file() else ""})
        return result

    def dashboard(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        process = subprocess.Popen([sys.executable, "-m", "autoskill", "dashboard", "--port", str(port)], cwd=self.target / "autoskill",
                                   env={**os.environ, "PYTHONPATH": str(APP)}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop_dashboard, process)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=0.2) as response:
                    if json.loads(response.read()).get("name") == "autoSkill":
                        return process, port
            except (urllib.error.URLError, TimeoutError):
                time.sleep(0.05)
        raise AssertionError("合成升级后台未启动")

    def stop_dashboard(self, process):
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=10)

    @unittest.skipUnless(sys.platform == "linux" and os.uname().machine == "x86_64", "The synthetic runtime fixture targets Linux x64")
    def test_bridge_package_snapshot_survives_cleanup_then_http_handoff_applies_and_cli_rolls_back(self):
        plan = self.command("upgrade", "plan", self.package.name, "--target", self.target.name)
        self.assertEqual((plan["old_version"], plan["new_version"]), ("1.0.0", "2.0.0"))
        self.assertFalse((self.root / "external workspace/.local/autoskill.db").exists())
        argv = ["upgrade", "prepare", self.package.name, "--target", self.target.name]
        args = cli.parser().parse_args(["--input-base", str(self.root), *argv])
        forwarded, stdin, attachments = command_input.for_bridge(args, argv)
        shutil.rmtree(self.package)
        reply = bridge.execute(forwarded, stdin, caller_cwd=str(self.root), attachments=attachments)
        self.assertEqual(reply["exit"], 0, reply)
        prepared = json.loads(reply["stdout"])
        journal = prepared["journal"]
        self.assertEqual(prepared["state"], "prepared")
        record = json.loads(Path(journal).read_text(encoding="utf-8"))
        self.assertTrue(Path(record["payload"]).is_dir())
        self.assertFalse(Path(record["plan"]["source"]).exists())
        with patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": "/proc/autoskill-readonly-status", "AUTOSKILL_BRIDGE": "auto"}):
            self.assertEqual(self.command("upgrade", "status", journal)["state"], "prepared")
        with TestClient(create_app(start_worker=False), base_url="http://127.0.0.1:8787") as client:
            self.assertEqual(client.post("/api/upgrades/apply", json={"journal": journal, "stop_services": "false"}).status_code, 400)
            response = client.post("/api/upgrades/apply", json={"journal": journal})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["state"], "launched")
            self.wait_state(journal, "completed")
            observed = client.post("/api/upgrades/status", json={"journal": journal}).json()
            self.assertEqual(observed["state"], "completed")
        self.assertEqual(json.loads((self.target / "package.json").read_text())["version"], "2.0.0")
        data = self.target / "autoskill/workspace/.local/credentials.json"
        self.assertEqual(json.loads(data.read_text())["synthetic"], "fixture-value")
        (data.parent / "after-upgrade.txt").write_text("升级后的新数据", encoding="utf-8")
        self.assertEqual(self.command("upgrade", "rollback", journal)["state"], "launched")
        self.wait_state(journal, "rolled_back")
        self.assertEqual(json.loads((self.target / "package.json").read_text())["version"], "1.0.0")
        self.assertEqual((data.parent / "after-upgrade.txt").read_text(encoding="utf-8"), "升级后的新数据")

    @unittest.skipUnless(sys.platform == "linux" and os.uname().machine == "x86_64", "The synthetic runtime fixture targets Linux x64")
    def test_prepare_handoff_returns_before_stopping_its_own_bridge_and_status_works_afterwards(self):
        process, port = self.dashboard()
        job = self.root / "known upgrade job"
        with patch.dict(os.environ, {"AUTOSKILL_BRIDGE": "on", "AUTOSKILL_BRIDGE_PORT": str(port)}):
            result = self.command("upgrade", "prepare", str(self.package), "--target", str(self.target),
                                  "--job-dir", str(job), "--stop-services")
        self.assertEqual((result["state"], result["journal"]), ("launched", str(job / "journal.json")))
        self.wait_state(result["journal"], "prepared")
        process.wait(timeout=10)
        with patch.dict(os.environ, {"AUTOSKILL_BRIDGE": "auto"}):
            self.assertEqual(self.command("upgrade", "status", result["journal"])["state"], "prepared")


if __name__ == "__main__":
    unittest.main()
