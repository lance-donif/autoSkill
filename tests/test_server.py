"""Dashboard numbers, scheduling, sharing and the loopback guard, tested against real run history."""
import datetime
import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))
from fastapi.testclient import TestClient  # noqa: E402

from autoskill import db, demo, paths, projects, reporting, runner, secrets, share  # noqa: E402
from autoskill.server import jobs, metrics, scheduler  # noqa: E402
from autoskill.server.app import create_app  # noqa: E402

spec = importlib.util.spec_from_file_location("ticket_desk", ROOT / "skills/autoskill/examples/ticket-desk/server.py")
ticket_desk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ticket_desk)
UTC = datetime.timezone.utc
PID = demo.PROJECT_ID
API_ONLY = ["TC-API-002", "TC-API-004", "TC-API-005", "TC-API-006"]


class Workspace(unittest.TestCase):
    """A fresh workspace with the demo project pointed at a private Ticket Desk server."""

    @classmethod
    def setUpClass(cls):
        cls.server = ticket_desk.make_server(0)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous = os.environ.get("AUTOSKILL_WORKSPACE")
        os.environ["AUTOSKILL_WORKSPACE"] = self.tmp.name
        demo.install(self.base)

    def tearDown(self):
        if self.previous is None:
            os.environ.pop("AUTOSKILL_WORKSPACE", None)
        else:
            os.environ["AUTOSKILL_WORKSPACE"] = self.previous
        self.tmp.cleanup()

    def run_cases(self, *case_ids, **kwargs):
        run = runner.create(PID, list(case_ids) or None, "playwright", **kwargs)
        return runner.execute(run["id"])


class MetricsTests(Workspace):
    def test_every_figure_equals_its_drill_down_list(self):
        self.run_cases(*API_ONLY)
        secrets.set_secret("TD_PASSWORD", "wrong")  # the login now fails: its dependents are blocked
        from autoskill import sessions

        sessions.clear(PID)
        self.run_cases(*API_ONLY)
        scope = metrics.Scope()
        s = metrics.summary(scope)
        r = s["results"]
        self.assertEqual((r["passed"], r["failed"], r["blocked"]), (5, 0, 3))  # prerequisites are not counted
        self.assertEqual(r["pass_rate"], 1.0)  # blocked results never enter the denominator
        for status, key in (("Pass", "passed"), ("Fail", "failed"), ("Blocked", "blocked")):
            self.assertEqual(metrics.results(scope, status)["total"], r[key])
        self.assertEqual(metrics.results(scope)["total"], r["executions"])
        self.assertEqual(sum(p["passed"] + p["failed"] + p["blocked"] for p in s["trend"]["points"]), r["executions"])
        self.assertEqual(metrics.runs(scope)["total"], s["runs"]["total"])
        self.assertEqual(metrics.cases(scope)["total"], s["cases"]["total"])

    def test_project_and_time_filters_apply_to_everything(self):
        projects.create("另一个项目", self.base, project_id="other")
        self.run_cases("TC-API-006")
        mine, other = metrics.summary(metrics.Scope([PID])), metrics.summary(metrics.Scope(["other"]))
        self.assertEqual((mine["results"]["executions"], mine["cases"]["total"], mine["runs"]["total"]), (1, 11, 1))
        self.assertEqual((other["results"]["executions"], other["cases"]["total"], other["runs"]["total"]), (0, 0, 0))
        self.assertIsNone(other["results"]["pass_rate"])  # no data is "—", never 0% or 100%
        past = metrics.Scope.parse(None, "2020-01-01T00:00:00+08:00", "2020-01-02T00:00:00+08:00")
        old = metrics.summary(past)
        self.assertEqual((old["results"]["executions"], old["runs"]["total"], old["cases"]["total"]), (0, 0, 0))
        self.assertEqual(metrics.results(past)["total"], 0)

    def test_still_failing_follows_the_latest_result_of_each_case(self):
        case = projects.case(PID, "TC-API-006")
        broken = json.loads(json.dumps(case))
        broken["assertions"]["status"] = 200  # the endpoint answers 401, so this expectation fails
        projects.save_cases(PID, [broken])
        self.run_cases("TC-API-006")
        self.assertEqual(metrics.summary(metrics.Scope())["results"]["still_failing"], 1)
        projects.save_cases(PID, [case])
        self.run_cases("TC-API-006")
        r = metrics.summary(metrics.Scope())["results"]
        self.assertEqual((r["failed"], r["still_failing"]), (1, 0))
        self.assertEqual(metrics.results(metrics.Scope(), "Fail", current_only=True)["total"], 0)


