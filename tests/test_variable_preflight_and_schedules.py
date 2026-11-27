"""Tests for variable precedence, pre-run preflight, strict UI variable guard and scheduled context."""
import datetime
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill import builder, db, execution_inputs, platform_accounts, projects, runner, secrets
from autoskill.replay import ir, ui
from autoskill.server import jobs, scheduler
from autoskill.server.app import create_app


class EchoHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return

    def do_GET(self):
        self.server.requests.append((self.path, self.headers.get("X-Cred", "")))
        data = json.dumps({"ok": True, "order_id": "from-extract"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        body = json.loads(raw) if raw else {}
        self.server.requests.append((self.path, body, self.headers.get("X-Cred", "")))
        data = json.dumps({"ok": True, "received": body}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class VariablePreflightAndScheduleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.site_a = ThreadingHTTPServer(("127.0.0.1", 0), EchoHandler)
        cls.site_b = ThreadingHTTPServer(("127.0.0.1", 0), EchoHandler)
        for server in (cls.site_a, cls.site_b):
            server.requests = []
            threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        cls.url_a = f"http://127.0.0.1:{cls.site_a.server_port}"
        cls.url_b = f"http://127.0.0.1:{cls.site_b.server_port}"

    @classmethod
    def tearDownClass(cls):
        for server in (cls.site_a, cls.site_b):
            server.shutdown()
            server.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": self.tmp.name, "AUTOSKILL_HEADED": "0"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.site_a.requests.clear()
        self.site_b.requests.clear()

    def test_variable_priority_explicit_inputs_over_extract_over_environment_and_independent_env_secret(self):
        pid = projects.create("变量优先级", self.url_a, project_id="var-priority")["id"]
        secrets.set_secret("API_SECRET", "real-secret-value")
        projects.update(pid, environments=[{
            "id": "test", "name": "测试", "base_url": self.url_a, "is_default": True,
            "variables": {"order_id": "from-env", "tenant": "env-tenant", "empty_key": ""},
        }])
        setup_case = builder.api_case("TC-API-001", "前置提取", "GET", "/extract", {"status": 200}, extract={"order_id": "/order_id"})
        target_case = builder.api_case(
            "TC-API-002", "目标请求", "POST", "/submit", {"status": 200},
            headers={"X-Cred": "${ENV:API_SECRET}"},
            json={"order_id": "{{order_id}}", "tenant": "{{tenant}}"},
            depends_on=["TC-API-001"],
        )
        projects.save_cases(pid, [setup_case, target_case])

        # Without explicit input for order_id: extracted value overrides environment value.
        run1 = runner.create(pid, ["TC-API-002"], environment_id="test")
        self.assertEqual(runner.execute(run1["id"])["status"], "Pass")
        self.assertEqual(self.site_a.requests[-1], ("/submit", {"order_id": "from-extract", "tenant": "env-tenant"}, "real-secret-value"))

        # With explicit input for order_id: explicit input beats prerequisite extraction AND environment!
        run2 = runner.create(pid, ["TC-API-002"], environment_id="test", inputs={"order_id": "from-input"})
        self.assertEqual(runner.execute(run2["id"])["status"], "Pass")
        self.assertEqual(self.site_a.requests[-1], ("/submit", {"order_id": "from-input", "tenant": "env-tenant"}, "real-secret-value"))

    def test_find_missing_variables_preflight_and_strict_runs_api(self):
        pid = projects.create("变量校验项目", self.url_a, project_id="preflight-proj")["id"]
        projects.update(pid, environments=[{
            "id": "test", "name": "测试", "base_url": self.url_a, "is_default": True,
            "login_required": True, "auth_mode": "account_password",
            "variables": {"filled": "ok", "empty_key": "", "zero_val": 0},
        }])
        setup = builder.api_case("TC-API-001", "提取", "GET", "/extract", {"status": 200}, extract={"extracted_id": "/order_id"})
        target = builder.api_case(
            "TC-API-002", "使用变量", "POST", "/check/{{missing_path}}", {"status": 200},
            json={"a": "{{filled}}", "b": "{{empty_key}}", "c": "{{extracted_id}}", "d": "{{RAND}}", "e": "{{zero_val}}"},
            depends_on=["TC-API-001"],
        )
        projects.save_cases(pid, [setup, target])

        # Default strict_variables=False keeps backward compatibility for direct runner.prepare.
        prepared = runner.prepare(pid, ["TC-API-002"])
        self.assertEqual(prepared["status"], "Queued")

        # strict_variables=True blocks before creating a run.
        with self.assertRaises(execution_inputs.MissingVariablesError) as caught:
            runner.prepare(pid, ["TC-API-002"], strict_variables=True)
        self.assertEqual(caught.exception.missing_variables, ["missing_path", "empty_key"])
        self.assertEqual(caught.exception.by_case, {"TC-API-002": ["missing_path", "empty_key"]})

        with TestClient(create_app(start_worker=False), base_url="http://127.0.0.1:8787") as client:
            # Preflight reports missing account and missing variables without creating a run.
            pre = client.post(f"/api/projects/{pid}/preflight", json={"case_ids": ["TC-API-002"]}).raise_for_status().json()
            self.assertFalse(pre["ok"])
            self.assertTrue(pre["account_missing"])
            self.assertFalse(pre["base_url_missing"])
            self.assertEqual(pre["missing_variables"], ["missing_path", "empty_key"])
            self.assertEqual(pre["by_case"], {"TC-API-002": ["missing_path", "empty_key"]})
            self.assertEqual(db.rows("SELECT id FROM runs"), [])

            # POST /api/runs enforces strict_variables=True and returns 400 with missing_variables.
            bad_run = client.post("/api/runs", json={"project_id": pid, "case_ids": ["TC-API-002"]})
            self.assertEqual(bad_run.status_code, 400)
            self.assertEqual(bad_run.json()["code"], "missing_variables")
            self.assertEqual(bad_run.json()["missing_variables"], ["missing_path", "empty_key"])

            # Providing inputs clears missing_variables and lets POST /api/runs succeed.
            ok_run = client.post("/api/runs", json={
                "project_id": pid, "case_ids": ["TC-API-002"],
                "inputs": {"missing_path": "p1", "empty_key": "filled-now"},
            })
            self.assertEqual(ok_run.status_code, 200, ok_run.text)
            self.assertEqual(len(ok_run.json()["runs"]), 1)

    def test_ui_replay_never_sends_unresolved_placeholder_to_driver(self):
        driver = Mock(spec=["goto", "url", "count", "act", "screenshot", "reload"])
        driver.url.return_value = self.url_a + "/form"
        driver.count.return_value = 1
        case = builder.ui_case("TC-UI-VAR", "表单输入", {
            "start_url": "/form",
            "actions": [{"action": "fill", "locators": [{"by": "css", "value": "#order"}], "value": "{{missing_order}}"}],
            "assertions": [],
        })
        ir.set_variables({"other": "1"})
        self.addCleanup(ir.set_variables, None)
        outcome = ui.run_case(driver, case, {"base_url": self.url_a, "allowed_origins": [self.url_a], "timeout_seconds": 2},
                              Path(self.tmp.name), "ui-var.png")
        self.assertEqual((outcome["status"], outcome["category"]), ("Blocked", "missing_variable"))
        self.assertIn("missing_order", outcome["reason"])
        driver.act.assert_not_called()

    def test_scheduled_tasks_persist_environment_and_account_independent_of_cases_page_switch(self):
        platform_accounts.upsert_platform("erp-main", {"name": "业务平台"})
        platform_accounts.upsert_account("acc-a", {"platform_id": "erp-main", "label": "默认账号 A", "username": "a", "password": "pw"})
        platform_accounts.upsert_account("acc-b", {"platform_id": "erp-main", "label": "临时账号 B", "username": "b", "password": "pw"})
        pid = projects.create("定时环境项目", self.url_a, project_id="sched-env")["id"]
        projects.update(pid, environments=[
            {"id": "env-a", "name": "环境 A", "base_url": self.url_a, "is_default": True,
             "login_required": True, "auth_mode": "account_password", "platform_id": "erp-main",
             "account_ids": ["acc-a", "acc-b"], "default_account_id": "acc-a", "variables": {"tag": "from-a"}},
            {"id": "env-b", "name": "环境 B", "base_url": self.url_b, "is_default": False,
             "login_required": True, "auth_mode": "account_password", "platform_id": "erp-main",
             "account_ids": ["acc-a", "acc-b"], "default_account_id": "acc-b", "variables": {"tag": "from-b"}},
        ])
        case = builder.api_case("TC-API-001", "标签上报", "POST", "/tag", {"status": 200}, json={"tag": "{{tag}}"})
        projects.save_cases(pid, [case])

        with TestClient(create_app(start_worker=False), base_url="http://127.0.0.1:8787") as client, \
                patch.object(jobs, "ensure_worker", return_value={"status": "running", "pid": 1, "reason": ""}):
            # User switches Cases page to env-b / acc-b BEFORE creating a default scheduled task.
            client.put(f"/api/projects/{pid}/active-context", json={"environment_id": "env-b", "account_id": "acc-b"}).raise_for_status()

            # Creating a task without explicit environment/account picks the project's DEFAULT (env-a / acc-a).
            created = client.post("/api/tasks", json={
                "project_id": pid, "title": "每日巡检", "engine": "playwright",
                "case_ids": ["TC-API-001"], "rule": {"kind": "interval", "minutes": 5},
            }).raise_for_status().json()
            self.assertEqual((created["environment_id"], created["environment_name"], created["account_id"], created["account_label"]),
                             ("env-a", "环境 A", "acc-a", "默认账号 A"))

            # Switching Cases page again does NOT affect the scheduled task when it fires.
            fired = scheduler.tick(datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=6))
            self.assertEqual(len(fired), 1)
            run_row = db.one("SELECT * FROM runs WHERE id = ?", (fired[0]["id"],))
            self.assertEqual((run_row["environment_id"], run_row["account_id"]), ("env-a", "acc-a"))
            self.assertEqual(runner.execute(run_row["id"])["status"], "Pass")
            self.assertEqual(self.site_a.requests[-1][1], {"tag": "from-a"})
            self.assertEqual(self.site_b.requests, [])

            # PATCH /api/tasks/{id} can explicitly change environment and account.
            patched = client.patch(f"/api/tasks/{created['id']}", json={"environment_id": "env-b", "account_id": "acc-b"}).raise_for_status().json()
            self.assertEqual((patched["environment_id"], patched["environment_name"], patched["account_id"], patched["account_label"]),
                             ("env-b", "环境 B", "acc-b", "临时账号 B"))

            # When a scheduled task misses a variable, strict_variables=True blocks the run cleanly.
            missing_case = builder.api_case("TC-API-MISS", "缺变量", "POST", "/tag", {"status": 200}, json={"x": "{{missing_sched_var}}"})
            projects.save_cases(pid, [missing_case])
            bad_task = scheduler.create({
                "project_id": pid, "title": "缺变量定时", "engine": "playwright",
                "case_ids": ["TC-API-MISS"], "environment_id": "env-a", "account_id": "acc-a",
                "rule": {"kind": "interval", "minutes": 5},
            })
            blocked_run = scheduler.run_now(bad_task["id"])
            self.assertEqual(blocked_run["status"], "Blocked")
            self.assertIn("missing_sched_var", blocked_run["reason"])
            self.assertEqual((blocked_run["environment_id"], blocked_run["account_id"]), ("env-a", "acc-a"))


if __name__ == "__main__":
    unittest.main()
