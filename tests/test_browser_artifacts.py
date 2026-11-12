"""Portable asset and native bookkeeping checks; all browser calls are mocked."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))
from autoskill import artifacts, paths, secrets
from autoskill.recorder import native


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, AUTOSKILL_WORKSPACE=self.temp.name)
        self.env.start()
        self.project = Path(self.temp.name) / "projects" / "portable"
        self.project.mkdir(parents=True)
        (self.project / "project.json").write_text('{"id":"portable"}')

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_upload_snapshot_is_portable_and_hash_verifiable(self):
        source = Path(self.temp.name) / "outside project.bin"
        source.write_bytes(b"portable upload contents")
        names = artifacts.stage_upload("portable", [source])
        self.assertEqual(len(names), 1)
        self.assertTrue(names[0].startswith("files/uploads/"))
        staged = Path(artifacts.resolve_upload_assets(self.project, names)[0])
        manifest = (self.project / "files/uploads/manifest.json").read_text()
        self.assertNotIn(str(source), manifest)
        self.assertEqual(json.loads(manifest)["assets"][names[0]]["sha256"], artifacts.sha256(source))
        source.write_bytes(b"changed source")
        self.assertEqual(staged.read_bytes(), b"portable upload contents")

    def test_uploaded_file_can_be_staged_again_by_project_name(self):
        source = Path(self.temp.name) / "sample.txt"
        source.write_text("sample")
        first = artifacts.stage_upload("portable", [source])
        second = artifacts.stage_upload("portable", first)
        self.assertEqual(first, second)
        self.assertEqual(Path(artifacts.resolve_upload_assets(self.project, second)[0]).read_text(), "sample")

    def test_windows_traversal_variables_and_symlink_escapes_are_rejected(self):
        for name in ("../escape", "/tmp/escape", "C:/escape.txt", "C:escape.txt", "a\\b", "a/../b", "${ENV:FILE}",
                     "{{RAND}}.txt", "a//b", "a/./b", "file.txt:stream"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                artifacts.resolve_upload_assets(self.project, [name])
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (outside / "secret").write_text("not an asset")
        try:
            (self.project / "linked").symlink_to(outside, target_is_directory=True)
        except OSError:
            return
        with self.assertRaises(ValueError):
            artifacts.resolve_upload_assets(self.project, ["linked/secret"])
        with self.assertRaises(ValueError):
            artifacts.resolve_output(self.project, "linked/new.txt")

    def test_private_outputs_and_metadata_do_not_expose_host_paths(self):
        rec = {"project_id": "portable"}
        file = artifacts.private_output("session", "reports/page.pdf", suffix=".pdf", rec=rec)
        self.assertTrue(file.is_relative_to(paths.local()))
        file.write_bytes(b"%PDF-example")
        meta = artifacts.metadata(file, kind="pdf")
        self.assertEqual(meta["sha256"], artifacts.sha256(file))
        self.assertEqual(meta["bytes"], 12)
        self.assertFalse(Path(meta["path"]).is_absolute())
        with self.assertRaises(ValueError):
            artifacts.private_output("session", str(Path(self.temp.name) / "public.pdf"))


class NativeTests(ArtifactTests):
    def setUp(self):
        super().setUp()
        self.rec = {"project_id": "portable", "secrets": []}

    def test_classifiers_cover_real_native_command_path_shapes(self):
        cases = [
            (["screenshot", "--threshold", "0.1", "page.png"], ["page.png"]),
            (["screenshot", "#main", "page.png"], ["page.png"]),
            (["screenshot", "#main"], []),
            (["record", "start", "demo.webm", "https://example.test", "--fps", "60"], ["demo.webm"]),
            (["wait", "--download", "file.pdf", "--timeout", "30000"], ["file.pdf"]),
            (["diff", "screenshot", "--baseline", "old.png", "--output=new.png"], ["new.png"]),
            (["batch", "screenshot --full image.png", "pdf report.pdf"], ["image.png", "report.pdf"]),
        ]
        for argv, wanted in cases:
            with self.subTest(argv=argv):
                self.assertEqual(native.output_operands(argv), wanted)
        self.assertEqual(native.upload_input_paths(["--state", "auth.json", "wait", "#ready", "--state", "visible"]), ["auth.json"])
        self.assertEqual(native.upload_input_paths(["cookies", "set", "--curl", "cookies.curl"]), ["cookies.curl"])
        self.assertEqual(native.upload_input_paths(["batch", "upload '#input' first.txt second.txt", "state load auth.json"]),
                         ["first.txt", "second.txt", "auth.json"])
        self.assertEqual(native.output_operands(["screenshot", "--screenshot-dir=one", "--screenshot-dir", "two"]), ["one", "two"])
        self.assertEqual(native.upload_input_paths(["fill", "#field", "--", "--config", "literal.json"]), [])

    def test_recording_guards_nested_lifecycle_but_preserves_literal_values(self):
        for argv in (["close"], ["network", "har", "stop"], ["batch", "screenshot", "close"],
                     ["--session", "other", "get", "url"], ["open", "https://example.test", "--relaunch"]):
            with self.subTest(argv=argv), self.assertRaises(ValueError):
                native.guard_recording(argv, self.rec)
        for argv in (["fill", "#field", "--state"], ["fill", "#field", "--", "--session"],
                     ["keyboard", "type", "--profile"], ["wait", "--text", "--profile"],
                     ["wait", "#ready", "--state", "visible"]):
            native.guard_recording(argv, self.rec)

    def test_unsupported_mutations_are_explicit_and_shared_portable_subset_matches(self):
        with patch.object(native.agent, "call", return_value={}):
            network = native.execute("session", ["network", "route", "*", "--abort"], self.rec)
            wait = native.execute("session", ["wait", "--text", "Ready"], self.rec)
            viewport = native.execute("session", ["set", "viewport", "1280", "720"], self.rec)
        self.assertEqual(network["step"]["action"], "native_step")
        self.assertFalse(network["step"]["replayable"])
        self.assertEqual(wait["step"]["action"], "native_step")
        self.assertEqual(viewport["step"]["action"], "browser_command")

    def test_cookie_header_storage_values_and_clipboard_shorthand_remain_refs(self):
        secrets.set_secret("CLIP", "private clipboard value")
        with patch.object(native.agent, "call", return_value={}) as call:
            clipboard = native.execute("session", ["clipboard", "write", "secret:CLIP"], self.rec)
            self.assertEqual(call.call_args.args[1], ["clipboard", "write", "private clipboard value"])
            self.assertEqual(clipboard["step"]["text"], "${ENV:CLIP}")
            for argv, value in ((["cookies", "set", "session", "cookie-private"], "cookie-private"),
                                (["set", "headers", '{"Authorization":"Bearer header-private"}'], "header-private"),
                                (["storage", "local", "set", "session", "storage-private"], "storage-private")):
                result = native.execute("session", argv, self.rec)
                self.assertEqual(result["step"]["action"], "browser_command")
                self.assertNotIn(value, json.dumps(result["step"]))
                self.assertTrue(secrets.references(result["step"]))

    def test_capture_outputs_are_private_and_observation_only(self):
        def call(_session, argv, **_kwargs):
            Path(argv[1]).write_bytes(b"%PDF-example")
            return {"path": argv[1]}
        with patch.object(native.agent, "call", side_effect=call):
            result = native.execute("session", ["pdf", "report.pdf"], self.rec)
        self.assertIsNone(result["step"])
        self.assertEqual(result["artifacts"][0]["sha256"], artifacts.sha256(paths.local() / result["result"]["path"]))
        self.assertNotIn(self.temp.name, json.dumps(self.rec))

    def test_state_save_is_private_plain_json_and_load_uses_transient_file_and_reference(self):
        raw_state = '{"cookies":[{"name":"session","value":"state-private"}],"origins":[]}'
        def save(_session, argv, **_kwargs):
            Path(argv[2]).write_text(raw_state)
            return {"path": argv[2]}
        with patch.object(native, "_raw_call", side_effect=save):
            result = native.execute("session", ["state", "save", "auth.json"], self.rec)
        file = paths.local() / result["artifacts"][0]["path"]
        self.assertEqual(json.loads(file.read_text()), json.loads(raw_state))
        self.assertEqual(file.stat().st_mode & 0o777, 0o600)
        self.assertFalse(result["result"]["encrypted"])
        seen = []
        def load(_session, argv, **_kwargs):
            seen.append(Path(argv[2]))
            self.assertEqual(seen[-1].read_text(), raw_state)
            return {}
        with patch.object(native, "_raw_call", side_effect=load):
            loaded = native.execute("session", ["state", "load", str(file)], self.rec)
        self.assertEqual(loaded["step"]["action"], "native_step")
        self.assertNotIn("state-private", json.dumps(loaded["step"]))
        self.assertNotIn(self.temp.name, json.dumps(loaded["step"]))
        self.assertFalse(seen[-1].exists())
        self.assertTrue(loaded["affects_page"])

    def test_record_stop_hashes_encoder_output_across_standalone_invocations(self):
        video = []
        def call(_session, argv, **_kwargs):
            if argv[:2] == ["record", "start"]:
                video.append(Path(argv[2]))
                return {"path": argv[2]}
            if argv[:2] == ["record", "stop"]:
                video[-1].write_bytes(b"video encoder output")
                return {"path": str(video[-1])}
            raise AssertionError(argv)
        with patch.object(native.agent, "call", side_effect=call), patch.object(native.shutil, "which", return_value="ffmpeg"):
            started = native.execute("standalone", ["record", "start", "demo.webm"])
            self.assertIn("artifact_pending", started["result"])
            ended = native.execute("standalone", ["record", "stop"])
        self.assertEqual(ended["artifacts"][0]["sha256"], artifacts.sha256(video[-1]))
        self.assertFalse(Path(ended["artifacts"][0]["path"]).is_absolute())

    def test_standalone_state_load_and_open_use_identical_browser_launch_settings(self):
        file = Path(self.temp.name) / "state.json"
        file.write_text('{"cookies":[],"origins":[]}')
        flags = ["--executable-path", "/configured/chrome", "--args", "quiet"]
        with patch.object(native.agent, "launch_args", return_value=flags), patch.object(native, "_raw_call", return_value={}) as raw, \
                patch.object(native.agent, "call", return_value={}) as call:
            native.execute("standalone", ["state", "load", str(file)])
            native.execute("standalone", ["open", "http://localhost:9000"])
        self.assertEqual(raw.call_args.kwargs["launch"], flags)
        self.assertEqual(raw.call_args.kwargs["launch"], call.call_args.kwargs["launch"])

    def test_mcp_stdio_is_an_explicit_unexecuted_transport_descriptor(self):
        with patch.object(native.agent, "call") as call, patch.object(native, "_raw_call") as raw:
            result = native.execute("session", ["mcp", "--tools", "all"], self.rec)
        call.assert_not_called()
        raw.assert_not_called()
        self.assertFalse(result["result"]["executed"])
        self.assertEqual(result["result"]["transport"], "stdio")


if __name__ == "__main__":
    unittest.main()
