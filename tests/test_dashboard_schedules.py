"""Exercise schedule editor conflicts and validation in Chromium against the actual HTTP service."""
import contextlib
import datetime
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "skills/autoskill/app"
sys.path.insert(0, str(APP))

from autoskill import builder, db, paths, projects, runner
from autoskill.server import scheduler


class DashboardScheduleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        executable = paths.chrome()
        if not executable:
            raise unittest.SkipTest("Dashboard tests need an installed or bundled browser")
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        cls.browser = cls.playwright.chromium.launch(executable_path=executable, headless=True, args=["--no-sandbox"])
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.folder / "workspace"),
                                               "AUTOSKILL_CASE_SKILL": str(self.folder / "case-sheet"),
                                               "AUTOSKILL_BRIDGE": "off", "AUTOSKILL_SESSION": "",
                                               "AUTOSKILL_RUNTIME_CONTEXT": "", "AUTOSKILL_WORKFLOW_BINDING": "",
                                               "AUTOSKILL_HEADED": "0", "AUTOSKILL_QUEUE_IDLE_SECONDS": "0.1", "PYTHONPATH": str(APP)})
        environment.start()
        self.addCleanup(environment.stop)
        project = projects.create("Schedule fixture", "http://127.0.0.1:49183", project_id="schedule-fixture")
        self.project_id = project["id"]
        projects.save_cases(self.project_id, [builder.api_case("TC-API-001", "Fixture case", "GET", "/ok",
                                                               {"status": 200, "json_equals": {"/ok": True}})])
        self.task = scheduler.create({"title": "Original task", "project_id": self.project_id, "case_ids": ["TC-API-001"],
                                      "engine": "playwright", "rule": {"kind": "interval", "minutes": 720}})
        self.task = scheduler.set_enabled(self.task["id"], False)
        self.addCleanup(self.disable_tasks)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.origin = f"http://127.0.0.1:{port}"
        self.log = self.folder / "dashboard.log"
        with self.log.open("wb") as output:
            self.process = subprocess.Popen([sys.executable, "-m", "autoskill", "dashboard", "--port", str(port), "--headless"],
                                            env=os.environ.copy(), stdout=output, stderr=output)
        self.addCleanup(self.stop_process)
        self.client = httpx.Client(base_url=self.origin, trust_env=False, timeout=10)
        self.addCleanup(self.client.close)
        self.wait_for_server()
        self.context = self.browser.new_context(viewport={"width": 1440, "height": 1000})
        self.addCleanup(self.context.close)
        self.errors = []
        self.page = self.new_page()

    def disable_tasks(self):
        db.execute("UPDATE tasks SET enabled = 0")

    def stop_process(self):
        self.disable_tasks()
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            active = any(runner.process_alive(row["pid"]) for row in db.rows("SELECT pid FROM runs WHERE pid IS NOT NULL"))
            if not active and not (self.folder / "workspace/.local/queue-worker.json").exists():
                return
            time.sleep(0.05)
        self.fail("Dashboard workers did not retire before temporary workspace cleanup")

    def wait_for_server(self):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.fail("Dashboard exited before readiness:\n" + self.log.read_text(encoding="utf-8"))
            with contextlib.suppress(httpx.HTTPError):
                if self.client.get("/api/status").status_code == 200:
                    return
            time.sleep(0.05)
        self.fail("Dashboard did not become ready:\n" + self.log.read_text(encoding="utf-8"))

    def tearDown(self):
        self.assertEqual(self.errors, [])

    def new_page(self):
        page = self.context.new_page()
        page.on("pageerror", lambda error: self.errors.append(str(error)))
        page.goto(self.origin + "/#/schedules")
        expect(page.locator("#view h1")).to_have_text("定时任务")
        return page

    def edit(self, page, title="Original task"):
        page.get_by_text(title, exact=True).click()
        return page.get_by_role("dialog", name="编辑定时任务", exact=True)

    def save(self, page, dialog):
        with page.expect_response(lambda response: response.request.method == "PATCH"
                                  and response.url.endswith("/api/tasks/" + self.task["id"])) as response:
            dialog.get_by_role("button", name="保存", exact=True).click()
        return response.value

    def other_session_update(self):
        other = self.new_page()
        dialog = self.edit(other)
        dialog.get_by_label("任务名称", exact=True).fill("Updated in another session")
        dialog.get_by_label("间隔", exact=True).select_option("360")
        self.assertEqual(self.save(other, dialog).status, 200)
        return self.client.get("/api/tasks/" + self.task["id"]).json()

    def test_stale_edit_keeps_other_session_changes_and_can_reload_to_save(self):
        dialog = self.edit(self.page)
        updated = self.other_session_update()
        dialog.get_by_label("间隔", exact=True).select_option("120")
        response = self.save(self.page, dialog)
        self.assertEqual(response.status, 400)
        self.assertEqual(response.request.post_data_json["expected_revision"], self.task["revision"])
        expect(dialog.get_by_role("alert")).to_contain_text("已被其他会话修改")
        current = self.client.get("/api/tasks/" + self.task["id"]).json()
        self.assertEqual((current["title"], current["rule"], current["revision"]),
                         (updated["title"], updated["rule"], updated["revision"]))
        dialog.get_by_role("button", name="取消", exact=True).click()
        self.page.reload()
        fresh = self.edit(self.page, updated["title"])
        fresh.get_by_label("间隔", exact=True).select_option("120")
        saved = self.save(self.page, fresh)
        self.assertEqual(saved.status, 200)
        self.assertEqual(saved.request.post_data_json["expected_revision"], updated["revision"])
        self.assertEqual(saved.json()["title"], updated["title"])
        self.assertEqual(saved.json()["rule"]["minutes"], 120)

    def test_stale_enable_switch_reports_conflict_without_changing_latest_task(self):
        updated = self.other_session_update()
        with self.page.expect_response(lambda response: response.request.method == "PATCH"
                                       and response.url.endswith("/api/tasks/" + self.task["id"])) as response:
            self.page.get_by_role("switch", name="启用「Original task」", exact=True).click()
        self.assertEqual(response.value.status, 400)
        self.assertEqual(response.value.request.post_data_json, {"enabled": True, "expected_revision": self.task["revision"]})
        expect(self.page.locator(".toast")).to_contain_text("已被其他会话修改")
        current = self.client.get("/api/tasks/" + self.task["id"]).json()
        self.assertFalse(current["enabled"])
        self.assertEqual((current["title"], current["revision"]), (updated["title"], updated["revision"]))
        self.page.reload()
        expect(self.page.get_by_role("switch", name="启用「Updated in another session」", exact=True)).to_have_attribute("aria-checked", "false")

    def test_task_browser_choice_defaults_to_preference_and_survives_edit_and_reload(self):
        self.page.locator("#scope").get_by_role("button", name="回放设置", exact=True).click()
        switch = self.page.get_by_role("switch", name="显示浏览器窗口", exact=True)
        if switch.get_attribute("aria-checked") == "true":
            switch.click()
        expect(switch).to_have_attribute("aria-checked", "false")
        self.page.keyboard.press("Escape")
        self.page.get_by_role("button", name="新建定时任务", exact=True).click()
        dialog = self.page.get_by_role("dialog", name="新建定时任务", exact=True)
        dialog.get_by_label("任务名称", exact=True).fill("Hidden browser task")
        expect(dialog.get_by_label("显示浏览器窗口", exact=True)).not_to_be_checked()
        with self.page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/api/tasks")) as response:
            dialog.get_by_role("button", name="创建任务", exact=True).click()
        saved = response.value.json()
        self.assertIs(saved["headed"], False)
        self.client.patch("/api/tasks/" + saved["id"], json={"enabled": False, "expected_revision": saved["revision"]})
        self.page.reload()
        edit = self.edit(self.page, "Hidden browser task")
        expect(edit.get_by_label("显示浏览器窗口", exact=True)).not_to_be_checked()
        edit.get_by_label("任务名称", exact=True).fill("Renamed hidden task")
        with self.page.expect_response(lambda response: response.request.method == "PATCH" and response.url.endswith("/api/tasks/" + saved["id"])) as changed:
            edit.get_by_role("button", name="保存", exact=True).click()
        self.assertIs(changed.value.json()["headed"], False)
        self.page.reload()
        expect(self.page.locator("tr").filter(has_text="Renamed hidden task")).to_contain_text("隐藏浏览器窗口")

    def test_empty_one_time_date_has_visible_error_and_can_be_corrected(self):
        posted = []
        self.page.on("request", lambda request: posted.append(request.post_data_json)
                     if request.method == "POST" and request.url.endswith("/api/tasks") else None)
        self.page.get_by_role("button", name="新建定时任务", exact=True).click()
        dialog = self.page.get_by_role("dialog", name="新建定时任务", exact=True)
        dialog.get_by_label("任务名称", exact=True).fill("One time task")
        rules = dialog.get_by_role("group", name="执行规则")
        rules.get_by_role("button", name="仅一次", exact=True).click()
        dialog.get_by_label("执行时间", exact=True).fill("")
        dialog.get_by_label("任务名称", exact=True).click()
        dialog.get_by_role("button", name="创建任务", exact=True).click()
        expect(dialog.get_by_role("alert")).to_have_text("请选择有效的执行时间")
        self.assertEqual(posted, [])
        rules.get_by_role("button", name="每天", exact=True).click()
        rules.get_by_role("button", name="仅一次", exact=True).click()
        expect(dialog.get_by_label("执行时间", exact=True)).to_have_value("")
        later = datetime.datetime.now().astimezone() + datetime.timedelta(days=2)
        dialog.get_by_label("执行时间", exact=True).fill(later.strftime("%Y-%m-%dT%H:%M"))
        with self.page.expect_response(lambda response: response.request.method == "POST"
                                       and response.url.endswith("/api/tasks")) as response:
            dialog.get_by_role("button", name="创建任务", exact=True).click()
        self.assertEqual(response.value.status, 200)
        self.assertEqual(len(posted), 1)
        self.assertEqual(response.value.json()["rule"]["kind"], "at")
        expect(self.page.get_by_text("One time task", exact=True)).to_be_visible()
        self.client.delete("/api/tasks/" + response.value.json()["id"]).raise_for_status()


if __name__ == "__main__":
    unittest.main()
