"""A failed upload for any architecture must keep every existing final attachment."""
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("platform_publisher", ROOT / "scripts/publish-release.py")
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


class PlatformReleaseTests(unittest.TestCase):
    def test_bad_second_digest_keeps_all_final_assets(self):
        calls = []
        def api(method, path, body=None, binary=None):
            calls.append((method, path, body))
            if path.startswith("/releases?"):
                return [{"id": 1, "tag_name": "vtest", "upload_url": "https://uploads.github.com/repo{?name}"}]
            if path == "/releases/1/assets":
                return [{"id": 10, "name": "windows.zip"}, {"id": 11, "name": "linux.zip"}]
            if method == "POST":
                return {"id": 20 if "windows.zip" in path else 21}
            if path == "/releases/assets/20":
                return {"size": 3, "digest": "sha256:good"}
            if path == "/releases/assets/21":
                return {"size": 3, "digest": "sha256:bad"}
            self.fail("Unexpected mutation " + method + " " + path)
            return None
        with tempfile.TemporaryDirectory() as folder:
            files = []
            for name in ("windows.zip", "linux.zip"):
                path = Path(folder) / name
                path.write_bytes(b"zip")
                files.append((path, {"file": name, "bytes": 3, "sha256": "good"}))
            with patch.object(publisher, "api", api), self.assertRaisesRegex(ValueError, "digest mismatch"):
                publisher.publish(files, "notes", "vtest", "commit")
        self.assertFalse(any(method in ("DELETE", "PATCH") for method, _, _ in calls))
