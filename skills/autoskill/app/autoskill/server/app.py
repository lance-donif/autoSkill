"""Loopback-only HTTP API for the dashboard. It shows and runs; it never generates cases."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import threading
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from .. import __version__, bridge, command_input, context_service, data_preferences, data_refs, data_store, db, deletion, demo, paths, projects, runner, secrets, sessions, task_context, task_reports
from ..replay import ir
from . import jobs, metrics, scheduler
from .data import register as register_data
from .execution_requests import register as register_execution_requests
from .host_context import register as register_host_context
from .load_testing import register as register_load_testing
from .ui_assets import mount_ui

LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
# Windows may map .js to text/plain in the registry; browsers then refuse ES modules.
import mimetypes  # noqa: E402

for _type, _ext in (("text/javascript", ".js"), ("text/css", ".css"), ("image/svg+xml", ".svg")):
    mimetypes.add_type(_type, _ext)


def scope_of(request: Request) -> metrics.Scope:
    q = request.query_params
    return metrics.Scope.parse(q.get("projects"), q.get("start"), q.get("end"))


def page(request: Request) -> tuple[int, int]:
    q = request.query_params
    return max(1, min(200, int(q.get("limit", 50)))), max(0, int(q.get("offset", 0)))


def run_view(run: dict) -> dict:
    run = runner.public_run(run)
    run["case_ids"] = json.loads(run["case_ids"])
    project = db.one("SELECT name FROM projects WHERE id = ?", (run["project_id"],))
    run["project_name"] = project["name"] if project else run["project_id"]
    return run


def result_view(row: dict) -> dict:
    detail = json.loads(row["detail"])
    attempts = detail.get("attempts")
    if isinstance(attempts, list):
        for attempt in attempts:
            if isinstance(attempt, dict) and attempt.get("path"):
                with contextlib.suppress(ValueError):
                    attempt["path"] = str(data_refs.resolve(attempt["path"]))
    return {**row, "detail": detail}


def case_view(project_id: str, case: dict) -> dict:
    """A case as the dashboard shows it: readable steps, never raw secrets (they are references already)."""
    view = {k: case.get(k) for k in ("id", "kind", "title", "module", "priority", "risk", "revision", "expected")}
    view.update(project_id=project_id, depends_on=projects.depends_on(case), replayable=ir.is_replayable(case),
                category=case.get("extensions", {}).get("category", ""))
    if case["kind"] == "ui":
        automation = case.get("extensions", {}).get("automation") or {}
        view["start_url"] = automation.get("start_url")
        view["steps"] = [{"text": s.get("name") or ir.describe(s), "check": "assertion" in s,
                          "locators": [ir.describe_locator(locator) for locator in ir.locators(s)]}
                         for s in [*automation.get("actions", []), *automation.get("assertions", [])]]
    else:
        view["request"] = {k: case.get(k) for k in ("method", "path", "headers", "params", "json", "form") if case.get(k) is not None}
        view["assertions"] = case.get("assertions")
        view["extract"] = case.get("extensions", {}).get("extract", {})
        view["traffic"] = case.get("extensions", {}).get("traffic", "business")
    return view


def demo_site(port: int = 8790):
    """Serve the bundled Ticket Desk site from this process so the example project works on first click."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("ticket_desk", paths.SKILL / "examples" / "ticket-desk" / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        server = module.make_server(port)
    except OSError:
        return None  # already running (or the port is taken): use what is there
    threading.Thread(target=server.serve_forever, daemon=True, name="ticket-desk").start()
    return server


