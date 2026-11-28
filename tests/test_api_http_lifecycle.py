"""Independent HTTP lifecycle oracles retain cold Cookie references and later business checks."""
import copy
import json
import os
import tempfile
import threading
import unittest
from http.cookiejar import Cookie
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from autoskill import builder, candidate_publication, data_refs, db, execution_inputs, projects, runner
from autoskill.core import store
from autoskill.replay import api


class LifecycleTarget(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return

    def answer(self, status, payload=None):
        body = b"" if payload is None else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        self.server.requests.append((self.command, self.path, self.headers.get("Cookie")))
        values = SimpleCookie(self.headers.get("Cookie", ""))
        return "token" in values and values["token"].value == self.server.token

    def body(self):
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or "{}")

    def do_POST(self):
        payload = self.body()
        if self.path == "/login":
            self.server.logins += 1
            self.server.token = "local-token-" + str(self.server.logins)
            return self.answer(200, {"token": self.server.token, "signed_in": True})
        if not self.authorized():
            return self.answer(401, {"error": "missing login"})
        self.server.next_id += 1
        identity = str(self.server.next_id)
        self.server.items[identity] = payload
        return self.answer(201, {"id": identity, "item": payload})

    def do_GET(self):
        if not self.authorized():
            return self.answer(401, {"error": "missing login"})
        return self.answer(200, {"item": self.server.items.get(self.path.rsplit("/", 1)[-1])})

    def do_PATCH(self):
        payload = self.body()
        if not self.authorized():
            return self.answer(401, {"error": "missing login"})
        identity = self.path.rsplit("/", 1)[-1]
        self.server.items[identity] = payload
        return self.answer(200, {"item": payload})

    def do_DELETE(self):
        if not self.authorized():
            return self.answer(401, {"error": "missing login"})
        if not self.server.ignore_delete:
            self.server.items.pop(self.path.rsplit("/", 1)[-1], None)
        if self.server.cancel_run:
            db.update("runs", self.server.cancel_run, {"cancel": 1})
        return self.answer(201)


class ApiHttpLifecycleTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": folder.name, "AUTOSKILL_SESSION": "http-lifecycle",
                                               "AUTOSKILL_HEADED": "0", "AUTOSKILL_BRIDGE": "off"})
        environment.start()
        self.addCleanup(environment.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), LifecycleTarget)
        self.server.items, self.server.requests = {}, []
        self.server.logins, self.server.next_id, self.server.token = 0, 0, ""
        self.server.ignore_delete = False
        self.server.cancel_run = ""
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.project = projects.create("接口生命周期", f"http://127.0.0.1:{self.server.server_port}", project_id="http-lifecycle")["id"]

    def cases(self):
        headers = {"Cookie": "token={{token}}"}
        login = builder.api_case("API-LOGIN", "登录返回会话", "POST", "/login",
                                 {"status": 200, "json_equals": {"/signed_in": True}}, extract={"token": "/token"})
        create = builder.api_case("API-CREATE", "创建本次记录", "POST", "/items",
                                  {"status": 201, "json_equals": {"/item": "{{payload}}"}},
                                  headers=headers, json="{{payload}}", extract={"id": "/id"}, depends_on=[login["id"]])
        read = builder.api_case("API-READ", "查询本次记录", "GET", "/items/{{id}}",
                                {"status": 200, "json_equals": {"/item": "{{payload}}"}}, headers=headers, depends_on=[create["id"]])
        update = builder.api_case("API-UPDATE", "修改本次记录", "PATCH", "/items/{{id}}",
                                  {"status": 200, "json_equals": {"/item": "{{updated}}"}},
                                  headers=headers, json="{{updated}}", depends_on=[read["id"]])
        read_updated = builder.api_case("API-UPDATED", "确认修改已保存", "GET", "/items/{{id}}",
                                        {"status": 200, "json_equals": {"/item": "{{updated}}"}}, headers=headers, depends_on=[update["id"]])
        delete = builder.api_case("API-DELETE", "删除本次记录", "DELETE", "/items/{{id}}", {"status": 201},
                                  headers=headers, depends_on=[read_updated["id"]])
        absent = builder.api_case("API-ABSENT", "确认本次记录已删除", "GET", "/items/{{id}}",
                                  {"status": 200, "json_equals": {"/item": None}}, headers=headers, depends_on=[delete["id"]])
        return [login, create, read, update, read_updated, delete, absent]

    def test_cookie_values_allow_only_complete_references(self):
        for value in ("token={{token}}", "token={{token}}; csrf=${ENV:CSRF}", "{{cookie}}"):
            with self.subTest(value=value):
                self.assertEqual(store.credential_errors({"headers": {"Cookie": value}}), [])
        for value in ("token=plaintext", "token={{token}}; other=plaintext", "token={{token}};", "prefix-{{token}}"):
            with self.subTest(value=value):
                self.assertTrue(store.credential_errors({"headers": {"Cookie": value}}))
        self.assertTrue(store.credential_errors({"json": {"cookie": "token={{token}}"}}))

    def test_real_cookie_login_create_and_read_keep_typed_inputs(self):
        cases = self.cases()[:3]
        task = candidate_publication.stage(self.project, cases, {}, {"fixture": "cookie-chain"})
        payload = {"number": 7, "enabled": False, "optional": None}
        queued = candidate_publication.queue(task["id"], inputs={"payload": payload}, start=False)
        self.assertEqual(len(queued["run_ids"]), 1)
        run = runner.execute(queued["run_id"])
        self.assertEqual(run["status"], "Pass")
        self.assertEqual(set(candidate_publication.receipt(run["id"])["published_ids"]), {case["id"] for case in cases})
        self.assertEqual(self.server.items["1"], payload)
        self.assertTrue(all(cookie == "token=local-token-1" for _, _, cookie in self.server.requests))
        for case in cases:
            saved = projects.case(self.project, case["id"])
            self.assertEqual(saved, execution_inputs.load(run)["cases"][case["id"]])
            row = db.one("SELECT * FROM results WHERE run_id = ? AND case_id = ?", (run["id"], case["id"]))
            self.assertTrue(execution_inputs.result_evidence(run, row, saved))

    def test_generated_cookie_merge_respects_url_scope_and_explicit_values(self):
        self.server.token = "chosen"
        case = builder.api_case("API-MIXED", "混合登录Cookie", "GET", "/items/missing",
                                {"status": 200, "json_equals": {"/item": None}}, headers={"Cookie": "token={{token}}"})
        case["extensions"]["execution"] = {"merge_cookies": True}
        with api.new_client(projects.config(self.project)) as client:
            for name, value, domain, path, secure in (("jar_only", "valid", "127.0.0.1", "/", False),
                                                       ("token", "stale", "127.0.0.1", "/", False),
                                                       ("foreign", "hidden", "example.invalid", "/", False),
                                                       ("wrong_path", "hidden", "127.0.0.1", "/other", False),
                                                       ("secure_only", "hidden", "127.0.0.1", "/", True)):
                client.cookies.jar.set_cookie(Cookie(0, name, value, None, False, domain, True, False,
                                                     path, True, secure, None, True, None, None, {}, False))
            result = api.run_case(client, case, projects.config(self.project), {"token": "chosen"})
            self.assertEqual(result["status"], "Pass")
            cookies = SimpleCookie(self.server.requests[-1][2])
            self.assertEqual({name: value.value for name, value in cookies.items()}, {"jar_only": "valid", "token": "chosen"})
            case["extensions"].pop("execution")
            result = api.run_case(client, case, projects.config(self.project), {"token": "chosen"})
            self.assertEqual(result["status"], "Pass")
            self.assertEqual(self.server.requests[-1][2], "token=chosen")

    def execute_lifecycle(self, *, include_postcondition=True, repetitions=1, selected=None):
        cases = self.cases()
        if include_postcondition:
            cases[-2]["extensions"]["postconditions"] = [cases[-1]["id"]]
        inputs = {"payload": {"name": "first", "number": 7, "enabled": False, "optional": None},
                  "updated": {"name": "updated", "number": 8, "enabled": True, "optional": None}}
        task = candidate_publication.stage(self.project, cases, {}, {"fixture": "full-lifecycle"})
        queued = candidate_publication.queue(task["id"], inputs=inputs, repetitions=repetitions, case_ids=selected, start=False)
        self.assertTrue(queued["run_ids"], queued)
        runs = [runner.execute(identity) for identity in queued["run_ids"]]
        return cases, queued, runs

    def test_complete_lifecycle_uses_later_empty_query_and_reuses_exact_cases(self):
        cases, queued, runs = self.execute_lifecycle(repetitions=2)
        self.assertEqual([run["status"] for run in runs], ["Pass", "Pass"])
        receipt = candidate_publication.receipt(queued["run_id"])
        self.assertEqual(set(receipt["published_ids"]), {case["id"] for case in cases}, receipt)
        self.assertEqual(self.server.items, {})
        self.assertEqual(self.server.logins, 2)
        for run in runs:
            row = db.one("SELECT * FROM results WHERE run_id = ? AND case_id = ?", (run["id"], "API-DELETE"))
            detail = json.loads(row["detail"])
            self.assertEqual(detail["business_status"], "Pass")
            self.assertEqual(detail["postcondition_source"]["business_status"], "Unverified")
            self.assertEqual(detail["postcondition_results"][0]["case_id"], "API-ABSENT")
            self.assertTrue(execution_inputs.result_evidence(run, row, cases[-2]))
            current = execution_inputs.capture(self.project, ["API-DELETE"], "playwright", api_only=True,
                                               inputs=execution_inputs.load(run)["inputs"], persist=False)
            self.assertTrue(execution_inputs.result_current(self.project, projects.case(self.project, "API-DELETE"), row, current=current))
            self.assertFalse(execution_inputs.result_current(self.project, projects.case(self.project, "API-DELETE"), row))
        before = len(self.server.requests)
        rerun = runner.create(self.project, case_ids=["API-DELETE"], api_only=True,
                              inputs=execution_inputs.load(runs[-1])["inputs"])
        rerun = runner.execute(rerun["id"])
        self.assertEqual((rerun["status"], rerun["total"], rerun["done"]), ("Pass", 1, 1))
        self.assertEqual(len(self.server.requests) - before, 6)
        after = db.one("SELECT * FROM results WHERE run_id = ? AND case_id = ?", (rerun["id"], "API-ABSENT"))
        self.assertEqual(after["prerequisite"], 0)
        self.assertEqual(self.server.items, {})
        for case in cases:
            self.assertEqual(projects.case(self.project, case["id"]), execution_inputs.load(runs[0])["cases"][case["id"]])

    def test_selected_candidate_counts_only_explicit_target_and_publishes_verified_closure(self):
        cases, queued, runs = self.execute_lifecycle(selected=["API-DELETE"])
        run = runs[0]
        self.assertEqual((run["status"], run["total"], run["done"]), ("Pass", 1, 1))
        self.assertEqual(execution_inputs.load(run)["selected"], ["API-DELETE"])
        self.assertEqual(len(db.rows("SELECT * FROM results WHERE run_id = ?", (run["id"],))), 7)
        self.assertEqual(set(candidate_publication.receipt(queued["run_id"])["published_ids"]), {case["id"] for case in cases})
        target = db.one("SELECT * FROM results WHERE run_id = ? AND case_id = ?", (run["id"], "API-ABSENT"))
        self.assertEqual(target["prerequisite"], 0)

    def test_successful_delete_response_with_failed_empty_query_never_publishes_action(self):
        self.server.ignore_delete = True
        cases, queued, runs = self.execute_lifecycle()
        self.assertEqual(runs[0]["status"], "Fail")
        receipt = candidate_publication.receipt(queued["run_id"])
        self.assertTrue({"API-DELETE", "API-ABSENT"}.issubset(receipt["unpublished"]))
        row = db.one("SELECT * FROM results WHERE run_id = ? AND case_id = ?", (runs[0]["id"], "API-DELETE"))
        detail = json.loads(row["detail"])
        self.assertEqual((row["status"], detail["business_status"]), ("Fail", "Fail"))
        self.assertEqual(detail["postcondition_source"]["response"]["status"], 201)
        self.assertEqual(detail["postcondition_results"][0]["case_id"], "API-ABSENT")
        self.assertTrue(execution_inputs.result_evidence(runs[0], row, cases[-2]))

    def test_cancelled_before_query_keeps_original_action_unverified(self):
        cases = self.cases()
        cases[-2]["extensions"]["postconditions"] = ["API-ABSENT"]
        task = candidate_publication.stage(self.project, cases, {}, {"fixture": "cancel-postcondition"})
        queued = candidate_publication.queue(task["id"], case_ids=["API-DELETE"], start=False,
                                             inputs={"payload": {"name": "first"}, "updated": {"name": "updated"}})
        self.server.cancel_run = queued["run_id"]
        run = runner.execute(queued["run_id"])
        self.assertEqual(run["status"], "Cancelled")
        self.assertEqual(projects.cases(self.project), [])
        self.assertEqual(candidate_publication.receipt(run["id"])["published_ids"], [])
        row = db.one("SELECT * FROM results WHERE run_id = ? AND case_id = ?", (run["id"], "API-DELETE"))
        detail = json.loads(row["detail"])
        self.assertEqual(detail["business_status"], "Unverified")
        self.assertEqual([attempt["kind"] for attempt in detail["attempts"]], ["case"])
        self.assertTrue(execution_inputs.result_evidence(run, row, cases[-2]))
        self.assertIsNone(db.one("SELECT * FROM results WHERE run_id = ? AND case_id = ?", (run["id"], "API-ABSENT")))

    def test_derived_result_requires_unchanged_same_run_target_evidence(self):
        cases, _, runs = self.execute_lifecycle(repetitions=2)
        run = runs[0]
        row = db.one("SELECT * FROM results WHERE run_id = ? AND case_id = ?", (run["id"], "API-DELETE"))
        altered = copy.deepcopy(row)
        detail = json.loads(altered["detail"])
        other = db.one("SELECT * FROM results WHERE run_id = ? AND case_id = ?", (runs[1]["id"], "API-ABSENT"))
        detail["postcondition_results"] = [other]
        altered["detail"] = json.dumps(detail)
        self.assertFalse(execution_inputs.result_evidence(run, altered, cases[-2]))
        target = db.one("SELECT * FROM results WHERE run_id = ? AND case_id = ?", (run["id"], "API-ABSENT"))
        target_attempt = json.loads(target["detail"])["attempts"][0]
        data_refs.resolve(target_attempt["path"]).unlink()
        self.assertFalse(execution_inputs.result_evidence(run, row, cases[-2]))
        receipt = candidate_publication.publish(self.project, cases, [run["id"]],
                                                expected_hashes={case["id"]: execution_inputs.content_hash(case) for case in cases})
        self.assertNotIn("API-DELETE", receipt["published_ids"])
        self.assertNotIn("API-ABSENT", receipt["published_ids"])

    def test_invalid_or_missing_postcondition_is_rejected_before_http(self):
        cases = self.cases()
        cases[-2]["extensions"]["postconditions"] = ["API-ABSENT"]
        for label in ("missing", "self", "empty", "duplicate", "unrelated"):
            altered = copy.deepcopy(cases)
            if label == "missing":
                altered.pop()
            elif label == "self":
                altered[-2]["extensions"]["postconditions"] = ["API-DELETE"]
            elif label == "empty":
                altered[-2]["extensions"]["postconditions"] = []
            elif label == "duplicate":
                altered[-2]["extensions"]["postconditions"] = ["API-ABSENT", "API-ABSENT"]
            else:
                altered[-1]["extensions"]["depends_on"] = ["API-CREATE"]
            with self.subTest(label=label), self.assertRaises(ValueError):
                runner.prepare(self.project, ["API-DELETE"], candidate_cases=altered, api_only=True)
        self.assertEqual(self.server.requests, [])
        self.assertEqual(projects.cases(self.project), [])


if __name__ == "__main__":
    unittest.main()
