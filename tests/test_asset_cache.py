"""Runtime download caches remain bound to the requested source and downloaded bytes."""
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def fetchers():
    for platform in ("windows", "linux"):
        spec = importlib.util.spec_from_file_location("fetch_" + platform, ROOT / f"scripts/fetch-{platform}-assets.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield platform, module


class AssetCacheTests(unittest.TestCase):
    def test_same_source_is_cached_and_changed_source_is_downloaded(self):
        for platform, module in fetchers():
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as folder:
                target = Path(folder) / "chrome.zip"
                with patch.object(module.urllib.request, "urlopen", side_effect=[io.BytesIO(b"version one"), io.BytesIO(b"version two")]) as network:
                    first = module.fetch("https://example.invalid/v1/chrome.zip", target)
                    self.assertEqual(module.fetch(first["url"], target), first)
                    self.assertEqual(network.call_count, 1)
                    second = module.fetch("https://example.invalid/v2/chrome.zip", target)
                self.assertEqual(network.call_count, 2)
                self.assertEqual(target.read_bytes(), b"version two")
                self.assertNotEqual(first["sha256"], second["sha256"])
                self.assertEqual(json.loads(target.with_name("chrome.zip.source.json").read_text()), second)

    def test_unidentified_or_corrupted_cache_is_repaired_automatically(self):
        for platform, module in fetchers():
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as folder:
                target = Path(folder) / "chrome.zip"
                target.write_bytes(b"old untracked archive")
                with patch.object(module.urllib.request, "urlopen", side_effect=[io.BytesIO(b"current"), io.BytesIO(b"current")]) as network:
                    expected = module.fetch("https://example.invalid/current/chrome.zip", target)
                    target.write_bytes(b"damaged")
                    repaired = module.fetch(expected["url"], target)
                self.assertEqual(network.call_count, 2)
                self.assertEqual(target.read_bytes(), b"current")
                self.assertEqual(repaired, expected)

    def test_failed_new_download_retains_the_old_cache_and_its_identity(self):
        for platform, module in fetchers():
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as folder:
                target = Path(folder) / "chrome.zip"
                with patch.object(module.urllib.request, "urlopen", return_value=io.BytesIO(b"old")):
                    previous = module.fetch("https://example.invalid/old/chrome.zip", target)
                with patch.object(module.urllib.request, "urlopen", side_effect=OSError("connection interrupted")):
                    with self.assertRaises(OSError):
                        module.fetch("https://example.invalid/new/chrome.zip", target)
                    self.assertEqual(module.fetch(previous["url"], target), previous)
                self.assertEqual(target.read_bytes(), b"old")
                self.assertEqual(json.loads(target.with_name("chrome.zip.source.json").read_text()), previous)

    def test_invalid_metadata_is_refetched(self):
        for platform, module in fetchers():
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as folder:
                target = Path(folder) / "chrome.zip"
                target.write_bytes(b"unknown")
                target.with_name("chrome.zip.source.json").write_text("not json", encoding="utf-8")
                with patch.object(module.urllib.request, "urlopen", return_value=io.BytesIO(b"current")) as network:
                    module.fetch("https://example.invalid/current/chrome.zip", target)
                network.assert_called_once()
                self.assertEqual(target.read_bytes(), b"current")
