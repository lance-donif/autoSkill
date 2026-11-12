"""Case sheet, one project per site, create/read/update/delete replay data, and the captcha reader."""
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))
os.environ.setdefault("AUTOSKILL_HEADED", "0")

from autoskill import captcha, plan, projects
from autoskill.recorder import compile as compiler
from autoskill.recorder import har
from autoskill.replay import api, codegen, ir

BASE = "http://127.0.0.1:9"


def entry(method, path, status, response, body=None, form=None):
    request = {"method": method, "url": BASE + path, "headers": []}
    if body is not None:
        request["postData"] = {"mimeType": "application/json", "text": json.dumps(body)}
    if form is not None:
        request["postData"] = {"mimeType": "application/x-www-form-urlencoded", "text": form}
    content = {"mimeType": "application/json", "text": json.dumps(response)}
    return {"_resourceType": "fetch", "request": request, "response": {"status": status, "content": content}}


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = {k: os.environ.get(k) for k in ("AUTOSKILL_WORKSPACE", "AUTOSKILL_CASE_SKILL")}
        os.environ["AUTOSKILL_WORKSPACE"] = self.tmp.name
        os.environ["AUTOSKILL_CASE_SKILL"] = str(Path(self.tmp.name) / "autoskill-cases")
        self.pid = projects.create("工单", BASE, project_id="t")["id"]

    def tearDown(self):
        for key, value in self.saved.items():
            os.environ.pop(key, None) if value is None else os.environ.__setitem__(key, value)
        self.tmp.cleanup()

    def rec(self, **extra):
        steps = [{"action": "click", "locators": [{"by": "testid", "value": "go"}]},
                 {"assertion": "visible", "locators": [{"by": "testid", "value": "done"}]}]
        return {"project_id": self.pid, "start_url": "/", "steps": steps, "secrets": [], "used_login": False, "as_login": False, **extra}

    def test_one_site_is_one_project(self):
        again = projects.create("另一个名字", "http://www.127.0.0.1:9/orders")
        other = projects.create("别的站点", "http://127.0.0.1:10")
        self.assertEqual((again["id"], again["reused"]), ("t", True))
        self.assertNotEqual(other["id"], "t")

    def test_crud_recording_chains_small_ids_and_keeps_created_data_unique(self):
        traffic = {"log": {"entries": [
            entry("POST", "/api/tickets", 201, {"id": 7, "title": "工单-ab12cd", "status": "open"}, body={"title": "工单-ab12cd"}),
            entry("GET", "/api/tickets", 200, {"items": [{"id": 7, "title": "工单-ab12cd"}], "total": 1}),
            entry("PUT", "/api/tickets/7", 200, {"id": 7, "status": "done"}, body={"status": "done", "page": 7}),
            entry("POST", "/api/tickets/7/close", 200, {"ok": True}, form="reason=dup&ticket_id=7"),
            entry("DELETE", "/api/tickets/7", 204, None),
        ]}}
        compiler.build(self.pid, self.rec(rand="ab12cd"), traffic, None, "工单增删改查")
        made = {c["method"] + " " + c["path"]: c for c in projects.cases(self.pid) if c["kind"] == "api"}
        create, update = made["POST /api/tickets"], made["PUT /api/tickets/{{ticket_id}}"]
        self.assertEqual(create["json"], {"title": "工单-{{RAND}}"})  # a new name on every replay
        self.assertEqual(create["assertions"]["json_equals"]["/title"], "工单-{{RAND}}")
        self.assertEqual(create["extensions"]["extract"], {"ticket_id": "/id"})  # id 7 is small but plainly an id
        self.assertEqual(update["json"], {"status": "done", "page": 7})  # page=7 is not the ticket
        self.assertEqual(made["POST /api/tickets/{{ticket_id}}/close"]["form"], {"reason": "dup", "ticket_id": "{{ticket_id}}"})
        self.assertEqual(projects.depends_on(made["DELETE /api/tickets/{{ticket_id}}"]), [create["id"]])
        self.assertEqual(api.fill(create["json"], {"RAND": "x1"}), {"title": "工单-x1"})

    def test_search_as_you_type_keeps_the_last_request_only(self):
        typed = [entry("GET", f"/ac/?q={'auto'[:n]}", 200, [{"phrase": "a"}]) for n in range(1, 5)]
        kept = har.drop_typeahead(har.entries({"log": {"entries": typed}}, BASE)[0])
        self.assertEqual([e["params"] for e in kept], [{"q": "auto"}])

    def test_rand_and_min_count_reach_the_exported_scripts(self):
        ir.set_rand("zz")
        self.assertEqual(ir.expand("工单-{{RAND}}"), "工单-zz")
        steps = [{"action": "fill", "locators": [{"by": "css", "value": "#t"}], "value": "工单-{{RAND}}"},
                 {"assertion": "min_count", "locators": [{"by": "css", "value": "li"}], "expected": 1}]
        ir.validate({"start_url": "/", "actions": steps[:1], "assertions": steps[1:]})
        for build in (codegen.pw_step, codegen.se_step):
            self.assertIn('"工单-" + RAND', build(steps[0]))
            compile(build(steps[1]), "step", "eval")

    def test_case_sheet_is_written_read_marked_and_replaced(self):
        source = Path(self.tmp.name) / "cases.json"
        source.write_text(json.dumps([{"title": "新增工单", "module": "工单", "type": "新增", "steps": ["打开", "提交"], "expected": "列表出现"},
                                      {"id": "TC-X", "title": "删除工单"}], ensure_ascii=False), encoding="utf-8")
        made = plan.write(self.pid, str(source))
        self.assertTrue(Path(made["excel"]).is_file())
        self.assertIn("name: autoskill-cases", (Path(made["skill"]) / "SKILL.md").read_text(encoding="utf-8"))
        shown = plan.show()
        self.assertEqual([(c["id"], c["module"], c["status"]) for c in shown["cases"]], [("TC-001", "工单", "未执行"), ("TC-X", "未分组", "未执行")])
        self.assertEqual(shown["cases"][0]["steps"], "1. 打开\n2. 提交")
        plan.link("TC-001", self.pid, "TC-UI-001", 1)
        self.assertEqual(plan.mark("TC-X", "阻塞", "需要短信验证码")["summary"], {"通过": 0, "失败": 0, "阻塞": 1, "未执行": 1})
        self.assertEqual(plan.show()["cases"][0]["replay"], "t/TC-UI-001")
        self.assertIsNone(plan.sync(quiet=True))  # nothing replayed yet
        # a hand-made sheet dropped into the same folder is executed the same way
        from openpyxl import Workbook

        Path(made["excel"]).unlink()
        book = Workbook()
        book.active.append(["编号", "用例标题"])
        book.active.append(["A-1", "自己的用例"])
        book.save(Path(made["skill"]) / "我的用例.xlsx")
        self.assertEqual(plan.mark("A-1", "通过")["summary"]["通过"], 1)

    def test_captcha_reader_returns_letters_and_digits(self):
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (120, 40), "white")
        ImageDraw.Draw(image).text((10, 10), "7k3m", fill="black")
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        try:
            self.assertRegex(captcha.solve(buffer.getvalue()), r"^[0-9A-Za-z]+$")
        except ValueError as error:  # the tiny default font may yield nothing: the reader must say so, not guess
            self.assertIn("没有从验证码图片里识别出字符", str(error))


if __name__ == "__main__":
    unittest.main()
