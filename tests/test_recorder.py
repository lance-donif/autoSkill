"""Compiling a recording: what gets asserted, what becomes a variable, and what must never be written down."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))
__import__("os").environ.setdefault("AUTOSKILL_HEADED", "0")  # tests never open browser windows
import jsonschema

from autoskill import projects, secrets
from autoskill.recorder import compile as compiler
from autoskill.recorder import har

BASE = "http://127.0.0.1:9"
TOKEN = "tok-abcdefghijklmnop1234"
PASSWORD = "s3cret-pw-not-for-disk"


def entry(method, path, status, response=None, *, kind="fetch", body=None, headers=None, mime="application/json", text=None):
    request = {"method": method, "url": BASE + path, "headers": [{"name": k, "value": v} for k, v in (headers or {}).items()]}
    if body is not None:
        request["postData"] = {"mimeType": "application/json", "text": json.dumps(body)}
    content = {"mimeType": mime, "text": text if text is not None else json.dumps(response)}
    return {"_resourceType": kind, "request": request, "response": {"status": status, "content": content}}


def valid(schema, value) -> bool:
    return not list(jsonschema.Draft202012Validator(schema).iter_errors(value))


class SchemaInferenceTests(unittest.TestCase):
    def test_array_items_require_only_what_every_item_has(self):
        rows = [{"id": 1, "name": "a", "heading": "x"}, {"id": 2, "name": "b"}, {"id": 3, "name": None, "extra": True}]
        schema = har.infer_schema({"items": rows, "total": 3})
        self.assertEqual(schema["properties"]["items"]["items"]["required"], ["id", "name"])
        self.assertTrue(valid(schema, {"items": rows, "total": 3}))  # a recording always satisfies its own schema
        self.assertFalse(valid(schema, {"items": [{"name": "no id"}], "total": 1}))
        self.assertFalse(valid(schema, {"items": rows}))
        self.assertFalse(valid(schema, {"items": rows, "total": "3"}))

    def test_maps_keyed_by_data_do_not_pin_their_keys(self):
        catalogue = {f"item-{i}": {"name": f"n{i}", "links": [{"url": "u"}] if i % 2 else []} for i in range(30)}
        schema = har.infer_schema(catalogue)
        self.assertNotIn("required", schema)
        self.assertEqual(schema["additionalProperties"]["required"], ["links", "name"])
        self.assertTrue(valid(schema, catalogue))
        self.assertTrue(valid(schema, {"another-key": {"name": "x", "links": []}}))  # new keys are fine
        self.assertFalse(valid(schema, {}))
        self.assertFalse(valid(schema, {"k": {"links": []}}))

    def test_wide_records_keep_their_fields_and_secrets_are_not_described(self):
        record = {f"field_{i}": i for i in range(20)}
        self.assertEqual(len(har.infer_schema(record)["required"]), 20)
        schema = har.infer_schema({"token": "abc", "user": {"id": 1}})
        self.assertEqual(schema["required"], ["token", "user"])
        self.assertNotIn("token", schema["properties"])
        self.assertEqual(har.infer_schema([1, "a"]), {"type": "array"})  # mixed types: no item constraint

    def test_stable_fields_are_echoes_and_enums_not_ids_or_secrets(self):
        response = {"id": "T-1009", "title": "新工单", "status": "open", "token": "abc", "created_at": "2026-11-12T10:00:00"}
        chosen = har.stable_fields(response, [{"title": "新工单"}])
        self.assertEqual(chosen, {"/title": "新工单", "/status": "open"})


class CompileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous = os.environ.get("AUTOSKILL_WORKSPACE")
        os.environ["AUTOSKILL_WORKSPACE"] = self.tmp.name
        self.pid = projects.create("编译测试", BASE, project_id="t")["id"]

    def tearDown(self):
        if self.previous is None:
            os.environ.pop("AUTOSKILL_WORKSPACE", None)
        else:
            os.environ["AUTOSKILL_WORKSPACE"] = self.previous
        self.tmp.cleanup()

    def rec(self, **extra):
        steps = [{"action": "click", "locators": [{"by": "testid", "value": "go"}]},
                 {"assertion": "visible", "locators": [{"by": "testid", "value": "done"}]}]
        return {"project_id": self.pid, "start_url": "/", "steps": steps, "secrets": [], "used_login": False, "as_login": False, **extra}

    def login_traffic(self):
        auth = {"Authorization": "Bearer " + TOKEN, "User-Agent": "x", "Cookie": "sid=abc"}
        items = [{"id": "I-1001", "name": "a"}, {"id": "I-1002", "name": "b", "note": "only here"}]
        return {"log": {"entries": [
            entry("GET", "/", 200, kind="document", mime="text/html", text="<title>T</title>"),
            entry("POST", "/api/login", 200, {"token": TOKEN, "user": {"role": "admin"}}, body={"username": "qa", "password": PASSWORD}),
            entry("GET", "/api/items", 200, {"items": items, "total": 2}, headers=auth),
            entry("GET", "/api/items", 200, {"items": items, "total": 2}, headers=auth),  # repeated request: one case
            entry("GET", "/api/items/I-1001", 200, {"id": "I-1001", "name": "a"}, headers=auth),
            entry("POST", "/api/upload", 200, {"ok": True}, headers=auth, mime="application/json"),
            entry("GET", "https://elsewhere.test/x".replace(BASE, ""), 200, {"a": 1}),
        ]}}

    def test_login_recording_becomes_a_dependency_chain_without_secrets(self):
        traffic = self.login_traffic()
        traffic["log"]["entries"][-1]["request"]["url"] = "https://elsewhere.test/x"
        summary = compiler.build(self.pid, self.rec(as_login=True), traffic, {"cookies": [], "origins": []}, "登录")
        cases = {c["id"]: c for c in projects.cases(self.pid)}
        by_path = {c["path"]: c for c in cases.values() if c["kind"] == "api"}
        login, listing, detail = by_path["/api/login"], by_path["/api/items"], by_path["/api/items/{{item_id}}"]
        self.assertEqual(login["json"]["username"], "qa")
        self.assertRegex(login["json"]["password"], r"^\$\{ENV:[A-Z_]+\}$")  # the password is a reference, stored locally
        self.assertEqual(secrets.resolve(login["json"]["password"]), PASSWORD)
        self.assertEqual(login["extensions"]["extract"], {"token": "/token"})
        self.assertEqual(listing["headers"], {"Authorization": "Bearer {{token}}"})  # cookies and browser noise are dropped
        self.assertEqual(projects.depends_on(listing), [login["id"]])
        self.assertEqual(listing["extensions"]["extract"], {"item_id": "/items/0/id"})
        self.assertEqual(set(projects.depends_on(detail)), {login["id"], listing["id"]})
        self.assertEqual(listing["assertions"]["schema"]["properties"]["items"]["items"]["required"], ["id", "name"])
        self.assertEqual(sum(c["kind"] == "api" for c in cases.values()), 4)  # the repeat collapsed; the other origin was skipped
        self.assertEqual(projects.auth(self.pid), {"ui_case": summary["ui_case"]["id"], "api_case": login["id"], "api_check_case": listing["id"]})
        self.assertTrue(any("elsewhere.test" in note for note in summary["notes"]))
        shareable = b"".join(f.read_bytes() for f in projects.path(self.pid).rglob("*") if f.is_file())
        for secret in (PASSWORD, TOKEN, "sid=abc"):
            self.assertNotIn(secret.encode(), shareable)

    def test_recording_while_signed_in_depends_on_the_login_cases(self):
        compiler.build(self.pid, self.rec(as_login=True), self.login_traffic(), {"cookies": [], "origins": []}, "登录")
        auth = projects.auth(self.pid)
        later = {"log": {"entries": [
            entry("GET", "/api/items", 200, {"items": [{"id": "I-1001", "name": "a"}], "total": 1}, headers={"Authorization": "Bearer another-session-token-000"}),
            entry("POST", "/api/items", 201, {"id": "I-2000", "name": "新的", "status": "open"}, body={"name": "新的"},
                  headers={"Authorization": "Bearer another-session-token-000"}),
        ]}}
        summary = compiler.build(self.pid, self.rec(used_login=True), later, None, "新建")
        self.assertEqual(summary["ui_case"]["depends_on"], [auth["ui_case"]])
        self.assertEqual((summary["api_new"], summary["api_reused"]), (1, 1))  # the listing request already had a case
        created = next(c for c in projects.cases(self.pid) if c.get("method") == "POST" and c["path"] == "/api/items")
        self.assertEqual(created["headers"], {"Authorization": "Bearer {{token}}"})  # a different session's token still maps to the login
        self.assertEqual(projects.depends_on(created), [auth["api_case"]])
        self.assertEqual(created["assertions"]["json_equals"], {"/name": "新的", "/status": "open"})
        self.assertEqual(created["risk"], "write")

    def test_a_page_without_api_calls_gets_no_api_cases(self):
        traffic = {"log": {"entries": [
            entry("GET", "/", 200, kind="document", mime="text/html", text="<html><title>纯前端页面</title></html>"),
            entry("GET", "/app.js", 200, kind="script", mime="text/javascript", text="console.log(1)"),
            entry("GET", "/missing.css", 404, kind="stylesheet", mime="text/css", text=""),
        ]}}
        summary = compiler.build(self.pid, self.rec(), traffic, None, "纯页面")
        # the page case proves the page loads; a plain HTTP request for it is what anti-bot gateways answer with 202
        self.assertEqual((summary["api_traffic"], summary["api_cases"]), ("none", []))
        self.assertFalse([c for c in projects.cases(self.pid) if c["kind"] == "api"])

    def test_explicit_xhr_json_is_not_misclassified_from_its_filename(self):
        catalogue = {f"k{i}": {"name": str(i)} if i % 2 else {} for i in range(30)}
        traffic = {"log": {"entries": [entry("GET", "/learn.json", 200, catalogue, kind="xhr")]}}
        summary = compiler.build(self.pid, self.rec(), traffic, None, "静态数据")
        case = next(c for c in projects.cases(self.pid) if c["kind"] == "api")
        self.assertEqual((summary["api_traffic"], case["extensions"]["traffic"], case["title"]), ("business", "business", "【待确认：接口功能目标】"))
        self.assertEqual(case["extensions"]["technical_capture_label"], "查询 /learn.json")
        self.assertTrue(any("只校验了状态码和大致结构" in note for note in summary["notes"]))

    def test_summary_counts_every_recorded_step_and_assertion(self):
        rec = self.rec()
        rec["steps"] = [{"action": "click", "locators": [{"by": "testid", "value": "a"}]},
                        {"assertion": "visible", "locators": [{"by": "testid", "value": "b"}]},  # a check made mid-flow
                        {"action": "click", "locators": [{"by": "testid", "value": "c"}]},
                        {"assertion": "visible", "locators": [{"by": "testid", "value": "d"}]}]
        summary = compiler.build(self.pid, rec, {"log": {"entries": []}}, None, "中途断言")
        self.assertEqual((summary["ui_case"]["steps"], summary["ui_case"]["assertions"]), (3, 2))  # opening the page is a step

    def test_each_workspace_records_in_its_own_browser_session(self):
        from autoskill.recorder import session

        mine = session.session_name()
        self.assertEqual(mine, session.session_name())
        with tempfile.TemporaryDirectory() as other:
            os.environ["AUTOSKILL_WORKSPACE"] = other
            try:
                self.assertNotEqual(session.session_name(), mine)
            finally:
                os.environ["AUTOSKILL_WORKSPACE"] = self.tmp.name

    def test_a_repeated_request_is_listed_once_even_when_its_case_already_exists(self):
        catalogue = {f"k{i}": {"name": str(i)} for i in range(30)}
        twice = {"log": {"entries": [entry("GET", "/learn.json", 200, catalogue, kind="xhr")] * 2}}
        first = compiler.build(self.pid, self.rec(), twice, None, "第一次")
        again = compiler.build(self.pid, self.rec(), twice, None, "第二次")
        self.assertEqual((first["api_new"], len(first["api_cases"])), (1, 1))
        self.assertEqual((again["api_new"], again["api_reused"], len(again["api_cases"])), (0, 1, 1))
        self.assertEqual(len(again["notes"]), len(set(again["notes"])))

    def test_no_assertions_is_reported_not_hidden(self):
        rec = self.rec()
        rec["steps"] = rec["steps"][:1]
        summary = compiler.build(self.pid, rec, {"log": {"entries": []}}, None, "只有动作")
        self.assertEqual((summary["ui_case"]["assertions"], summary["api_traffic"]), (0, "none"))
        self.assertTrue(any("没有记录断言" in note for note in summary["notes"]))


if __name__ == "__main__":
    unittest.main()
