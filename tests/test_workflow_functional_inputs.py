"""Business oracles remain strict while optional metadata and typed inputs reach real HTTP runs."""
import copy
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import test_workflow as fixture

from autoskill import builder, db, execution_inputs, plan, projects, runner, workflow, workflow_definition, workflow_state


class TypedEcho(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return

    def do_POST(self):
        value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(value)
        body = json.dumps({"received": value}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FunctionalWorkflowInputTests(unittest.TestCase):
    setUp = fixture.WorkflowTests.setUp
    tearDownClass = classmethod(fixture.WorkflowTests.tearDownClass.__func__)
    key = fixture.WorkflowTests.key
    create = fixture.WorkflowTests.create
    claim = fixture.WorkflowTests.claim
    submit = fixture.WorkflowTests.submit

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), TypedEcho)
        cls.server.requests = []
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

    def document(self):
        return {"description": "按提供的数据创建业务记录。", "preconditions": "测试服务可用。",
                "steps": ["1. 提交本次业务数据。", "2. 核对返回的数据与本次输入一致。"],
                "expected": "2. 返回的数据与本次输入一致，保留数字、开关及嵌套值的类型。",
                "source": {"kind": "synthetic_requirement", "text": "创建操作应原样保存提供的数据类型和值。"},
                "author": "【待确认：真实编制人】", "executor": "【待确认：执行者】",
                "environment": "【待确认：环境名称】", "remarks": "【待确认：备注】", "business_path": ["【待确认：分组名称】"]}

    def queue(self, document=None, *, inputs=None, identity="API-TYPED", parametrized=True, oracle_expected=None):
        definition = {"inputs": inputs} if inputs is not None else {}
        flow = self.create(validation=definition)
        self.submit(flow, self.claim(flow), {"requirements": [{"id": "r1", "text": "业务数据类型和值保持不变。", "source_ids": ["s1"]}]})
        document = copy.deepcopy(document or self.document())
        expected = {"status": 200, "json_equals": {"/received": "{{payload}}" if parametrized else {"quantity": 2}}}
        self.submit(flow, self.claim(flow), {
            "scenarios": [{"id": "sc1", "title": "保存业务数据", "requirement_ids": ["r1"], "oracle_ids": ["o1"]}],
            "oracles": [{"id": "o1", "kind": "api", "requirement_ids": ["r1"], "checks": expected,
                         "functional_expected": document["expected"]}]})
        case = builder.api_case(identity, document["description"], "POST", "/records", expected,
                                json="{{payload}}" if parametrized else {"quantity": 2}, test_case_document=document)
        self.submit(flow, self.claim(flow), {"cases": [case], "bindings": {"sc1": [identity]}})
        self.assertIsNone(self.claim(flow))
        item = next(item for item in workflow.status(flow)["items"] if item["phase"] == "execute")
        self.assertFalse(item["publication_receipt"]["complete"])
        return flow, item, db.one("SELECT * FROM runs WHERE id = ?", (item["run_id"],))

    def test_unknown_metadata_does_not_block_first_typed_http_validation(self):
        payloads = [{"quantity": 2, "enabled": True, "optional": None, "items": [1, "订单"], "nested": {"ratio": 0.25}},
                    {"quantity": 7, "enabled": False, "optional": "已填", "items": [], "nested": {"ratio": 1.5}}]
        scopes = []
        for number, payload in enumerate(payloads):
            document = self.document()
            if number:
                document["author"] = "本地合成测试主体"
            supplied = {"payload": payload}
            flow, item, run = self.queue(document, inputs=supplied, identity=f"API-TYPED-{number}")
            manifest = execution_inputs.load(run)
            self.assertEqual(manifest["inputs"], supplied)
            self.assertEqual(manifest["variables"]["payload"], payload)
            self.assertTrue(manifest["api_only"])
            frozen = manifest["cases"][f"API-TYPED-{number}"]
            self.assertEqual(frozen["extensions"]["functional_design"]["state"], "compiled_from_workflow_oracles")
            before = len(self.server.requests)
            self.assertEqual(runner.execute(run["id"])["status"], "Pass")
            self.assertEqual(self.server.requests[before:], [payload])
            self.assertEqual(self.claim(flow)["phase"], "knowledge")
            published = projects.case(self.project, frozen["id"])
            self.assertEqual(published, frozen)
            result = db.one("SELECT * FROM results WHERE run_id = ?", (run["id"],))
            self.assertEqual(json.loads(result["detail"])["business_status"], "Pass")
            self.assertTrue(execution_inputs.result_evidence(run, result, frozen))
            self.assertFalse(execution_inputs.result_current(self.project, published, result))
            receipt = next(value for value in workflow.status(flow)["items"] if value["id"] == item["id"])
            self.assertEqual(receipt["publication_receipt"]["published_ids"], [frozen["id"]])
            self.assertTrue(receipt["publication_receipt"]["complete"])
            scopes.append(manifest["scope_hashes"][frozen["id"]])
        self.assertNotEqual(scopes[0], scopes[1])

    def test_equivalent_prose_arrays_and_strings_map_and_publish_after_real_http(self):
        for number in range(2):
            document = self.document()
            text = document["expected"]
            document["expected"] = [text] if number else text
            oracle_expected = text if number else [text]
            identity = f"API-PROSE-{number}"
            flow, _item, run = self.queue(document, identity=identity, parametrized=False, oracle_expected=oracle_expected)
            frozen = execution_inputs.load(run)["cases"][identity]
            self.assertEqual(frozen["extensions"]["functional_design"]["state"], "compiled_from_workflow_oracles")
            self.assertEqual(frozen["extensions"]["test_case_document"]["expected"], document["expected"])
            before = len(self.server.requests)
            self.assertEqual(runner.execute(run["id"])["status"], "Pass")
            self.assertEqual(self.server.requests[before:], [{"quantity": 2}])
            self.assertEqual(self.claim(flow)["phase"], "knowledge")
            original = projects.case(self.project, identity)
            definitions = workflow_definition.snapshot(workflow_state.workflow(flow))["documents"]
            candidate = copy.deepcopy(original)
            prose = candidate["extensions"]["test_case_document"]
            prose["steps"] = "\n".join(prose["steps"])
            prose["expected"] = oracle_expected
            materialized = workflow_definition._materialize({"cases": [candidate]}, definitions, self.project, True)["materialization.json"]
            self.assertEqual(materialized["cases"][0]["extensions"]["functional_design"], original["extensions"]["functional_design"])
            prose["expected"] = text + " "
            with self.assertRaisesRegex(workflow.WorkflowError, "原始检查条件"):
                workflow_definition._materialize({"cases": [candidate]}, definitions, self.project, True)

    def test_missing_input_is_blocked_without_sending_literal_or_publishing(self):
        flow, _item, run = self.queue()
        self.assertEqual(execution_inputs.load(run)["inputs"], {})
        before = len(self.server.requests)
        self.assertEqual(runner.execute(run["id"])["status"], "Blocked")
        self.assertEqual(len(self.server.requests), before)
        task = self.claim(flow)
        self.assertEqual(task["phase"], "diagnose")
        self.assertTrue(task["publication_receipt"]["complete"])
        self.assertEqual(task["publication_receipt"]["published_ids"], [])
        self.assertIn("API-TYPED", task["publication_receipt"]["unpublished"])
        self.assertEqual(projects.cases(self.project), [])

    def test_unknown_business_expectation_still_cannot_establish_business_pass(self):
        document = self.document()
        document["expected"] = "【待确认：业务预期】"
        flow, _item, run = self.queue(document, parametrized=False)
        self.assertEqual(runner.execute(run["id"])["status"], "Pass")
        task = self.claim(flow)
        self.assertEqual(task["phase"], "diagnose")
        result = db.one("SELECT detail FROM results WHERE run_id = ?", (run["id"],))
        self.assertEqual(json.loads(result["detail"])["business_status"], "Unverified")
        self.assertEqual(task["publication_receipt"]["published_ids"], [])
        self.assertEqual(projects.cases(self.project), [])

    def test_author_only_maintenance_preserves_mapping_but_oracle_changes_are_rejected(self):
        flow, _item, run = self.queue(parametrized=False)
        runner.execute(run["id"])
        self.assertEqual(self.claim(flow)["phase"], "knowledge")
        original = projects.case(self.project, "API-TYPED")
        document = workflow_definition.snapshot(workflow_state.workflow(flow))["documents"]
        candidate = copy.deepcopy(original)
        candidate["extensions"]["test_case_document"].update(author="维护主体（合成）", remarks="补齐元数据")
        materialized = workflow_definition._materialize({"cases": [candidate]}, document, self.project, True)["materialization.json"]
        self.assertEqual(materialized["cases"][0]["extensions"]["functional_design"], original["extensions"]["functional_design"])
        for field, value in (("expected", "返回另一种结果。"), ("steps", ["1. 改做另一个操作。"]),
                             ("source", "其他需求"), ("unknown_metadata", "不能默认忽略未知字段")):
            altered = copy.deepcopy(candidate)
            altered["extensions"]["test_case_document"][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(workflow.WorkflowError, "原始检查条件"):
                workflow_definition._materialize({"cases": [altered]}, document, self.project, True)
        altered = copy.deepcopy(candidate)
        altered["assertions"]["json_equals"] = {"/received": {"quantity": 99}}
        with self.assertRaisesRegex(workflow.WorkflowError, "原始检查条件"):
            workflow_definition._materialize({"cases": [altered]}, document, self.project, True)
        remapped = copy.deepcopy(candidate)
        expected = copy.deepcopy(document["oracles.json"])
        workflow_definition._functional_mapping(remapped, original, expected, document["scenarios.json"], document["definition.json"])
        self.assertEqual(remapped["extensions"]["functional_design"], original["extensions"]["functional_design"])
        remapped["extensions"]["test_case_document"]["expected"] = "新增的业务结果。"
        expected[0]["functional_expected"] = "新增的业务结果。"
        workflow_definition._functional_mapping(remapped, original, expected, document["scenarios.json"], document["definition.json"])
        self.assertEqual(remapped["extensions"]["functional_design"]["state"], "unmapped")

    def test_failed_plan_binding_is_public_while_real_business_result_stays_separate(self):
        source = self.folder / "design.json"
        source.write_text(json.dumps([{"id": "DESIGN-1", "title": "保存业务数据", "module": "记录", "preconditions": "测试服务可用。",
                                      "steps": ["1. 保存一条记录。"], "expected": "1. 数据被原样保存。"}]), encoding="utf-8")
        written = plan.write(self.project, str(source))
        document = self.document()
        document["source"] = {"kind": "case_plan", "file": written["excel"], "case_id": "DESIGN-1"}
        flow, item, run = self.queue(document, parametrized=False)
        pending = next(value for value in workflow.status(flow)["items"] if value["id"] == item["id"])
        self.assertIn("另一份设计任务", pending["plan_bindings"][0]["error"])
        self.assertEqual(pending["plan_bindings"][0]["run_id"], run["id"])
        self.assertEqual(runner.execute(run["id"])["status"], "Pass")
        following = self.claim(flow)
        self.assertEqual(following["phase"], "knowledge")
        self.assertEqual(following["plan_bindings"], pending["plan_bindings"])
        finished = next(value for value in workflow.status(flow)["items"] if value["id"] == item["id"])
        self.assertEqual(finished["plan_bindings"], pending["plan_bindings"])
        self.assertEqual(finished["publication_receipt"]["published_ids"], ["API-TYPED"])
        self.assertEqual(plan.show()["cases"][0]["status"], "未执行")
        requests = len(self.server.requests)
        self.assertEqual(workflow.status(flow), workflow.status(flow))
        self.assertEqual(len(self.server.requests), requests)

    def test_invalid_inputs_and_reserved_random_value_reject_before_execution(self):
        for inputs in ([1], {"RAND": "fixed"}):
            with self.subTest(inputs=inputs), self.assertRaises(workflow.WorkflowError):
                self.create(validation={"inputs": inputs})
        self.assertEqual(db.rows("SELECT id FROM runs"), [])


if __name__ == "__main__":
    unittest.main()
