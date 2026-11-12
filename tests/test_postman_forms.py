"""Exported query and form fields retain native replay semantics and secret references."""
import json
import sys
import unittest
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill import builder
from autoskill.replay import postman


class PostmanFormTests(unittest.TestCase):
    def export(self, **data):
        case = builder.api_case("TC-API-001", "表单提交", "POST", "/submit", {"status": 200, "json_equals": {"/ok": True}}, **data)
        collection, environment = postman.export("表单测试", [case], "https://example.test")
        return collection["item"][0]["request"], environment

    def test_form_export_keeps_repeated_and_empty_values(self):
        request, _ = self.export(form={"tag": ["red", "blue", ""], "empty": "", "note": "a&b=中文"})
        self.assertEqual(request["body"], {"mode": "urlencoded", "urlencoded": [
            {"key": "tag", "value": "red"}, {"key": "tag", "value": "blue"}, {"key": "tag", "value": ""},
            {"key": "empty", "value": ""}, {"key": "note", "value": "a&b=中文"},
        ]})

    def test_form_secrets_become_environment_references(self):
        request, environment = self.export(form={"password": "${ENV:TEST_PASSWORD}", "token": ["${ENV:TEST_TOKEN}", "other"]})
        self.assertEqual(request["body"]["urlencoded"][0], {"key": "password", "value": "{{TEST_PASSWORD}}"})
        self.assertEqual(request["body"]["urlencoded"][1], {"key": "token", "value": "{{TEST_TOKEN}}"})
        values = {item["key"]: item for item in environment["values"]}
        for name in ("TEST_PASSWORD", "TEST_TOKEN"):
            self.assertEqual((values[name]["value"], values[name]["type"]), ("", "secret"))

    def test_query_fields_and_raw_url_preserve_multivalue_and_reserved_characters(self):
        request, _ = self.export(params={"tag": ["red", "blue", ""], "a&b": "x+y 中文", "token": "${ENV:TEST_TOKEN}"})
        self.assertEqual(parse_qsl(urlsplit(request["url"]["raw"].replace("{{base_url}}", "https://example.test")).query,
                                   keep_blank_values=True),
                         [("tag", "red"), ("tag", "blue"), ("tag", ""), ("a&b", "x+y 中文"), ("token", "{{TEST_TOKEN}}")])
        self.assertEqual(len(request["url"]["query"]), 5)

    def test_json_bodies_and_explicit_media_types_are_unchanged(self):
        request, _ = self.export(json={"name": "new"}, headers={"Content-Type": "application/merge-patch+json"})
        self.assertEqual(json.loads(request["body"]["raw"]), {"name": "new"})
        self.assertEqual(request["header"], [{"key": "Content-Type", "value": "application/merge-patch+json"}])


if __name__ == "__main__":
    unittest.main()
