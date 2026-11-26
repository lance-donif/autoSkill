"""Design-stage projects, project environments and delivery boundaries."""
import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill import builder, cli, execution_inputs, plan, projects, runner
from autoskill.replay import ir
from autoskill.server.app import create_app


class EchoTarget(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or "{}")
        self.server.requests.append(payload)
        body = json.dumps({"echo": payload.get("msg")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), EchoTarget)
    server.requests = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _design_file(folder: Path, title: str) -> Path:
    file = folder / "design.json"
    file.write_text(json.dumps([{"id": "TC-001", "title": title, "steps": ["打开页面", "提交表单"],
                                 "expected": ["1. 显示表单。", "2. 显示提交成功。"], "source": "需求文档"}], ensure_ascii=False),
                    encoding="utf-8")
    return file


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.folder / "data"), "AUTOSKILL_HEADED": "0",
                                                   "AUTOSKILL_BRIDGE": "off", "AUTOSKILL_SESSION": "environment-test"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        for name in ("AUTOSKILL_DELIVERY_DIR", "AUTOSKILL_CALLER_CWD"):
            self.addCleanup(os.environ.pop, name, None)
            os.environ.pop(name, None)
        self.dev, self.prod = _server(), _server()
        for server in (self.dev, self.prod):
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        self.dev_url = f"http://127.0.0.1:{self.dev.server_port}"
        self.prod_url = f"http://127.0.0.1:{self.prod.server_port}"

    def test_requirement_stage_project_delivers_design_and_binds_address_later(self):
        stage = projects.create("设计阶段项目", project_id="design-stage")
        self.assertEqual(stage["base_url"], "")
        file = _design_file(self.folder, "需求阶段用例")
        document = self.folder / "需求文档.md"
        document.write_text("# v1\n", encoding="utf-8")
        written = plan.write(stage["id"], str(file), source_doc=str(document))
        self.assertTrue(Path(written["delivery_excel"]).is_file())
        self.assertTrue(Path(written["initial_excel"]).is_file())
        self.assertEqual(written["cases"], 1)
        duplicate = plan.write(stage["id"], str(file), source_doc=str(document))
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["task_id"], written["task_id"])
        self.assertFalse(plan.write(stage["id"], str(file)).get("duplicate"))  # no document version means a deliberate new task
        case = builder.api_case("API-CHECK", "检查回声", "POST", "/check", {"status": 200})
        projects.save_cases(stage["id"], [case])
        with self.assertRaisesRegex(ValueError, "还没有被测地址"):
            execution_inputs.capture(stage["id"], [case["id"]], "playwright", persist=False)
        projects.update(stage["id"], base_url=self.dev_url)
        self.assertEqual(projects.get(stage["id"])["base_url"], self.dev_url)
        self.assertEqual(execution_inputs.capture(stage["id"], [case["id"]], "playwright", persist=False)["config"]["base_url"], self.dev_url)

    def test_environments_are_validated_and_frozen_with_the_run(self):
        pid = projects.create("环境项目", self.dev_url, project_id="environments")["id"]
        case = builder.api_case("API-ECHO", "回声", "POST", "/check", {"status": 200, "json_equals": {"/echo": "{{greeting}}"}},
                                json={"msg": "{{greeting}}"})
        projects.save_cases(pid, [case])
        invalid = [
            {"environments": [{"id": "a", "name": "A", "is_default": True}, {"id": "b", "name": "B", "is_default": True}], "active_environment_id": "a"},
            {"environments": [{"id": "a", "name": "A"}, {"id": "a", "name": "B"}]},
            {"environments": [{"id": "a", "name": ""}]},
            {"environments": [{"id": "a", "name": "A", "variables": {"1bad": "x"}}]},
            {"environments": [{"id": "a", "name": "A", "variables": {"nested": {"x": 1}}}]},
            {"environments": [{"id": "a", "name": "A"}], "active_environment_id": "missing"},
        ]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                projects.update(pid, **payload)
        projects.update(pid, environments=[
            {"id": "dev", "name": "开发", "base_url": self.dev_url, "variables": {"greeting": "dev-ok"}, "is_default": True},
            {"id": "prod", "name": "生产", "base_url": self.prod_url, "variables": {"greeting": "prod-ok"}}])
        self.assertEqual(projects.get(pid)["environment"]["id"], "dev")
        projects.update(pid, active_environment_id="prod")
        dev = execution_inputs.capture(pid, [case["id"]], "playwright", persist=False)
        self.assertEqual(dev["environment"], {"id": "prod", "name": "生产", "base_url": self.prod_url,
                                              "variables": {"greeting": "prod-ok"}, "is_default": False})
        self.assertEqual(dev["config"]["base_url"], self.prod_url)
        self.assertEqual(dev["config"]["_environment"]["recorded_base_url"], self.dev_url)
        self.assertNotIn("environments", dev["config"])
        self.assertEqual(dev["variables"]["greeting"], "prod-ok")
        explicit = execution_inputs.capture(pid, [case["id"]], "playwright", persist=False, environment_id="dev",
                                            inputs={"greeting": "explicit"})
        self.assertEqual(explicit["config"]["base_url"], self.dev_url)
        self.assertEqual(explicit["variables"]["greeting"], "explicit")
        self.assertNotEqual(dev["scope_hashes"], explicit["scope_hashes"])

        run = runner.create(pid, [case["id"]], api_only=True)
        self.assertEqual(runner.execute(run["id"])["status"], "Pass")
        self.assertEqual(self.prod.requests[-1], {"msg": "prod-ok"})
        self.assertEqual(self.dev.requests, [])
        run = runner.create(pid, [case["id"]], api_only=True, environment_id="dev", inputs={"greeting": "explicit"})
        self.assertEqual(runner.execute(run["id"])["status"], "Pass")
        self.assertEqual(self.dev.requests[-1], {"msg": "explicit"})
        self.assertEqual(execution_inputs.load(run)["environment"]["id"], "dev")

    def test_recorded_urls_and_page_values_follow_the_environment(self):
        self.assertEqual(ir.absolute_url("http://old.example:8080/app?x=1", "http://new.example:9090", "http://old.example:8080"),
                         "http://new.example:9090/app?x=1")
        self.assertEqual(ir.absolute_url("https://other.example/app", "http://new.example:9090", "http://old.example:8080"),
                         "https://other.example/app")
        self.assertEqual(ir.absolute_url("/app", "http://new.example:9090"), "http://new.example:9090/app")
        ir.set_rand("rand1")
        ir.set_variables({"name": "张三", "count": 2})
        self.addCleanup(ir.set_variables, None)
        self.addCleanup(ir.set_rand, "")
        self.assertEqual(ir.expand("{{name}}-{{count}}-{{RAND}}"), "张三-2-rand1")
        self.assertEqual(ir.expand("{{missing}}"), "{{missing}}")

    def test_dashboard_environments_route_switches_the_run_environment(self):
        pid = projects.create("环境接口", self.dev_url, project_id="environment-api")["id"]
        with TestClient(create_app(8787, start_worker=False), base_url="http://127.0.0.1:8787") as client:
            saved = client.put(f"/api/projects/{pid}/environments", json={
                "environments": [{"id": "dev", "name": "开发", "base_url": self.dev_url, "variables": {"k": "v"}, "is_default": True},
                                 {"id": "prod", "name": "生产", "base_url": self.prod_url}],
                "active_environment_id": "prod"})
            self.assertEqual(saved.status_code, 200, saved.text)
            self.assertEqual(saved.json()["environment"]["id"], "prod")
            rejected = client.put(f"/api/projects/{pid}/environments", json={"environments": "not-a-list"})
            self.assertEqual(rejected.status_code, 400)

    def test_delivery_copies_only_the_finished_files_to_the_user_folder(self):
        pid = projects.create("交付项目", self.dev_url, project_id="delivery")["id"]
        user_folder, file = self.folder / "user-project", _design_file(self.folder, "交付用例")
        user_folder.mkdir()
        os.environ["AUTOSKILL_DELIVERY_DIR"] = str(user_folder)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(cli.main(["plan", "write", pid, "--file", str(file)]), 0)
        written = json.loads(stdout.getvalue())
        self.assertEqual(written["delivery"]["folder"], str(user_folder.resolve()))
        self.assertEqual({item.name for item in user_folder.iterdir()}, {"交付项目-功能测试用例.xlsx", "交付项目-功能测试用例-初版.xlsx"})
        self.assertTrue(all(item.suffix == ".xlsx" for item in user_folder.iterdir()))
        self.assertTrue(Path(written["initial_excel"]).is_file())
        self.assertFalse(Path(written["skill"]).resolve().is_relative_to(user_folder.resolve()))
        self.assertFalse(Path(written["excel"]).resolve().is_relative_to(user_folder.resolve()))
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(cli.main(["task-report", written["initial_report_id"]]), 0)
        self.assertEqual({item.suffix for item in user_folder.iterdir()}, {".xlsx", ".md"})

    def test_delivery_folder_follows_the_host_or_explicit_setting(self):
        from autoskill import paths

        caller = self.folder / "workspace"
        with patch.dict(os.environ, {"AUTOSKILL_CALLER_CWD": str(caller), "AUTOSKILL_HOST": "generic"}):
            self.assertIsNone(paths.delivery_dir())
        with patch.dict(os.environ, {"AUTOSKILL_CALLER_CWD": str(caller), "AUTOSKILL_HOST": "dsh"}):
            self.assertEqual(paths.delivery_dir(), caller.resolve())
        explicit = self.folder / "delivered"
        os.environ["AUTOSKILL_DELIVERY_DIR"] = str(explicit)
        os.environ["AUTOSKILL_HOST"] = "generic"
        self.assertEqual(paths.delivery_dir(), explicit.resolve())

    def test_delivery_keeps_a_differently_edited_user_file(self):
        from autoskill import deliverables

        source = self.folder / "报告.md"
        source.write_text("新内容\n", encoding="utf-8")
        user_folder = self.folder / "edited"
        user_folder.mkdir()
        delivered = deliverables.copy([("报告.md", source)], user_folder)
        self.assertFalse(delivered["files"][0].get("kept"))
        source.write_text("更新的内容\n", encoding="utf-8")
        delivered = deliverables.copy([("报告.md", source)], user_folder)
        self.assertTrue(delivered["files"][0]["kept"])
        self.assertEqual((user_folder / "报告.md").read_text(encoding="utf-8"), "新内容\n")
        delivered = deliverables.copy([("报告.md", source)], user_folder, overwrite=True)
        self.assertFalse(delivered["files"][0].get("kept"))
        self.assertEqual((user_folder / "报告.md").read_text(encoding="utf-8"), "更新的内容\n")


if __name__ == "__main__":
    unittest.main()
