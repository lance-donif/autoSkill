"""autoskill command line: the only interface an agent needs. Every command prints one JSON object."""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import capabilities, db, demo, paths, projects, runner, secrets, sessions
from . import knowledge as knowledge_store
from .replay import ir

STATUS_TEXT = {"Pass": "通过", "Fail": "失败", "Blocked": "阻塞", "Cancelled": "已取消"}


def emit(value: dict, code: int = 0) -> int:
    print(json.dumps(value, ensure_ascii=False, indent=1))
    return code


def case_page(project_id: str, *, query: str = "", module: str | None = None, kind: str | None = None,
              offset: int = 0, limit: int = 30, case_id: str | None = None, pool: list[dict] | None = None) -> dict:
    """Bounded summaries by default; expand only an explicitly selected case."""
    offset, limit = knowledge_store.bounds(offset, limit)
    pool = projects.cases(project_id) if pool is None else pool
    terms = query.casefold().split()
    matched = [c for c in pool if (module is None or c.get("module", "") == module)
               and (kind is None or c["kind"] == kind) and (case_id is None or c["id"] == case_id)
               and all(term in " ".join(str(c.get(key, "")) for key in ("id", "title", "module", "category")).casefold() for term in terms)]
    matched.sort(key=lambda c: c["id"])
    if case_id is not None and not matched:
        raise ValueError(f"项目里没有符合筛选条件的用例 {case_id}")
    page = matched[offset:offset + limit]
    items = []
    for case in page:
        depends = projects.depends_on(case)
        item = {"id": case["id"], "kind": case["kind"], "title": case["title"][:200],
                "module": case.get("module", "")[:120], "depends_on": depends[:10],
                "replayable": ir.is_replayable(case)}
        if len(depends) > 10:
            item["depends_on_total"] = len(depends)
        items.append(item)
    returned = len(items)
    result = {"project": project_id, "cases": items,
              "summary": {"total": len(pool), "ui": sum(c["kind"] == "ui" for c in pool), "api": sum(c["kind"] == "api" for c in pool)},
              "pagination": {"total": len(pool), "matched": len(matched), "offset": offset, "limit": limit,
                             "returned": returned, "has_more": offset + returned < len(matched),
                             "next_offset": offset + returned if offset + returned < len(matched) else None},
              "next": "用 --query/--module/--kind 筛选、--offset 翻页；--case 编号 只展开选中的完整用例"}
    if case_id is not None and page:
        result["case"] = page[0]
    return result


def context(project_id: str, *, query: str = "", module: str | None = None,
            offset: int = 0, limit: int = 30) -> dict:
    """A bounded project overview; retrieve selected cases/knowledge on demand."""
    info, auth = projects.get(project_id), projects.auth(project_id)
    cases = projects.cases(project_id)
    page = case_page(project_id, query=query, module=module, offset=offset, limit=limit, pool=cases)
    notes = knowledge_store.search(project_id, query, limit=5, snippet_chars=120, budget_chars=3000)
    saved = sessions.summary(project_id)
    login_case = next((c for c in cases if c["id"] == auth.get("ui_case")), None)
    steps = []
    if info["missing_secrets"]:
        steps.append("先补齐凭据：" + "；".join(f"autoskill secret set {n}" for n in info["missing_secrets"][:8]))
    if not login_case:
        steps.append("这个项目还没有登录用例。如果被测系统需要登录：autoskill record start "
                     f"{project_id} --as-login，完成登录操作并用 autoskill check 断言登录成功后 record stop；不需要登录则直接录制")
    else:
        steps.append(f"需要登录的操作不用手动登录：autoskill record start {project_id} 会先执行登录用例 {login_case['id']}")
    steps.append(f"回放已有用例：autoskill run {project_id}；查看结果：autoskill dashboard")
    return {
        "project": {k: info[k] for k in ("id", "name", "category", "base_url", "start_url", "allowed_origins")},
        "login": {
            "ui_case": auth.get("ui_case"), "api_case": auth.get("api_case"),
            "title": login_case["title"] if login_case else None,
            "saved_session": saved["saved"], "saved_at": saved["saved_at"],
            "note": "登录由 autoskill 自动完成并在本机复用；不要自己输入账号密码" if login_case else "尚未定义登录方式",
        },
        "missing_secrets": info["missing_secrets"][:30], "missing_secrets_total": len(info["missing_secrets"]),
        "cases": page["cases"], "case_summary": page["summary"], "case_pagination": page["pagination"],
        "knowledge": notes["notes"], "knowledge_pagination": notes["pagination"],
        "next_steps": steps,
    }


