"""autoskill command line: the only interface an agent needs. Every command prints one JSON object."""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import db, demo, paths, projects, runner, secrets, sessions
from .replay import ir

STATUS_TEXT = {"Pass": "通过", "Fail": "失败", "Blocked": "阻塞", "Cancelled": "已取消"}


def emit(value: dict, code: int = 0) -> int:
    print(json.dumps(value, ensure_ascii=False, indent=1))
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
        steps.append(f"需要登录的操作不用手动登录：autoskill record start {project_id} 会先执行登录用例 {login_case['id']}")
    steps.append(f"回放已有用例：autoskill run {project_id}；查看结果：autoskill dashboard")
    return {
        "project": {k: info[k] for k in ("id", "name", "category", "base_url", "start_url", "allowed_origins", "approvals")},
        "login": {
            "ui_case": auth.get("ui_case"), "api_case": auth.get("api_case"),
            "title": login_case["title"] if login_case else None,
            "saved_session": saved["saved"], "saved_at": saved["saved_at"],
            "note": "登录由 autoskill 自动完成并在本机复用；不要自己输入账号密码" if login_case else "尚未定义登录方式",
        },
        "missing_secrets": info["missing_secrets"],
        "cases": [{"id": c["id"], "kind": c["kind"], "title": c["title"], "module": c["module"],
                   "depends_on": projects.depends_on(c), "replayable": ir.is_replayable(c)} for c in cases],
        "knowledge": knowledge(project_id),
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
    update.add_argument("--allow-origin", action="append", help="额外允许访问的地址，可重复")
    update.add_argument("--approve", action="append", choices=sorted(projects.RISKS), help="允许的高风险操作，可重复")
    update.add_argument("--timeout", type=int)
    update.add_argument("--insecure-tls", choices=("on", "off"), help="内网自签名证书：on 表示不校验证书")

    sub.add_parser("context", help="开始工作前先看这个：登录方式、已有用例、知识库、下一步").add_argument("project")
    sub.add_parser("cases", help="列出项目用例").add_argument("project")

    secret = sub.add_parser("secret", help="本机凭据：set / list / delete").add_subparsers(dest="action", required=True)
    setter = secret.add_parser("set")
    setter.add_argument("name")
    setter.add_argument("value", nargs="?", help="省略时从标准输入读取")
    secret.add_parser("list")
    secret.add_parser("delete").add_argument("name")

    login = sub.add_parser("login", help="执行项目的登录用例并保存登录态")
    login.add_argument("project")
    login.add_argument("--force", action="store_true", help="忽略已保存的登录态，重新登录")

    record = sub.add_parser("record", help="录制：start / stop / status / undo / cancel").add_subparsers(dest="action", required=True)
    start = record.add_parser("start")
    start.add_argument("project")
    start.add_argument("--as-login", action="store_true", help="本次录制的是登录流程本身")
    start.add_argument("--no-login", action="store_true", help="以未登录状态开始")
    start.add_argument("--url", help="起始地址，默认项目地址")
    stop = record.add_parser("stop")
    stop.add_argument("--title", required=True)
    stop.add_argument("--module", default="")
    stop.add_argument("--category", default="")
    for name in ("status", "undo", "cancel"):
        record.add_parser(name)

    browser = sub.add_parser("browser", help="在录制中操作页面：open snapshot click fill select press …")
    browser.add_argument("args", nargs=argparse.REMAINDER)

    check = sub.add_parser("check", help="对当前页面做断言；成立才会记录")
    check.add_argument("kind", choices=sorted(ir.ASSERTIONS))
    check.add_argument("target", nargs="?", help="@e5、testid=名称、text=文字、label=标签、role=button:名称 或 CSS 选择器")
    check.add_argument("expected", nargs="?")
    check.add_argument("--attribute")

    run = sub.add_parser("run", help="回放用例；前置用例（如登录）自动先执行")
    run.add_argument("project")
    run.add_argument("--case", action="append", dest="cases", help="用例编号，可重复；省略则回放全部")
    run.add_argument("--engine", choices=runner.ENGINES, default="playwright")
    run.add_argument("--kind", choices=("ui", "api"), help="只回放某一类")

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
    doc = sub.add_parser("doctor", help="检查运行时是否齐全；不联网")
    doc.add_argument("--launch", action="store_true", help="同时真实启动一次浏览器（Playwright 与 Selenium）")
    sub.add_parser("verify", help="按清单校验随包运行时文件的完整性（SHA-256）")
    return top


def export_postman(project_id: str, out: str | None) -> dict:
    from pathlib import Path

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
                report[engine] = "启动失败：" + (str(error) if isinstance(error, ValueError) else type(error).__name__)
    finally:
        server.shutdown()
        server.server_close()
    return report


def doctor(launch: bool = False) -> dict:
    import importlib.util

    modules = ("playwright", "selenium", "httpx", "jsonschema", "openpyxl", "fastapi", "uvicorn")
    report = {
        "bundled_runtime": paths.VENDOR.is_dir(),
        "chrome": paths.chrome(), "chromedriver": paths.chromedriver(), "agent_browser": paths.agent_browser(),
        "python": sys.version.split()[0],
        "modules": {name: importlib.util.find_spec(name) is not None for name in modules},
        "workspace": str(paths.workspace()), "network": "不联网、不下载、不检查更新",
    }
    problems = [name for name, ok in report["modules"].items() if not ok]
    problems += [name for name in ("chrome", "chromedriver", "agent_browser") if not report[name]]
    if launch and not problems:
        report["browser_launch"] = launch_check()
        problems += [f"{engine} 浏览器" for engine, result in report["browser_launch"].items() if result != "ok"]
    report["ok"] = not problems
    if problems:
        report["missing"] = problems
        report["hint"] = "请使用完整离线包；缺失的部分不会联网下载"
    return report


def dispatch(args) -> tuple[dict, int]:
    command, action = args.command, getattr(args, "action", None)
    if command == "project":
        if action == "create":
            return {"created": projects.create(args.name, args.url, args.category, args.id)}, 0
        if action == "list":
            return {"projects": projects.list_all()}, 0
        if action == "show":
            return projects.get(args.project), 0
        return projects.update(args.project, name=args.name, category=args.category, allowed_origins=args.allow_origin,
                               approvals=args.approve, timeout_seconds=args.timeout,
                               ignore_https_errors=None if args.insecure_tls is None else args.insecure_tls == "on"), 0
    if command == "context":
        return context(args.project), 0
    if command == "cases":
        return {"cases": context(args.project)["cases"]}, 0
    if command == "secret":
        if action == "set":
            secrets.set_secret(args.name, read_secret_value(args))
            return {"saved": args.name, "note": "已加密保存在本机；不会进入用例、报告或分享包"}, 0
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
                pool = [c for c in projects.cases(args.project) if c["kind"] == args.kind and ir.is_replayable(c)]
                chosen = [c["id"] for c in pool if not args.cases or c["id"] in args.cases]
                if not chosen:
                    raise ValueError("没有符合条件的可回放用例")
            run = runner.create(args.project, chosen, args.engine)
            final = runner.execute(run["id"])
        return run_summary(final), {"Pass": 0, "Fail": 1}.get(final["status"], 2)
    from .recorder import session

    if command == "record":
        if action == "start":
            return session.start(args.project, login=not args.no_login, as_login=args.as_login, url=args.url), 0
        if action == "stop":
            return session.stop(args.title, args.module, args.category), 0
        return getattr(session, action)(), 0
    if command == "browser":
        return session.browser(args.args), 0
    return session.check(args.kind, args.target, args.expected, args.attribute), 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = parser().parse_args(argv)
    db.init()
    real_stdout = sys.stdout
    try:
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
        raise
    return emit(result, code)


if __name__ == "__main__":
    sys.exit(main())
