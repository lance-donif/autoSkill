"""Build and optionally execute a folder-organized project for Selenium's official public fixtures."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill import builder, case_folders, data_store, db, project_materials, projects, reporting, runner  # noqa: E402  # Run against this checkout's application package.
from autoskill.core import store  # noqa: E402  # Run against this checkout's application package.

BASE = "https://www.selenium.dev/selenium/web/"
FORM = BASE + "web-form.html"
OFFICIAL_SOURCES = ("web-form.html", "dynamic.html", "submitted-form.html")


def action(operation: str, selector: str | None = None, **fields) -> dict:
    step = {"action": operation, **fields}
    if selector:
        step["locators"] = [{"by": "css", "value": selector}]
    return step


def check(assertion: str, selector: str | None = None, **fields) -> dict:
    step = {"assertion": assertion, **fields}
    if selector:
        step["locators"] = [{"by": "css", "value": selector}]
    return step


def functional_designs() -> list[dict]:
    form = "进入 Selenium 官方测试站点的【Web form】页面。"
    dynamic = "进入 Selenium 官方测试站点的动态交互演示页面。"
    designs = [
        ([form, "在【Text input】输入“Selenium-{{RAND}}”。"], "2. 【Text input】显示本次输入的完整内容“Selenium-{{RAND}}”。"),
        ([form, "在【Text input】输入“自动化测试2026”。"], "2. 【Text input】完整显示“自动化测试2026”。"),
        ([form, "在【Textarea】输入两行内容：第一行为“第一行”，第二行为“第二行-{{RAND}}”。"],
         "2. 【Textarea】保留两行内容及两行之间的换行。"),
        ([form, "在【Text input】输入“原始内容”。", "将【Text input】的内容替换为“替换内容”。"],
         "3. 【Text input】仅显示“替换内容”，不再保留“原始内容”。"),
        ([form, "在【Dropdown (datalist)】输入候选城市“New York”。"], "2. 【Dropdown (datalist)】显示“New York”。"),
        ([form, "在【Dropdown (select)】选择【Two】。"], "2. 下拉框当前选中【Two】，对应官方选项值为2。"),
        ([form, "勾选【Default checkbox】。"], "2. 【Default checkbox】处于已勾选状态。"),
        ([form, "取消勾选【Checked checkbox】。"], "2. 【Checked checkbox】处于未勾选状态。"),
        ([form, "选择【Default radio】。"], "2. 【Default radio】已选中，同组【Checked radio】不再选中。"),
        ([form, "查看【Disabled input】的可编辑状态。"], "2. 【Disabled input】保持禁用状态。"),
        ([form, "查看【Readonly input】的内容和可编辑状态。"], "2. 输入框显示“Readonly input”，并保持只读状态。"),
        ([form, "在【Date picker】输入日期“12/31/2026”。"], "2. 【Date picker】显示输入的日期“12/31/2026”。"),
        ([form, "将焦点移到【Example range】，按一次向右方向键。"], "2. 【Example range】的值由5增加为6。"),
        ([form, "在【Text input】输入“Selenium-{{RAND}}”。", "点击【Submit】。"],
         "3. 进入表单提交确认页面，并显示确认文字“Received!”。"),
        ([form, "点击【Return to index】。"], "2. 进入 Selenium 官方测试页面索引。"),
        ([dynamic, "点击【Add a box!】。", "查看新方块出现后的页面状态。"], "3. 页面显示本次操作新增的红色方块。"),
        ([dynamic, "点击【Reveal a new input】。", "在新出现的输入框中输入“Dynamic-{{RAND}}”。"],
         "3. 新出现的输入框显示完整内容“Dynamic-{{RAND}}”。"),
    ]
    return [{"steps": steps, "expected": expected} for steps, expected in designs]


def definitions() -> list[tuple[tuple[str, ...], dict]]:
    specifications = [
        (("表单", "文本输入"), "文本输入保存英文字母与随机测试标识", FORM,
         [action("fill", "[name=my-text]", value="Selenium-{{RAND}}")],
         [check("value", "[name=my-text]", expected="Selenium-{{RAND}}")]),
        (("表单", "文本输入"), "文本输入保存中文与数字", FORM,
         [action("fill", "[name=my-text]", value="自动化测试2026")],
         [check("value", "[name=my-text]", expected="自动化测试2026")]),
        (("表单", "文本输入"), "多行文本保留两行输入", FORM,
         [action("fill", "[name=my-textarea]", value="第一行\n第二行-{{RAND}}")],
         [check("value", "[name=my-textarea]", expected="第一行\n第二行-{{RAND}}")]),
        (("表单", "文本输入"), "重新填写文本替换原有内容", FORM,
         [action("fill", "[name=my-text]", value="原始内容"), action("fill", "[name=my-text]", value="替换内容")],
         [check("value", "[name=my-text]", expected="替换内容")]),
        (("表单", "选择控件"), "数据列表输入保存官方候选城市", FORM,
         [action("fill", "[name=my-datalist]", value="New York")],
         [check("value", "[name=my-datalist]", expected="New York")]),
        (("表单", "选择控件"), "下拉框选择Two并保存其值", FORM,
         [action("select", "[name=my-select]", value="2")],
         [check("value", "[name=my-select]", expected="2")]),
        (("表单", "选择控件"), "勾选初始未选中的复选框", FORM,
         [action("check", "#my-check-2")], [check("checked", "#my-check-2", expected=True)]),
        (("表单", "选择控件"), "取消初始已选中的复选框", FORM,
         [action("uncheck", "#my-check-1")], [check("checked", "#my-check-1", expected=False)]),
        (("表单", "选择控件"), "单选框切换后同组保持互斥", FORM,
         [action("check", "#my-radio-2")],
         [check("checked", "#my-radio-2", expected=True), check("checked", "#my-radio-1", expected=False)]),
        (("表单", "状态与约束"), "禁用输入框保留disabled状态", FORM, [],
         [check("attribute", "[name=my-disabled]", attribute="disabled", expected="true")]),
        (("表单", "状态与约束"), "只读输入框保留只读状态与初始值", FORM, [],
         [check("attribute", "[name=my-readonly]", attribute="readonly", expected="true"),
          check("value", "[name=my-readonly]", expected="Readonly input")]),
        (("表单", "文本输入"), "日期文本框保存明确输入的日期", FORM,
         [action("fill", "[name=my-date]", value="12/31/2026")],
         [check("value", "[name=my-date]", expected="12/31/2026")]),
        (("表单", "选择控件"), "范围控件右移一格由5变为6", FORM,
         [action("press", "[name=my-range]", key="ArrowRight")],
         [check("value", "[name=my-range]", expected="6")]),
        (("表单", "提交与校验"), "提交表单跳转并显示Received确认", FORM,
         [action("fill", "[name=my-text]", value="Selenium-{{RAND}}"), action("click", "button[type=submit]")],
         [check("url_contains", expected="/selenium/web/submitted-form.html"), check("text", "#message", expected="Received!")]),
        (("页面导航", "返回索引"), "从表单返回官方测试页面索引", FORM,
         [action("click", "a[href='./index.html']")],
         [check("url_contains", expected="/selenium/web/index.html")]),
        (("动态交互", "延迟元素"), "点击Add后延迟显示新的红色方块", BASE + "dynamic.html",
         [check("hidden", "#box0"), action("click", "#adder")],
         [check("visible", "#box0", timeout_seconds=10), check("attribute", "#box0", attribute="class", expected="redbox")]),
        (("动态交互", "延迟输入"), "显示隐藏输入框后可以填写内容", BASE + "dynamic.html",
         [check("hidden", "#revealed"), action("click", "#reveal"), action("wait_visible", "#revealed"),
          action("fill", "#revealed", value="Dynamic-{{RAND}}")],
         [check("value", "#revealed", expected="Dynamic-{{RAND}}")]),
    ]
    cases = []
    prefixes = {"文本输入": "WBSR", "选择控件": "XZKJ", "状态与约束": "ZTYYS", "提交与校验": "TJYJY",
                "返回索引": "FHSY", "延迟元素": "YCYS", "延迟输入": "YCSR"}
    for number, ((folder, title, url, actions, assertions), design) in enumerate(zip(specifications, functional_designs(), strict=True), 1):
        document = {**design, "description": "验证" + title + "。", "preconditions": "可访问 Selenium 官方公开测试站点，不需要登录。",
                    "business_path": ["Selenium 官方测试页", *folder], "prefix": prefixes[folder[-1]],
                    "source": {"kind": "official_fixture_and_html_semantics", "url": url,
                               "rule_skill": "用户上传 test-cases.rar；原始归档 SHA256 08f913f3c8d4de28fac9c19acd9481355c78f6ccbb2e427788dc3ec7df72307f"}}
        if number == 13:
            document["preconditions"] += "【Example range】的初始值为5，步长为1。"
        case = builder.ui_case(f"TC-UI-{number:03}", title, {"start_url": url, "actions": actions, "assertions": assertions},
                               module=folder[0], source="Selenium 官方静态测试页源代码与 HTML 控件语义", risk="read",
                               tags=["selenium-official", "external-public", "source-designed-ir"], test_case_document=document)
        case["requirement_ids"] = ["REQ-SEL-FORM" if folder[0] == "表单" else "REQ-SEL-NAV" if folder[0] == "页面导航" else "REQ-SEL-DYNAMIC"]
        case["extensions"]["design_folder"] = list(folder)
        case["extensions"]["oracle_source"] = {"kind": "official_fixture_and_html_semantics", "url": url,
                                                  "note": "预期来自官方页面源码、官方首个脚本示例与 HTML 控件语义，不从本次执行结果反推。"}
        cases.append((folder, case))
    return cases


def snapshot_sources(project_id: str) -> list[dict]:
    import httpx

    output = projects.path(project_id) / "knowledge" / "官方来源"
    output.mkdir(parents=True, exist_ok=True)
    records = []
    with httpx.Client(timeout=45, follow_redirects=True) as client:
        for name in OFFICIAL_SOURCES:
            response = client.get(BASE + name)
            response.raise_for_status()
            (output / name).write_bytes(response.content)
            records.append({"url": str(response.url), "file": "knowledge/官方来源/" + name,
                            "sha256": hashlib.sha256(response.content).hexdigest(), "bytes": len(response.content), "captured_at": db.now()})
    store.write(output / "sources.json", records)
    return records


def organize(project_id: str, cases: list[tuple[tuple[str, ...], dict]]) -> dict:
    for group in dict.fromkeys(folder[0] for folder, _ in cases):
        tree = case_folders.list_folders(project_id)
        if not any(item["parent_id"] is None and item["name"] == group for item in tree["folders"]):
            case_folders.create(project_id, group, expected_revision=tree["revision"])
    for folder in dict.fromkeys(folder for folder, _ in cases):
        tree = case_folders.list_folders(project_id)
        parent = next(item["id"] for item in tree["folders"] if item["parent_id"] is None and item["name"] == folder[0])
        if not any(item["parent_id"] == parent and item["name"] == folder[1] for item in tree["folders"]):
            case_folders.create(project_id, folder[1], parent_id=parent, expected_revision=tree["revision"])
    for folder in dict.fromkeys(folder for folder, _ in cases):
        tree = case_folders.list_folders(project_id)
        parent = next(item["id"] for item in tree["folders"] if item["parent_id"] is None and item["name"] == folder[0])
        target = next(item["id"] for item in tree["folders"] if item["parent_id"] == parent and item["name"] == folder[1])
        selected = [case["id"] for path, case in cases if path == folder]
        case_folders.move_cases(project_id, selected, target, expected_revision=tree["revision"])
    return case_folders.list_folders(project_id)


def seed(project_id: str | None = None) -> dict:
    data_store.ensure_initialized()
    db.init()
    existing = projects.same_site(FORM)
    project = projects.get(existing) if existing else projects.create("Selenium 官方测试页", FORM, category="官方公开测试站点", project_id=project_id)
    identity = project["id"]
    cases = definitions()
    expected = [{"id": "REQ-SEL-FORM", "module": "表单", "title": "官方表单控件按 HTML 语义保存输入、选择状态和提交结果", "source": FORM},
                {"id": "REQ-SEL-NAV", "module": "页面导航", "title": "Return to index 链接导航至官方测试页索引", "source": FORM},
                {"id": "REQ-SEL-DYNAMIC", "module": "动态交互", "title": "官方动态页点击后延迟添加方块、显示可编辑输入", "source": BASE + "dynamic.html"}]
    store.write(projects.path(identity) / "analysis/requirements.json", expected)
    sources = snapshot_sources(identity)
    projects.update(identity, timeout_seconds=10, page_load_seconds=60)
    projects.save_cases(identity, [case for _, case in cases])
    tree = organize(identity, cases)
    note = """# Selenium 官方测试页

