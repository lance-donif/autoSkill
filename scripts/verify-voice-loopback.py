"""Explicit real-Chrome WebRTC acceptance; media policies may block delivery, which remains a failure."""
import importlib.util
import json
import math
import os
import shutil
import struct
import sys
import tempfile
import threading
import unittest
import wave
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

spec = importlib.util.spec_from_file_location("voice_ticket_desk", ROOT / "skills/autoskill/examples/ticket-desk/server.py")
ticket_desk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ticket_desk)


def synthetic_audio(file: Path) -> None:
    rate = 48_000
    with wave.open(str(file), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(b"".join(struct.pack("<h", round(math.sin(2 * math.pi * 440 * index / rate) * 8000)) for index in range(rate * 2)))


class VoiceLoopbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ticket_desk.make_server(0)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.tmp = tempfile.TemporaryDirectory()
        cls.audio = Path(cls.tmp.name) / "synthetic-440hz.wav"
        synthetic_audio(cls.audio)
        cls.playwright = sync_playwright().start()
        executable = os.environ.get("AUTOSKILL_CHROMIUM") or shutil.which("chromium") or shutil.which("google-chrome")
        cls.executable = executable
        cls.browser = cls.playwright.chromium.launch(executable_path=executable, headless=True,
                                                   args=["--no-sandbox", "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
                                                         f"--use-file-for-fake-audio-capture={cls.audio}", "--autoplay-policy=no-user-gesture-required"])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        self.context = self.browser.new_context(permissions=["microphone"])
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.goto(self.base + "/voice.html")

    def record(self, label, value):
        target = os.environ.get("AUTOSKILL_VOICE_EVIDENCE")
        if target:
            folder = Path(target)
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{self._testMethodName}-{label}.json").write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

    def connect(self, source="microphone"):
        self.page.get_by_test_id("voice-source").select_option(source)
        self.page.get_by_test_id("voice-start").click()
        self.page.wait_for_function("""() => { const state = voiceHarness.snapshot();
            return state.status === 'error' || (state.status === 'connected' && state.bytesReceived > 0 && state.peakRms > 0.005); }""", timeout=20_000)
        snapshot = self.page.evaluate("voiceHarness.snapshot()")
        self.record(source, {"state": snapshot, "error": self.page.get_by_test_id("voice-error").text_content(), "browser_version": self.browser.version})
        self.assertEqual(snapshot["status"], "connected", self.page.get_by_test_id("voice-error").text_content())
        self.assertEqual(snapshot["localAudioTracks"], 1)
        self.assertEqual(snapshot["remoteAudioTracks"], 1)
        self.assertGreater(snapshot["packetsReceived"], 0)
        self.assertGreater(snapshot["totalAudioEnergy"], 0)
        self.assertEqual(self.page.evaluate("Array.from(document.querySelector('audio').srcObject.getAudioTracks(), track => track.readyState)"), ["live"])
        return snapshot

    def disconnect(self):
        self.page.get_by_test_id("voice-stop").click()
        self.page.wait_for_function("voiceHarness.snapshot().status === 'stopped'")
        snapshot = self.page.evaluate("voiceHarness.snapshot()")
        self.record("cleanup", snapshot)
        self.assertTrue(all(snapshot["cleanup"].values()))
        self.assertEqual(snapshot["sender"], "closed")
        self.assertEqual(snapshot["receiver"], "closed")
        self.assertIsNone(self.page.evaluate("document.querySelector('audio').srcObject"))
        self.assertTrue(self.page.get_by_test_id("voice-start").is_enabled())
        return snapshot

    def test_fake_microphone_permission_real_audio_receipt_and_cleanup(self):
        before = self.connect()
        self.assertEqual(before["source"], "microphone")
        self.assertEqual(before["permission"], "granted")
        after = self.disconnect()
        self.assertGreaterEqual(after["bytesReceived"], before["bytesReceived"])

    def test_synthetic_source_and_restart_release_each_connection(self):
        first = self.connect("synthetic")
        self.assertEqual(first["permission"], "unused")
        self.disconnect()
        second = self.connect("microphone")
        self.assertEqual(second["permission"], "granted")
        self.disconnect()

    def test_permission_denial_reports_failure_and_releases_peers(self):
        browser = self.playwright.chromium.launch(executable_path=self.executable, headless=True, args=["--no-sandbox", "--use-fake-device-for-media-stream"])
        self.addCleanup(browser.close)
        denied = browser.new_context()
        self.addCleanup(denied.close)
        page = denied.new_page()
        cdp = denied.new_cdp_session(page)
        cdp.send("Browser.setPermission", {"permission": {"name": "microphone"}, "setting": "denied", "origin": self.base,
                                          "browserContextId": cdp.send("Target.getTargetInfo")["targetInfo"]["browserContextId"]})
        page.goto(self.base + "/voice.html")
        page.get_by_test_id("voice-start").click()
        page.wait_for_function("voiceHarness.snapshot().status === 'error'")
        snapshot = page.evaluate("voiceHarness.snapshot()")
        self.record("denied", snapshot)
        self.assertEqual(snapshot["permission"], "denied")
        self.assertTrue(all(snapshot["cleanup"].values()))
        self.assertTrue(page.get_by_test_id("voice-error").is_visible())
        self.assertEqual(json.loads(page.get_by_test_id("voice-details").text_content())["bytesReceived"], 0)

    def test_microphone_track_is_released_on_stop_or_media_failure(self):
        self.page.add_init_script("""const original = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
            window.voiceObservedTracks = [];
            navigator.mediaDevices.getUserMedia = async (constraints) => {
              const stream = await original(constraints);
              window.voiceObservedTracks = stream.getAudioTracks().map(track => ({track, initialState: track.readyState}));
              return stream;
            };""")
        self.page.reload()
        self.page.get_by_test_id("voice-start").click()
        self.page.wait_for_function("window.voiceObservedTracks.length === 1")
        self.assertEqual(self.page.evaluate("voiceObservedTracks[0].initialState"), "live")
        if self.page.get_by_test_id("voice-stop").is_enabled():
            self.page.get_by_test_id("voice-stop").click()
        self.page.wait_for_function("voiceHarness.snapshot().cleanup !== null")
        self.assertEqual(self.page.evaluate("voiceObservedTracks[0].track.readyState"), "ended")
        self.assertTrue(all(self.page.evaluate("voiceHarness.snapshot().cleanup").values()))
        self.record("released", self.page.evaluate("voiceHarness.snapshot()"))


if __name__ == "__main__":
    unittest.main()