def run_summary(run: dict) -> dict:
    rows = db.rows("SELECT * FROM results WHERE run_id = ? ORDER BY id", (run["id"],))
    return {
        "run_id": run["id"], "status": run["status"], "status_text": STATUS_TEXT.get(run["status"], run["status"]),
        "engine": run["engine"], "total": run["total"], "passed": run["passed"], "failed": run["failed"],
        "blocked": run["blocked"], "reason": run["reason"],
        "results": [{"case_id": r["case_id"], "title": r["case_title"], "kind": r["kind"], "status": r["status"],
                     "prerequisite": bool(r["prerequisite"]), "ms": r["duration_ms"],
                     "detail": r["actual"] or r["reason"]} for r in rows],
    }


def read_secret_value(args) -> str:
    if args.value is not None:
        return args.value
    value = sys.stdin.readline().rstrip("\r\n")
    if not value:
        raise ValueError("没有读到凭据内容：把值作为第二个参数传入，或通过标准输入提供")
    return value


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="autoskill", description="录一次，到处回放：录制浏览器操作与接口，生成无需 AI 的回放用例。")
    sub = top.add_subparsers(dest="command", required=True)

    caps = sub.add_parser("capabilities", help="机器可读的浏览器能力：执行、录制、双引擎回放与导出；不启动浏览器")
    caps.add_argument("query", nargs="*", help="可选：命令或能力组，例如 click、network、dialog accept")
    caps.add_argument("--group", help="仅列出该组；完整组名见不带参数的输出")

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
    update.add_argument("--audio-input", help="明确指定测试麦克风的 16-bit PCM WAV（复制到项目），off 清除")
    update.add_argument("--assertion-timeout", type=float, help="页面断言等待秒数；AI 长响应可单独调大")
    update.add_argument("--conversation-timeout", type=float, help="SSE/WebSocket 对话的总等待秒数")

    for name, help_text in (("context", "项目概览：登录、用例/知识摘要与下一步（默认最多 30 条用例）"),
                            ("cases", "分页查找项目用例；--case 编号 展开单条")):
        listing = sub.add_parser(name, help=help_text)
        listing.add_argument("project")
        listing.add_argument("--query", default="")
        listing.add_argument("--module")
        listing.add_argument("--offset", type=int, default=0)
        listing.add_argument("--limit", type=int, default=30)
        if name == "cases":
            listing.add_argument("--kind", choices=("ui", "api"))
            listing.add_argument("--case", dest="case_id")

    notes = sub.add_parser("knowledge", help="显式积累与按需查找项目知识：list / search / show / add").add_subparsers(dest="action", required=True)
    for name in ("list", "search"):
        item = notes.add_parser(name)
        item.add_argument("project")
        item.add_argument("query", nargs="?" if name == "list" else None, default="")
        item.add_argument("--offset", type=int, default=0)
        item.add_argument("--limit", type=int, default=20)
    note_show = notes.add_parser("show")
    note_show.add_argument("project")
    note_show.add_argument("name")
    note_show.add_argument("--offset", type=int, default=0)
    note_show.add_argument("--limit", type=int, default=4000, help="正文字符数，上限 16000")
    note_add = notes.add_parser("add", aliases=("record",), help="显式保存已确认知识并引用来源/用例/运行；不会从失败自动学习")
    note_add.add_argument("project")
    note_add.add_argument("--title", required=True)
    note_add.add_argument("--name", help="可选的新 Markdown 文件名；不能覆盖已有文件")
    note_content = note_add.add_mutually_exclusive_group(required=True)
    note_content.add_argument("--content")
    note_content.add_argument("--file", help="读取 UTF-8 正文")
    note_add.add_argument("--source", action="append", default=[])
    note_add.add_argument("--case", action="append", default=[], dest="case_ids")
    note_add.add_argument("--run", action="append", default=[], dest="run_ids")

    from . import protocols_cli

    api = sub.add_parser("api", help="直接执行/保存 HTTP、SSE 与 WebSocket 协议用例").add_subparsers(dest="api_action", required=True)
    protocols_cli.register(api)

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
    start.add_argument("--headed", action="store_true", help="显示浏览器窗口（默认就是显示）")
    stop = record.add_parser("stop")
    stop.add_argument("--title", required=True)
    stop.add_argument("--module", "--subproject", default="", help="子项目（模块）：同一站点下的功能区域")
    stop.add_argument("--plan", help="这次录制执行的是用例表里的哪一条，例如 TC-003；结果会写回用例表")
    stop.add_argument("--category", default="")
    for name in ("status", "undo", "cancel"):
        record.add_parser(name)

    browser = sub.add_parser("browser", add_help=False, help="页面操作与 native 能力；browser help 查看 JSON 能力表")
    browser.add_argument("args", nargs=argparse.REMAINDER)

    check = sub.add_parser("check", help="对当前页面做断言；成立才会记录")
    check.add_argument("kind", choices=sorted(ir.ASSERTIONS))
    check.add_argument("target", nargs="?", help="@e5、testid=名称、text=文字、label=标签、role=button:名称 或 CSS 选择器")
    check.add_argument("expected", nargs="?")
    check.add_argument("--attribute")
    check.add_argument("--timeout", type=float, help="本条断言最多等待的秒数")
    check.add_argument("--stable-for", type=float, help="期望连续保持的秒数，用于流式回答完成后稳定性判断")

    run = sub.add_parser("run", help="回放用例；前置用例（如登录）自动先执行")
    run.add_argument("project")
    run.add_argument("--case", action="append", dest="cases", help="用例编号，可重复；省略则回放全部")
    run.add_argument("--engine", choices=runner.ENGINES, default="playwright")
    run.add_argument("--kind", choices=("ui", "api"), help="只回放某一类")
    run.add_argument("--headed", action="store_true", help="显示浏览器窗口（默认就是显示）")

    export = sub.add_parser("export", help="导出：postman / scripts").add_subparsers(dest="action", required=True)
    for name in ("postman", "scripts"):
        item = export.add_parser(name)
        item.add_argument("project")
        item.add_argument("--out", help="输出目录，默认项目目录下")

    rep = sub.add_parser("report", help="导出 Excel 用例表与执行报告（每条用例取最近一次回放结果）")
    rep.add_argument("project")
    rep.add_argument("--out", help="输出目录")
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
    plan = sub.add_parser("plan", help="用例表（用例 Skill）：write / show / mark / sync").add_subparsers(dest="action", required=True)
    writer = plan.add_parser("write", help="把根据需求文档设计的用例写成 Excel 用例表")
    writer.add_argument("project")
    writer.add_argument("--file", required=True, help="用例 JSON 文件（数组）")
    plan_show = plan.add_parser("show", help="分页查找用例表，默认只列 30 条摘要")
    plan_show.add_argument("--query", default="")
    plan_show.add_argument("--module")
    plan_show.add_argument("--status", choices=("通过", "失败", "阻塞", "未执行"))
    plan_show.add_argument("--case", dest="case_id")
    plan_show.add_argument("--offset", type=int, default=0)
    plan_show.add_argument("--limit", type=int, default=30)
    plan_show.add_argument("--details", action="store_true", help="展开所选页的步骤/预期；选中单条时信息更完整")
    marker = plan.add_parser("mark", help="给一条用例记录执行结果")
    marker.add_argument("case")
    marker.add_argument("status", choices=("通过", "失败", "阻塞", "未执行"))
    marker.add_argument("--note", default="")
    plan.add_parser("sync", help="把回放结果写回用例表")
    for command in (login, start, run, dash):
        command.add_argument("--headless", action="store_true", help="不显示浏览器窗口，在后台运行（默认显示最大化的窗口）")
    return top


