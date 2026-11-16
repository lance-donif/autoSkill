"""Workflow knowledge publication and declared use share existing atomic journals."""
import json
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import test_knowledge_entries as entry_tests

from autoskill import db, domain, projects, workflow
from autoskill import knowledge_entries as knowledge


class KnowledgeWorkflowTests(unittest.TestCase):
    setUp = entry_tests.KnowledgeEntryTests.setUp
    body = entry_tests.KnowledgeEntryTests.body
    payload = entry_tests.KnowledgeEntryTests.payload
    save = entry_tests.KnowledgeEntryTests.save
    validation = entry_tests.KnowledgeEntryTests.validation

    @classmethod
    def setUpClass(cls):
        entry_tests.KnowledgeEntryTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        entry_tests.KnowledgeEntryTests.tearDownClass.__func__(cls)

    def submit(self, identity, task, payload, *, key=None, used=None):
        return workflow.submit(identity, task["id"], lease_token=task["lease_token"], attempt_id=task["attempt_id"],
                               idempotency_key=key or uuid.uuid4().hex, payload=payload, knowledge_used=used)

    def test_v2_revision_and_workflow_history_publish_in_one_generation(self):
        identity, validation_id, _ = self.validation()
        task = self.last_task
        self.assertEqual(task["phase"], "knowledge")
        self.assertEqual(task["context"]["validation_id"], validation_id)
        self.assertEqual(task["context"]["repetitions"], 3)
        payload = self.payload(self.body(kind="procedure", validation_refs=[{"workflow_id": identity, "validation_id": validation_id}]))
        response = self.submit(identity, task, payload)
        self.assertTrue(response["accepted"])
        snapshot = domain.read(self.pid)
        history = snapshot["documents"][f"workflows/{identity}/knowledge.json"]
        self.assertEqual(len(history), 1)
        ref = history[0]
        self.assertEqual(knowledge.get(self.pid, ref["id"])["verification"], "execution_reproduced")
        changed = domain.manifest(self.pid)["changed"]
        self.assertIn(knowledge.revision_path(ref["id"], 1), changed)
        self.assertIn(knowledge.pointer_path(ref["id"]), changed)
        self.assertIn(knowledge.binding_path(ref["id"], 1), changed)
        self.assertIn(f"workflows/{identity}/knowledge.json", changed)

    def test_invalid_later_mutation_leaves_head_history_and_current_entry_unchanged(self):
        identity, _, _ = self.validation(1)
        before = domain.read(self.pid)
        payload = self.payload()
        payload["mutations"].append({"op": "create", "client_key": "bad", "entry": {"title": "invalid"}})
        with self.assertRaises(workflow.WorkflowError) as failure:
            self.submit(identity, self.last_task, payload)
        self.assertEqual(failure.exception.code, "validation_error")
        self.assertEqual(domain.read(self.pid), before)
        self.assertEqual(knowledge.search(self.pid, query="接口知识")["entries"], [])
        result = self.submit(identity, self.last_task, self.payload())
        self.assertTrue(result["accepted"])

    def test_publication_crash_replays_exact_entry_and_history_without_duplicate_revision(self):
        identity, _, _ = self.validation(1)
        task, key, payload = self.last_task, uuid.uuid4().hex, self.payload()
        publish = domain.publish

        def interrupt(*args, **kwargs):
            publish(*args, **kwargs)
            raise OSError("simulated failure after HEAD")

        with patch.object(domain, "publish", side_effect=interrupt), self.assertRaises(OSError):
            self.submit(identity, task, payload, key=key)
        published = domain.read(self.pid)
        reply = self.submit(identity, task, payload, key=key)
        self.assertTrue(reply["accepted"])
        self.assertEqual(domain.read(self.pid), published)
        history = published["documents"][f"workflows/{identity}/knowledge.json"]
        self.assertEqual(len(history), 1)
        self.assertEqual(knowledge.get(self.pid, history[0]["id"])["revision"], 1)

    def test_usage_in_requirements_envelope_is_exact_and_idempotency_bound(self):
        entry = self.save()
        ref = {key: entry[key] for key in ("owner_project_id", "id", "revision", "content_hash")}
        ref["purpose"] = "检查来源遗漏"
        identity = workflow.create(self.pid, {"title": "引用知识", "sources": [{"id": "s1", "text": "响应 ok 应为 true"}]},
                                   idempotency_key=uuid.uuid4().hex)["workflow_id"]
        task = workflow.next_task(identity, idempotency_key=uuid.uuid4().hex)["task"]
        payload = {"requirements": [{"id": "r1", "text": "响应 ok 为 true", "source_ids": ["s1"]}]}
        before = domain.read(self.pid)
        with self.assertRaises(workflow.WorkflowError):
            self.submit(identity, task, payload, used=[{**ref, "content_hash": "a" * 64}])
        self.assertEqual(domain.read(self.pid), before)
        key = uuid.uuid4().hex
        result = self.submit(identity, task, payload, key=key, used=[ref])
        self.assertEqual(result["knowledge_used"], [ref])
        self.assertEqual(self.submit(identity, task, payload, key=key, used=[ref]), result)
        with self.assertRaises(workflow.WorkflowError) as failure:
            self.submit(identity, task, payload, key=key, used=[{**ref, "purpose": "不同使用目的"}])
        self.assertEqual(failure.exception.code, "idempotency_conflict")
        request = db.one("SELECT * FROM workflow_requests WHERE scope = ? AND request_key = ?", (identity, key))
        self.assertEqual(json.loads(request["response"])["knowledge_used"], [ref])
        self.assertEqual(knowledge.read_entry(self.pid, entry["id"])["revision"], 1)

    def usage_task(self, project, entry):
        ref = {key: entry[key] for key in ("owner_project_id", "id", "revision", "content_hash")}
        ref["purpose"] = "引用另一个项目的观察设计检查"
        identity = workflow.create(project, {"title": "交叉引用", "sources": [{"id": "s1", "text": "业务值应符合来源"}]},
                                   idempotency_key=uuid.uuid4().hex)["workflow_id"]
        task = workflow.next_task(identity, idempotency_key=uuid.uuid4().hex)["task"]
        payload = {"requirements": [{"id": "r1", "text": "业务值应符合来源", "source_ids": ["s1"]}]}
        return identity, task, payload, ref

    def test_opposite_project_usage_validates_before_locks_and_sqlite_writer(self):
        left = self.save()
        right = self.save(self.payload(self.body(project=self.other)), project=self.other)
        jobs = [self.usage_task(self.pid, right), self.usage_task(self.other, left)]
        barrier = threading.Barrier(2, timeout=10)
        validate = knowledge.validate_knowledge_used

        def concurrent_validate(project, refs):
            barrier.wait()
            return validate(project, refs)

        with patch.object(knowledge, "validate_knowledge_used", side_effect=concurrent_validate), ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.submit, identity, task, payload, used=[ref]) for identity, task, payload, ref in jobs]
            for future in futures:
                self.assertTrue(future.result(timeout=20)["accepted"])

    def test_completed_usage_replay_does_not_require_source_to_remain_available(self):
        source = self.save(self.payload(self.body(project=self.other)), project=self.other)
        identity, task, payload, ref = self.usage_task(self.pid, source)
        key = uuid.uuid4().hex
        accepted = self.submit(identity, task, payload, key=key, used=[ref])
        folder = projects.path(self.other)
        folder.rename(folder.with_name("other-offline"))
        with patch.object(knowledge, "validate_knowledge_used", side_effect=AssertionError("A completed immutable request must replay")):
            self.assertEqual(self.submit(identity, task, payload, key=key, used=[ref]), accepted)

    def test_reading_reproduced_knowledge_has_no_state_or_domain_writes(self):
        identity, validation_id, _ = self.validation()
        entry = self.save(self.payload(self.body(kind="procedure", validation_refs=[{"workflow_id": identity, "validation_id": validation_id}])))
        snapshot = domain.read(self.pid)
        with db.connect() as connection:
            before = "\n".join(connection.iterdump())
        for _ in range(2):
            self.assertEqual(knowledge.get(self.pid, entry["id"])["verification"], "execution_reproduced")
            knowledge.search(self.pid)
        with db.connect() as connection:
            after = "\n".join(connection.iterdump())
        self.assertEqual(before, after)
        self.assertEqual(domain.read(self.pid), snapshot)


if __name__ == "__main__":
    unittest.main()