class SchedulerTests(Workspace):
    def test_rules(self):
        base = datetime.datetime(2026, 11, 12, 0, 0, tzinfo=UTC)  # Thursday 08:00 in Shanghai
        daily = scheduler.next_due({"kind": "daily", "time": "09:00", "timezone": "Asia/Shanghai"}, base)
        self.assertEqual(daily, datetime.datetime(2026, 11, 12, 1, 0, tzinfo=UTC))
        monday = scheduler.next_due({"kind": "weekly", "time": "09:00", "days": [0], "timezone": "Asia/Shanghai"}, base)
        self.assertEqual(monday, datetime.datetime(2026, 11, 16, 1, 0, tzinfo=UTC))
        merged = scheduler.next_due({"kind": "interval", "minutes": 1}, base + datetime.timedelta(seconds=185), base)
        self.assertEqual(merged, base + datetime.timedelta(minutes=4))  # missed occurrences collapse into the next
        for bad in ({"kind": "interval", "minutes": 0}, {"kind": "interval", "minutes": True}, {"kind": "daily", "time": "25:00"},
                    {"kind": "daily", "time": "09:00", "timezone": "Nowhere/None"}, {"kind": "weekly", "time": "09:00", "days": []},
                    {"kind": "at", "at": "2020-01-01T00:00:00+08:00"}, {"kind": "at"}, {"kind": "cron"}):
            with self.subTest(rule=bad), self.assertRaises(ValueError):
                scheduler.next_due(bad, base)

    def test_due_task_fires_once_and_not_while_its_run_is_active(self):
        task = scheduler.create({"title": "巡检", "project_id": PID, "engine": "playwright", "case_ids": ["TC-API-006"],
                                 "rule": {"kind": "interval", "minutes": 5}})
        later = datetime.datetime.now(UTC) + datetime.timedelta(minutes=6)
        first = scheduler.tick(later)
        self.assertEqual((len(first), first[0]["trigger"]), (1, "schedule"))
        self.assertEqual(scheduler.tick(later), [])
        self.assertEqual(scheduler.tick(later + datetime.timedelta(minutes=6)), [])  # previous run still queued
        final = runner.execute(first[0]["id"])
        self.assertEqual(final["status"], "Pass")
        self.assertEqual(scheduler.get(task["id"])["last_status"], "Pass")
        self.assertEqual(metrics.summary(metrics.Scope())["tasks"]["fired_in_range"], 1)

    def test_pause_resume_once_and_delete(self):
        soon = (datetime.datetime.now(UTC) + datetime.timedelta(seconds=30)).isoformat()
        once = scheduler.create({"title": "一次", "project_id": PID, "engine": "playwright", "rule": {"kind": "at", "at": soon}})
        fired = scheduler.tick(datetime.datetime.now(UTC) + datetime.timedelta(minutes=1))
        self.assertEqual(len(fired), 1)
        self.assertFalse(scheduler.get(once["id"])["enabled"])  # a one-off task disables itself
        task = scheduler.create({"title": "每天", "project_id": PID, "engine": "playwright", "rule": {"kind": "daily", "time": "09:00"}})
        self.assertIsNone(scheduler.set_enabled(task["id"], False)["next_due"])
        self.assertEqual(scheduler.tick(datetime.datetime.now(UTC) + datetime.timedelta(days=2)), [])
        self.assertIsNotNone(scheduler.set_enabled(task["id"], True)["next_due"])
        scheduler.delete(task["id"])
        with self.assertRaises(ValueError):
            scheduler.get(task["id"])

    def test_a_run_whose_process_died_is_marked_blocked(self):
        run = runner.create(PID, ["TC-API-006"])
        db.update("runs", run["id"], {"status": "Running", "pid": 999999})
        jobs.sweep()
        swept = db.one("SELECT status, reason FROM runs WHERE id = ?", (run["id"],))
        self.assertEqual(swept["status"], "Blocked")
        self.assertIn("意外结束", swept["reason"])


