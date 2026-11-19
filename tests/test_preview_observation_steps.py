import hashlib
import importlib.util
import json
import unittest
from pathlib import Path

from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts/fixtures/runoob-observations.json"
SCRIPT = importlib.util.spec_from_file_location("pages_builder", ROOT / "scripts/build-pages-preview.py")
builder = importlib.util.module_from_spec(SCRIPT)
SCRIPT.loader.exec_module(builder)


class ObservedPreviewStepsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads((ROOT / "preview/fixtures.json").read_text(encoding="utf-8"))
        cls.source = json.loads(SOURCE.read_text(encoding="utf-8"))

    def test_archived_steps_and_checks_match_hashed_original_workbook(self):
        record = next(r for r in self.source["cases"] if r["source_kind"] == "historical")
        original_file = next((ROOT / record["source_url"]).parent.glob("*.xlsx"))
        self.assertEqual(hashlib.sha256(original_file.read_bytes()).hexdigest(), record["artifact_hashes"]["workbook_sha256"])
        book = load_workbook(original_file, read_only=True)
        self.addCleanup(book.close)
        number, row = next((n, r) for n, r in enumerate(book["执行明细"].values, 1) if r[:2] == (record["run_id"], record["case_id"]))
        ref = f"执行明细!R{number}C6"
        parts = [(n, fragment) for address, n, fragment in book["长文本"].values if address == ref]
        raw = json.loads("".join(fragment for _, fragment in sorted(parts)) or row[5])
        self.assertEqual(record["timeline"], raw["steps"])
        self.assertEqual([s["index"] for s in record["timeline"] if s.get("check")], [2, 3])
        self.assertEqual(len(record["checks"]), 6)
        for public, original in zip(record["checks"], raw["assertions"], strict=True):
            for key in ("name", "expected", "passed", "phase", "elapsed_ms"):
                self.assertEqual(public[key], original[key])
            if public.get("actual_excerpt"):
                self.assertIn(public["actual"], original["actual"])
            else:
                self.assertEqual(public["actual"], original["actual"])
        self.assertEqual(record["attempts"], [{"id": a["id"], "sha256": a["sha256"]} for a in raw["attempts"]])

    def test_all_observation_views_use_the_original_run_and_revision(self):
        self.assertEqual(self.source, self.fixture["/observations/runoob"])
        runs = {r["run_id"]: self.fixture["/runs/" + r["run_id"]]["results"]
                for r in self.source["cases"] + self.source["previous_cases"]}
        results = self.fixture["/results"]["items"]
        for record in self.source["cases"] + self.source["previous_cases"]:
            run_id, cid = record["run_id"], record["case_id"]
            status = "Pass" if record["outcome"] == "pass" else "Fail"
            for rows in (runs[run_id], results, self.fixture["/results?status=" + status]["items"]):
                with self.subTest(run=run_id, case=cid):
                    row = next(r for r in rows if (r["run_id"], r["case_id"]) == (run_id, cid))
                    self.assertEqual(row["case_revision"], record["revision"])
                    self.assertEqual(row["detail"], builder.observation_detail(record))
                    self.assertEqual(row["detail"]["attempts"][0]["outcome"], record["outcome"])
        # The three revisions read an invalid selector, the site banner, and the tutorial heading respectively.
        expected = [(3254, [False, False, False]), (2774, [True, False, True]), (2022, [True, True, True])]
        records = sorted((r for r in self.source["cases"] + self.source["previous_cases"] if r["case_id"] == "RN-PYTHON3-INTRO"),
                         key=lambda r: r["revision"])
        self.assertEqual([(r["timeline"][0]["ms"], [c["passed"] for c in r["checks"]]) for r in records], expected)

    def test_available_screenshots_match_each_run_case_and_hash(self):
        rendered = set()
        for record in self.source["cases"] + self.source["previous_cases"]:
            key = record["run_id"], record["case_id"]
            row = next(r for r in self.fixture["/runs/" + key[0]]["results"] if r["case_id"] == key[1])
            if digest := record["artifact_hashes"].get("screenshot_sha256"):
                raw = (ROOT / record["screenshot_path"]).read_bytes()
                self.assertTrue(raw.startswith(b"\x89PNG\r\n\x1a\n"))
                self.assertEqual(hashlib.sha256(raw).hexdigest(), digest, key)
                self.assertEqual(row["detail"]["screenshots"], [digest + ".png"], key)
                rendered.add(key)
            else:
                self.assertNotIn("screenshot_path", record)
                self.assertNotIn("screenshots", row["detail"])
        self.assertEqual(len(rendered), 4)
        sample = next(r for r in self.source["cases"] if r.get("screenshot_path"))
        with self.assertRaisesRegex(ValueError, "Invalid screenshot"):
            builder.observation_detail({**sample, "artifact_hashes": {**sample["artifact_hashes"], "screenshot_sha256": "0" * 64}})

    def test_timeout_and_missing_evidence_are_not_successful_steps(self):
        for record in self.source["cases"] + self.source["previous_cases"]:
            if record["case_id"] != "RN-HOME-PYTHON3":
                continue
            detail = builder.observation_detail(record)
            self.assertEqual([step["status"] for step in detail["steps"]], ["failed"])
            self.assertTrue(detail["steps"][0]["error"])
            self.assertFalse(detail["assertions"])
            self.assertEqual(detail["pending_steps"], [{"text": "Python3 教程入口可见", "status": "not_run", "check": True}])
            incomplete = builder.observation_detail({**record, "timeline": []})
            self.assertFalse(incomplete["steps"])
            self.assertNotIn("pending_steps", incomplete)
            self.assertEqual([s["status"] for s in incomplete["definition_steps"]], ["definition_only", "definition_only"])
            self.assertTrue(all("ms" not in step for step in incomplete["definition_steps"]))
