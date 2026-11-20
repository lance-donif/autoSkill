"""Complete HTTP case edits preserve action identities, conditional revisions and actual evidence."""
import copy
import hashlib
import json
import os
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from fastapi.testclient import TestClient

from autoskill import builder, db, execution_inputs, projects, runner, task_reports
from autoskill.server.app import create_app


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        return

    def do_GET(self):
        data = b'{"ok":true,"value":7}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class CaseEditingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.root / "data"), "AUTOSKILL_CASE_SKILL": str(self.root / "plan"),
                                              "AUTOSKILL_HEADED": "0", "AUTOSKILL_SESSION": "", "AUTOSKILL_WORKFLOW_BINDING": "", "AUTOSKILL_BRIDGE": "off"})
        environment.start()
        self.addCleanup(environment.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .02}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.pid = projects.create("合成就绪检查", f"http://127.0.0.1:{self.server.server_port}", project_id="case-edit")["id"]
        document = {"description": "验证查询服务就绪状态后返回真实就绪标记。", "preconditions": "本地合成就绪检查服务已启动。",
                    "steps": ["1. 查询服务就绪状态。", "2. 核对返回的就绪标记。"], "expected": "2. 就绪标记 ok 为 true，数值为 7，不得为负。",
                    "source": {"kind": "synthetic_requirement", "id": "READY", "text": "服务以 ok=true 与 value=7 表示就绪。"},
                    "business_path": ["合成就绪检查", "服务管理", "就绪查询"], "author": "合成需求编写人员"}
        self.api = builder.api_case("API", "查询服务就绪状态", "GET", "/health", {"status": 200, "json_equals": {"/ok": True, "/value": 7}}, test_case_document=document)
        dependent = builder.api_case("DEPENDENT", "核对前置服务后查询数值", "GET", "/health", {"status": 200, "json_equals": {"/value": 7}}, depends_on=["API"])
        failing = builder.api_case("FAIL", "保留不符合预期的真实结果", "GET", "/health", {"status": 200, "json_equals": {"/ok": False}})
        self.ui = builder.ui_case("UI", "验证两个合成按钮的连续操作", {
            "start_url": "/ui", "actions": [
                {"action": "click", "selector": "#first", "custom_note": {"origin": "保留未知扩展"}},
                {"assertion": "text", "selector": "#state", "expected": "第一步完成"},
                {"action": "click", "selector": "#second"}],
            "assertions": [{"assertion": "text", "selector": "#state", "expected": "完成"}]})
        projects.save_cases(self.pid, [self.api, dependent, failing, self.ui])
        self.app = create_app(start_worker=False)
        self.client = TestClient(self.app, base_url="http://127.0.0.1:8787")
        self.addCleanup(self.client.close)

    def read(self, identity="API"):
        response = self.client.get(f"/api/case-definitions/{self.pid}/{identity}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def edit(self, current, definition=None, *, client=None):
        candidate = copy.deepcopy(definition if definition is not None else current["case"])
        for binding in current["editing"]["actions"]:
            candidate["extensions"]["automation"]["actions"][binding["index"]]["_edit_id"] = binding["id"]
        return (client or self.client).put(f"/api/projects/{self.pid}/cases/{current['case']['id']}",
                                         json={"expected_content_revision": current["content_revision"], "case": candidate})

    def project_bytes(self):
        folder = projects.path(self.pid)
        return {str(file.relative_to(folder)): file.read_bytes() for directory in (folder / "testcases", folder / "analysis", folder / "versions")
                for file in directory.rglob("*") if file.is_file()}

    def run_api(self, *identities):
        with patch.object(runner, "spawn", return_value=None):
            run = runner.create(self.pid, list(identities))
        return runner.execute(run["id"])

    def test_get_is_complete_and_noop_ui_api_save_neither_versions_nor_runs(self):
        for identity in ("API", "UI"):
            current = self.read(identity)
            self.assertEqual(current["case"], projects.case(self.pid, identity))
            before = self.project_bytes()
            runs = db.one("SELECT COUNT(*) n FROM runs")["n"]
            response = self.edit(current)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertFalse(response.json()["changed"])
            self.assertEqual(response.json()["case"], current["case"])
            self.assertEqual(response.json()["content_revision"], current["content_revision"])
            self.assertEqual(response.json()["verification"]["status"], "unchanged")
            self.assertEqual(self.project_bytes(), before)
            self.assertEqual(db.one("SELECT COUNT(*) n FROM runs")["n"], runs)
        self.assertEqual([item["index"] for item in self.read("UI")["editing"]["actions"]], [0, 2])

    def test_concurrent_editors_have_one_winner_and_stale_submit_has_no_extra_writes(self):
        first, second = self.read(), self.read()
        barrier = threading.Barrier(2)

        def save(current, title):
            candidate = copy.deepcopy(current["case"])
            candidate["title"] = title
            with TestClient(self.app, base_url="http://127.0.0.1:8787") as client:
                barrier.wait(5)
                return self.edit(current, candidate, client=client)

        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = [future.result() for future in [pool.submit(save, first, "查询就绪结果甲"), pool.submit(save, second, "查询就绪结果乙")]]
        self.assertEqual(sorted(reply.status_code for reply in replies), [200, 409])
        self.assertEqual(projects.case(self.pid, "API")["revision"], first["case"]["revision"] + 1)
        stale = next(reply.json() for reply in replies if reply.status_code == 409)
        self.assertEqual(stale["code"], "revision_conflict")
        self.assertEqual(stale["content_revision"], self.read()["content_revision"])
        before = self.project_bytes()
        self.assertEqual(self.edit(first).status_code, 409)
        self.assertEqual(self.project_bytes(), before)
        self.assertEqual(db.one("SELECT COUNT(*) n FROM runs")["n"], 0)

    def test_legacy_ui_without_automation_can_edit_prose_without_inventing_actions(self):
        legacy = copy.deepcopy(self.ui)
        legacy["id"] = "LEGACY"
        legacy["extensions"].pop("automation")
        projects.save_cases(self.pid, [legacy])
        current = self.read("LEGACY")
        self.assertEqual(current["editing"]["actions"], [])
        candidate = copy.deepcopy(current["case"])
        candidate["title"] = "手工维护待录制用例的功能目标"
        response = self.edit(current, candidate)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("automation", response.json()["case"]["extensions"])
        self.assertEqual(db.one("SELECT COUNT(*) n FROM runs")["n"], 0)

    def test_full_api_request_edit_preserves_untouched_cases_and_does_not_run(self):
        current = self.read()
        untouched = {case["id"]: copy.deepcopy(case) for case in projects.cases(self.pid) if case["id"] != "API"}
        candidate = copy.deepcopy(current["case"])
        candidate.update(method="POST", path="/query", headers={"X-Test-Identity": "合成读取身份"}, params={"group": "ready"},
                         json={"filter": ["就绪", 7]}, test_data={"note": "手工准备查询条件"})
        candidate["extensions"]["extract"] = {"ready_value": "/value"}
        result = self.edit(current, candidate)
        self.assertEqual(result.status_code, 200, result.text)
        body = result.json()
        self.assertEqual(body["case"]["method"], "POST")
        self.assertEqual(body["case"]["json"], candidate["json"])
        self.assertEqual(body["case"]["extensions"]["test_case_document"], current["case"]["extensions"]["test_case_document"])
        self.assertEqual(body["verification"]["status"], "NotRun")
        self.assertEqual(body["case"]["revision"], current["case"]["revision"] + 1)
        self.assertEqual(untouched, {case["id"]: case for case in projects.cases(self.pid) if case["id"] != "API"})
        self.assertEqual(db.one("SELECT COUNT(*) n FROM runs")["n"], 0)

    def test_schema_identity_api_contract_requirements_and_dependency_errors_do_not_write(self):
        current = self.read()
        invalid = []
        for key, value in (("id", "OTHER"), ("kind", "ui"), ("schema_version", "9"), ("title", ""), ("requirement_ids", ["MISSING"]), ("extensions", None)):
            candidate = copy.deepcopy(current["case"])
            candidate[key] = value
            invalid.append(candidate)
        for dependencies in (["API"], ["MISSING"], ["DEPENDENT"], ["FAIL", "FAIL"], "FAIL"):
            candidate = copy.deepcopy(current["case"])
            candidate["extensions"]["depends_on"] = dependencies
            invalid.append(candidate)
        for assertions in ({"status": 200, "json_equals": {"not-a-pointer": True}}, {"status": 200, "schema": {"$ref": "https://example.invalid/schema"}}):
            candidate = copy.deepcopy(current["case"])
            candidate["assertions"] = assertions
            invalid.append(candidate)
        before = self.project_bytes()
        for candidate in invalid:
            with self.subTest(candidate=candidate):
                response = self.edit(current, candidate)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(self.project_bytes(), before)

        candidate = copy.deepcopy(current["case"])
        candidate["assertions"] = {"status": 200}
        response = self.edit(current, candidate)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.read()["case"]["assertions"], {"status": 200})

    def test_action_identities_reject_add_delete_duplicate_and_swap_even_with_same_action_kind(self):
        current = self.read("UI")
        candidate = copy.deepcopy(current["case"])
        for binding in current["editing"]["actions"]:
            candidate["extensions"]["automation"]["actions"][binding["index"]]["_edit_id"] = binding["id"]
        actions = candidate["extensions"]["automation"]["actions"]
        invalid = [[actions[0]], [*actions, copy.deepcopy(actions[-1])], [actions[-1], actions[1], actions[0]],
                   [actions[0], actions[1], copy.deepcopy(actions[0])]]
        before = self.project_bytes()
        for incoming in invalid:
            changed = copy.deepcopy(candidate)
            changed["extensions"]["automation"]["actions"] = incoming
            response = self.client.put(f"/api/projects/{self.pid}/cases/UI", json={"expected_content_revision": current["content_revision"], "case": changed})
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(self.project_bytes(), before)

    def test_action_type_can_change_and_inline_and_final_assertions_can_be_removed_or_added(self):
        current = self.read("UI")
        candidate = copy.deepcopy(current["case"])
        automation = candidate["extensions"]["automation"]
        for binding in current["editing"]["actions"]:
            automation["actions"][binding["index"]]["_edit_id"] = binding["id"]
        automation["actions"][0].update(action="goto", url="/new-screen", value="obsolete", key="obsolete")
        automation["actions"].pop(1)
        automation["assertions"] = [{"assertion": "count", "selector": "#items", "expected": 0}, {"assertion": "title", "expected": "新页面"}]
        response = self.client.put(f"/api/projects/{self.pid}/cases/UI", json={"expected_content_revision": current["content_revision"], "case": candidate})
        self.assertEqual(response.status_code, 200, response.text)
        saved = response.json()["case"]["extensions"]["automation"]
        self.assertEqual([step["action"] for step in saved["actions"]], ["goto", "click"])
        self.assertEqual(saved["actions"][0]["custom_note"], {"origin": "保留未知扩展"})
        self.assertFalse({"selector", "value", "key", "_edit_id"} & set(saved["actions"][0]))
        self.assertEqual(saved["assertions"], automation["assertions"])

    def test_changed_manual_expected_keeps_actual_pass_unverified_and_old_failure_report_bytes(self):
        failure = self.run_api("FAIL")
        self.assertEqual(failure["status"], "Fail")
        report = task_reports.get(failure["test_report"]["report_id"])
        files = {key: Path(report[key]) for key in ("excel", "markdown", "bugs")}
        previous_bytes = {key: file.read_bytes() for key, file in files.items()}
        initial = self.run_api("API")
        self.assertEqual(initial["status"], "Pass")
        old_result = db.one("SELECT * FROM results WHERE run_id = ? AND prerequisite = 0", (initial["id"],))
        current = self.read()
        candidate = copy.deepcopy(current["case"])
        candidate["expected"] = "2. 就绪标记为 true，数值必须是 7 毫秒，不得按秒解释。"
        response = self.edit(current, candidate)
        self.assertEqual(response.status_code, 200, response.text)
        updated = response.json()["case"]
        self.assertEqual(updated["extensions"]["test_case_document"]["expected"], candidate["expected"])
        self.assertEqual(updated["extensions"]["functional_design"]["state"], "unmapped")
        self.assertFalse(execution_inputs.result_current(self.pid, updated, old_result))
        second = self.run_api("API")
        self.assertEqual(second["status"], "Pass")
        result = db.one("SELECT detail FROM results WHERE run_id = ? AND prerequisite = 0", (second["id"],))
        self.assertEqual(json.loads(result["detail"])["business_status"], "Unverified")
        self.assertEqual({key: file.read_bytes() for key, file in files.items()}, previous_bytes)
        self.assertEqual(db.one("SELECT status FROM runs WHERE id = ?", (failure["id"],))["status"], "Fail")
        again = self.read()
        forged = copy.deepcopy(again["case"])
        forged["extensions"].pop("functional_design")
        self.assertEqual(self.edit(again, forged).json()["case"]["extensions"]["functional_design"]["state"], "unmapped")

    def test_current_list_invalidates_dependency_scope_without_rewriting_history(self):
        first = self.run_api("API")
        second = self.run_api("DEPENDENT")
        self.assertEqual((first["status"], second["status"]), ("Pass", "Pass"))
        before = self.client.get("/api/cases").json()["items"]
        self.assertEqual({item["case_id"]: item["current_status"] for item in before if item["case_id"] in ("API", "DEPENDENT")}, {"API": "Pass", "DEPENDENT": "Pass"})
        dependent = copy.deepcopy(projects.case(self.pid, "DEPENDENT"))
        current = self.read()
        candidate = copy.deepcopy(current["case"])
        candidate["title"] = "重新命名服务查询步骤"
        self.assertEqual(self.edit(current, candidate).status_code, 200)
        rows = {item["case_id"]: item for item in self.client.get("/api/cases").json()["items"]}
        for identity in ("API", "DEPENDENT"):
            self.assertEqual(rows[identity]["last_status"], "Pass")
            self.assertIsNone(rows[identity]["current_status"])
            self.assertFalse(rows[identity]["current"])
        self.assertEqual(projects.case(self.pid, "DEPENDENT"), dependent)
        old = db.one("SELECT * FROM results WHERE run_id = ? AND prerequisite = 0", (second["id"],))
        self.assertFalse(execution_inputs.result_current(self.pid, dependent, old))
        self.assertEqual(hashlib.sha256(Path(second["test_report"]["excel"]).read_bytes()).hexdigest(), second["test_report"]["sha256"])
        for identity in ("API", "DEPENDENT"):
            detail = self.client.get(f"/api/projects/{self.pid}/cases/{identity}").json()
            self.assertIsNone(detail["current_status"])
            self.assertEqual(detail["last_status"], "Pass")

    def test_conflicting_business_copies_are_rejected_and_rich_source_is_preserved(self):
        current = self.read()
        candidate = copy.deepcopy(current["case"])
        candidate["expected"] = "2. 数值为7。"
        candidate["extensions"]["test_case_document"]["expected"] = "2. 数值不得为7。"
        before = self.project_bytes()
        reply = self.edit(current, candidate)
        self.assertEqual(reply.status_code, 400, reply.text)
        self.assertEqual(self.project_bytes(), before)
        candidate = copy.deepcopy(current["case"])
        candidate["extensions"]["test_case_document"].update(author="新的手工维护人员", remarks="原否定和单位不改写", prefix="JX", business_path=["合成就绪检查", "运维", "就绪查询"])
        response = self.edit(current, candidate)
        self.assertEqual(response.status_code, 200, response.text)
        document = response.json()["case"]["extensions"]["test_case_document"]
        self.assertEqual(document["source"], current["case"]["extensions"]["test_case_document"]["source"])
        self.assertEqual(document["expected"], current["case"]["expected"])
        self.assertEqual(document["business_path"], candidate["extensions"]["test_case_document"]["business_path"])


if __name__ == "__main__":
    unittest.main()
