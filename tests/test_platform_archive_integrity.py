"""Public upgrade sources must contain their helper and a verified full program inventory."""
import hashlib
import importlib.util
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("archive_publisher", ROOT / "scripts/publish-release.py")
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


class PlatformArchiveIntegrityTests(unittest.TestCase):
    def archive(self, folder, *, omit=None, corrupt=None, unlisted=None):
        files = dict.fromkeys(("cordis.patch.yml", "plugin/index.mjs", "autoskill/SKILL.md", "autoskill-upgrade.py", "setup-linux.sh",
                               "autoskill/vendor/linux-x64/python/bin/python3", "autoskill/vendor/linux-x64/runtime-manifest.json"), b"fixture")
        files["package.json"] = b'{"version":"2.2.0"}'
        files["autoskill/offline-manifest.json"] = b'{"autoskill":"2.2.0","target":"linux-x64"}'
        if omit:
            del files[omit]
        inventory = {name: hashlib.sha256(value).hexdigest() for name, value in files.items() if name != unlisted}
        files["package-manifest.json"] = json.dumps({"format": 1, "version": "2.2.0", "files": inventory}).encode()
        if corrupt:
            files[corrupt] += b"modified"
        path = Path(folder) / "autoSkill-Linux-x64-Offline.zip"
        with zipfile.ZipFile(path, "w") as bundle:
            for name, value in files.items():
                bundle.writestr(name, value)
        return path

    def test_complete_inventory_is_verified_before_release(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self.archive(folder)
            result = publisher.describe_archive(path, "2.2.0")
            self.assertEqual(result["bytes"], path.stat().st_size)
            self.assertEqual(result["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())

    def test_modified_program_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder, self.assertRaisesRegex(ValueError, "digest mismatch"):
            publisher.describe_archive(self.archive(folder, corrupt="plugin/index.mjs"), "2.2.0")

    def test_missing_standalone_upgrade_helper_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder, self.assertRaisesRegex(ValueError, "Incorrect package layout"):
            publisher.describe_archive(self.archive(folder, omit="autoskill-upgrade.py"), "2.2.0")

    def test_unlisted_program_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder, self.assertRaisesRegex(ValueError, "Incomplete upgrade package inventory"):
            publisher.describe_archive(self.archive(folder, unlisted="plugin/index.mjs"), "2.2.0")


if __name__ == "__main__":
    unittest.main()
