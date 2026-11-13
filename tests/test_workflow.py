"""Workflow service integration: real HTTP evidence, durable claims and staged publication."""
import copy
import json
import os
import sys
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill import builder, db, domain, projects, runner, workflow
from autoskill import workflow_state as state


class AnswerHandler(BaseHTTPRequestHandler):
    calls = 0

    def do_GET(self):
        type(self).calls += 1
        body = json.dumps({"priority": "medium"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), AnswerHandler)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=5)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.folder = Path(self.directory.name)
        self.environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": self.directory.name, "AUTOSKILL_SESSION": "workflow-test",
                                                   "AUTOSKILL_HEADED": "0", "AUTOSKILL_CASE_SKILL": str(self.folder / "plan")})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.project = projects.create("Workflow fixture", f"http://127.0.0.1:{self.server.server_port}", project_id="workflow-fixture")["id"]
        self.spawn = patch.object(runner, "spawn", return_value=None)
        self.spawn.start()
        self.addCleanup(self.spawn.stop)
        self.worker = patch("autoskill.server.jobs.ensure_worker", return_value={"status": "running", "pid": None})
        self.worker.start()
        self.addCleanup(self.worker.stop)

    def key(self):
        return uuid.uuid4().hex

    def create(self, sources=None, **extra):
        return workflow.create(self.project, {"title": "优先级验证", "sources": sources or [{"id": "s1", "text": "优先级应为 high"}], **extra},
                               idempotency_key=self.key())["workflow_id"]

    def claim(self, identity):
        return workflow.next_task(identity, idempotency_key=self.key())["task"]

    def submit(self, identity, task, payload, key=None):
        return workflow.submit(identity, task["id"], lease_token=task["lease_token"], attempt_id=task["attempt_id"],
                               idempotency_key=key or self.key(), payload=payload)

    def to_materialize(self, identity, expected="high"):
        requirements = self.claim(identity)
        self.assertEqual(requirements["phase"], "requirements")
        self.submit(identity, requirements, {"requirements": [{"id": "r1", "text": f"优先级为 {expected}", "source_ids": ["s1"]}]})
        design = self.claim(identity)
        self.submit(identity, design, {"scenarios": [{"id": "sc1", "title": "检查优先级", "requirement_ids": ["r1"], "oracle_ids": ["o1"]}],
                                       "oracles": [{"id": "o1", "kind": "api", "requirement_ids": ["r1"],
                                                    "checks": {"status": 200, "json_equals": {"/priority": expected}}}]})
        materialize = self.claim(identity)
        self.assertEqual(materialize["phase"], "materialize")
        return materialize

    def queue_candidate(self, expected="high", repetitions=1):
        identity = self.create(validation={"repetitions": repetitions})
        task = self.to_materialize(identity, expected)
        case = builder.api_case("TC-API-001", "优先级", "GET", "/priority", {"status": 200, "json_equals": {"/priority": expected}})
        self.submit(identity, task, {"cases": [case], "bindings": {"sc1": [case["id"]]}})
        self.assertEqual(projects.cases(self.project), [])
        self.assertIsNone(self.claim(identity))
        execution = next(item for item in workflow.status(identity)["items"] if item["phase"] == "execute")
        return identity, case, execution["run_id"]

    def test_real_http_failure_is_complete_work_without_changing_oracle(self):
        identity, case, run_id = self.queue_candidate()
        before = AnswerHandler.calls
        result = runner.execute(run_id)
        self.assertEqual(result["status"], "Fail")
        diagnosis = self.claim(identity)
        self.assertEqual(diagnosis["phase"], "diagnose")
        self.assertEqual(diagnosis["context"]["execution"]["conclusion"], "violated")
        self.assertEqual(projects.case(self.project, case["id"]), case)
        self.submit(identity, diagnosis, {"disposition": "business_defect", "reason": "预期 high，真实 HTTP 响应 medium", "evidence_refs": [run_id]})
        knowledge = self.claim(identity)
        self.submit(identity, knowledge, {"entries": [{"id": "k1", "text": "优先级接口存在缺陷", "evidence_refs": [run_id]}]})
        final = workflow.status(identity)
        self.assertTrue(final["complete"])
        self.assertEqual(final["conclusion"], "violated")
        self.assertEqual(AnswerHandler.calls - before, 1)
        self.assertEqual(workflow.read(identity, kind="oracles")["items"][0]["checks"]["json_equals"]["/priority"], "high")

    def test_real_http_pass_is_published_only_after_content_bound_evidence(self):
        identity, case, run_id = self.queue_candidate("medium")
        self.assertEqual(projects.cases(self.project), [])
        runner.execute(run_id)
        knowledge = self.claim(identity)
        self.assertEqual(knowledge["phase"], "knowledge")
        self.assertEqual(projects.case(self.project, case["id"]), case)
        self.submit(identity, knowledge, {"entries": [{"id": "k1", "text": "接口完成验证"}]})
        self.assertEqual(workflow.status(identity)["conclusion"], "satisfied")

    def test_candidate_waits_for_all_repetitions_before_publication(self):
        identity, case, first_run = self.queue_candidate("medium", repetitions=2)
        runner.execute(first_run)
        self.assertIsNone(self.claim(identity))
        self.assertEqual(projects.cases(self.project), [])
        self.assertIsNone(self.claim(identity))
        second = db.one("SELECT run_id FROM workflow_items WHERE workflow_id = ? AND phase = 'execute' AND run_id != ?", (identity, first_run))
        runner.execute(second["run_id"])
        self.assertEqual(self.claim(identity)["phase"], "knowledge")
        self.assertEqual(projects.case(self.project, case["id"]), case)

    def test_failed_maintenance_candidate_never_replaces_live_case(self):
        identity, original, first_run = self.queue_candidate()
        runner.execute(first_run)
        diagnosis = self.claim(identity)
        self.submit(identity, diagnosis, {"disposition": "repair", "reason": "尝试恢复执行绑定"})
        maintain = self.claim(identity)
        self.assertEqual(maintain["phase"], "maintain")
        candidate = {**original, "revision": original["revision"] + 1}
        self.submit(identity, maintain, {"cases": [candidate]})
        self.assertIsNone(self.claim(identity))
        second = db.one("SELECT run_id FROM workflow_items WHERE workflow_id = ? AND phase = 'execute' AND run_id != ?", (identity, first_run))
        runner.execute(second["run_id"])
        rejected = self.claim(identity)
        self.assertEqual(rejected["context"]["publication"], "rejected")
        self.assertEqual(projects.case(self.project, original["id"]), original)

    def test_new_source_cycle_can_change_oracle_without_erasing_old_failure(self):
        identity, original, first_run = self.queue_candidate()
        runner.execute(first_run)
        diagnosis = self.claim(identity)
        self.submit(identity, diagnosis, {"disposition": "business_defect", "reason": "high 与 medium 不符"})
        self.submit(identity, self.claim(identity), {"entries": [{"id": "k1", "text": "旧来源失败已保留"}]})
        before = workflow.status(identity)
        change = workflow.revise(identity, {"sources": [{"id": "s1", "text": "新需求明确优先级为 medium"}]},
                                 expected_hash=before["definition_hash"], idempotency_key=self.key())
        self.assertNotEqual(change["cycle_id"], before["cycle_id"])
        task = self.to_materialize(identity, "medium")
        candidate = builder.api_case(original["id"], "优先级", "GET", "/priority", {"status": 200, "json_equals": {"/priority": "medium"}})
        candidate["revision"] = 2
        self.submit(identity, task, {"cases": [candidate], "bindings": {"sc1": [candidate["id"]]}})
        self.assertIsNone(self.claim(identity))
        second = db.one("SELECT run_id FROM workflow_items WHERE workflow_id = ? AND phase = 'execute' AND run_id != ?", (identity, first_run))
        runner.execute(second["run_id"])
        self.submit(identity, self.claim(identity), {"entries": [{"id": "k2", "text": "新来源已验证"}]})
        self.assertEqual(workflow.status(identity)["conclusion"], "satisfied")
        self.assertEqual(db.one("SELECT status FROM results WHERE run_id = ?", (first_run,))["status"], "Fail")
        self.assertEqual(projects.case(self.project, original["id"])["assertions"]["json_equals"]["/priority"], "medium")
        self.assertEqual(workflow.read(identity, kind="oracles", head=before["domain_head"])["items"][0]["checks"]["json_equals"]["/priority"], "high")

    def test_candidate_promotion_conflict_preserves_concurrent_live_edit(self):
        identity, case, run_id = self.queue_candidate("medium")
        other = {**case, "title": "并发编辑"}
        projects.save_cases(self.project, [other])
        runner.execute(run_id)
        diagnosis = self.claim(identity)
        self.assertEqual(diagnosis["phase"], "diagnose")
        self.assertEqual(diagnosis["context"]["publication"], "conflict")
        self.assertEqual(projects.case(self.project, case["id"])["title"], "并发编辑")

    def test_claim_and_submit_idempotency_reject_changed_payload(self):
        identity = self.create()
        key = self.key()
        first = workflow.next_task(identity, idempotency_key=key)
        self.assertEqual(first, workflow.next_task(identity, idempotency_key=key))
        self.assertIsNone(self.claim(identity))
        payload = {"requirements": [{"id": "r1", "text": "应为 high", "source_ids": ["s1"]}]}
        submit_key = self.key()
        original = self.submit(identity, first["task"], payload, submit_key)
        self.assertEqual(original, self.submit(identity, first["task"], payload, submit_key))
        with self.assertRaises(workflow.WorkflowError) as caught:
            self.submit(identity, first["task"], {"requirements": []}, submit_key)
        self.assertEqual(caught.exception.code, "idempotency_conflict")

    def test_materialization_cannot_replace_required_oracle_with_actual_answer(self):
        identity = self.create()
        task = self.to_materialize(identity)
        weakened = builder.api_case("TC-API-001", "优先级", "GET", "/priority", {"status": 200, "json_equals": {"/priority": "medium"}})
        with self.assertRaises(workflow.WorkflowError) as caught:
            self.submit(identity, task, {"cases": [weakened], "bindings": {"sc1": [weakened["id"]]}})
        self.assertEqual(caught.exception.code, "oracle_change")
        self.assertEqual(projects.cases(self.project), [])
        self.assertNotIn("materialization.json", domain.read(self.project)["documents"])

    def test_backend_coverage_finds_requirement_omitted_by_host(self):
        identity = self.create()
        self.submit(identity, self.claim(identity), {"requirements": [
            {"id": "r1", "text": "已覆盖", "source_ids": ["s1"]}, {"id": "r2", "text": "被遗漏", "source_ids": ["s1"]}]})
        self.submit(identity, self.claim(identity), {
            "scenarios": [{"id": "sc1", "title": "只覆盖第一项", "requirement_ids": ["r1"], "oracle_ids": ["o1"]}],
            "oracles": [{"id": "o1", "kind": "api", "requirement_ids": ["r1"], "checks": {"status": 200}}]})
        completion = self.claim(identity)
        self.assertEqual(completion["phase"], "completion")
        self.assertEqual(completion["context"]["missing_requirement_ids"], ["r2"])

    def test_parallel_hosts_never_claim_the_same_item(self):
        identity = self.create()
        with ThreadPoolExecutor(max_workers=2) as pool:
            claimed = list(pool.map(lambda _: workflow.next_task(identity, idempotency_key=self.key()), range(2)))
        self.assertEqual(sum(result["task"] is not None for result in claimed), 1)

    def test_explicit_binding_separates_two_workflows_in_one_host_session(self):
        first, second = self.create(), self.create()
        a, b = self.claim(first), self.claim(second)
        with self.assertRaises(workflow.WorkflowError) as caught:
            workflow.current_binding()
        self.assertEqual(caught.exception.code, "ambiguous_binding")
        with workflow.use_binding(a["binding_token"]):
            self.assertEqual(workflow.current_binding()["workflow_id"], first)
        with workflow.use_binding(b["binding_token"]):
            self.assertEqual(workflow.current_binding()["workflow_id"], second)

    def test_expired_lease_is_fenced_and_safe_work_can_be_reclaimed(self):
        identity = self.create()
        original = self.claim(identity)
        db.execute("UPDATE workflow_attempts SET lease_until = '2000-01-01' WHERE id = ?", (original["attempt_id"],))
        replacement = self.claim(identity)
        self.assertEqual(original["id"], replacement["id"])
        self.assertNotEqual(original["attempt_id"], replacement["attempt_id"])
        with self.assertRaises(workflow.WorkflowError) as caught:
            self.submit(identity, original, {"requirements": []})
        self.assertEqual(caught.exception.code, "stale_lease")

    def test_unknown_write_requires_real_investigation_and_never_replays(self):
        identity = self.create()
        task = self.claim(identity)
        binding = {"workflow_id": identity, "item_id": task["id"], "attempt_id": task["attempt_id"], "lease_token": task["lease_token"]}
        intent = workflow.operation_intent("click", {"target": "submit"}, binding=binding, idempotency_key="click-once")
        workflow.operation_receipt(intent, {"error": "连接中断，结果未知"}, status="unknown")
        db.execute("UPDATE workflow_attempts SET lease_until = '2000-01-01' WHERE id = ?", (task["attempt_id"],))
        investigation = self.claim(identity)
        self.assertEqual(investigation["phase"], "investigate")
        with self.assertRaises(workflow.WorkflowError):
            self.submit(identity, investigation, {"resolution": "observed", "observation_operation_ids": []})
        probe = workflow.operation_intent("check", {"check": {"assertion": "text", "expected": "saved"}}, binding=binding, side_effect=False)
        workflow.operation_receipt(probe, {"check": {"assertion": "text", "expected": "saved"}, "passed": False, "actual": "加载失败"}, status="failed")
        self.submit(identity, investigation, {"resolution": "incomplete", "observation_operation_ids": [probe["operation_id"]]})
        knowledge = self.claim(identity)
        self.submit(identity, knowledge, {"entries": [{"id": "k1", "text": "操作结果仍未知，未自动重播"}]})
        self.assertEqual(workflow.status(identity)["conclusion"], "incomplete")
        self.assertEqual(db.one("SELECT COUNT(*) n FROM workflow_operations WHERE kind = 'click'")["n"], 1)

    def test_head_published_before_db_receipt_is_recovered_once(self):
        identity = self.create()
        task = self.claim(identity)
        payload = {"requirements": [{"id": "r1", "text": "应为 high", "source_ids": ["s1"]}]}
        key = self.key()
        original = domain.publish

        def publish_then_interrupt(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError("crash after HEAD")

        with patch.object(domain, "publish", side_effect=publish_then_interrupt), self.assertRaises(OSError):
            self.submit(identity, task, payload, key)
        self.assertEqual(workflow.read(identity, kind="requirements")["items"][0]["id"], "r1")
        restored = self.submit(identity, task, payload, key)
        self.assertTrue(restored["accepted"])
        self.assertEqual(sum(item["phase"] == "design" for item in workflow.status(identity)["items"]), 1)

    def test_definition_change_during_host_work_rejects_stale_proposal(self):
        identity = self.create()
        task = self.claim(identity)
        head = domain.read(self.project)["head"]
        domain.commit(self.project, {f"workflows/{identity}/sources.json": [{"id": "s1", "text": "新需求"}]}, head)
        with self.assertRaises(workflow.WorkflowError) as caught:
            self.submit(identity, task, {"requirements": [{"id": "r1", "text": "旧需求", "source_ids": ["s1"]}]})
        self.assertEqual(caught.exception.code, "revision_conflict")

    def test_transport_temp_paths_do_not_change_attachment_idempotency(self):
        left, right = self.folder / "one.md", self.folder / "two.md"
        left.write_text("来源内容", encoding="utf-8")
        right.write_bytes(left.read_bytes())
        spec = {"title": "附件", "sources": [{"id": "s1", "uri": "user.md", "file": str(left)}]}
        key = self.key()
        first = workflow.create(self.project, spec, idempotency_key=key)
        second = copy.deepcopy(spec)
        second["sources"][0]["file"] = str(right)
        self.assertEqual(first, workflow.create(self.project, second, idempotency_key=key))
        right.write_text("不同内容", encoding="utf-8")
        with self.assertRaises(workflow.WorkflowError) as caught:
            workflow.create(self.project, second, idempotency_key=key)
        self.assertEqual(caught.exception.code, "idempotency_conflict")

    def test_pagination_is_revision_bound_and_text_reads_are_snapshot_bound(self):
        identity = self.create(sources=[{"id": "s1", "text": "需求" * 10000}])
        head = workflow.read(identity, kind="sources")["head"]
        page = workflow.read(identity, kind="sources", head=head, text_offset=0, text_length=20)
        self.assertEqual(len(page["text"]), 20)
        old = domain.read(self.project)["head"]
        domain.commit(self.project, {f"workflows/{identity}/sources.json": [{"id": "s1", "text": "新需求"}]}, old)
        self.assertEqual(page, workflow.read(identity, kind="sources", head=head, text_offset=0, text_length=20))
        with db.connect() as connection:
            state.item(connection, identity, "knowledge")
        first = workflow.status(identity, limit=1)
        self.assertIsNotNone(first["next_cursor"])
        workflow.cancel(identity, idempotency_key=self.key())
        with self.assertRaises(workflow.WorkflowError) as caught:
            workflow.status(identity, cursor=first["next_cursor"])
        self.assertEqual(caught.exception.code, "cursor_expired")

    def test_cancel_and_resume_retain_checkpoint_and_fence_old_holder(self):
        identity = self.create()
        task = self.claim(identity)
        workflow.heartbeat(identity, task["id"], lease_token=task["lease_token"], attempt_id=task["attempt_id"],
                           idempotency_key=self.key(), checkpoint={"done": ["read-source"]})
        workflow.cancel(identity, idempotency_key=self.key())
        self.assertIsNone(self.claim(identity))
        workflow.resume(identity, idempotency_key=self.key())
        resumed = self.claim(identity)
        self.assertEqual(resumed["checkpoint"], {"done": ["read-source"]})
        with self.assertRaises(workflow.WorkflowError):
            workflow.operation_intent("click", {}, binding={"workflow_id": identity, "item_id": task["id"], "attempt_id": task["attempt_id"], "lease_token": task["lease_token"]})


if __name__ == "__main__":
    unittest.main()
