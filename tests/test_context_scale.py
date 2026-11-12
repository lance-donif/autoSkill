"""Real 5,000-case catalogs/sheets keep default agent payloads bounded."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill import cli, db, knowledge, paths, plan, projects  # noqa: E402


class LargeProjectRetrievalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.env = mock.patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": cls.tmp.name,
                                               "AUTOSKILL_CASE_SKILL": str(Path(cls.tmp.name) / "autoskill-cases"),
                                               "AUTOSKILL_BRIDGE": "off"})
        cls.env.start()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.addClassCleanup(cls.env.stop)
        db.init()
        cls.pid = projects.create("规模测试", "http://127.0.0.1:9", project_id="scale")["id"]
        cases = [{"schema_version": "1.1.0", "id": f"TC-UI-{n:05d}", "kind": "ui", "revision": 1,
                  "lifecycle": "active", "requirement_ids": ["REQ-RECORDED"], "module": f"module-{n % 20:02d}",
                  "title": f"业务场景 {n:05d}", "priority": "P1", "preconditions": "准备测试数据 " * 100,
                  "steps": ["PRIVATE-DETAIL-" + str(n) + ": " + "业务操作 " * 100], "test_data": {},
                  "expected": "验证业务结果 " * 100, "source": "synthetic scale regression", "risk": "read", "tags": [],
                  "extensions": {"automation": {"start_url": "/", "actions": [{"action": "goto", "url": "/"}],
                                                 "assertions": [{"assertion": "title", "expected": "Scale"}]}}}
                 for n in range(5000)]
        projects.save_cases(cls.pid, cases)
        source = Path(cls.tmp.name) / "designed-cases.json"
        source.write_text(json.dumps([{"id": case["id"], "module": case["module"], "title": case["title"],
                                       "steps": case["steps"], "expected": case["expected"]} for case in cases]), encoding="utf-8")
        plan.write(cls.pid, str(source))
        folder = projects.path(cls.pid) / "knowledge"
        for n in range(100):
            (folder / f"legacy-{n:03d}.md").write_text(f"# 已确认知识 {n}\n" + ("订单状态规则与流程 " * 500), encoding="utf-8")
        db.insert("runs", {"id": "RUN-SCALE-FAIL", "project_id": cls.pid, "engine": "playwright",
                           "status": "Fail", "created_at": db.now()})

    def invoke(self, argv):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli.main(argv)
        self.assertEqual(code, 0, output.getvalue())
        return json.loads(output.getvalue()), output.getvalue()

    def test_default_context_and_case_sheet_payloads_do_not_grow_with_catalog(self):
        context, text = self.invoke(["context", self.pid])
        self.assertEqual(context["case_pagination"]["total"], 5000)
        self.assertEqual(len(context["cases"]), 30)
        self.assertLessEqual(len(context["knowledge"]), 5)
        self.assertLess(len(text), 20000)
        self.assertNotIn("PRIVATE-DETAIL-", text)
        sheet, text = self.invoke(["plan", "show"])
        self.assertEqual(sheet["pagination"]["total"], 5000)
        self.assertEqual(len(sheet["cases"]), 30)
        self.assertLess(len(text), 15000)
        self.assertNotIn("PRIVATE-DETAIL-", text)
        skill = (paths.case_skill() / "SKILL.md").read_text(encoding="utf-8")
        self.assertLess(len(skill), 2500)
        self.assertIn("5000", skill)
        self.assertNotIn("业务场景 04999", skill)
        project_skill = (projects.path(self.pid) / "SKILL.md").read_text(encoding="utf-8")
        self.assertLess(len(project_skill), 10000)
        self.assertIn("5000", project_skill)
        self.assertNotIn("TC-UI-04999", project_skill)

    def test_pages_are_disjoint_search_reaches_last_case_and_expansion_is_explicit(self):
        first, _ = self.invoke(["cases", self.pid])
        second, _ = self.invoke(["cases", self.pid, "--offset", "30"])
        self.assertFalse({c["id"] for c in first["cases"]} & {c["id"] for c in second["cases"]})
        self.assertEqual(second["pagination"]["next_offset"], 60)
        found, _ = self.invoke(["cases", self.pid, "--query", "04999", "--module", "module-19"])
        self.assertEqual([c["id"] for c in found["cases"]], ["TC-UI-04999"])
        selected, text = self.invoke(["cases", self.pid, "--case", "TC-UI-04999"])
        self.assertEqual(selected["case"]["id"], "TC-UI-04999")
        self.assertIn("PRIVATE-DETAIL-4999", text)
        sheet, _ = self.invoke(["plan", "show", "--case", "TC-UI-04999", "--details"])
        self.assertIn("PRIVATE-DETAIL-4999", sheet["cases"][0]["steps"])

    def test_knowledge_needs_explicit_sources_and_preserves_case_run_evidence(self):
        with self.assertRaisesRegex(ValueError, "来源"):
            knowledge.add(self.pid, "无来源", "不能凭失败猜成规则")
        before = knowledge.search(self.pid, "RUN-SCALE-FAIL")["pagination"]["matched"]
        self.assertEqual(before, 0)  # a failed run did not become knowledge automatically
        made = knowledge.add(self.pid, "订单状态人工确认", "取消订单保留审计记录。", sources=["需求文档 section 4"],
                             case_ids=["TC-UI-04999"], run_ids=["RUN-SCALE-FAIL"])
        found = knowledge.search(self.pid, "审计记录")
        self.assertEqual(found["notes"][0]["file"], made["file"])
        shown = knowledge.show(self.pid, made["file"])
        self.assertIn("取消订单保留审计记录", shown["content"])
        self.assertEqual(shown["metadata"]["case_ids"], ["TC-UI-04999"])
        self.assertEqual(shown["metadata"]["runs"][0]["status"], "Fail")
        self.assertEqual(shown["metadata"]["confirmation"], "explicit_command")

    def test_knowledge_path_and_pagination_boundaries_are_enforced(self):
        for name in ("../outside.md", "/tmp/outside.md", "..\\outside.md"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                knowledge.show(self.pid, name)
        outside = Path(self.tmp.name) / "outside.md"
        outside.write_text("outside content", encoding="utf-8")
        link = projects.path(self.pid) / "knowledge" / "outside-link.md"
        try:
            link.symlink_to(outside)
        except OSError:
            pass
        else:
            with self.assertRaises(ValueError):
                knowledge.show(self.pid, link.name)
            self.assertNotIn(link.name, [note["file"] for note in knowledge.search(self.pid, "outside")["notes"]])
        with self.assertRaises(ValueError):
            cli.case_page(self.pid, limit=5000)
        with self.assertRaises(ValueError):
            plan.show(offset=-1)
        large = projects.path(self.pid) / "knowledge" / "large-legacy.md"
        large.write_text("较长的历史文档" * 12000 + "FINAL-PAGE", encoding="utf-8")
        shown = knowledge.show(self.pid, large.name, offset=84000, limit=100)
        self.assertEqual(shown["content"], "FINAL-PAGE")
        self.assertFalse(shown["has_more"])

    def test_swappable_handmade_sheet_still_works_without_importing_all_rows(self):
        from openpyxl import Workbook

        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ, {"AUTOSKILL_CASE_SKILL": temporary}):
            book = Workbook()
            book.active.append(["编号", "用例标题"])
            book.active.append(["REPLACED-1", "替换后的用例"])
            book.save(Path(temporary) / "replacement.xlsx")
            shown = plan.show(details=False)
            self.assertEqual(shown["pagination"]["total"], 1)
            self.assertEqual(shown["cases"][0]["id"], "REPLACED-1")
            self.assertEqual(plan.mark("REPLACED-1", "阻塞", "等待业务确认")["summary"]["阻塞"], 1)


if __name__ == "__main__":
    unittest.main()
