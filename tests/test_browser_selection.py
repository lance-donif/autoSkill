"""Linux setup detects a working pair without overriding the user's browser preference."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))
from autoskill import paths, platforms


@unittest.skipUnless(sys.platform == "linux", "Linux setup selection")
class BrowserSelectionTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.vendor = self.root / "vendor"
        self.bundled = self.binary("vendor/chrome/chrome-linux64/chrome")
        self.driver = self.binary("vendor/chromedriver/chromedriver-linux64/chromedriver")
        self.system = self.binary("system/chrome")
        for context in (patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.root / "workspace")}, clear=True),
                        patch.object(paths, "VENDOR", self.vendor), patch.object(paths, "runtime_target", return_value="linux-x64"),
                        patch.object(paths, "system_chrome", return_value=self.system),
                        patch.object(platforms, "runtime_problems", return_value=[]),
                        patch.object(platforms, "browser_pair", return_value={"ok": True})):
            self.enterContext(context)

    def binary(self, name):
        file = self.root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("fixture: never executed", encoding="utf-8")
        file.chmod(0o755)
        return str(file)

    def test_default_setup_keeps_system_preference_and_matching_selenium_pair(self):
        self.assertTrue(platforms.configure()["ok"])
        self.assertEqual(paths.chrome(), self.system)
        self.assertEqual(paths.chrome(bundled=True), self.bundled)
        self.assertEqual(paths.chromedriver(), self.driver)
        paths.save_settings(browser="bundled")
        self.assertEqual(paths.chrome(), self.bundled)
        paths.save_settings(browser="system")
        self.assertEqual(paths.chrome(), self.system)

    def test_detected_pair_is_still_the_fallback_when_system_chrome_is_missing(self):
        self.assertTrue(platforms.configure()["ok"])
        with patch.object(paths, "system_chrome", return_value=None):
            self.assertEqual(paths.chrome(), self.bundled)

    def test_explicit_setup_choice_survives_system_mode_and_repeated_setup(self):
        self.assertTrue(platforms.configure(self.bundled, self.driver)["ok"])
        paths.save_settings(browser="system")
        self.assertEqual(paths.chrome(), self.bundled)
        self.assertTrue(platforms.configure()["ok"])
        self.assertEqual(paths.chrome(), self.bundled)
        with patch.dict(os.environ, {"AUTOSKILL_CHROMIUM": self.system}):
            self.assertEqual(paths.chrome(), self.system)

    def test_legacy_default_setup_no_longer_masks_system_chrome(self):
        settings = {"chrome": "chrome/chrome-linux64/chrome", "chromedriver": self.driver}
        (self.vendor / "runtime-settings.json").write_text(json.dumps(settings), encoding="utf-8")
        self.assertEqual(paths.chrome(), self.system)
        self.assertEqual(paths.chrome(bundled=True), self.bundled)

    def test_legacy_external_configuration_remains_an_explicit_override(self):
        custom = self.binary("custom/chromium")
        settings = {"chrome": custom, "chromedriver": self.driver}
        (self.vendor / "runtime-settings.json").write_text(json.dumps(settings), encoding="utf-8")
        self.assertEqual(paths.chrome(), custom)
        self.assertTrue(platforms.configure()["ok"])
        self.assertEqual(paths.chrome(), custom)
