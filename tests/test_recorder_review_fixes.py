"""Regression coverage for request semantics and recording contracts found in code review."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qsl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

import httpx

from autoskill import projects
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
                patch.object(session, "PageProbe") as probe, patch.object(ui.time, "monotonic", side_effect=[0, 1, 2, 9]):
            probe.return_value.count.return_value = 1
            probe.return_value.read.return_value = True
            with self.assertRaisesRegex(session.RecordError, "断言现在不成立"):
                session.check("hidden", "#visible", None)
        self.assertEqual(rec["steps"], [])


if __name__ == "__main__":
    unittest.main()
