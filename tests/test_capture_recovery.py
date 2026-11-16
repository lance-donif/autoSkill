"""Interrupted observers preserve original evidence without trapping a real recording session."""
import json
import os
import signal
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill import candidate_publication, data_refs, domain, paths, projects, runner, runtime_identity
from autoskill.recorder import agent, capture, session


class ReadyPage(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return

    def do_GET(self):
        body = '<!doctype html><meta charset="utf-8"><button id="save" onclick="this.textContent=\'已保存\'">保存</button>'.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class CaptureRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.root / "data"), "AUTOSKILL_SESSION": "capture-recovery",
                                                  "AUTOSKILL_CASE_SKILL": str(self.root / "sheet"), "AUTOSKILL_HEADED": "0",
                                                  "AUTOSKILL_BRIDGE": "off", "AUTOSKILL_WORKFLOW_BINDING": "", "AUTOSKILL_RUNTIME_CONTEXT": ""}))
        self.enterContext(patch("autoskill.server.jobs.ensure_worker", return_value={"status": "queued-test"}))

    def test_broken_projection_writes_an_unavailable_receipt_and_keeps_original_bytes(self):
        folder = paths.workspace() / "network-broken"
        folder.mkdir(parents=True)
        events = folder / "events.jsonl"
        original = b'{"method":"Network.loadingFinished"}\n{"truncated":'
        events.write_bytes(original)
        observer = capture.Observer.__new__(capture.Observer)
        observer.folder, observer.token, observer.ready = folder, "original-token", True
        observer.entries, observer.bodies = [], {}
        observer.journal = events.open("a", encoding="utf-8")
        observer.ws = Mock()
        observer.ws.recv.return_value = ""
        observer.send = Mock()
        observer.run()
        result = json.loads((folder / "capture.json").read_text())
        self.assertFalse(result["available"])
        self.assertEqual(result["entries"], [])
        self.assertIn("JSONDecodeError", result["error"])
        self.assertEqual(events.read_bytes(), original)

    def test_mismatched_capture_token_still_fails_without_signalling_a_process(self):
        folder = paths.workspace() / "network-other"
        folder.mkdir(parents=True)
        original = json.dumps({"token": "other-session", "available": True, "entries": []}).encode()
        (folder / "capture.json").write_bytes(original)
        handle = {"folder": str(folder), "pid": os.getpid(), "token": "this-session", "owner": {**runtime_identity.current(), "location_id": "other"}}
        with patch.object(capture.os, "kill", side_effect=AssertionError("No foreign process signal")), \
                self.assertRaisesRegex(RuntimeError, "不属于当前录制会话"):
            capture.stop(handle)
        self.assertEqual((folder / "capture.json").read_bytes(), original)
        self.assertFalse((folder / "control.json").exists())
        (folder / "ready.json").write_text(json.dumps({"token": "other-session", "ready": True}))
        handle["owner"] = runtime_identity.current()
        with patch.object(capture.os, "kill", side_effect=AssertionError("No mismatched observer signal")), \
                self.assertRaisesRegex(RuntimeError, "身份不匹配"):
            capture._terminate_observer(handle, folder)

    def start_page(self):
        if not paths.agent_browser() or not paths.chrome():
            self.skipTest("requires local recorder browser")
        if not hasattr(self, "project"):
            server = ThreadingHTTPServer(("127.0.0.1", 0), ReadyPage)
            threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            self.project = projects.create("观察器中断恢复", f"http://127.0.0.1:{server.server_port}", project_id="capture-recovery")["id"]
            self.addCleanup(session.cancel)
        session.start(self.project, login=False)
        session.browser(["click", "#save"])
        self.assertTrue(session.check("text", "#save", "已保存", timeout_seconds=3)["passed"])
        recording = session.current()
        handle = recording["network_capture"]
        self.assertTrue(runtime_identity.owns(handle["pid"], handle["owner"]))
        process, token = capture._children[handle["pid"]]
        self.assertEqual(token, handle["token"])
        return recording, data_refs.resolve(handle["folder"]), process

    def test_dead_observer_stop_preserves_ui_and_raw_evidence_then_cancel_and_new_start_work(self):
        recording, folder, process = self.start_page()
        process.terminate()
        process.wait(timeout=5)
        original = (folder / "events.jsonl").read_bytes()
        self.assertTrue(original)
        agent.call(session.session_name(), ["close"], check=False)
        result = session.stop("保存后的真实页面状态")
        self.assertFalse(result["network_capture"]["available"])
        self.assertEqual(result["api_cases"], [])
        self.assertEqual(result["ui_case"]["recording_status"], "Pass")
        self.assertEqual(projects.cases(self.project), [])
        retained = json.loads(data_refs.resolve(result["network_capture"]["retained_recording"]).read_text())
        self.assertEqual(retained["steps"], recording["steps"])
        self.assertEqual((folder / "events.jsonl").read_bytes(), original)
        source = f"conversations/{result['conversation_captures']['id']}/source.original.cdp.jsonl"
        self.assertEqual(domain.read_file(self.project, source), original)
        self.assertFalse(session.status()["recording"])
        self.assertEqual(len(result["run_ids"]), 1)
        run_id = result["run_ids"][0]
        self.assertEqual(runner.execute(run_id)["status"], "Pass")
        self.assertEqual(candidate_publication.receipt(run_id)["published_ids"], [result["ui_case"]["id"]])

        cancelled, failed_folder, failed_process = self.start_page()
        failed_process.terminate()
        failed_process.wait(timeout=5)
        old_events = (failed_folder / "events.jsonl").read_bytes()
        stopped = session.cancel()
        self.assertTrue(stopped["cancelled"])
        self.assertEqual(json.loads(data_refs.resolve(stopped["retained_recording"]).read_text())["steps"], cancelled["steps"])
        self.assertEqual((failed_folder / "events.jsonl").read_bytes(), old_events)
        self.assertFalse(session.status()["recording"])
        abandoned, abandoned_folder, abandoned_process = self.start_page()
        abandoned_process.terminate()
        abandoned_process.wait(timeout=5)
        abandoned_events = (abandoned_folder / "events.jsonl").read_bytes()
        agent.call(session.session_name(), ["close"], check=False)
        restarted = session.start(self.project, login=False)
        self.assertIn("原 2 步和网络来源已保留", restarted["notes"][0])
        retained = [json.loads(file.read_text()) for file in session.recording_dir().glob("recording-retained-*.json")]
        self.assertTrue(any(value["network_capture"] == abandoned["network_capture"] and value["steps"] == abandoned["steps"] for value in retained))
        self.assertEqual((abandoned_folder / "events.jsonl").read_bytes(), abandoned_events)
        self.assertTrue(session.status()["recording"])
        self.assertTrue(session.cancel()["cancelled"])

    @unittest.skipUnless(hasattr(signal, "SIGSTOP"), "requires a stoppable local observer process")
    def test_unresponsive_owned_observer_is_reaped_after_deadline_and_session_can_restart(self):
        recording, folder, process = self.start_page()
        os.kill(process.pid, signal.SIGSTOP)
        original = (folder / "events.jsonl").read_bytes()
        try:
            result = session.cancel()
            self.assertTrue(result["cancelled"])
            self.assertIn("已停止本次观察器", result["notes"][0])
            self.assertIsNotNone(process.poll())
            self.assertFalse(runtime_identity.owns(process.pid, recording["network_capture"]["owner"]))
            self.assertEqual((folder / "events.jsonl").read_bytes(), original)
            self.assertFalse(session.status()["recording"])
            self.start_page()
            self.assertTrue(session.cancel()["cancelled"])
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
