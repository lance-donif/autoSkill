"""A failed release promotion restores the entire previous batch without deleting it."""
import hashlib
import importlib.util
import io
import tempfile
import unittest
import urllib.parse
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("replacement_publisher", ROOT / "scripts/publish-release.py")
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


class ReleaseAPI:
    def __init__(self, files):
        self.release = {"id": 1, "tag_name": "vtest", "upload_url": "https://uploads.example.invalid/release{?name}",
                        "name": "old name", "body": "old notes", "draft": False, "prerelease": False}
        self.assets = {index: {"id": index, "name": path.name, "size": 3, "digest": "sha256:old"}
                       for index, (path, _) in enumerate(files, 10)}
        self.old = {key: dict(value) for key, value in self.assets.items()}
        self.fail = lambda method, path, body: False
        self.after_apply = False
        self.calls = []

    def __call__(self, method, path, body=None, binary=None):
        self.calls.append((method, path, body))
        failure = self.fail(method, path, body)
        if failure and not self.after_apply:
            raise OSError("injected API failure")
        result = self.apply(method, path, body, binary)
        if failure:
            raise OSError("injected lost response after the server applied the request")
        return result

    def apply(self, method, path, body, binary):
        if path.startswith("/releases?"):
            return [dict(self.release)]
        if path == "/releases/1/assets":
            return [dict(asset) for asset in self.assets.values()]
        if method == "POST":
            asset_id = max(self.assets, default=19) + 1
            name = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)["name"][0]
            self.assets[asset_id] = {"id": asset_id, "name": name, "size": len(binary),
                                     "digest": "sha256:" + hashlib.sha256(binary).hexdigest()}
            return dict(self.assets[asset_id])
        if path == "/releases/1":
            self.release.update(body)
            return dict(self.release)
        asset_id = int(path.rsplit("/", 1)[1])
        if method == "DELETE":
            del self.assets[asset_id]
            return None
        if method == "PATCH":
            if any(asset["id"] != asset_id and asset["name"] == body["name"] for asset in self.assets.values()):
                raise OSError("duplicate asset name")
            self.assets[asset_id].update(body)
        return dict(self.assets[asset_id])


class ReleaseReplacementTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.files = []
        for name in (*publisher.TARGETS, "SHA256SUMS.txt"):
            path = Path(directory.name) / name
            path.write_bytes(b"new archive")
            self.files.append((path, {"file": name, "bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}))

    def assert_previous_batch(self, remote):
        for asset_id, original in remote.old.items():
            self.assertEqual(remote.assets[asset_id], original)
        self.assertFalse(any(method == "DELETE" for method, _, _ in remote.calls))

    def test_first_or_middle_promotion_failure_restores_all_original_names(self):
        for position in (0, 2):
            with self.subTest(position=position):
                remote = ReleaseAPI(self.files)
                name = self.files[position][0].name
                remote.fail = lambda method, path, body, position=position, name=name: (
                    method == "PATCH" and path.endswith(f"/{14 + position}") and body.get("name") == name)
                with patch.object(publisher, "api", remote), self.assertRaisesRegex(OSError, "injected"):
                    publisher.publish(self.files, "new notes", "vtest", "commit")
                self.assert_previous_batch(remote)

    def test_failed_backup_rename_also_restores_originals(self):
        remote = ReleaseAPI(self.files)
        remote.fail = lambda method, path, body: method == "PATCH" and path == "/releases/assets/11" and body["name"].endswith(".previous")
        with patch.object(publisher, "api", remote), self.assertRaises(OSError):
            publisher.publish(self.files, "new notes", "vtest", "commit")
        self.assert_previous_batch(remote)

    def test_lost_promotion_response_is_rolled_back_after_it_was_applied(self):
        remote = ReleaseAPI(self.files)
        remote.after_apply = True
        name = self.files[1][0].name
        remote.fail = lambda method, path, body: method == "PATCH" and path == "/releases/assets/15" and body["name"] == name
        with patch.object(publisher, "api", remote), self.assertRaises(OSError):
            publisher.publish(self.files, "new notes", "vtest", "commit")
        self.assert_previous_batch(remote)

    def test_lost_release_response_restores_metadata_and_assets(self):
        remote = ReleaseAPI(self.files)
        remote.after_apply = True
        remote.fail = lambda method, path, body: method == "PATCH" and path == "/releases/1" and body["body"] == "new notes"
        with patch.object(publisher, "api", remote), self.assertRaises(OSError):
            publisher.publish(self.files, "new notes", "vtest", "commit")
        self.assert_previous_batch(remote)
        self.assertEqual(remote.release["body"], "old notes")

    def test_a_transient_rollback_error_is_retried(self):
        remote = ReleaseAPI(self.files)
        counts = {"restore": 0}
        def fail(method, path, body):
            if method != "PATCH":
                return False
            if path == "/releases/assets/14" and body["name"] == self.files[0][0].name:
                return True
            if path == "/releases/assets/10" and body["name"] == self.files[0][0].name:
                counts["restore"] += 1
                return counts["restore"] == 1
            return False
        remote.fail = fail
        with patch.object(publisher, "api", remote), self.assertRaises(OSError):
            publisher.publish(self.files, "new notes", "vtest", "commit")
        self.assertEqual(counts["restore"], 2)
        self.assert_previous_batch(remote)

    def test_failed_restoration_keeps_every_original_backup(self):
        remote = ReleaseAPI(self.files)
        remote.fail = lambda method, path, body: method == "PATCH" and not body.get("name", "").endswith(".previous")
        with patch.object(publisher, "api", remote), self.assertRaisesRegex(RuntimeError, "Previous files were retained"):
            publisher.publish(self.files, "new notes", "vtest", "commit")
        self.assertTrue(all(remote.assets[asset_id]["name"].endswith(".previous") for asset_id in remote.old))
        self.assertTrue(all(remote.assets[asset_id]["digest"] == "sha256:old" for asset_id in remote.old))
        self.assertFalse(any(method == "DELETE" for method, _, _ in remote.calls))

    def test_success_deletes_backups_only_after_final_assets_are_read(self):
        remote = ReleaseAPI(self.files)
        with patch.object(publisher, "api", remote):
            release, final = publisher.publish(self.files, "new notes", "vtest", "commit")
        self.assertEqual(release["body"], "new notes")
        self.assertEqual([asset["name"] for asset in final], [path.name for path, _ in self.files])
        self.assertTrue(set(remote.old).isdisjoint(remote.assets))
        last_read = max(index for index, (method, path, _) in enumerate(remote.calls) if method == "GET" and "/assets/" in path)
        first_delete = next(index for index, (method, _, _) in enumerate(remote.calls) if method == "DELETE")
        self.assertGreater(first_delete, last_read)

    def test_cleanup_failure_keeps_the_successful_new_batch(self):
        remote = ReleaseAPI(self.files)
        remote.fail = lambda method, path, body: method == "DELETE"
        with patch.object(publisher, "api", remote), redirect_stdout(io.StringIO()):
            _, final = publisher.publish(self.files, "new notes", "vtest", "commit")
        self.assertEqual([asset["name"] for asset in final], [path.name for path, _ in self.files])
        self.assertTrue(all(remote.assets[asset_id]["name"].endswith(".previous") for asset_id in remote.old))

    def test_legacy_publisher_uses_the_same_rollback(self):
        remote = ReleaseAPI(self.files[:1])
        name = self.files[0][0].name
        staged = remote("POST", "https://uploads.example.invalid/release?name=staged.zip", binary=b"new")
        remote.fail = lambda method, path, body: method == "PATCH" and path == f"/releases/assets/{staged['id']}" and body["name"] == name
        with patch.object(publisher.legacy, "api", remote), self.assertRaises(OSError):
            publisher.legacy.replace_assets(dict(remote.release), list(remote.old.values()), [(staged, {"file": name})], {"body": "new notes"})
        self.assert_previous_batch(remote)
