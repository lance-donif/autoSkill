"""Schedule requests and occurrences survive retries, races, and interrupted queue writes."""
import datetime
import json
import os
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))
from autoskill import builder, data_refs, db, execution_inputs, projects
from autoskill.server import jobs, scheduler
from autoskill.server.app import create_app


class ScheduleTransactions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": self.tmp.name, "AUTOSKILL_HEADED": "0"})
        self.env.start()
        self.pid = projects.create("定时任务验证", "http://127.0.0.1:9", project_id="scheduled")["id"]
        self.spec = {"project_id": self.pid, "title": "每日维护", "kind": "maintenance", "workflow_id": "workflow-a", "host": "dsh",
                     "rule": {"kind": "interval", "minutes": 5}}

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def later(self):
        return datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=6)

    def test_retried_create_has_one_definition_and_conflicting_payload_is_rejected(self):
        spec = {**self.spec, "idempotency_key": "user-intent-a"}
        with ThreadPoolExecutor(max_workers=4) as pool:
            tasks = list(pool.map(lambda _: scheduler.create(spec), range(4)))
        self.assertEqual(len({t["id"] for t in tasks}), 1)
        self.assertEqual(scheduler.page()["total"], 1)
        with self.assertRaisesRegex(ValueError, "相同幂等键"):
            scheduler.create({**spec, "title": "另一个意图"})

    def test_cas_and_title_updates_preserve_timing_and_retry_receipt(self):
        first = scheduler.create(self.spec)
        spec = {"title": "新名称", "expected_revision": first["revision"], "idempotency_key": "rename-once"}
        second = scheduler.update(first["id"], spec)
        self.assertEqual(second["next_due"], first["next_due"])
        self.assertEqual(second["revision"], 2)
        self.assertEqual(scheduler.update(first["id"], spec), second)
        with self.assertRaisesRegex(ValueError, "其他会话修改"):
            scheduler.update(first["id"], {"title": "过期覆盖", "expected_revision": first["revision"]})

    def test_weekly_http_tasks_skip_the_spring_gap_and_fire_once_during_the_fall_fold(self):
        cases = [("02:30", "2027-03-07T06:00Z", "2027-03-07T07:30Z", "2027-03-14T07:30Z", "2027-03-21T06:30Z"),
                 ("01:30", "2027-11-07T04:00Z", "2027-11-07T05:30Z", "2027-11-07T06:30Z", "2027-11-14T06:30Z")]
        with TestClient(create_app(start_worker=False), base_url="http://127.0.0.1") as client, patch.object(jobs, "ensure_worker", return_value={}):
            for hour, start, first, missed, following in cases:
                with self.subTest(hour=hour):
                    rule = {"kind": "weekly", "days": [6], "time": hour, "timezone": "America/New_York"}
                    with patch.object(scheduler, "datetime", wraps=datetime) as clock:
                        clock.datetime.now.return_value = datetime.datetime.fromisoformat(start)
                        response = client.post("/api/tasks", json={**self.spec, "rule": rule})
                    response.raise_for_status()
                    task = response.json()
                    self.assertEqual(datetime.datetime.fromisoformat(task["next_due"]), datetime.datetime.fromisoformat(first))
                    scheduler.tick(datetime.datetime.fromisoformat(first))
                    scheduler.tick(datetime.datetime.fromisoformat(missed))
                    latest = client.get("/api/tasks/" + task["id"]).json()
                    self.assertEqual(datetime.datetime.fromisoformat(latest["next_due"]), datetime.datetime.fromisoformat(following))
                    self.assertEqual([row["due_at"] for row in db.rows("SELECT due_at FROM schedule_occurrences WHERE task_id=?", (task["id"],))], [task["next_due"]])
                    scheduler.set_enabled(task["id"], False)

    def test_one_clock_creates_one_outbox_and_delivery_is_not_business_completion(self):
        task = scheduler.create(self.spec)
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: scheduler.tick(self.later()), range(2)))
        deliveries = scheduler.pending_deliveries()
        self.assertEqual(deliveries["total"], 1)
        item = deliveries["items"][0]
        self.assertEqual(item["payload"]["workflow_id"], "workflow-a")
        first = scheduler.claim_delivery(item["id"], "host-one")
        competitor = scheduler.claim_delivery(item["id"], "host-two")
        self.assertEqual(competitor["token"], "")
        receipt = {"host_schedule_id": "native-host-once-1"}
        ack = scheduler.ack_delivery(item["id"], first["token"], receipt)
        self.assertEqual(ack["state"], "delivered")
        self.assertEqual(scheduler.ack_delivery(item["id"], first["token"], receipt), ack)
        occurrence = db.one("SELECT * FROM schedule_occurrences WHERE task_id = ?", (task["id"],))
        self.assertEqual(occurrence["status"], "host_delivered")
        self.assertIsNone(occurrence["run_id"])

    def test_expired_side_effect_delivery_is_unknown_and_requires_recovery(self):
        scheduler.create(self.spec)
        scheduler.tick(self.later())
        item = scheduler.pending_deliveries()["items"][0]
        scheduler.claim_delivery(item["id"], "crashed-host")
        db.update("host_outbox", item["id"], {"lease_until": "2000-01-01T00:00:00+00:00"})
        uncertain = scheduler.claim_delivery(item["id"], "replacement-host")
        self.assertEqual(uncertain["state"], "unknown")
        self.assertEqual(uncertain["attempts"], 1)
        with self.assertRaises(ValueError):
            scheduler.resolve_delivery(item["id"], retry=True)
        resolved = scheduler.resolve_delivery(item["id"], retry=True, reason="宿主按 occurrence_id 查询确认未创建任务")
        self.assertEqual(resolved["state"], "pending")
        self.assertEqual(scheduler.claim_delivery(item["id"], "replacement-host")["attempts"], 2)

    def test_pre_dispatch_failure_releases_only_the_current_claim(self):
        scheduler.create(self.spec)
        scheduler.tick(self.later())
        item = scheduler.pending_deliveries()["items"][0]
        claim = scheduler.claim_delivery(item["id"], "host-one")
        with self.assertRaises(ValueError):
            scheduler.release_delivery(item["id"], "wrong-token", "准备会话失败")
        released = scheduler.release_delivery(item["id"], claim["token"], "准备会话失败，尚未调用 prompt")
        self.assertEqual(released["state"], "pending")
        self.assertEqual(released["token"], "")

    def _prepared(self, task, trigger):
        # A separate writer must be able to proceed while replay or native inputs are frozen.
        with db.connect() as other:
            other.execute("PRAGMA busy_timeout=100")
            other.execute("BEGIN IMMEDIATE")
        return {"id": db.new_id("run"), "project_id": self.pid, "engine": "playwright", "status": "Queued",
                "case_ids": [], "created_at": db.now(), "task_id": task["id"], "trigger": trigger}

    @staticmethod
    def _enqueue(prepared, connection=None):
        connection.execute("INSERT INTO runs (id, project_id, engine, status, case_ids, created_at, task_id, trigger) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                           (prepared["id"], prepared["project_id"], prepared["engine"], prepared["status"], json.dumps(prepared["case_ids"]),
                            prepared["created_at"], prepared["task_id"], prepared["trigger"]))
        return prepared

    def test_crash_after_queue_insert_rolls_back_clock_and_occurrence(self):
        soon = (datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=30)).isoformat()
        task = scheduler.create({**self.spec, "kind": "replay", "engine": "playwright", "rule": {"kind": "at", "at": soon}})

        def interrupted(prepared, connection=None):
            self._enqueue(prepared, connection)
            raise RuntimeError("injected interruption")

        with (patch.object(scheduler, "_prepare", side_effect=self._prepared),
              patch.object(scheduler.runner, "enqueue", side_effect=interrupted, create=True),
              self.assertRaisesRegex(RuntimeError, "injected interruption")):
            scheduler.tick(self.later())
        self.assertTrue(scheduler.get(task["id"])["enabled"])
        self.assertEqual(scheduler.get(task["id"])["next_due"], task["next_due"])
        self.assertEqual(db.rows("SELECT * FROM runs"), [])
        self.assertEqual(db.rows("SELECT * FROM schedule_occurrences"), [])
        with patch.object(scheduler, "_prepare", side_effect=self._prepared), patch.object(scheduler.runner, "enqueue", side_effect=self._enqueue, create=True):
            runs = scheduler.tick(self.later())
        self.assertEqual(len(runs), 1)
        self.assertFalse(scheduler.get(task["id"])["enabled"])
        self.assertEqual(len(db.rows("SELECT * FROM schedule_occurrences")), 1)

    def test_run_request_is_idempotent_and_skipped_occurrences_remain_visible(self):
        task = scheduler.create(self.spec)
        first = scheduler.run_now(task["id"], idempotency_key="manual-once")
        self.assertEqual(scheduler.run_now(task["id"], idempotency_key="manual-once")["id"], first["id"])
        scheduler.tick(self.later())
        rows = db.rows("SELECT * FROM schedule_occurrences ORDER BY created_at")
        self.assertEqual(len(rows), 2)
        self.assertIn("skipped", {row["status"] for row in rows})
        self.assertEqual(scheduler.pending_deliveries()["total"], 1)

    def test_competing_ticks_and_retried_manual_requests_freeze_only_enqueued_runs(self):
        case = builder.api_case("PING", "本机回放", "GET", "/ping", {"status": 200})
        projects.save_cases(self.pid, [case])
        task = scheduler.create({**self.spec, "kind": "replay", "case_ids": ["PING"]})

        def concurrent(operation):
            gate = threading.Barrier(3)

            def invoke(_index):
                gate.wait(timeout=10)
                return operation()

            with ThreadPoolExecutor(max_workers=3) as pool:
                return list(pool.map(invoke, range(3)))

        manual = concurrent(lambda: scheduler.run_now(task["id"], idempotency_key="one-click"))
        self.assertEqual(len({row["id"] for row in manual}), 1)
        first_due = datetime.datetime.fromisoformat(task["next_due"])
        self.assertFalse(any(concurrent(lambda: scheduler.tick(first_due))))
        jobs.cancel(manual[0]["id"])
        second_due = first_due + datetime.timedelta(minutes=5)
        self.assertEqual(sum(map(len, concurrent(lambda: scheduler.tick(second_due)))), 1)
        occurrences = db.rows("SELECT status, run_id FROM schedule_occurrences ORDER BY rowid")
        self.assertEqual([row["status"] for row in occurrences], ["Queued", "skipped", "Queued"])
        runs = db.rows("SELECT * FROM runs")
        self.assertEqual({row["id"] for row in runs}, {row["run_id"] for row in occurrences if row["run_id"]})
        files = {data_refs.resolve(row["input_manifest"]) for row in runs}
        self.assertEqual(set((Path(self.tmp.name) / ".local/execution-inputs").glob("*.json")), files)
        for row in runs:
            self.assertEqual(execution_inputs.load(row)["selected"], ["PING"])
        scheduler.set_enabled(task["id"], False)
        with patch.object(scheduler, "_prepare") as prepare:
            with self.assertRaisesRegex(ValueError, "其他会话修改"):
                scheduler.fire(task, idempotency_key="stale-request")
            self.assertEqual(scheduler.tick(second_due + datetime.timedelta(minutes=5)), [])
            prepare.assert_not_called()

    def test_pagination_and_long_intervals_do_not_restrict_user_tasks(self):
        scheduler.create({**self.spec, "rule": {"kind": "interval", "minutes": 43200}, "title": "巡检" * 80})
        scheduler.create(self.spec)
        first = scheduler.page(limit=1)
        second = scheduler.page(offset=first["next_offset"], limit=1)
        self.assertEqual(first["total"], 2)
        self.assertNotEqual(first["items"][0]["id"], second["items"][0]["id"])
        self.assertFalse(second["has_more"])

    def test_generic_skill_does_not_assume_dsh_and_explicit_host_context_is_retained(self):
        task = scheduler.create({**self.spec, "host": "generic", "host_session_id": "host-session-b"})
        scheduler.tick(self.later())
        self.assertEqual(scheduler.pending_deliveries()["total"], 0)
        pending = scheduler.pending_deliveries("generic")["items"][0]
        self.assertEqual(pending["payload"]["parameters"]["host_session_id"], "host-session-b")
        self.assertEqual(scheduler.get(task["id"])["host"], "generic")