def export_postman(project_id: str, out: str | None) -> dict:
    from pathlib import Path

    from .replay import postman

    cases = {c["id"]: c for c in projects.cases(project_id)}
    api_ids = [cid for cid, c in cases.items() if c["kind"] == "api"]
    if not api_ids:
        raise ValueError("项目里还没有接口用例；先录制一条")
    ordered = [cases[cid] for cid, _ in runner.build_plan(api_ids, cases)]
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
    """Really start the bundled browser on a local page with both engines; file presence proves nothing."""
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
            except Exception as error:
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
        report["browser_versions"] = platforms.browser_pair(report["chrome"], report["chromedriver"])
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


def dispatch(args) -> tuple[dict, int]:
    command, action = args.command, getattr(args, "action", None)
    if command == "capabilities":
        return capabilities.catalog(" ".join(args.query), args.group), 0
    if command == "browser":
        query = capabilities.help_query(args.args)
        if query is not None:
            return capabilities.catalog(query), 0
    if command == "project":
        if action == "create":
            return {"created": projects.create(args.name, args.url, args.category, args.id)}, 0
        if action == "list":
            return {"projects": projects.list_all()}, 0
        if action == "show":
            return projects.get(args.project), 0
        media_config = None
        if args.audio_input is not None:
            if args.audio_input == "off":
                media_config = {}
            else:
                from . import artifacts, media

                staged = artifacts.stage_upload(args.project, [args.audio_input])
                media_config = {"audio_input_file": staged[0]}
                media.chrome_args({"media": media_config}, projects.path(args.project))
        return projects.update(args.project, name=args.name, category=args.category, allowed_origins=args.allow_origin,
                               approvals=args.approve, timeout_seconds=args.timeout, page_load_seconds=args.page_load,
                               ignore_https_errors=None if args.insecure_tls is None else args.insecure_tls == "on",
                               media=media_config, assertion_timeout_seconds=args.assertion_timeout,
                               conversation_timeout_seconds=args.conversation_timeout), 0
    if command == "context":
        return context(args.project, query=args.query, module=args.module, offset=args.offset, limit=args.limit), 0
    if command == "cases":
        return case_page(args.project, query=args.query, module=args.module, kind=args.kind, offset=args.offset,
                         limit=args.limit, case_id=args.case_id), 0
    if command == "knowledge":
        if action in ("list", "search"):
            return knowledge_store.search(args.project, args.query, offset=args.offset, limit=args.limit), 0
        if action == "show":
            return knowledge_store.show(args.project, args.name, offset=args.offset, limit=args.limit), 0
        content = args.content
        if args.file:
            with open(args.file, encoding="utf-8-sig") as stream:
                content = stream.read(knowledge_store.MAX_CONTENT + 1)
        return knowledge_store.add(args.project, args.title, content, sources=args.source, case_ids=args.case_ids,
                                   run_ids=args.run_ids, name=args.name), 0
    if command == "api":
        from . import protocols_cli

        result = protocols_cli.dispatch(args)
        return result, {"Pass": 0, "Fail": 1}.get(result.get("status", "Pass"), 2)
    if command == "secret":
        if action == "set":
            secrets.set_secret(args.name, read_secret_value(args))
            return {"saved": args.name, "note": "已在本机明文保存；不会进入用例、报告或默认分享包"}, 0
        if action == "delete":
            secrets.delete_secret(args.name)
            return {"deleted": args.name}, 0
        return {"secrets": secrets.names()}, 0
    if command == "report":
        from . import reporting

        return reporting.build(args.project, args.out), 0
    if command == "pack":
        from . import share

        if args.full:
            return share.pack_full(args.out), 0
        phrase = None
        if args.include_secrets:
            phrase = args.passphrase or os.environ.get("AUTOSKILL_PASSPHRASE")
            if not phrase:
                raise ValueError("--include-secrets 需要口令：加 --passphrase，或设置环境变量 AUTOSKILL_PASSPHRASE")
        return share.pack(args.projects or None, args.out, phrase), 0
    if command == "plan":
        from . import plan

        if action == "write":
            return plan.write(args.project, args.file), 0
        if action == "mark":
            return plan.mark(args.case, args.status, args.note), 0
        return (plan.sync() if action == "sync" else plan.show(query=args.query, module=args.module,
                status=args.status, case_id=args.case_id, offset=args.offset, limit=args.limit, details=args.details)), 0
    if command == "import":
        from . import share

        return share.import_pack(args.file, args.passphrase or os.environ.get("AUTOSKILL_PASSPHRASE"), args.overwrite), 0
    if command == "demo":
        return {"installed": demo.install(args.url),
                "start_demo_site": "python examples/ticket-desk/server.py --port 8790（在 Skill 目录下）"}, 0
    if command == "doctor":
        report = doctor(args.launch)
        return report, 0 if report["ok"] else 1
    if command == "verify":
        report = verify()
        return report, 0 if report["ok"] else 1
    if command == "export":
        if action == "postman":
            return export_postman(args.project, args.out), 0
        from .replay import codegen

        return codegen.export(args.project, args.out), 0
    if command == "dashboard":
        from .server import app as server

        server.serve(args.port)
        return {"stopped": True}, 0
    if command in ("login", "run"):
        if command == "login":
            auth = projects.auth(args.project)
            chosen = [auth[k] for k in ("ui_case", "api_case") if auth.get(k)]
            if not chosen:
                raise ValueError(f"项目还没有登录用例；用 autoskill record start {args.project} --as-login 录制一次登录")
            run = runner.create(args.project, chosen, "playwright", trigger="login")
            final = runner.execute(run["id"], force_login=args.force)
        else:
            chosen = args.cases
            if args.kind:
                pool = [c for c in projects.cases(args.project) if c["kind"] == args.kind and runner.can_queue(c)]
                chosen = [c["id"] for c in pool if not args.cases or c["id"] in args.cases]
                if not chosen:
                    raise ValueError("没有符合条件的可回放用例")
            run = runner.create(args.project, chosen, args.engine)
            final = runner.execute(run["id"])
        summary = run_summary(final)
        if command == "run":  # the verdicts go back into the case sheet these cases came from
            from . import plan

            written = plan.sync(quiet=True)
            if written:
                summary["plan"] = written
        return summary, {"Pass": 0, "Fail": 1}.get(final["status"], 2)
    from .recorder import session

    if command == "record":
        if action == "start":
            return session.start(args.project, login=not args.no_login, as_login=args.as_login, url=args.url), 0
        if action == "stop":
            return session.stop(args.title, args.module, args.category, args.plan), 0
        return getattr(session, action)(), 0
    if command == "browser":
        return session.browser(args.args), 0
    return session.check(args.kind, args.target, args.expected, args.attribute,
                         timeout_seconds=args.timeout, stable_for_seconds=args.stable_for), 0