def create_app(port: int = 8787, start_worker: bool = True) -> FastAPI:
    data_store.ensure_initialized()
    db.init()
    worker = jobs.Worker()
    state: dict = {"demo_site": None}

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ARG001  # FastAPI passes its app to the lifespan callback.
        jobs.sweep()
        if any(p.get("id") == demo.PROJECT_ID and p.get("base_url") == demo.DEFAULT_URL for p in projects.list_all()):
            state["demo_site"] = demo_site()  # the example project needs its site
        if start_worker:
            worker.start()
            bridge.announce(port)
        yield
        bridge.withdraw()
        worker.close()
        if state["demo_site"]:
            state["demo_site"].shutdown()

    app = FastAPI(title="autoSkill", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    _register_http_handlers(app, port)
    _register_overview_routes(app)
    _register_project_routes(app, state)
    _register_download_routes(app)
    _register_settings_routes(app)
    _register_run_creation_route(app)
    _register_run_detail_routes(app)
    _register_deletion_routes(app)
    _register_task_report_routes(app)
    _register_schedule_routes(app)
    _register_context_routes(app)
    _register_workflow_routes(app)
    _register_asset_routes(app)
    _register_conversation_routes(app)
    _register_judge_routes(app)
    _register_host_routes(app)
    register_host_context(app)
    register_execution_requests(app)
    register_data(app)
    register_load_testing(app)
    _register_unknown_route(app)

    app.state.ui_revision = mount_ui(app, paths.APP / "ui")
    return app


def _register_http_handlers(app: FastAPI, port: int) -> None:
    """Register http handlers in their original order."""
    @app.middleware("http")
    async def local_only(request: Request, call_next):
        """Refuse DNS-rebinding hosts and cross-site writes; the API is for this machine's dashboard only."""
        host = urlsplit("http://" + request.headers.get("host", "")).hostname
        if host not in LOCAL_HOSTS:
            return JSONResponse({"detail": "仅允许本机访问"}, status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            allowed = {f"http://{h}:{port}" for h in ("127.0.0.1", "localhost")}
            if (origin and origin not in allowed) or request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "拒绝跨站请求"}, status_code=403)
        from ..workflow import use_binding

        try:
            session_id = _identity_header(request, "x-autoskill-session")
            _identity_header(request, "x-autoskill-host-session")
        except ValueError as error:
            return JSONResponse({"detail": str(error), "code": "validation_error"}, status_code=400)
        with task_context.use_session(session_id), use_binding(request.headers.get("x-autoskill-workflow-binding")):
            response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ValueError)
    async def bad_request(request: Request, error: ValueError):  # noqa: ARG001  # FastAPI exception handlers require the request parameter.
        return JSONResponse({"detail": str(error), "error": str(error), "code": getattr(error, "code", "validation_error"),
                             "details": getattr(error, "details", {})}, status_code=400)


def _identity_header(request: Request, name: str) -> str | None:
    """Decode explicit UTF-8 transport without imposing ASCII limits on session identities."""
    encoded = request.headers.get(name + "-b64")
    if encoded is None:
        return request.headers.get(name)
    try:
        return base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True).decode("utf-8")
    except (ValueError, UnicodeError) as error:
        raise ValueError("会话标识的 Base64 UTF-8 编码无效") from error


def _register_overview_routes(app: FastAPI) -> None:
    """Register overview routes in their original order."""
    @app.get("/api/status")
    def status():
        return {"name": "autoSkill", "version": __version__, "ui_revision": app.state.ui_revision, "projects": len(projects.list_all()),
                "runtime": {"bundled": paths.VENDOR.is_dir(), "chrome": bool(paths.chrome()),
                            "selenium": bool(paths.chrome(bundled=True) and paths.chromedriver()),
                            "recorder": bool(paths.agent_browser())},
                "browser": paths.browser_info(),
                "skill_path": str(paths.SKILL), "workspace": str(paths.workspace()), "case_skill": str(paths.case_skill()), "bridge": bridge.enabled(),
                "instance": os.environ.get("AUTOSKILL_INSTANCE_ID")}

    @app.post("/api/bridge")
    def run_command(body: dict, request: Request):
        """Run one autoskill command for a caller whose own process is sandboxed (see bridge.py).

        Never reachable from a web page: browsers cannot send this header cross-site without a preflight,
        which this service does not answer, and they always send an Origin header on POST.
        """
        if not bridge.enabled():
            raise HTTPException(403, "后台服务关闭了代为执行功能")
        if request.headers.get("x-autoskill-bridge") != "1" or request.headers.get("origin"):
            raise HTTPException(403, "只接受 autoskill 命令行的请求")
        argv = body.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            raise ValueError("argv 无效")
        stdin_text = body.get("stdin") if isinstance(body.get("stdin"), str) else None
        msys_root = body.get("msys_root") if isinstance(body.get("msys_root"), str) else None
        return bridge.execute(argv, stdin_text, msys_root, body.get("headed"), body.get("session_id"), body.get("case_skill"),
                              body.get("caller_cwd"), body.get("attachments"), body.get("workflow_binding"),
                              body.get("host", "generic"), body.get("host_session_id", ""))

    @app.get("/api/summary")
    def summary(request: Request, tz: int = 480):
        return metrics.summary(scope_of(request), tz)

    @app.get("/api/results")
    def results(request: Request, status: str | None = None, current: bool = False):
        limit, offset = page(request)
        return metrics.results(scope_of(request), status, current, limit, offset)

    @app.get("/api/runs")
    def runs(request: Request, trigger: str | None = None, status: str | None = None, exclude_load: bool = False):
        limit, offset = page(request)
        return metrics.runs(scope_of(request), trigger, status, limit, offset, exclude_load=exclude_load)

    @app.get("/api/cases")
    def cases(request: Request, kind: str | None = None, added: bool = False):
        return metrics.cases(scope_of(request), kind, added)


