"""A hundred saved cases identify twenty linked changes and publish only verified scoped candidates."""
import copy
import json
import os
import sys
import tempfile
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))
from autoskill import builder, db, execution_inputs, projects, runner, workflow, workflow_changes, workflow_definition, workflow_state
from autoskill.core import store


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        return

    def do_GET(self):
        data = json.dumps({"priority": "medium"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class ChangeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": self.directory.name, "AUTOSKILL_HEADED": "0"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .02}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.project = projects.create("需求迭代", f"http://127.0.0.1:{self.server.server_port}", project_id="changes")["id"]
        self.spawn = patch.object(runner, "spawn", return_value=None)
        self.spawn.start()
        self.addCleanup(self.spawn.stop)
        self.worker = patch("autoskill.server.jobs.ensure_worker", return_value={"status": "test"})
        self.worker.start()
        self.addCleanup(self.worker.stop)
        self.cases = []
        store.write(projects.path(self.project) / "analysis/requirements.json",
                    [{"id": f"REQ-{number:03}", "module": "业务", "title": "业务预期", "source": "原需求"} for number in range(100)]
                    + [{"id": "MISSING", "module": "业务", "title": "缺少本工作流关联", "source": "其他文档"}])
        for number in range(100):
            case = builder.api_case(f"CASE-{number:03}", f"业务用例 {number}", "GET", "/answer",
                                    {"status": 200, "json_equals": {"/priority": "high"}})
            case["requirement_ids"] = [f"REQ-{number:03}"]
            self.cases.append(case)
        projects.save_cases(self.project, self.cases)
        self.flow = workflow.create(self.project, {"title": "原需求", "sources": [{"id": "changed", "text": "前20项优先级 high"},
                                                                                   {"id": "stable", "text": "后80项规则不变"}]},
                                    idempotency_key=uuid.uuid4().hex)["workflow_id"]
        self.submit(self.flow, {"requirements": [{"id": f"REQ-{number:03}", "text": "优先级 high", "source_ids": ["changed" if number < 20 else "stable"]}
                                                for number in range(100)]})
        self.submit(self.flow, {"scenarios": [{"id": f"SC-{number:03}", "title": "业务预期", "requirement_ids": [f"REQ-{number:03}"], "oracle_ids": [f"O-{number:03}"]}
                                              for number in range(100)],
                                "oracles": [{"id": f"O-{number:03}", "kind": "api", "requirement_ids": [f"REQ-{number:03}"],
                                             "checks": self.cases[number]["assertions"]} for number in range(100)]})
        self.submit(self.flow, {"cases": [], "bindings": {f"SC-{number:03}": [self.cases[number]["id"]] for number in range(100)}})

    def submit(self, identity, payload):
        task = workflow.next_task(identity, idempotency_key=uuid.uuid4().hex)["task"]
        self.assertIsNotNone(task)
        return workflow.submit(identity, task["item_id"], lease_token=task["lease_token"], attempt_id=task["attempt_id"],
                               idempotency_key=uuid.uuid4().hex, payload=payload)

    def request(self, **extra):
        body = {"sources": [{"id": "changed", "text": "前20项优先级 medium，其他规则不变"}], **extra}
        analysis = workflow_changes.analyze(self.flow, body)
        return {**body, "analysis_hash": analysis["analysis_hash"], "case_ids": analysis["direct_case_ids"]}

    def test_twenty_out_of_hundred_are_scoped_and_eighty_unchanged_after_real_regression(self):
        old_run = runner.create(self.project, ["CASE-000"])
        self.assertEqual(runner.execute(old_run["id"])["status"], "Fail")
        original = {case["id"]: workflow_state.packed(case) for case in projects.cases(self.project)}
        request = self.request()
        self.assertEqual(len(request["case_ids"]), 20)
        analysis = workflow_changes.analyze(self.flow, request)
        self.assertEqual((len(analysis["unaffected_case_ids"]), len(analysis["unmapped_case_ids"])), (80, 0))
        key = uuid.uuid4().hex
        made = workflow_changes.maintain(self.flow, request, idempotency_key=key)
        identity = made["workflow_id"]
        self.assertEqual(workflow_changes.maintain(self.flow, request, idempotency_key=key)["workflow_id"], identity)
        self.submit(identity, {"requirements": [{"id": "REQ-NEW", "text": "前20项优先级 medium", "source_ids": ["changed"]}]})
        expected = {"status": 200, "json_equals": {"/priority": "medium"}}
        human_expected = "2. 业务优先级为 medium，其他规则保持不变。"
        self.submit(identity, {"scenarios": [{"id": "SC-NEW", "title": "核对修改规则", "requirement_ids": ["REQ-NEW"], "oracle_ids": ["O-NEW"]}],
                               "oracles": [{"id": "O-NEW", "kind": "api", "requirement_ids": ["REQ-NEW"], "checks": expected, "functional_expected": human_expected}]})
        candidates = []
        for case in self.cases[:20]:
            candidate = copy.deepcopy(case)
            candidate.update(revision=2, assertions=expected, requirement_ids=["REQ-NEW"])
            candidate["extensions"]["test_case_document"] = {
                "description": "验证新需求下业务优先级为 medium", "preconditions": "查询服务已运行，当前测试身份有读取业务优先级的权限。",
                "steps": ["1. 使用有查询权限的身份查询业务优先级。", "2. 核对返回的业务优先级。"], "expected": human_expected,
                "source": {"workflow_id": identity, "source_ids": ["changed"], "requirement_ids": ["REQ-NEW"]},
                "business_path": ["需求迭代", "业务规则", "优先级"], "author": "来源维护宿主（合成）"}
            candidates.append(candidate)
        self.submit(identity, {"cases": candidates, "bindings": {"SC-NEW": request["case_ids"]}})
        self.assertEqual(projects.case(self.project, "CASE-000")["assertions"]["json_equals"]["/priority"], "high")
        workflow.next_task(identity, idempotency_key=uuid.uuid4().hex)
        run = db.one("SELECT * FROM runs WHERE trigger = 'workflow' AND session_id = ? ORDER BY created_at DESC LIMIT 1", (identity,))
        self.assertEqual(runner.execute(run["id"])["status"], "Pass")
        workflow.next_task(identity, idempotency_key=uuid.uuid4().hex)
        self.assertEqual(projects.case(self.project, "CASE-000")["assertions"]["json_equals"]["/priority"], "medium")
        published = projects.case(self.project, "CASE-000")
        self.assertEqual(published["steps"], candidates[0]["extensions"]["test_case_document"]["steps"])
        self.assertEqual(published["extensions"]["functional_design"]["state"], "compiled_from_workflow_oracles")
        result = db.one("SELECT * FROM results WHERE run_id = ? AND case_id = 'CASE-000'", (run["id"],))
        self.assertTrue(execution_inputs.result_current(self.project, projects.case(self.project, "CASE-000"), result))
        self.assertEqual(len(projects.cases(self.project)), 100)
        for number in range(20, 100):
            identity = f"CASE-{number:03}"
            self.assertEqual(workflow_state.packed(projects.case(self.project, identity)), original[identity])
        previous = db.one("SELECT detail FROM results WHERE run_id = ?", (old_run["id"],))
        self.assertEqual(json.loads(previous["detail"])["business_status"], "Fail")
        self.assertEqual(db.one("SELECT status FROM runs WHERE id = ?", (old_run["id"],))["status"], "Fail")

    def test_human_expected_without_changed_source_mapped_checks_stays_unverified(self):
        original = projects.case(self.project, "CASE-000")
        original["assertions"] = {"status": 200, "json_equals": {"/priority": "medium"}}
        projects.save_cases(self.project, [original])
        original = projects.case(self.project, "CASE-000")
        candidate = copy.deepcopy(original)
        candidate["extensions"]["test_case_document"] = {
            "description": "验证管理员专属规则不会向普通身份公开", "preconditions": "当前身份为普通用户。",
            "steps": ["1. 使用普通身份查询业务规则。", "2. 检查管理员规则的可见范围。"],
            "expected": "2. 不应显示管理员规则。", "source": "合成权限需求：普通身份不能读取管理员规则。"}
        documents = {"definition.json": {"id": "manual-design"}, "scenarios.json": [{"id": "SC", "oracle_ids": ["O"]}],
                     "requirements.json": workflow_definition.snapshot(workflow_state.workflow(self.flow))["documents"]["requirements.json"],
                     "oracles.json": [{"id": "O", "kind": "api", "checks": candidate["assertions"],
                                       "functional_expected": candidate["extensions"]["test_case_document"]["expected"]}]}
        materialized = workflow_definition._materialize({"cases": [candidate], "bindings": {"SC": ["CASE-000"]}}, documents, self.project, False)
        staged = materialized["materialization.json"]["cases"][0]
        self.assertEqual(staged["extensions"]["functional_design"]["state"], "unmapped")
        run = runner.create(self.project, ["CASE-000"], candidate_cases=[staged])
        self.assertEqual(runner.execute(run["id"])["status"], "Pass")
        result = json.loads(db.one("SELECT detail FROM results WHERE run_id = ?", (run["id"],))["detail"])
        self.assertEqual(result["business_status"], "Unverified")
        self.assertEqual(projects.case(self.project, "CASE-000"), original)

    def test_source_backed_locator_maintenance_keeps_the_existing_human_title(self):
        document = {"description": "验证审核员将草稿订单提交为待审核状态。", "preconditions": "审核员已登录，指定订单处于草稿状态。",
                    "steps": ["1. 打开指定草稿订单，点击【提交审核】。"], "expected": "1. 订单状态变为“待审核”。",
                    "source": {"kind": "synthetic_requirement", "text": "提交草稿订单后进入待审核状态。"}}
        original = builder.ui_case("UI-MAINTAIN", "提交草稿订单审核", {
            "start_url": "/draft", "actions": [{"action": "click", "selector": "#old-submit"}],
            "assertions": [{"assertion": "text", "selector": "#state", "expected": "待审核"}]}, test_case_document=document)
        projects.save_cases(self.project, [original])
        candidate = copy.deepcopy(original)
        candidate["extensions"]["automation"]["actions"][0]["selector"] = "#new-submit"
        documents = {"definition.json": {"id": "locator-maintenance"}, "scenarios.json": [{"id": "SC", "oracle_ids": ["O"]}],
                     "requirements.json": workflow_definition.snapshot(workflow_state.workflow(self.flow))["documents"]["requirements.json"],
                     "oracles.json": [{"id": "O", "kind": "ui", "checks": workflow_definition.oracle_of(original)}],
                     "materialization.json": {"cases": [original], "bindings": {"SC": [original["id"]]}}}
        materialized = workflow_definition._materialize({"cases": [candidate]}, documents, self.project, True)
        staged = materialized["materialization.json"]["cases"][0]
        self.assertEqual(staged["title"], original["title"])
        self.assertEqual(staged["extensions"]["test_case_document"], document)
        self.assertEqual(projects.case(self.project, original["id"]), original)

    def test_dependency_and_unmapped_are_explicit_and_stale_analysis_cannot_start(self):
        dependent = projects.case(self.project, "CASE-020")
        dependent["extensions"]["depends_on"] = ["CASE-000"]
        unlinked = projects.case(self.project, "CASE-099")
        unlinked["requirement_ids"] = ["MISSING"]
        projects.save_cases(self.project, [dependent, unlinked])
        request = self.request()
        analysis = workflow_changes.analyze(self.flow, request)
        self.assertEqual(analysis["dependency_case_ids"], ["CASE-020"])
        # The stored materialization still provides a valid scenario link for CASE-099.
        self.assertNotIn("CASE-099", analysis["unmapped_case_ids"])
        new = builder.api_case("CASE-100", "没有需求关联的用例", "GET", "/answer", {"status": 200, "json_equals": {"/priority": "medium"}})
        projects.save_cases(self.project, [new])
        fresh = workflow_changes.analyze(self.flow, request)
        self.assertEqual(fresh["unmapped_case_ids"], ["CASE-100"])
        with self.assertRaisesRegex(workflow_state.WorkflowError, "影响分析已变化"):
            workflow_changes.maintain(self.flow, request, idempotency_key=uuid.uuid4().hex)

    def test_change_scope_cannot_modify_unselected_and_failed_regression_cannot_publish(self):
        request = self.request()
        made = workflow_changes.maintain(self.flow, request, idempotency_key=uuid.uuid4().hex)
        flow = workflow_state.workflow(made["workflow_id"])
        definition = workflow_definition.snapshot(flow)["documents"]["definition.json"]
        documents = {"definition.json": definition,
                     "requirements.json": workflow_definition.snapshot(workflow_state.workflow(self.flow))["documents"]["requirements.json"],
                     "scenarios.json": [{"id": "SC", "requirement_ids": ["R"], "oracle_ids": ["O"]}],
                     "oracles.json": [{"id": "O", "kind": "api", "checks": self.cases[99]["assertions"]}]}
        with self.assertRaisesRegex(workflow_state.WorkflowError, "只能包含本次已选择"):
            workflow_definition._materialize({"cases": [self.cases[99]], "bindings": {"SC": ["CASE-099"]}}, documents, self.project, False)
        with self.assertRaisesRegex(workflow_state.WorkflowError, "覆盖全部已选择"):
            workflow_definition._materialize({"cases": [], "bindings": {"SC": ["CASE-000"]}}, documents, self.project, False)
        identity = made["workflow_id"]
        self.submit(identity, {"requirements": [{"id": "REQ-LOW", "text": "前20项优先级 low", "source_ids": ["changed"]}]})
        expected = {"status": 200, "json_equals": {"/priority": "low"}}
        self.submit(identity, {"scenarios": [{"id": "SC-LOW", "title": "核对修改规则", "requirement_ids": ["REQ-LOW"], "oracle_ids": ["O-LOW"]}],
                               "oracles": [{"id": "O-LOW", "kind": "api", "requirement_ids": ["REQ-LOW"], "checks": expected}]})
        candidates = [{**copy.deepcopy(case), "revision": 2, "assertions": expected, "requirement_ids": ["REQ-LOW"]} for case in self.cases[:20]]
        self.submit(identity, {"cases": candidates, "bindings": {"SC-LOW": request["case_ids"]}})
        workflow.next_task(identity, idempotency_key=uuid.uuid4().hex)
        run = db.one("SELECT * FROM runs WHERE trigger = 'workflow' AND session_id = ? ORDER BY created_at DESC LIMIT 1", (identity,))
        self.assertEqual(runner.execute(run["id"])["status"], "Fail")
        workflow.next_task(identity, idempotency_key=uuid.uuid4().hex)
        self.assertEqual(projects.case(self.project, "CASE-000")["assertions"]["json_equals"]["/priority"], "high")


if __name__ == "__main__":
    unittest.main()
