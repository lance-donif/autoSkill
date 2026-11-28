"""Offline trimming must preserve runtime code, target binaries, resources and notices."""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("bundle_builder", Path(__file__).resolve().parents[1] / "scripts/build-windows-bundle.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class BundlePruning(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def file(self, name, content="fixture"):
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def test_chrome_retains_chinese_english_browser_and_media_assets(self):
        self.file("locales/en-US.pak")
        self.file("locales/zh-CN.pak")
        obsolete = self.file("locales/fr.pak")
        media = self.file("WidevineCdm/cdm.so")
        builder.prune_chrome_locales(self.root)
        self.assertEqual(sorted(p.name for p in (self.root / "locales").glob("*.pak")), ["en-US.pak", "zh-CN.pak"])
        self.assertFalse(obsolete.exists())
        self.assertTrue(media.exists())

    def test_trim_preserves_runtime_targets_and_updates_original_wheel_records(self):
        manager = self.file("selenium/webdriver/common/windows/selenium-manager.exe")
        linux = self.file("selenium/webdriver/common/linux/selenium-manager")
        mac = self.file("selenium/webdriver/common/macos/selenium-manager")
        fixture = self.file("numpy/_core/tests/data/fixture.npy")
        runtime = self.file("numpy/testing/_private/utils.py")
        unrelated = self.file("native_extension/test/runtime.py")
        license_file = self.file("numpy-2.dist-info/licenses/LICENSE")
        record = self.file("numpy-2.dist-info/RECORD", "numpy/_core/tests/data/fixture.npy,sha256=demo,10\nnumpy/testing/_private/utils.py,,\n")
        builder.prune_site_packages(self.root, "windows-x64")
        self.assertTrue(all(p.exists() for p in (manager, runtime, unrelated, license_file)))
        self.assertFalse(any(p.exists() for p in (linux, mac, fixture)))
        self.assertEqual(record.read_text(), "numpy/testing/_private/utils.py,,\n")
