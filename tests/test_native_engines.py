import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill import db, engines, native_assets, native_run, paths, projects, runner, secrets
from autoskill.engines import reports


class Handler(BaseHTTPRequestHandler):
    active = 0
    maximum = 0
    lock = threading.Lock()

    def do_GET(self):
        with self.lock:
            type(self).active += 1
            type(self).maximum = max(type(self).maximum, type(self).active)
        try:
            time.sleep(0.12)
            self.send_response(200)
            self.send_header("Content-Type", "text/html" if self.path == "/page" else "application/json")
            self.end_headers()
            with contextlib.suppress(BrokenPipeError):
                self.wfile.write(b"<title>Native fixture</title><h1>preserved</h1>" if self.path == "/page" else b'{"value":"okay"}')
        finally:
            with self.lock:
                type(self).active -= 1

    def log_message(self, *_args):
        pass


class Workspace(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.environment = mock.patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.root / "workspace"), "AUTOSKILL_HEADED": "0"})
        self.environment.start()
        db.init()
        self.pid = projects.create("Native tests", "http://native.example", project_id="native")["id"]

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def fixture(self, body: str) -> Path:
        folder = self.root / "original"
        folder.mkdir(exist_ok=True)
        (folder / "test_fixture.py").write_text(body)
        return folder

    def execute(self, source: Path, engine="selenium-unittest", parameters=None, **asset_options):
        asset = native_assets.import_asset(self.pid, str(source), engine, **asset_options)
        run = native_run.enqueue(self.pid, asset["id"], parameters=parameters)
        native_run.execute(run["id"])
        return native_run.get(run["id"])


