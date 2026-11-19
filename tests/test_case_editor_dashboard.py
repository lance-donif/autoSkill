"""Edit canonical API/UI definitions through real Chromium forms and keep stale drafts safe."""
import copy
import re
import time

from playwright.sync_api import expect
from test_load_dashboard import DashboardWorkspace

from autoskill import builder, case_document, projects


class CaseEditorDashboardTests(DashboardWorkspace):
    def seed_api(self):
        case = builder.api_case("TC-EDIT-API", "退款结果查询", "GET", "/slow", {
            "status": 200, "json_equals": {"/ok": True}, "body_contains": ['"ok"'],
            "header_contains": {"content-type": "application/json"},
            "schema": {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}, "max_ms": 2000,
        }, headers={"X-Test": "original"}, params={"search": "old", "page": 1},
            json={"cart": {"quantity": 1, "enabled": True}, "notes": ["first", None]},
            extract={"refund_ok": "/ok"}, depends_on=["TC-BEGIN"], test_case_document={
                "description": "查询已经提交的退款结果", "preconditions": "退款已提交", "steps": ["进入退款详情", "查看退款结果"],
                "expected": "展示退款结果", "author": "原作者", "remarks": "保留备注",
            })
        case["extensions"]["compatibility"] = {"nested": [1, False, {"untouched": "original"}]}
        case["extensions"]["oracle"] = {"source": "requirement", "ref": "REQ-RECORDED"}
        projects.save_cases(self.pid, [case])
        return case

    def editor(self, identity):
        self.open("cases?range=all")
        self.page.get_by_role("row").filter(has=self.page.get_by_role("checkbox", name="选择 " + identity, exact=True)).click()
        self.page.get_by_role("dialog").get_by_role("button", name="编辑用例", exact=True).click()
        dialog = self.page.get_by_role("dialog", name="编辑用例", exact=True)
        expect(dialog).to_be_visible()
        return dialog

    def save_editor(self, dialog, status=200):
        with self.page.expect_response(lambda response: response.request.method == "PUT" and f"/projects/{self.pid}/cases/" in response.url) as event:
            dialog.get_by_role("button", name="保存修改", exact=True).click()
        self.assertEqual(event.value.status, status, event.value.text())
        return event.value.json()

    def definition(self, identity):
        response = self.client.get(f"/api/case-definitions/{self.pid}/{identity}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_api_form_preserves_full_definition_and_requires_manual_run(self):
        original = self.seed_api()
        before_runs = self.client.get("/api/runs").json()["total"]
        skill = projects.path(self.pid) / "SKILL.md"
        before_skill = skill.read_bytes() if skill.exists() else None
        dialog = self.editor(original["id"])
        dialog.get_by_label("功能描述", exact=True).fill("人工维护的退款查询功能")
        dialog.get_by_label("前置条件", exact=True).fill("用户已完成退款申请")
        dialog.get_by_label("功能测试步骤", exact=True).fill("进入退款详情\n核对退款结果")
        dialog.get_by_label("预期结果", exact=True).fill("展示当前退款处理结果")
        dialog.get_by_label("请求头.X-Test", exact=True).fill("edited")
        dialog.get_by_label("查询参数.search", exact=True).fill("退款")
        dialog.get_by_label("查询参数.page", exact=True).fill("2")
        dialog.get_by_label("JSON 请求体.cart.quantity", exact=True).fill("3")
        dialog.get_by_label("JSON 请求体.cart.enabled", exact=True).uncheck()
        dialog.get_by_label("JSON 请求体.notes[0]", exact=True).fill("保留人工期望")
        result = self.save_editor(dialog)
        self.assertTrue(result["changed"])
        self.assertEqual(result["verification"]["status"], "NotRun")
        expected = copy.deepcopy(original)
        expected["revision"] += 1
        expected["headers"]["X-Test"] = "edited"
        expected["params"] = {"search": "退款", "page": 2}
        expected["json"] = {"cart": {"quantity": 3, "enabled": False}, "notes": ["保留人工期望", None]}
        expected = case_document.apply(expected, {**original["extensions"]["test_case_document"], "description": "人工维护的退款查询功能",
            "preconditions": "用户已完成退款申请", "steps": ["进入退款详情", "核对退款结果"], "expected": "展示当前退款处理结果"})
        expected["extensions"]["functional_design"] = {"state": "unmapped", "reason": "业务预期已手工修改；既有技术断言是否覆盖新预期尚未确认"}
        self.assertEqual(self.definition(original["id"])["case"], expected)
        self.assertEqual(self.client.get("/api/runs").json()["total"], before_runs)
        self.assertEqual(skill.read_bytes() if skill.exists() else None, before_skill)
        detail = self.page.get_by_role("dialog", name=original["title"], exact=True)
        expect(detail).to_contain_text("当前版本尚待验证")
        with self.page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/api/runs")) as event:
            detail.get_by_role("button", name="运行这条用例", exact=True).click()
        identity = event.value.json()["runs"][0]["id"]
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            run = self.client.get("/api/runs/" + identity).json()
            if run["status"] not in ("Running", "Queued"):
                break
            time.sleep(0.05)
        self.assertEqual(run["status"], "Pass", run)
        self.assertEqual(run["total"], 1)

    def test_request_body_form_switch_and_validation_error_recover(self):
        original = self.seed_api()
        dialog = self.editor(original["id"])
        dialog.get_by_label("请求路径", exact=True).fill("invalid-path")
        dialog.get_by_label("请求体类型", exact=True).select_option("form")
        dialog.get_by_role("button", name="添加字段：表单请求体", exact=True).click()
        dialog.get_by_label("表单请求体 字段名 1", exact=True).fill("message")
        dialog.get_by_label("表单请求体.新字段", exact=True).fill("人工录入")
        self.save_editor(dialog, 400)
        expect(dialog.get_by_role("alert")).to_contain_text("path")
        expect(dialog.get_by_label("表单请求体.新字段", exact=True)).to_have_value("人工录入")
        dialog.get_by_label("请求路径", exact=True).fill("/slow")
        result = self.save_editor(dialog)
        self.assertEqual(result["case"]["form"], {"message": "人工录入"})
        self.assertNotIn("json", result["case"])
        self.assertEqual(result["case"]["assertions"], original["assertions"])

    def test_conflict_keeps_draft_and_requires_explicit_discard_to_read_latest(self):
        original = self.seed_api()
        dialog = self.editor(original["id"])
        dialog.get_by_label("功能描述", exact=True).fill("本地不应丢失的草稿")
        current = self.definition(original["id"])
        remote = copy.deepcopy(current["case"])
        remote["title"] = "另一会话保存的标题"
        response = self.client.put(f"/api/projects/{self.pid}/cases/{original['id']}", json={
            "case": remote, "expected_content_revision": current["content_revision"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.save_editor(dialog, 409)
        expect(dialog.get_by_role("alert")).to_contain_text("本地草稿已保留")
        expect(dialog.get_by_label("功能描述", exact=True)).to_have_value("本地不应丢失的草稿")
        self.assertEqual(self.definition(original["id"])["case"]["title"], remote["title"])
        dialog.get_by_role("button", name="放弃草稿并读取最新版本", exact=True).click()
        expect(dialog.get_by_label("功能描述", exact=True)).to_have_value("本地不应丢失的草稿")
        dialog.get_by_role("button", name="继续编辑", exact=True).click()
        dialog.get_by_role("button", name="放弃草稿并读取最新版本", exact=True).click()
        dialog.get_by_role("button", name="放弃修改", exact=True).click()
        dialog = self.page.get_by_role("dialog", name="编辑用例", exact=True)
        expect(dialog).to_contain_text("第 2 版")
        dialog.get_by_text("基本信息与测试数据", exact=True).click()
        expect(dialog.get_by_label("用例标题", exact=True)).to_have_value(remote["title"])
        expect(dialog.get_by_label("功能描述", exact=True)).to_have_value(original["extensions"]["test_case_document"]["description"])

    def test_unsaved_close_escape_route_and_refresh_keep_draft(self):
        original = self.seed_api()
        dialog = self.editor(original["id"])
        dialog.get_by_label("功能描述", exact=True).fill("受保护的草稿")
        self.page.keyboard.press("Escape")
        expect(dialog.get_by_role("alert")).to_contain_text("有未保存的修改")
        dialog.get_by_role("button", name="继续编辑", exact=True).click()
        expect(self.page.locator("#view")).not_to_have_attribute("aria-busy", "true")
        expect(self.page.locator("#view h1")).to_have_text("测试用例")
        self.page.evaluate("window.dispatchEvent(new HashChangeEvent('hashchange'))")
        expect(dialog.get_by_label("功能描述", exact=True)).to_have_value("受保护的草稿")
        self.page.evaluate("location.hash = '#/runs'")
        expect(self.page).to_have_url(re.compile(r"#/cases\?range=all$"))
        expect(dialog.get_by_role("alert")).to_contain_text("有未保存的修改")
        dialog.get_by_role("button", name="继续编辑", exact=True).click()
        expect(self.page.locator("#view")).not_to_have_attribute("aria-busy", "true")
        expect(self.page.locator("#view h1")).to_have_text("测试用例")
        event = self.page.evaluate("""() => { const event = new Event('beforeunload', {cancelable:true});
            window.dispatchEvent(event); return event.defaultPrevented; }""")
        self.assertTrue(event)
        self.page.evaluate("window.__editorReloadGuard = 'original-page'")
        with self.page.expect_event("dialog") as before_unload:
            self.page.evaluate("setTimeout(() => location.reload(), 0)")
        self.assertEqual(before_unload.value.type, "beforeunload")
        before_unload.value.dismiss()
        self.assertEqual(self.page.evaluate("window.__editorReloadGuard"), "original-page")
        expect(dialog.get_by_label("功能描述", exact=True)).to_have_value("受保护的草稿")
        dialog.get_by_role("button", name="取消", exact=True).click()
        expect(dialog.get_by_label("功能描述", exact=True)).to_have_value("受保护的草稿")
        dialog.get_by_role("button", name="放弃修改", exact=True).click()
        expect(dialog).not_to_be_visible()
        expect(self.page).to_have_url(re.compile(r"#/cases\?range=all$"))
        expect(self.page.locator("#view h1")).to_have_text("测试用例")
        self.assertEqual(self.definition(original["id"])["case"], original)

    def test_discarding_a_route_draft_navigates_after_guard_is_removed(self):
        original = self.seed_api()
        dialog = self.editor(original["id"])
        dialog.get_by_label("功能描述", exact=True).fill("只在本地的修改")
        self.page.evaluate("location.hash = '#/runs'")
        expect(dialog.get_by_role("alert")).to_contain_text("有未保存的修改")
        expect(self.page.locator("#view")).not_to_have_attribute("aria-busy", "true")
        expect(self.page.locator("#view h1")).to_have_text("测试用例")
        dialog.get_by_role("button", name="放弃修改", exact=True).click()
        expect(dialog).not_to_be_visible()
        expect(self.page).to_have_url(re.compile(r"#/runs$"))
        expect(self.page.locator("#view h1")).to_have_text("执行记录")
        self.assertEqual(self.definition(original["id"])["case"], original)
        clean = self.editor(original["id"])
        self.page.evaluate("location.hash = '#/runs'")
        expect(clean).not_to_be_visible()
        expect(self.page).to_have_url(re.compile(r"#/runs$"))
        expect(self.page.locator("#view h1")).to_have_text("执行记录")

    def test_ui_actions_keep_identity_inline_assertions_edit_and_xpath_context_roundtrip(self):
        original = builder.ui_case("TC-EDIT-UI", "页面退款结果", {"start_url": "/ui", "actions": [
            {"action": "click", "name": "提交核对", "locators": [{"by": "testid", "value": "check"}], "compatibility": {"keep": True}},
            {"assertion": "visible", "locators": [{"by": "testid", "value": "result"}]},
            {"action": "wait_visible", "locators": [{"by": "testid", "value": "result"}], "recording_result": {"actual": "通过", "passed": True}},
        ], "assertions": [{"assertion": "text", "locators": [{"by": "testid", "value": "result"}], "expected": "通过"}]})
        projects.save_cases(self.pid, [original])
        dialog = self.editor(original["id"])
        self.assertEqual(dialog.get_by_role("button", name="删除断言", exact=True).count(), 2)
        self.assertEqual(dialog.get_by_role("button", name=re.compile("新增动作|删除动作|复制动作|上移动作|下移动作")).count(), 0)
        dialog.get_by_label("步骤 1 操作类型", exact=True).select_option("dblclick")
        dialog.get_by_label("步骤 1 元素 候选1 定位方式", exact=True).select_option("xpath")
        dialog.get_by_label("步骤 1 元素 候选1 定位值", exact=True).fill("./button")
        dialog.get_by_label("步骤 1 元素 候选1 XPath 元素锚点", exact=True).fill("#actions")
        dialog.get_by_label("步骤 1 元素 候选1 第几个", exact=True).fill("2")
        dialog.get_by_role("button", name="添加上下文：步骤 1", exact=True).click()
        dialog.get_by_label("步骤 1 上下文1 类型", exact=True).select_option("frame")
        dialog.get_by_label("步骤 1 上下文1 候选1 定位值", exact=True).fill("#payment-frame")
        dialog.get_by_role("button", name="添加上下文：步骤 1", exact=True).click()
        dialog.get_by_label("步骤 1 上下文2 类型", exact=True).select_option("shadow")
        dialog.get_by_label("步骤 1 上下文2 候选1 定位值", exact=True).fill("payment-panel")
        dialog.get_by_role("button", name="删除断言", exact=True).first.click()
        dialog.get_by_role("button", name="添加结束断言", exact=True).click()
        dialog.get_by_label("结束断言 2 元素 候选1 定位值", exact=True).fill("[data-testid='result']")
        result = self.save_editor(dialog)
        automation = result["case"]["extensions"]["automation"]
        self.assertEqual([step["action"] for step in automation["actions"]], ["dblclick", "wait_visible"])
        self.assertEqual(automation["actions"][0]["compatibility"], {"keep": True})
        self.assertEqual(automation["actions"][1]["recording_result"], original["extensions"]["automation"]["actions"][2]["recording_result"])
        self.assertEqual(automation["actions"][0]["locators"], [{"by": "xpath", "value": "./button", "xpath_anchor": "#actions", "nth": 1}])
        self.assertEqual(automation["actions"][0]["context"], [
            {"kind": "frame", "locators": [{"by": "css", "value": "#payment-frame"}]},
            {"kind": "shadow", "locators": [{"by": "css", "value": "payment-panel"}]},
        ])
        self.assertEqual(len(automation["assertions"]), 2)
        self.assertFalse(any("_edit_id" in step for step in automation["actions"]))

    def test_type_switch_cleans_old_parameters_then_manual_ui_run_passes(self):
        original = builder.ui_case("TC-EDIT-SWITCH", "检查页面提交结果", {"start_url": "/ui", "actions": [
            {"action": "fill", "locators": [{"by": "testid", "value": "check"}], "value": "old input", "retained": {"unknown": True}},
        ], "assertions": [{"assertion": "text", "locators": [{"by": "testid", "value": "result"}], "expected": "通过"}]})
        projects.save_cases(self.pid, [original])
        dialog = self.editor(original["id"])
        self.page.set_viewport_size({"width": 712, "height": 900})
        dialog.get_by_label("步骤 1 操作类型", exact=True).select_option("click")
        dialog.get_by_label("结束断言 1 判断方式", exact=True).select_option("eventually")
        dialog.get_by_label("启用结束断言 1 等待时间（秒）", exact=True).check()
        dialog.get_by_label("结束断言 1 等待时间（秒）", exact=True).fill("3")
        dialog.get_by_label("启用结束断言 1 连续成立时间（秒）", exact=True).check()
        dialog.get_by_label("结束断言 1 连续成立时间（秒）", exact=True).fill("0.1")
        dimensions = dialog.evaluate("""node => ({width:node.clientWidth, scroll:node.scrollWidth,
            body:node.querySelector('.drawer-body').clientWidth, bodyScroll:node.querySelector('.drawer-body').scrollWidth})""")
        self.assertLessEqual(dimensions["scroll"], dimensions["width"])
        self.assertLessEqual(dimensions["bodyScroll"], dimensions["body"])
        self.evidence("case-editor-narrow-ui.png")
        result = self.save_editor(dialog)
        action = result["case"]["extensions"]["automation"]["actions"][0]
        self.assertNotIn("value", action)
        self.assertEqual(action["action"], "click")
        self.assertEqual(action["retained"], {"unknown": True})
        detail = self.page.get_by_role("dialog", name=original["title"], exact=True)
        with self.page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/api/runs")) as event:
            detail.get_by_role("button", name="运行这条用例", exact=True).click()
        run_id = event.value.json()["runs"][0]["id"]
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            run = self.client.get("/api/runs/" + run_id).json()
            if run["status"] not in ("Queued", "Running"):
                break
            time.sleep(0.05)
        self.assertEqual(run["status"], "Pass", run)

    def test_no_change_save_preserves_version_and_definition(self):
        original = self.seed_api()
        result = self.save_editor(self.editor(original["id"]))
        self.assertFalse(result["changed"])
        self.assertEqual(result["case"], original)
        self.assertEqual(self.definition(original["id"])["case"], original)

    def test_captcha_and_transition_context_inheritance_preserve_explicit_root(self):
        original = builder.ui_case("TC-EDIT-CONTEXT", "验证码与状态校验", {"start_url": "/ui", "actions": [
            {"action": "captcha", "locators": [{"by": "css", "value": "#code"}],
             "context": [{"kind": "frame", "locators": [{"by": "css", "value": "#form-frame"}]}],
             "image": [{"by": "css", "value": "#captcha"}],
             "image_context": [{"kind": "frame", "locators": [{"by": "css", "value": "#image-frame"}]}]},
        ], "assertions": [{"assertion": "checked", "expected": False, "locators": [{"by": "css", "value": "#agree"}],
                           "context": [{"kind": "shadow", "locators": [{"by": "css", "value": "agreement-panel"}]}],
                           "transition_from": {"assertion": "checked", "expected": True}}]})
        projects.save_cases(self.pid, [original])
        untouched = self.save_editor(self.editor(original["id"]))
        self.assertFalse(untouched["changed"])
        self.assertEqual(untouched["case"], original)
        self.page.get_by_role("dialog", name=original["title"], exact=True).get_by_role("button", name="编辑用例", exact=True).click()
        dialog = self.page.get_by_role("dialog", name="编辑用例", exact=True)
        dialog.get_by_label("步骤 1 图片沿用步骤上下文", exact=True).check()
        dialog.get_by_label("沿用父断言上下文", exact=True).uncheck()
        dialog.get_by_label("结束断言 1 断言类型", exact=True).select_option("count")
        dialog.get_by_label("结束断言 1 预期值", exact=True).fill("1")
        result = self.save_editor(dialog)
        automation = result["case"]["extensions"]["automation"]
        self.assertNotIn("image_context", automation["actions"][0])
        self.assertEqual(automation["actions"][0]["context"], original["extensions"]["automation"]["actions"][0]["context"])
        self.assertEqual(automation["assertions"][0]["assertion"], "count")
        self.assertEqual(automation["assertions"][0]["expected"], 1)
        self.assertEqual(automation["assertions"][0]["transition_from"], {"assertion": "checked", "expected": True, "context": []})