def _register_project_routes(app: FastAPI, state: dict) -> None:
    """Register project routes in their original order."""
    @app.get("/api/projects")
    def list_projects():
        return projects.list_all()

    @app.post("/api/projects")
    async def create_project(request: Request):
        body = await request.json()
        return projects.create(body.get("name", ""), body.get("url", ""), body.get("category", ""))

    @app.post("/api/demo")
    def install_demo():
        state["demo_site"] = state["demo_site"] or demo_site()
        return demo.install()

    @app.get("/api/projects/{project_id}")
    def project(project_id: str):
        info = projects.get(project_id)
        info["session"] = sessions.summary(project_id)
        info["credentials"] = projects.credentials(project_id)  # plain text: the settings page shows and edits them
        return info

    @app.patch("/api/projects/{project_id}")
    async def update_project(project_id: str, request: Request):
        body = await request.json()
        return projects.update(project_id, name=body.get("name"), category=body.get("category"),
                               allowed_origins=body.get("allowed_origins"), approvals=body.get("approvals"),
                               timeout_seconds=body.get("timeout_seconds"),
                               ignore_https_errors=body.get("ignore_https_errors"))

    @app.delete("/api/projects/{project_id}/session")
    def forget_session(project_id: str):
        projects.path(project_id)
        return {"cleared": sessions.clear(project_id)}

    @app.get("/api/projects/{project_id}/cases/{case_id}")
    def case(project_id: str, case_id: str):
        return case_view(project_id, projects.case(project_id, case_id))