def delegate(args, argv: list[str], reason: str) -> int:
    """Run this command through the local service and relay exactly what it printed."""
    from . import bridge

    port, why = bridge.service()
    if port is None:
        return emit({"error": bridge.restricted_message(reason, why)}, 2)
    try:
        argv = bridge.prepare_argv(argv)
    except (ValueError, OSError) as error:
        return emit({"error": str(error)}, 2)
    stdin_text = None
    if args.command == "secret" and args.action == "set" and args.value is None:
        stdin_text = sys.stdin.readline()
    reply = bridge.forward(port, argv, stdin_text)
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


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    argv = list(sys.argv[1:] if argv is None else argv)
    # Everything after `browser` belongs to agent-browser, including global
    # flags. Put it behind argparse's delimiter without interpreting its values.
    parse_argv = ["browser", "--", *argv[1:]] if argv[:1] == ["browser"] else argv
    args = parser().parse_args(parse_argv)
    if args.command == "browser" and args.args[:1] == ["--"]:
        args.args = args.args[1:]
    undo_msys(args)
    # Discovery must work without a writable workspace, active recording,
    # background service or browser binary, including AUTOSKILL_BRIDGE=on.
    if (args.command == "capabilities" or (args.command == "browser" and capabilities.help_query(args.args) is not None)
            or (args.command == "api" and args.api_action == "capabilities")):
        try:
            result, code = dispatch(args)
            return emit(result, code)
        except ValueError as error:
            return emit({"error": str(error)}, 2)
    if getattr(args, "headless", False) or getattr(args, "headed", False):
        os.environ["AUTOSKILL_HEADED"] = "0" if getattr(args, "headless", False) else "1"
    mode = os.environ.get("AUTOSKILL_BRIDGE", "auto")  # auto: only when this process cannot do the work itself
    if args.command != "dashboard" and mode != "off":
        reason = "已设置 AUTOSKILL_BRIDGE=on" if mode == "on" else paths.restricted()
        if reason:  # sandboxed: the service the user (or the harness plugin) runs outside the sandbox does the work
            return delegate(args, argv, reason)
    real_stdout = sys.stdout
    try:
        db.init()
        if args.command != "dashboard":  # the dashboard is long-running and talks to a person
            sys.stdout = sys.stderr  # anything a library prints goes to stderr; stdout carries the JSON only
        try:
            result, code = dispatch(args)
        finally:
            sys.stdout = real_stdout
    except Exception as error:
        from .recorder.agent import AgentError
        from .recorder.session import RecordError

        if isinstance(error, (ValueError, PermissionError, RecordError, AgentError)):
            return emit({"error": str(error)}, 2)
        import traceback

        traceback.print_exc(file=sys.stderr)
        if isinstance(error, OSError) or type(error).__module__ == "sqlite3":
            reason = paths.restricted()
            message = (f"读写 autoSkill 数据失败（{reason}）" if reason else "读写 autoSkill 数据失败") + f"：{type(error).__name__}: {error}"
        else:
            message = f"autoSkill 内部错误：{type(error).__name__}: {error}（详细信息在标准错误输出里）"
        return emit({"error": message[:600]}, 2)
    return emit(result, code)


if __name__ == "__main__":
    sys.exit(main())
