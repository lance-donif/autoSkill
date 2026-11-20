"""Bounded agent reads and actual bridge commands preserve caller inputs and runtime identity."""
import base64
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "skills/autoskill/app"
sys.path.insert(0, str(APP))

from autoskill import bridge, builder, cli, command_input, context_service, paging, paths, plan, projects
from autoskill.server.app import create_app


class AgentReadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.root / "workspace"), "AUTOSKILL_BRIDGE": "off",
                                                   "AUTOSKILL_CASE_SKILL": str(self.root / "sheet"), "AUTOSKILL_INPUT_MAP": "",
                                                   "AUTOSKILL_CALLER_CWD": "", "AUTOSKILL_RUNTIME_CONTEXT": ""})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.pid = projects.create("分页", "http://127.0.0.1:9", project_id="input")["id"]

    def make_cases(self, count=67):
        cases = [builder.api_case(f"TC-API-{number:03}", f"用例 {number}", "GET", "/", {"status": 200, "json_equals": {"/ok": True}})
                 for number in range(count)]
        projects.save_cases(self.pid, cases)

    def command(self, *argv, stdin=None, extra=None):
        environment = {**os.environ, "PYTHONPATH": str(APP), **(extra or {})}
        result = subprocess.run([sys.executable, "-m", "autoskill", *argv], cwd=self.root, env=environment, input=stdin,
                                capture_output=True, text=True, encoding="utf-8", check=False, timeout=30)
        return result.returncode, json.loads(result.stdout)

    def test_case_pages_full_details_and_legacy_python_contract(self):
        self.make_cases()
        first = context_service.cases(self.pid, limit=20)
        self.assertEqual((len(first["cases"]), first["page"]["total"], first["page"]["has_more"]), (20, 67, True))
        seen, page = list(first["cases"]), first["page"]
        while page["next_cursor"]:
            result = context_service.cases(self.pid, limit=20, cursor=page["next_cursor"])
            seen.extend(result["cases"])
            page = result["page"]
        self.assertEqual(len({item["id"] for item in seen}), 67)
        self.assertEqual(len(cli.context(self.pid)["cases"]), 67)
        code, limited = self.command("cases", self.pid)
        self.assertEqual((code, len(limited["cases"]), limited["page"]["total"]), (0, 50, 67))
        self.assertEqual(len(self.command("cases", self.pid, "--all")[1]["cases"]), 67)
        detail = self.command("cases", self.pid, "--case", "TC-API-000")[1]
        self.assertEqual(detail["case"]["assertions"]["json_equals"], {"/ok": True})
        original = projects.case(self.pid, "TC-API-000")
        projects.save_cases(self.pid, [{**original, "title": "已修改"}])
        with self.assertRaises(paging.PageError) as raised:
            context_service.cases(self.pid, cursor=first["page"]["next_cursor"], limit=20)
        self.assertEqual(raised.exception.code, "cursor_expired")

    def test_knowledge_is_indexed_then_read_by_unicode_offsets(self):
        folder = projects.path(self.pid) / "knowledge"
        text = "工单🙂甲\r\n" * 1000
        (folder / "rules.md").write_bytes(text.encode("utf-8"))
        first = context_service.knowledge(self.pid)
        self.assertNotIn("content", first["knowledge"][0])
        revision = first["knowledge"][0]["content_revision"]
        part = context_service.read_knowledge(self.pid, "knowledge/rules.md", offset=3, length=17, content_revision=revision)
        self.assertEqual(part["content"], text[3:20])
        self.assertEqual(part["total_length"], len(text))
        self.assertEqual(len(cli.knowledge(self.pid, limit=23)[0]["content"]), 23)
        (folder / "rules.md").write_text("changed", encoding="utf-8")
        with self.assertRaises(paging.PageError):
            context_service.read_knowledge(self.pid, "rules.md", content_revision=revision)

    def test_http_and_cli_share_case_pages_and_preserve_request_session(self):
        self.make_cases(3)
        with TestClient(create_app(start_worker=False), base_url="http://127.0.0.1:8787") as client:
            response = client.get(f"/api/case-definitions/{self.pid}?limit=2")
            self.assertEqual(response.json(), context_service.cases(self.pid, limit=2))
            context = client.get(f"/api/context/{self.pid}?limit=1").json()
            self.assertEqual((len(context["cases"]), context["page"]["cases"]["total"]), (1, 3))

    def test_bridge_file_forms_and_stdin_do_not_rewrite_equal_project_name(self):
        caller = self.root / "caller"
        caller.mkdir()
        source = caller / self.pid
        source.write_text(json.dumps([{"title": "第一行\n第二行 中文"}]), encoding="utf-8")
        for file_args in (["--file", self.pid], ["--file=" + self.pid]):
            argv = ["plan", "write", self.pid, *file_args]
            args = cli.parser().parse_args(["--input-base", str(caller), *argv])
            forwarded, stdin_text, attachments = command_input.for_bridge(args, argv)
            self.assertEqual(forwarded[2], self.pid)
            response = bridge.execute(forwarded, stdin_text, caller_cwd=str(caller), attachments=attachments)
            self.assertEqual(response["exit"], 0, response)
            self.assertEqual(plan.show()["cases"][0]["title"], "第一行\n第二行 中文")
        code, result = self.command("plan", "write", self.pid, "--file", "-", stdin='[\n{"title":"从标准输入"}\n]')
        self.assertEqual((code, result["cases"]), (0, 1))

    def test_counted_plan_rejects_split_steps_before_creating_or_replacing_a_delivery(self):
        parts = [{"id": key, "title": title} for key, title in
                 (("OPEN", "访问网站"), ("ARTICLE", "打开文章"), ("IMAGE", "定位流程图并截图"))]
        design = json.dumps(parts, ensure_ascii=False)
        write = ("plan", "write", self.pid, "--file", "-")
        empty = self.root / "not-initialized"
        new_sheet = self.root / "new-sheet"
        for count, message in (("1", "实际有 3 条"), ("0", "正整数")):
            code, rejected = self.command(*write, "--expected-cases", count, stdin=design,
                                          extra={"AUTOSKILL_WORKSPACE": str(empty), "AUTOSKILL_CASE_SKILL": str(new_sheet)})
            self.assertEqual((code, rejected["code"]), (2, "validation_error"))
            self.assertIn(message, rejected["error"])
        self.assertFalse(empty.exists())
        self.assertFalse(new_sheet.exists())

        # A legacy multi-row plan remains readable and editable after a rejected replacement.
        code, original = self.command(*write, stdin=design)
        self.assertEqual((code, original["cases"]), (0, 3))
        roots = (self.root / "workspace", self.root / "sheet")

        def contents():
            return {path: hashlib.sha256(path.read_bytes()).digest() for root in roots for path in root.rglob("*") if path.is_file()}

        before = contents()
        code, rejected = self.command(*write, "--expected-cases", "1", stdin=design)
        self.assertEqual((code, rejected["code"]), (2, "validation_error"))
        for detail in ("期望 1 条", "实际有 3 条", "steps", "expected"):
            self.assertIn(detail, rejected["error"])
        self.assertEqual(contents(), before)
        self.assertEqual(plan.show()["task_id"], original["task_id"])
        code, marked = self.command("plan", "mark", "OPEN", "未执行", "--note", "等待资料")
        self.assertEqual((code, marked["case"]), (0, "OPEN"))
        self.assertEqual(plan.show()["cases"][0]["note"], "等待资料")

    def test_counted_plan_keeps_one_ordered_flow_and_accepts_independent_states_from_the_same_source(self):
        steps = ["访问网站", "打开指定文章", "定位指定内容", "截取目标内容"]
        expected = ["1. 入口可见", "2. 标题匹配指定文章", "3. 目标段落可见", "4. 生成一张目标截图"]
        flow = {"id": "ARTICLE", "title": "取得指定文章的内容截图", "steps": steps, "expected": expected}
        source = self.root / "design.json"
        source.write_text(json.dumps([flow], ensure_ascii=False), encoding="utf-8")
        argv = ["plan", "write", self.pid, "--file", source.name, "--expected-cases", "1"]
        args = cli.parser().parse_args(["--input-base", str(self.root), *argv])
        forwarded, stdin, attachments = command_input.for_bridge(args, argv)
        reply = bridge.execute(forwarded, stdin, caller_cwd=str(self.root), attachments=attachments)
        self.assertEqual(reply["exit"], 0, reply)
        made = json.loads(reply["stdout"])
        self.assertEqual((made["cases"], made["test_report"]["status"]), (1, "ready"))
        frozen = Path(made["initial_excel"]).read_bytes()
        row, = plan.show()["cases"]
        self.assertEqual(row["steps"].splitlines(), [f"{number}. {text}" for number, text in enumerate(steps, 1)])
        self.assertEqual((row["id"], row["expected"].splitlines(), row["status"]), ("ARTICLE", expected, "未执行"))
        book = load_workbook(made["initial_excel"], read_only=True)
        self.addCleanup(book.close)
        sheet = book["功能测试用例"]
        self.assertEqual(sheet.max_column, 14)
        self.assertEqual([cells[5:7] for cells in sheet.values if cells[9] == "未执行"], [(row["steps"], row["expected"])])

        both = [{"id": key, "title": title, "source": "同一份访问要求", "requirement_ids": ["R-1"]}
                for key, title in (("PUBLIC", "访客可查看公开稿"), ("PRIVATE", "访客打开私密稿被拒绝"))]
        code, separate = self.command("plan", "write", self.pid, "--expected-cases", "2", "--file", "-",
                                      stdin=json.dumps(both, ensure_ascii=False))
        self.assertEqual((code, separate["cases"]), (0, 2))
        self.assertEqual([item["id"] for item in plan.show()["cases"]], ["PUBLIC", "PRIVATE"])
        self.assertEqual(Path(made["initial_excel"]).read_bytes(), frozen)

    def test_attachment_snapshot_survives_original_removal_and_preserves_binary(self):
        caller = self.root / "caller"
        caller.mkdir()
        source = caller / "需求.bin"
        source.write_bytes(b"\x00\xfforiginal")
        spec = caller / "spec.json"
        spec.write_text(json.dumps({"sources": [{"file": source.name}], "artifacts": [], "metadata": {"file": "not-a-path"}}), encoding="utf-8")
        argv = ["workflow", "start", self.pid, "--file=" + str(spec), "--idempotency-key", "request"]
        args = cli.parser().parse_args(argv)
        forwarded, stdin_text, attachments = command_input.for_bridge(args, argv)
        source.unlink()
        mapping = command_input.materialize_attachments(attachments, self.root / "transport")
        self.assertEqual(Path(mapping[str(source)]).read_bytes(), b"\x00\xfforiginal")
        self.assertIn("--file=-", forwarded)
        with patch.dict(os.environ, {"AUTOSKILL_INPUT_MAP": json.dumps(mapping)}), patch("sys.stdin", io.StringIO(stdin_text)):
            decoded = command_input.read_json("-", command="workflow", action="start", base=caller)
        self.assertEqual(decoded["sources"][0]["file"], mapping[str(source)])
        self.assertEqual(decoded["metadata"]["file"], "not-a-path")

    def test_runtime_context_selects_the_configured_workspace_and_usage_errors_are_json(self):
        token = base64.urlsafe_b64encode(json.dumps({"version": 1, "workspace": str(paths.workspace()), "port": 8876}).encode()).decode().rstrip("=")
        code, result = self.command("--runtime-context=" + token, "project", "show", self.pid,
                                    extra={"AUTOSKILL_WORKSPACE": str(self.root / "other")})
        self.assertEqual((code, result["id"]), (0, self.pid))
        code, result = self.command("workflow", "submit")
        self.assertEqual((code, result["code"]), (2, "validation_error"))


if __name__ == "__main__":
    unittest.main()
