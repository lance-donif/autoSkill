"""Bridge file boundaries cover native captures and streaming request assets."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))
from autoskill import artifacts, bridge, paths, projects, secrets


class BridgeCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.caller = self.root / "caller"
        self.caller.mkdir()
        self.env = patch.dict(os.environ, AUTOSKILL_WORKSPACE=str(self.workspace))
        self.env.start()
        projects.create("Bridge", "http://127.0.0.1:1", project_id="bridge")
        self.audio = self.caller / "voice.pcm"
        self.audio.write_bytes(b"\x00\x01" * 10)

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_all_native_capture_outputs_and_global_directory_flags_are_checked(self):
        outside = str(self.root / "outside")
        commands = [
            ["screenshot", "--full", outside + ".png"], ["screenshot", "#main", outside + ".png"],
            ["pdf", outside + ".pdf"], ["download", "#download", outside + ".csv"],
            ["record", "start", outside + ".webm"], ["record", "restart", outside + ".mp4"],
            ["trace", "stop", outside + ".json"], ["profiler", "stop", outside + ".json"],
            ["network", "har", "stop", outside + ".har"], ["state", "save", outside + ".json"],
            ["wait", "--download", outside + ".pdf", "--timeout", "60000"],
            ["diff", "screenshot", "--baseline", str(self.audio), "--output=" + outside + ".png"],
            ["--screenshot-dir", outside, "screenshot"], ["--download-path=" + outside, "read", "https://example.test"],
            ["screenshot", "--screenshot-dir=shots", "--screenshot-dir=" + outside],
        ]
        with patch.object(bridge.subprocess, "run") as run:
            for command in commands:
                with self.subTest(command=command), self.assertRaises(ValueError):
                    bridge.execute(["browser", *command])
            run.assert_not_called()
        self.assertFalse(Path(outside).exists())

    def test_screenshot_selectors_and_option_values_are_not_mistaken_for_outputs(self):
        for command in (["screenshot", "#main"], ["screenshot", "--threshold", "0.01"],
                        ["screenshot", "--screenshot-format", "jpeg", "--screenshot-quality", "85"],
                        ["screenshot", "#main", "page.png"], ["wait", "#ready", "--state", "visible"]):
            with self.subTest(command=command):
                self.assertIsNone(bridge.check(["browser", *command]))

    def test_nested_batches_cannot_hide_outside_output_or_mcp_typed_path(self):
        outside = str(self.root / "outside.png")
        for command in (["batch", "get title", "pdf " + shlex.quote(outside)],
                        ["batch", shlex.join(["batch", shlex.join(["screenshot", outside])])],
                        ["batch", shlex.join(["mcp", "call", "agent_browser_pdf", json.dumps({"path": outside})])]):
            with self.subTest(command=command), self.assertRaises(ValueError):
                bridge.check(["browser", *command])

    def test_workspace_output_boundary_and_symlink_resolution_are_preserved(self):
        for command in (["report", "bridge", "--out", "reports"],
                        ["browser", "pdf", str(self.workspace / "page.pdf")],
                        ["browser", "pdf", str(paths.local() / "page.pdf")]):
            bridge.check(command)
        for command in (["report", "bridge", "--out", "../outside"], ["browser", "pdf", "../outside.pdf"],
                        ["browser", "screenshot", "C:/outside.png"]):
            with self.subTest(command=command), self.assertRaises(ValueError):
                bridge.check(command)
        linked = self.workspace / "link"
        try:
            linked.symlink_to(self.caller, target_is_directory=True)
        except OSError:
            return
        with self.assertRaises(ValueError):
            bridge.check(["browser", "pdf", str(linked / "escape.pdf")])

    def test_public_and_localhost_urls_have_no_extra_bridge_restrictions(self):
        for command in (["browser", "read", "https://example.com/docs"], ["browser", "open", "http://localhost:9000"],
                        ["browser", "open", "http://127.0.0.1:9000"], ["project", "create", "--name", "public", "--url", "https://example.com"]):
            self.assertIsNone(bridge.check(command))

    def test_forwarding_rewrites_file_operands_without_rewriting_selectors_or_outputs(self):
        plain = bridge.prepare_argv(["browser", "upload", "#file", "voice.pcm"], self.caller)
        self.assertEqual(plain, ["browser", "upload", "#file", str(self.audio)])
        nested = bridge.prepare_argv(["browser", "batch", "upload '#file' voice.pcm", "screenshot report.png"], self.caller)
        self.assertEqual(shlex.split(nested[2]), ["upload", "#file", str(self.audio)])
        self.assertEqual(shlex.split(nested[3]), ["screenshot", "report.png"])
        script = bridge.prepare_argv(["browser", "eval", "--script-file", "voice.pcm"], self.caller)
        self.assertEqual(script[-1], str(self.audio))
        literal = ["browser", "fill", "#field", "--", "--config", "voice.pcm"]
        self.assertEqual(bridge.prepare_argv(literal, self.caller), literal)
        mcp = bridge.prepare_argv(["browser", "mcp", "call", "agent_browser_upload", '{"selector":"#file","files":["voice.pcm"]}'], self.caller)
        self.assertEqual(json.loads(mcp[-1]), {"selector": "#file", "files": [str(self.audio)]})

    def test_spec_path_and_all_repeated_audio_flags_keep_the_callers_location(self):
        arguments = ["api", "request", "bridge", "--file=spec.json", "--audio-input", "voice.pcm", "--audio-input=second.pcm"]
        prepared = bridge.prepare_argv(arguments, self.caller)
        self.assertIn("--file=" + str(self.caller / "spec.json"), prepared)
        self.assertIn(str(self.audio), prepared)
        self.assertIn("--audio-input=" + str(self.caller / "second.pcm"), prepared)
        self.assertEqual(bridge.prepare_argv(["project", "update", "bridge", "--audio-input", "off"], self.caller)[-1], "off")

    def test_staged_project_upload_spelling_survives_forwarding(self):
        staged = artifacts.stage_upload("bridge", [self.audio])[0]
        (paths.local() / "recording.json").write_text('{"project_id":"bridge"}')
        command = ["browser", "upload", "#file", staged]
        self.assertEqual(bridge.prepare_argv(command, self.caller), command)
        bridge.check(command)

    def spec(self, **changes):
        value = {"path": "/voice", "transport": {"type": "websocket", "steps": [{"send_audio": {"file": "voice.pcm"}}],
                                                       "audio_output": {"filename": "reply.wav"}}}
        value["transport"].update(changes)
        path = self.caller / "spec.json"
        path.write_text(json.dumps(value))
        return path

    def test_api_spec_audio_inputs_resolve_beside_spec_without_staging_during_check(self):
        spec = self.spec()
        before = set(projects.path("bridge").rglob("*"))
        bridge.check(["api", "request", "bridge", "--file", str(spec), "--audio-input", str(self.audio)])
        self.assertEqual(before, set(projects.path("bridge").rglob("*")))

    def test_api_audio_output_and_bad_json_fail_before_any_request_or_subprocess(self):
        with patch.object(bridge.subprocess, "run") as run:
            for name in (str(self.root / "reply.wav"), "../reply.wav", "C:/reply.wav", "a\\reply.wav"):
                spec = self.spec(audio_output={"filename": name})
                with self.subTest(name=name), self.assertRaises(ValueError):
                    bridge.execute(["api", "request", "bridge", "--file", str(spec)])
            spec.write_text("not JSON")
            with self.assertRaises(ValueError):
                bridge.execute(["api", "request", "bridge", "--file", str(spec)])
            run.assert_not_called()

    def test_mcp_known_paths_are_checked_but_opaque_behavior_stays_explicitly_unverified(self):
        accepted = ["browser", "mcp", "call", "agent_browser_pdf", json.dumps({"path": str(paths.local() / "page.pdf")})]
        result = bridge.check(accepted)
        self.assertEqual(result["status"], "unverified")
        plugin = bridge.check(["browser", "plugin", "run", "custom", "task.run", "--payload", '{"custom":"schema"}'])
        self.assertEqual(plugin["status"], "unverified")
        self.assertIsNone(bridge.check(["browser", "mcp", "--tools", "all"]))
        with self.assertRaises(ValueError):
            bridge.check(["browser", "mcp", "call", "agent_browser_pdf", json.dumps({"path": str(self.root / "page.pdf")})])
        with self.assertRaises(ValueError):
            bridge.check(["browser", "mcp", "call", "custom_tool", json.dumps({"extraArgs": ["--screenshot-dir", str(self.root)]})])

    def test_mcp_path_reference_is_resolved_before_boundary_check(self):
        secrets.set_secret("OUTPUT", str(self.root / "outside.pdf"))
        with self.assertRaises(ValueError):
            bridge.check(["browser", "mcp", "call", "agent_browser_pdf", '{"path":"${ENV:OUTPUT}"}'])

    def test_unverified_metadata_reaches_the_actual_bridge_json_response(self):
        def run(_argv, *, stdout, stderr, **_kwargs):
            stdout.write(b'{"result":{"success":true}}')
            return SimpleNamespace(returncode=0)
        with patch.object(bridge.subprocess, "run", side_effect=run) as run_mock:
            reply = bridge.execute(["browser", "mcp", "call", "custom_tool", "{}"])
        self.assertEqual(reply["exit"], 0)
        self.assertEqual(json.loads(reply["stdout"])["file_boundary_validation"]["status"], "unverified")
        run_mock.assert_called_once()
        self.assertEqual(list((paths.local() / "bridge-io").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
