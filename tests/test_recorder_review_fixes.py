"""Regression coverage for request semantics and recording contracts found in code review."""
import json
import os
import sys
import tempfile
import unittest
from itertools import count
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qsl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

import httpx

from autoskill import projects, runner
from autoskill.recorder import compile as compiler
from autoskill.recorder import session
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
        compiler.build(self.pid, rec, {"log": {"entries": entries}}, state, "录制回归")
        return [case for case in projects.cases(self.pid) if case["kind"] == "api"]

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
                        patch.object(session, "PageProbe") as probe, patch.object(session, "_describe") as describe:
                    probe.return_value.count.return_value = 0
                    result = session.check("hidden", target, None)
                    self.assertEqual((result["recorded"], result["actual"]), (1, "不可见"))
                    self.assertEqual(rec["steps"], [{"assertion": "hidden", "locators": [locator]}])
                    describe.assert_not_called()

    def test_hidden_does_not_record_a_visible_element(self):
        rec = {"project_id": self.pid, "steps": [], "refs": {}}
        with patch.object(session, "current", return_value=rec), patch.object(session, "_sync_tabs"), \
                patch.object(session, "PageProbe") as probe, patch.object(ui.time, "monotonic", side_effect=count(step=10)):
            probe.return_value.count.return_value = 1
            probe.return_value.read.return_value = True
            with self.assertRaisesRegex(session.RecordError, "断言现在不成立"):
                session.check("hidden", "#visible", None)
        self.assertEqual(rec["steps"], [])

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
        self.assertEqual(len(self.compile([entry("PATCH", "/thing", {"ok": True}, body={"name": "new"})])), 3)

    def cookie_traffic(self):
        login = entry("POST", "/api/login", {"success": True}, body={"username": "qa", "password": "example"})
        login["response"]["headers"] = [{"name": "Set-Cookie", "value": "sid=recorded; Path=/; HttpOnly"}]
        return [entry("GET", "/api/status", {"logged_in": False}, headers={"Cookie": "visitor=anonymous"}),
                login, entry("GET", "/api/me", {"role": "admin"}, headers={"Cookie": "sid=recorded"})]

    def test_cookie_login_dependencies_keep_authenticated_and_anonymous_requests_separate(self):
        cases = self.compile(self.cookie_traffic(), as_login=True)
        before, login, protected = cases
        self.assertEqual(projects.depends_on(before), [])
        self.assertEqual(projects.depends_on(protected), [login["id"]])
        self.assertEqual(projects.auth(self.pid)["api_case"], login["id"])
        self.assertNotIn("sid=recorded", json.dumps(cases))

        def handle(request):
            if request.url.path == "/api/login":
                return httpx.Response(200, json={"success": True}, headers={"set-cookie": "sid=replayed; Path=/"})
            if request.url.path == "/api/status":
                return httpx.Response(200, json={"logged_in": bool(request.headers.get("cookie"))})
            self.assertEqual(request.headers.get("cookie"), "sid=replayed")
            return httpx.Response(200, json={"role": "admin"})

        with patch.object(api, "new_client", side_effect=lambda *args: httpx.Client(transport=httpx.MockTransport(handle))):
            running = runner.Session({"project_id": self.pid, "id": "cookie-test", "engine": "playwright"})
            try:
                for case in cases:
                    self.assertEqual(running.run_one(case)["status"], "Pass")
                self.assertEqual(running.run_one(before)["status"], "Pass")
            finally:
                running.close()

    def test_document_login_cookies_depend_on_the_ui_login(self):
        login = entry("POST", "/login", {"ok": True}, form="username=qa")
        login["_resourceType"] = "document"
        login["response"]["cookies"] = [{"name": "sid", "value": "recorded"}]
        cases = self.compile([login, entry("GET", "/api/me", {"role": "admin"}, headers={"Cookie": "sid=recorded"})],
                             as_login=True)
        self.assertEqual(len(cases), 1)
        self.assertEqual(projects.depends_on(cases[0]), [projects.auth(self.pid)["ui_case"]])

    def test_reusing_an_older_cookie_case_restores_its_login_dependency(self):
        cases = self.compile(self.cookie_traffic(), as_login=True)
        cases[-1]["extensions"].pop("depends_on")
        projects.save_cases(self.pid, [cases[-1]])
        updated = self.compile(self.cookie_traffic(), as_login=True)
        self.assertEqual(len(updated), 3)
        self.assertEqual(projects.depends_on(updated[-1]), [updated[1]["id"]])


if __name__ == "__main__":
    unittest.main()
