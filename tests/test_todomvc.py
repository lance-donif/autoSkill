"""The vendored TodoMVC site must stay byte-identical to upstream, and its 20 cases must stay replayable."""
import hashlib
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "skills/autoskill/examples/todomvc-site"
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))
from autoskill.replay import ir  # noqa: E402


def blob(data: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


class TodoFixtureTests(unittest.TestCase):
    def test_upstream_source_bytes_match_manifest(self):
        manifest = json.loads((SITE / "source-manifest.json").read_text(encoding="utf-8"))
        for item in manifest["files"]:
            self.assertEqual(blob((SITE / item["file"]).read_bytes()), item["source_blob"], item["file"])
        self.assertEqual(blob((SITE / "LICENSE").read_bytes()), manifest["license_blob"])

    def test_twenty_ui_cases_pass_the_step_whitelist(self):
        cases = json.loads((ROOT / "tests/fixtures/todomvc-ui-cases.json").read_text(encoding="utf-8"))
        self.assertEqual(len(cases), 20)
        self.assertEqual(len({c["id"] for c in cases}), 20)
        for case in cases:
            ir.validate(case["extensions"]["automation"])


if __name__ == "__main__":
    unittest.main()
