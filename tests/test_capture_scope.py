"""Archived recording captures affect only replay cases that explicitly depend on those sources."""
import copy
import json
import unittest

import test_execution_inputs as fixture

from autoskill import builder, data_refs, domain, execution_inputs, projects, runner
from autoskill.core import store


class CaptureScopeTests(unittest.TestCase):
    setUpClass = classmethod(fixture.ExecutionInputTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.ExecutionInputTests.tearDownClass.__func__)
    setUp = fixture.ExecutionInputTests.setUp
    execute = fixture.ExecutionInputTests.execute
    overwrite = fixture.ExecutionInputTests.overwrite

    def change(self, values):
        return domain.commit(self.pid, values, expected_head=domain.manifest(self.pid).get("head"))

    def test_unrelated_capture_append_change_and_removal_keep_real_pass_current(self):
        first = "conversations/capture-first/manifest.json"
        second = "conversations/capture-next/raw.har"
        self.change({first: {"source": "original unrelated recording"}})
        final, rows = self.execute()
        raw_manifest = data_refs.resolve(final["input_manifest"]).read_bytes()
        frozen = execution_inputs.load(final)
        self.assertIn("domain/" + first, frozen["sources"]["files"])
        self.assertTrue(execution_inputs.result_current(self.pid, self.case, rows[0]))
        for change in ({second: b"new unrelated traffic"}, {first: {"source": "updated unrelated capture"}}, {first: None}):
            self.change(change)
            self.assertTrue(execution_inputs.result_current(self.pid, self.case, rows[0]))
            self.assertTrue(execution_inputs.result_evidence(final, rows[0], self.case))
            self.assertEqual(data_refs.resolve(final["input_manifest"]).read_bytes(), raw_manifest)
        current = execution_inputs.capture(self.pid, [self.case["id"]], "playwright", persist=False)
        self.assertIn("domain/" + second, current["sources"]["files"])

    def test_explicit_capture_and_dependency_capture_changes_or_removal_invalidate(self):
        first, second = "conversations/capture-direct/raw.har", "conversations/capture-dependency/manifest.json"
        self.change({first: b"original direct", second: {"source": "original dependency"}})
        prerequisite = builder.api_case("API-SOURCE", "来源前置", "GET", "/before", {"status": 200, "json_equals": {"/ok": True}})
        prerequisite["extensions"]["source_capture"] = {"id": "capture-dependency"}
        self.case["extensions"].update(depends_on=[prerequisite["id"]], test_case_document={
            "source": {"kind": "case_plan", "upstream": {"kind": "conversation_capture", "file": "domain/" + first}}})
        projects.save_cases(self.pid, [self.case, prerequisite])
        self.case = projects.case(self.pid, self.case["id"])
        for name, original, changed in ((first, b"original direct", b"changed direct"),
                                        (second, {"source": "original dependency"}, {"source": "changed dependency"})):
            with self.subTest(source=name):
                final, results = self.execute()
                target = next(row for row in results if row["case_id"] == self.case["id"])
                self.assertTrue(execution_inputs.result_current(self.pid, self.case, target))
                self.change({name: changed})
                self.assertFalse(execution_inputs.result_current(self.pid, self.case, target))
                self.assertTrue(execution_inputs.result_evidence(final, target, self.case))
                self.change({name: original})
                self.assertTrue(execution_inputs.result_current(self.pid, self.case, target))
                self.change({name: None})
                self.assertFalse(execution_inputs.result_current(self.pid, self.case, target))
                self.assertTrue(execution_inputs.result_evidence(final, target, self.case))
                self.change({name: original})

    def test_unrelated_sibling_capture_reference_does_not_enter_target_scope(self):
        self.change({"conversations/capture-sibling/raw.har": b"first"})
        sibling = builder.api_case("API-SIBLING", "其他来源用例", "GET", "/other", {"status": 200, "json_equals": {"/ok": True}})
        sibling["extensions"]["source_capture"] = {"id": "capture-sibling"}
        projects.save_cases(self.pid, [self.case, sibling])
        prepared = runner.prepare(self.pid, [self.case["id"], sibling["id"]])
        final, rows = self.execute(prepared)
        original = {row["case_id"]: row for row in rows}
        self.change({"conversations/capture-sibling/raw.har": b"second"})
        self.assertTrue(execution_inputs.result_current(self.pid, self.case, original[self.case["id"]]))
        self.assertFalse(execution_inputs.result_current(self.pid, sibling, original[sibling["id"]]))
        self.assertTrue(execution_inputs.result_evidence(final, original[sibling["id"]], sibling))

    def test_real_case_config_and_requirement_changes_remain_in_scope(self):
        self.change({"conversations/capture-unrelated/raw.har": b"archived", "workflows/w/requirements.json": {"rule": "original"}})
        final, rows = self.execute()
        changed = copy.deepcopy(self.case)
        changed["path"] = "/different"
        self.overwrite([changed])
        self.assertFalse(execution_inputs.result_current(self.pid, changed, rows[0]))
        self.overwrite([self.case])
        config = projects.config(self.pid)
        store.write(projects.path(self.pid) / "qa-config.json", {**config, "timeout_seconds": config["timeout_seconds"] + 1})
        self.assertFalse(execution_inputs.result_current(self.pid, self.case, rows[0]))
        store.write(projects.path(self.pid) / "qa-config.json", config)
        self.assertTrue(execution_inputs.result_current(self.pid, self.case, rows[0]))
        self.change({"workflows/w/requirements.json": {"rule": "changed"}})
        self.assertFalse(execution_inputs.result_current(self.pid, self.case, rows[0]))
        self.assertTrue(execution_inputs.result_evidence(final, rows[0], self.case))
        self.assertEqual(json.loads(rows[0]["detail"])["business_status"], "Pass")


if __name__ == "__main__":
    unittest.main()
