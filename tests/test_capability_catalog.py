"""Discovery stays available even when no recording or usable runtime exists."""
import contextlib
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill import capabilities, cli  # noqa: E402
from autoskill.recorder import agent, native, session  # noqa: E402


class CapabilityDiscoveryTests(unittest.TestCase):
    def invoke(self, argv):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli.main(argv)
        return code, json.loads(output.getvalue())

    def test_discovery_does_not_touch_runtime_workspace_service_or_recording(self):
        requests = [
            ["capabilities"], ["capabilities", "--group", "network"],
            ["capabilities", "dialog", "accept"], ["api", "capabilities"],
            ["browser"], ["browser", "help"], ["browser", "--help"],
            ["browser", "click", "--help"], ["browser", "help", "network"],
        ]
        with mock.patch.dict(os.environ, {"AUTOSKILL_BRIDGE": "on"}), \
             mock.patch.object(cli.db, "init", side_effect=AssertionError("database touched")), \
             mock.patch.object(cli.paths, "restricted", side_effect=AssertionError("workspace probed")), \
             mock.patch.object(cli, "delegate", side_effect=AssertionError("service contacted")), \
             mock.patch.object(session, "current", side_effect=AssertionError("recording required")), \
             mock.patch.object(agent, "call", side_effect=AssertionError("browser launched")):
            for request in requests:
                with self.subTest(request=request):
                    code, response = self.invoke(request)
                    self.assertEqual(code, 0)
                    if request[0] != "api":
                        self.assertEqual(response["schema_version"], 1)
                        self.assertGreater(response["counts"]["total"], 0)
                    self.assertFalse(response["verified"])

    def test_native_execution_never_implies_portable_replay(self):
        response = capabilities.catalog()
        rows = response["capabilities"]
        self.assertEqual(len(rows), len({row["id"] for row in rows}))
        for row in rows:
            with self.subTest(command=row["command"]):
                portable = row["recording"] in ("portable", "configuration")
                self.assertEqual(row["replayable"], {"playwright": portable, "selenium": portable})
                self.assertEqual(row["exportable"], {"playwright": portable, "selenium": portable})
                self.assertEqual(row["recordable"], row["recording"] in ("portable", "native_only"))
                self.assertFalse(row["verified"])
                self.assertTrue(row["dependencies"])
                self.assertTrue(row["limitations"])
        self.assertEqual(capabilities.catalog("network route")["capabilities"][0]["recording"], "native_only")
        self.assertEqual(capabilities.catalog("screenshot")["capabilities"][0]["recording"], "observation")
        self.assertEqual(capabilities.catalog("focus")["capabilities"][0]["recording"], "portable")

    def test_group_alias_queries_and_return_values_are_independent(self):
        group = capabilities.catalog(group="network")
        self.assertTrue(all(row["group"] == "network" for row in group["capabilities"]))
        self.assertEqual(group["counts"]["total"], len(group["capabilities"]))
        alias = capabilities.catalog("scrollinto")["capabilities"][0]
        self.assertEqual(alias["command"], "scrollintoview")
        alias["replayable"]["playwright"] = False
        self.assertTrue(capabilities.catalog("scrollintoview")["capabilities"][0]["replayable"]["playwright"])
        self.assertEqual(capabilities.catalog("autoskill browser dialog accept")["capabilities"][0]["command"], "dialog accept")
        self.assertEqual(capabilities.catalog("cookies set --curl cookies.txt")["capabilities"][0]["recording"], "native_only")
        self.assertEqual(capabilities.catalog("wait --text Done")["capabilities"][0]["recording"], "native_only")

    def test_unknown_query_has_json_error_without_recording_lookup(self):
        with mock.patch.object(session, "current", side_effect=AssertionError("recording required")):
            code, response = self.invoke(["capabilities", "no-such-command"])
        self.assertEqual(code, 2)
        self.assertIn("Unknown browser capability", response["error"])

    def test_literal_help_text_is_not_intercepted(self):
        self.assertIsNone(capabilities.help_query(["fill", "@e1", "--", "--help"]))
        self.assertEqual(capabilities.help_query(["click", "--help"]), "click")

    def test_catalog_matches_actual_native_step_and_observation_routing(self):
        samples = [
            ("set headers", ["set", "headers", '{"X-Test":"1"}'], "portable"),
            ("set viewport", ["set", "viewport", "800", "600"], "portable"),
            ("storage set", ["storage", "local", "set", "test", "value"], "portable"),
            ("cookies clear", ["cookies", "clear"], "portable"),
            ("clipboard write", ["clipboard", "write", "value"], "portable"),
            ("network route", ["network", "route", "*", "--abort"], "native_only"),
            ("wait condition", ["wait", "--text", "Done"], "native_only"),
            ("a11y url", ["a11y", "http://fixture.invalid/"], "native_only"),
            ("console", ["console"], "observation"),
            ("a11y", ["a11y"], "observation"),
        ]
        with mock.patch.object(agent, "call", return_value={}) as call:
            for query, argv, expected in samples:
                with self.subTest(command=query):
                    outcome = native.execute("isolated-test", argv, {"project_id": "test"},
                                             store_value=lambda value, label: value)
                    step = outcome.get("step")
                    actual = "observation" if step is None else "native_only" if step["action"] == "native_step" else "portable"
                    row = capabilities.catalog(query)["capabilities"][0]
                    self.assertEqual((row["recording"], actual), (expected, expected))
            self.assertEqual(call.call_count, len(samples))

    def test_browser_global_flags_reach_dispatch_unchanged(self):
        requests = [
            ["--session", "isolated", "get", "cdp-url"],
            ["--config", "trusted.json", "console"],
            ["fill", "@e1", "--", "--help"],
        ]
        for request in requests:
            with self.subTest(request=request), \
                 mock.patch.dict(os.environ, {"AUTOSKILL_BRIDGE": "off"}), \
                 mock.patch.object(cli.db, "init"), \
                 mock.patch.object(cli, "dispatch", return_value=({"forwarded": True}, 0)) as dispatch:
                code, response = self.invoke(["browser", *request])
                self.assertEqual((code, response), (0, {"forwarded": True}))
                self.assertEqual(dispatch.call_args.args[0].args, request)

    def test_delegation_input_preparation_failures_keep_json_contract(self):
        from autoskill import bridge

        for error in (ValueError("batch 引号不完整"), OSError("输入文件不可读")):
            with self.subTest(error=type(error).__name__), \
                 mock.patch.object(bridge, "service", return_value=(12345, "")), \
                 mock.patch.object(bridge, "prepare_argv", side_effect=error), \
                 mock.patch.object(bridge, "forward") as forward:
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = cli.delegate(cli.parser().parse_args(["browser", "snapshot"]), ["browser", "snapshot"], "restricted")
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(output.getvalue()), {"error": str(error)})
                forward.assert_not_called()


if __name__ == "__main__":
    unittest.main()
