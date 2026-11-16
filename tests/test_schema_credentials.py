"""Response schema field definitions are not credential values or substitutes for business checks."""
import copy
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import test_workflow as fixture

from autoskill import builder, db, execution_inputs, projects, runner, secrets, workflow
from autoskill.core import store

TOKEN_SCHEMA = {"type": "object", "required": ["token"], "properties": {"token": {"type": "string", "minLength": 1}}}


class LoginChain(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return

    def respond(self, status, value):
        raw = json.dumps(value).encode() if status != 204 else b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(("POST", self.path))
        if self.path == "/login":
            if value.get("password") != "synthetic-local-password":
                return self.respond(403, {"error": "invalid login"})
            return self.respond(200, {"token": "fresh-api-token"})
        if self.headers.get("Authorization") != "Bearer fresh-api-token":
            return self.respond(401, {"error": "missing login"})
        self.server.value = value["value"]
        return self.respond(201, {"id": "91", "created": True})

    def do_GET(self):
        self.server.requests.append(("GET", self.path))
        if self.headers.get("Authorization") != "Bearer fresh-api-token":
            return self.respond(401, {"error": "missing login"})
        return self.respond(200, {"total_cents": 101} if self.path.endswith("/price") else {"value": self.server.value})

    def do_DELETE(self):
        self.server.requests.append(("DELETE", self.path))
        self.respond(204, None)


class SchemaCredentialTests(unittest.TestCase):
    setUp = fixture.WorkflowTests.setUp
    tearDownClass = classmethod(fixture.WorkflowTests.tearDownClass.__func__)
    key = fixture.WorkflowTests.key
    create = fixture.WorkflowTests.create
    claim = fixture.WorkflowTests.claim
    submit = fixture.WorkflowTests.submit
    to_materialize = fixture.WorkflowTests.to_materialize

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), LoginChain)
        cls.server.requests = []
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

    def test_response_schema_names_and_extract_pointers_are_not_values(self):
        case = builder.api_case("API-LOGIN", "接口登录", "POST", "/login", {"status": 200, "schema": copy.deepcopy(TOKEN_SCHEMA)},
                                json={"password": "${ENV:TEST_PASSWORD}"}, extract={"token": "/token"})
        case["assertions"]["schema"]["properties"].update(
            password={"type": "string"}, secret={"type": "string"}, authorization={"type": "string"})
        self.assertEqual(store.credential_errors(case, case["id"]), [])
        for field, value in (("json", {"password": "synthetic-hardcoded-value"}),
                             ("headers", {"Authorization": "Bearer synthetic-hardcoded-value"}),
                             ("json", {"assertions": {"schema": {"token": "synthetic-hardcoded-value"}}}),
                             ("form", {"client_secret": "synthetic-hardcoded-value"})):
            altered = copy.deepcopy(case)
            altered[field] = value
            with self.subTest(field=field, keys=list(value)):
                errors = store.credential_errors(altered, altered["id"])
                self.assertTrue(errors)
                self.assertTrue(all(error.startswith("API-LOGIN." + field) for error in errors))
        case["assertions"]["json_equals"] = {"/token": "synthetic-hardcoded-value"}
        self.assertIn("assertions.json_equals./token", store.credential_errors(case, case["id"])[0])

    def test_actual_five_api_chain_publishes_four_passed_cases_and_keeps_price_failure(self):
        secrets.set_secret("TEST_PASSWORD", "synthetic-local-password")
        payload = {"quantity": 2, "enabled": True}
        auth = {"Authorization": "Bearer {{token}}"}
        login = builder.api_case("TC-API-001", "接口登录", "POST", "/login", {"status": 200, "schema": TOKEN_SCHEMA},
                                 json={"password": "${ENV:TEST_PASSWORD}"}, extract={"token": "/token"})
        create = builder.api_case("TC-API-002", "创建记录", "POST", "/items", {"status": 201, "json_equals": {"/created": True}},
                                  headers=auth, json={"value": "{{payload}}"}, extract={"id": "/id"}, depends_on=[login["id"]])
        price = builder.api_case("TC-API-003", "按原规则计价", "GET", "/items/{{id}}/price",
                                 {"status": 200, "json_equals": {"/total_cents": 100}}, headers=auth, depends_on=[create["id"]])
        read = builder.api_case("TC-API-004", "读回记录", "GET", "/items/{{id}}",
                                {"status": 200, "json_equals": {"/value": "{{payload}}"}}, headers=auth, depends_on=[create["id"]])
        delete = builder.api_case("TC-API-005", "清理本次记录", "DELETE", "/items/{{id}}", {"status": 204}, headers=auth, depends_on=[read["id"]])
        cases = [login, create, price, read, delete]
        identity = self.create(validation={"inputs": {"payload": payload}})
        self.submit(identity, self.claim(identity), {"requirements": [{"id": "r1", "text": "登录可造数读回清理；记录计价应为100。", "source_ids": ["s1"]}]})
        self.submit(identity, self.claim(identity), {
            "scenarios": [{"id": "s" + case["id"], "title": case["title"], "requirement_ids": ["r1"], "oracle_ids": ["o" + case["id"]]} for case in cases],
            "oracles": [{"id": "o" + case["id"], "kind": "api", "requirement_ids": ["r1"], "checks": case["assertions"]} for case in cases]})
        self.submit(identity, self.claim(identity), {"cases": cases, "bindings": {"s" + case["id"]: [case["id"]] for case in cases}})
        self.assertIsNone(self.claim(identity))
        item = next(item for item in workflow.status(identity)["items"] if item["phase"] == "execute")
        before = len(self.server.requests)
        run = runner.execute(item["run_id"])
        self.assertEqual((run["status"], run["passed"], run["failed"]), ("Fail", 4, 1))
        task = self.claim(identity)
        receipt = task["publication_receipt"]
        self.assertTrue(receipt["complete"])
        self.assertEqual(set(receipt["published_ids"]), {case["id"] for case in (login, create, read, delete)})
        self.assertEqual(set(receipt["unpublished"]), {price["id"]})
        self.assertEqual(len(self.server.requests[before:]), 5)
        self.assertEqual(self.server.requests[before:].count(("POST", "/login")), 1)
        frozen = execution_inputs.load(run)["cases"]
        for case in projects.cases(self.project):
            self.assertEqual(case, frozen[case["id"]])
        failed = db.one("SELECT * FROM results WHERE run_id = ? AND case_id = ?", (run["id"], price["id"]))
        self.assertEqual(failed["status"], "Fail")
        self.assertIn("100", failed["reason"])
        self.assertIn("101", failed["reason"])
        self.assertTrue(execution_inputs.result_evidence(run, failed, frozen[price["id"]]))

    def test_invalid_api_oracles_are_rejected_at_materialize_with_case_location(self):
        for assertions in ({"status": 200, "schema": True}, {"status": 200, "schema": {"type": "object"}}):
            with self.subTest(assertions=assertions):
                identity = self.create()
                task = self.to_materialize(identity, rules=assertions)
                case = builder.api_case("API-NEEDS-CHECK", "需要业务检查", "GET", "/items", assertions)
                with self.assertRaises(workflow.WorkflowError) as caught:
                    self.submit(identity, task, {"cases": [case], "bindings": {"sc1": [case["id"]]}})
                self.assertEqual(caught.exception.details["case_id"], case["id"])
                self.assertEqual(caught.exception.details["field"], "assertions")
                self.assertIn(case["id"] + ".assertions", str(caught.exception))
                self.assertIn("重新提交当前 materialize", str(caught.exception))
                self.assertEqual(db.rows("SELECT id FROM runs WHERE session_id = ?", (identity,)), [])
                current = workflow.status(identity)
                self.assertFalse(any(item["phase"] == "execute" for item in current["items"]))
                self.assertEqual(next(item for item in current["items"] if item["id"] == task["id"])["state"], "leased")


if __name__ == "__main__":
    unittest.main()