class ApiGuardTests(Workspace):
    def test_loopback_only_and_no_cross_site_writes(self):
        with TestClient(create_app(8787, start_worker=False), base_url="http://127.0.0.1:8787") as client:
            self.assertEqual(client.get("/api/status").status_code, 200)
            self.assertEqual(client.get("/api/status", headers={"Host": "evil.test"}).status_code, 403)
            self.assertEqual(client.post("/api/runs", json={}, headers={"Origin": "https://evil.test"}).status_code, 403)
            self.assertEqual(client.post("/api/runs", json={}, headers={"Sec-Fetch-Site": "cross-site"}).status_code, 403)

    def test_secrets_are_write_only_and_files_cannot_be_traversed(self):
        with TestClient(create_app(8787, start_worker=False), base_url="http://127.0.0.1:8787") as client:
            self.assertEqual(client.put("/api/secrets/X_TOKEN", json={"value": "v"}).status_code, 200)
            self.assertEqual(client.get("/api/secrets/X_TOKEN").status_code, 404)
            run = runner.create(PID, ["TC-API-006"])
            for name in ("..%2f..%2fsecrets.json", "..%5csecrets.json", "secrets.json"):
                self.assertEqual(client.get(f"/api/runs/{run['id']}/files/{name}").status_code, 404, name)
            bad = client.post("/api/projects", json={"name": "", "url": "not a url"})
            self.assertEqual(bad.status_code, 400)
            self.assertIn("项目名称", bad.json()["detail"])


