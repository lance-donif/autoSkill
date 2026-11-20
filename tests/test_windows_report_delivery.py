"""Exercise the report writer through real CLI processes and the loopback dashboard."""
import base64
import contextlib
import errno
import functools
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from openpyxl import load_workbook
from test_bridge import APP, Sandbox
from test_replay_ui import Quiet

from autoskill import report_locations


def evidence(label, **fields):
    sys.stderr.write(label + " " + json.dumps(fields, ensure_ascii=False, sort_keys=True) + "\n")


class ReportDeliveryTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.app = APP
        self.workspace = Path(self.tmp.name) / "数据 (reports)"
        os.environ["AUTOSKILL_WORKSPACE"] = str(self.workspace)
        self.source = Path(self.tmp.name) / "设计 (input).json"
        row = {"id": "TC-001", "title": "查阅介绍页", "steps": ["进入介绍页"], "expected": "显示知识检索介绍"}
        self.source.write_text(json.dumps([row], ensure_ascii=False), encoding="utf-8")
        self.port = 8787

    def env(self, **extra):
        return {**super().env(**extra), "PYTHONPATH": str(self.app)}

    def dashboard(self, app=APP):
        if self.server:
            self.server.terminate()
            self.server.wait(20)
        self.app = app
        self.port = self.start_service()

    def context(self):
        value = {"version": 1, "host": "dsh", "workspace": str(self.workspace), "port": self.port}
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    def command(self, mode, *args, code=0):
        actual, output, error = self.cli("--runtime-context=" + self.context(), "--session", mode, *args,
                                         sandboxed=False, AUTOSKILL_BRIDGE=mode)
        self.assertEqual(actual, code, output + error)
        return json.loads(output)

    def create(self, url="http://127.0.0.1:9/"):
        self.command("off", "project", "create", "--id", "report-test", "--name", "本地测试", "--url", url)

    def write_plan(self, mode):
        return self.command(mode, "plan", "write", "report-test", "--file", str(self.source))

    def run_counts(self):
        with contextlib.closing(sqlite3.connect(self.workspace / ".local/autoskill.db")) as database:
            return [database.execute("SELECT count(*) FROM " + table).fetchone()[0] for table in ("runs", "results")]

    def check_files(self, receipt):
        self.assertEqual(receipt["status"], "ready", receipt)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for field, prefix in (("excel", ""), ("markdown", "markdown_"), ("bugs", "bugs_")):
            content = Path(receipt[field]).read_bytes()
            self.assertEqual(hashlib.sha256(content).hexdigest(), receipt[prefix + "sha256"])
            self.assertEqual(len(content), receipt[prefix + "size"])
            with opener.open(f"http://127.0.0.1:{self.port}" + receipt[prefix + "download_url"], timeout=10) as response:
                self.assertEqual(response.read(), content)
            if field != "excel":
                self.assertTrue(content.decode("utf-8").strip())
        with contextlib.closing(load_workbook(receipt["excel"], read_only=True)) as book:
            self.assertEqual(book["功能测试用例"].max_column, 14)
        if receipt.get("delivery_excel"):
            self.assertEqual(Path(receipt["delivery_excel"]).read_bytes(), Path(receipt["excel"]).read_bytes())
        evidence("receipt", **{key: receipt[key] for key in ("kind", "report_id", "status", "sha256", "markdown_sha256", "bugs_sha256")})

    def test_direct_and_http_bridge_create_readable_designs(self):
        self.dashboard()
        self.create()
        for mode in ("off", "on"):
            with self.subTest(bridge=mode):
                written = self.write_plan(mode)
                self.check_files(written["test_report"])
                self.assertEqual(Path(written["initial_excel"]).read_bytes(), Path(written["delivery_excel"]).read_bytes())
                self.assertEqual(self.command(mode, "plan", "show")["test_report"], written["test_report"])
        self.assertEqual(self.run_counts(), [0, 0])

    def test_native_descriptor_access_modes(self):
        if os.name != "nt":
            self.skipTest("native Windows CRT required")
        target = Path(self.tmp.name) / "fsync.bin"
        target.write_bytes(b"native fd control")
        with target.open("rb") as file, self.assertRaises(OSError) as raised:
            os.fsync(file.fileno())
        evidence("native rb fsync", errno=raised.exception.errno, winerror=getattr(raised.exception, "winerror", None))
        self.assertEqual(raised.exception.errno, errno.EBADF)
        with target.open("r+b") as file:
            os.fsync(file.fileno())

    def test_existing_v251_report_retries_after_upgrade(self):
        baseline = os.environ.get("AUTOSKILL_REPORT_BASELINE")
        if os.name != "nt" or not baseline:
            self.skipTest("native Windows and a v2.5.1 checkout required")
        self.assertTrue((Path(baseline) / "autoskill/reporting.py").is_file())
        self.dashboard(Path(baseline))
        self.create()
        failed = {}
        for mode in ("off", "on"):
            written = self.write_plan(mode)
            receipt = written["test_report"]
            self.assertEqual(receipt["status"], "failed", receipt)
            self.assertFalse(Path(written["delivery_excel"]).exists())
            with contextlib.closing(load_workbook(written["excel"], read_only=True)) as book:
                self.assertEqual(book["用例"].max_column, 11)
            original_retry = self.command(mode, "task-report", receipt["report_id"], "--retry", code=2)
            for original in (receipt, original_retry):
                self.assertIn("[Errno 9]", original["error"])
            snapshot = report_locations.folder(receipt["report_id"]) / "snapshot.json"
            failed[mode] = (receipt, snapshot.read_bytes())
            evidence("v2.5.1", bridge=mode, report_id=receipt["report_id"], write=receipt["error"], retry=original_retry["error"])

        before = self.run_counts()
        self.dashboard()
        for mode, (old, snapshot) in failed.items():
            ready = self.command(mode, "task-report", old["report_id"], "--retry")
            self.assertEqual((ready["report_id"], ready["source_hash"]), (old["report_id"], old["source_hash"]))
            self.assertEqual((Path(ready["excel"]).parent / "snapshot.json").read_bytes(), snapshot)
            self.check_files(ready)
            self.assertEqual(self.command(mode, "plan", "show")["test_report"]["report_id"], ready["report_id"])
        self.assertEqual(self.run_counts(), before)

    def check_publication_failure(self, note, expected_code):
        updated = self.command("on", "plan", "mark", "TC-001", "未执行", "--note", note)
        failed = updated["test_report"]
        evidence("native I/O", **{key: failed.get(key) for key in ("stage", "code", "errno", "winerror", "error")})
        self.assertEqual((failed["status"], failed["stage"], failed["code"]), ("failed", "design_delivery", expected_code))
        self.assertEqual(failed["errno"], errno.EACCES)
        self.assertTrue(failed["retryable"])
        if expected_code == "file_in_use":
            self.assertIn(failed["winerror"], (32, 33))
        return failed

    def test_windows_acl_and_open_workbook_allow_retry_on_same_id(self):
        if os.name != "nt":
            self.skipTest("native Windows DACL and file sharing required")
        self.dashboard()
        self.create()
        written = self.write_plan("on")
        self.check_files(written["test_report"])
        target = Path(written["delivery_excel"])
        before = target.read_bytes()
        counts = self.run_counts()
        # Deny only AddFile on this temporary directory. Other report/receipt paths stay writable.
        subprocess.run(["icacls", str(target.parent), "/deny", "*S-1-1-0:(WD)"], check=True)
        try:
            failure = self.check_publication_failure("文件夹禁止创建文件", "permission_denied")
            self.assertEqual(target.read_bytes(), before)
        finally:
            subprocess.run(["icacls", str(target.parent), "/remove:d", "*S-1-1-0"], check=True)
        self.check_files(self.command("on", "task-report", failure["report_id"], "--retry"))
        before = target.read_bytes()
        # The Windows CRT permits reads but withholds delete sharing while this handle is open.
        with target.open("rb") as opened:
            probe = target.with_name("replace-probe")
            probe.write_bytes(before)
            with self.assertRaises(PermissionError) as native:
                probe.replace(target)
            probe.unlink()
            winerror = native.exception.winerror
            self.assertIn(winerror, (5, 32, 33))
            code = "permission_denied" if winerror == 5 else "file_in_use"
            failure = self.check_publication_failure("文件仍在打开", code)
            self.assertEqual(failure["winerror"], winerror)
            self.assertEqual(opened.read(), before)
        self.check_files(self.command("on", "task-report", failure["report_id"], "--retry"))
        self.assertEqual(self.run_counts(), counts)

    def page(self):
        folder = Path(self.tmp.name) / "site"
        folder.mkdir()
        buttons = "".join(f"<button>Item {index}</button>" for index in range(1, 171))
        (folder / "index.html").write_text(
            '<!doctype html><meta charset="utf-8"><title>Local recording</title>'
            '<div onclick="document.querySelector(\'#result\').textContent=\'知识检索介绍 · \'+event.target.textContent">'
            + buttons + '</div><p id="result"></p>', encoding="utf-8")
        site = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=folder))
        worker = threading.Thread(target=site.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 5)
        self.addCleanup(site.server_close)
        self.addCleanup(site.shutdown)
        return f"http://127.0.0.1:{site.server_port}/"

    def shell_command(self, shell, mode, command):
        # EncodedCommand carries the literal script as UTF-16LE under both shell versions.
        # It avoids injecting an extra command-line quoting layer before PowerShell parses it.
        script = ("[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)\n"
                  "& $env:REPORT_TEST_CMD --runtime-context=$env:REPORT_TEST_CONTEXT --session $env:AUTOSKILL_BRIDGE " + command +
                  "\nexit $LASTEXITCODE")
        environment = self.env(AUTOSKILL_BRIDGE=mode, AUTOSKILL_DEV_PYTHON=sys.executable, REPORT_TEST_CONTEXT=self.context(),
                               REPORT_TEST_CMD=str(self.app.parent / "autoskill.cmd"))
        encoded = base64.b64encode(script.encode("utf-16-le")).decode()
        done = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded], env=environment,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", timeout=120, check=False)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return json.loads(done.stdout)

    def screenshots(self, mode):
        from PIL import Image

        session = hashlib.sha256(mode.encode()).hexdigest()[:32]
        folder = self.workspace / ".local/recordings" / session / "screenshots"
        explicit = self.workspace / "显式截图 (result).png"
        for args in (["#result"], [str(explicit)]):
            receipt = self.command(mode, "browser", "screenshot", *args)["result"]
            file = Path(receipt["path"])
            self.assertEqual(receipt["reference"], file.relative_to(self.workspace).as_posix())
            if args == ["#result"]:
                self.assertEqual(file.parent, folder)
            else:
                self.assertEqual(file, explicit)
            self.assertEqual((receipt["sha256"], receipt["size"]), (hashlib.sha256(file.read_bytes()).hexdigest(), file.stat().st_size))
            with Image.open(file) as screenshot:
                self.assertEqual(screenshot.format, "PNG")
                self.assertEqual((receipt["format"], receipt["width"], receipt["height"]), ("png", *screenshot.size))
                screenshot.verify()
            evidence("recording screenshot", bridge=mode, **{key: receipt[key] for key in ("reference", "sha256", "width", "height")})

    def wait_for_run(self, mode, run_id):
        deadline = time.monotonic() + 90
        while True:
            status = self.command(mode, "run-status", run_id)
            if status.get("publication", {}).get("complete") and status["test_report"]["status"] in ("ready", "failed"):
                self.assertEqual(status["status"], "Pass", status)
                return status
            if time.monotonic() >= deadline:
                self.fail(f"Run did not finish in time: {status}")
            time.sleep(0.3)

    def record(self, shell, mode):
        started = self.command(mode, "record", "start", "report-test", "--no-login", "--headless")
        try:
            match = re.search(r'button "(Item \d+)" \[ref=e164\]', started["snapshot"])
            self.assertIsNotNone(match, started["snapshot"])
            self.shell_command(shell, mode, "browser click '@e164'")
            checked = self.command(mode, "check", "contains", "#result", "知识检索介绍")
            self.assertTrue(checked["passed"], checked)
            self.assertEqual(checked["actual"], "知识检索介绍 · " + match[1])
            if self.app == APP:
                self.screenshots(mode)
            saved = self.shell_command(shell, mode, "record stop --title '打开“RAG 与知识检索”介绍页' --plan TC-001")
            self.assertEqual(saved["ui_case"]["title"], "打开“RAG 与知识检索”介绍页")
        finally:
            if self.command(mode, "record", "status")["recording"]:
                self.command(mode, "record", "cancel")
        result = self.wait_for_run(mode, saved["run_id"])
        self.assertIn(saved["ui_case"]["id"], result["publication"]["published_ids"])
        return saved["test_report"], result["test_report"]

    def check_shell(self, shell, mode):
        if os.name != "nt":
            self.skipTest("native Windows PowerShell and .cmd required")
        self.dashboard()
        self.create(self.page())
        design = self.write_plan(mode)
        self.check_files(design["test_report"])
        initial = Path(design["initial_excel"]).read_bytes()
        recording, run = self.record(shell, mode)
        final = self.command(mode, "plan", "show")
        self.assertEqual(final["cases"][0]["status"], "通过", final)
        self.assertEqual(final["delivery_excel"], design["delivery_excel"])
        self.assertEqual(Path(final["initial_excel"]).read_bytes(), initial)
        for item in (recording, run, final["test_report"]):
            self.check_files(item)
        counts = self.run_counts()
        self.assertEqual(counts, [1, 1])
        report_id = recording["report_id"]
        Path(recording["markdown"]).unlink()
        self.assertEqual(self.command(mode, "task-report", report_id, code=2)["status"], "failed")
        self.check_files(self.command(mode, "task-report", report_id, "--retry"))
        self.assertEqual(self.run_counts(), counts)
        evidence("PowerShell .cmd", shell=shell, bridge=mode, title="打开“RAG 与知识检索”介绍页", ref="@e164", runs_and_results=counts)

    def test_windows_powershell_51_cmd_with_direct_recording(self):
        self.check_shell("powershell.exe", "off")

    def test_powershell_7_cmd_through_http_bridge(self):
        self.check_shell("pwsh.exe", "on")

    def test_v251_recording_and_run_reports_retry_without_replay(self):
        baseline = os.environ.get("AUTOSKILL_REPORT_BASELINE")
        if os.name != "nt" or not baseline:
            self.skipTest("native Windows and a v2.5.1 checkout required")
        mode = "on"
        self.dashboard(Path(baseline))
        self.create(self.page())
        self.assertEqual(self.write_plan(mode)["test_report"]["status"], "failed")
        recording, run = self.record("pwsh.exe", mode)
        final = self.command(mode, "plan", "show")["test_report"]
        failed = (recording, run, final)
        for old in failed:
            self.assertEqual(old["status"], "failed", old)
            self.assertIn("[Errno 9]", old["error"])
            evidence("v2.5.1 after replay", kind=old["kind"], report_id=old["report_id"], error=old["error"])
        self.assertEqual(self.run_counts(), [1, 1])
        self.dashboard()
        for old in failed:
            snapshot = report_locations.folder(old["report_id"]) / "snapshot.json"
            before = snapshot.read_bytes()
            ready = self.command(mode, "task-report", old["report_id"], "--retry")
            self.assertEqual((ready["report_id"], ready["source_hash"]), (old["report_id"], old["source_hash"]))
            self.assertEqual(snapshot.read_bytes(), before)
            self.check_files(ready)
        self.assertEqual(self.run_counts(), [1, 1])
