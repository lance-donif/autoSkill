"""Independent local HTTP lifecycle contracts for the Python code review rule.

The fixture contract and assertions are declared before any request. No remote
service, browser, stored credential or previous execution result is consulted.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import tempfile
import threading
import unittest
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill import candidate_publication, data_refs, db, execution_inputs, projects, runner
from autoskill.core import store
from autoskill.recorder.compile import Compiler
from autoskill.replay import api

EVIDENCE: list[dict] = []
REQ = "REQ-API-REVIEW"
ORDER = ["Z-login", "Y-create", "X-read", "W-update", "V-confirm", "B-delete", "A-absent"]
INPUT_A = {"payload": {"quantity": 2, "enabled": True, "optional": None, "labels": ["blue"]},
           "updated_payload": {"quantity": 3, "enabled": False, "optional": "checked", "labels": []}}
INPUT_B = {"payload": {"quantity": 5, "enabled": False, "optional": "different", "labels": ["red"]},
           "updated_payload": {"quantity": 8, "enabled": True, "optional": None, "labels": ["green"]}}


def api_case(identity: str, method: str, path: str, assertions: dict, *, dependencies: tuple = (), **fields) -> dict:
    """Declare an independent business case without deriving expected values from observations."""
    return {"schema_version": "1.1.0", "id": identity, "kind": "api", "revision": 1, "lifecycle": "active",
            "requirement_ids": [REQ], "module": "本地记录生命周期", "title": identity, "priority": "P1",
            "preconditions": "使用本次登录和本次创建的记录", "steps": ["执行本步业务操作并检查独立预期"],
            "test_data": "独立审查合成数据", "expected": "记录按明确输入创建、修改，清理后查不到", "source": "local-fixture-contract-v1",
            "risk": "read" if method == "GET" else "delete" if method == "DELETE" else "write", "tags": [],
            "extensions": {"oracle": {"source": "requirement", "requirement_ids": [REQ]}, "execution": {"api_only": True},
                           **({"depends_on": list(dependencies)} if dependencies else {})},
            "api_source": "source-and-official-docs", "source_ref": "local-fixture-contract-v1",
            "method": method, "path": path, "assertions": copy.deepcopy(assertions), **copy.deepcopy(fields)}


def lifecycle(delete_status: int = 204) -> list[dict]:
    headers = {"Cookie": "session={{session_token}}; csrf={{csrf_token}}"}
    login = api_case(ORDER[0], "POST", "/login", {"status": 200, "json_equals": {"/role": "auditor"}}, json={"user": "reviewer"})
    login["extensions"]["extract"] = {"session_token": "/token", "csrf_token": "/csrf"}
    create = api_case(ORDER[1], "POST", "/records", {"status": 201, "json_equals": {
        "/name": "record-{{RAND}}", "/payload": "{{payload}}", "/state": "draft"}}, dependencies=(ORDER[0],),
        headers=headers, json={"name": "record-{{RAND}}", "payload": "{{payload}}"})
    create["extensions"]["extract"] = {"record_id": "/id"}
    read = api_case(ORDER[2], "GET", "/records/{{record_id}}", {"status": 200, "json_equals": {
        "/name": "record-{{RAND}}", "/payload": "{{payload}}", "/state": "draft"}}, dependencies=(ORDER[1],), headers=headers)
    update = api_case(ORDER[3], "PATCH", "/records/{{record_id}}", {"status": 200, "json_equals": {
        "/payload": "{{updated_payload}}", "/state": "reviewed"}}, dependencies=(ORDER[2],), headers=headers,
        json={"payload": "{{updated_payload}}", "state": "reviewed"})
    confirm = api_case(ORDER[4], "GET", "/records/{{record_id}}", {"status": 200, "json_equals": {
        "/payload": "{{updated_payload}}", "/state": "reviewed"}}, dependencies=(ORDER[3],), headers=headers)
    delete = api_case(ORDER[5], "DELETE", "/records/{{record_id}}", {"status": delete_status}, dependencies=(ORDER[4],), headers=headers)
    delete["extensions"]["postconditions"] = [ORDER[6]]
    absent = api_case(ORDER[6], "GET", "/records", {"status": 200, "json_equals": {"/items": []}},
                      dependencies=(ORDER[5],), headers=headers, params={"id": "{{record_id}}"})
    return [login, create, read, update, confirm, delete, absent]


class LifecycleServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), LifecycleHandler)
        self.records: dict[int, dict] = {}
        self.sessions: dict[str, str] = {}
        self.serial = 0
        self.login_count = 0
        self.delete_status = 204
        self.retain_on_delete = False
        self.events: list[dict] = []


class LifecycleHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, message, *args):
        return

    def _respond(self, status: int, value=None):
        body = json.dumps(value).encode() if value is not None else b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.server.events.append({"method": self.command, "path": self.path, "status": status})

    def _body(self):
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")

    def _authenticated(self):
        cookies = SimpleCookie(self.headers.get("Cookie", ""))
        token = cookies["session"].value if "session" in cookies else ""
        csrf = cookies["csrf"].value if "csrf" in cookies else ""
        if token in self.server.sessions and self.server.sessions[token] == csrf:
            return True
        self._respond(401, {"error": "unauthenticated"})
        return False

    def do_POST(self):
        body = self._body()
        if self.path == "/login":
            if body != {"user": "reviewer"}:
                self._respond(400, {"error": "invalid_login"})
                return
            self.server.login_count += 1
            token = f"synthetic-session-{self.server.login_count}"
            csrf = f"synthetic-csrf-{self.server.login_count}"
            self.server.sessions[token] = csrf
            self._respond(200, {"token": token, "csrf": csrf, "role": "auditor"})
            return
        if not self._authenticated():
            return
        if self.path != "/records" or not isinstance(body.get("payload"), dict):
            self._respond(400, {"error": "invalid_record"})
            return
        self.server.serial += 1
        record = {"id": self.server.serial, "name": body["name"], "payload": body["payload"], "state": "draft"}
        self.server.records[record["id"]] = record
        self._respond(201, record)

    def do_GET(self):
        if not self._authenticated():
            return
        parsed = urlsplit(self.path)
        if parsed.path == "/records":
            identity = int(parse_qs(parsed.query)["id"][0])
            self._respond(200, {"items": [self.server.records[identity]] if identity in self.server.records else []})
            return
        identity = int(parsed.path.rsplit("/", 1)[1])
        self._respond(200, self.server.records[identity]) if identity in self.server.records else self._respond(404, {"error": "not_found"})

    def do_PATCH(self):
        body = self._body()
        if not self._authenticated():
            return
        identity = int(urlsplit(self.path).path.rsplit("/", 1)[1])
        if identity not in self.server.records:
            self._respond(404, {"error": "not_found"})
            return
        self.server.records[identity].update(body)
        self._respond(200, self.server.records[identity])

    def do_DELETE(self):
        if not self._authenticated():
            return
        identity = int(urlsplit(self.path).path.rsplit("/", 1)[1])
        if not self.server.retain_on_delete:
            self.server.records.pop(identity, None)
        self._respond(self.server.delete_status)


class ApiReviewCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="autoskill-api-review-")
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.workspace), "AUTOSKILL_HEADED": "0",
                                                   "AUTOSKILL_CASE_SKILL": str(self.workspace / "case-plan"),
                                                   "AUTOSKILL_SESSION": "api-independent-review", "NO_PROXY": "127.0.0.1,localhost"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        db.init()
        self.pid = "api-independent-review"

    def create_project(self, url="http://127.0.0.1:17891"):
        projects.create("独立接口审查", url, project_id=self.pid)
        projects.sync_requirement_records(self.pid, [{"id": REQ, "module": "记录", "title": "本次记录完整生命周期", "source": "local-fixture-contract-v1"}])


class CookieContractTests(ApiReviewCase):
    def setUp(self):
        super().setUp()
        self.create_project()

    def test_cookie_templates_validate_and_save_without_literal_tokens(self):
        definitions = lifecycle()[:2]
        for case in definitions:
            self.assertEqual([], store.credential_errors(case))
        self.assertEqual("session=fresh-token; csrf=fresh-csrf", api.fill(definitions[1]["headers"], {
            "session_token": "fresh-token", "csrf_token": "fresh-csrf"})["Cookie"])
        projects.save_cases(self.pid, definitions, expected_hashes={case["id"]: None for case in definitions})
        self.assertEqual(definitions, projects.cases(self.pid))
        for cookie in ("session=literal", "session={{session_token}}; csrf=literal"):
            with self.subTest(cookie=cookie):
                self.assertTrue(store.credential_errors({"headers": {"Cookie": cookie}}))

    def test_compiler_correlates_json_token_used_as_cookie(self):
        compiler = Compiler(self.pid, {"secrets": []}, None, None)
        login = {"method": "POST", "path": "/login", "params": {}, "headers": {"content-type": "application/json"},
                 "json": {"user": "reviewer"}, "form": None, "body_note": "", "status": 200, "mime": "application/json",
                 "response": {"token": "synthetic-captured-token", "role": "auditor"}, "cookies": {}}
        compiler.add_business(login, "独立审查", {})
        compiler.add_business({**login, "method": "GET", "path": "/records", "json": None,
                               "headers": {"cookie": "session=synthetic-captured-token"}, "response": {"items": []}}, "独立审查", {})
        producer, consumer = compiler.cases
        self.assertIn(producer["id"], consumer["extensions"].get("depends_on", []))
        extracted = producer["extensions"].get("extract", {})
        token_name = next((key for key, pointer in extracted.items() if pointer == "/token"), None)
        self.assertIsNotNone(token_name)
        self.assertEqual("session=fresh-token", api.fill(consumer["headers"], {token_name: "fresh-token"}).get("Cookie"))
        self.assertNotIn("synthetic-captured-token", json.dumps(compiler.cases))
        self.assertEqual([], store.credential_errors(consumer))

    def test_cookie_jar_source_and_ambiguous_json_sources_are_distinguished(self):
        base = {"method": "POST", "path": "/login", "params": {}, "headers": {}, "json": None, "form": None,
                "body_note": "", "status": 200, "mime": "application/json", "response": {"role": "auditor"}, "cookies": {}}
        with self.subTest(source="set_cookie"):
            compiler = Compiler(self.pid, {"secrets": []}, None, None)
            compiler.add_business({**base, "cookies": {"session": "synthetic-jar-cookie"}}, "独立审查", {})
            compiler.add_business({**base, "method": "GET", "path": "/records", "headers": {"cookie": "session=synthetic-jar-cookie"},
                                   "response": {"items": []}}, "独立审查", {})
            producer, consumer = compiler.cases
            self.assertIn(producer["id"], consumer["extensions"].get("depends_on", []))
            self.assertNotIn("Cookie", consumer.get("headers", {}))
            self.assertFalse(consumer["extensions"].get("execution", {}).get("merge_cookies"))
        with self.subTest(source="ambiguous_json"):
            compiler = Compiler(self.pid, {"secrets": []}, None, None)
            for path in ("/first-login", "/second-login"):
                compiler.add_business({**base, "path": path, "response": {"token": "synthetic-shared-value", "role": "auditor"}}, "独立审查", {})
            compiler.add_business({**base, "method": "GET", "path": "/records", "headers": {"cookie": "session=synthetic-shared-value"},
                                   "response": {"items": []}}, "独立审查", {})
            consumer = compiler.cases[-1]
            producer_ids = {case["id"] for case in compiler.cases[:-1]}
            self.assertFalse(producer_ids & set(consumer["extensions"].get("depends_on", [])))
            self.assertTrue(consumer["extensions"].get("execution", {}).get("unresolved_recorded_credentials"))
            self.assertTrue(compiler.notes)
            self.assertNotIn("synthetic-shared-value", json.dumps(compiler.cases))

    def test_status_201_alone_has_no_complete_business_verification(self):
        case = api_case("status-only", "DELETE", "/records/1", {"status": 201})
        outcome = {"status": "Pass", "category": "", "assertions": [{"name": "状态码", "passed": True, "expected": 201, "actual": 201}]}
        self.assertEqual("Unverified", runner.verification(case, outcome)["business_status"])


class LifecycleContractTests(ApiReviewCase):
    def setUp(self):
        super().setUp()
        self.server = LifecycleServer()
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.create_project(f"http://127.0.0.1:{self.server.server_port}")

    def execute_candidates(self, cases, inputs=None, selected=None):
        prepared = runner.create(self.pid, selected or [case["id"] for case in cases], candidate_cases=cases,
                                 candidate_auth={"api_case": ORDER[0]}, api_only=True, inputs=inputs or INPUT_A, headed=False)
        runner.execute(prepared["id"])
        run = db.one("SELECT * FROM runs WHERE id = ?", (prepared["id"],))
        stored_rows = db.rows("SELECT * FROM results WHERE run_id = ? ORDER BY id", (run["id"],))
        rows = {row["case_id"]: row for row in stored_rows}
        self.assertEqual(len(stored_rows), len(rows), "Each case must have exactly one final result")
        EVIDENCE.append({"test": self.id(), "run_id": run["id"], "status": run["status"], "input_hash": run["input_hash"],
                         "result_storage_order": list(rows), "results": {key: {"status": value["status"], "business": json.loads(value["detail"]).get("business_status"),
                                                                              "evidence_valid": execution_inputs.result_evidence(run, value)} for key, value in rows.items()},
                         "attempt_timings": self.attempt_timings(rows), "http_events": copy.deepcopy(self.server.events)})
        return run, rows

    @staticmethod
    def attempt_timings(rows):
        timings = []
        for row in rows.values():
            for reference in json.loads(row["detail"])["attempts"]:
                receipt = json.loads(data_refs.resolve(reference["path"]).read_bytes())
                timings.append({key: receipt[key] for key in ("id", "case_id", "run_id", "kind", "state", "started_at", "finished_at")})
        return sorted(timings, key=lambda item: (item["started_at"], item["id"]))

    def assert_lifecycle_execution_order(self, rows, events):
        self.assertEqual(set(ORDER), set(rows))
        self.assertEqual(len(ORDER), len(rows))
        self.assertEqual(7, len(events))
        resource_path = events[2]["path"]
        self.assertRegex(resource_path, r"^/records/[1-9][0-9]*$")
        identity = resource_path.rsplit("/", 1)[1]
        expected = [("POST", "/login"), ("POST", "/records"), ("GET", resource_path), ("PATCH", resource_path),
                    ("GET", resource_path), ("DELETE", resource_path), ("GET", "/records?id=" + identity)]
        self.assertEqual(expected, [(event["method"], event["path"]) for event in events])
        timings = self.attempt_timings(rows)
        deletion = next(item for item in timings if item["case_id"] == ORDER[5] and item["kind"] == "case")
        absence = next(item for item in timings if item["case_id"] == ORDER[6] and item["kind"] == "case")
        derived = next(item for item in timings if item["case_id"] == ORDER[5] and item["kind"] == "postcondition")
        self.assertLessEqual(deletion["finished_at"], absence["started_at"])
        self.assertLessEqual(absence["finished_at"], derived["started_at"])
        self.assertTrue(all(item["state"] == "completed" for item in (deletion, absence, derived)))

    def publish(self, cases, run_ids, repetitions=1):
        return candidate_publication.publish(self.pid, cases, run_ids, repetitions=repetitions,
                                             expected_hashes={case["id"]: None for case in cases}, recording_observations=False)

    def test_real_lifecycle_publishes_and_reuses_same_order(self):
        cases = lifecycle()
        run, rows = self.execute_candidates(cases)
        self.assertEqual("Pass", run["status"])
        self.assert_lifecycle_execution_order(rows, self.server.events)
        self.assertEqual({}, self.server.records)
        self.assertTrue(all(execution_inputs.result_evidence(run, row) for row in rows.values()))
        receipt = self.publish(cases, [run["id"]])
        self.assertEqual(set(ORDER), set(receipt["published_ids"]), receipt)
        self.assertEqual(ORDER, [case["id"] for case in projects.cases(self.pid)])
        projects.set_auth(self.pid, {"api_case": ORDER[0]})
        reused_start = len(self.server.events)
        reused = runner.create(self.pid, api_only=True, inputs=INPUT_B, headed=False)
        runner.execute(reused["id"])
        reused_run = db.one("SELECT * FROM runs WHERE id = ?", (reused["id"],))
        reused_rows = db.rows("SELECT * FROM results WHERE run_id = ? ORDER BY id", (reused["id"],))
        self.assertEqual("Pass", reused_run["status"])
        self.assertEqual(len(ORDER), len(reused_rows))
        self.assert_lifecycle_execution_order({row["case_id"]: row for row in reused_rows}, self.server.events[reused_start:])
        self.assertEqual(2, self.server.login_count)
        self.assertEqual({}, self.server.records)
        self.assertNotEqual(run["input_hash"], reused_run["input_hash"])
        self.assertTrue(all(execution_inputs.result_evidence(reused_run, row) for row in reused_rows))
        deletion_row = next(row for row in reused_rows if row["case_id"] == ORDER[5])
        deletion_case = projects.case(self.pid, ORDER[5])
        before = execution_inputs.capture(self.pid, None, "playwright", api_only=True, inputs=INPUT_B, persist=False)
        self.assertTrue(execution_inputs.result_current(self.pid, deletion_case, deletion_row, current=before))
        changed_postcondition = projects.case(self.pid, ORDER[6])
        changed_postcondition["assertions"]["json_equals"]["/items"] = [{"state": "new-business-expectation"}]
        projects.save_cases(self.pid, [changed_postcondition])
        after = execution_inputs.capture(self.pid, None, "playwright", api_only=True, inputs=INPUT_B, persist=False)
        self.assertFalse(execution_inputs.result_current(self.pid, deletion_case, deletion_row, current=after))
        self.assertTrue(execution_inputs.result_evidence(reused_run, deletion_row, deletion_case))
        EVIDENCE.append({"test": self.id(), "run_id": reused_run["id"], "reused_result_storage_order": [row["case_id"] for row in reused_rows],
                         "attempt_timings": self.attempt_timings({row["case_id"]: row for row in reused_rows}),
                         "postcondition_edit": {"current_before": True, "current_after": False, "historical_evidence_valid": True},
                         "http_events": copy.deepcopy(self.server.events)})

    def test_delete_201_is_verified_by_declared_real_postcondition(self):
        self.server.delete_status = 201
        cases = lifecycle(201)
        run, rows = self.execute_candidates(cases, selected=[ORDER[5]])
        self.assert_lifecycle_execution_order(rows, self.server.events)
        self.assertEqual("Pass", run["status"])
        self.assertEqual(set(ORDER), set(self.publish(cases, [run["id"]])["published_ids"]))
        self.assertEqual({}, self.server.records)

    def test_delete_success_response_with_failed_absence_is_not_published(self):
        self.server.retain_on_delete = True
        cases = lifecycle()
        run, rows = self.execute_candidates(cases)
        self.assertEqual("Fail", rows[ORDER[6]]["status"])
        self.assertEqual({"/items": []}, cases[-1]["assertions"]["json_equals"])
        receipt = self.publish(cases, [run["id"]])
        self.assertNotIn(ORDER[5], receipt["published_ids"], receipt)
        self.assertNotIn(ORDER[6], receipt["published_ids"], receipt)
        self.assertIn(ORDER[5], receipt["unpublished"])

    def test_unexecuted_postcondition_cannot_publish_successful_delete(self):
        cases = lifecycle()
        cases[-1]["params"]["id"] = "{{missing_identity}}"
        run, rows = self.execute_candidates(cases)
        self.assertEqual("Blocked", rows[ORDER[6]]["status"])
        receipt = self.publish(cases, [run["id"]])
        self.assertNotIn(ORDER[5], receipt["published_ids"], receipt)
        self.assertFalse(any(event["path"].startswith("/records?") for event in self.server.events))

    def test_missing_dependency_is_rejected_before_http_mutation(self):
        cases = lifecycle()
        cases[1]["extensions"]["depends_on"] = ["missing-login"]
        with self.assertRaises(ValueError):
            runner.prepare(self.pid, [ORDER[5]], candidate_cases=cases, api_only=True, inputs=INPUT_A)
        self.assertEqual([], self.server.events)
        self.assertEqual([], projects.cases(self.pid))

    def test_postcondition_requires_independent_oracle_and_action_dependency(self):
        variants = {
            "observation": lambda cases: cases[-1]["extensions"].update(oracle={"source": "observation"}),
            "unrelated": lambda cases: cases[-1]["extensions"].update(depends_on=[ORDER[1]]),
            "missing": lambda cases: cases[-2]["extensions"].update(postconditions=["missing-check"]),
            "empty": lambda cases: cases[-2]["extensions"].update(postconditions=[]),
        }
        for label, change in variants.items():
            with self.subTest(variant=label):
                cases = lifecycle()
                change(cases)
                with self.assertRaises(ValueError):
                    runner.prepare(self.pid, [ORDER[5]], candidate_cases=cases, api_only=True, inputs=INPUT_A)
        self.assertEqual([], self.server.events)
        self.assertEqual([], projects.cases(self.pid))

    def test_different_inputs_do_not_merge_pass_or_current_scope(self):
        cases = lifecycle()
        first, first_rows = self.execute_candidates(cases, INPUT_A)
        second, second_rows = self.execute_candidates(cases, INPUT_B)
        self.assertEqual("Pass", first["status"])
        self.assertEqual("Pass", second["status"])
        self.assertTrue(all(execution_inputs.result_evidence(first, row) for row in first_rows.values()))
        self.assertTrue(all(execution_inputs.result_evidence(second, row) for row in second_rows.values()))
        current_a = execution_inputs.capture(self.pid, None, "playwright", candidate_cases=cases,
                                             candidate_auth={"api_case": ORDER[0]}, api_only=True, inputs=INPUT_A, persist=False)
        current_b = execution_inputs.capture(self.pid, None, "playwright", candidate_cases=cases,
                                             candidate_auth={"api_case": ORDER[0]}, api_only=True, inputs=INPUT_B, persist=False)
        for case in cases:
            with self.subTest(case=case["id"]):
                row = first_rows[case["id"]]
                self.assertTrue(execution_inputs.result_current(self.pid, case, row, current=current_a))
                self.assertFalse(execution_inputs.result_current(self.pid, case, row, current=current_b))
        receipt = self.publish(cases, [first["id"], second["id"]], repetitions=2)
        self.assertEqual([], receipt["published_ids"], receipt)
        self.assertEqual([], projects.cases(self.pid))


def main() -> int:
    parser = argparse.ArgumentParser(description="运行隔离本地HTTP接口审查契约")
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("names", nargs="*")
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.loadTestsFromNames(args.names, module=sys.modules[__name__]) if args.names else unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if args.json_output:
        summary = {"tests_run": result.testsRun, "ok": result.wasSuccessful(), "failures": [{"test": test.id(), "traceback": trace} for test, trace in result.failures],
                   "errors": [{"test": test.id(), "traceback": trace} for test, trace in result.errors],
                   "skipped": [{"test": test.id(), "reason": reason} for test, reason in result.skipped], "executions": EVIDENCE}
        args.json_output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
