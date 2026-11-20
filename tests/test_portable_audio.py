"""Real WebSocket audio remains readable after relocation without rewriting hashed native evidence."""
import copy
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from websockets.sync.server import serve

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill import data_refs, db, native_assets, native_reader, native_run, paging, paths, projects, task_report_data
from autoskill.engines import ai_audio


class PortableAudioTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.a, self.b = self.root / "original-data", self.root / "copied-data"
        environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.a), "AUTOSKILL_SESSION": "", "AUTOSKILL_HEADED": "0"})
        environment.start()
        self.addCleanup(environment.stop)

    def receive(self, file):
        state = SimpleNamespace(endpoint={"_audio_output": str(file)}, touch=lambda: None, first_audio=None, first_content=None,
                                last_content=None, audio_received={"bytes": 0, "chunks": 0}, audio_hasher=hashlib.sha256())
        ai_audio.receive(state, b"first")
        ai_audio.receive(state, b"second")
        return state.audio_received

    def test_managed_output_is_relative_and_explicit_external_protocol_output_stays_accessible(self):
        inside = self.a / ".local/runs/audio/native/audio/output.pcm"
        internal = self.receive(inside)
        self.assertEqual(internal["path"], ".local/runs/audio/native/audio/output.pcm")
        self.assertEqual(data_refs.resolve(internal["path"]).read_bytes(), b"firstsecond")
        external = self.receive(self.root / "standalone-protocol/output.pcm")
        self.assertTrue(Path(external["path"]).is_absolute())
        self.assertEqual(Path(external["path"]).read_bytes(), b"firstsecond")
        self.assertEqual(internal["sha256"], external["sha256"])

    def test_projection_traverses_only_transport_turns_and_attempts_without_changing_business_paths(self):
        file = self.a / "audio/output.pcm"
        file.parent.mkdir(parents=True)
        file.write_bytes(b"audio")
        audio = {"path": "audio/output.pcm", "sha256": hashlib.sha256(b"audio").hexdigest()}
        business = {"path": "audio/output.pcm", "audio_received": copy.deepcopy(audio)}
        original = {"turns": [{"audio_received": audio, "response": {"body": business}, "attempts": [{"outcome": {"turns": [{"audio_received": audio}]}}]}]}
        before = copy.deepcopy(original)
        view = native_reader._audio_paths(original)
        turn = view["turns"][0]
        self.assertEqual(turn["audio_received"]["path"], str(file))
        self.assertEqual(turn["attempts"][0]["outcome"]["turns"][0]["audio_received"]["path"], str(file))
        self.assertEqual(turn["response"]["body"], business)
        self.assertEqual(original, before)

    @staticmethod
    def echo(socket):
        json.loads(socket.recv())
        for message in socket:
            if isinstance(message, bytes):
                socket.send(message)
            elif json.loads(message).get("type") == "end_audio":
                socket.send(json.dumps({"delta": "received"}))
                socket.send(json.dumps({"done": True}))
                return

    def executed_audio(self, endpoint):
        project = projects.create("Portable voice", "http://" + endpoint.removeprefix("ws://"), project_id="voice")["id"]
        source = self.root / "audio-asset"
        source.mkdir()
        data = bytes(range(64)) * 8
        (source / "fixture.pcm").write_bytes(data)
        spec = {"endpoint": {"url": endpoint, "protocol": "websocket", "audio_input": {"file": "fixture.pcm", "chunk_bytes": 128,
                                                                                                   "end_event": {"type": "end_audio"}}},
                "samples": [{"id": "voice", "question": "voice", "expected": "received"}]}
        (source / "spec.json").write_text(json.dumps(spec))
        asset = native_assets.import_asset(project, str(source), "ai-eval")
        run = native_run.enqueue(project, asset["id"])
        self.assertEqual(native_run.execute(run["id"])["status"], "Pass")
        raw = native_run.get(run["id"])
        audio = raw["results"][0]["detail"]["turns"][0]["audio_received"]
        self.assertFalse(data_refs.is_absolute(audio["path"]))
        self.assertEqual(data_refs.resolve(audio["path"]).read_bytes(), data)
        self.assertEqual(audio["sha256"], hashlib.sha256(data).hexdigest())
        return run, data

    def legacy_fixture(self, run):
        """Reconstruct the historical absolute-path representation before testing read-only relocation."""
        folder = paths.local() / "runs" / run["id"] / "native"
        report, outcome_file = folder / "ai-evaluation.json", folder / "outcome.json"
        documents = [json.loads(file.read_bytes()) for file in (report, outcome_file)]
        for document in documents:
            for case in document["cases"]:
                for turn in case["turns"]:
                    audio = turn["audio_received"]
                    audio["path"] = str(data_refs.resolve(audio["path"]))
        report.write_text(json.dumps(documents[0]))
        reference = {"path": "ai-evaluation.json", "sha256": hashlib.sha256(report.read_bytes()).hexdigest(), "size": report.stat().st_size}
        documents[1]["evidence"] = [reference]
        outcome_file.write_text(json.dumps(documents[1]))
        row = db.one("SELECT * FROM results WHERE run_id = ?", (run["id"],))
        detail = json.loads(row["detail"])
        detail.update(documents[1]["cases"][0], evidence=[reference])
        self.assertFalse(task_report_data._native_row_evidence(run, documents[1], {**row, "detail": detail}))
        # Absolute audio paths predate the index and digest that bind modern rows to an exact case.
        detail.pop("native_case_index")
        detail.pop("native_case_hash")
        db.update("results", row["id"], {"detail": detail})
        marker = self.a / "data-root.json"
        metadata = json.loads(marker.read_bytes()) if marker.is_file() else {"schema_version": 1, "data_id": "portable-voice"}
        metadata["legacy_roots"] = [str(self.a)]
        marker.write_text(json.dumps(metadata))
        return folder, detail

    def test_real_websocket_old_audio_paths_rebase_for_case_and_all_after_copy(self):
        with serve(self.echo, "127.0.0.1", 0) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                run, audio_bytes = self.executed_audio(f"ws://127.0.0.1:{server.socket.getsockname()[1]}/voice")
            finally:
                server.shutdown()
                thread.join(timeout=3)
        folder, detail = self.legacy_fixture(run)
        before = {file.relative_to(self.a).as_posix(): file.read_bytes() for file in folder.rglob("*") if file.is_file()}
        revision = paging.revision(detail)
        audio_path = detail["turns"][0]["audio_received"]["path"]
        shutil.copytree(self.a, self.b)
        os.environ["AUTOSKILL_WORKSPACE"] = str(self.b)
        Path(audio_path).write_bytes(b"old location must not be used")
        for original_present in (True, False):
            with self.subTest(original_present=original_present):
                if not original_present:
                    shutil.rmtree(self.a)
                raw = native_run.get(run["id"])
                self.assertEqual(raw["results"][0]["detail"]["turns"][0]["audio_received"]["path"], audio_path)
                view = native_reader.status(run["id"], case_id=detail["id"])
                self.assertEqual(view["content_revision"], revision)
                projected = Path(view["case"]["turns"][0]["audio_received"]["path"])
                self.assertTrue(projected.is_relative_to(self.b))
                self.assertEqual(projected.read_bytes(), audio_bytes)
                full = native_reader.status(run["id"], all_items=True)
                self.assertEqual(full["page"]["content_revision"], paging.revision(raw))
                self.assertEqual(full["results"][0]["detail"]["turns"][0]["audio_received"]["path"], str(projected))
                self.assertEqual(full["outcome"]["cases"][0]["turns"][0]["audio_received"]["path"], str(projected))
                stored_run = db.one("SELECT * FROM runs WHERE id = ?", (run["id"],))
                self.assertTrue(task_report_data.run_snapshot(stored_run)["cases"][0]["evidence_valid"])
        self.assertEqual({name: (self.b / name).read_bytes() for name in before}, before)


if __name__ == "__main__":
    unittest.main()
