"""Public reuse preserves tested flow order and each explicitly linked design row's real execution."""
import copy
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from openpyxl import load_workbook

from autoskill import builder, candidate_publication, case_plans, db, execution_inputs, plan_execution, projects, runner
from autoskill.server.app import create_app


class FlowTarget(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return

    def answer(self, status, payload):
        self.server.requests.append((self.command, self.path, status))
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or "{}")
        if self.path == "/login":
            self.server.token += 1
            return self.answer(200, {"token": f"token-{self.server.token}", "authenticated": True})
        if self.headers.get("Authorization") != f"Bearer token-{self.server.token}":
            return self.answer(401, {})
        self.server.item += 1
        self.server.alive = True
        return self.answer(201, {"id": self.server.item, "amount": payload.get("amount")})

    def do_GET(self):
        if self.path == "/unrelated":
            return self.answer(200, {"ok": True})
        if not self.server.alive:
            return self.answer(404, {"deleted": True})
        return self.answer(200, {"price": 10 if self.server.correct else 99})

    def do_DELETE(self):
        self.server.alive = False
        self.answer(200, {"deleted": True})


class PublicReuseFlowTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": folder.name, "AUTOSKILL_SESSION": "public-reuse",
                                              "AUTOSKILL_CASE_SKILL": str(self.folder / "sheet"), "AUTOSKILL_WORKFLOW_BINDING": "",
                                              "AUTOSKILL_BRIDGE": "off", "AUTOSKILL_HEADED": "0"})
        environment.start()
        self.addCleanup(environment.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FlowTarget)
        self.server.requests, self.server.token, self.server.item = [], 0, 0
        self.server.alive, self.server.correct = False, False
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .02}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.project = projects.create("公开复用", f"http://127.0.0.1:{self.server.server_port}", project_id="public-reuse")["id"]
        self.inputs = self.folder / "inputs.json"
        self.inputs.write_text('{"amount":10}', encoding="utf-8")

    def cases(self):
        def api(identity, method, path, expected, **options):
            case = builder.api_case(identity, identity, method, path, expected, **options)
            case["extensions"]["execution"] = {"api_only": True}
            return case
        login = api("Z-LOGIN", "POST", "/login", {"status": 200, "json_equals": {"/authenticated": True}}, extract={"token": "/token"})
        create = api("Y-CREATE", "POST", "/items", {"status": 201, "json_equals": {"/amount": "{{amount}}"}},
                     json={"amount": "{{amount}}"}, headers={"Authorization": "Bearer {{token}}"},
                     depends_on=[login["id"]], extract={"item_id": "/id"})
        check = api("M-CHECK", "GET", "/items/{{item_id}}", {"status": 200, "json_equals": {"/price": 10}}, depends_on=[create["id"]])
        cleanup = api("A-DELETE", "DELETE", "/items/{{item_id}}", {"status": 200, "json_equals": {"/deleted": True}}, depends_on=[create["id"]])
        gone = api("B-GONE", "GET", "/items/{{item_id}}", {"status": 404, "json_equals": {"/deleted": True}}, depends_on=[cleanup["id"]])
        return [login, create, check, cleanup, gone]

    def command(self, *arguments, code=0):
        environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "skills/autoskill/app")}
        result = subprocess.run([sys.executable, "-m", "autoskill", *arguments], env=environment, capture_output=True, text=True, timeout=45, check=False)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def queued(self, task, **options):
        queued = candidate_publication.queue(task["id"], inputs={"amount": 10}, start=False, **options)
        return queued, runner.execute(queued["run_id"])

    def test_partial_publication_then_selected_retry_preserves_nonlexical_flow_order(self):
        cases = self.cases()
        unrelated = builder.api_case("OUTSIDE-1", "Other operation", "GET", "/unrelated", {"status": 200, "json_equals": {"/ok": True}})
        projects.save_cases(self.project, [unrelated])
        task = candidate_publication.stage(self.project, cases, {}, {"fixture": "ordered-partial"})
        first, failed = self.queued(task)
        self.assertEqual(failed["status"], "Fail")
        self.assertEqual([row["case_id"] for row in db.rows("SELECT * FROM results WHERE run_id=? ORDER BY id", (first["run_id"],))], [case["id"] for case in cases])
        self.assertFalse(self.server.alive)
        self.assertEqual(candidate_publication.receipt(first["run_id"])["published_ids"], sorted(case["id"] for case in cases if case["id"] != "M-CHECK"))
        other = {**unrelated, "id": "OUTSIDE-2"}
        projects.save_cases(self.project, [other])
        self.server.correct = True
        second, passed = self.queued(task, case_ids=["M-CHECK"])
        self.assertEqual(passed["status"], "Pass")
        self.assertIn("M-CHECK", candidate_publication.receipt(second["run_id"])["published_ids"])
        current = projects.cases(self.project)
        self.assertEqual([case["id"] for case in current if not case["id"].startswith("OUTSIDE")], [case["id"] for case in cases])
        self.assertEqual([case["id"] for case in current if case["id"].startswith("OUTSIDE")], ["OUTSIDE-1", "OUTSIDE-2"])
        for case in cases:
            self.assertEqual(projects.case(self.project, case["id"]), case)
        self.server.requests = []
        final = self.command("run", self.project, "--kind", "api", "--api-only", "--inputs", str(self.inputs))
        self.assertEqual(final["status"], "Pass")
        self.assertEqual([method for method, path, _ in self.server.requests if path != "/unrelated"], ["POST", "POST", "GET", "DELETE", "GET"])
        self.assertFalse(self.server.alive)
        self.assertEqual(db.one("SELECT status FROM runs WHERE id=?", (first["run_id"],))["status"], "Fail")

    def test_failed_new_definition_cannot_reorder_an_older_saved_case(self):
        cases = self.cases()
        self.server.correct = True
        original = [cases[0], cases[1], cases[3], cases[4], cases[2]]
        projects.save_cases(self.project, original)
        changed = copy.deepcopy(cases)
        changed[2]["assertions"]["json_equals"]["/price"] = 88
        task = candidate_publication.stage(self.project, changed, {}, {"fixture": "failed-replacement"})
        _, final = self.queued(task)
        self.assertEqual(final["status"], "Fail")
        self.assertEqual(projects.cases(self.project), original)

    def test_direct_workflow_publication_uses_frozen_execution_order(self):
        cases = self.cases()
        self.server.correct = True
        run = runner.create(self.project, [case["id"] for case in cases], candidate_cases=cases, api_only=True, inputs={"amount": 10})
        self.assertEqual(runner.execute(run["id"])["status"], "Pass")
        receipt = candidate_publication.publish(self.project, list(reversed(cases)), [run["id"]],
                                                expected_hashes={case["id"]: None for case in cases})
        self.assertEqual(receipt["published_ids"], sorted(case["id"] for case in cases))
        self.assertEqual(projects.cases(self.project), cases)

    def linked_public_flow(self, business_path=None):
        cases = self.cases()
        self.server.correct = True
        projects.save_cases(self.project, cases)
        design = self.folder / "design.json"
        design.write_text(json.dumps([{"id": f"DESIGN-{number}", "title": case["title"], "steps": "执行本行业务操作", "expected": "满足独立业务契约",
                                       **({"business_path": business_path} if business_path else {})}
                                      for number, case in enumerate(cases, 1)] + [{"id": "DESIGN-6", "title": "Unexecuted row"}]), encoding="utf-8")
        written = self.command("plan", "write", self.project, "--file", str(design))
        initial = Path(written["initial_excel"]).read_bytes()
        for number, case in enumerate(cases, 1):
            linked = self.command("plan", "link", f"DESIGN-{number}", case["id"])
            self.assertEqual(linked["task_id"], written["task_id"])
        linked_rows = self.command("plan", "show")["cases"]
        self.assertTrue(all(row.get("execution_source") for row in linked_rows))
        self.assertEqual([row.get("business_path") for row in linked_rows[:5]], [business_path] * 5)
        self.assertEqual(self.server.requests, [])
        self.assertEqual(self.command("plan", "show")["summary"]["未执行"], 6)
        final = self.command("run", self.project, "--case", "M-CHECK", "--case", "B-GONE", "--api-only", "--inputs", str(self.inputs))
        self.assertEqual(final["status"], "Pass")
        run_id = final["run_id"]
        run = db.one("SELECT * FROM runs WHERE id=?", (run_id,))
        self.assertEqual(run["total"], 2)
        results = db.rows("SELECT * FROM results WHERE run_id=? ORDER BY id", (run_id,))
        self.assertEqual([row["prerequisite"] for row in results], [1, 1, 0, 1, 0])
        shown = self.command("plan", "show")
        self.assertEqual(shown["task_id"], written["task_id"])
        self.assertEqual(final["test_report"]["summary_counts"]["Pass"], 2)
        self.assertEqual(Path(shown["initial_excel"]).read_bytes(), initial)
        view = case_plans.get(self.project, "public-reuse")
        self.assertTrue(all(row["evidence"].get("latest_execution") for row in view["rows"][:5]),
                        json.dumps({"evidence": [row["evidence"] for row in view["rows"]], "bindings": plan_execution._read()}, ensure_ascii=False))
        self.assertEqual([row["evidence"]["latest_execution"]["run_id"] for row in view["rows"][:5]], [run_id] * 5)
        self.assertEqual([row["evidence"]["latest_execution"]["status"] for row in view["rows"][:5]], ["Pass"] * 5)
        # Explicit run inputs do not establish that today's default input scope has passed.
        self.assertEqual([row["evidence"]["current"] for row in view["rows"][:5]], [False] * 5)
        self.assertEqual(view["rows"][5]["status"], "NotRun")
        self.assertEqual(len(plan_execution._read()["bindings"]), 5)
        for row in results:
            self.assertTrue(execution_inputs.result_evidence(run, row, execution_inputs.load(run)["cases"][row["case_id"]]))
        book = load_workbook(shown["delivery_excel"])
        self.addCleanup(book.close)
        self.assertEqual([row[9] for row in book["功能测试用例"].values if row[9] in {"通过", "未执行"}], ["通过"] * 5 + ["未执行"])

    def test_public_link_and_selected_replay_update_real_prerequisites_in_original_full_sheet(self):
        self.linked_public_flow()

    def test_public_link_keeps_explicit_business_path_and_original_design_identity(self):
        self.linked_public_flow(["真实预订业务", "独立资源生命周期"])

    def test_unlinked_manual_block_is_visible_but_manual_pass_is_not_execution(self):
        source = self.folder / "design.json"
        source.write_text(json.dumps([{"id": "BLOCKED", "title": "Unavailable input"}, {"id": "MANUAL", "title": "No execution proof"}]), encoding="utf-8")
        written = self.command("plan", "write", self.project, "--file", str(source))
        reason = "业务输入尚未提供，无法开始实际操作"
        self.command("plan", "mark", "BLOCKED", "阻塞", "--note", reason)
        self.command("plan", "mark", "MANUAL", "通过", "--note", "手填不构成证据")
        with TestClient(create_app(start_worker=False), base_url="http://127.0.0.1") as client:
            response = client.get(f"/api/case-plans/{self.project}?session=public-reuse")
            self.assertEqual(response.status_code, 200)
            view = response.json()
        self.assertEqual([row["status"] for row in view["rows"]], ["Blocked", "NotRun"])
        self.assertIn(reason, view["rows"][0]["evidence"]["reason"])
        self.assertIn("没有本行执行回执", view["rows"][0]["evidence"]["reason"])
        self.assertIsNone(view["rows"][0]["evidence"]["result"])
        self.assertFalse(view["rows"][0]["evidence"]["current"])
        self.assertEqual(view["test_report"]["summary_counts"]["Blocked"], 1)
        book = load_workbook(written["delivery_excel"])
        self.addCleanup(book.close)
        self.assertEqual([row[9] for row in book["功能测试用例"].values if row[9] in {"阻塞", "未执行"}], ["阻塞", "未执行"])

    @unittest.skipUnless(os.environ.get("AUTOSKILL_CHROMIUM"), "Real Chromium is required")
    def test_real_dashboard_keeps_unlinked_blocked_reason_visible(self):
        from playwright.sync_api import expect, sync_playwright
        from test_data_dashboard import DataServer

        with patch.dict(os.environ, {"AUTOSKILL_CASE_SKILL": ""}):
            source = self.folder / "browser-design.json"
            source.write_text(json.dumps([{"id": "BLOCKED", "title": "缺少业务输入"}]), encoding="utf-8")
            self.command("plan", "write", self.project, "--file", str(source))
            reason = "业务输入尚未提供，无法开始实际操作"
            self.command("plan", "mark", "BLOCKED", "阻塞", "--note", reason)
            service = DataServer(self.folder)
            self.addCleanup(service.close)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=os.environ["AUTOSKILL_CHROMIUM"], headless=True, args=["--no-sandbox"])
                try:
                    page = browser.new_page(viewport={"width": 1100, "height": 850})
                    page.goto(service.origin + f"/#/case-plans?projects={self.project}&session=public-reuse")
                    row = page.locator('[data-case="BLOCKED"]')
                    expect(row).to_be_visible()
                    expect(row).to_contain_text("阻塞")
                    expect(row).to_contain_text(reason)
                    expect(row).to_contain_text("没有本行执行回执")
                    self.assertNotIn("尚未关联可执行用例", row.inner_text())
                    evidence = os.environ.get("AUTOSKILL_REUSE_EVIDENCE")
                    if evidence:
                        page.screenshot(path=str(Path(evidence) / "unlinked-blocked.png"), full_page=True)
                finally:
                    browser.close()


if __name__ == "__main__":
    unittest.main()
