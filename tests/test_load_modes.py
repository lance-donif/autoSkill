"""Locust modes against an isolated loopback service with frozen prerequisite and target inputs."""
import socket
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from textwrap import dedent
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

    def test_slow_timeline_sync_cannot_consume_the_rendezvous_duration(self):
        # Locust patches the interpreter, so isolate its campaign from this test process.
        script = dedent('''
            from unittest.mock import patch

            from autoskill import load_report, locust_worker
            from gevent.monkey import get_original

            sleep = get_original("time", "sleep")
            campaign = locust_worker.Campaign({"id": "rendezvous"}, {
                "mode": "performance", "concurrency": 1, "duration_seconds": .25,
            }, 0)

            def slow_timeline(*args, **kwargs):
                sleep(.50)

            with patch.object(campaign, "cancelled", return_value=False), patch.object(load_report, "write_timeline", slow_timeline):
                assert campaign.admit(1) == (1, 1)
                assert campaign.samples == 1
                assert not campaign.timeline_write.ready()
                campaign.timeline_write.get(timeout=3)
        ''')
        done = subprocess.run([sys.executable, "-c", script], cwd=ROOT / "skills/autoskill/app", text=True, capture_output=True,
                              timeout=15, check=False)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

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

    def test_invalid_http_headers_do_not_spend_the_single_target_slot(self):
        cases = [builder.api_case(name, name, "GET", "/invalid", {"status": 200}, headers={"X-Label": value})
                 for name, value in (("INVALID-ENCODING", "中文"), ("INVALID-FIELD", "illegal\nvalue"))]
        projects.save_cases("load", cases)
        run = load_testing.enqueue("load", [cases[0]["id"], cases[1]["id"], "LOGIN"], mode="performance", concurrency=1, target_requests=1)
        runner.execute(run["id"])
        detail = load_testing.get(run["id"])
        self.assertEqual([path for path, _, _ in self.server.seen], ["/login"])
        self.assertEqual([s["label"] for s in detail["load_report"]["samples"]], ["LOGIN"])
        self.assertEqual(detail["metrics"]["request_count"], 1)
        for row in detail["results"][:2]:
            self.assertIn("请求构建失败", row["reason"])
            self.assertFalse(row["detail"].get("target_started"))
            self.assertFalse(row["detail"].get("request"))

    def temporary_failures(self, fails):
        original = test_load_testing.Handler.do_GET
        logins = 0

        def receive(handler):
            nonlocal logins
            if handler.path == "/login":
                logins += 1
                if fails(logins):
                    handler.send_response(503)
                    handler.send_header("Content-Length", "0")
                    handler.end_headers()
                    return
            original(handler)

        return patch.object(test_load_testing.Handler, "do_GET", receive)

    def test_transient_full_group_prerequisite_failure_recovers_before_time_limit(self):
        for mode in ({"mode": "performance", "concurrency": 2, "duration_seconds": 1.4}, {"mode": "stress",
                     "initial_concurrency": 2, "max_concurrency": 2, "step_concurrency": 1, "step_duration_seconds": 1.4}):
            with self.subTest(mode=mode), self.temporary_failures(lambda i: i in (3, 4)):
                run = self.execute(**mode)
                stage = run["load_report"]["stages"][0]
                self.assertGreaterEqual(stage["end_ms"] - stage["start_ms"], 1398)
                self.assertGreater(len(run["load_report"]["samples"]), 2)
                self.assertGreaterEqual(sum(row["state"]["outcome"] == "not_run" for row in run["results"] if not row["prerequisite"]), 2)

    def test_count_only_ends_after_repeated_whole_group_preparation_failures(self):
        with self.temporary_failures(lambda i: i > 2):
            run = load_testing.enqueue("load", ["TARGET"], mode="performance", concurrency=2, target_requests=12)
            worker = threading.Thread(target=runner.execute, args=(run["id"],))
            worker.start()
            worker.join(25)
            if worker.is_alive():
                jobs.cancel(run["id"])
                worker.join(25)
                self.fail("Count-only run did not detect sustained lack of target progress")
            detail = load_testing.get(run["id"])
        self.assertIn("整组", detail["reason"])
        self.assertEqual(len(detail["load_report"]["samples"]), 2)
        self.assertEqual(sum(path == "/target" for path, _, _ in self.server.seen), 2)

    def test_stage_changes_before_locust_spawns_additional_users(self):
        detail = self.execute("LOGIN", mode="stress", initial_concurrency=1, step_concurrency=5, max_concurrency=6, step_duration_seconds=.35)
        users = {1: set(), 2: set()}
        for row in detail["results"]:
            sample = row["detail"]["attempts"][0].get("load_sample")
            if sample:
                users[sample["stage"]].add(row["detail"]["load_user"])
        self.assertEqual(users[1], {1})
        self.assertTrue({2, 6} <= users[2], users)
        clocks = detail["load_report"]
        final = clocks["stages"][1]
        ready = next(t["time_ms"] for t in clocks["threads"] if t["count"] == 6)
        self.assertLessEqual(final["start_ms"], ready)
        self.assertGreaterEqual(final["end_ms"] - ready, 348)

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
                self.assertIn("metrics", result["items"][0])
            with patch.object(load_testing.load_report, "project", side_effect=AssertionError("history projected samples")):
                compact = client.get(api, params={"include_metrics": "false"}).json()
            self.assertEqual(compact["total"], 2)
            self.assertTrue(all("metrics" not in run for run in compact["items"]))
