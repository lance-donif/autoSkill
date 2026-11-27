"""Tests for shared platform accounts, scoped login cache, entry checks, auth overrides and queueing."""
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill import builder, db, platform_accounts, projects, runner, sessions
from autoskill.replay import ui
from autoskill.server import jobs
from autoskill.server.app import create_app


class PlatformApiHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        body = json.loads(raw) if raw else {}
        if self.path == "/api/login":
            user = body.get("username", "unknown")
            self.server.logins.append(user)
            token = f"tok-{user}-{len(self.server.logins)}"
            data = json.dumps({"ok": True, "token": token, "user": user}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", f"sid=cookie-{user}; Path=/")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_response(404)
        self.end_headers()

    def do_GET(self):
        auth = self.headers.get("Authorization", "")
        cookie = self.headers.get("Cookie", "")
        self.server.calls.append((self.path, auth, cookie))
        if self.path == "/api/public":
            data = json.dumps({"ok": True, "anonymous": not auth and not cookie}).encode()
            status = 200
        elif self.path == "/api/me":
            if not auth.startswith("Bearer tok-"):
                data = json.dumps({"ok": False}).encode()
                status = 401
            else:
                who = auth.split("-")[1]
                data = json.dumps({"ok": True, "who": who}).encode()
                status = 200
        else:
            data = json.dumps({"ok": True}).encode()
            status = 200
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class FakeBrowserDriver:
    """In-memory browser driver for deterministic entry verification and session tests."""

    def __init__(self, pages: dict[str, dict]):
        self.pages = pages
        self.current_url = "about:blank"
        self.state = None
        self.visited: list[str] = []
        self.clicked: list[dict] = []
        self.cancel_requested = None

    def reset(self, state=None):
        self.state = state
        self.current_url = "about:blank"

    def goto(self, url: str):
        self.current_url = url
        self.visited.append(url)

    def reload(self):
        return None

    def url(self) -> str:
        return self.current_url

    def title(self) -> str:
        return self._page().get("title", "")

    def _page(self) -> dict:
        for key, page in self.pages.items():
            if self.current_url.endswith(key) or self.current_url == key:
                return page
        return {}

    def count(self, locator: dict) -> int:
        elements = self._page().get("elements", {})
        key = locator.get("name") if locator.get("by") == "role" else locator.get("value")
        return 1 if key in elements else 0

    def read(self, locator: dict, kind: str, attr=None):
        elements = self._page().get("elements", {})
        key = locator.get("name") if locator.get("by") == "role" else locator.get("value")
        item = elements.get(key, {})
        if kind == "visible":
            return bool(item.get("visible", True))
        return item.get(kind, item.get("text", ""))

    def act(self, locator: dict, op: str, value):
        self.clicked.append({"locator": locator, "op": op, "value": value})
        if op == "click":
            key = locator.get("name") if locator.get("by") == "role" else locator.get("value")
            target = self._page().get("elements", {}).get(key, {}).get("href")
            if target:
                self.goto(target)

    def screenshot(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x89PNG\r\n\x1a\nfake-entry-screenshot")

    def storage_state(self) -> dict:
        return {"cookies": [{"name": "sid", "value": "ui-saved"}]}

    def close(self):
        return None


class PlatformAccountsAndQueueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = ThreadingHTTPServer(("127.0.0.1", 0), PlatformApiHandler)
        cls.site.logins = []
        cls.site.calls = []
        threading.Thread(target=cls.site.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.site.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.site.shutdown()
        cls.site.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": self.tmp.name, "AUTOSKILL_HEADED": "0", "AUTOSKILL_MAX_PARALLEL": "2"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.site.logins.clear()
        self.site.calls.clear()

    def _setup_shared_platform_projects(self):
        platform_accounts.upsert_platform("erp-main", {"name": "业务平台", "login_url": self.base + "/login", "auth_mode": "account_password"})
        platform_accounts.upsert_account("acc-a", {
            "platform_id": "erp-main", "environment_key": "test", "environment_name": "测试",
            "label": "订单管理员 A", "username": "alice", "password": "secret-alice-plain",
        })
        platform_accounts.upsert_account("acc-b", {
            "platform_id": "erp-main", "environment_key": "test", "environment_name": "测试",
            "label": "库存专员 B", "username": "bob", "password": "secret-bob-plain",
        })
        orders = projects.create("订单系统", self.base, project_id="orders")["id"]
        inventory = projects.create("库存系统", project_id="inventory")["id"]
        env_cfg = {
            "id": "test", "name": "测试", "base_url": self.base, "variables": {"empty_var": ""},
            "is_default": True, "login_required": True, "auth_mode": "account_password",
            "platform_id": "erp-main", "platform_name": "业务平台",
            "account_ids": ["acc-a", "acc-b"], "default_account_id": "acc-a",
        }
        projects.update(orders, environments=[env_cfg])
        projects.update(inventory, environments=[env_cfg])
        return orders, inventory

    def test_platform_accounts_crud_shared_projects_and_password_boundaries(self):
        orders, inventory = self._setup_shared_platform_projects()
        with TestClient(create_app(start_worker=False), base_url="http://127.0.0.1:8787") as client:
            catalog = client.get("/api/platform-accounts").raise_for_status().json()
            self.assertEqual([p["id"] for p in catalog["platforms"]], ["erp-main"])
            acc_a = next(item for item in catalog["accounts"] if item["id"] == "acc-a")
            self.assertEqual(acc_a["password"], "secret-alice-plain")
            self.assertEqual([p["id"] for p in acc_a["shared_projects"]], ["inventory", "orders"])

            # Updating the shared account once updates what both projects observe.
            updated = client.put("/api/platform-accounts/acc-a", json={"password": "updated-plain-pw", "label": "平台管理员 A"}).raise_for_status().json()
            self.assertEqual(updated["account"]["password"], "updated-plain-pw")
            self.assertEqual([p["id"] for p in updated["shared_projects"]], ["inventory", "orders"])

            # GET /api/projects list must never expose passwords.
            listed = client.get("/api/projects").raise_for_status().json()
            self.assertNotIn("updated-plain-pw", json.dumps(listed, ensure_ascii=False))
            self.assertNotIn("secret-bob-plain", json.dumps(listed, ensure_ascii=False))
            orders_list_item = next(p for p in listed if p["id"] == orders)
            self.assertNotIn("password", orders_list_item["account"])
            self.assertNotIn("password", orders_list_item["environments"][0]["accounts"][0])

            # GET /api/projects/{id} exposes plain-text passwords and shared project refs for settings.
            for pid in (orders, inventory):
                detail = client.get(f"/api/projects/{pid}").raise_for_status().json()
                detail_acc = next(a for a in detail["environments"][0]["accounts"] if a["id"] == "acc-a")
                self.assertEqual(detail_acc["password"], "updated-plain-pw")
                self.assertEqual(detail_acc["label"], "平台管理员 A")
                self.assertEqual(detail_acc["platform_name"], "业务平台")
                self.assertEqual([p["id"] for p in detail_acc["shared_projects"]], ["inventory", "orders"])

            # PUT /api/projects/{id}/active-context switches active account without leaking password.
            switched = client.put(f"/api/projects/{orders}/active-context", json={"environment_id": "test", "account_id": "acc-b"}).raise_for_status().json()
            self.assertEqual(switched["active_account_id"], "acc-b")
            self.assertNotIn("secret-bob-plain", json.dumps(switched, ensure_ascii=False))

            # Platform upsert and account deletion work cleanly.
            plat = client.put("/api/platforms/erp-other", json={"name": "另一平台", "auth_mode": "account_password"}).raise_for_status().json()
            self.assertEqual(plat["name"], "另一平台")
            deleted = client.delete("/api/platform-accounts/acc-b").raise_for_status().json()
            self.assertEqual(deleted, {"deleted": "acc-b"})
            self.assertIsNone(platform_accounts.get_account("acc-b"))

    def test_save_cases_assigns_and_preserves_internal_id(self):
        pid = projects.create("内部标识项目", self.base, project_id="internal-ids")["id"]
        case = builder.api_case("TC-API-001", "初版", "GET", "/api/public", {"status": 200})
        projects.save_cases(pid, [case])
        saved = projects.case(pid, "TC-API-001")
        internal_id = saved["extensions"]["internal_id"]
        self.assertTrue(internal_id.startswith("uid-"))

        # Re-saving without internal_id in the payload preserves the original internal_id.
        edited = builder.api_case("TC-API-001", "改标题", "GET", "/api/public", {"status": 200})
        projects.save_cases(pid, [edited])
        again = projects.case(pid, "TC-API-001")
        self.assertEqual(again["extensions"]["internal_id"], internal_id)
        self.assertEqual(again["title"], "改标题")

    def test_multi_account_session_isolation_reuse_and_case_auth_override(self):
        orders, inventory = self._setup_shared_platform_projects()
        for pid in (orders, inventory):
            login = builder.api_case("TC-API-LOGIN", "登录", "POST", "/api/login", {"status": 200},
                                     json={"username": "{{username}}", "password": "{{password}}"}, extract={"token": "/token"})
            me_alice = builder.api_case("TC-API-ME", "当前用户", "GET", "/api/me", {"status": 200, "json_equals": {"/ok": True}},
                                        headers={"Authorization": "Bearer {{token}}"}, depends_on=["TC-API-LOGIN"])
            me_bob = builder.api_case("TC-API-BOB", "指定账号 B", "GET", "/api/me", {"status": 200, "json_equals": {"/who": "bob"}},
                                      headers={"Authorization": "Bearer {{token}}"}, depends_on=["TC-API-LOGIN"])
            me_bob["extensions"]["auth_override"] = {"mode": "account", "account_id": "acc-b"}
            anon = builder.api_case("TC-API-ANON", "显式不登录", "GET", "/api/public", {"status": 200, "json_equals": {"/anonymous": True}},
                                    depends_on=["TC-API-LOGIN"])
            anon["extensions"]["auth_override"] = {"mode": "none"}
            projects.save_cases(pid, [login, me_alice, me_bob, anon])
            projects.set_auth(pid, {"api_case": "TC-API-LOGIN"})

        # Run orders with acc-a: logs in alice, runs bob via override, runs anon without auth.
        run1 = runner.create(orders, ["TC-API-ME", "TC-API-BOB", "TC-API-ANON"], environment_id="test", account_id="acc-a")
        final1 = runner.execute(run1["id"])
        self.assertEqual(final1["status"], "Pass", final1)
        self.assertEqual(self.site.logins, ["alice", "bob"])

        # Verify results.detail records actual account & environment without exposing passwords.
        rows = {row["case_id"]: json.loads(row["detail"]) for row in db.rows("SELECT * FROM results WHERE run_id = ?", (run1["id"],))}
        self.assertEqual(rows["TC-API-ME"]["account"]["account_id"], "acc-a")
        self.assertEqual(rows["TC-API-BOB"]["account"]["account_id"], "acc-b")
        self.assertEqual(rows["TC-API-ANON"]["account"]["mode"], "none")
        self.assertEqual(rows["TC-API-ME"]["environment"]["id"], "test")
        raw_details = json.dumps(rows, ensure_ascii=False)
        self.assertNotIn("secret-alice-plain", raw_details)
        self.assertNotIn("secret-bob-plain", raw_details)

        # Running inventory with acc-a reuses the shared platform login cache (no new alice login!).
        run2 = runner.create(inventory, ["TC-API-ME"], environment_id="test", account_id="acc-a")
        final2 = runner.execute(run2["id"])
        self.assertEqual(final2["status"], "Pass")
        self.assertEqual(self.site.logins, ["alice", "bob"])
        inv_login = db.one("SELECT detail FROM results WHERE run_id = ? AND case_id = 'TC-API-LOGIN'", (run2["id"],))
        self.assertTrue(json.loads(inv_login["detail"]).get("reused"))

        # Account B's session is still intact (not overwritten by acc-a), and another platform does NOT reuse it.
        sess_a = sessions.load(orders, platform_id="erp-main", environment_id="test", account_id="acc-a")
        sess_b = sessions.load(orders, platform_id="erp-main", environment_id="test", account_id="acc-b")
        sess_other = sessions.load(orders, platform_id="erp-other", environment_id="test", account_id="acc-a")
        self.assertIn("tok-alice-", sess_a["variables"]["token"])
        self.assertIn("tok-bob-", sess_b["variables"]["token"])
        self.assertEqual(sess_other, {})

    def test_entry_verification_nav_and_missing_entry_screenshot_stops_dependents(self):
        pid = projects.create("平台导航项目", self.base, project_id="nav-entry")["id"]
        platform_accounts.upsert_platform("erp-main", {"name": "业务平台"})
        platform_accounts.upsert_account("acc-a", {"platform_id": "erp-main", "label": "A", "username": "a", "password": "pw"})
        projects.update(pid, environments=[{
            "id": "test", "name": "测试", "base_url": self.base, "is_default": True,
            "login_required": True, "auth_mode": "account_password", "platform_id": "erp-main",
            "account_ids": ["acc-a"], "default_account_id": "acc-a",
            "entry": {
                "mode": "nav",
                "login_url": self.base + "/home",
                "nav_label": "订单中心",
                "verify_url_contains": "/orders",
                "verify_locators": [{"by": "testid", "value": "orders-title"}],
            },
        }])
        first = builder.ui_case("TC-UI-001", "进入订单", {"start_url": "/orders", "actions": [], "assertions": [{"assertion": "visible", "locators": [{"by": "testid", "value": "orders-title"}]}]})
        second = builder.ui_case("TC-UI-002", "依赖订单", {"start_url": "/orders", "actions": [], "assertions": []})
        second["extensions"]["depends_on"] = ["TC-UI-001"]
        projects.save_cases(pid, [first, second])

        # Case 1: nav entry missing -> Blocked with entry_not_found + screenshot, dependent case does not run.
        missing_driver = FakeBrowserDriver({
            "/home": {"elements": {"库存中心": {"href": self.base + "/inventory"}}},
        })
        with patch.object(runner, "make_driver", return_value=missing_driver), patch.object(ui, "POLL", 0.01):
            run_missing = runner.create(pid, ["TC-UI-002"], environment_id="test", account_id="acc-a")
            with patch.object(projects, "config", return_value={**projects.config(pid), "timeout_seconds": 0.05}):
                pass
            # Lower timeout directly in frozen manifest before execute? Better: set project timeout small first.
        projects.update(pid, timeout_seconds=0.05)
        with patch.object(runner, "make_driver", return_value=missing_driver), patch.object(ui, "POLL", 0.01):
            run_missing = runner.create(pid, ["TC-UI-002"], environment_id="test", account_id="acc-a")
            final_missing = runner.execute(run_missing["id"])
        self.assertEqual(final_missing["status"], "Blocked")
        r1 = db.one("SELECT * FROM results WHERE run_id = ? AND case_id = 'TC-UI-001'", (run_missing["id"],))
        r2 = db.one("SELECT * FROM results WHERE run_id = ? AND case_id = 'TC-UI-002'", (run_missing["id"],))
        self.assertEqual((r1["status"], r1["category"]), ("Blocked", "entry_not_found"))
        self.assertIn("订单中心", r1["reason"])
        r1_detail = json.loads(r1["detail"])
        self.assertTrue(r1_detail.get("screenshots"))
        self.assertTrue((runner.evidence_dir(run_missing["id"]) / r1_detail["screenshots"][0]).is_file())
        self.assertEqual((r2["status"], r2["category"]), ("Blocked", "prerequisite_failed"))

        # Case 2: nav entry present -> clicks nav, verifies target, both cases pass.
        good_driver = FakeBrowserDriver({
            "/home": {"elements": {"订单中心": {"href": self.base + "/orders"}}},
            "/orders": {"elements": {"orders-title": {"visible": True}}},
        })
        with patch.object(runner, "make_driver", return_value=good_driver), patch.object(ui, "POLL", 0.01):
            run_ok = runner.create(pid, ["TC-UI-002"], environment_id="test", account_id="acc-a")
            final_ok = runner.execute(run_ok["id"])
        self.assertEqual(final_ok["status"], "Pass", final_ok)

    def test_shared_account_queue_serializes_same_account_and_parallelizes_distinct_accounts(self):
        orders, inventory = self._setup_shared_platform_projects()
        billing = projects.create("账单系统", project_id="billing")["id"]
        projects.update(billing, environments=[{
            "id": "test", "name": "测试", "base_url": self.base, "is_default": True,
            "login_required": True, "auth_mode": "account_password", "platform_id": "erp-main",
            "account_ids": ["acc-a", "acc-b"], "default_account_id": "acc-b",
        }])
        for pid in (orders, inventory, billing):
            projects.save_cases(pid, [builder.api_case("TC-API-001", "检查", "GET", "/api/public", {"status": 200})])

        r_orders_a = runner.create(orders, ["TC-API-001"], environment_id="test", account_id="acc-a")
        r_inv_a = runner.create(inventory, ["TC-API-001"], environment_id="test", account_id="acc-a")
        r_bill_b = runner.create(billing, ["TC-API-001"], environment_id="test", account_id="acc-b")

        self.assertEqual(r_orders_a["account_lock_key"], "platform:erp-main:test:acc-a")
        self.assertEqual(r_inv_a["account_lock_key"], "platform:erp-main:test:acc-a")
        self.assertEqual(r_bill_b["account_lock_key"], "platform:erp-main:test:acc-b")

        # Claim orders(acc-a): inventory(acc-a) must NOT be claimable, but billing(acc-b) CAN run in parallel!
        self.assertTrue(db.claim_run(r_orders_a["id"], os.getpid(), 2, "tok-1"))
        self.assertFalse(db.claim_run(r_inv_a["id"], os.getpid(), 2, "tok-2"))
        self.assertTrue(db.claim_run(r_bill_b["id"], os.getpid(), 2, "tok-3"))

        # Check jobs.start_queued also respects account_lock_key when choosing candidates.
        db.update("runs", r_bill_b["id"], {"status": "Queued", "claim_token": ""})
        spawned = []
        with patch.object(runner, "spawn", side_effect=spawned.append):
            jobs.start_queued()
        self.assertEqual(spawned, [r_bill_b["id"]])

    def test_runs_api_exposes_context_six_counters_and_has_error_pack(self):
        orders, _ = self._setup_shared_platform_projects()
        ok_case = builder.api_case("TC-API-OK", "通过用例", "GET", "/api/public", {"status": 200, "json_equals": {"/ok": True}})
        fail_case = builder.api_case("TC-API-FAIL", "失败用例", "GET", "/api/public", {"status": 200, "json_equals": {"/ok": False}})
        dep_blocked = builder.api_case("TC-API-BLOCK", "受阻用例", "GET", "/api/public", {"status": 200}, depends_on=["TC-API-FAIL"])
        projects.save_cases(orders, [ok_case, fail_case, dep_blocked])

        run_pass = runner.execute(runner.create(orders, ["TC-API-OK"], environment_id="test", account_id="acc-a")["id"])
        run_mix = runner.execute(runner.create(orders, ["TC-API-OK", "TC-API-FAIL", "TC-API-BLOCK"], environment_id="test", account_id="acc-a")["id"])

        with TestClient(create_app(start_worker=False), base_url="http://127.0.0.1:8787") as client:
            pass_detail = client.get(f"/api/runs/{run_pass['id']}").raise_for_status().json()
            self.assertFalse(pass_detail["has_error_pack"])
            self.assertEqual((pass_detail["total"], pass_detail["executed"], pass_detail["passed"],
                              pass_detail["failed"], pass_detail["blocked"], pass_detail["not_run"]),
                             (1, 1, 1, 0, 0, 0))
            self.assertEqual((pass_detail["environment_id"], pass_detail["environment_name"],
                              pass_detail["account_id"], pass_detail["account_label"]),
                             ("test", "测试", "acc-a", "订单管理员 A"))

            mix_detail = client.get(f"/api/runs/{run_mix['id']}").raise_for_status().json()
            self.assertTrue(mix_detail["has_error_pack"])
            self.assertEqual((mix_detail["total"], mix_detail["executed"], mix_detail["passed"],
                              mix_detail["failed"], mix_detail["blocked"], mix_detail["not_run"]),
                             (3, 3, 1, 1, 1, 0))

            runs_list = client.get("/api/runs").raise_for_status().json()["items"]
            listed_mix = next(item for item in runs_list if item["id"] == run_mix["id"])
            self.assertTrue(listed_mix["has_error_pack"])
            self.assertEqual(listed_mix["account_label"], "订单管理员 A")
            self.assertEqual(listed_mix["executed"], 3)
            self.assertEqual(listed_mix["blocked"], 1)


if __name__ == "__main__":
    unittest.main()