class NativeTests(Workspace):
    def test_original_unittest_positive_negative_and_fixture_cleanup(self):
        source = self.fixture("import unittest\nfrom pathlib import Path\n"
                              "def tearDownModule(): Path('cleanup.txt').write_text('done')\n"
                              "class Original(unittest.TestCase):\n"
                              " def test_pass(self): self.assertEqual(2 + 2, 4)\n"
                              " def test_fail(self): self.assertEqual('actual', 'expected')\n")
        result = self.execute(source)
        self.assertEqual(result["status"], "Fail", result)
        self.assertEqual([row["status"] for row in result["results"]], ["Fail", "Pass"])
        self.assertTrue((paths.local() / "runs" / result["id"] / "native/input/cleanup.txt").is_file())
        self.assertFalse((source / "cleanup.txt").exists())
        self.assertIn("expected", result["results"][0]["reason"])

    def test_queue_executes_pinned_source_after_reimport(self):
        source = self.fixture("import unittest\nclass Original(unittest.TestCase):\n def test_pin(self): self.assertTrue(True)\n")
        asset = native_assets.import_asset(self.pid, str(source), "selenium-unittest")
        queued = native_run.enqueue(self.pid, asset["id"])
        (source / "test_fixture.py").write_text("import unittest\nclass Changed(unittest.TestCase):\n def test_changed(self): self.fail('new revision')\n")
        changed = native_assets.import_asset(self.pid, str(source), "selenium-unittest", asset_id=asset["id"])
        self.assertNotEqual(asset["head"], changed["head"])
        self.assertEqual(native_run.execute(queued["id"])["status"], "Pass")
        later = native_run.enqueue(self.pid, asset["id"])
        self.assertEqual(native_run.execute(later["id"])["status"], "Fail")

    def test_unavailable_runtime_is_blocked_without_launching_or_downloading(self):
        source = self.fixture("import unittest\n")
        with mock.patch.dict(os.environ, {"AUTOSKILL_NATIVE_PYTHON": str(self.root / "missing-python")}):
            result = self.execute(source)
        self.assertEqual(result["status"], "Blocked")
        self.assertIn("不存在", result["reason"])
        self.assertEqual(result["passed"], 0)

    def test_exit_zero_without_native_report_is_not_business_pass(self):
        source = self.fixture("pass\n")
        result = self.execute(source, argv=["python", "test_fixture.py"])
        self.assertEqual(result["status"], "Blocked")
        self.assertEqual(result["outcome"]["business_status"], "Unverified")

    def test_queued_cancel_never_starts(self):
        asset = native_assets.import_asset(self.pid, str(self.fixture("raise RuntimeError('must not run')\n")), "selenium-unittest")
        run = native_run.enqueue(self.pid, asset["id"])
        self.assertEqual(native_run.cancel(run["id"])["status"], "Cancelled")
        self.assertEqual(native_run.execute(run["id"])["status"], "Cancelled")

    def test_cancel_terminates_native_descendants_and_keeps_events(self):
        source = self.fixture("import unittest,subprocess,sys,time\nfrom pathlib import Path\n"
                              "class Slow(unittest.TestCase):\n def test_wait(self):\n"
                              "  child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(90)'])\n"
                              "  Path('descendant.pid').write_text(str(child.pid))\n  time.sleep(90)\n")
        asset = native_assets.import_asset(self.pid, str(source), "selenium-unittest")
        run = native_run.enqueue(self.pid, asset["id"])
        executor = threading.Thread(target=native_run.execute, args=(run["id"],))
        executor.start()
        pid_file = paths.local() / "runs" / run["id"] / "native/input/descendant.pid"
        deadline = time.monotonic() + 20
        while not pid_file.is_file() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(pid_file.is_file())
        native_run.cancel(run["id"])
        executor.join(25)
        self.assertFalse(executor.is_alive())
        self.assertFalse(runner.process_alive(int(pid_file.read_text())))
        result = native_run.get(run["id"])
        self.assertEqual(result["status"], "Cancelled")
        self.assertTrue((pid_file.parent.parent / "events.jsonl").is_file())

    def test_manifest_preserves_full_tree_and_hashes(self):
        source = self.fixture("import unittest\n")
        (source / "data").mkdir()
        (source / "data/bytes.bin").write_bytes(b"\x00\xfffixture")
        asset = native_assets.import_asset(self.pid, str(source), "selenium-unittest", argv=["python", "-m", "unittest", "discover", "-v"])
        self.assertEqual({entry["path"] for entry in asset["files"]}, {"test_fixture.py", "data/bytes.bin"})
        self.assertEqual(asset["argv"][-1], "-v")
        copied = self.root / "copied"
        native_assets.materialize(self.pid, asset["id"], copied, asset["head"])
        self.assertEqual((copied / "data/bytes.bin").read_bytes(), b"\x00\xfffixture")

    def test_parameters_use_frozen_credentials_after_store_changes(self):
        source = self.fixture("import unittest,os\nclass Original(unittest.TestCase):\n def test_token(self): self.assertEqual(os.environ['TOKEN'], 'synthetic-old')\n")
        asset = native_assets.import_asset(self.pid, str(source), "selenium-unittest")
        secrets.set_secret("NATIVE_TOKEN", "synthetic-old")
        run = native_run.enqueue(self.pid, asset["id"], parameters={"env": {"TOKEN": "${ENV:NATIVE_TOKEN}"}})
        secrets.set_secret("NATIVE_TOKEN", "synthetic-new")
        self.assertEqual(native_run.execute(run["id"])["status"], "Pass")
        self.assertNotIn("synthetic-old", json.dumps(native_run.get(run["id"])))

    def test_queued_files_survive_source_and_domain_replacement(self):
        source = self.fixture("import unittest\nclass Original(unittest.TestCase):\n def test_saved(self): self.assertTrue(True)\n")
        asset = native_assets.import_asset(self.pid, str(source), "selenium-unittest")
        run = native_run.enqueue(self.pid, asset["id"])
        shutil.rmtree(source)
        shutil.rmtree(projects.path(self.pid) / "domain")
        self.assertEqual(native_run.execute(run["id"])["status"], "Pass")

    def test_performance_failure_is_not_a_business_failure_and_planned_count_survives(self):
        asset = native_assets.import_asset(self.pid, str(self.fixture("pass\n")), "selenium-unittest")
        run = native_run.enqueue(self.pid, asset["id"])
        outcome = {"status": "Fail", "metrics": {"planned_cases": 3}, "cases": [
            {"id": "performance", "status": "Fail", "verdict_scope": "performance", "assertions": [{"passed": False}]}]}
        native_run._record(run, outcome)
        result = native_run.get(run["id"])
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["done"], 1)
        self.assertEqual(result["results"][0]["detail"]["business_status"], "Unverified")

    def test_successful_native_launcher_cleans_background_descendant(self):
        source = self.fixture("import unittest,subprocess,sys\nfrom pathlib import Path\n"
                              "class Original(unittest.TestCase):\n def test_child(self):\n"
                              "  child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(90)'])\n"
                              "  Path('child.pid').write_text(str(child.pid))\n  self.assertTrue(True)\n")
        result = self.execute(source)
        self.assertEqual(result["status"], "Pass", result["outcome"])
        file = paths.local() / "runs" / result["id"] / "native/input/child.pid"
        self.assertFalse(runner.process_alive(int(file.read_text())))

    def test_timeout_preserves_diagnostic(self):
        source = self.fixture("import unittest,time\nclass Slow(unittest.TestCase):\n def test_slow(self): time.sleep(90)\n")
        result = self.execute(source, parameters={"timeout_seconds": 0.3})
        self.assertEqual(result["status"], "Blocked")
        self.assertIn("时限", result["reason"])

    @unittest.skipIf(os.name == "nt", "This fixture uses a Unix interpreter symlink")
    def test_original_explicit_interpreter_takes_precedence(self):
        source = self.fixture("import unittest\nclass Original(unittest.TestCase):\n def test_ok(self): self.assertTrue(True)\n")
        executable = self.root / "python-selected"
        executable.symlink_to(sys.executable)
        with mock.patch.dict(os.environ, {"AUTOSKILL_NATIVE_PYTHON": str(self.root / "missing")}):
            result = self.execute(source, argv=[str(executable), "-m", "unittest", "discover"])
        self.assertEqual(result["status"], "Pass", result["outcome"])

    def test_supervisor_stops_child_after_owner_crash(self):
        folder = self.root / "supervised"
        folder.mkdir()
        spec = {"argv": [sys.executable, "-c", "import os,time;from pathlib import Path;Path('native.pid').write_text(str(os.getpid()));time.sleep(90)"],
                "cwd": str(folder), "evidence": str(folder)}
        owner = self.root / "owner.py"
        owner.write_text("import time\nfrom autoskill.engines.process import Process\np=Process(" + repr(spec) + ")\ntime.sleep(90)\n")
        environment = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [str(ROOT / "skills/autoskill/app"), os.environ.get("PYTHONPATH")]))}
        child = subprocess.Popen([sys.executable, str(owner)], env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 20
            while not (folder / "native.pid").exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue((folder / "native.pid").exists())
            pid = int((folder / "native.pid").read_text())
            child.kill()
            child.wait(timeout=5)
            while runner.process_alive(pid) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(runner.process_alive(pid))
            while not (folder / "process-result.json").exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            result = json.loads((folder / "process-result.json").read_text())
            self.assertEqual(result["state"], "owner_exited")
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)


