"""Load analytics use the exact dispatched exchange and its run-bound receipt."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))
from autoskill import data_refs, execution_inputs, load_report, paths


class LoadReportEvidenceTests(unittest.TestCase):
    def setUp(self):
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        environment = patch.dict(os.environ, AUTOSKILL_WORKSPACE=workspace.name)
        environment.start()
        self.addCleanup(environment.stop)
        self.run = {"id": "run-local", "input_hash": "frozen"}
        self.manifest = {"cases": {}, "selected": [], "load": {"mode": "performance", "driver": "locust"}}
        self.folder = paths.local() / "runs" / self.run["id"]

    def row(self, case_id, outcome, *, kind="api", prerequisite=False, traffic="business", attempt=None):
        case = {"id": case_id, "kind": kind, "extensions": {"traffic": traffic}}
        self.manifest["cases"][case_id] = case
        self.manifest["selected"].append(case_id)
        attempt_id = attempt or case_id
        path = self.folder / "attempts" / (attempt_id + ".json")
        row = {"run_id": self.run["id"], "input_hash": "frozen", "case_id": case_id,
               "case_content_hash": execution_inputs.content_hash(case), "prerequisite": prerequisite}
        receipt = {**row, "id": attempt_id, "kind": "case", "state": "completed", "outcome": outcome}
        execution_inputs.write_json(path, receipt)
        ref = {"id": attempt_id, "kind": "case", "case_id": case_id,
               "path": data_refs.store(path), "sha256": execution_inputs.content_hash(receipt)}
        return {**row, "state": {"evidence": "valid"}, "detail": {"attempts": [ref]}}

    def test_only_hash_bound_dispatched_targets_enter_the_report(self):
        passed = {"status": "Pass", "business_status": "Pass", "target_started": True,
                  "response": {"status": 200, "bytes": 1234, "body": "private response"},
                  "load_sample": {"start_ms": 12345.75, "elapsed_ms": 0.625, "stage": 2}}
        failed = {**passed, "status": "Fail", "business_status": "Fail", "reason": "private failure text",
                  "response": {"status": 503}, "request": {"headers": {"Authorization": "private credential"}}}
        row = self.row("SEARCH", passed)
        retry = self.row("SEARCH", failed, attempt="retry")
        row["detail"]["attempts"].extend(retry["detail"]["attempts"])
        row["detail"]["attempts"][0]["load_sample"] = {"start_ms": 0, "elapsed_ms": 9999}
        corrupt = self.row("BROKEN", passed)
        corrupt["detail"]["attempts"][0]["sha256"] = "replaced"
        copied = self.row("COPIED", passed)
        copied["detail"]["attempts"] = row["detail"]["attempts"]
        rows = [row, retry, corrupt, copied, self.row("STATIC", passed, traffic="resource"),
                self.row("LOGIN", passed, prerequisite=True), self.row("PREFLIGHT", {**passed, "target_started": False}),
                self.row("NO-CLOCK", {"status": "Fail", "target_started": True, "response": {"status": 404}, "duration_ms": 1})]
        threads = [{"time_ms": 12344, "count": 1}, {"time_ms": 12347, "count": 0}]
        stages = [{"index": 2, "concurrency": 1, "start_ms": 12344, "end_ms": 12347}]
        stages.append({"index": 3, "concurrency": 2, "start_ms": 12347, "end_ms": None})
        load_report.write_timeline(self.run["id"], threads=[{**threads[0], "note": "private credential"}, threads[1]], stages=stages)
        self.assertNotIn("private credential", (self.folder / "load-timeline.json").read_text())
        report = load_report.project(self.run, rows, self.manifest)
        sample = {"label": "SEARCH", "kind": "api", "start_ms": 12345.75, "elapsed_ms": 0.625, "stage": 2}
        self.assertEqual(report, {"samples": [{**sample, "success": True, "code": 200},
                                              {**sample, "success": False, "code": 503, "error": "503/Service Unavailable"}],
                                  "threads": threads, "stages": stages, "granularity_ms": 1000})

    def test_chart_resolution_follows_the_observed_locust_span(self):
        def result(name, start, elapsed):
            return self.row(name, {"target_started": True, "status": "Pass", "business_status": "Pass",
                                   "load_sample": {"start_ms": start, "elapsed_ms": elapsed, "stage": 1}})

        first = result("FIRST", 20_000, 0.5)
        almost = result("ALMOST", 619_000, 999.5)
        self.assertEqual(load_report.project(self.run, [first, almost], self.manifest)["granularity_ms"], 1000)
        ten_minutes = result("END", 619_000, 1000)
        self.assertEqual(load_report.project(self.run, [first, ten_minutes], self.manifest)["granularity_ms"], 60_000)

    def test_verified_legacy_elapsed_without_invented_start_or_wire_bytes(self):
        self.manifest["load"] = {"iterations": 1, "concurrency": 1}
        api = self.row("API", {"status": "Fail", "business_status": "Fail", "duration_ms": 50,
                               "response": {"status": 200, "bytes": 120}, "category": "assertion_failed"})
        ui = self.row("UI", {"status": "Pass", "execution_only_pass": True, "duration_ms": 59, "target_started": True,
                             "steps": [{}, {}], "setup_cases": [{"duration_ms": 25}]}, kind="ui")
        unverified = self.row("UNVERIFIED", {"status": "Pass", "response": {"status": 200}, "duration_ms": 1})
        unverified["state"]["evidence"] = "invalid"
        blocked = self.row("SKIPPED", {"status": "Blocked", "target_started": False, "duration_ms": 0})
        report = load_report.project(self.run, [api, ui, unverified, blocked], self.manifest)
        self.assertEqual(report["samples"], [
            {"label": "API", "kind": "api", "start_ms": None, "elapsed_ms": 50, "success": False,
             "code": 200, "error": "断言失败"},
            {"label": "UI", "kind": "ui", "start_ms": None, "elapsed_ms": 34, "success": True},
        ])
        self.assertEqual((report["threads"], report["stages"]), ([], []))
        self.assertNotIn("granularity_ms", report)
        other = {"run_id": "unrelated", "threads": [{"count": 9, "time_ms": 0}], "stages": []}
        (self.folder / "load-timeline.json").write_text(json.dumps(other))
        self.assertEqual(load_report.project(self.run, [], self.manifest)["threads"], [])

    def test_timed_client_preserves_raised_error_and_submillisecond_observation(self):
        client = Mock()
        client.request.side_effect = TimeoutError("transport stopped")
        timing = {}
        with patch.object(load_report.time, "time_ns", return_value=10_250_000), patch.object(
            load_report.time, "monotonic_ns", side_effect=[100_000, 850_000],
        ), self.assertRaises(TimeoutError):
            load_report.TimedClient(client, timing).request("GET", "https://local.invalid/?secret=private")
        self.assertEqual(timing, {"start_ms": 10.25, "elapsed_ms": 0.75})