def _register_download_routes(app: FastAPI) -> None:
    """Register download routes in their original order."""
    @app.get("/api/projects/{project_id}/postman")
    def postman_file(project_id: str, part: str = "collection"):
        """Download the Postman collection or environment. Secret values are never included."""
        from ..replay import postman

        cases = {c["id"]: c for c in projects.cases(project_id)}
        api_ids = [cid for cid, c in cases.items() if c["kind"] == "api"]
        if not api_ids:
            raise ValueError("项目里还没有接口用例")
        ordered = [cases[cid] for cid, _ in runner.build_plan(api_ids, cases) if cases[cid]["kind"] == "api"]
        info = projects.get(project_id)
        collection, environment = postman.export(info["name"], ordered, info["base_url"])
        chosen = environment if part == "environment" else collection
        name = f"{project_id}.postman_{'environment' if part == 'environment' else 'collection'}.json"
        return Response(json.dumps(chosen, ensure_ascii=False, indent=2), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/api/projects/{project_id}/cases/{case_id}/script")
    def script_file(project_id: str, case_id: str, engine: str = "playwright"):
        """Download one UI case as a standalone script, prerequisites included."""
        from ..replay import codegen

        if engine not in runner.ENGINES:
            raise ValueError("引擎只能是 playwright 或 selenium")
        cases = {c["id"]: c for c in projects.cases(project_id)}
        if case_id not in cases or cases[case_id]["kind"] != "ui" or not ir.is_replayable(cases[case_id]):
            raise ValueError("这条用例没有可导出的页面步骤")
        chain = [cases[cid] for cid, _ in runner.build_plan([case_id], cases) if cases[cid]["kind"] == "ui"]
        source = codegen.script(chain, projects.config(project_id)["base_url"], engine)
        name = f"{case_id.lower()}_{engine}.py"
        return Response(source, media_type="text/x-python; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/api/projects/{project_id}/report")
    def report_file(project_id: str, part: str = "excel"):
        """Download the project report: cases, steps, assertions and the latest result of each case."""
        from pathlib import Path

        from .. import reporting

        built = reporting.build(project_id)
        file = Path(built["markdown" if part == "markdown" else "excel"])
        return FileResponse(file, filename=file.name)


def _register_settings_routes(app: FastAPI) -> None:
    """Register settings routes in their original order."""
    @app.put("/api/secrets/{name}")
    async def put_secret(name: str, request: Request):
        """Store or replace a credential; project settings show it as plain text."""
        value = (await request.json()).get("value")
        if not isinstance(value, str) or not value:
            raise ValueError("请填写凭据内容")
        secrets.set_secret(name, value)
        return {"saved": name}

    @app.get("/api/settings")
    def get_settings():
        return data_preferences.read()

    @app.put("/api/settings")
    async def put_settings(request: Request):
        return data_preferences.update(await request.json())


def _register_run_creation_route(app: FastAPI) -> None:
    """Register run creation route in their original order."""
    @app.post("/api/runs")
    async def start_runs(request: Request):
        """Queue runs: one project, or a batch across projects. Each item is {project_id, case_ids?}."""
        body = await request.json()
        engine = body.get("engine", "playwright")
        items = body.get("items") or [{"project_id": body.get("project_id"), "case_ids": body.get("case_ids")}]
        batch = db.new_id("batch") if len(items) > 1 else None
        created, skipped = [], []
        for item in items:
            try:
                created.append(runner.create(item["project_id"], item.get("case_ids") or None, engine, batch_id=batch,
                                             headed=body.get("headed")))
            except ValueError as error:
                skipped.append({"project_id": item.get("project_id"), "reason": str(error)})
        if not created:
            raise ValueError(skipped[0]["reason"] if skipped else "没有可运行的用例")
        return {"batch_id": batch, "runs": [{"id": r["id"], "project_id": r["project_id"], "total": r["total"]} for r in created],
                "skipped": skipped}


def _register_run_detail_routes(app: FastAPI) -> None:
    """Register run detail routes in their original order."""
    @app.get("/api/runs/{run_id}")
    def run_detail(run_id: str):
        run = db.one("SELECT * FROM runs WHERE id = ?", (run_id,))
        if not run:
            raise HTTPException(404, "运行不存在")
        rows = db.rows("SELECT * FROM results WHERE run_id = ? ORDER BY id", (run_id,))
        return {**run_view(run), "results": [result_view(row) for row in rows], "test_report": task_reports.for_run(run_id)}

    @app.post("/api/runs/{run_id}/cancel")
    def cancel_run(run_id: str):
        return run_view(jobs.cancel(run_id))

    @app.post("/api/runs/{run_id}/rerun")
    async def rerun(run_id: str, request: Request):
        """Run again as a new run; `failed_only` keeps just the cases that did not pass."""
        body = await request.json()
        run = db.one("SELECT * FROM runs WHERE id = ?", (run_id,))
        if not run:
            raise HTTPException(404, "运行不存在")
        case_ids = json.loads(run["case_ids"])
        if body.get("failed_only"):
            rows = db.rows("SELECT case_id FROM results WHERE run_id = ? AND prerequisite = 0 AND status != 'Pass'", (run_id,))
            case_ids = [r["case_id"] for r in rows]
            if not case_ids:
                raise ValueError("这次运行没有未通过的用例")
        active = {c["id"] for c in projects.cases(run["project_id"])}
        kept = [c for c in case_ids if c in active]
        if not kept:
            raise ValueError("这些用例已被归档，无法重新运行")
        created = runner.create(run["project_id"], kept, body.get("engine") or run["engine"], headed=body.get("headed"))
        return {"id": created["id"], "total": created["total"], "dropped": len(case_ids) - len(kept)}

    @app.get("/api/runs/{run_id}/files/{name}")
    def run_file(run_id: str, name: str):
        """Screenshots of a run. Names are flat; anything else is refused."""
        folder = runner.evidence_dir(run_id).resolve()
        file = (folder / name).resolve()
        if file.parent != folder or file.suffix != ".png" or not file.is_file():
            raise HTTPException(404, "文件不存在")
        return FileResponse(file, media_type="image/png")


def _register_deletion_routes(app: FastAPI) -> None:
    @app.delete("/api/projects/{project_id}/cases/{case_id}")
    def delete_case(project_id: str, case_id: str):
        return deletion.delete_cases([{"project_id": project_id, "case_id": case_id}])

    @app.post("/api/cases/delete")
    async def delete_cases(request: Request):
        body = await request.json()
        if not isinstance(body, dict) or not isinstance(body.get("items"), list):
            raise ValueError("items 必须是包含 project_id、case_id 的数组")
        return await asyncio.to_thread(deletion.delete_cases, body["items"])

    @app.delete("/api/runs/{run_id}")
    def delete_run(run_id: str):
        return deletion.delete_runs([run_id])

    @app.post("/api/runs/delete")
    async def delete_runs(request: Request):
        body = await request.json()
        if not isinstance(body, dict) or not isinstance(body.get("run_ids"), list):
            raise ValueError("run_ids 必须是运行标识数组")
        return await asyncio.to_thread(deletion.delete_runs, body["run_ids"])


def _register_task_report_routes(app: FastAPI) -> None:
    @app.get("/api/task-reports/{report_id}")
    def task_report_status(report_id: str):
        return task_reports.get(report_id)

    @app.post("/api/task-reports/{report_id}/retry")
    async def task_report_retry(report_id: str):
        return await asyncio.to_thread(task_reports.retry, report_id)

    @app.get("/api/task-reports/{report_id}/excel")
    def task_report_excel(report_id: str):
        file = task_reports.download(report_id)
        return FileResponse(file, filename=file.name, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @app.get("/api/task-reports/{report_id}/markdown")
    def task_report_markdown(report_id: str):
        file = task_reports.download(report_id, format="markdown")
        return FileResponse(file, filename=file.name, media_type="text/markdown; charset=utf-8")

    @app.get("/api/task-reports/{report_id}/bugs")
    def task_report_bugs(report_id: str):
        file = task_reports.download(report_id, "bugs")
        return FileResponse(file, filename=file.name, media_type="text/markdown; charset=utf-8")


def _register_schedule_routes(app: FastAPI) -> None:
    """Register schedule routes in their original order."""
    @app.get("/api/tasks")
    def tasks(request: Request):
        if request.query_params.get("paged") == "1":
            return scheduler.page(scope_of(request).projects, offset=int(request.query_params.get("offset", 0)),
                                  limit=int(request.query_params.get("limit", 50)), all_items=request.query_params.get("all") == "1")
        return scheduler.list_all(scope_of(request).projects)

    @app.get("/api/tasks/{task_id}")
    def task(task_id: str):
        return scheduler.get(task_id)

    @app.post("/api/tasks")
    async def create_task(request: Request):
        spec = await request.json()
        spec.setdefault("host", request.headers.get("x-autoskill-host", "generic"))
        spec.setdefault("host_session_id", _identity_header(request, "x-autoskill-host-session") or "")
        return {**scheduler.create(spec), "queue_dispatcher": jobs.ensure_worker()}

    @app.patch("/api/tasks/{task_id}")
    async def update_task(task_id: str, request: Request):
        body = await request.json()
        if "enabled" in body and set(body) <= {"enabled", "expected_revision"}:
            result = scheduler.set_enabled(task_id, bool(body["enabled"]), expected_revision=body.get("expected_revision"))
            return {**result, **({"queue_dispatcher": jobs.ensure_worker()} if body["enabled"] else {})}
        return {**scheduler.update(task_id, body), "queue_dispatcher": jobs.ensure_worker()}

    @app.delete("/api/tasks/{task_id}")
    def delete_task(task_id: str):
        scheduler.delete(task_id)
        return {"deleted": task_id}

    @app.post("/api/tasks/{task_id}/run")
    async def run_task(task_id: str, request: Request):
        body = await request.json() if await request.body() else {}
        run = scheduler.run_now(task_id, idempotency_key=body.get("idempotency_key"))
        if run["status"] not in ("Queued", "awaiting_host"):
            raise ValueError(run.get("reason") or "任务没有启动")
        return {"id": run["id"], "status": run["status"], "queue_dispatcher": jobs.ensure_worker(),
                **({"delivery": run} if run["status"] == "awaiting_host" else {})}


def _reader_options(request: Request) -> dict:
    query = request.query_params
    return {"limit": int(query.get("limit", 50)), "cursor": query.get("cursor"), "all_items": query.get("all") == "1"}


def _register_context_routes(app: FastAPI) -> None:
    @app.get("/api/context/{project_id}")
    def context(project_id: str, request: Request):
        return context_service.context(project_id, **_reader_options(request), knowledge_limit=int(request.query_params.get("knowledge_limit", 5)),
                                       knowledge_cursor=request.query_params.get("knowledge_cursor"))

    @app.get("/api/case-definitions/{project_id}")
    def cases(project_id: str, request: Request):
        filters = {key: request.query_params.get(key) for key in ("kind", "module", "requirement")}
        return context_service.cases(project_id, **_reader_options(request), **filters, query=request.query_params.get("query", ""))

    @app.get("/api/case-definitions/{project_id}/{case_id}")
    def case(project_id: str, case_id: str):
        return context_service.case(project_id, case_id)

    @app.get("/api/knowledge/{project_id}")
    def knowledge(project_id: str, request: Request):
        return context_service.knowledge(project_id, **_reader_options(request), query=request.query_params.get("query", ""))

    @app.get("/api/knowledge/{project_id}/read")
    def read(project_id: str, request: Request):
        query = request.query_params
        return context_service.read_knowledge(project_id, query.get("file", ""), offset=int(query.get("offset", 0)),
                                              length=int(query.get("length", 12000)), content_revision=query.get("revision"), all_items=query.get("all") == "1")


def _workflow_action(workflow_id: str, action: str, body: dict) -> dict:
    from .. import workflow

    options = {"idempotency_key": body.get("idempotency_key", "")}
    if action == "revise":
        from pathlib import Path

        spec = command_input.normalize_payload(body.get("spec", {}), "workflow", "revise", Path(body.get("base_dir") or paths.workspace()))
        return workflow.revise(workflow_id, spec, **options, expected_hash=body.get("expected_hash"))
    if action == "next":
        return workflow.next_task(workflow_id, **options, owner=body.get("owner"), lease_seconds=body.get("lease_seconds", 300))
    if action in ("recover", "cancel", "resume"):
        return getattr(workflow, action)(workflow_id, **options)
    if action not in ("submit", "heartbeat", "release"):
        raise ValueError("没有这个工作流操作")
    options.update(lease_token=body.get("lease_token", ""), attempt_id=body.get("attempt_id", ""))
    if action == "submit":
        options["payload"] = body.get("payload", {})
    else:
        options["checkpoint"] = body.get("checkpoint")
        if action == "heartbeat":
            options["lease_seconds"] = body.get("lease_seconds", 300)
    return getattr(workflow, action)(workflow_id, body.get("item_id", ""), **options)


def _register_workflow_routes(app: FastAPI) -> None:
    @app.post("/api/workflows")
    async def create(request: Request):
        from pathlib import Path

        from .. import workflow

        body = await request.json()
        spec = command_input.normalize_payload(body.get("spec", {}), "workflow", "start", Path(body.get("base_dir") or paths.workspace()))
        return workflow.create(body.get("project_id", ""), spec, idempotency_key=body.get("idempotency_key", ""))

    @app.get("/api/workflows/{workflow_id}")
    def status(workflow_id: str, request: Request):
        from .. import workflow

        return workflow.status(workflow_id, limit=int(request.query_params.get("limit", 50)), cursor=request.query_params.get("cursor"))

    @app.get("/api/workflows/{workflow_id}/read")
    def read(workflow_id: str, request: Request):
        from .. import workflow

        query = request.query_params
        return workflow.read(workflow_id, kind=query.get("kind", "definition"), offset=int(query.get("offset", 0)), limit=int(query.get("limit", 50)),
                             head=query.get("head"), text_offset=int(query["text_offset"]) if "text_offset" in query else None,
                             text_length=int(query.get("text_length", 12000)))

    @app.post("/api/workflows/{workflow_id}/{action}")
    async def action(workflow_id: str, action: str, request: Request):
        return _workflow_action(workflow_id, action, await request.json())


def _register_asset_routes(app: FastAPI) -> None:
    @app.get("/api/engines")
    def engines(probe: bool = False, engine: str | None = None):
        from .. import engines as registry

        return {"engines": registry.probe(engine) if probe else registry.list_engines()}

    @app.get("/api/assets/{project_id}")
    def assets(project_id: str, request: Request):
        from .. import native_assets, paging

        pool = native_assets.list_assets(project_id)
        rows, page = paging.select(pool, content_revision=paging.revision(pool), query={"project": project_id}, **_reader_options(request))
        return {"assets": rows, "page": page}

    @app.get("/api/assets/{project_id}/{asset_id}")
    def asset(project_id: str, asset_id: str, head: str | None = None):
        from .. import native_assets

        return native_assets.get(project_id, asset_id, head=head)

    @app.post("/api/assets/{project_id}")
    async def import_asset(project_id: str, request: Request):
        from pathlib import Path

        from .. import native_assets

        body = await request.json()
        spec = command_input.normalize_payload(body, "asset", "import", Path(body.get("base_dir") or paths.workspace()))
        options = {key: spec[key] for key in ("name", "argv", "cwd", "source_info", "expected_head", "asset_id") if key in spec}
        return native_assets.import_asset(project_id, spec.get("source", ""), spec.get("engine", ""), **options)

    @app.post("/api/native-runs")
    async def run_asset(request: Request):
        from .. import native_run

        body = await request.json()
        run = native_run.enqueue(body.get("project_id", ""), body.get("asset_id", ""), head=body.get("head"), parameters=body.get("parameters"),
                                 session_id=body.get("session_id"), host=body.get("host", request.headers.get("x-autoskill-host", "generic")),
                                 host_session_id=body.get("host_session_id", _identity_header(request, "x-autoskill-host-session") or ""))
        return {**run, "queue_dispatcher": jobs.ensure_worker()}

    @app.get("/api/native-runs/{run_id}")
    def native_result(run_id: str, request: Request):
        from .. import native_reader

        return native_reader.status(run_id, case_id=request.query_params.get("case"), **_reader_options(request))

    @app.post("/api/native-runs/{run_id}/cancel")
    def cancel_native(run_id: str):
        from .. import native_reader, native_run

        native_run.cancel(run_id)
        return native_reader.status(run_id)


def _register_conversation_routes(app: FastAPI) -> None:
    @app.post("/api/conversations/{project_id}")
    async def capture(project_id: str, request: Request):
        from pathlib import Path

        from .. import conversation_capture

        body = await request.json()
        source = command_input.resolve_path(body.get("source", ""), Path(body.get("base_dir") or paths.workspace()))
        return conversation_capture.import_source(project_id, source, source_format=body.get("format"), name=body.get("name", ""),
                                                   expected_head=body.get("expected_head"))

    @app.get("/api/conversations/{project_id}/{capture_id}")
    def show(project_id: str, capture_id: str, head: str | None = None):
        from .. import conversation_capture

        return conversation_capture.get(project_id, capture_id, head=head)

    @app.post("/api/conversations/{project_id}/{capture_id}/build")
    async def build(project_id: str, capture_id: str, request: Request):
        from .. import conversation_capture

        body = await request.json()
        return conversation_capture.build_asset(project_id, capture_id, mapping=body.get("mapping"), asset_id=body.get("asset_id"),
                                                 name=body.get("name", ""), expected_head=body.get("expected_head"))


def _register_judge_routes(app: FastAPI) -> None:
    @app.post("/api/judgements/next")
    async def claim(request: Request):
        from .. import dialogue_judge

        body = await request.json()
        return dialogue_judge.next_judgement(idempotency_key=body.get("idempotency_key", ""), owner=body.get("owner"),
                                               lease_seconds=body.get("lease_seconds", 300), run_id=body.get("run_id"))

    @app.get("/api/judgements/{judge_id}")
    def show(judge_id: str):
        from .. import dialogue_judge

        return dialogue_judge.get(judge_id)

    @app.get("/api/judgements/{judge_id}/read")
    def read(judge_id: str, offset: int = 0, length: int = 12000):
        from .. import dialogue_judge

        return dialogue_judge.read_input(judge_id, offset=offset, length=length)

    @app.post("/api/judgements/{judge_id}/submit")
    async def submit(judge_id: str, request: Request):
        from .. import dialogue_judge

        body = await request.json()
        return dialogue_judge.submit(judge_id, attempt_id=body.get("attempt_id", ""), lease_token=body.get("lease_token", ""),
                                      idempotency_key=body.get("idempotency_key", ""), judgement=body.get("judgement", {}))

    @app.post("/api/judgements/{judge_id}/{action}")
    async def change_lease(judge_id: str, action: str, request: Request):
        from .. import dialogue_judge

        if action not in ("heartbeat", "release"):
            raise ValueError("没有这个判定操作")
        body = await request.json()
        options = {"lease_seconds": body.get("lease_seconds", 300)} if action == "heartbeat" else {}
        return getattr(dialogue_judge, action)(judge_id, attempt_id=body.get("attempt_id", ""), lease_token=body.get("lease_token", ""),
                                                 idempotency_key=body.get("idempotency_key", ""), **options)


def _register_host_routes(app: FastAPI) -> None:
    @app.get("/api/host/deliveries")
    def pending(host: str = "dsh", offset: int = 0, limit: int = 50):
        return scheduler.pending_deliveries(host, offset=offset, limit=limit)

    @app.post("/api/host/deliveries/{delivery_id}/claim")
    async def claim(delivery_id: str, request: Request):
        body = await request.json()
        return scheduler.claim_delivery(delivery_id, body.get("owner", ""), lease_seconds=body.get("lease_seconds", 120))

    @app.post("/api/host/deliveries/{delivery_id}/ack")
    async def ack(delivery_id: str, request: Request):
        body = await request.json()
        return scheduler.ack_delivery(delivery_id, body.get("token", ""), body.get("receipt", {}))

    @app.post("/api/host/deliveries/{delivery_id}/resolve")
    async def resolve(delivery_id: str, request: Request):
        body = await request.json()
        return scheduler.resolve_delivery(delivery_id, receipt=body.get("receipt"), retry=body.get("retry", False), reason=body.get("reason", ""))

    @app.post("/api/host/deliveries/{delivery_id}/release")
    async def release(delivery_id: str, request: Request):
        body = await request.json()
        return scheduler.release_delivery(delivery_id, body.get("token", ""), body.get("reason", ""))


def _register_unknown_route(app: FastAPI) -> None:
    """Register unknown route in their original order."""
    @app.get("/api/{rest:path}")
    def unknown(rest: str):  # noqa: ARG001  # The callback must bind the route's named rest path parameter.
        raise HTTPException(404, "没有这个接口")


def serve(port: int = 8787) -> None:
    import uvicorn

    print(f"autoSkill 仪表盘：http://127.0.0.1:{port}  （Ctrl+C 停止）", flush=True)  # noqa: T201  # The interactive server prints its startup address.
    uvicorn.run(create_app(port), host="127.0.0.1", port=port, log_level="warning")