站点：https://www.selenium.dev/selenium/web/web-form.html

这组用例根据实际打开的官方页面、保存的官方 HTML 源码与 HTML 控件语义编写，
再由 autoSkill Selenium 引擎真实回放；不是成功录制所得，也未把 HTTP 200 当作 UI 通过。

目录按功能组织：表单包含文本输入、选择控件、状态与约束、提交与校验；
页面导航包含返回索引；动态交互包含延迟元素和延迟输入。
每个场景独立开始，不依赖前一个场景残留数据。

未覆盖：文件上传、原生颜色选择弹窗、跨平台浏览器和线上容量。
动态页的 10 秒是本次测试等待预算，不是官方性能 SLA。
原始源码及 SHA-256 位于“官方来源”。实际结果、失败与运行编号以 reports 回执和执行记录为准。
"""
    project_materials.write_knowledge(identity, "项目说明.md", note,
                                      hashlib.sha256((projects.path(identity) / "knowledge/项目说明.md").read_bytes()).hexdigest()
                                      if (projects.path(identity) / "knowledge/项目说明.md").is_file() else None)
    lines = ["# 目录与用例覆盖", "", "本表是项目文件夹结构的可读索引，实际用例定义仍由 testcases 中的原文件统一保存。", ""]
    for folder in dict.fromkeys(folder for folder, _ in cases):
        lines.extend(["## " + " / ".join(folder), ""])
        lines.extend(f"- `{case['id']}`：{case['title']}" for path, case in cases if path == folder)
        lines.append("")
    index = projects.path(identity) / "knowledge/目录与覆盖.md"
    project_materials.write_knowledge(identity, "目录与覆盖.md", "\n".join(lines),
                                      hashlib.sha256(index.read_bytes()).hexdigest() if index.is_file() else None)
    return {"project_id": identity, "project_root": str(projects.path(identity)), "cases": len(cases), "folders": tree, "sources": sources}


def deliver(project_id: str) -> dict:
    cases = projects.cases(project_id)
    full = reporting.export_cases(project_id, [case["id"] for case in cases])
    tree = case_folders.list_folders(project_id)
    form = next(folder for folder in tree["folders"] if folder["parent_id"] is None and folder["name"] == "表单")
    selection = case_folders.selection(project_id, form["id"], expected_revision=tree["revision"])
    organization = {key: selection[key] for key in ("folder_id", "folder_name", "recursive", "revision", "folders", "assignments")}
    folder_export = reporting.export_cases(project_id, selection["case_ids"], organization=organization)
    archive = project_materials.export_project(project_id)
    return {"project_excel": full, "form_excel": folder_export, "materials": archive}


def main() -> None:
    parser = argparse.ArgumentParser(description="将 Selenium 官方测试页用例保存到选定 autoSkill 数据目录。")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--project-id")
    parser.add_argument("--execute", action="store_true", help="用 autoSkill 真实回放全部已保存用例")
    parser.add_argument("--engine", choices=runner.ENGINES, default="selenium")
    arguments = parser.parse_args()
    os.environ["AUTOSKILL_WORKSPACE"] = str(arguments.workspace.resolve())
    result = seed(arguments.project_id)
    if arguments.execute:
        run = runner.create(result["project_id"], engine=arguments.engine, session_id="selenium-official")
        result["run"] = runner.execute(run["id"])
    result["deliverables"] = deliver(result["project_id"])
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
