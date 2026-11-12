"""Saved protocol cases -> prerequisite runner -> private audio -> explicit knowledge."""
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))
from autoskill import cli, db, knowledge, projects, protocols_cli, runner
import test_streaming_protocols as fixture


class ProtocolRunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.StreamingProtocolTests.setUpClass()
        cls.base = fixture.StreamingProtocolTests.base

    @classmethod
    def tearDownClass(cls):
        fixture.StreamingProtocolTests.tearDownClass()

    def test_saved_stream_and_two_voice_cases_preserve_evidence_and_accumulate_knowledge(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.dict(os.environ, AUTOSKILL_WORKSPACE=str(root / "workspace"),
                            AUTOSKILL_CASE_SKILL=str(root / "case-skill"), AUTOSKILL_HEADED="0"):
                pid = projects.create("Protocol runner", self.base, project_id="protocol-run")["id"]
                sse = protocols_cli.build_case(pid, {"title": "done SSE", "path": "/sse", "transport": {
                    "type": "sse", "timeout_seconds": 3, "assertions": {"text_contains": ["最终答案"]}}})
                projects.save_cases(pid, [sse])
                cases, samples = [sse], []
                for number in (100, 200):
                    sample = root / f"tone-{number}.wav"
                    content = struct.pack("<h", number) * 640
                    samples.append(content)
                    with wave.open(str(sample), "wb") as stream:
                        stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                        stream.writeframes(content)
                    case = protocols_cli.build_case(pid, {"title": "done voice " + str(number), "path": "/ws",
                        "extensions": {"depends_on": [cases[-1]["id"]]}, "transport": {
                            "type": "websocket", "timeout_seconds": 3,
                            "steps": [{"send_audio": {"file": str(sample), "encoding": "base64"}},
                                      {"send": {"type": "input_audio_buffer.commit"}},
                                      {"receive": {"until": "response.done"}}],
                            "audio_output": {"filename": "reply.wav", "sample_rate": 16000},
                            "assertions": {"min_audio_bytes": len(content), "min_audio_frames": 1}}})
                    projects.save_cases(pid, [case])
                    cases.append(case)
                run = runner.create(pid, [cases[-1]["id"]])
                final = runner.execute(run["id"])
                rows = db.rows("SELECT case_id, status, detail, prerequisite FROM results WHERE run_id=? ORDER BY id", (run["id"],))
                self.assertEqual([row["case_id"] for row in rows], [case["id"] for case in cases])
                self.assertEqual([row["status"] for row in rows], ["Pass"] * 3, rows)
                self.assertEqual((final["status"], final["total"], final["passed"]), ("Pass", 1, 1))
                details = [json.loads(row["detail"]) for row in rows]
                self.assertEqual([detail["transport"] for detail in details], ["sse", "websocket", "websocket"])
                for case, sample, detail in zip(cases[1:], samples, details[1:]):
                    self.assertEqual(detail["metrics"]["sent_audio_bytes"], len(sample))
                    self.assertEqual(len(detail["artifacts"]), 1)
                    file = runner.evidence_dir(run["id"]) / "artifacts" / case["id"] / "reply.wav"
                    with wave.open(str(file), "rb") as stream:
                        self.assertEqual(stream.readframes(640), sample)
                    self.assertFalse(file.is_relative_to(projects.path(pid)))
                # A passed run does not silently turn model/server output into trusted knowledge.
                self.assertEqual(knowledge.search(pid, "done-contract")["notes"], [])
                saved = knowledge.add(pid, "done-contract", "SSE waits for [DONE]; voice waits for response.done.",
                                      case_ids=[case["id"] for case in cases], run_ids=[run["id"]])
                note = knowledge.show(pid, saved["file"])
                self.assertEqual(note["metadata"]["runs"][0]["status"], "Pass")
                self.assertIn("response.done", note["content"])
                context = cli.context(pid, query="done-contract")
                self.assertEqual(context["knowledge"][0]["file"], saved["file"])
                self.assertLess(len(json.dumps(context)), 20000)


if __name__ == "__main__":
    unittest.main()
