"""Architecture dispatch and UOS browser selection; these do not simulate a UOS acceptance run."""
import importlib.util
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))
from autoskill import paths, platforms


class LinuxPlatformTests(unittest.TestCase):
    def test_architecture_aliases_never_fall_back_to_windows(self):
        for machine in ("aarch64", "arm64"):
            self.assertEqual(paths.runtime_target("linux", machine), "linux-arm64")
        for machine in ("x86_64", "AMD64"):
            self.assertEqual(paths.runtime_target("linux", machine), "linux-x64")
        self.assertEqual(paths.runtime_target("linux", "loongarch64"), "linux-loongarch64")
        self.assertEqual(paths.runtime_target("win32", "AMD64"), "windows-x64")

    def test_uos_old_glibc_is_reported_without_changing_system(self):
        info = {"system": "linux", "target": "linux-arm64", "libc": "glibc", "libc_version": "2.28"}
        self.assertEqual(platforms.runtime_problems(info), [])
        self.assertTrue(platforms.runtime_problems({**info, "libc_version": "2.27"}))
        self.assertTrue(platforms.runtime_problems({**info, "libc": "musl"}))
        self.assertTrue(platforms.runtime_problems({**info, "target": "linux-loongarch64"}))

    @unittest.skipUnless(sys.platform == "linux", "Linux path selection")
    def test_arm_native_paths_and_explicit_broken_override(self):
        with tempfile.TemporaryDirectory() as folder:
            vendor = Path(folder)
            native = vendor / "chrome/chrome-linux-arm64/chrome"
            native.parent.mkdir(parents=True)
            native.write_bytes(b"arm fixture")
            native.chmod(0o755)
            with patch.object(paths, "VENDOR", vendor), patch.object(paths, "runtime_target", return_value="linux-arm64"), patch.dict(os.environ, {"AUTOSKILL_BROWSER": "bundled"}, clear=True):
                self.assertEqual(paths.chrome(), str(native))
                with patch.dict(os.environ, {"AUTOSKILL_CHROMIUM": str(vendor / "missing")}):
                    self.assertIsNone(paths.chrome())
                (vendor / "runtime-settings.json").write_text(json.dumps({"chrome": "/usr/local/uos/chromium"}))
                with patch.object(paths, "_first", return_value="selected") as first:
                    self.assertEqual(paths.chrome(), "selected")
                    first.assert_called_once_with(Path("/usr/local/uos/chromium"))

    def test_mismatched_driver_is_not_accepted(self):
        with patch.object(platforms, "binary_status", side_effect=[{"ok": True, "major": 156}, {"ok": True, "major": 157}]):
            self.assertFalse(platforms.browser_pair("chrome", "driver")["ok"])

    def test_old_uos_can_select_system_browser_and_persist_it(self):
        with tempfile.TemporaryDirectory() as folder:
            vendor = Path(folder)
            with patch.object(paths, "VENDOR", vendor), patch.object(platforms, "runtime_problems", return_value=[]), \
                    patch.object(paths, "chrome", return_value="bundled/chrome"), patch.object(paths, "chromedriver", return_value="bundled/driver"), \
                    patch.object(platforms.shutil, "which", side_effect=lambda n: "/usr/bin/" + n if n in ("chromium", "chromedriver") else None), \
                    patch.object(platforms, "browser_pair", side_effect=[{"ok": False, "error": "GLIBC_2.35 not found"}, {"ok": True}]), \
                    patch.dict(os.environ, {}, clear=True):
                result = platforms.configure()
                self.assertTrue(result["ok"])
                self.assertEqual(json.loads((vendor / "runtime-settings.json").read_text()),
                                 {"chrome": "/usr/bin/chromium", "chromedriver": "/usr/bin/chromedriver"})

    def test_builder_rejects_x64_binary_for_arm_package(self):
        spec = importlib.util.spec_from_file_location("linux_builder", ROOT / "scripts/build-linux-bundle.py")
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        with tempfile.TemporaryDirectory() as folder:
            binary = Path(folder) / "native"
            binary.write_bytes(b"\x7fELF\x02\x01" + b"\0" * 12 + struct.pack("<H", 62))
            builder.elf_check(binary, "x64")
            with self.assertRaisesRegex(ValueError, "Wrong ELF architecture"):
                builder.elf_check(binary, "arm64")
