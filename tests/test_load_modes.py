"""Locust modes against an isolated loopback service with frozen prerequisite and target inputs."""
import socket
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import test_load_testing
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))
from autoskill import builder, db, load_testing, projects, runner
from autoskill.server import jobs
from autoskill.server.app import create_app


class LocustModes(unittest.TestCase):
    setUp = test_load_testing.LoadTests.setUp
    tearDown = test_load_testing.LoadTests.tearDown

    def execute(self, case_id="TARGET", **load):
        run = load_testing.enqueue("load", [case_id], **load)
        runner.execute(run["id"])
        return load_testing.get(run["id"])

    def test_first_of_two_limits_reserves_only_selected_requests_and_drains_them(self):
        original_socket = socket.socket
        first = self.execute(mode="performance", concurrency=6, target_requests=3, duration_seconds=8)
        self.assertEqual((first["status"], first["engine"], first["metrics"]["request_count"]), ("Pass", "locust", 3))
        self.assertEqual(first["test_report"]["status"], "ready")
        self.assertEqual(len(first["load_report"]["samples"]), 3)
        self.assertEqual(max(t["count"] for t in first["load_report"]["threads"]), 3)
        self.assertEqual(sum(path == "/target" for path, _, _ in self.server.seen), 3)
        self.assertEqual(sum(path == "/login" for path, _, _ in self.server.seen), 3)
        self.assertTrue(all(row["state"]["evidence"] == "valid" for row in first["results"]))
        self.assertIs(socket.socket, original_socket)
        self.server.seen.clear()
        timed = self.execute(mode="performance", concurrency=2, target_requests=50, duration_seconds=.23)
        samples = timed["load_report"]["samples"]
        stage = timed["load_report"]["stages"][0]
        self.assertGreaterEqual(len(samples), 2)
        self.assertLess(len(samples), 50)
        self.assertEqual(len(samples), sum(path == "/target" for path, _, _ in self.server.seen))
        self.assertEqual(timed["load_report"]["threads"][-1]["count"], 0)
        self.assertTrue(all(s["start_ms"] <= stage["end_ms"] + 5 for s in samples))

    def test_nondivisible_stages_use_real_locust_users_and_preserve_slow_request_stage(self):
        detail = self.execute("LOGIN", mode="stress", initial_concurrency=1, step_concurrency=2, max_concurrency=4, step_duration_seconds=.8)
        stages = detail["load_report"]["stages"]
        self.assertEqual([s["concurrency"] for s in stages], [1, 3, 4])
        self.assertTrue(all(s["end_ms"] - s["start_ms"] >= 798 for s in stages))
        self.assertEqual(max(t["count"] for t in detail["load_report"]["threads"]), 4)
        self.assertEqual(detail["load_report"]["threads"][-1]["count"], 0)
        self.assertGreaterEqual(self.server.maximum, 2)
        samples = detail["load_report"]["samples"]
        self.assertEqual({s["stage"] for s in samples}, {1, 2, 3})
        for sample in samples:
            slot = stages[sample["stage"] - 1]
            self.assertLessEqual(slot["start_ms"], sample["start_ms"])
            self.assertLessEqual(sample["start_ms"], slot["end_ms"] + 5)
        self.assertEqual(len(samples), sum(path == "/login" for path, _, _ in self.server.seen))

    def test_automatic_postcondition_is_evidence_only_and_never_sent_after_the_budget(self):
        target = projects.case("load", "TARGET")
        target["extensions"]["postconditions"] = ["VERIFY"]
        check = builder.api_case("VERIFY", "后置", "GET", "/verify", {"status": 200, "json_equals": {"/value": 42}},
                                 headers={"X-Token": "{{token}}"}, depends_on=["TARGET"])
        projects.save_cases("load", [target, check])
        detail = self.execute(mode="performance", concurrency=1, target_requests=2)
        self.assertEqual([path for path, _, _ in self.server.seen], ["/login", "/target", "/verify", "/login", "/target"])
        targets = [r for r in detail["results"] if r["case_id"] == "TARGET"]
        self.assertEqual([row["state"]["outcome"] for row in targets], ["pass", "fail"])
        self.assertTrue(all(row["state"]["evidence"] == "valid" for row in targets))
        self.assertIn("后置", targets[1]["reason"])
        self.assertEqual(detail["metrics"]["request_count"], 2)
        self.assertEqual(len(detail["load_report"]["samples"]), 2)

    def test_unreachable_count_stops_with_prerequisite_evidence(self):
        login = projects.case("load", "LOGIN")
        login["assertions"]["status"] = 201
        projects.save_cases("load", [login])
        detail = self.execute(mode="performance", concurrency=3, target_requests=8)
        self.assertEqual(detail["status"], "Blocked")
        self.assertEqual((detail["metrics"]["request_count"], detail["load_report"]["samples"]), (0, []))
        self.assertIn("前置", detail["reason"])
        self.assertTrue(all(path == "/login" for path, _, _ in self.server.seen))
        self.assertEqual(len(self.server.seen), 3)

    def test_cancel_while_prerequisites_are_preparing_releases_the_rendezvous(self):
        run = load_testing.enqueue("load", ["TARGET"], mode="performance", concurrency=3, target_requests=50)
        worker = threading.Thread(target=runner.execute, args=(run["id"],))
        worker.start()
        self.assertTrue(self.server.entered.wait(20))
        jobs.cancel(run["id"])
        worker.join(25)
        self.assertFalse(worker.is_alive())
        detail = load_testing.get(run["id"])
        self.assertEqual((detail["status"], detail["load_report"]["samples"]), ("Cancelled", []))
        self.assertFalse(any(path == "/target" for path, _, _ in self.server.seen))

    def test_manual_stop_drains_a_sent_request_and_preserves_its_verdict(self):
        target_sent = threading.Event()
        original = test_load_testing.Handler.do_GET

        def observe(handler):
            if handler.path == "/target":
                target_sent.set()
            original(handler)

        with patch.object(test_load_testing.Handler, "do_GET", observe):
            run = load_testing.enqueue("load", ["TARGET"], mode="performance", concurrency=1, target_requests=20)
            worker = threading.Thread(target=runner.execute, args=(run["id"],))
            worker.start()
            self.assertTrue(target_sent.wait(20))
            jobs.cancel(run["id"])
            worker.join(25)
        self.assertFalse(worker.is_alive())
        detail = load_testing.get(run["id"])
        self.assertEqual(detail["status"], "Cancelled")
        self.assertEqual([s["success"] for s in detail["load_report"]["samples"]], [True])
        self.assertEqual([r["state"]["outcome"] for r in detail["results"] if not r["prerequisite"]], ["pass"])

    def test_http_excludes_ui_and_filters_legacy_mode_before_pagination(self):
        ui = builder.ui_case("UI", "页面", {"start_url": "/", "actions": [], "assertions": [{"assertion": "url_contains", "expected": "/"}]})
        mixed = builder.api_case("MIX", "混合", "GET", "/target", {"status": 200}, depends_on=["UI"])
        projects.save_cases("load", [ui, mixed])
        with TestClient(create_app(start_worker=False), base_url="http://127.0.0.1:8787") as client, patch.object(jobs, "ensure_worker"):
            base = {"items": [{"project_id": "load", "case_ids": ["TARGET"]}]}
            api = "/api/load-tests"
            options = client.get(api + "/options").json()["items"]
            self.assertEqual({row["case_id"] for row in options}, {"LOGIN", "TARGET"})
            for invalid in ({"mode": "performance", "concurrency": 3},
                            {"mode": "performance", "concurrency": True, "target_requests": 1},
                            {"mode": "stress", "initial_concurrency": 4, "step_concurrency": 1, "max_concurrency": 3, "step_duration_seconds": 1}):
                self.assertEqual(client.post(api, json={**base, **invalid}).status_code, 400)
            for case_id in ("UI", "MIX"):
                response = client.post(api, json={"items": [{"project_id": "load", "case_ids": [case_id]}], "iterations": 1})
                self.assertEqual(response.status_code, 400)
            self.assertFalse(db.rows("SELECT id FROM runs"))
            for spec in ({"concurrency": 1, "iterations": 2}, {"mode": "stress", "initial_concurrency": 1,
                        "step_concurrency": 1, "max_concurrency": 2, "step_duration_seconds": 1}):
                self.assertEqual(client.post(api, json={**base, **spec}).status_code, 200)
            for mode in ("stress", "performance"):
                result = client.get(api, params={"mode": mode, "limit": 1}).json()
                self.assertEqual((result["total"], result["items"][0]["load"]["mode"]), (1, mode))
