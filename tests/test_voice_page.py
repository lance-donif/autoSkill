"""Portable microphone permission and resource-release regressions, independent of ICE availability."""
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_tests(loader, tests, pattern):
    spec = importlib.util.spec_from_file_location("voice_acceptance", ROOT / "scripts/verify-voice-loopback.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return unittest.TestSuite([module.VoiceLoopbackTests(name) for name in (
        "test_permission_denial_reports_failure_and_releases_peers",
        "test_microphone_track_is_released_on_stop_or_media_failure",
    )])
