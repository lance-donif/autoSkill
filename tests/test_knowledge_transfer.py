"""Knowledge transfer validates whole chains and never imports execution trust."""
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

from autoskill import builder, db, domain, knowledge_entries, knowledge_transfer, projects, runner
from autoskill.knowledge_entries import KnowledgeError


class KnowledgeTransferTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(Path(temporary.name) / "data"),
                                             "AUTOSKILL_BRIDGE": "off", "AUTOSKILL_SESSION": "",
                                             "AUTOSKILL_WORKFLOW_BINDING": ""})
        environment.start()
        self.addCleanup(environment.stop)
        db.init()
        self.source = projects.create("来源", "http://127.0.0.1:19001", project_id="knowledge-source")["id"]
        self.target = projects.create("目标", "http://127.0.0.1:19002", project_id="knowledge-target")["id"]

    def create_entry(self, project_id=None, *, title="订单退款", text="退款后订单应关闭", lifecycle="active", kind="observation"):
        project_id = project_id or self.source
        entry = {"schema_version": 1, "id": str(uuid.uuid4()), "revision": 1, "parent_content_hash": None,
                 "title": title, "text": text, "kind": kind, "lifecycle": lifecycle,
                 "scope": {"project_id": project_id, "team_id": None, "business_scope": None, "source_version_hashes": {}},
                 "evidence_refs": [], "validation_refs": [], "creation_mode": "workflow_v2", "created_at": db.now()}
        self.store_entry(project_id, entry)
        return entry

    def store_entry(self, project_id, entry, *, current=None):
        current = domain.read(project_id) if current is None else current
        entry["content_hash"] = knowledge_entries.entry_content_hash(entry)
        domain.commit(project_id, knowledge_entries.make_changes(entry), current["head"], "测试知识修订")

    def revise(self, project_id, entry_id, *, text="退款确认后订单应关闭", creation_mode="workflow_v2", **fields):
        current = domain.read(project_id)
        previous = knowledge_entries.read_entry(project_id, entry_id, current=current)
        entry = {**copy.deepcopy(previous), **fields, "revision": previous["revision"] + 1,
                 "parent_content_hash": previous["content_hash"], "text": text, "creation_mode": creation_mode}
        self.store_entry(project_id, entry, current=current)
        return entry

    def export(self, entry, *, project_id=None):
        return knowledge_transfer.export_package(project_id or self.source, entries=[{"id": entry["id"], "revision": entry["revision"]}])

    def import_package(self, package, key="initial", **kwargs):
        return knowledge_transfer.import_package(self.target, package, idempotency_key=key, **kwargs)

    def resign(self, package, *, chain=False):
        if chain:
            for item in package["entries"]:
                parent = None
                for node in item["source_chain"]:
                    node["parent_content_hash"] = parent
                    node["content"]["parent_content_hash"] = parent
                    node["content_hash"] = domain.content_hash(node["content"])
                    parent = node["content_hash"]
        package["package_hash"] = domain.content_hash({key: value for key, value in package.items() if key != "package_hash"})
        return package

    def assert_error(self, code, call):
        with self.assertRaises(KnowledgeError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)

    def test_cross_project_roundtrip_keeps_full_source_chain_and_separate_local_revision(self):
        original = self.create_entry()
        revision2 = self.revise(self.source, original["id"])
        revision3 = self.revise(self.source, original["id"], text="退款到账后订单关闭")
        package = self.export(revision3)
        self.assertEqual([node["revision"] for node in package["entries"][0]["source_chain"]], [1, 2, 3])
        self.assertEqual(package["entries"][0]["source_chain"][1]["content_hash"], revision2["content_hash"])
        receipt = self.import_package(package)
        imported = receipt["imported"][0]
        target = knowledge_entries.get(self.target, imported["entry_id"])
        self.assertEqual(target["revision"], 1)
        self.assertNotEqual(target["id"], original["id"])
        self.assertEqual(target["text"], revision3["text"])
        self.assertEqual(target["scope"], revision3["scope"])
        self.assertEqual(target["creation_mode"], "import")
        self.assertEqual(target["verification"], "hypothesis")
        self.assertEqual(target["evidence_status"], "imported_unverified")
        self.assertEqual(target["provenance"]["origin_revision"], 3)
        self.assertEqual(target["provenance"]["origin_content_hash"], revision3["content_hash"])
        target_package = self.export(target, project_id=self.target)
        self.assertEqual(target_package["exporter_project_id"], self.target)
        self.assertEqual(target_package["entries"][0]["selected_revision"], 1)
        source_content = target_package["entries"][0]["source_chain"][0]["content"]
        self.assertEqual(source_content["provenance"]["origin_entry_id"], original["id"])

    def test_multihop_import_preserves_immediate_origin_and_historical_provenance(self):
        original = self.create_entry()
        target_id = self.import_package(self.export(original))["imported"][0]["entry_id"]
        target = knowledge_entries.get(self.target, target_id)
        package = self.export(target, project_id=self.target)
        third = projects.create("第三项目", "http://127.0.0.1:19003", project_id="knowledge-third")["id"]
        receipt = knowledge_transfer.import_package(third, package, idempotency_key="multihop")
        entry = knowledge_entries.get(third, receipt["imported"][0]["entry_id"])
        self.assertEqual(entry["provenance"]["origin_project_id"], self.target)
        self.assertEqual(entry["provenance"]["origin_entry_id"], target_id)
        self.assertEqual(entry["provenance"]["origin_revision"], 1)
        self.assertEqual(entry["provenance"]["source_provenance"]["origin_entry_id"], original["id"])
        self.assertEqual(entry["evidence_status"], "imported_unverified")

    def test_export_all_uses_only_active_canonical_entries_and_no_files(self):
        active = self.create_entry()
        withdrawn = self.create_entry(title="旧规则", lifecycle="withdrawn")
        folder = projects.path(self.source) / "knowledge"
        folder.mkdir(exist_ok=True)
        (folder / "legacy.md").write_text("legacy-do-not-export", encoding="utf-8")
        projects.set_auth(self.source, {"password": "credential-do-not-export"})
        current = domain.read(self.source)
        domain.attach(self.source, "captures/raw.har", b"raw-har-do-not-export", current["head"])
        package = knowledge_transfer.export_package(self.source, all_items=True)
        self.assertEqual([entry["entry_id"] for entry in package["entries"]], [active["id"]])
        self.assertNotIn("do-not-export", json.dumps(package))
        explicit = self.export(withdrawn)
        self.assertEqual(explicit["entries"][0]["source_chain"][0]["content"]["lifecycle"], "withdrawn")

    def test_empty_all_export_roundtrips_without_entries(self):
        package = knowledge_transfer.export_package(self.source, all_items=True)
        self.assertEqual(package["entries"], [])
        receipt = self.import_package(package)
        self.assertEqual(receipt["imported"], [])
        self.assertEqual(receipt["unchanged"], [])
        self.assertEqual(self.import_package(package), receipt)

    def test_export_freezes_current_pointers_and_chain_at_one_head(self):
        original = self.create_entry()
        real_read = domain.read
        changed = False

        def read_then_update(project_id, head=None):
            nonlocal changed
            snapshot = real_read(project_id, head)
            if project_id == self.source and not changed:
                changed = True
                entry = {**original, "revision": 2, "parent_content_hash": original["content_hash"], "text": "later revision"}
                self.store_entry(self.source, entry, current=snapshot)
            return snapshot

        with patch.object(domain, "read", side_effect=read_then_update):
            package = knowledge_transfer.export_package(self.source, all_items=True)
        self.assertEqual(package["entries"][0]["selected_revision"], 1)
        self.assertEqual(package["entries"][0]["source_chain"][0]["content"]["text"], original["text"])
        self.assertEqual(knowledge_entries.read_entry(self.source, original["id"])["revision"], 2)

    def test_import_request_replay_returns_original_historical_receipt(self):
        original = self.create_entry()
        package = self.export(original)
        receipt = self.import_package(package)
        local_id = receipt["imported"][0]["entry_id"]
        local_revision = self.revise(self.target, local_id)
        before = domain.read(self.target)
        self.assertNotEqual(before["head"], receipt["domain_head"])
        self.assertEqual(self.import_package(package), receipt)
        self.assertEqual(domain.read(self.target)["head"], before["head"])
        self.assertEqual(knowledge_entries.read_entry(self.target, local_id), local_revision)

    def test_same_origin_reimport_with_new_key_preserves_local_revision_and_provenance(self):
        original = self.create_entry()
        package = self.export(original)
        local_id = self.import_package(package)["imported"][0]["entry_id"]
        local_revision = self.revise(self.target, local_id, text="local validated correction", validation_refs=[])
        before = domain.read(self.target)
        receipt = self.import_package(package, "second-request")
        after = domain.read(self.target)
        self.assertEqual(receipt["imported"], [])
        self.assertEqual(receipt["unchanged"], [{"entry_id": local_id, "revision": 2}])
        self.assertEqual(knowledge_entries.read_entry(self.target, local_id), local_revision)
        entry_refs = {path: value for path, value in before["files"].items() if path.startswith("knowledge/entries/")}
        self.assertEqual(entry_refs, {path: value for path, value in after["files"].items() if path.startswith("knowledge/entries/")})
        self.assertEqual(self.import_package(package, "second-request"), receipt)

    def test_reimport_preserves_real_local_http_observation_after_explicit_v2_revision(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.target = projects.create("本地观测", f"http://127.0.0.1:{server.server_port}", project_id="local-observation")["id"]
        original = self.create_entry()
        package = self.export(original)
        receipt = self.import_package(package)
        local_id = receipt["imported"][0]["entry_id"]
        case = builder.api_case("API-001", "检查本地接口", "GET", "/health", {"status": 200, "json_equals": {"/ok": True}})
        projects.save_cases(self.target, [case])
        prepared = runner.prepare(self.target, [case["id"]], headed=False)
        run = runner.enqueue(prepared)
        self.assertEqual(runner.execute(run["id"])["status"], "Pass")
        result = db.one("SELECT * FROM results WHERE run_id = ?", (run["id"],))
        attempt = json.loads(result["detail"])["attempts"][0]
        current = domain.read(self.target)
        previous = knowledge_entries.read_entry(self.target, local_id, current=current)
        body = {name: copy.deepcopy(previous[name]) for name in knowledge_entries.ENTRY_FIELDS if name in previous}
        body["scope"]["project_id"] = self.target
        body["evidence_refs"] = [{"run_id": run["id"], "result_id": result["id"], "execution_attempt_id": attempt["id"], "receipt_sha256": attempt["sha256"]}]
        prepared_revision = knowledge_entries.prepare_mutations(self.target, "local-check", "local-cycle", {"schema_version": 2, "mutations": [
            {"op": "revise", "client_key": "local-observation", "entry_id": local_id, "expected_revision": 1,
             "expected_content_hash": previous["content_hash"], "entry": body},
        ]}, current=current)
        domain.commit(self.target, prepared_revision["changes"], current["head"])
        verified = knowledge_entries.get(self.target, local_id)
        self.assertEqual(verified["verification"], "observed")
        self.assertEqual(verified["evidence_status"], "valid")
        self.assertEqual(self.import_package(package), receipt)
        noop = self.import_package(package, "after-local-observation")
        self.assertEqual(noop["unchanged"], [{"entry_id": local_id, "revision": 2}])
        self.assertEqual(knowledge_entries.get(self.target, local_id), verified)

    def test_same_idempotency_key_different_payload_or_mode_conflicts(self):
        original = self.create_entry()
        package = self.export(original)
        self.import_package(package)
        before = domain.read(self.target)["head"]
        modified = copy.deepcopy(package)
        modified["entries"][0]["source_verification"] = {"verification": "validated"}
        self.resign(modified)
        self.assert_error("idempotency_conflict", lambda: self.import_package(modified))
        self.assert_error("idempotency_conflict", lambda: self.import_package(package, as_new=True))
        self.assertEqual(domain.read(self.target)["head"], before)

    def test_new_source_revision_advances_unmodified_import_and_maps_versions_separately(self):
        original = self.create_entry()
        first = self.import_package(self.export(original))
        local_id = first["imported"][0]["entry_id"]
        self.revise(self.source, original["id"])
        source3 = self.revise(self.source, original["id"], text="third source revision")
        second = self.import_package(self.export(source3), "next-source")
        self.assertEqual(second["imported"][0]["entry_id"], local_id)
        self.assertEqual(second["imported"][0]["revision"], 2)
        self.assertEqual(second["imported"][0]["origin_revision"], 3)
        entry = knowledge_entries.read_entry(self.target, local_id)
        self.assertEqual(entry["parent_content_hash"], knowledge_entries.read_entry(self.target, local_id, revision=1)["content_hash"])
        snapshot = domain.read(self.target)
        lineage = next(value for path, value in snapshot["documents"].items() if path.startswith("knowledge/imports/lineages/"))
        self.assertEqual(lineage["imported_revisions"], {"1": 1, "3": 2})
        self.assertEqual(lineage["local_revisions"]["2"]["origin_revision"], 3)
        origin = next(value for path, value in snapshot["documents"].items()
                      if path.startswith("knowledge/imports/origins/") and "/revisions/" not in path)
        self.assertEqual(set(origin["revisions"]), {"1", "2", "3"})

    def test_source_update_conflicts_with_local_edit_then_as_new_is_idempotent(self):
        original = self.create_entry()
        first = self.import_package(self.export(original))
        local_id = first["imported"][0]["entry_id"]
        local_revision = self.revise(self.target, local_id, text="local knowledge")
        newer = self.revise(self.source, original["id"], text="source knowledge")
        package = self.export(newer)
        before = domain.read(self.target)["head"]
        self.assert_error("revision_conflict", lambda: self.import_package(package, "conflicting"))
        self.assertEqual(domain.read(self.target)["head"], before)
        fresh = self.import_package(package, "new-lineage", as_new=True)
        new_id = fresh["imported"][0]["entry_id"]
        self.assertNotEqual(new_id, local_id)
        self.assertEqual(fresh["imported"][0]["revision"], 1)
        self.assertEqual(knowledge_entries.read_entry(self.target, local_id), local_revision)
        self.assertEqual(self.import_package(package, "new-lineage", as_new=True), fresh)
        another = self.import_package(package, "another-lineage", as_new=True)
        self.assertNotEqual(another["imported"][0]["entry_id"], new_id)

    def test_previously_imported_old_revision_is_noop_but_never_selected_rollback_conflicts(self):
        original = self.create_entry()
        package1 = self.export(original)
        local_id = self.import_package(package1)["imported"][0]["entry_id"]
        source2 = self.revise(self.source, original["id"])
        package2 = self.export(source2)
        source3 = self.revise(self.source, original["id"], text="third source")
        self.import_package(self.export(source3), "third")
        old = self.import_package(package1, "old-selected")
        self.assertEqual(old["unchanged"], [{"entry_id": local_id, "revision": 2}])
        self.assert_error("revision_conflict", lambda: self.import_package(package2, "old-unselected"))

    def test_same_origin_same_revision_different_hash_conflicts_even_in_new_lineage(self):
        original = self.create_entry()
        package = self.export(original)
        self.import_package(package)
        forged = copy.deepcopy(package)
        forged["entries"][0]["source_chain"][0]["content"]["text"] = "rewritten history"
        self.resign(forged, chain=True)
        before = domain.read(self.target)["head"]
        self.assert_error("source_revision_conflict", lambda: self.import_package(forged, "clash"))
        self.assert_error("source_revision_conflict", lambda: self.import_package(forged, "clash-new", as_new=True))
        self.assertEqual(domain.read(self.target)["head"], before)

    def test_changed_ancestor_conflicts_for_a_new_selected_source_revision(self):
        original = self.create_entry()
        source2 = self.revise(self.source, original["id"])
        self.import_package(self.export(source2))
        source3 = self.revise(self.source, original["id"], text="third")
        forged = self.export(source3)
        forged["entries"][0]["source_chain"][0]["content"]["title"] = "rewritten ancestor"
        self.resign(forged, chain=True)
        self.assert_error("source_revision_conflict", lambda: self.import_package(forged, "new-with-forged-history"))

    def test_source_verification_and_forged_kernel_fields_remain_untrusted_claims(self):
        original = self.create_entry()
        package = self.export(original)
        source = package["entries"][0]
        source["source_verification"] = {"verification": "validated", "evidence_status": "current"}
        source["source_chain"][0]["content"].update(verification="validated", evidence_status="current", creation_mode="workflow_v2")
        self.resign(package, chain=True)
        local_id = self.import_package(package)["imported"][0]["entry_id"]
        entry = knowledge_entries.get(self.target, local_id)
        self.assertEqual(entry["verification"], "hypothesis")
        self.assertEqual(entry["evidence_status"], "imported_unverified")
        self.assertEqual(entry["creation_mode"], "import")
        self.assertEqual(entry["provenance"]["source_verification"], source["source_verification"])
        canonical = knowledge_entries.read_entry(self.target, local_id)
        self.assertNotIn("verification", canonical)
        self.assertNotIn("evidence_status", canonical)

    def test_package_and_source_content_tampering_are_rejected(self):
        original = self.create_entry()
        package = self.export(original)
        package["entries"][0]["source_chain"][0]["content"]["text"] = "tampered"
        self.assert_error("invalid_package", lambda: self.import_package(package))
        self.resign(package)
        self.assert_error("invalid_package", lambda: self.import_package(package))

    def test_missing_ancestor_or_mismatched_wrappers_reject_entire_package(self):
        original = self.create_entry()
        source2 = self.revise(self.source, original["id"])
        valid = self.export(source2)
        variants = []
        missing = copy.deepcopy(valid)
        missing["entries"][0]["source_chain"].pop(0)
        variants.append(missing)
        for field, value in (("revision", 1), ("parent_content_hash", "0" * 64)):
            changed = copy.deepcopy(valid)
            changed["entries"][0]["source_chain"][1][field] = value
            variants.append(changed)
        changed = copy.deepcopy(valid)
        changed["entries"][0]["source_chain"][1]["content"]["revision"] = 1
        variants.append(changed)
        changed = copy.deepcopy(valid)
        changed["entries"][0]["source_chain"][0]["content"]["content_hash"] = "0" * 64
        variants.append(changed)
        for index, package in enumerate(variants):
            with self.subTest(index=index):
                before = domain.read(self.target)["head"]
                self.resign(package)
                self.assert_error("invalid_package", lambda package=package, index=index: self.import_package(package, f"invalid-{index}"))
                self.assertEqual(domain.read(self.target)["head"], before)

    def test_duplicate_unknown_schema_and_invalid_entry_body_are_rejected(self):
        original = self.create_entry()
        valid = self.export(original)
        duplicate = copy.deepcopy(valid)
        duplicate["entries"].append(copy.deepcopy(duplicate["entries"][0]))
        schema = {**copy.deepcopy(valid), "schema_version": 2}
        body = copy.deepcopy(valid)
        body["entries"][0]["source_chain"][0]["content"]["title"] = ""
        self.resign(body, chain=True)
        for index, package in enumerate((duplicate, schema, body)):
            with self.subTest(index=index):
                self.resign(package)
                self.assert_error("invalid_package", lambda package=package, index=index: self.import_package(package, f"invalid-{index}"))

    def test_valid_first_entry_invalid_second_entry_has_no_publication(self):
        first, second = self.create_entry(), self.create_entry(title="第二条")
        package = knowledge_transfer.export_package(self.source, entries=[{"id": first["id"], "revision": 1}, {"id": second["id"], "revision": 1}])
        package["entries"][1]["source_chain"][0]["content"]["text"] = "tampered second"
        self.resign(package)
        before = domain.read(self.target)
        with patch.object(domain, "stage", wraps=domain.stage) as stage:
            self.assert_error("invalid_package", lambda: self.import_package(package))
        stage.assert_not_called()
        self.assertEqual(domain.read(self.target), before)

    def test_second_entry_local_conflict_rolls_back_first_entry_and_all_maps(self):
        first, second = self.create_entry(), self.create_entry(title="第二条")
        local_id = self.import_package(self.export(second))["imported"][0]["entry_id"]
        self.revise(self.target, local_id, text="local edit")
        second2 = self.revise(self.source, second["id"], text="source edit")
        package = knowledge_transfer.export_package(self.source, entries=[{"id": first["id"], "revision": 1}, {"id": second2["id"], "revision": 2}])
        before = domain.read(self.target)
        self.assert_error("revision_conflict", lambda: self.import_package(package, "mixed-conflict"))
        self.assertEqual(domain.read(self.target), before)

    def test_domain_cas_conflict_leaves_no_partial_entry_or_import_receipt_and_can_retry(self):
        original = self.create_entry()
        package = self.export(original)
        publish = domain.publish

        def concurrent_publish(project_id, stage_id):
            current = domain.read(project_id)
            domain.commit(project_id, {"unrelated.json": {"value": "concurrent"}}, current["head"], "并发修改")
            return publish(project_id, stage_id)

        with patch.object(domain, "publish", side_effect=concurrent_publish):
            self.assert_error("revision_conflict", lambda: self.import_package(package))
        current = domain.read(self.target)
        self.assertEqual(current["documents"], {"unrelated.json": {"value": "concurrent"}})
        receipt = self.import_package(package)
        self.assertEqual(len(receipt["imported"]), 1)
        self.assertIn("unrelated.json", domain.read(self.target)["documents"])

    def test_successful_multi_entry_import_has_exactly_one_stage_and_publication(self):
        self.create_entry()
        self.create_entry(title="第二条")
        package = knowledge_transfer.export_package(self.source, all_items=True)
        with patch.object(domain, "stage", wraps=domain.stage) as stage, patch.object(domain, "publish", wraps=domain.publish) as publish:
            receipt = self.import_package(package)
        self.assertEqual(len(receipt["imported"]), 2)
        self.assertEqual(stage.call_count, 1)
        self.assertEqual(publish.call_count, 1)
        self.assertEqual(domain.read(self.target)["head"], receipt["domain_head"])

    def test_concurrent_identical_imports_return_one_receipt_and_one_publication(self):
        package = self.export(self.create_entry())
        with patch.object(domain, "publish", wraps=domain.publish) as publish, ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(self.import_package, package) for _ in range(8)]
            receipts = [future.result(timeout=10) for future in futures]
        self.assertTrue(all(receipt == receipts[0] for receipt in receipts))
        self.assertEqual(publish.call_count, 1)
        local_id = receipts[0]["imported"][0]["entry_id"]
        self.assertEqual(knowledge_entries.read_entry(self.target, local_id)["revision"], 1)

    def test_changed_local_content_without_revision_increment_cannot_be_overwritten(self):
        original = self.create_entry()
        local_id = self.import_package(self.export(original))["imported"][0]["entry_id"]
        entry = knowledge_entries.read_entry(self.target, local_id)
        entry["text"] = "changed local bytes"
        self.store_entry(self.target, entry)
        newer = self.revise(self.source, original["id"])
        self.assert_error("revision_conflict", lambda: self.import_package(self.export(newer), "changed-local-hash"))
        self.assertEqual(knowledge_entries.read_entry(self.target, local_id)["text"], "changed local bytes")

    def test_selector_and_request_validation(self):
        original = self.create_entry()
        for kwargs in ({}, {"entries": []}, {"entries": [{"id": original["id"], "revision": True}]},
                       {"entries": [{"id": original["id"], "revision": 1}], "all_items": True},
                       {"entries": [{"id": original["id"], "revision": 1}] * 2}, {"all_items": "true"}):
            with self.subTest(kwargs=kwargs):
                self.assert_error("invalid_selector", lambda kwargs=kwargs: knowledge_transfer.export_package(self.source, **kwargs))
        package = self.export(original)
        self.assert_error("invalid_package", lambda: self.import_package(package, ""))
        self.assert_error("invalid_package", lambda: self.import_package(package, as_new=1))
        self.assert_error("invalid_package", lambda: self.import_package({"nan": float("nan")}))


if __name__ == "__main__":
    unittest.main()