class ShareTests(Workspace):
    def test_pack_carries_cases_and_knowledge_but_nothing_private(self):
        (projects.path(PID) / "knowledge" / "business-rules.md").write_text("# 规则\n\n只有管理员可以新建工单。\n", encoding="utf-8")
        self.run_cases("TC-API-004")  # creates a session, run history and evidence that must stay behind
        token = __import__("autoskill.sessions", fromlist=["x"]).load(PID)["variables"]["token"]
        packed = share.pack(out=str(Path(self.tmp.name) / "p.zip"))
        with zipfile.ZipFile(packed["file"]) as archive:
            names = archive.namelist()
            blob = b"".join(archive.read(n) for n in names)
        self.assertIn(f"projects/{PID}/knowledge/business-rules.md", names)
        self.assertIn(f"projects/{PID}/testcases/api-cases.json", names)
        self.assertFalse(any(part in name for name in names for part in (".local", "sessions", ".db", "versions/")))
        self.assertNotIn(b"Passw0rd!demo", blob)
        self.assertNotIn(token.encode(), blob)
        self.assertEqual(packed["secrets_not_included"], ["TD_PASSWORD"])

    def test_import_into_a_fresh_workspace_then_run(self):
        packed = share.pack(out=str(Path(self.tmp.name) / "p.zip"), passphrase="correct horse 42")
        with tempfile.TemporaryDirectory() as other:
            os.environ["AUTOSKILL_WORKSPACE"] = other
            try:
                without = share.import_pack(packed["file"])
                self.assertEqual(([p["id"] for p in without["imported"]], without["missing_secrets"]), ([PID], ["TD_PASSWORD"]))
                with self.assertRaisesRegex(ValueError, "口令"):
                    share.import_pack(packed["file"], passphrase="wrong", overwrite=True)
                restored = share.import_pack(packed["file"], passphrase="correct horse 42", overwrite=True)
                self.assertEqual((restored["secrets_restored"], restored["missing_secrets"]), (["TD_PASSWORD"], []))
                self.assertEqual(self.run_cases("TC-API-004")["status"], "Pass")
                again = share.import_pack(packed["file"])
                self.assertEqual(again["skipped"][0]["id"], PID)  # never silently replaces an existing project
            finally:
                os.environ["AUTOSKILL_WORKSPACE"] = self.tmp.name

    def test_a_copied_folder_with_someone_elses_secret_store_still_works(self):
        (paths.local() / "secrets.json").write_text(json.dumps({"TD_PASSWORD": "bm90LWEtZHBhcGktYmxvYg=="}), encoding="utf-8")
        os.environ.pop("TD_PASSWORD", None)
        listed = projects.list_all()
        self.assertFalse(any(p.get("broken") for p in listed))  # unreadable secrets are "not set here", not a broken project
        self.assertEqual(projects.get(PID)["missing_secrets"], ["TD_PASSWORD"])
        secrets.set_secret("TD_PASSWORD", "Passw0rd!demo")
        self.assertEqual(self.run_cases("TC-API-002")["status"], "Pass")

    def test_full_pack_contains_the_skill_and_projects_but_nothing_private(self):
        self.run_cases("TC-API-004")
        with tempfile.TemporaryDirectory() as out:
            packed = share.pack_full(out)
            with zipfile.ZipFile(packed["file"]) as archive:
                names = archive.namelist()
        self.assertIn("autoskill/SKILL.md", names)
        self.assertIn("autoskill/app/autoskill/cli.py", names)
        self.assertEqual(packed["secrets_not_included"], ["TD_PASSWORD"])
        self.assertFalse(any(".local" in n or n.endswith((".db", "secrets.json", ".pyc")) for n in names))
        with self.assertRaisesRegex(ValueError, "不能输出到 Skill 目录内部"):
            share.pack_full(str(paths.SKILL / "x.zip"))

    def test_import_refuses_unsafe_or_invalid_packs(self):
        target = Path(self.tmp.name) / "evil.zip"
        for name in (f"projects/{PID}/../../escape.json", "projects/../x/project.json", f"projects/{PID}/run.cmd",
                     "C:/windows/x.json", f"projects/{PID}/a\\..\\b.json"):
            with zipfile.ZipFile(target, "w") as archive:
                archive.writestr(share.MANIFEST, "{}")
                archive.writestr(name, "{}")
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "已拒绝导入"):
                share.import_pack(str(target))
        with zipfile.ZipFile(target, "w") as archive:
            archive.writestr("readme.txt", "not a pack")
        with self.assertRaisesRegex(ValueError, "不是 autoSkill 分享包"):
            share.import_pack(str(target))
        self.assertFalse((paths.workspace() / "escape.json").exists())


class ReportTests(Workspace):
    def test_excel_report_uses_latest_results_and_treats_content_as_text(self):
        from openpyxl import load_workbook

        case = projects.case(PID, "TC-API-006")
        risky = json.loads(json.dumps(case))
        risky["title"] = "=1+1"
        projects.save_cases(PID, [risky])
        self.run_cases("TC-API-006")
        built = reporting.build(PID, str(Path(self.tmp.name) / "out"))
        self.assertEqual((built["cases"], built["passed"], built["not_run"]), (11, 1, 10))
        sheet = load_workbook(built["excel"])["用例与结果"]
        row = next(r for r in sheet.iter_rows(min_row=2) if r[0].value == "TC-API-006")
        self.assertEqual((row[3].value, row[3].data_type, row[8].value), ("=1+1", "s", "通过"))


if __name__ == "__main__":
    unittest.main()
