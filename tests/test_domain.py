"""Immutable domain generations, optimistic publication and executable case CAS."""
import json
import multiprocessing
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill import builder, domain, paths, projects

PID = "domain-test"


def publish_from_process(workspace, barrier, results, path):
    os.environ["AUTOSKILL_WORKSPACE"] = workspace
    base = domain.read(PID)["head"]
    barrier.wait(timeout=20)
    try:
        result = domain.commit(PID, {path: {"writer": path}}, base)
        results.put(("published", result["head"]))
    except domain.ConflictError:
        results.put(("conflict", None))


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name) / "workspace"
        environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.workspace)})
        environment.start()
        self.addCleanup(environment.stop)
        projects.create("领域测试", "https://domain.invalid", project_id=PID)

    def test_document_and_binary_generation_keep_history_and_hashes(self):
        self.assertEqual(domain.read(PID), {"head": None, "documents": {}, "files": {}})
        first = domain.commit(PID, {"sources/spec.json": {"标题": "需求"}, "native/model.bin": b"\x00\xffdata"}, None, "导入需求", {"origin": "imported"})
        snapshot = domain.read(PID)
        self.assertEqual(snapshot["documents"], {"sources/spec.json": {"标题": "需求"}})
        self.assertEqual(domain.read_file(PID, "native/model.bin"), b"\x00\xffdata")
        self.assertEqual(snapshot["files"]["sources/spec.json"]["sha256"], domain.content_hash({"标题": "需求"}))
        second = domain.commit(PID, {"sources/spec.json": None}, first["head"], "替换", remove=["native/model.bin"])
        self.assertEqual(domain.read_document(PID, "sources/spec.json"), None)
        self.assertEqual(domain.read_document(PID, "sources/spec.json", first["head"]), {"标题": "需求"})
        self.assertEqual(domain.read_file(PID, "native/model.bin", first["head"]), b"\x00\xffdata")
        self.assertEqual(domain.manifest(PID)["parent"], first["head"])
        self.assertEqual(second["changed"], ["native/model.bin", "sources/spec.json"])
        domain.validate_project(projects.path(PID))

    def test_noop_and_stale_publish(self):
        first = domain.commit(PID, {"a": {"x": 1, "y": 2}}, None)
        self.assertEqual(domain.commit(PID, {"a": {"y": 2, "x": 1}}, first["head"])["changed"], [])
        with self.assertRaises(domain.ConflictError) as conflict:
            domain.commit(PID, {"b": 2}, None)
        self.assertEqual(conflict.exception.actual_head, first["head"])
        self.assertEqual(domain.read(PID)["documents"], {"a": {"x": 1, "y": 2}})

    def test_manifest_only_does_not_read_objects(self):
        domain.commit(PID, {"sources/large": "body"}, None)
        with patch.object(domain, "_object", side_effect=AssertionError("unexpected object read")):
            self.assertIn("sources/large", domain.manifest(PID)["files"])

    def test_failed_head_replace_preserves_previous_generation(self):
        first = domain.commit(PID, {"a": 1}, None)
        replace = Path.replace

        def fail_head(source, target):
            if Path(target).name == "HEAD.json":
                raise OSError("fixture failed HEAD")
            return replace(source, target)

        with patch.object(Path, "replace", fail_head), self.assertRaisesRegex(OSError, "HEAD"):
            domain.commit(PID, {"a": 2, "b": 3}, first["head"])
        self.assertEqual(domain.read(PID)["documents"], {"a": 1})
        self.assertEqual(domain.read(PID)["head"], first["head"])
        self.assertFalse(list((projects.path(PID) / "domain").glob(".*.tmp")))

    def test_stage_is_durable_unpublished_and_retry_is_idempotent(self):
        staged = domain.stage(PID, {"workflows/a.json": {"goal": "测试"}}, None, "候选发布")
        self.assertEqual(domain.read(PID)["documents"], {})
        self.assertTrue((paths.local() / "domain-staging" / PID / staged["stage_id"] / "patch.json").is_file())
        published = domain.publish(PID, staged["stage_id"])
        later = domain.commit(PID, {"knowledge/a.json": {"rule": "later"}}, published["head"])
        self.assertEqual(domain.publish(PID, staged["stage_id"]), published)
        self.assertEqual(domain.read(PID)["head"], later["head"])

    def test_staged_patch_rejects_stale_base(self):
        staged = domain.stage(PID, {"a": 1}, None)
        domain.commit(PID, {"b": 2}, None)
        with self.assertRaises(domain.ConflictError):
            domain.publish(PID, staged["stage_id"])
        self.assertEqual(domain.read(PID)["documents"], {"b": 2})

    def test_empty_staged_patch_retains_an_idempotent_publication_receipt(self):
        staged = domain.stage(PID, {}, None)
        published = domain.publish(PID, staged["stage_id"])
        self.assertEqual(published["changed"], [])
        domain.commit(PID, {"later": True}, published["head"])
        self.assertEqual(domain.publish(PID, staged["stage_id"]), published)

    def test_disjoint_stage_rebase_preserves_concurrent_workflow(self):
        base = domain.commit(PID, {"workflow-a": 1, "obsolete-a": 2}, None)["head"]
        staged = domain.stage(PID, {"workflow-a": 3}, base, remove=["obsolete-a"])
        concurrent = domain.commit(PID, {"workflow-b": 4}, base)
        with self.assertRaises(domain.ConflictError):
            domain.publish(PID, staged["stage_id"])
        published = domain.publish(PID, staged["stage_id"], allow_disjoint=True)
        self.assertEqual(published["previous_head"], concurrent["head"])
        self.assertEqual(domain.read(PID)["documents"], {"workflow-a": 3, "workflow-b": 4})
        self.assertEqual(domain.publish(PID, staged["stage_id"], allow_disjoint=True), published)

    def test_disjoint_stage_rebase_rejects_changed_modified_or_removed_paths(self):
        base = domain.commit(PID, {"a": 1, "b": 2}, None)["head"]
        staged = domain.stage(PID, {"a": 3}, base, remove=["b"])
        concurrent = domain.commit(PID, {"b": 4}, base)
        with self.assertRaises(domain.ConflictError):
            domain.publish(PID, staged["stage_id"], allow_disjoint=True)
        self.assertEqual(domain.read(PID)["head"], concurrent["head"])
        empty_base = domain.stage(PID, {"a": 5}, None)
        with self.assertRaises(domain.ConflictError):
            domain.publish(PID, empty_base["stage_id"], allow_disjoint=True)

    def test_integrity_and_paths_are_validated(self):
        for path in ("../a", "/a", "a//b", "C:/a", "a\\b", "a/./b"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                domain.commit(PID, {path: 1}, None)
        published = domain.attach(PID, "attachments/blob", b"original", None)
        ref = domain.file_ref(PID, "attachments/blob", published["head"])
        object_file = projects.path(PID) / "domain/objects" / ref["sha256"]
        object_file.write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "哈希"):
            domain.read_file(PID, "attachments/blob")
        with self.assertRaisesRegex(ValueError, "哈希"):
            domain.validate_project(projects.path(PID))

    def test_real_processes_cannot_both_publish_the_same_base(self):
        context = multiprocessing.get_context("spawn")
        barrier, results = context.Barrier(2), context.Queue()
        workers = [context.Process(target=publish_from_process, args=(str(self.workspace), barrier, results, path)) for path in ("a", "b")]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(30)
            self.assertFalse(worker.is_alive())
            self.assertEqual(worker.exitcode, 0)
        outcomes = [results.get(timeout=5)[0] for _ in workers]
        self.assertCountEqual(outcomes, ["published", "conflict"])
        self.assertEqual(len(domain.read(PID)["documents"]), 1)

    def test_case_cas_preserves_exact_validated_candidate_body(self):
        case = builder.api_case("TC-API-001", "原用例", "GET", "/old", {"status": 204})
        projects.save_cases(PID, [case])
        old = projects.case(PID, case["id"])
        candidate = json.loads(json.dumps(old))
        candidate.update(title="已验证候选", revision=old["revision"] + 1)
        projects.save_cases(PID, [candidate], expected_hashes={case["id"]: domain.content_hash(old)})
        self.assertEqual(domain.content_hash(projects.case(PID, case["id"])), domain.content_hash(candidate))
        with self.assertRaises(projects.CaseConflictError):
            projects.save_cases(PID, [old], expected_hashes={case["id"]: domain.content_hash(old)})
        self.assertEqual(projects.case(PID, case["id"]), candidate)
        new = builder.api_case("TC-API-002", "新用例", "GET", "/new", {"status": 204})
        new["requirement_ids"] = ["REQ-RECORDED"]
        projects.save_cases(PID, [new], expected_hashes={new["id"]: None})
        self.assertEqual(projects.case(PID, new["id"]), new)


if __name__ == "__main__":
    unittest.main()
