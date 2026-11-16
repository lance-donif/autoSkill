"""Regression coverage for request semantics and recording contracts found in code review."""
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import count
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qsl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

import httpx

from autoskill import projects, runner, secrets
from autoskill.recorder import compile as compiler
from autoskill.recorder import har, session
from autoskill.replay import api, ui

BASE = "https://example.test"


def entry(method, path, response, *, body=None, form=None, headers=None, content_type="application/json"):
    request = {"method": method, "url": BASE + path,
               "headers": [{"name": key, "value": value} for key, value in (headers or {}).items()]}
    if body is not None:
        request["postData"] = {"mimeType": content_type, "text": json.dumps(body)}
    if form is not None:
        request["postData"] = {"mimeType": "application/x-www-form-urlencoded", "text": form}
    return {"_resourceType": "fetch", "request": request,
            "response": {"status": 200, "content": {"mimeType": "application/json", "text": json.dumps(response)}}}


class RecorderReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": self.tmp.name, "AUTOSKILL_HEADED": "0"})
        environment.start()
        self.addCleanup(environment.stop)
        self.pid = projects.create("录制回归", BASE, project_id="t")["id"]

    def compile(self, entries, *, state=None, **extra):
        rec = {"project_id": self.pid, "start_url": "/", "steps": [{"action": "click", "selector": "#submit"}],
               "secrets": [], "used_login": False, "as_login": False, **extra}
        existing = projects.cases(self.pid)
        self.summary = compiler.build(self.pid, rec, {"log": {"entries": entries}}, state, "录制回归")
        self.assertEqual(projects.cases(self.pid), existing)
        return [case for case in self.summary["candidates"] if case["kind"] == "api"]

    def test_repeated_query_and_form_values_reach_the_replayed_request(self):
        fields = "tag=red&tag=blue&tag=&single=one&empty="
        cases = self.compile([entry("GET", "/filter?" + fields, {"ok": True}),
                              entry("POST", "/filter", {"ok": True}, form=fields)])
        sent = []

        def handle(request):
            sent.append(parse_qsl(request.url.query.decode() if request.method == "GET" else request.content.decode(),
                                  keep_blank_values=True))
            return httpx.Response(200, json={"ok": True})

        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            for case in cases:
                values = case["params"] if case["method"] == "GET" else case["form"]
                self.assertEqual(values, {"tag": ["red", "blue", ""], "single": "one", "empty": ""})
                self.assertEqual(api.run_case(client, case, projects.config(self.pid), {})["status"], "Pass")
        self.assertEqual(sent, [parse_qsl(fields, keep_blank_values=True)] * 2)

    def test_hidden_records_missing_elements_without_describing_them(self):
        for target, locator in (("#deleted", {"by": "css", "value": "#deleted"}),
                                ("testid=deleted", {"by": "testid", "value": "deleted"})):
            with self.subTest(target=target):
                rec = {"project_id": self.pid, "steps": [], "refs": {}}
                with patch.object(session, "current", return_value=rec), patch.object(session, "_sync_tabs"), \
                        patch.object(session, "PageProbe", autospec=True) as probe, patch.object(session, "_describe") as describe:
                    probe.return_value.count.return_value = 0
                    result = session.check("hidden", target, None)
                    self.assertEqual((result["recorded"], result["actual"]), (1, "不可见"))
                    self.assertEqual(rec["steps"][0]["assertion"], "hidden")
                    self.assertEqual(rec["steps"][0]["locators"], [locator])
                    self.assertTrue(rec["steps"][0]["recording_result"]["passed"])
                    describe.assert_not_called()

    def test_hidden_records_the_failed_expectation_for_a_visible_element(self):
        rec = {"project_id": self.pid, "steps": [], "refs": {}}
        with patch.object(session, "current", return_value=rec), patch.object(session, "_sync_tabs"), \
                patch.object(session, "PageProbe", autospec=True) as probe, patch.object(ui.time, "monotonic", side_effect=count(step=10)):
            probe.return_value.count.return_value = 1
            probe.return_value.read.return_value = True
            result = session.check("hidden", "#visible", None)
        self.assertFalse(result["passed"])
        self.assertEqual(rec["steps"][0]["assertion"], "hidden")
        self.assertEqual(rec["steps"][0]["recording_result"]["actual"], "可见")

    def test_json_media_types_are_kept_and_distinguish_reused_requests(self):
        media_types = ("application/merge-patch+json", "application/json-patch+json", "application/json")
        cases = self.compile([entry("PATCH", "/thing", {"ok": True}, body={"name": "new"},
                                    headers={"Content-Type": media_type}, content_type=media_type)
                              for media_type in media_types])
        self.assertEqual(len(cases), 3)
        sent = []

        def handle(request):
            sent.append(request.headers["content-type"])
            return httpx.Response(200, json={"ok": True})

        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            for case in cases:
                self.assertEqual(api.run_case(client, case, projects.config(self.pid), {})["status"], "Pass")
        self.assertEqual(sent, list(media_types))
        projects.save_cases(self.pid, cases)  # Existing imported definitions exercise request reuse.
        reused = self.compile([entry("PATCH", "/thing", {"ok": True}, body={"name": "new"})])
        self.assertEqual(len(reused), 1)
        self.assertEqual(reused[0]["id"], cases[-1]["id"])
        self.assertEqual(len(projects.cases(self.pid)), 3)

    def cookie_traffic(self):
        login = entry("POST", "/api/login", {"success": True}, body={"username": "qa", "password": "example"})
        login["response"]["headers"] = [{"name": "Set-Cookie", "value": "sid=recorded; Path=/; HttpOnly"}]
        return [entry("GET", "/api/status", {"logged_in": False}, headers={"Cookie": "visitor=anonymous"}),
                login, entry("GET", "/api/me", {"role": "admin"}, headers={"Cookie": "sid=recorded"})]

    def test_cookie_login_dependencies_keep_authenticated_and_anonymous_requests_separate(self):
        secrets.set_secret("DECLARED_PASSWORD", "example")
        cases = self.compile(self.cookie_traffic(), as_login=True)
        before, login, protected = cases
        self.assertEqual(projects.depends_on(before), [])
        self.assertEqual(projects.depends_on(protected), [login["id"]])
        self.assertEqual(self.summary["candidate_auth"]["api_case"], login["id"])
        self.assertEqual(projects.auth(self.pid), {})
        self.assertNotIn("sid=recorded", json.dumps(cases))

        def handle(request):
            if request.url.path == "/api/login":
                return httpx.Response(200, json={"success": True}, headers={"set-cookie": "sid=replayed; Path=/"})
            if request.url.path == "/api/status":
                return httpx.Response(200, json={"logged_in": bool(request.headers.get("cookie"))})
            self.assertEqual(request.headers.get("cookie"), "sid=replayed")
            return httpx.Response(200, json={"role": "admin"})

        # A cookie with no captured producer cannot silently turn into an anonymous request.
        self.assertTrue(before["extensions"]["execution"]["unresolved_recorded_credentials"])
        self.assertTrue(any("Cookie visitor" in note for note in self.summary["notes"]))
        with self.assertRaisesRegex(ValueError, "临时凭据"):
            runner.prepare(self.pid, [before["id"]], candidate_cases=self.summary["candidates"],
                           candidate_auth=self.summary["candidate_auth"])
        with patch.object(api, "new_client", side_effect=lambda *args: httpx.Client(transport=httpx.MockTransport(handle))):
            running = runner.Session(runner.prepare(self.pid, [login["id"], protected["id"]],
                                                    candidate_cases=self.summary["candidates"], candidate_auth=self.summary["candidate_auth"]))
            try:
                for case in (login, protected):
                    self.assertEqual(running.run_one(case)["status"], "Pass")
            finally:
                running.close()

    def test_document_login_cookies_depend_on_the_ui_login(self):
        login = entry("POST", "/login", {"ok": True}, form="username=qa")
        login["_resourceType"] = "document"
        login["response"]["cookies"] = [{"name": "sid", "value": "recorded"}]
        cases = self.compile([login, entry("GET", "/api/me", {"role": "admin"}, headers={"Cookie": "sid=recorded"})],
                             as_login=True)
        self.assertEqual(len(cases), 1)
        self.assertEqual(projects.depends_on(cases[0]), [self.summary["candidate_auth"]["ui_case"]])

    def test_reusing_an_older_cookie_case_restores_its_login_dependency(self):
        cases = self.compile(self.cookie_traffic(), as_login=True)
        cases[-1]["extensions"].pop("depends_on")
        projects.save_cases(self.pid, [cases[-1]])
        updated = self.compile(self.cookie_traffic(), as_login=True)
        self.assertEqual(len(updated), 3)
        self.assertEqual(projects.depends_on(updated[-1]), [updated[1]["id"]])

    def assert_collision_replay(self, reverse=False, identity=1):
        producers = [entry("POST", "/api/orders", {"id": identity}, body={"title": "order"}),
                     entry("POST", "/api/customers", {"id": identity}, body={"name": "customer"})]
        cases = self.compile([*(reversed(producers) if reverse else producers),
                              entry("DELETE", "/api/customers/1", {"ok": True})])
        customer_case = next(case for case in cases if case["path"] == "/api/customers")
        deleting = cases[-1]
        self.assertEqual(projects.depends_on(deleting), [customer_case["id"]])
        self.assertEqual(customer_case["extensions"]["extract"], {"customer_id": "/id"})
        self.assertEqual(deleting["path"], "/api/customers/{{customer_id}}")
        customers = {101: "pre-existing customer"}
        deleted = []

        def handle(request):
            if request.url.path == "/api/orders":
                return httpx.Response(200, json={"id": "101" if isinstance(identity, str) else 101})
            if request.url.path == "/api/customers":
                customers[202] = "created customer"
                return httpx.Response(200, json={"id": "202" if isinstance(identity, str) else 202})
            target = int(request.url.path.rsplit("/", 1)[-1])
            deleted.append(target)
            del customers[target]
            return httpx.Response(200, json={"ok": True})

        variables = {}
        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            for case in cases:
                self.assertEqual(api.run_case(client, case, projects.config(self.pid), variables)["status"], "Pass")
        self.assertEqual(deleted, [202])
        self.assertEqual(customers, {101: "pre-existing customer"})

    def test_colliding_ids_target_the_matching_resource_during_replay(self):
        self.assert_collision_replay()

    def test_colliding_ids_do_not_depend_on_producer_order(self):
        self.assert_collision_replay(reverse=True)

    def test_short_string_ids_keep_resource_disambiguation_and_do_not_capture_page_numbers(self):
        self.assert_collision_replay(identity="1")
        for value in ("", "0", "x"):
            self.assertEqual(har.correlatable({"id": value}), [])
        self.assertEqual(har.correlatable({"page": "1", "code": "4", "key": "2", "title": "4"}), [])

    def test_short_string_id_completion_uses_fresh_http_created_record_and_leaves_old_record_untouched(self):
        created = entry("POST", "/api/todos", {"id": "4", "title": "fixture", "status": "ready"}, body={"title": "fixture"})
        created["response"]["status"] = 201
        completed = entry("POST", "/api/todos/4/complete", {"id": "4", "title": "fixture", "status": "done"})
        cases = self.compile([created, completed])
        self.assertEqual(cases[0]["extensions"]["extract"], {"todo_id": "/id"})
        self.assertEqual(cases[1]["path"], "/api/todos/{{todo_id}}/complete")
        self.assertEqual(projects.depends_on(cases[1]), [cases[0]["id"]])
        records, sent = {"4": "old"}, []

        class TodoSite(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                sent.append(self.path)
                identity = "91" if self.path == "/api/todos" else self.path.split("/")[-2]
                records[identity] = "ready" if self.path == "/api/todos" else "done"
                body = json.dumps({"id": identity, "title": "fixture", "status": records[identity]}).encode()
                self.send_response(201 if self.path == "/api/todos" else 200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        site = ThreadingHTTPServer(("127.0.0.1", 0), TodoSite)
        thread = threading.Thread(target=site.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{site.server_port}"
        config = {**projects.config(self.pid), "base_url": base, "allowed_origins": [base]}
        try:
            variables = {}
            with api.new_client(config) as client:
                for case in cases:
                    result = api.run_case(client, case, config, variables)
                    self.assertEqual(result["status"], "Pass", result)
            self.assertEqual(sent, ["/api/todos", "/api/todos/91/complete"])
            self.assertEqual(records, {"4": "old", "91": "done"})
        finally:
            site.shutdown()
            site.server_close()
            thread.join(timeout=3)

    def test_colliding_ids_in_request_fields_use_named_resources(self):
        cases = self.compile([entry("POST", "/api/orders", {"id": 1}, body={"title": "order"}),
                              entry("POST", "/api/customers", {"id": 1}, body={"name": "customer"}),
                              entry("POST", "/api/link?order_id=1&customerId=1", {"ok": True},
                                    body={"order_id": 1, "customerId": 1, "customer": {"id": 1}, "page": 1})])
        order, customer, linked = cases
        self.assertEqual(set(projects.depends_on(linked)), {order["id"], customer["id"]})
        self.assertEqual(linked["params"], {"order_id": "{{order_id}}", "customerId": "{{customer_id}}"})
        self.assertEqual(api.fill(linked["json"], {"order_id": 101, "customer_id": 202}),
                         {"order_id": 101, "customerId": 202, "customer": {"id": 202}, "page": 1})

    def test_nested_response_fields_disambiguate_equal_ids_from_one_endpoint(self):
        cases = self.compile([entry("GET", "/api/bootstrap", {"order": {"id": 7}, "customer": {"id": 7}}),
                              entry("DELETE", "/api/customers/7", {"ok": True}),
                              entry("DELETE", "/api/orders/7", {"ok": True})])
        producer, customer, order = cases
        variables = {name: 202 if pointer == "/customer/id" else 101
                     for name, pointer in producer["extensions"]["extract"].items()}
        self.assertEqual(set(producer["extensions"]["extract"].values()), {"/customer/id", "/order/id"})
        self.assertEqual(api.fill(customer["path"], variables), "/api/customers/202")
        self.assertEqual(api.fill(order["path"], variables), "/api/orders/101")

    def test_later_reads_keep_the_original_resource_producer(self):
        cases = self.compile([entry("POST", "/api/customers", {"id": 1}, body={"name": "customer"}),
                              entry("GET", "/api/customers", {"items": [{"id": 1}]}),
                              entry("POST", "/api/orders", {"id": 1}, body={"title": "order"}),
                              entry("DELETE", "/api/customers/1", {"ok": True})])
        self.assertEqual(projects.depends_on(cases[-1]), [cases[0]["id"]])

    def test_a_single_unrelated_id_does_not_override_explicit_resource_hints(self):
        cases = self.compile([entry("POST", "/api/orders", {"id": 1}, body={"title": "order"}),
                              entry("DELETE", "/api/customers/1", {"ok": True}),
                              entry("POST", "/api/link?customerId=1", {"ok": True}, body={"customer_id": 1})])
        self.assertEqual(cases[1]["path"], "/api/customers/1")
        self.assertEqual(cases[2]["params"], {"customerId": "1"})
        self.assertEqual(cases[2]["json"], {"customer_id": 1})
        self.assertEqual([projects.depends_on(case) for case in cases], [[], [], []])
        self.assertTrue(any("资源与已捕获来源不匹配" in note for note in self.summary["notes"]))

    def test_field_matching_does_not_override_a_conflicting_resource(self):
        cases = self.compile([entry("POST", "/api/orders", {"id": 1001}, body={"title": "order"}),
                              entry("POST", "/api/invoices", {"code": 1001}, body={"title": "invoice"}),
                              entry("DELETE", "/api/customers/1001", {"ok": True})])
        self.assertEqual(cases[-1]["path"], "/api/customers/1001")
        self.assertEqual(projects.depends_on(cases[-1]), [])
        self.assertTrue(any("资源与已捕获来源不匹配" in note for note in self.summary["notes"]))

    def test_ambiguous_ids_without_resource_hints_are_replayed_as_recorded(self):
        cases = self.compile([entry("POST", "/api/orders", {"id": 1}, body={"title": "order"}),
                              entry("POST", "/api/invoices", {"id": 1}, body={"title": "invoice"}),
                              entry("DELETE", "/1", {"ok": True})])
        self.assertEqual(cases[-1]["path"], "/1")
        self.assertEqual(projects.depends_on(cases[-1]), [])
        self.assertTrue(any("无法唯一关联" in note for note in self.summary["notes"]))
        sent = []

        def handle(request):
            sent.append(request.url.path)
            return httpx.Response(200, json={"ok": True})

        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            self.assertEqual(api.run_case(client, cases[-1], projects.config(self.pid), {})["status"], "Pass")
        self.assertEqual(sent, ["/1"])

    def test_tokens_still_correlate_across_resource_paths(self):
        token = "recorded-token-for-cross-resource-test"
        cases = self.compile([entry("POST", "/api/login", {"token": token}, body={"username": "qa"}),
                              entry("POST", "/api/customers", {"ok": True}, body={"token": token},
                                    headers={"Authorization": "Bearer " + token})], as_login=True)
        self.assertEqual(projects.depends_on(cases[-1]), [cases[0]["id"]])
        self.assertEqual(cases[-1]["json"], {"token": "{{token}}"})
        self.assertEqual(cases[-1]["headers"], {"Authorization": "Bearer {{token}}"})


if __name__ == "__main__":
    unittest.main()
