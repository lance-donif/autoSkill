"""Real Chromium E2E checks for platform env/account UI, two-tab settings, sync conflicts, run stats and schedules.

Note: Unmerged backend routes (/active-context, /preflight, /platform-accounts, /sync/export, /sync/import,
/sync/conflicts/*, /runs/*/error-pack) are intercepted with contract-faithful Playwright route stubs so this
suite verifies the complete UI flows both standalone and prior to branch merge.
"""
import json
import os
import re
import sys
import unittest
from pathlib import Path

from playwright.sync_api import expect

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_dashboard_schedules as fixtures

from autoskill import builder, projects


class PlatformUiE2ETests(unittest.TestCase):
    setUpClass = classmethod(fixtures.DashboardScheduleTests.setUpClass.__func__)
    disable_tasks = fixtures.DashboardScheduleTests.disable_tasks
    stop_process = fixtures.DashboardScheduleTests.stop_process
    wait_for_server = fixtures.DashboardScheduleTests.wait_for_server
    new_page = fixtures.DashboardScheduleTests.new_page
    tearDown = fixtures.DashboardScheduleTests.tearDown

    def setUp(self):
        fixtures.DashboardScheduleTests.setUp(self)
        self.second_pid = projects.create("库存系统", "http://127.0.0.1:49184", project_id="inventory")["id"]
        projects.save_cases(self.second_pid, [
            builder.api_case("INV-API-001", "库存查询", "GET", "/stock", {"status": 200}),
        ])
        self.shot_dir = Path(os.environ["AUTOSKILL_UI_SHOT_DIR"]) if os.environ.get("AUTOSKILL_UI_SHOT_DIR") else None

    def maybe_shot(self, name):
        if self.shot_dir:
            self.shot_dir.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(self.shot_dir / name), full_page=False)

    def test_cases_per_project_env_account_and_preflight_missing_variables(self):
        active_context_calls = []
        run_calls = []
        state = {
            "order_env": "test",
            "order_acc": "acc-a",
            "inv_env": "empty-login",
        }
        accounts = {
            "acc-a": {"id": "acc-a", "label": "订单管理员 A", "username": "admin_a", "platform_name": "业务平台"},
            "acc-b": {"id": "acc-b", "label": "只读审核员 B", "username": "audit_b", "platform_name": "业务平台"},
        }

        def build_projects():
            order_envs = [
                {"id": "test", "name": "测试环境", "base_url": "http://test.orders.invalid", "is_default": True,
                 "login_required": True, "auth_mode": "account_password", "account_ids": ["acc-a", "acc-b"],
                 "default_account_id": "acc-a", "accounts": [accounts["acc-a"], accounts["acc-b"]]},
                {"id": "local", "name": "免登联调", "base_url": "http://local.orders.invalid", "is_default": False,
                 "login_required": False, "auth_mode": "none", "account_ids": [], "accounts": []},
            ]
            inv_envs = [
                {"id": "empty-login", "name": "验收环境", "base_url": "http://uat.inv.invalid", "is_default": True,
                 "login_required": True, "auth_mode": "account_password", "account_ids": [], "accounts": []},
            ]
            o_env = next(item for item in order_envs if item["id"] == state["order_env"])
            i_env = inv_envs[0]
            return [
                {"id": self.project_id, "name": "Schedule fixture", "base_url": o_env["base_url"], "broken": False,
                 "cases": {"total": 1, "ui": 0, "api": 1}, "missing_secrets": [],
                 "environments": order_envs, "active_environment_id": o_env["id"], "environment": o_env,
                 "active_account_id": state["order_acc"],
                 "account": accounts[state["order_acc"]] if o_env["login_required"] else None},
                {"id": self.second_pid, "name": "库存系统", "base_url": i_env["base_url"], "broken": False,
                 "cases": {"total": 1, "ui": 0, "api": 1}, "missing_secrets": [],
                 "environments": inv_envs, "active_environment_id": i_env["id"], "environment": i_env,
                 "active_account_id": "", "account": None},
            ]

        def handle_projects(route):
            if route.request.method == "GET":
                route.fulfill(status=200, content_type="application/json", body=json.dumps(build_projects()))
                return
            route.continue_()

        def handle_context(route):
            payload = route.request.post_data_json
            active_context_calls.append((route.request.url, payload))
            if self.project_id in route.request.url:
                if "environment_id" in payload:
                    state["order_env"] = payload["environment_id"]
                if "account_id" in payload:
                    state["order_acc"] = payload["account_id"]
            route.fulfill(status=200, content_type="application/json", body=json.dumps(build_projects()[0]))

        def handle_preflight(route):
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "ok": False, "missing_variables": ["tenant_id", "order_no"],
                "by_case": {"TC-API-001": ["tenant_id", "order_no"]},
                "account_missing": False, "base_url_missing": False,
                "message": "运行前缺少变量：tenant_id、order_no",
            }))

        def handle_runs(route):
            if route.request.method == "POST":
                run_calls.append(route.request.post_data_json)
            route.continue_()

        self.page.route("**/api/projects", handle_projects)
        self.page.route("**/api/projects/*/active-context", handle_context)
        self.page.route("**/api/projects/*/preflight", handle_preflight)
        self.page.route("**/api/runs", handle_runs)

        self.page.goto(self.origin + "/#/cases")
        expect(self.page.locator("#scope").get_by_role("button", name="切换运行环境")).to_have_count(0)

        order_card = self.page.locator(f'[data-project-card="{self.project_id}"]')
        inv_card = self.page.locator(f'[data-project-card="{self.second_pid}"]')
        expect(order_card.get_by_role("button", name="切换运行环境")).to_contain_text("测试环境")
        expect(order_card.get_by_role("button", name="切换运行账号")).to_contain_text("订单管理员 A")
        expect(inv_card.get_by_role("button", name="未配置账号")).to_be_visible()
        expect(order_card.get_by_text("正在加载文件夹…")).to_have_count(0)
        self.maybe_shot("cases-env-account.png")

        order_card.get_by_role("button", name="切换运行账号").click()
        order_card.get_by_role("menuitemcheckbox").filter(has_text="只读审核员 B").click()
        expect(order_card.get_by_role("button", name="切换运行账号")).to_contain_text("只读审核员 B")
        self.assertEqual(active_context_calls[-1][1], {"environment_id": "test", "account_id": "acc-b"})

        order_card.get_by_role("button", name="切换运行环境").click()
        order_card.get_by_role("menuitemcheckbox").filter(has_text="免登联调").click()
        expect(order_card.get_by_role("button", name="切换运行环境")).to_contain_text("免登联调")
        expect(order_card.get_by_role("button", name="切换运行账号")).to_have_count(0)
        expect(order_card.get_by_role("button", name="未配置账号")).to_have_count(0)

        order_card.get_by_role("button", name="运行", exact=True).click()
        expect(self.page.locator("#launch-receipts")).to_contain_text("tenant_id")
        expect(self.page.locator("#launch-receipts")).to_contain_text("order_no")
        self.assertEqual(run_calls, [])

    def test_project_settings_two_tabs_variables_bulk_and_plaintext_accounts(self):
        saved_accounts = []
        saved_patches = []
        run_posts = []

        detail = {
            "id": self.project_id, "name": "Schedule fixture", "base_url": "http://test.orders.invalid",
            "timeout_seconds": 20, "page_load_seconds": 90, "ignore_https_errors": False,
            "session": {"saved": False}, "credentials": {}, "login": {},
            "active_environment_id": "test",
            "environments": [
                {"id": "test", "name": "测试", "base_url": "http://test.orders.invalid", "is_default": True,
                 "login_required": True, "auth_mode": "account_password",
                 "platform_id": "erp-main", "platform_name": "业务平台",
                 "entry": {"mode": "nav", "login_url": "http://sso.invalid/login", "target_url": "",
                           "nav_label": "订单管理", "verify_url_contains": "/orders"},
                 "account_ids": ["acc-a"], "default_account_id": "acc-a",
                 "accounts": [{"id": "acc-a", "platform_id": "erp-main", "platform_name": "业务平台",
                               "environment_key": "test", "environment_name": "测试",
                               "label": "订单管理员 A", "username": "admin_a", "password": "plain-secret-123",
                               "shared_projects": [{"id": self.project_id, "name": "订单系统"}, {"id": self.second_pid, "name": "库存系统"}]}],
                 "variables": {"tenant_id": "T01", "empty_key": "", "region": "cn-east"}},
            ],
        }

        self.page.route(f"**/api/projects/{self.project_id}", lambda route: (
            route.fulfill(status=200, content_type="application/json", body=json.dumps(detail))
            if route.request.method == "GET" else (
                saved_patches.append(route.request.post_data_json),
                route.fulfill(status=200, content_type="application/json", body=json.dumps(detail)),
            ) if route.request.method == "PATCH" else route.continue_()
        ))
        self.page.route("**/api/platform-accounts", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"platforms": [], "accounts": detail["environments"][0]["accounts"]}),
        ))
        self.page.route("**/api/platform-accounts/*", lambda route: (
            saved_accounts.append(route.request.post_data_json),
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"account": route.request.post_data_json, "shared_projects": []})),
        ))
        self.page.route("**/api/runs", lambda route: (
            run_posts.append(route.request.post_data_json) if route.request.method == "POST" else None,
            route.continue_(),
        ))

        self.page.goto(self.origin + f"/#/cases?settings={self.project_id}")
        drawer = self.page.get_by_role("dialog", name="项目设置")
        expect(drawer).to_be_visible()
        expect(drawer.get_by_role("tab", name="基础与登录")).to_have_attribute("aria-selected", "true")
        expect(drawer.get_by_role("tab", name="变量 (3)")).to_be_visible()
        expect(drawer.locator('input[type="password"]')).to_have_count(0)
        expect(drawer.get_by_label("密码", exact=True)).to_have_value("plain-secret-123")
        expect(drawer).to_contain_text("平台：业务平台")
        expect(drawer).to_contain_text("共用：订单系统、库存系统")
        expect(drawer.get_by_role("button", name="立即登录")).to_have_count(0)
        expect(drawer.get_by_role("button", name="验证登录")).to_have_count(0)
        self.maybe_shot("settings-basic-tab.png")

        drawer.get_by_label("密码", exact=True).fill("plain-secret-456")
        drawer.get_by_role("tab", name="变量 (3)").click()
        expect(drawer.locator(".var-table tbody tr")).to_have_count(3)
        drawer.get_by_label("只看空值", exact=True).check()
        expect(drawer.locator(".var-table tbody tr")).to_have_count(1)
        expect(drawer.locator('.var-table input[aria-label="变量名"]').first).to_have_value("empty_key")
        drawer.get_by_label("只看空值", exact=True).uncheck()
        drawer.get_by_label("搜索变量", exact=True).fill("cn-east")
        expect(drawer.locator(".var-table tbody tr")).to_have_count(1)
        drawer.get_by_label("搜索变量", exact=True).fill("")
        self.maybe_shot("settings-vars-table.png")

        drawer.get_by_role("button", name="批量文本", exact=True).click()
        bulk = drawer.get_by_label("批量变量文本", exact=True)
        expect(bulk).to_have_value(re.compile(r"tenant_id=T01"))
        bulk.fill("tenant_id=T02\nempty_key=\nregion=cn-north\nnew_flag=1")
        expect(drawer.get_by_role("tab", name="变量 (4)")).to_be_visible()
        drawer.get_by_role("button", name="表格", exact=True).click()
        expect(drawer.locator(".var-table tbody tr")).to_have_count(4)

        drawer.get_by_label("新建环境复制变量名", exact=True).select_option("test")
        drawer.get_by_role("button", name="添加环境", exact=True).click()
        expect(drawer.get_by_role("tab", name="变量 (4)")).to_be_visible()
        expect(drawer.locator('.var-table input[aria-label="变量值"]').first).to_have_value("")

        drawer.locator(".drawer-foot").get_by_role("button", name="保存", exact=True).click()
        expect(drawer).to_have_count(0)
        self.assertEqual(len(saved_accounts), 1)
        self.assertEqual(saved_accounts[0]["password"], "plain-secret-456")
        self.assertEqual(len(saved_patches), 1)
        self.assertEqual(saved_patches[0]["environments"][0]["variables"]["tenant_id"], "T02")
        self.assertEqual(saved_patches[0]["environments"][1]["variables"], {"tenant_id": "", "empty_key": "", "region": "", "new_flag": ""})
        self.assertEqual(run_posts, [])

    def test_data_page_sync_zip_export_import_and_split_conflict_resolution(self):
        resolved_payloads = []
        conflict_detail = {
            "id": "conf-001", "source_file": "autoskill-sync-orders-20261127-1200-abcd.zip",
            "project_id": self.project_id, "project_name": "订单系统", "status": "pending",
            "cases": [{"key": "uid-1", "case_id": "TC-API-001", "title": "退款校验", "diff_fields": ["assertions"],
                       "local": {"status": 200}, "incoming": {"status": 201}}],
            "environments": [{"id": "test", "name": "测试环境", "diff_fields": ["base_url"],
                              "local": {"base_url": "http://center.invalid"}, "incoming": {"base_url": "http://dev.invalid"}}],
            "accounts": [{"id": "acc-a", "label": "订单管理员 A", "platform_id": "erp-main", "diff_fields": ["username"],
                          "local": {"username": "center_a"}, "incoming": {"username": "dev_a"}}],
            "auth": {"changed": False},
        }
        sync_state = {
            "sync_dir": "/srv/sync", "error_dir": "/srv/errors", "last_scan": None,
            "files": [{"file": conflict_detail["source_file"], "status": "conflict", "project_id": self.project_id, "conflict_id": "conf-001"}],
            "conflicts_count": 1,
            "conflicts": [{"id": "conf-001", "project_id": self.project_id, "project_name": "订单系统",
                           "source_file": conflict_detail["source_file"], "status": "pending",
                           "counts": {"cases": 1, "environments": 1, "accounts": 1, "auth": 0}}],
        }

        self.page.route("**/api/sync", lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(sync_state)))
        self.page.route("**/api/sync/export", lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "file": "/tmp/autoskill-sync-orders-20261127-1200-1234.zip",
            "filename": "autoskill-sync-orders-20261127-1200-1234.zip",
            "bytes": 1024, "download_url": "/api/sync/exports/exp-1", "projects": [self.project_id], "case_count": 1,
        })))
        self.page.route("**/api/sync/conflicts/conf-001", lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(conflict_detail)))
        self.page.route("**/api/sync/conflicts/conf-001/resolve", lambda route: (
            resolved_payloads.append(route.request.post_data_json),
            sync_state["conflicts"].clear(),
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"conflict_id": "conf-001", "status": "resolved", "applied": {}})),
        ))

        self.page.goto(self.origin + "/#/data")
        expect(self.page.locator("#view")).to_contain_text("1 条用例 · 1 个环境 · 1 个账号")
        self.page.get_by_role("button", name="导出用例包", exact=True).click()
        expect(self.page.get_by_test_id("sync-export-path")).to_contain_text("autoskill-sync-orders-20261127-1200-1234.zip")
        expect(self.page.get_by_role("link", name="下载同步 ZIP", exact=True)).to_be_visible()

        self.page.get_by_role("button", name="处理", exact=True).first.click()
        drawer = self.page.get_by_role("dialog", name="同步冲突处理")
        expect(drawer).to_be_visible()
        expect(drawer).to_contain_text("当前版本")
        expect(drawer).to_contain_text("传入版本")
        expect(drawer.get_by_role("link", name="导出两版", exact=True)).to_have_attribute("href", "/api/sync/conflicts/conf-001/export-both")
        self.maybe_shot("sync-conflict-drawer.png")

        drawer.get_by_label("使用传入用例 TC-API-001", exact=True).check()
        drawer.get_by_role("button", name="按勾选应用", exact=True).click()
        expect(drawer).to_have_count(0)
        self.assertEqual(resolved_payloads, [{
            "cases": {"mode": "custom", "use_incoming_keys": ["uid-1"]},
            "environments": {"mode": "keep_local"},
            "accounts": {"mode": "keep_local"},
        }])

    def test_runs_six_stats_context_no_password_and_error_pack_download(self):
        run_item = {
            "id": "run-platform-01", "project_id": self.project_id, "project_name": "订单系统",
            "family": "replay", "status": "Fail", "history_status": "Fail", "trigger": "manual", "engine": "playwright",
            "environment_id": "test", "environment_name": "测试环境",
            "account_id": "acc-a", "account_label": "订单管理员 A",
            "has_error_pack": True, "total": 5, "done": 4, "executed": 4, "passed": 2, "failed": 1, "blocked": 1, "not_run": 1,
            "created_at": "2026-11-27T04:00:00Z", "started_at": "2026-11-27T04:00:01Z", "finished_at": "2026-11-27T04:00:05Z",
            "state": {"phase": "settled", "outcome": "fail"},
            "results": [{
                "id": 1, "case_id": "TC-API-001", "case_title": "退款校验", "kind": "api", "status": "Fail",
                "duration_ms": 120, "category": "entry_not_found", "reason": "平台导航未找到订单入口",
                "state": {"phase": "settled", "outcome": "fail"},
                "detail": {"account": {"mode": "account", "account_id": "acc-a", "label": "订单管理员 A", "username": "admin_a"}},
            }],
        }
        self.page.route("**/api/runs?*", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"items": [run_item], "total": 1, "workspace_identity": "ws-1"})))
        self.page.route("**/api/runs/run-platform-01", lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(run_item)))

        self.page.goto(self.origin + "/#/runs")
        row = self.page.locator("tbody tr").first
        expect(row).to_contain_text("测试环境")
        expect(row).to_contain_text("订单管理员 A")
        expect(row).to_contain_text("总 5 · 执行 4 · 1 未执行 · 2 通过 · 1 失败 · 1 受阻")
        expect(row.get_by_role("link", name="下载错误包", exact=True)).to_have_attribute("href", "/api/runs/run-platform-01/error-pack")

        self.page.goto(self.origin + "/#/runs/run-platform-01")
        summary = self.page.locator(".run-summary")
        expect(summary).to_contain_text("环境：测试环境")
        expect(summary).to_contain_text("账号：订单管理员 A")
        expect(summary.locator(".stat-label")).to_have_text(["总用例", "本次执行", "通过", "失败", "受阻", "未执行", "总耗时"])
        expect(summary.get_by_role("link", name="下载错误包 ZIP", exact=True)).to_have_attribute("href", "/api/runs/run-platform-01/error-pack")
        expect(self.page.locator("#view")).to_contain_text("找不到项目入口")
        self.assertNotIn("plain-secret", self.page.locator("#view").inner_text())
        self.maybe_shot("run-report-six-stats.png")

    def test_schedules_editor_environment_and_login_required_account_selection(self):
        posted = []
        mocked_projects = [{
            "id": self.project_id, "name": "Schedule fixture", "broken": False,
            "cases": {"total": 1, "ui": 0, "api": 1},
            "environments": [
                {"id": "test", "name": "测试环境", "is_default": True, "login_required": True,
                 "account_ids": ["acc-a", "acc-b"], "default_account_id": "acc-a",
                 "accounts": [{"id": "acc-a", "label": "订单管理员 A"}, {"id": "acc-b", "label": "审核员 B"}]},
                {"id": "local", "name": "免登联调", "is_default": False, "login_required": False, "accounts": []},
            ],
        }]
        self.page.route("**/api/projects", lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(mocked_projects)))
        self.page.route("**/api/tasks", lambda route: (
            posted.append(route.request.post_data_json),
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "id": "task-new", "title": route.request.post_data_json["title"], "next_due": "2026-11-28T01:00:00Z",
            })),
        ) if route.request.method == "POST" else route.continue_())

        self.page.goto(self.origin + "/#/schedules")
        self.page.reload()
        self.page.get_by_role("button", name="新建定时任务", exact=True).click()
        dialog = self.page.get_by_role("dialog", name="新建定时任务", exact=True)
        dialog.get_by_label("任务名称", exact=True).fill("定时平台回归")
        expect(dialog.get_by_label("运行环境", exact=True)).to_have_value("test")
        expect(dialog.get_by_label("运行账号", exact=True)).to_be_visible()
        dialog.get_by_label("运行账号", exact=True).select_option("acc-b")

        dialog.get_by_label("运行环境", exact=True).select_option("local")
        expect(dialog.get_by_label("运行账号", exact=True)).to_be_hidden()

        dialog.get_by_label("运行环境", exact=True).select_option("test")
        expect(dialog.get_by_label("运行账号", exact=True)).to_be_visible()
        dialog.get_by_label("运行账号", exact=True).select_option("acc-b")
        dialog.get_by_role("button", name="创建任务", exact=True).click()
        expect(dialog).to_have_count(0)
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0]["environment_id"], "test")
        self.assertEqual(posted[0]["account_id"], "acc-b")


if __name__ == "__main__":
    unittest.main()
