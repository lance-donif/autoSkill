"""A failed upload for any architecture must keep every existing final attachment."""
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("platform_publisher", ROOT / "scripts/publish-release.py")
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


class PlatformReleaseTests(unittest.TestCase):
    def test_new_draft_uses_the_verified_existing_tag(self):
        draft = {"id": 1, "tag_name": "vtest"}
        with (patch.object(publisher, "api", side_effect=[[], {"sha": "commit"}, draft, []]) as api,
              patch.object(publisher.legacy, "replace_assets") as replace):
            publisher.publish([], "notes", "vtest", "commit")
        self.assertEqual(api.call_args_list, [
            call("GET", "/releases?per_page=100"),
            call("GET", "/commits/refs%2Ftags%2Fvtest"),
            call("POST", "/releases", {"tag_name": "vtest", "name": "autoSkill vtest", "body": "notes", "draft": True}),
            call("GET", "/releases/1/assets"),
        ])
        self.assertEqual(replace.call_args.args[0], draft)

    def test_tag_at_a_different_commit_stops_before_draft_creation(self):
        with (patch.object(publisher, "api", side_effect=[[], {"sha": "other"}]) as api,
              self.assertRaisesRegex(ValueError, "source commit")):
            publisher.publish([], "notes", "vtest", "commit")
        self.assertEqual([request.args[0] for request in api.call_args_list], ["GET", "GET"])

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
