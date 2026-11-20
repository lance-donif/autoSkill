"""Saved design history exports its own blockers without borrowing current execution or rewriting delivery."""
import hashlib
import io
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import test_public_reuse_flow as reuse
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from autoskill import builder, case_plans, db, projects, runner, task_reports
from autoskill.server.app import create_app


class CasePlanHistoryDeliveryTests(unittest.TestCase):
    setUp = reuse.PublicReuseFlowTests.setUp
    command = reuse.PublicReuseFlowTests.command

    def test_history_http_and_download_keep_manual_block_after_new_task_replaces_active_sheet(self):
        good = builder.api_case("API-GOOD", "Health contract", "GET", "/unrelated", {"status": 200, "json_equals": {"/ok": True}})
        bad = builder.api_case("API-BAD", "Deliberately failing contract", "GET", "/unrelated", {"status": 200, "json_equals": {"/ok": False}})
        projects.save_cases(self.project, [good, bad])
        source = self.folder / "history-design.json"
        rows = [{"id": f"TC-{number:03d}", "title": f"Original design row {number}", "description": f"Original design row {number}",
                 "steps": "1. 执行原定业务。", "expected": "1. 满足原独立预期。", "business_path": ["公开复用", "原设计"]} for number in range(1, 5)]
        source.write_text(json.dumps(rows), encoding="utf-8")
        written = self.command("plan", "write", self.project, "--file", str(source))
        initial = Path(written["initial_excel"]).read_bytes()
        for row, case, code in (("TC-001", "API-GOOD", 0), ("TC-002", "API-BAD", 1)):
            self.command("plan", "link", row, case)
            self.command("run", self.project, "--case", case, code=code)
        reason = "未提供审批人账号，批准业务未执行；原预期保持不变。"
        self.command("plan", "mark", "TC-003", "未执行", "--note", reason)
        self.command("plan", "mark", "TC-004", "通过", "--note", "手工通过不是执行证据")
        before = case_plans.get(self.project, "public-reuse")
        self.assertEqual([row["status"] for row in before["rows"]], ["Pass", "Fail", "NotRun", "NotRun"])
        version = before["version"]
        self.command("plan", "link", "TC-004", "API-GOOD")
        frozen_path = Path(before["folder"]) / "history" / str(version) / "snapshot.json"
        frozen = frozen_path.read_bytes()
        self.command("run", self.project, "--case", "API-GOOD")
        complete = self.command("plan", "show")
        self.assertEqual((complete["summary"]["通过"], complete["summary"]["失败"], complete["summary"]["未执行"]), (2, 1, 1))
        completed_excel = Path(complete["delivery_excel"]).read_bytes()
        source.write_text(json.dumps([{"id": "NEXT", "title": "A new independent task"}]), encoding="utf-8")
        replacement = self.command("plan", "write", self.project, "--file", str(source))
        self.assertNotEqual(replacement["task_id"], written["task_id"])
        replacement_excel = Path(replacement["delivery_excel"]).read_bytes()
        counts = db.one("SELECT (SELECT count(*) FROM runs) AS runs, (SELECT count(*) FROM results) AS results")
        with TestClient(create_app(start_worker=False), base_url="http://127.0.0.1") as client, \
                patch.object(task_reports, "_render", side_effect=AssertionError("Read-only history must not render")), \
                patch.object(runner, "execute", side_effect=AssertionError("Read-only history must not execute")):
            address = f"/api/case-plans/{self.project}/history/{version}"
            historical = client.get(address + "?session=public-reuse")
            self.assertEqual(historical.status_code, 200)
            exported = client.get(address + "/excel?session=public-reuse")
            self.assertEqual(exported.status_code, 200)
            with patch("zipfile.time.localtime", return_value=(2030, 2, 3, 4, 5, 6, 0, 0, -1)):
                repeated = client.get(address + "/excel?session=public-reuse")
            self.assertEqual(repeated.status_code, 200)
            self.assertEqual(repeated.content, exported.content)
            deliveries = client.get(f"/api/case-plans/{self.project}/deliveries?session=public-reuse").json()["deliveries"]
            self.assertEqual([item["task_id"] for item in deliveries], [written["task_id"]])
            receipt = deliveries[0]["report"]
            self.assertEqual(receipt["report_id"], complete["test_report"]["report_id"])
            self.assertEqual(receipt["summary_counts"], {"Pass": 2, "Fail": 1, "NotRun": 1})
            original_download = client.get(receipt["download_url"])
            self.assertEqual(original_download.status_code, 200)
            self.assertEqual(hashlib.sha256(original_download.content).hexdigest(), receipt["sha256"])
            self.assertEqual(original_download.content, completed_excel)
            self.assertEqual(client.get(f"/api/case-plans/{self.project}/deliveries?session=another-task").json()["deliveries"], [])
        saved = historical.json()
        self.assertEqual(saved["summary"], before["summary"])
        self.assertEqual([row["status"] for row in saved["rows"]], ["Pass", "Fail", "NotRun", "NotRun"])
        self.assertIsNone(saved["rows"][2]["evidence"]["result"])
        self.assertFalse(saved["rows"][2]["evidence"]["current"])
        book = load_workbook(io.BytesIO(exported.content))
        self.addCleanup(book.close)
        main_rows = {row[3]: row for row in book["功能测试用例"].values if row[9] in {"通过", "失败", "阻塞", "未执行"}}
        self.assertEqual([main_rows[f"Original design row {number}"][9] for number in range(1, 5)], ["通过", "失败", "未执行", "未执行"])
        self.assertIn(reason, main_rows["Original design row 3"][12])
        self.assertIn("没有本行执行回执", main_rows["Original design row 3"][12])
        overview = {row[0]: row[1] for row in book["任务概览"].values}
        self.assertEqual({key: overview[key] for key in ("通过", "失败", "未执行")}, {"通过": 1, "失败": 1, "未执行": 2})
        self.assertEqual(db.one("SELECT (SELECT count(*) FROM runs) AS runs, (SELECT count(*) FROM results) AS results"), counts)
        self.assertEqual(frozen_path.read_bytes(), frozen)
        self.assertEqual(Path(written["initial_excel"]).read_bytes(), initial)
        self.assertEqual(Path(complete["delivery_excel"]).read_bytes(), completed_excel)
        self.assertEqual(Path(replacement["delivery_excel"]).read_bytes(), replacement_excel)
        self.assertEqual(hashlib.sha256(frozen).hexdigest(), hashlib.sha256(frozen_path.read_bytes()).hexdigest())
        if os.environ.get("AUTOSKILL_CHROMIUM"):
            self.assert_browser_history(written["task_id"], version, receipt, reason)
        self.assertEqual(frozen_path.read_bytes(), frozen)
        self.assertEqual(Path(complete["delivery_excel"]).read_bytes(), completed_excel)
        self.assertEqual(Path(replacement["delivery_excel"]).read_bytes(), replacement_excel)
        self.assertEqual(db.one("SELECT (SELECT count(*) FROM runs) AS runs, (SELECT count(*) FROM results) AS results"), counts)

    def assert_browser_history(self, old_task, version, receipt, reason):
        from playwright.sync_api import expect, sync_playwright
        from test_data_dashboard import DataServer

        service = DataServer(self.folder)
        self.addCleanup(service.close)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=os.environ["AUTOSKILL_CHROMIUM"], headless=True, args=["--no-sandbox"])
            try:
                page = browser.new_page(viewport={"width": 1200, "height": 1000})
                requests = []
                page.on("request", lambda request: requests.append(request.url) if "/deliveries?" in request.url else None)
                page.goto(service.origin + f"/#/case-plans?projects={self.project}&session=public-reuse")
                expect(page.locator('[data-case="NEXT"]')).to_be_visible()
                self.assertEqual(requests, [])
                page.get_by_text("用例表选项", exact=True).click()
                page.get_by_role("button", name="历史版本", exact=True).click()
                previous = page.locator(f'[data-task="{old_task}"]')
                expect(previous).to_be_visible()
                expect(previous).to_contain_text("1 未执行 · 2 通过 · 1 失败")
                self.assertEqual(len(requests), 1)
                download_link = previous.get_by_role("link", name="下载原任务 Excel", exact=True)
                self.assertEqual(download_link.get_attribute("href"), receipt["download_url"])
                with page.expect_download() as saved:
                    download_link.click()
                self.assertEqual(hashlib.sha256(Path(saved.value.path()).read_bytes()).hexdigest(), receipt["sha256"])
                page.get_by_role("button", name=f"查看版本 {version}", exact=True).click()
                blocked = page.locator('section[aria-label="设计历史版本"] [data-case="TC-003"]')
                expect(blocked).to_be_visible()
                expect(blocked).to_contain_text("未执行")
                expect(blocked).to_contain_text(reason)
                expect(previous).to_be_visible()
                evidence = os.environ.get("AUTOSKILL_REUSE_EVIDENCE")
                if evidence:
                    page.screenshot(path=str(Path(evidence) / "history-final-delivery.png"), full_page=True)
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
