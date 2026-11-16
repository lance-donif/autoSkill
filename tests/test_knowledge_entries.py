"""Knowledge revisions qualify real local receipts without inheriting claims."""
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

from autoskill import builder, context_service, data_refs, db, domain, paging, projects, runner, workflow
from autoskill import knowledge_entries as knowledge


class AnswerHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        raw = json.dumps({"ok": self.server.ok}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        return


class KnowledgeEntryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), AnswerHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        env = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": directory.name, "AUTOSKILL_HEADED": "0", "AUTOSKILL_CASE_SKILL": str(self.folder / "plan")})
        env.start()
        self.addCleanup(env.stop)
        self.pid = projects.create("知识验证", f"http://127.0.0.1:{self.server.server_port}", project_id="knowledge")["id"]
        self.server.ok = True
        self.other = projects.create("同组知识", "http://127.0.0.1:1", project_id="other")["id"]
        self.case = builder.api_case("TC-API-001", "响应结果", "GET", "/business", {"status": 200, "json_equals": {"/ok": True}})
        projects.save_cases(self.pid, [self.case])
        for target in (patch.object(runner, "spawn", return_value=None), patch("autoskill.server.jobs.ensure_worker", return_value={"status": "running"})):
            target.start()
            self.addCleanup(target.stop)

    def body(self, *, project=None, **extra):
        return {"title": "接口知识", "text": "检查响应中的业务值", "kind": "observation",
                "scope": {"team_id": None, "business_scope": None, "project_id": project or self.pid, "source_version_hashes": {}},
                "evidence_refs": [], "validation_refs": [], **extra}

    def payload(self, body=None, *, key="first"):
        return {"schema_version": 2, "mutations": [{"op": "create", "client_key": key, "entry": body or self.body()}]}

    def save(self, payload=None, *, project=None, workflow_id="workflow", cycle="cycle"):
        project = project or self.pid
        current = domain.read(project)
        prepared = knowledge.prepare_mutations(project, workflow_id, cycle, payload or self.payload(), current=current)
        staged = domain.stage(project, prepared["changes"], current["head"])
        domain.publish(project, staged["stage_id"])
        return knowledge.get(project, prepared["entries"][0]["id"])

    def execute(self):
        run = runner.create(self.pid, [self.case["id"]])
        run = runner.execute(run["id"])
        result = db.one("SELECT * FROM results WHERE run_id = ? AND case_id = ?", (run["id"], self.case["id"]))
        attempt = json.loads(result["detail"])["attempts"][0]
        return run, result, attempt

    def evidence(self, result, attempt):
        return {"run_id": result["run_id"], "result_id": result["id"], "execution_attempt_id": attempt["id"], "sha256": attempt["sha256"]}

    def grouped(self, project):
        current = knowledge.settings(project)
        return knowledge.settings(project, {"expected_hash": current["content_hash"], "team_id": "team", "business_scope": "payments"})

    def test_prepare_is_atomic_deterministic_and_has_no_publication_side_effect(self):
        before = domain.read(self.pid)
        first = knowledge.prepare_mutations(self.pid, "workflow", "cycle", self.payload())
        second = knowledge.prepare_mutations(self.pid, "workflow", "cycle", self.payload())
        self.assertEqual(first, second)
        self.assertEqual(domain.read(self.pid), before)
        bad = self.payload()
        bad["mutations"].append({"op": "create", "client_key": "bad", "entry": {"text": "missing fields"}})
        with self.assertRaises(knowledge.KnowledgeError):
            knowledge.prepare_mutations(self.pid, "workflow", "cycle", bad)
        self.assertEqual(domain.read(self.pid), before)
        entry = self.save()
        self.assertEqual(uuid.UUID(entry["id"]).version, 5)
        self.assertEqual(entry["verification"], "hypothesis")
        self.assertEqual(entry["applicability"], "unknown")

    def test_create_rev_conflict_and_withdraw_preserve_exact_history(self):
        first = self.save()
        revise = {"schema_version": 2, "mutations": [{"op": "revise", "client_key": "second", "entry_id": first["id"],
                   "expected_revision": 1, "expected_content_hash": first["content_hash"], "entry": self.body(text="更新的观察")} ]}
        second = self.save(revise)
        self.assertEqual(second["revision"], 2)
        self.assertEqual(second["parent_content_hash"], first["content_hash"])
        self.assertEqual(knowledge.get(self.pid, first["id"], revision=1), first)
        with self.assertRaises(knowledge.KnowledgeError) as failure:
            self.save(revise)
        self.assertEqual(failure.exception.code, "revision_conflict")
        with self.assertRaises(knowledge.KnowledgeError):
            self.save()
        withdrawn = self.save({"schema_version": 2, "mutations": [{"op": "withdraw", "client_key": "third", "entry_id": first["id"],
                              "expected_revision": 2, "expected_content_hash": second["content_hash"], "reason": "范围已变化"}]})
        self.assertEqual(withdrawn["revision"], 3)
        self.assertEqual(withdrawn["lifecycle"], "withdrawn")
        self.assertEqual(knowledge.get(self.pid, first["id"], revision=2)["text"], "更新的观察")

    def test_batch_identity_and_kernel_fields_cannot_be_spoofed(self):
        for field, value in (("verification", "execution_reproduced"), ("id", str(uuid.uuid4())), ("creation_mode", "workflow_v2")):
            with self.subTest(field=field), self.assertRaises(knowledge.KnowledgeError):
                self.save(self.payload(self.body(**{field: value})))
        payload = self.payload()
        payload["mutations"] *= 2
        with self.assertRaises(knowledge.KnowledgeError):
            self.save(payload)

    def test_legacy_conversion_requires_text_and_never_trusts_old_claims(self):
        with self.assertRaises(knowledge.KnowledgeError):
            self.save({"entries": [{"id": "legacy-only"}]})
        _, result, attempt = self.execute()
        old = {"id": "legacy", "text": "旧知识正文", "verification": "observed", "evidence_refs": [self.evidence(result, attempt)]}
        entry = self.save({"entries": [old]})
        self.assertEqual(entry["verification"], "hypothesis")
        self.assertEqual(entry["creation_mode"], "legacy_conversion")
        self.assertEqual(entry["legacy_metadata"], old)
        self.assertEqual(entry["evidence_refs"], [])

    def test_real_completed_receipt_supports_observation_but_not_root_cause(self):
        _, result, attempt = self.execute()
        body = self.body(evidence_refs=[self.evidence(result, attempt)])
        entry = self.save(self.payload(body))
        self.assertEqual((entry["verification"], entry["evidence_status"]), ("observed", "valid"))
        self.assertEqual(entry["applicability"], "unknown")
        root_cause = self.save(self.payload({**body, "kind": "root_cause_hypothesis"}, key="cause"))
        self.assertEqual(root_cause["verification"], "hypothesis")
        self.assertEqual(root_cause["evidence_status"], "valid")

    def test_actual_failure_is_an_observation_and_is_never_rewritten_as_success(self):
        self.server.ok = False
        run, result, attempt = self.execute()
        entry = self.save(self.payload(self.body(text="业务断言实际失败", evidence_refs=[self.evidence(result, attempt)])))
        self.assertEqual(entry["verification"], "observed")
        self.assertEqual(run["status"], "Fail")
        self.assertEqual(db.one("SELECT status FROM results WHERE id = ?", (result["id"],))["status"], "Fail")

    def test_diagnosis_receipt_refs_resolve_actual_json_pointer(self):
        _, result, attempt = self.execute()
        ref = {"kind": "receipt", "run_id": result["run_id"], "case_id": result["case_id"], "execution_attempt_id": attempt["id"],
               "receipt_sha256": attempt["sha256"], "pointer": "/outcome/assertions/0"}
        entry = self.save(self.payload(self.body(evidence_refs=[ref])))
        self.assertEqual(entry["verification"], "observed")
        invalid = self.save(self.payload(self.body(evidence_refs=[{**ref, "pointer": "/outcome/assertions/99"}]), key="missing-pointer"))
        self.assertEqual((invalid["verification"], invalid["evidence_status"]), ("hypothesis", "tampered"))
        metadata = self.save(self.payload(self.body(evidence_refs=[{**ref, "pointer": "/id"}]), key="metadata"))
        self.assertEqual(metadata["verification"], "hypothesis")

    def test_blocked_before_send_with_no_fact_checks_does_not_promote_observation(self):
        candidate = {**self.case, "path": "/{{missing}}"}
        run = runner.create(self.pid, [candidate["id"]], candidate_cases=[candidate])
        final = runner.execute(run["id"])
        result = db.one("SELECT * FROM results WHERE run_id = ?", (run["id"],))
        detail = json.loads(result["detail"])
        self.assertEqual(final["status"], "Blocked")
        receipt = json.loads(data_refs.resolve(detail["attempts"][0]["path"]).read_bytes())
        self.assertEqual(receipt["outcome"].get("assertions", []), [])
        self.assertIsNone(receipt["outcome"].get("request"))
        entry = self.save(self.payload(self.body(evidence_refs=[self.evidence(result, detail["attempts"][0])])))
        self.assertEqual(entry["verification"], "hypothesis")
        self.assertEqual(entry["evidence_status"], "valid")

    def test_tampered_or_deleted_receipt_revokes_current_observation(self):
        _, result, attempt = self.execute()
        entry = self.save(self.payload(self.body(evidence_refs=[self.evidence(result, attempt)])))
        file = data_refs.resolve(attempt["path"])
        original = file.read_bytes()
        file.write_bytes(original.replace(b'"completed"', b'"altered"'))
        current = knowledge.get(self.pid, entry["id"])
        self.assertEqual((current["verification"], current["evidence_status"]), ("hypothesis", "tampered"))
        file.unlink()
        current = knowledge.get(self.pid, entry["id"])
        self.assertEqual((current["verification"], current["evidence_status"]), ("hypothesis", "missing"))

    def test_malformed_result_evidence_cannot_crash_search_or_context(self):
        _, result, attempt = self.execute()
        entry = self.save(self.payload(self.body(evidence_refs=[self.evidence(result, attempt)])))
        for detail in ([], "invalid shape", {"attempts": [None]}, {"attempts": {"id": "invalid"}}):
            with self.subTest(detail=detail), db.connect() as connection:
                connection.execute("UPDATE results SET detail = ? WHERE id = ?", (json.dumps(detail), result["id"]))
            projected = knowledge.get(self.pid, entry["id"])
            self.assertEqual((projected["verification"], projected["evidence_status"]), ("hypothesis", "tampered"))
            self.assertTrue(knowledge.search(self.pid, query="接口知识")["entries"])
            self.assertTrue(context_service.context(self.pid)["knowledge_entries"])

    def test_foreign_receipt_and_wrong_scope_do_not_establish_local_fact(self):
        _, result, attempt = self.execute()
        ref = self.evidence(result, attempt)
        foreign = self.save(self.payload(self.body(project=self.other, evidence_refs=[ref])), project=self.other)
        self.assertEqual((foreign["verification"], foreign["evidence_status"]), ("hypothesis", "tampered"))
        body = self.body(evidence_refs=[ref])
        body["scope"]["source_version_hashes"] = {"domain/source.json": "a" * 64}
        wrong = self.save(self.payload(body))
        self.assertEqual(wrong["verification"], "hypothesis")

    def test_applicability_changed_and_unknown_are_independent_of_observation(self):
        domain.commit(self.pid, {"source.json": {"version": 1}}, None)
        frozen = domain.read(self.pid)
        _, result, attempt = self.execute()
        body = self.body(evidence_refs=[self.evidence(result, attempt)])
        body["scope"]["source_version_hashes"] = {"domain/source.json": frozen["files"]["source.json"]["sha256"]}
        entry = self.save(self.payload(body))
        self.assertEqual((entry["verification"], entry["applicability"]), ("observed", "match"))
        domain.commit(self.pid, {"source.json": {"version": 2}}, domain.read(self.pid)["head"])
        changed = knowledge.get(self.pid, entry["id"])
        self.assertEqual((changed["verification"], changed["applicability"]), ("observed", "changed"))
        domain.commit(self.pid, {}, domain.read(self.pid)["head"], remove=["source.json"])
        self.assertEqual(knowledge.get(self.pid, entry["id"])["applicability"], "unknown")

    def test_settings_cas_and_authoritative_group_membership(self):
        self.assertIsNone(knowledge.settings(self.pid)["content_hash"])
        setting = self.grouped(self.pid)
        with self.assertRaises(knowledge.KnowledgeError) as failure:
            knowledge.settings(self.pid, {"expected_hash": None, "team_id": "other", "business_scope": "payments"})
        self.assertEqual(failure.exception.code, "revision_conflict")
        for pair in (("team", None), ("", "payments"), (None, "payments")):
            with self.subTest(pair=pair), self.assertRaises(knowledge.KnowledgeError):
                knowledge.settings(self.pid, {"expected_hash": setting["content_hash"], "team_id": pair[0], "business_scope": pair[1]})
        body = self.body(project=self.other)
        body["scope"].update(team_id="team", business_scope="payments")
        self.save(self.payload(body), project=self.other)
        self.assertEqual(knowledge.search(self.pid, team=True, query="接口知识")["entries"], [])
        self.grouped(self.other)
        results = knowledge.search(self.pid, team=True, query="接口知识")
        self.assertEqual(len(results["entries"]), 1)
        self.assertEqual(results["scope_note"], "team")

    def test_team_result_exposes_source_verification_without_local_promotion(self):
        self.grouped(self.pid)
        self.grouped(self.other)
        _, result, attempt = self.execute()
        source = self.save(self.payload(self.body(evidence_refs=[self.evidence(result, attempt)])))
        result = next(row for row in knowledge.search(self.other, team=True)["entries"] if row["id"] == source["id"])
        self.assertEqual(result["source_verification"], "observed")
        self.assertEqual((result["verification"], result["applicability"]), ("hypothesis", "unknown"))

    def test_unconfigured_team_is_project_only_and_legacy_files_remain_searchable(self):
        file = projects.path(self.pid) / "knowledge" / "legacy.txt"
        file.write_text("旧文件里的订单业务知识", encoding="utf-8")
        results = knowledge.search(self.pid, query="订单业务", team=True)
        self.assertEqual(results["scope_note"], "project_only_unconfigured")
        self.assertEqual(len(results["entries"]), 1)
        self.assertTrue(results["entries"][0]["legacy"])
        self.assertEqual(results["entries"][0]["verification"], "hypothesis")
        self.assertTrue(file.is_file())

    def test_large_legacy_and_canonical_bodies_are_searchable_but_context_is_bounded(self):
        text = "x" * (1024 * 1024) + "末尾独立关键词"
        canonical = self.save(self.payload(self.body(title="t" * 1000, text=text)))
        file = projects.path(self.pid) / "knowledge" / "large.txt"
        file.write_text(text, encoding="utf-8")
        results = knowledge.search(self.pid, query="末尾独立关键词")
        self.assertEqual(len(results["entries"]), 2)
        for row in results["entries"]:
            self.assertNotIn("text", row)
            self.assertNotIn("provenance", row)
            self.assertNotIn("evidence_refs", row)
            self.assertLessEqual(len(row["preview"]), 400)
            self.assertLessEqual(len(row["title"]), 160)
            self.assertTrue(row["truncated"])
            self.assertEqual(row["total_characters"], len(text))
        context = context_service.context(self.pid)
        self.assertLess(len(json.dumps(context, ensure_ascii=False)), 15000)
        self.assertEqual(knowledge.get(self.pid, canonical["id"])["text"], text)
        self.assertEqual(context_service.read_knowledge(self.pid, "knowledge/large.txt", all_items=True)["content"], text)

    def test_search_cursor_expires_on_settings_domain_and_raw_file_changes(self):
        for key in ("a", "b", "c"):
            self.save(self.payload(self.body(title="cursor " + key), key=key))
        self.grouped(self.pid)
        self.grouped(self.other)
        first = knowledge.search(self.pid, query="cursor", team=True, limit=1)
        second = knowledge.search(self.pid, query="cursor", team=True, limit=1, cursor=first["page"]["next_cursor"])
        self.assertNotEqual(first["entries"][0]["id"], second["entries"][0]["id"])
        setting = knowledge.settings(self.other)
        knowledge.settings(self.other, {"expected_hash": setting["content_hash"], "team_id": None, "business_scope": None})
        with self.assertRaises(paging.PageError) as failure:
            knowledge.search(self.pid, query="cursor", team=True, limit=1, cursor=first["page"]["next_cursor"])
        self.assertEqual(failure.exception.code, "cursor_expired")
        first = knowledge.search(self.pid, query="cursor", limit=1)
        (projects.path(self.pid) / "knowledge" / "new.txt").write_text("changed", encoding="utf-8")
        with self.assertRaises(paging.PageError):
            knowledge.search(self.pid, query="cursor", limit=1, cursor=first["page"]["next_cursor"])
        first = knowledge.search(self.pid, query="cursor", limit=1)
        domain.commit(self.pid, {"source.json": {}}, domain.read(self.pid)["head"])
        with self.assertRaises(paging.PageError):
            knowledge.search(self.pid, query="cursor", limit=1, cursor=first["page"]["next_cursor"])

    def test_exact_usage_refs_require_retained_revision_and_hash(self):
        entry = self.save()
        ref = {key: entry[key] for key in ("owner_project_id", "id", "revision", "content_hash")}
        ref["purpose"] = "使用该观察设计重试检查"
        self.assertEqual(knowledge.validate_knowledge_used(self.other, [ref]), [ref])
        for changed in ({"content_hash": "a" * 64}, {"revision": 2}, {"purpose": " "}):
            with self.subTest(changed=changed), self.assertRaises(knowledge.KnowledgeError):
                knowledge.validate_knowledge_used(self.other, [{**ref, **changed}])

    def test_import_cannot_inherit_local_execution_or_withdraw_to_promote(self):
        _, result, attempt = self.execute()
        original = self.save(self.payload(self.body(evidence_refs=[self.evidence(result, attempt)])))
        entry = knowledge.read_entry(self.pid, original["id"])
        entry.update(id=str(uuid.uuid4()), creation_mode="import", imported_at=db.now(),
                     provenance={"source_verification": "observed", "origin_project_id": "foreign"})
        domain.commit(self.pid, knowledge.make_changes(entry), domain.read(self.pid)["head"])
        imported = knowledge.get(self.pid, entry["id"])
        self.assertEqual((imported["verification"], imported["evidence_status"]), ("hypothesis", "imported_unverified"))
        withdraw = {"schema_version": 2, "mutations": [{"op": "withdraw", "client_key": "withdraw-import", "entry_id": entry["id"],
                    "expected_revision": 1, "expected_content_hash": imported["content_hash"], "reason": "保留历史"}]}
        withdrawn = self.save(withdraw)
        self.assertEqual(withdrawn["evidence_status"], "imported_unverified")

    def test_import_local_revision_requires_new_local_receipt_after_import(self):
        _, old_result, old_attempt = self.execute()
        entry = {**self.body(evidence_refs=[self.evidence(old_result, old_attempt)]), "id": str(uuid.uuid4()), "revision": 1,
                 "schema_version": 1, "parent_content_hash": None, "creation_mode": "import", "lifecycle": "active", "imported_at": db.now(),
                 "provenance": {"origin_project_id": "foreign", "source_verification": "observed"}}
        domain.commit(self.pid, knowledge.make_changes(entry), domain.read(self.pid)["head"])
        imported = knowledge.get(self.pid, entry["id"])
        mutation = {"op": "revise", "client_key": "local", "entry_id": entry["id"], "expected_revision": 1,
                    "expected_content_hash": imported["content_hash"], "entry": self.body(evidence_refs=[self.evidence(old_result, old_attempt)])}
        old = self.save({"schema_version": 2, "mutations": [mutation]})
        self.assertEqual(old["verification"], "hypothesis")
        _, result, attempt = self.execute()
        mutation.update(client_key="new-local", expected_revision=2, expected_content_hash=old["content_hash"],
                        entry=self.body(evidence_refs=[self.evidence(result, attempt)]))
        local = self.save({"schema_version": 2, "mutations": [mutation]})
        self.assertEqual(local["verification"], "observed")
        self.assertEqual(local["provenance"], imported["provenance"])

    def validation(self, repetitions=3, *, failed_round=None):
        def key():
            return uuid.uuid4().hex

        identity = workflow.create(self.pid, {"title": "重复检查", "sources": [{"id": "s1", "text": "ok 应为 true"}],
                                              "validation": {"repetitions": repetitions}}, idempotency_key=key())["workflow_id"]

        def submit(payload):
            task = workflow.next_task(identity, idempotency_key=key())["task"]
            workflow.submit(identity, task["id"], lease_token=task["lease_token"], attempt_id=task["attempt_id"], idempotency_key=key(), payload=payload)

        submit({"requirements": [{"id": "r1", "text": "响应 ok 为 true", "source_ids": ["s1"]}]})
        submit({"scenarios": [{"id": "sc1", "title": "业务验证", "requirement_ids": ["r1"], "oracle_ids": ["o1"]}],
                "oracles": [{"id": "o1", "kind": "api", "requirement_ids": ["r1"], "checks": self.case["assertions"]}]})
        submit({"cases": [self.case], "bindings": {"sc1": [self.case["id"]]}})
        run_ids = []
        for number in range(repetitions):
            for _ in range(3):
                self.assertIsNone(workflow.next_task(identity, idempotency_key=key())["task"])
                row = db.rows("SELECT * FROM workflow_items WHERE workflow_id = ? AND phase = 'execute' ORDER BY sequence", (identity,))[-1]
                if row["run_id"] and row["run_id"] not in run_ids:
                    break
            self.assertIsNotNone(row["run_id"])
            run_ids.append(row["run_id"])
            self.server.ok = failed_round != number
            runner.execute(row["run_id"])
        self.server.ok = True
        self.last_task = workflow.next_task(identity, idempotency_key=key())["task"]
        row = db.rows("SELECT * FROM workflow_items WHERE workflow_id = ? AND phase = 'execute' ORDER BY sequence", (identity,))[0]
        return identity, json.loads(row["context"])["validation_id"], run_ids

    def test_predeclared_three_complete_business_runs_bind_procedure_revision(self):
        identity, validation_id, run_ids = self.validation()
        body = self.body(kind="procedure", validation_refs=[{"workflow_id": identity, "validation_id": validation_id}])
        entry = self.save(self.payload(body))
        self.assertEqual((entry["verification"], entry["evidence_status"]), ("execution_reproduced", "valid"))
        self.assertEqual(entry["applicability"], "unknown")
        binding = domain.read(self.pid)["documents"][knowledge.binding_path(entry["id"], 1)]
        self.assertEqual(binding["run_ids"], run_ids)
        self.assertEqual(binding["content_hash"], entry["content_hash"])
        self.assertEqual(len(set(binding["execution_attempt_ids"])), 3)
        result = db.one("SELECT * FROM results WHERE run_id = ?", (run_ids[0],))
        attempt = json.loads(result["detail"])["attempts"][0]
        data_refs.resolve(attempt["path"]).unlink()
        changed = knowledge.get(self.pid, entry["id"])
        self.assertEqual((changed["verification"], changed["evidence_status"]), ("hypothesis", "missing"))

    def test_two_predeclared_runs_and_arbitrary_passes_cannot_reproduce_procedure(self):
        identity, validation_id, _ = self.validation(2)
        refs = [{"workflow_id": identity, "validation_id": validation_id}]
        self.execute()
        entry = self.save(self.payload(self.body(kind="procedure", validation_refs=refs)))
        self.assertEqual(entry["verification"], "hypothesis")

    def test_any_invalid_round_prevents_procedure_even_with_three_later_passes(self):
        identity, validation_id, run_ids = self.validation(4, failed_round=0)
        entry = self.save(self.payload(self.body(kind="procedure", validation_refs=[{"workflow_id": identity, "validation_id": validation_id, "run_ids": run_ids[1:]}])))
        self.assertEqual(entry["verification"], "hypothesis")

    def test_imported_procedure_requires_a_new_predeclared_local_plan(self):
        identity, validation_id, _ = self.validation()
        body = self.body(kind="procedure", validation_refs=[{"workflow_id": identity, "validation_id": validation_id}])
        imported = {**body, "id": str(uuid.uuid4()), "revision": 1, "schema_version": 1, "parent_content_hash": None,
                    "creation_mode": "import", "lifecycle": "active", "imported_at": db.now(),
                    "provenance": {"origin_project_id": "foreign", "source_verification": "execution_reproduced"}}
        domain.commit(self.pid, knowledge.make_changes(imported), domain.read(self.pid)["head"])
        current = knowledge.get(self.pid, imported["id"])
        mutation = {"op": "revise", "client_key": "old-plan", "entry_id": imported["id"], "expected_revision": 1,
                    "expected_content_hash": current["content_hash"], "entry": body}
        old = self.save({"schema_version": 2, "mutations": [mutation]})
        self.assertEqual(old["verification"], "hypothesis")
        identity, validation_id, _ = self.validation()
        body = self.body(kind="procedure", validation_refs=[{"workflow_id": identity, "validation_id": validation_id}])
        mutation.update(client_key="new-plan", expected_revision=2, expected_content_hash=old["content_hash"], entry=body)
        new = self.save({"schema_version": 2, "mutations": [mutation]})
        self.assertEqual(new["verification"], "execution_reproduced")
        self.assertEqual(new["provenance"], imported["provenance"])

    def test_procedure_rebinding_does_not_silently_carry_old_verification(self):
        identity, validation_id, _ = self.validation()
        entry = self.save(self.payload(self.body(kind="procedure", validation_refs=[{"workflow_id": identity, "validation_id": validation_id}])))
        self.assertEqual(entry["verification"], "execution_reproduced")
        changed = self.save({"schema_version": 2, "mutations": [{"op": "revise", "client_key": "revise", "entry_id": entry["id"],
                            "expected_revision": 1, "expected_content_hash": entry["content_hash"], "entry": self.body(kind="procedure", text="另一个执行建议")}]})
        self.assertEqual(changed["verification"], "hypothesis")
        self.assertEqual(knowledge.get(self.pid, entry["id"], revision=1)["verification"], "execution_reproduced")

    def test_withdrawn_procedure_keeps_historical_verification_on_independent_axis(self):
        identity, validation_id, _ = self.validation()
        entry = self.save(self.payload(self.body(kind="procedure", validation_refs=[{"workflow_id": identity, "validation_id": validation_id}])))
        withdrawn = self.save({"schema_version": 2, "mutations": [{"op": "withdraw", "client_key": "withdraw", "entry_id": entry["id"],
                              "expected_revision": 1, "expected_content_hash": entry["content_hash"], "reason": "已停用此过程"}]})
        self.assertEqual((withdrawn["lifecycle"], withdrawn["verification"]), ("withdrawn", "execution_reproduced"))


if __name__ == "__main__":
    unittest.main()
