"""Real fake-microphone/WebRTC RTP and delayed SSE completion, entirely local."""
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))
os.environ.setdefault("AUTOSKILL_HEADED", "0")
from autoskill import artifacts, media, paths, projects
from autoskill.recorder import agent, session
from autoskill.replay import codegen, ui
from autoskill.replay.playwright_driver import PlaywrightDriver
from autoskill.replay.selenium_driver import SeleniumDriver

spec = importlib.util.spec_from_file_location("autoskill_voice_fixture", ROOT / "tests/fixtures/voice/server.py")
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


def tone(file):
    """Synthetic test input: not recorded speech or a model-quality sample."""
    with wave.open(str(file), "wb") as output:
        output.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
        output.writeframes(b"".join(struct.pack("<h", int(10000 * math.sin(2 * math.pi * 440 * i / 48000))) for i in range(48000)))


class MicrophoneInputValidation(unittest.TestCase):
    def test_only_explicit_project_pcm_wav_enables_test_microphone(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            tone(root / "input.wav")
            config = {"media": {"audio_input_file": "input.wav"}}
            args = media.chrome_args(config, root)
            self.assertIn("--use-fake-device-for-media-stream", args)
            self.assertIn("--use-fake-ui-for-media-stream", args)
            self.assertIn("--use-file-for-fake-audio-capture=" + str(root / "input.wav"), args)
            self.assertEqual(["microphone"], media.permissions(config))
            self.assertEqual([], media.chrome_args({}, root))
            self.assertEqual([], media.permissions({}))
            for invalid in ("../input.wav", str(root / "input.wav"), "C:/input.wav", "missing.wav"):
                with self.subTest(path=invalid), self.assertRaises(ValueError):
                    media.chrome_args({"media": {"audio_input_file": invalid}}, root)
            (root / "bad.wav").write_text("not a WAV")
            with self.assertRaises(ValueError):
                media.chrome_args({"media": {"audio_input_file": "bad.wav"}}, root)


@unittest.skipUnless(paths.agent_browser() and paths.chrome() and paths.chromedriver(), "complete Chrome runtime required")
class RealtimeBrowserVoice(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = fixture.VoiceServer(("127.0.0.1", 0), fixture.VoiceHandler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp.name)
        self.before = {key: os.environ.get(key) for key in ("AUTOSKILL_WORKSPACE", "AUTOSKILL_HEADED")}
        os.environ["AUTOSKILL_WORKSPACE"] = str(self.root / "workspace")
        os.environ["AUTOSKILL_HEADED"] = "0"
        self.pid = projects.create("Local voice lab", self.base, project_id="voice-lab")["id"]
        tone(self.root / "test-tone.wav")
        relative = artifacts.stage_upload(self.pid, [str(self.root / "test-tone.wav")])[0]
        self.config = {**projects.config(self.pid), "timeout_seconds": 1, "assertion_timeout_seconds": 10,
                       "page_load_seconds": 10, "media": {"audio_input_file": relative}}
        from autoskill.core import store
        store.write(projects.path(self.pid) / "qa-config.json", self.config)

    def tearDown(self):
        try:
            session.cancel()
        finally:
            for key, value in self.before.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            self.temp.cleanup()

    def test_wav_microphone_real_rtp_datachannel_long_sse_and_standalone(self):
        session.start(self.pid, login=False)
        session.browser(["click", "#start"])
        try:
            session.check("text", "#state", "Connected", timeout_seconds=12)
        except Exception:
            diagnostics = agent.call(session.session_name(), ["eval", "--stdin"], stdin_text="JSON.stringify(window.voiceReport)")
            report = json.loads(diagnostics.get("result", "{}"))
            # Keep the real transport test executable on an unrestricted full
            # Chrome. This environment's managed Chromium forbids direct ICE;
            # do not change machine policy or replace RTP assertions with mocks.
            policy_file = Path("/etc/chromium/policies/managed/misc.json")
            policy = json.loads(policy_file.read_text()) if policy_file.is_file() else {}
            peers = report.get("peers", [])
            if (policy.get("WebRtcIPHandling") == "disable_non_proxied_udp" and peers
                    and all("a=candidate:" not in peer.get("local", "") for peer in peers)):
                self.skipTest("Blocked: managed Chromium WebRtcIPHandling=disable_non_proxied_udp produces no local ICE candidates; real WAV/RTP test retained")
            self.fail("WebRTC connection failed: " + json.dumps(diagnostics))
        session.check("text", "#transport", "Audio RTP received", timeout_seconds=12)
        session.browser(["fill", "#message", "hello {{RAND}}"])
        session.browser(["click", "#send"])
        session.check("text", "#state", "Thinking", timeout_seconds=1)
        # First token arrives much earlier than the explicit completion signal.
        session.browser(["wait", "#done", "--timeout", "10000", "--stable-for", "200"])
        session.check("text", "#answer", "Local reply: hello {{RAND}}", timeout_seconds=10)
        session.browser(["click", "#stop"])
        session.check("text", "#state", "Stopped", timeout_seconds=3)
        report = agent.call(session.session_name(), ["eval", "--stdin"], stdin_text="JSON.stringify(window.voiceReport)")["result"]
        report = json.loads(report)
        self.assertTrue(report["started"] and report["stopped"] and report["tracksEnded"])
        self.assertGreater(report["sent"], 0)
        self.assertGreater(report["received"], 0)
        self.assertGreater(report["packetsReceived"], 0)
        self.assertGreater(report["energy"], 0)
        self.assertIn("peer:user", report["dataMessages"])
        self.assertIn("client:done", report["dataMessages"])
        self.assertGreater(report["doneAt"] - report["firstTokenAt"], 1500)
        finished = session.stop("Local WAV microphone, RTP, data channel and delayed SSE completion")
        case = next(case for case in projects.cases(self.pid) if case["id"] == finished["ui_case"]["id"])
        self.replay_and_export(case)

    def test_actual_wav_microphone_waveform_capture_replay_and_export(self):
        session.start(self.pid, login=False)
        session.browser(["click", "#capture-microphone"])
        session.check("text", "#microphone", "WAV audio captured", timeout_seconds=10)
        report = json.loads(agent.call(session.session_name(), ["eval", "--stdin"], stdin_text="JSON.stringify(window.micReport)")["result"])
        self.assertGreater(report["rms"], 0.005)
        self.assertGreater(report["sampleRate"], 0)
        self.assertTrue(report["tracksEnded"])
        finished = session.stop("Configured WAV microphone produces real browser audio samples")
        case = next(case for case in projects.cases(self.pid) if case["id"] == finished["ui_case"]["id"])
        self.replay_and_export(case)

    def replay_and_export(self, case):
        relative = self.config["media"]["audio_input_file"]
        portable_input = self.root / "assets" / relative
        portable_input.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(projects.path(self.pid) / relative, portable_input)
        for engine, driver_type in (("playwright", PlaywrightDriver), ("selenium", SeleniumDriver)):
            with self.subTest(engine=engine, phase="replay"):
                config = {**self.config, "_asset_root": str(projects.path(self.pid)), "_output_root": str(self.root / "downloads" / engine)}
                driver = driver_type(config, headless=True)
                try:
                    driver.reset()
                    result = ui.run_case(driver, case, config, self.root / "evidence", engine + ".png")
                    self.assertEqual("Pass", result["status"], json.dumps(result, ensure_ascii=False))
                finally:
                    driver.close()
            with self.subTest(engine=engine, phase="standalone"):
                source = codegen.script([case], self.base, engine, config=self.config)
                self.assertNotIn("from autoskill", source)
                self.assertNotIn(str(projects.path(self.pid)), source)
                file = self.root / ("voice_" + engine + ".py")
                file.write_text(source, encoding="utf-8")
                env = {**paths.child_env(), "HEADED": "0", "CHROME_BINARY": paths.chrome(), "CHROMEDRIVER": paths.chromedriver(),
                       "PROJECT_ASSETS": str(projects.path(self.pid)), "NO_PROXY": "localhost,127.0.0.1,::1",
                       "no_proxy": "localhost,127.0.0.1,::1", "DOWNLOAD_ROOT": str(self.root / "standalone" / engine)}
                done = subprocess.run([sys.executable, str(file)], cwd=self.root, env=env, capture_output=True, text=True, timeout=100)
                self.assertEqual(0, done.returncode, done.stdout + done.stderr)
                self.assertIn("PASS", done.stdout)


if __name__ == "__main__":
    unittest.main()
