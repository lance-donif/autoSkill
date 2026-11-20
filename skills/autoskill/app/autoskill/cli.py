"""autoskill command line: the only interface an agent needs. Every command prints one JSON object."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path

from . import command_input, db, demo, paths, projects, secrets, sessions, task_context
from .locking import session_lock
from .replay import ir

STATUS_TEXT = {"Pass": "通过", "Fail": "失败", "Blocked": "阻塞", "Cancelled": "已取消"}


# These modules were available as CLI attributes before dispatch became lazy.
_LEGACY_MODULES = ("context_service", "runner", "task_state")


def __getattr__(name: str):
    if name in _LEGACY_MODULES:
        from importlib import import_module

        return import_module("." + name, __package__)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()).union(_LEGACY_MODULES))


class CommandParser(argparse.ArgumentParser):
    """Argument errors use the same machine-readable envelope as service errors."""

    def error(self, message):
        raise ValueError("命令参数有误：" + message)


def emit(value: dict, code: int = 0) -> int:
    print(json.dumps(value, ensure_ascii=False, indent=1))  # noqa: T201  # CLI stdout must contain exactly one JSON object.
    return code


def knowledge(project_id: str, limit: int = 2400) -> list[dict]:
    """Knowledge files that hold real content (the placeholders created with a project are skipped)."""
    found = []
    folder = projects.path(project_id) / "knowledge"
    for file in sorted(folder.glob("*")) if folder.is_dir() else []:
        if file.suffix not in (".md", ".txt", ".json", ".csv"):
            continue
        text = file.read_text(encoding="utf-8", errors="replace").strip()
        if "待补充；未提供不代表不存在" in text and len(text) < 120:
            continue
        found.append({"file": f"knowledge/{file.name}", "content": text[:limit], "truncated": len(text) > limit})
    return found


def context(project_id: str) -> dict:
    """Everything an agent should know before touching the project, in one answer."""
    info, auth = projects.get(project_id), projects.auth(project_id)
    cases = projects.cases(project_id)
    saved = sessions.summary(project_id)
    login_case = next((c for c in cases if c["id"] == auth.get("ui_case")), None)
    steps = []
    if info["missing_secrets"]:
        steps.append("先补齐凭据：" + "；".join(f"autoskill secret set {n}" for n in info["missing_secrets"]))
    if not login_case:
        steps.append("这个项目还没有登录用例。如果被测系统需要登录：autoskill record start "
                     f"{project_id} --as-login，完成登录操作并用 autoskill check 断言登录成功后 record stop；不需要登录则直接录制")
    else:
        steps.append(f"autoskill record start {project_id} 会先执行已有登录用例 {login_case['id']}；如仍需人工认证，保留当前浏览器继续")
    steps.append(f"回放已有用例：autoskill run {project_id}；查看结果：autoskill dashboard")
    return {
        "project": {k: info[k] for k in ("id", "name", "category", "base_url", "start_url", "allowed_origins")},
        "login": {
            "ui_case": auth.get("ui_case"), "api_case": auth.get("api_case"),
            "title": login_case["title"] if login_case else None,
            "saved_session": saved["saved"], "saved_at": saved["saved_at"],
            "note": "优先复用已有登录；需要人工认证时在当前窗口完成，再用同一 --session 的 browser snapshot 继续" if login_case else "尚未定义登录方式",
        },
        "missing_secrets": info["missing_secrets"],
        "cases": [{"id": c["id"], "kind": c["kind"], "title": c["title"], "module": c["module"],
                   "depends_on": projects.depends_on(c), "replayable": ir.is_replayable(c)} for c in cases],
        "knowledge": knowledge(project_id),
        "next_steps": steps,
    }


def run_summary(run: dict, *, brief: bool = False) -> dict:
    from . import task_state

    rows = None if brief else db.rows("SELECT * FROM results WHERE run_id = ? ORDER BY id", (run["id"],))
    totals = task_state.run_values(run, results=rows)
    summary = {
        "run_id": run["id"], "status": run["status"], "status_text": STATUS_TEXT.get(run["status"], run["status"]),
        "state": totals["state"],
        "engine": run["engine"], "total": run["total"], "passed": totals["passed"], "failed": totals["failed"],
        "not_run": totals["not_run"], "reason": run["reason"],
    }
    if rows is not None:
        summary["results"] = []
        for row in rows:
            state = task_state.result(row)
            summary["results"].append({
                "case_id": row["case_id"], "title": row["case_title"], "kind": row["kind"],
                "status": task_state.CASE_STATUS[state["outcome"]],
                "prerequisite": bool(row["prerequisite"]), "ms": row["duration_ms"],
                "detail": row["actual"] or row["reason"], "state": state,
            })
    if "test_report" in run:
        report = run["test_report"]
        if brief:
            fields = ("report_id", "status", "excel", "markdown", "bugs", "reason", "error", "retryable")
            report = {key: report[key] for key in fields if key in report}
        summary["test_report"] = report
    if "publication" in run:
        summary["publication"] = run["publication"]
    return summary


def read_secret_value(args) -> str:
    if args.value is not None:
        return args.value
    value = sys.stdin.readline().rstrip("\r\n")
    if not value:
        raise ValueError("没有读到凭据内容：把值作为第二个参数传入，或通过标准输入提供")
    return value


def _page_options(command, *, default: int = 50) -> None:
    command.add_argument("--limit", type=int, default=default, help="每页数量，默认小批读取；没有总数量上限")
    command.add_argument("--cursor", help="上一页返回的 next_cursor")
    command.add_argument("--all", action="store_true", dest="all_items", help="明确读取全部条目")


def _reader_commands(sub) -> None:
    reader = sub.add_parser("context", help="项目摘要、分页用例与知识索引")
    reader.add_argument("project")
    _page_options(reader)
    reader.add_argument("--knowledge-limit", type=int, default=5)
    reader.add_argument("--knowledge-cursor")
    listing = sub.add_parser("cases", help="分页列出用例，或读取完整定义")
    listing.add_argument("project")
    listing.add_argument("--case", dest="case_id", help="读取一个用例的完整定义")
    for name in ("kind", "module", "requirement", "query"):
        listing.add_argument("--" + name)
    _page_options(listing)
    knowledge_sub = sub.add_parser("knowledge", help="知识索引与按需正文读取").add_subparsers(dest="action", required=True)
    listing = knowledge_sub.add_parser("list")
    listing.add_argument("project")
    listing.add_argument("--query", default="")
    _page_options(listing)
    reader = knowledge_sub.add_parser("read")
    reader.add_argument("project")
    reader.add_argument("file")
    reader.add_argument("--offset", type=int, default=0)
    reader.add_argument("--length", type=int, default=12000)
    reader.add_argument("--revision", help="上一段返回的 content_revision；变化时明确报错")
    reader.add_argument("--all", action="store_true", dest="all_items")
    _knowledge_commands(knowledge_sub)
    failure = sub.add_parser("failure-context", help="只读冻结的执行失败与证据，不重新运行或生成诊断")
    failure.add_argument("project")
    failure.add_argument("--run", required=True)
    failure.add_argument("--execution-attempt", required=True)
    failure.add_argument("--case", required=True, dest="case_id")


def _knowledge_commands(sub) -> None:
    settings = sub.add_parser("settings", help="读取或条件更新团队与业务范围")
    settings.add_argument("project")
    settings.add_argument("--from-file", dest="file", help="包含 expected_hash、team_id 和 business_scope 的 JSON；- 表示标准输入")
    search = sub.add_parser("search", help="搜索规范知识及旧知识文件")
    search.add_argument("project")
    search.add_argument("--query", required=True)
    search.add_argument("--team", action="store_true", help="按项目配置的相同团队及业务范围查询")
    search.add_argument("--limit", type=int, default=50)
    search.add_argument("--cursor")
    reader = sub.add_parser("get", help="读取指定知识修订与当前状态")
    reader.add_argument("project")
    reader.add_argument("entry_id")
    reader.add_argument("--revision", type=int, required=True)
    exporter = sub.add_parser("export", help="导出所选知识及完整修订链到新 JSON 文件")
    exporter.add_argument("project")
    selection = exporter.add_mutually_exclusive_group(required=True)
    selection.add_argument("--entry", action="append", help="知识 id@修订号；可重复指定")
    selection.add_argument("--all", action="store_true", dest="all_items", help="导出本项目当前有效的全部规范知识")
    exporter.add_argument("--output", required=True, help="新的 JSON 文件；已有文件不会被覆盖")
    importer = sub.add_parser("import", help="原子导入知识包；来源执行结论不作为本机验证")
    importer.add_argument("project")
    importer.add_argument("--file", required=True, help="知识包 JSON；- 表示标准输入")
    importer.add_argument("--idempotency-key", required=True)
    importer.add_argument("--as-new", action="store_true", help="在来源修订分叉时明确创建独立知识条目")


def _workflow_commands(sub) -> None:
    workflow = sub.add_parser("workflow", help="持久化测试工作流").add_subparsers(dest="action", required=True)
    start = workflow.add_parser("start")
    start.add_argument("project")
    start.add_argument("--file", required=True, help="工作流 JSON；- 表示完整标准输入")
    start.add_argument("--idempotency-key", required=True)
    revise = workflow.add_parser("revise")
    revise.add_argument("workflow")
    revise.add_argument("--file", required=True, help="新来源版本 JSON；候选定义由内核条件发布")
    revise.add_argument("--idempotency-key", required=True)
    revise.add_argument("--expected-hash")
    status = workflow.add_parser("status")
    status.add_argument("workflow")
    status.add_argument("--cycle", choices=("current", "all"), default="current", help="默认当前周期；all 包含历史周期")
    _page_options(status)
    reader = workflow.add_parser("read")
    reader.add_argument("workflow")
    reader.add_argument("--kind", default="definition")
    reader.add_argument("--offset", type=int, default=0)
    reader.add_argument("--limit", type=int, default=50)
    reader.add_argument("--head", help="固定 domain_head，跨页读取同一版本")
    reader.add_argument("--text-offset", type=int, help="按字符读取较大的定义文档")
    reader.add_argument("--text-length", type=int, default=12000)
    for name in ("next", "submit", "heartbeat", "release", "recover", "cancel", "resume"):
        operation = workflow.add_parser(name)
        operation.add_argument("workflow")
        operation.add_argument("--idempotency-key", required=True)
        if name in ("submit", "heartbeat", "release"):
            operation.add_argument("item")
            operation.add_argument("--lease-token", required=True)
            operation.add_argument("--attempt-id", required=True)
            operation.add_argument("--file", required=name == "submit", help="提交或检查点 JSON；- 表示完整标准输入")
        if name in ("next", "heartbeat"):
            operation.add_argument("--lease-seconds", type=int, default=300)
        if name == "next":
            operation.add_argument("--owner")


def _asset_commands(sub) -> None:
    assets = sub.add_parser("asset", help="导入与查看原生测试资产").add_subparsers(dest="action", required=True)
    importer = assets.add_parser("import")
    importer.add_argument("project")
    importer.add_argument("--source", help="原生目录或文件，相对调用目录")
    importer.add_argument("--engine")
    importer.add_argument("--name", default="")
    importer.add_argument("--file", help="可选 JSON：source、engine、name、argv 数组、cwd 等")
    listing = assets.add_parser("list")
    listing.add_argument("project")
    _page_options(listing)
    show = assets.add_parser("show")
    show.add_argument("project")
    show.add_argument("asset")
    show.add_argument("--head")
    engines = sub.add_parser("engine", help="执行适配器与实际安装能力").add_subparsers(dest="action", required=True)
    engines.add_parser("list")
    engines.add_parser("probe").add_argument("engine", nargs="?")
    native = sub.add_parser("native", help="运行原生测试资产并读取实际结果").add_subparsers(dest="action", required=True)
    run = native.add_parser("run")
    run.add_argument("project")
    run.add_argument("asset")
    run.add_argument("--head")
    run.add_argument("--file", help="运行参数 JSON；- 表示标准输入")
    run.add_argument("--wait", action="store_true", help="同步等待；需要宿主 AI 判定的对话不要使用，以免阻塞判定")
    run.add_argument("--host", help="宿主路由，默认继承本次运行绑定或 generic")
    run.add_argument("--host-session-id", help="可选的现有宿主会话；与浏览器 --session 不同")
    status = native.add_parser("status")
    status.add_argument("run")
    status.add_argument("--case", dest="case_id", help="读取单个用例完整结果；重复编号可用 result:<结果 id>")
    _page_options(status)
    native.add_parser("cancel").add_argument("run")
    native.add_parser("dispatch", help="启动或恢复本机队列驱动；不重复创建运行")


def _judge_commands(sub) -> None:
    judge = sub.add_parser("judge", help="宿主 AI 领取并提交对话质量判定").add_subparsers(dest="action", required=True)
    claim = judge.add_parser("next")
    claim.add_argument("--idempotency-key", required=True)
    claim.add_argument("--owner")
    claim.add_argument("--lease-seconds", type=int, default=300)
    claim.add_argument("--run", dest="run_id")
    judge.add_parser("status").add_argument("judge")
    reader = judge.add_parser("read")
    reader.add_argument("judge")
    reader.add_argument("--offset", type=int, default=0)
    reader.add_argument("--length", type=int, default=12000)
    for name in ("submit", "heartbeat", "release"):
        operation = judge.add_parser(name)
        operation.add_argument("judge")
        operation.add_argument("--attempt-id", required=True)
        operation.add_argument("--lease-token", required=True)
        operation.add_argument("--idempotency-key", required=True)
        if name == "submit":
            operation.add_argument("--file", required=True, help="宿主判定 JSON；- 表示标准输入")
        elif name == "heartbeat":
            operation.add_argument("--lease-seconds", type=int, default=300)


def _conversation_commands(sub) -> None:
    capture = sub.add_parser("conversation", help="捕获页面会话接口并生成多轮问数资产").add_subparsers(dest="action", required=True)
    importer = capture.add_parser("import")
    importer.add_argument("project")
    importer.add_argument("--source", required=True, help="cURL 文本或 HAR 文件；只解析，不通过 shell 执行")
    importer.add_argument("--format", choices=("curl", "har"))
    importer.add_argument("--name", default="")
    importer.add_argument("--expected-head")
    for name in ("show", "build"):
        command = capture.add_parser(name)
        command.add_argument("project")
        command.add_argument("capture")
        if name == "show":
            command.add_argument("--head")
        else:
            command.add_argument("--file", help="显式字段映射及每轮独立预期的 JSON；- 表示标准输入")
            command.add_argument("--asset-id")
            command.add_argument("--name", default="")
            command.add_argument("--expected-head")


def _schedule_commands(sub) -> None:
    schedule = sub.add_parser("schedule", help="持久化回放、原生执行与 AI 维护调度").add_subparsers(dest="action", required=True)
    listing = schedule.add_parser("list")
    listing.add_argument("--project", action="append", dest="projects")
    listing.add_argument("--offset", type=int, default=0)
    listing.add_argument("--limit", type=int, default=50)
    listing.add_argument("--all", action="store_true", dest="all_items")
    schedule.add_parser("show").add_argument("task")
    for name in ("create", "update"):
        operation = schedule.add_parser(name)
        if name == "update":
            operation.add_argument("task")
        operation.add_argument("--file", required=True, help="调度 JSON；- 表示标准输入")
    for name in ("run", "pause", "resume", "delete"):
        operation = schedule.add_parser(name)
        operation.add_argument("task")
        if name == "run":
            operation.add_argument("--idempotency-key")
        elif name != "delete":
            operation.add_argument("--expected-revision", type=int)


def _data_commands(sub) -> None:
    data = sub.add_parser("data", help="独立数据目录：状态、完整导入和导出").add_subparsers(dest="action", required=True)
    data.add_parser("status", help="只读查看当前数据目录")
    importing = data.add_parser("import", help="把完整旧数据或数据 ZIP 复制到空目录")
    importing.add_argument("source", help="旧数据目录、旧程序目录或完整数据 ZIP")
    importing.add_argument("--target", help="空的新数据目录；省略时使用当前数据目录")
    exporting = data.add_parser("export", help="导出全部数据，包含凭据、登录态和运行历史")
    exporting.add_argument("--out", "--destination", dest="destination", help="完整数据 ZIP 的保存路径；省略时在数据目录旁生成")


def _delete_commands(sub) -> None:
    removal = sub.add_parser("delete", help="删除指定用例或执行记录").add_subparsers(dest="action", required=True)
    cases = removal.add_parser("case", help="删除当前用例，保留历史执行记录")
    cases.add_argument("project")
    cases.add_argument("case_ids", nargs="+")
    runs = removal.add_parser("run", help="删除执行记录及对应证据；正在执行的先取消再删除")
    runs.add_argument("run_ids", nargs="+")


def parser(*, include_run_engines: bool = True) -> argparse.ArgumentParser:
    top = CommandParser(prog="autoskill", description="录一次，到处回放：录制浏览器操作与接口，生成无需 AI 的回放用例。")
    top.add_argument("--session", help="AI 测试会话标识；同一任务每条命令使用同一个值，也可设 AUTOSKILL_SESSION")
    top.add_argument("--runtime-context", help="插件提供的工作区与服务绑定；沿用注册 Skill 中的前缀")
    top.add_argument("--workflow-binding", help="workflow next 返回的 binding_token；把操作明确关联到该任务尝试")
    top.add_argument("--host-session", help="宿主提供的会话身份，供后续判定投递；不是浏览器任务身份")
    top.add_argument("--input-base", help=argparse.SUPPRESS)
    sub = top.add_subparsers(dest="command", required=True)

    project = sub.add_parser("project", help="项目：create / list / show / update").add_subparsers(dest="action", required=True)
    create = project.add_parser("create", help="新建项目；给出要测的地址即授权该地址")
    create.add_argument("--name", required=True)
    create.add_argument("--url", required=True)
    create.add_argument("--category", default="")
    create.add_argument("--id")
    project.add_parser("list")
    project.add_parser("show").add_argument("project")
    update = project.add_parser("update")
    update.add_argument("project")
    update.add_argument("--name")
    update.add_argument("--category")
    update.add_argument("--allow-origin", action="append", help=argparse.SUPPRESS)  # no longer limits anything; old commands still parse
    update.add_argument("--approve", action="append", help=argparse.SUPPRESS)
    update.add_argument("--timeout", type=int, help="单步等待元素的秒数，默认 20")
    update.add_argument("--page-load", type=int, help="等待页面打开的秒数，默认 90；页面本身很慢时调大（10–600）")
    update.add_argument("--insecure-tls", choices=("on", "off"), help="内网自签名证书：on 表示不校验证书")

    _reader_commands(sub)
    _workflow_commands(sub)
    _asset_commands(sub)
    _conversation_commands(sub)
    _judge_commands(sub)
    _schedule_commands(sub)
    _data_commands(sub)
    _delete_commands(sub)

    secret = sub.add_parser("secret", help="本机凭据：set / list / delete").add_subparsers(dest="action", required=True)
    setter = secret.add_parser("set")
    setter.add_argument("name")
    setter.add_argument("value", nargs="?", help="省略时从标准输入读取")
    secret.add_parser("list")
    secret.add_parser("delete").add_argument("name")

    login = sub.add_parser("login", help="执行项目的登录用例并保存登录态")
    login.add_argument("project")
    login.add_argument("--force", action="store_true", help="忽略已保存的登录态，重新登录")
    login.add_argument("--headed", action="store_true", help="显示浏览器窗口（默认就是显示）")

    record = sub.add_parser("record", help="录制：start / stop / status / undo / cancel").add_subparsers(dest="action", required=True)
    start = record.add_parser("start")
    start.add_argument("project")
    start.add_argument("--as-login", action="store_true", help="本次录制的是登录流程本身")
    start.add_argument("--no-login", action="store_true", help="以未登录状态开始")
    start.add_argument("--url", help="起始地址，默认项目地址")
    start.add_argument("--reuse", help="在同一个录制标签页执行指定的已保存页面用例作为前置")
    start.add_argument("--headed", action="store_true", help="显示浏览器窗口（默认就是显示）")
    stop = record.add_parser("stop")
    stop.add_argument("--title", required=True)
    stop.add_argument("--module", "--subproject", default="", help="子项目（模块）：同一站点下的功能区域")
    stop.add_argument("--plan", help="这次录制执行的是用例表里的哪一条，例如 TC-003；结果会写回用例表")
    stop.add_argument("--category", default="")
    for name in ("status", "undo", "cancel"):
        record.add_parser(name)

    browser = sub.add_parser("browser", help="在录制中操作页面：open snapshot click fill select press …")
    browser.add_argument("args", nargs=argparse.REMAINDER)

    check = sub.add_parser("check", help="保存预期并检查页面实际结果")
    check.add_argument("kind", choices=sorted(ir.ASSERTIONS))
    check.add_argument("target", nargs="?", help="@e5、testid=名称、text=文字、label=标签、role=button:名称 或 CSS 选择器")
    check.add_argument("expected", nargs="?")
    check.add_argument("--attribute")
    check.add_argument("--timeout", type=float)
    check.add_argument("--stable-for", type=float)
    check.add_argument("--mode", choices=("eventually", "always"))
    check.add_argument("--from-json", help="转换前的完整断言 JSON；缺省定位时沿用当前目标")

    run = sub.add_parser("run", help="回放用例；前置用例（如登录）自动先执行")
    run.add_argument("project")
    run.add_argument("--case", action="append", dest="cases", help="用例编号，可重复；省略则回放全部")
    engine = run.add_argument("--engine", default="playwright")
    if include_run_engines:
        from .runner import ENGINES

        engine.choices = ENGINES
    run.add_argument("--kind", choices=("ui", "api"), help="只回放某一类")
    run.add_argument("--candidate", help="验证 record stop 或工作流返回的候选任务标识，通过后发布可复用用例")
    run.add_argument("--api-only", action="store_true", help="独立 HTTP 冷启动；不借用浏览器或已保存登录")
    run.add_argument("--inputs", dest="file", help="运行参数 JSON 文件（- 读标准输入）；RAND 由每次运行生成，不能覆盖")
    run.add_argument("--headed", action="store_true", help="显示浏览器窗口（默认就是显示）")
    run_status = sub.add_parser("run-status", help="只读执行状态、发布结果和已有报告；不执行用例或重新导出")
    run_status.add_argument("run_id")
    run_status.add_argument("--brief", action="store_true", help="只读运行汇总、发布回执和报告入口，省略逐条结果")

    export = sub.add_parser("export", help="导出：postman / scripts").add_subparsers(dest="action", required=True)
    for name in ("postman", "scripts"):
        item = export.add_parser(name)
        item.add_argument("project")
        item.add_argument("--out", help="输出目录，默认项目目录下")

    rep = sub.add_parser("report", help="导出 Excel 用例表与执行报告（每条用例取最近一次回放结果）")
    rep.add_argument("project")
    rep.add_argument("--out", help="输出目录")
    receipt = sub.add_parser("task-report", help="读取本次任务的 Excel 与 Markdown 交付结果；不使用其他任务的最近结果")
    receipt.add_argument("report_id")
    receipt.add_argument("--retry", action="store_true", help="用对应任务快照重试生成，不重新执行测试")
    packer = sub.add_parser("pack", help="把项目（用例、知识库、登录方式）打成一个分享包")
    packer.add_argument("projects", nargs="*", help="项目标识；省略则打包全部项目")
    packer.add_argument("--out", help="输出文件或目录")
    packer.add_argument("--full", action="store_true", help="打出含运行时的完整包：对方解压即用（约 270 MB）")
    packer.add_argument("--include-secrets", action="store_true", help="把凭据用口令加密后一并带走（团队共用测试账号时）")
    packer.add_argument("--passphrase", help="配合 --include-secrets；省略时从环境变量 AUTOSKILL_PASSPHRASE 读取")
    importer = sub.add_parser("import", help="导入别人给的分享包")
    importer.add_argument("file")
    importer.add_argument("--passphrase", help="分享包带有加密凭据时提供")
    importer.add_argument("--overwrite", action="store_true", help="本机已有同名项目时用分享包里的版本替换")

    sub.add_parser("demo", help="安装示例项目（示例工单系统）").add_argument("--url", default=demo.DEFAULT_URL)
    dash = sub.add_parser("dashboard", help="启动仪表盘")
    dash.add_argument("--port", type=int, default=8787)
    dash.add_argument("--headed", action="store_true", help="显示浏览器窗口（默认就是显示）")
    doc = sub.add_parser("doctor", help="检查运行时是否齐全；不联网")
    doc.add_argument("--launch", action="store_true", help="同时真实启动一次浏览器（Playwright 与 Selenium）")
    sub.add_parser("verify", help="按清单校验随包运行时文件的完整性（SHA-256）")
    prefs = sub.add_parser("settings", help="本机设置：用哪个浏览器；不带参数时显示当前设置")
    prefs.add_argument("--browser", choices=("system", "bundled"),
                       help="system：优先用本机 Chrome，随包浏览器备用（默认）；bundled：只用随包浏览器")
    plan = sub.add_parser("plan", help="用例表（用例 Skill）：write / show / link / mark / sync").add_subparsers(dest="action", required=True)
    writer = plan.add_parser("write", help="把根据需求文档设计的用例写成 Excel 用例表")
    writer.add_argument("project")
    writer.add_argument("--file", required=True, help="用例 JSON 文件（数组）")
    writer.add_argument("--expected-cases", type=int, help="按独立验收目标预先确定的用例数；单一连续流程填 1")
    plan.add_parser("show", help="读出用例表：要执行哪些用例、各自的结果")
    linker = plan.add_parser("link", help="把当前用例表的一行关联到同项目已有用例；随后回放才写入真实结果")
    linker.add_argument("case", help="本次设计表中的用例编号")
    linker.add_argument("replay_case", help="同项目可复用用例编号")
    marker = plan.add_parser("mark", help="给一条用例记录执行结果")
    marker.add_argument("case")
    marker.add_argument("status", choices=("通过", "失败", "未执行"))
    marker.add_argument("--note", default="")
    plan.add_parser("sync", help="把回放结果写回用例表")
    for command in (login, start, run, dash):
        command.add_argument("--headless", action="store_true", help="不显示浏览器窗口，在后台运行（默认显示最大化的窗口）")
    return top


def export_postman(project_id: str, out: str | None) -> dict:
    from pathlib import Path

    from . import runner
    from .replay import postman

    cases = {c["id"]: c for c in projects.cases(project_id)}
    api_ids = [cid for cid, c in cases.items() if c["kind"] == "api"]
    if not api_ids:
        raise ValueError("项目里还没有接口用例；先录制一条")
    ordered = [cases[cid] for cid, _ in runner.build_plan(api_ids, cases) if cases[cid]["kind"] == "api"]
    info = projects.get(project_id)
    collection, environment = postman.export(info["name"], ordered, info["base_url"])
    folder = Path(out) if out else projects.path(project_id) / "postman"
    folder.mkdir(parents=True, exist_ok=True)
    files = {"collection": folder / "collection.json", "environment": folder / "environment.json"}
    files["collection"].write_text(json.dumps(collection, ensure_ascii=False, indent=2), encoding="utf-8")
    files["environment"].write_text(json.dumps(environment, ensure_ascii=False, indent=2), encoding="utf-8")
    blanks = [v["key"] for v in environment["values"] if v["type"] == "secret"]
    return {"requests": len(ordered), "collection": str(files["collection"]), "environment": str(files["environment"]),
            "fill_in_before_running": blanks, "note": "凭据值没有导出；在 Postman 环境里填写上面列出的变量后按顺序运行"}


def verify() -> dict:
    """Check every bundled runtime file against the manifest written when the package was built."""
    import hashlib

    manifest_file = paths.VENDOR / "runtime-manifest.json"
    if not manifest_file.is_file():
        raise ValueError("没有找到随包运行时清单；verify 只适用于完整离线包")
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))["files"]
    missing, changed = [], []
    for name, expected in manifest.items():
        file = (paths.VENDOR / name).resolve()
        if not file.is_relative_to(paths.VENDOR.resolve()) or not file.is_file():
            missing.append(name)
            continue
        digest = hashlib.sha256()
        with file.open("rb") as stream:
            for block in iter(lambda: stream.read(1 << 20), b""):
                digest.update(block)
        if digest.hexdigest() != expected:
            changed.append(name)
    return {"ok": not missing and not changed, "files": len(manifest), "missing": missing[:20], "changed": changed[:20]}


def launch_check() -> dict:
    """Start both engines with their configured browsers and check a local page."""
    import functools
    import threading
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    from . import runner

    class Quiet(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    site = paths.SKILL / "examples" / "ticket-desk" / "web"
    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(site)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    config = {"base_url": base, "allowed_origins": [base], "timeout_seconds": 20}
    report = {}
    try:
        for engine in runner.ENGINES:
            try:
                driver = runner.make_driver(engine, config)
                try:
                    driver.reset()
                    driver.goto(base + "/")
                    report[engine] = "ok" if "工单" in driver.title() else "页面标题不符：" + driver.title()
                finally:
                    driver.close()
            except Exception as error:  # noqa: BLE001  # Doctor must report every browser startup failure without aborting its checks.
                report[engine] = "启动失败：" + (str(error) or type(error).__name__)[:1800]
    finally:
        server.shutdown()
        server.server_close()
    return report


def doctor(launch: bool = False) -> dict:
    import importlib.util

    from . import platforms

    modules = ("playwright", "selenium", "httpx", "jsonschema", "openpyxl", "fastapi", "uvicorn")
    report = {
        "platform": platforms.system_info(),
        "bundled_runtime": paths.VENDOR.is_dir(),
        "chrome": paths.chrome(), "chromedriver": paths.chromedriver(), "agent_browser": paths.agent_browser(),
        "python": sys.version.split()[0],
        "modules": {name: importlib.util.find_spec(name) is not None for name in modules},
        "workspace": str(paths.workspace()), "network": "不联网、不下载、不检查更新",
    }
    if os.environ.get("AUTOSKILL_BRIDGE") == "off":
        report["executed_by"] = "autoSkill 后台服务（发起命令的进程在受限沙箱里，已自动转交）"
    problems = platforms.runtime_problems(report["platform"])
    problems += [name for name, ok in report["modules"].items() if not ok]
    if sys.platform == "linux" and importlib.util.find_spec("cryptography") is None:
        problems.append("cryptography（Linux 本机凭据加密）")
    problems += [name for name in ("chrome", "chromedriver", "agent_browser") if not report[name]]
    if launch and not problems:
        report["browser_versions"] = platforms.browser_pair(paths.chrome(bundled=True), report["chromedriver"])
        report["agent_browser_version"] = platforms.binary_status(report["agent_browser"])
        if not report["browser_versions"]["ok"] or not report["agent_browser_version"]["ok"]:
            problems.append("浏览器 / 驱动版本或运行时系统库，请查看 browser_versions 和 agent_browser_version")
    if launch and not problems:
        report["browser_launch"] = launch_check()
        problems += [f"{engine} 浏览器" for engine, result in report["browser_launch"].items() if result != "ok"]
    report["ok"] = not problems
    if problems:
        report["missing"] = problems
        report["hint"] = "请使用匹配系统和 CPU 的完整包；Linux / UOS 先运行 sh setup-linux.sh；缺失的部分不会联网下载"
    return report


def _dispatch_project(args) -> tuple[dict, int]:
    """Handle the project command without changing its result contract."""
    action = getattr(args, "action", None)
    if action == "create":
        return {"created": projects.create(args.name, args.url, args.category, args.id)}, 0
    if action == "list":
        return {"projects": projects.list_all()}, 0
    if action == "show":
        return projects.get(args.project), 0
    return projects.update(args.project, name=args.name, category=args.category, allowed_origins=args.allow_origin,
                           approvals=args.approve, timeout_seconds=args.timeout, page_load_seconds=args.page_load,
                           ignore_https_errors=None if args.insecure_tls is None else args.insecure_tls == "on"), 0


def _dispatch_context(args) -> tuple[dict, int]:
    """Keep the legacy Python API while making command responses explicitly paginated."""
    from . import context_service

    return context_service.context(args.project, limit=args.limit, cursor=args.cursor, all_items=args.all_items,
                                   knowledge_limit=args.knowledge_limit, knowledge_cursor=args.knowledge_cursor), 0


def _dispatch_cases(args) -> tuple[dict, int]:
    from . import context_service

    if args.case_id:
        return context_service.case(args.project, args.case_id), 0
    return context_service.cases(args.project, limit=args.limit, cursor=args.cursor, all_items=args.all_items,
                                 kind=args.kind, module=args.module, requirement=args.requirement, query=args.query or ""), 0


def _dispatch_delete(args) -> tuple[dict, int]:
    from . import deletion

    result = (deletion.delete_cases([{"project_id": args.project, "case_id": case_id} for case_id in args.case_ids])
              if args.action == "case" else deletion.delete_runs(args.run_ids))
    return result, 2 if result["errors"] else 0


def _dispatch_knowledge(args) -> tuple[dict, int]:
    from . import context_service, knowledge_entries, knowledge_transfer

    if args.action == "read":
        return context_service.read_knowledge(args.project, args.file, offset=args.offset, length=args.length,
                                              content_revision=args.revision, all_items=args.all_items), 0
    if args.action == "list":
        return context_service.knowledge(args.project, limit=args.limit, cursor=args.cursor, all_items=args.all_items, query=args.query), 0
    if args.action == "settings":
        if args.file is None:
            return knowledge_entries.settings(args.project), 0
        payload = _input_json(args)
        if not isinstance(payload, dict):
            raise ValueError("知识设置需要是 JSON 对象")
        return knowledge_entries.settings(args.project, payload), 0
    if args.action == "search":
        return knowledge_entries.search(args.project, query=args.query, team=args.team, limit=args.limit, cursor=args.cursor), 0
    if args.action == "get":
        return knowledge_entries.get(args.project, args.entry_id, revision=args.revision), 0
    if args.action == "import":
        return knowledge_transfer.import_package(args.project, _input_json(args), idempotency_key=args.idempotency_key, as_new=args.as_new), 0
    return _export_knowledge(args), 0


def _export_knowledge(args) -> dict:
    from . import knowledge_transfer

    entries = []
    for selector in args.entry or []:
        entry_id, separator, revision = selector.rpartition("@")
        if not separator or not entry_id or not revision.isdecimal() or int(revision) < 1:
            raise ValueError("--entry 需要采用知识 id@正整数修订号格式")
        entries.append({"id": entry_id, "revision": int(revision)})
    package = knowledge_transfer.export_package(args.project, entries=entries or None, all_items=args.all_items)
    output = Path(args.output).expanduser()
    output = (output if output.is_absolute() else command_input.base_directory(args) / output).absolute()
    try:
        with output.open("x", encoding="utf-8") as stream:
            json.dump(package, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
    except FileExistsError as error:
        raise ValueError("输出文件已存在，不能覆盖：" + str(output)) from error
    return {"path": str(output), "package_hash": package["package_hash"], "entry_count": len(package["entries"])}


def _dispatch_failure_context(args) -> tuple[dict, int]:
    from . import failure_context

    return failure_context.read(args.project, run_id=args.run, execution_attempt_id=args.execution_attempt, case_id=args.case_id), 0


def _input_json(args):
    return command_input.read_json(args.file, command=args.command, action=getattr(args, "action", None), base=command_input.base_directory(args))


def _dispatch_workflow(args) -> tuple[dict, int]:
    from . import workflow

    if args.action == "start":
        return workflow.create(args.project, _input_json(args), idempotency_key=args.idempotency_key), 0
    if args.action == "revise":
        return workflow.revise(args.workflow, _input_json(args), idempotency_key=args.idempotency_key, expected_hash=args.expected_hash), 0
    if args.action == "read":
        return workflow.read(args.workflow, kind=args.kind, offset=args.offset, limit=args.limit,
                             head=args.head, text_offset=args.text_offset, text_length=args.text_length), 0
    if args.action == "status":
        value = workflow.status(args.workflow, limit=args.limit, cursor=args.cursor, cycle=args.cycle)
        if args.all_items:
            if args.cursor:
                raise ValueError("--all 不能与 --cursor 同时使用")
            items = list(value["items"])
            while value.get("next_cursor"):
                value = workflow.status(args.workflow, limit=args.limit, cursor=value["next_cursor"], cycle=args.cycle)
                items.extend(value["items"])
            value = {**value, "items": items, "cursor": None, "next_cursor": None}
        return value, 0
    options = {"idempotency_key": args.idempotency_key}
    if args.action == "next":
        return workflow.next_task(args.workflow, owner=args.owner, lease_seconds=args.lease_seconds, **options), 0
    if args.action in ("submit", "heartbeat", "release"):
        options.update(lease_token=args.lease_token, attempt_id=args.attempt_id)
        if args.action == "submit":
            payload = _input_json(args)
            if isinstance(payload, dict) and "payload" in payload:
                if set(payload) - {"payload", "knowledge_used"}:
                    raise ValueError("提交信封只接受 payload 和 knowledge_used")
                options.update(payload=payload["payload"], knowledge_used=payload.get("knowledge_used"))
            else:
                options["payload"] = payload
        else:
            options["checkpoint"] = _input_json(args) if args.file else None
            if args.action == "heartbeat":
                options["lease_seconds"] = args.lease_seconds
        return getattr(workflow, args.action)(args.workflow, args.item, **options), 0
    return getattr(workflow, args.action)(args.workflow, **options), 0


def _dispatch_asset(args) -> tuple[dict, int]:
    from . import native_assets, paging

    if args.action == "show":
        return native_assets.get(args.project, args.asset, head=args.head), 0
    if args.action == "list":
        pool = native_assets.list_assets(args.project)
        rows, page = paging.select(pool, content_revision=paging.revision(pool), query={"project": args.project},
                                   limit=args.limit, cursor=args.cursor, all_items=args.all_items)
        return {"assets": rows, "page": page}, 0
    spec = _input_json(args) if args.file else {}
    if not isinstance(spec, dict):
        raise ValueError("资产输入需要是 JSON 对象")
    source = command_input.resolve_path(args.source, command_input.base_directory(args)) if args.source else spec.pop("source", None)
    engine = args.engine or spec.pop("engine", None)
    if not source or not engine:
        raise ValueError("导入资产需要 source 和 engine；用 --source/--engine 或 JSON 提供")
    name = args.name or spec.pop("name", "")
    options = {key: spec[key] for key in ("argv", "cwd", "source_info", "expected_head", "asset_id") if key in spec}
    if args.source:
        options.setdefault("source_info", {"original_source": str((command_input.base_directory(args) / args.source).resolve())})
    return native_assets.import_asset(args.project, source, engine, name=name, **options), 0


def _dispatch_engine(args) -> tuple[dict, int]:
    from . import engines

    return {"engines": engines.list_engines() if args.action == "list" else engines.probe(args.engine)}, 0


def _dispatch_native(args) -> tuple[dict, int]:
    from . import native_reader, native_run
    from .server import jobs

    if args.action == "dispatch":
        return {"queue_dispatcher": jobs.ensure_worker()}, 0
    if args.action == "status":
        return native_reader.status(args.run, limit=args.limit, cursor=args.cursor, all_items=args.all_items, case_id=args.case_id), 0
    if args.action == "cancel":
        native_run.cancel(args.run)
        return native_reader.status(args.run), 0
    run = native_run.enqueue(args.project, args.asset, head=args.head, parameters=_input_json(args) if args.file else None,
                             host=args.host or os.environ.get("AUTOSKILL_HOST") or "generic",
                             host_session_id=args.host_session_id if args.host_session_id is not None else os.environ.get("AUTOSKILL_HOST_SESSION_ID", ""))
    if not args.wait:
        return {**run, "queue_dispatcher": jobs.ensure_worker(), "note": "已入队；用 native status 查看实际结果，并用 judge next 处理宿主判定"}, 0
    native_run.execute(run["id"])
    final = native_reader.status(run["id"])
    return final, {"Pass": 0, "Fail": 1}.get(final["status"], 2)


def _dispatch_conversation(args) -> tuple[dict, int]:
    from . import conversation_capture

    if args.action == "import":
        source = command_input.resolve_path(args.source, command_input.base_directory(args))
        return conversation_capture.import_source(args.project, source, source_format=args.format, name=args.name, expected_head=args.expected_head), 0
    if args.action == "show":
        return conversation_capture.get(args.project, args.capture, head=args.head), 0
    return conversation_capture.build_asset(args.project, args.capture, mapping=_input_json(args) if args.file else None,
                                             asset_id=args.asset_id, name=args.name, expected_head=args.expected_head), 0


def _dispatch_judge(args) -> tuple[dict, int]:
    from . import dialogue_judge

    if args.action == "status":
        return dialogue_judge.get(args.judge), 0
    if args.action == "read":
        return dialogue_judge.read_input(args.judge, offset=args.offset, length=args.length), 0
    if args.action == "next":
        return dialogue_judge.next_judgement(idempotency_key=args.idempotency_key, owner=args.owner,
                                               lease_seconds=args.lease_seconds, run_id=args.run_id), 0
    if args.action in ("heartbeat", "release"):
        options = {"lease_seconds": args.lease_seconds} if args.action == "heartbeat" else {}
        return getattr(dialogue_judge, args.action)(args.judge, attempt_id=args.attempt_id, lease_token=args.lease_token,
                                                     idempotency_key=args.idempotency_key, **options), 0
    return dialogue_judge.submit(args.judge, attempt_id=args.attempt_id, lease_token=args.lease_token,
                                  idempotency_key=args.idempotency_key, judgement=_input_json(args)), 0


def _dispatch_schedule(args) -> tuple[dict, int]:
    from .server import jobs, scheduler

    if args.action == "list":
        return scheduler.page(args.projects, offset=args.offset, limit=args.limit, all_items=args.all_items), 0
    if args.action == "create":
        spec = _input_json(args)
        spec.setdefault("host", os.environ.get("AUTOSKILL_HOST") or "generic")
        spec.setdefault("host_session_id", os.environ.get("AUTOSKILL_HOST_SESSION_ID", ""))
        return {**scheduler.create(spec), "queue_dispatcher": jobs.ensure_worker()}, 0
    if args.action == "update":
        return {**scheduler.update(args.task, _input_json(args)), "queue_dispatcher": jobs.ensure_worker()}, 0
    if args.action in ("pause", "resume"):
        result = scheduler.set_enabled(args.task, args.action == "resume", expected_revision=args.expected_revision)
        return {**result, **({"queue_dispatcher": jobs.ensure_worker()} if args.action == "resume" else {})}, 0
    if args.action == "delete":
        scheduler.delete(args.task)
        return {"deleted": args.task}, 0
    if args.action == "run":
        return {**scheduler.run_now(args.task, idempotency_key=args.idempotency_key), "queue_dispatcher": jobs.ensure_worker()}, 0
    return scheduler.get(args.task), 0


def _dispatch_data(args) -> tuple[dict, int]:
    from . import data_store

    base = command_input.base_directory(args)
    if args.action == "status":
        return data_store.status(), 0
    if args.action == "import":
        source = command_input.resolve_path(args.source, base)
        original = (base / Path(args.source).expanduser()).resolve()
        target = (base / Path(args.target).expanduser()).resolve() if args.target else None
        options = {"original_source": original} if Path(source) != original else {}
        return data_store.import_data(source, target, **options), 0
    destination = (base / Path(args.destination).expanduser()).resolve() if args.destination else None
    return data_store.export_data(destination), 0


def _dispatch_secret(args) -> tuple[dict, int]:
    """Handle the secret command without changing its result contract."""
    action = getattr(args, "action", None)
    if action == "set":
        secrets.set_secret(args.name, read_secret_value(args))
        return {"saved": args.name, "note": "已明文保存在本机；普通分享包仅在选择携带凭据时包含，完整包会包含凭据与保存的登录态"}, 0
    if action == "delete":
        secrets.delete_secret(args.name)
        return {"deleted": args.name}, 0
    return {"secrets": secrets.names()}, 0


def _dispatch_report(args) -> tuple[dict, int]:
    """Handle the report command without changing its result contract."""
    from . import reporting

    return reporting.build(args.project, args.out), 0


def _dispatch_task_report(args) -> tuple[dict, int]:
    from . import task_reports

    result = task_reports.retry(args.report_id) if args.retry else task_reports.get(args.report_id)
    return result, 2 if result["status"] == "failed" else 0


def _dispatch_pack(args) -> tuple[dict, int]:
    """Handle the pack command without changing its result contract."""
    from . import share

    if args.full:
        return share.pack_full(args.out), 0
    phrase = None
    if args.include_secrets:
        phrase = args.passphrase or os.environ.get("AUTOSKILL_PASSPHRASE")
        if not phrase:
            raise ValueError("--include-secrets 需要口令：加 --passphrase，或设置环境变量 AUTOSKILL_PASSPHRASE")
    return share.pack(args.projects or None, args.out, phrase), 0


def _dispatch_settings(args) -> tuple[dict, int]:
    """Handle the settings command without changing its result contract."""
    if args.browser:
        paths.save_settings(browser=args.browser)
    return {"browser": paths.browser_info()}, 0


def _dispatch_plan(args) -> tuple[dict, int]:
    """Handle the plan command without changing its result contract."""
    action = getattr(args, "action", None)
    from . import plan

    if action == "write":
        payload = _input_json(args)
        expected = getattr(args, "expected_cases", None)
        if expected is not None:
            plan.validate_case_count(payload, expected)
            _initialize_workspace()
        with tempfile.TemporaryDirectory(prefix="autoskill-plan-") as folder:
            source = Path(folder) / "cases.json"
            source.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            return plan.write(args.project, str(source)), 0
    if action == "mark":
        return plan.mark(args.case, args.status, args.note), 0
    if action == "link":
        from . import case_plans

        shown = plan.show()
        view = case_plans.get(shown["project"], task_context.session_id() or "")
        if view.get("task_id") != shown.get("task_id") or Path(view["folder"]).resolve() != Path(shown["skill"]).resolve():
            raise ValueError("当前会话设计表已变化，请重新读取 plan show 后关联")
        linked = case_plans.link(shown["project"], args.case, args.replay_case, session=view["session"], expected_revision=view["revision"])
        return {"case": args.case, "replay": shown["project"] + "/" + args.replay_case, "task_id": shown.get("task_id"),
                "delivery_excel": shown.get("delivery_excel"), "revision": linked["revision"],
                "next": f"autoskill run {shown['project']} --case {args.replay_case} 回放后，真实结果会写回本次用例表；继续使用同一 --session"}, 0
    return (plan.sync() if action == "sync" else plan.show()), 0


def _dispatch_import(args) -> tuple[dict, int]:
    """An existing project may be skipped normally; an unsuccessful import needs attention."""
    from . import share

    source = command_input.resolve_path(args.file, command_input.base_directory(args))
    result = share.import_pack(source, args.passphrase or os.environ.get("AUTOSKILL_PASSPHRASE"), args.overwrite)
    return result, 2 if result["errors"] else 0


def _dispatch_demo(args) -> tuple[dict, int]:
    """Handle the demo command without changing its result contract."""
    return {"installed": demo.install(args.url),
            "start_demo_site": "python examples/ticket-desk/server.py --port 8790（在 Skill 目录下）"}, 0


def _dispatch_doctor(args) -> tuple[dict, int]:
    """Handle the doctor command without changing its result contract."""
    report = doctor(args.launch)
    return report, 0 if report["ok"] else 1


def _dispatch_verify(_args) -> tuple[dict, int]:
    """Handle the verify command without changing its result contract."""
    report = verify()
    return report, 0 if report["ok"] else 1


def _dispatch_export(args) -> tuple[dict, int]:
    """Handle the export command without changing its result contract."""
    action = getattr(args, "action", None)
    if action == "postman":
        return export_postman(args.project, args.out), 0
    from .replay import codegen

    return codegen.export(args.project, args.out), 0


def _dispatch_dashboard(args) -> tuple[dict, int]:
    """Handle the dashboard command without changing its result contract."""
    from .server import app as server

    server.serve(args.port)
    return {"stopped": True}, 0


def _dispatch_execution(args) -> tuple[dict, int]:
    """Handle the execution command without changing its result contract."""
    from . import runner

    command = args.command
    if command == "run" and args.candidate:
        return _dispatch_candidate_run(args)
    if command == "login":
        auth = projects.auth(args.project)
        chosen = [auth[k] for k in ("ui_case", "api_case") if auth.get(k)]
        if not chosen:
            raise ValueError(f"项目还没有登录用例；用 autoskill record start {args.project} --as-login 录制一次登录")
        run = runner.create(args.project, chosen, "playwright", trigger="login", force_login=args.force)
        final = runner.execute(run["id"], force_login=args.force)
    else:
        chosen = args.cases
        if args.kind:
            pool = [c for c in projects.cases(args.project) if c["kind"] == args.kind and ir.is_replayable(c)]
            chosen = [c["id"] for c in pool if not args.cases or c["id"] in args.cases]
            if not chosen:
                raise ValueError("没有符合条件的可回放用例")
        run = runner.create(args.project, chosen, args.engine, api_only=args.api_only, inputs=_input_json(args) if args.file else None)
        final = runner.execute(run["id"])
    summary = run_summary(final)
    if command == "run":  # the verdicts go back into the case sheet these cases came from
        from . import plan

        written = plan.sync(quiet=True, run_id=run["id"])
        if written:
            summary["plan"] = written
    return summary, {"Pass": 0, "Fail": 1}.get(final["status"], 2)


def _dispatch_candidate_run(args) -> tuple[dict, int]:
    from . import candidate_publication, runner

    task = candidate_publication.read(args.candidate)
    if task["project_id"] != args.project:
        raise ValueError("候选任务属于其他项目")
    if args.cases and set(args.cases) - {case["id"] for case in task["cases"]}:
        raise ValueError("选择包含不在当前候选任务中的用例")
    selected = [case["id"] for case in task["cases"] if (not args.kind or case["kind"] == args.kind)
                and (not args.cases or case["id"] in args.cases)]
    if not selected:
        raise ValueError("没有符合条件的候选用例")
    queued = candidate_publication.queue(args.candidate, case_ids=selected, engine=args.engine,
                                        api_only=args.api_only, inputs=_input_json(args) if args.file else None, start=False)
    completed = [runner.execute(run_id) for run_id in queued["run_ids"]]
    result = {**queued, "runs": [run_summary(run) for run in completed]}
    if completed:
        receipt = candidate_publication.receipt(completed[-1]["id"]) or {}
        result.update(receipt, publication=receipt)
    failed = any(run["status"] == "Fail" for run in completed)
    return result, 1 if failed else 2 if result.get("unpublished") else 0


def _dispatch_run_status(args) -> tuple[dict, int]:
    from . import runner, task_reports

    run = db.one("SELECT * FROM runs WHERE id = ?", (args.run_id,))
    if not run:
        raise ValueError("运行不存在")
    result = runner.public_run(run)
    try:
        result["test_report"] = task_reports.get("run_" + run["id"])
    except ValueError as error:
        result["test_report"] = {"status": "unavailable", "reason": str(error)}
    return run_summary(result, brief=args.brief), 0


def _dispatch_recording(args) -> tuple[dict, int]:
    """Dispatch recording operations and the existing assertion fallback."""
    command, action = args.command, getattr(args, "action", None)
    from .recorder import session

    if command == "record":
        if action == "start":
            return session.start(args.project, login=not args.no_login, as_login=args.as_login, url=args.url, reuse=args.reuse), 0
        if action == "stop":
            return session.stop(args.title, args.module, args.category, args.plan), 0
        return getattr(session, action)(), 0
    if command == "browser":
        return session.browser(args.args), 0
    options = {key: value for key, value in (("timeout_seconds", args.timeout), ("stable_for_seconds", args.stable_for), ("mode", args.mode)) if value is not None}
    if args.from_json:
        options["transition_from"] = json.loads(args.from_json)
    result = session.check(args.kind, args.target, args.expected, args.attribute, **options)
    return result, 1 if result.get("passed") is False else 0


def dispatch(args) -> tuple[dict, int]:
    """Route one parsed CLI command to its named handler."""
    handlers = {
        "project": _dispatch_project,
        "context": _dispatch_context,
        "cases": _dispatch_cases,
        "delete": _dispatch_delete,
        "knowledge": _dispatch_knowledge,
        "failure-context": _dispatch_failure_context,
        "workflow": _dispatch_workflow,
        "asset": _dispatch_asset,
        "engine": _dispatch_engine,
        "native": _dispatch_native,
        "conversation": _dispatch_conversation,
        "judge": _dispatch_judge,
        "schedule": _dispatch_schedule,
        "data": _dispatch_data,
        "secret": _dispatch_secret,
        "report": _dispatch_report,
        "task-report": _dispatch_task_report,
        "pack": _dispatch_pack,
        "settings": _dispatch_settings,
        "plan": _dispatch_plan,
        "import": _dispatch_import,
        "demo": _dispatch_demo,
        "doctor": _dispatch_doctor,
        "verify": _dispatch_verify,
        "export": _dispatch_export,
        "dashboard": _dispatch_dashboard,
        "login": _dispatch_execution,
        "run": _dispatch_execution,
        "run-status": _dispatch_run_status,
    }
    handler = handlers.get(args.command, _dispatch_recording)
    if args.command in ("record", "browser", "check"):
        with session_lock():
            return handler(args)
    return handler(args)


def delegate(args, argv: list[str], reason: str) -> int:
    """Run this command through the local service and relay exactly what it printed."""
    from . import bridge

    port, why = bridge.service()
    if port is None:
        return emit({"error": bridge.restricted_message(reason, why)}, 2)
    argv, stdin_text, attachments = command_input.for_bridge(args, argv)
    reply = bridge.forward(port, argv, stdin_text, caller_cwd=str(command_input.base_directory(args)), attachments=attachments)
    sys.stdout.write(reply.get("stdout", ""))
    sys.stderr.write(reply.get("stderr", ""))
    return int(reply.get("exit", 2))


def undo_msys(args) -> None:
    """Restore arguments Git Bash rewrote from "/x" to "<Git root>/x". Only for values that are never file paths."""
    root = (os.environ.get("AUTOSKILL_MSYS_ROOT") or "").rstrip("/")
    if not root:
        return

    def fix(value):
        if isinstance(value, str) and root.lower() in value.lower():
            import re

            return re.sub(re.escape(root) + "(?=/)", "", value, flags=re.IGNORECASE)
        return value

    if args.command == "check":
        args.target, args.expected = fix(args.target), fix(args.expected)
    elif args.command == "browser":
        args.args = [fix(a) for a in args.args]
    elif args.command == "record" and getattr(args, "url", None):
        args.url = fix(args.url)
    args.session = fix(args.session)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        # Only the run subparser reads engine choices. Direct parser() callers get the full parser.
        args = parser(include_run_engines="run" in argv).parse_args(argv)
        undo_msys(args)
        command_input.runtime_context(args.runtime_context or os.environ.get("AUTOSKILL_RUNTIME_CONTEXT"))
        if args.host_session is not None:
            os.environ["AUTOSKILL_HOST_SESSION_ID"] = args.host_session
    except ValueError as error:
        return emit({"error": str(error), "code": "validation_error"}, 2)
    identity = args.session if args.session is not None else task_context.session_id()
    binding = args.workflow_binding or os.environ.get("AUTOSKILL_WORKFLOW_BINDING")
    scope = contextlib.nullcontext()
    if binding:
        from . import workflow

        scope = workflow.use_binding(binding)
    with task_context.use_session(identity), scope:
        return _main(args, argv)


def _initialize_workspace() -> None:
    from . import data_store

    data_store.ensure_initialized()
    db.init()


def _main(args, argv: list[str]) -> int:
    """Execute one command after the caller's session identity has been selected."""
    if getattr(args, "headless", False) or getattr(args, "headed", False):
        os.environ["AUTOSKILL_HEADED"] = "0" if getattr(args, "headless", False) else "1"
    real_stdout = sys.stdout
    try:
        mode = os.environ.get("AUTOSKILL_BRIDGE", "auto")
        bound_data_status = (args.command == "data" and args.action == "status"
                             and (args.runtime_context or os.environ.get("AUTOSKILL_RUNTIME_CONTEXT")))
        if bound_data_status and mode != "off":
            return delegate(args, argv, "当前命令已绑定 autoSkill 后台数据目录")
        local_data = args.command == "data" and mode != "on"
        if args.command != "dashboard" and mode != "off" and not local_data:
            reason = "已设置 AUTOSKILL_BRIDGE=on" if mode == "on" else paths.restricted()
            if reason:
                return delegate(args, argv, reason)
        # A counted plan validates its input before creating any workspace or sheet.
        counted_plan = args.command == "plan" and args.action == "write" and args.expected_cases is not None
        if args.command != "data" and not counted_plan:
            _initialize_workspace()
        if args.command != "dashboard":  # the dashboard is long-running and talks to a person
            sys.stdout = sys.stderr  # anything a library prints goes to stderr; stdout carries the JSON only
        try:
            result, code = dispatch(args)
        finally:
            sys.stdout = real_stdout
    except Exception as error:  # noqa: BLE001  # Unexpected command failures must still produce one JSON response.
        from .recorder.agent import AgentError
        from .recorder.session import RecordError

        if isinstance(error, (ValueError, PermissionError, RecordError, AgentError)):
            return emit({"error": str(error), "code": getattr(error, "code", "validation_error"), "details": getattr(error, "details", {})}, 2)
        import traceback

        traceback.print_exc(file=sys.stderr)
        if isinstance(error, OSError) or type(error).__module__ == "sqlite3":
            reason = "" if args.command == "data" else paths.restricted()
            message = (f"读写 autoSkill 数据失败（{reason}）" if reason else "读写 autoSkill 数据失败") + f"：{type(error).__name__}: {error}"
        else:
            message = f"autoSkill 内部错误：{type(error).__name__}: {error}（详细信息在标准错误输出里）"
        return emit({"error": message[:600]}, 2)
    return emit(result, code)


__all__ = tuple(name for name in __dir__() if not name.startswith("_"))


if __name__ == "__main__":
    sys.exit(main())