class InstalledEngines(Workspace):
    def setUp(self):
        super().setUp()
        Handler.maximum = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        super().tearDown()

    def require(self, engine):
        if not engines.get(engine).probe()["available"]:
            self.skipTest(engine + " is not installed")

    def collection(self, expected=200, apifox=False):
        item = {"id": "native-request", "name": "Native assertion", "request": {"method": "GET", "url": f"http://127.0.0.1:{self.server.server_port}/"},
                "event": [{"listen": "test", "script": {"type": "text/javascript", "exec":
                          [f'pm.test("expected status", function () {{pm.response.to.have.status({expected});}});']}}]}
        value = {"info": {"name": "Synthetic native collection", "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"},
                 "item": [{"name": "Original folder", "id": "folder", "item": [item]}]}
        if apifox:
            value["apifoxCli"] = "1.1"
            item["metaInfo"] = {"type": "http"}
        file = self.root / ("fixture.apifox-cli.json" if apifox else "fixture.postman_collection.json")
        file.write_text(json.dumps(value))
        return file

    def test_newman_actual_native_assertions(self):
        self.require("postman-newman")
        for expected, status in ((200, "Pass"), (201, "Fail")):
            result = self.execute(self.collection(expected), "postman-newman")
            self.assertEqual(result["status"], status, result["outcome"])
            self.assertEqual(result["results"][0]["detail"]["assertions"][0]["passed"], expected == 200)

    def test_newman_script_failure_is_not_hidden_by_suppressed_exit_code(self):
        self.require("postman-newman")
        source = self.collection()
        data = json.loads(source.read_text())
        data["item"][0]["item"][0]["event"][0]["script"]["exec"] = ['throw new Error("synthetic script failure");']
        source.write_text(json.dumps(data))
        result = self.execute(source, "postman-newman", {"args": ["--suppress-exit-code"]})
        self.assertEqual(result["status"], "Blocked", result["outcome"])
        self.assertIn("synthetic script failure", str(result["results"]))

    def test_apifox_actual_native_assertions(self):
        self.require("apifox")
        for expected, status in ((200, "Pass"), (201, "Fail")):
            result = self.execute(self.collection(expected, True), "apifox")
            self.assertEqual(result["status"], status, result["outcome"])

    def test_jmeter_real_concurrency_report_and_negative_assertion(self):
        self.require("jmeter")
        source = ROOT / "tests/fixtures/native_load.jmx"
        for expected, status in (("okay", "Pass"), ("wrong", "Fail")):
            parameters = {"args": [f"-Jport={self.server.server_port}", f"-Jexpected={expected}"], "thresholds": {"max_error_rate": 0}}
            result = self.execute(source, "jmeter", parameters, argv=["jmeter", "-n", "-t", "native_load.jmx", "-l", "old.jtl", "-j", "old.log", "-e", "-o", "old-html"])
            self.assertEqual(result["status"], status, result["outcome"])
            self.assertEqual(result["outcome"]["metrics"]["samples"], 6)
            self.assertGreaterEqual(Handler.maximum, 2)
            evidence = paths.local() / "runs" / result["id"] / "native"
            self.assertTrue((evidence / "html/index.html").is_file())
            self.assertTrue((evidence / "samples.jtl").is_file())

    def test_selenium_pytest_keeps_pom_data_parameterization_and_fixture_scope(self):
        self.require("selenium-pytest")
        if not os.environ.get("NATIVE_BROWSER") or not os.environ.get("NATIVE_DRIVER"):
            self.skipTest("Native Selenium browser paths were not supplied")
        source = self.root / "pom"
        shutil.copytree(ROOT / "tests/fixtures/native_pom", source)
        for expected, status in (("preserved", "Pass"), ("wrong", "Fail")):
            events = self.root / (expected + ".events")
            result = self.execute(source, "selenium-pytest", {"env": {"EXPECTED_TITLE": expected, "FIXTURE_EVENTS": str(events),
                                                                    "FIXTURE_URL": f"http://127.0.0.1:{self.server.server_port}"}})
            self.assertEqual(result["status"], status, result["outcome"])
            self.assertEqual(len(result["results"]), 2)
            self.assertEqual(events.read_text(), "setup\nteardown\n")


class NativeReportTests(unittest.TestCase):
    def test_successful_exit_does_not_hide_reported_assertion_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            result = reports.summarize([{"status": "Fail", "assertions": [{"passed": False}]}], {"state": "completed", "exit_code": 0}, Path(directory))
        self.assertEqual(result["status"], "Fail")


if __name__ == "__main__":
    unittest.main()
