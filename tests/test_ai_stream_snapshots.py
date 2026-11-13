"""Final streamed answer snapshots do not duplicate deltas or distort visible-content timing."""
import asyncio
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill.engines import ai_protocols

STREAMS = {
    "/default": [{"delta": "hel"}, {"delta": "lo"}, {"type": "completed", "answer": "hello"}],
    "/explicit": [{"delta": "draft"}, {"type": "completed", "answer": "ignore", "result": {"answer": "correct"}}],
    "/last-delta": [{"delta": "hel"}, {"delta": "lo"}, {"type": "completed", "delta": "!"}],
    "/repeat": [{"delta": "哈"}, {"delta": "哈"}, {"type": "completed", "answer": "哈哈"}],
    "/typed": [{"type": "completed", "answer": {"count": 2, "ready": True}}],
    "/missing-pointer": [{"delta": "hello"}, {"type": "completed", "delta": "!", "answer": "ignore"}],
    "/raw-final": [{"delta": "hel"}, {"delta": "lo"}, "hello"],
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200)
        self.send_header("Content-Type", "application/json" if self.path == "/null" else "text/event-stream")
        self.end_headers()
        if self.path == "/null":
            self.wfile.write(b'{"answer":null}')
            return
        for number, event in enumerate(STREAMS[self.path]):
            prefix = "event: completed\n" if self.path == "/raw-final" and number == 2 else ""
            data = prefix + "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
            self.wfile.write(data.encode("utf-8"))
            self.wfile.flush()


class StreamSnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def request(self, path, **options):
        async def send():
            async with httpx.AsyncClient(timeout=None, trust_env=False) as client:
                return await ai_protocols.request(client, {"url": f"http://127.0.0.1:{self.server.server_port}" + path,
                                                           "protocol": "sse", **options}, {"question": "synthetic"})
        result = asyncio.run(send())
        self.assertEqual(result["error"], "")
        self.assertTrue(result["protocol_complete"])
        return result

    def test_default_final_answer_is_a_snapshot_in_actual_sse(self):
        result = self.request("/default")
        self.assertEqual(result["answer"], "hello")
        self.assertEqual([row["content"] for row in result["events"]], ["hel", "lo", ""])
        final = result["events"][-1]
        self.assertEqual((final["answer"], final["content_mode"], final["content_changed"], final["complete"]), ("hello", "snapshot", False, True))

    def test_configured_final_pointer_takes_priority_over_default_answer_and_prior_delta(self):
        result = self.request("/explicit", final_answer_pointer="/result/answer")
        self.assertEqual(result["answer"], "correct")
        self.assertTrue(result["events"][-1]["content_changed"])

    def test_completion_can_still_carry_the_last_incremental_fragment(self):
        result = self.request("/last-delta")
        self.assertEqual(result["answer"], "hello!")
        self.assertEqual((result["events"][-1]["content"], result["events"][-1]["complete"]), ("!", True))

    def test_repeated_characters_are_not_deduplicated(self):
        self.assertEqual(self.request("/repeat")["answer"], "哈哈")

    def test_structured_and_raw_final_answers_keep_their_declared_shape(self):
        self.assertEqual(self.request("/typed")["answer"], {"count": 2, "ready": True})
        self.assertEqual(self.request("/raw-final", final_answer_pointer="")["answer"], "hello")
        self.assertIsNone(self.request("/null", protocol="http")["answer"])

    def test_missing_custom_snapshot_pointer_does_not_consume_an_unmapped_answer(self):
        result = self.request("/missing-pointer", final_answer_pointer="/result/answer")
        self.assertEqual(result["answer"], "hello!")
        self.assertEqual(result["events"][-1]["content_mode"], "delta")

    def test_repeated_final_snapshot_keeps_content_timestamps_but_records_completion_time(self):
        with patch.object(ai_protocols.time, "monotonic", return_value=10):
            state = ai_protocols.Observation({"protocol": "sse"})
        with patch.object(ai_protocols.time, "monotonic", return_value=11):
            state.event({"delta": "hel"})
        with patch.object(ai_protocols.time, "monotonic", return_value=12):
            state.event({"delta": "lo"})
        with patch.object(ai_protocols.time, "monotonic", return_value=30):
            state.event({"type": "completed", "answer": "hello"})
            result = state.result()
        self.assertEqual(result["timings"]["first_content_ms"], 1000)
        self.assertEqual(result["timings"]["last_content_ms"], 2000)
        self.assertEqual(result["timings"]["protocol_complete_ms"], 20000)
        self.assertEqual(result["events"][-1]["elapsed_ms"], 20000)

    def test_changed_final_snapshot_updates_only_the_last_content_timestamp(self):
        with patch.object(ai_protocols.time, "monotonic", return_value=10):
            state = ai_protocols.Observation({"protocol": "sse"})
        with patch.object(ai_protocols.time, "monotonic", return_value=11):
            state.event({"delta": "draft"})
        with patch.object(ai_protocols.time, "monotonic", return_value=30):
            state.event({"done": True, "answer": "corrected"})
            result = state.result()
        self.assertEqual(result["answer"], "corrected")
        self.assertEqual(result["timings"]["first_content_ms"], 1000)
        self.assertEqual(result["timings"]["last_content_ms"], 20000)


if __name__ == "__main__":
    unittest.main()
