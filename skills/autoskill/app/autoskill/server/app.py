"""Loopback-only HTTP API for the dashboard. It shows and runs; it never generates cases."""
from __future__ import annotations

import contextlib
import json
import os
import threading
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .. import __version__, db, demo, paths, projects, runner, secrets, sessions
from ..replay import ir
from . import jobs, metrics, scheduler

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
    run = dict(run)
    run["case_ids"] = json.loads(run["case_ids"])
    project = db.one("SELECT name FROM projects WHERE id = ?", (run["project_id"],))
    run["project_name"] = project["name"] if project else run["project_id"]
    return run


def case_view(project_id: str, case: dict) -> dict:
    """A case as the dashboard shows it: readable steps, never raw secrets (they are references already)."""
    view = {k: case.get(k) for k in ("id", "kind", "title", "module", "priority", "risk", "revision", "expected")}
    view.update(project_id=project_id, depends_on=projects.depends_on(case), replayable=ir.is_replayable(case),
                category=case.get("extensions", {}).get("category", ""))
    if case["kind"] == "ui":
        automation = case.get("extensions", {}).get("automation") or {}
        view["start_url"] = automation.get("start_url")
        view["steps"] = [{"text": s.get("name") or ir.describe(s), "check": "assertion" in s,
                          "locators": [ir.describe_locator(l) for l in ir.locators(s)]}
                         for s in [*automation.get("actions", []), *automation.get("assertions", [])]]
    else:
        view["request"] = {k: case.get(k) for k in ("method", "path", "headers", "params", "json") if case.get(k) is not None}
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
    db.init()
    worker = jobs.Worker()
    state: dict = {"demo_site": None}

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        jobs.sweep()
        if any(p.get("id") == demo.PROJECT_ID and p.get("base_url") == demo.DEFAULT_URL for p in projects.list_all()):
            state["demo_site"] = demo_site()  # the example project needs its site
        if start_worker:
            worker.start()
        yield
        worker.close()
        if state["demo_site"]:
            state["demo_site"].shutdown()

    app = FastAPI(title="autoSkill", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

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
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ValueError)
    async def bad_request(request: Request, error: ValueError):
        return JSONResponse({"detail": str(error)}, status_code=400)

    # --- overview ----------------------------------------------------
    @app.get("/api/status")
    def status():
        return {"name": "autoSkill", "version": __version__, "projects": len(projects.list_all()),
                "runtime": {"bundled": paths.VENDOR.is_dir(), "chrome": bool(paths.chrome()),
                            "selenium": bool(paths.chrome() and paths.chromedriver()),
                            "recorder": bool(paths.agent_browser())},
                "skill_path": str(paths.SKILL), "instance": os.environ.get("AUTOSKILL_INSTANCE_ID")}

    @app.get("/api/summary")
    def summary(request: Request, tz: int = 480):
        return metrics.summary(scope_of(request), tz)

    @app.get("/api/results")
    def results(request: Request, status: str | None = None, current: bool = False):
        limit, offset = page(request)
        return metrics.results(scope_of(request), status, current, limit, offset)

    @app.get("/api/runs")
    def runs(request: Request, trigger: str | None = None, status: str | None = None):
        limit, offset = page(request)
        return metrics.runs(scope_of(request), trigger, status, limit, offset)

    @app.get("/api/cases")
    def cases(request: Request, kind: str | None = None, added: bool = False):
        return metrics.cases(scope_of(request), kind, added)

    # --- projects ----------------------------------------------------
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
        return info

    @app.patch("/api/projects/{project_id}")
    async def update_project(project_id: str, request: Request):
        body = await request.json()
        return projects.update(project_id, name=body.get("name"), category=body.get("category"),
                               allowed_origins=body.get("allowed_origins"), approvals=body.get("approvals"),
                               timeout_seconds=body.get("timeout_seconds"))

    @app.delete("/api/projects/{project_id}/session")
    def forget_session(project_id: str):
        projects.path(project_id)
        return {"cleared": sessions.clear(project_id)}

    @app.get("/api/projects/{project_id}/cases/{case_id}")
    def case(project_id: str, case_id: str):
        return case_view(project_id, projects.case(project_id, case_id))

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

    @app.put("/api/secrets/{name}")
    async def put_secret(name: str, request: Request):
        """Write-only: a value can be stored or replaced, never read back."""
        value = (await request.json()).get("value")
        if not isinstance(value, str) or not value:
            raise ValueError("请填写凭据内容")
        secrets.set_secret(name, value)
        return {"saved": name}

    # --- runs --------------------------------------------------------
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
                created.append(runner.create(item["project_id"], item.get("case_ids") or None, engine, batch_id=batch))
            except ValueError as error:
                skipped.append({"project_id": item.get("project_id"), "reason": str(error)})
        if not created:
            raise ValueError(skipped[0]["reason"] if skipped else "没有可运行的用例")
        return {"batch_id": batch, "runs": [{"id": r["id"], "project_id": r["project_id"], "total": r["total"]} for r in created],
                "skipped": skipped}

    @app.get("/api/runs/{run_id}")
    def run_detail(run_id: str):
        run = db.one("SELECT * FROM runs WHERE id = ?", (run_id,))
        if not run:
            raise HTTPException(404, "运行不存在")
        rows = db.rows("SELECT * FROM results WHERE run_id = ? ORDER BY id", (run_id,))
        for row in rows:
            row["detail"] = json.loads(row["detail"])
        return {**run_view(run), "results": rows}

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
        created = runner.create(run["project_id"], kept, body.get("engine") or run["engine"])
        return {"id": created["id"], "total": created["total"], "dropped": len(case_ids) - len(kept)}

    @app.get("/api/runs/{run_id}/files/{name}")
    def run_file(run_id: str, name: str):
        """Screenshots of a run. Names are flat; anything else is refused."""
        folder = runner.evidence_dir(run_id).resolve()
        file = (folder / name).resolve()
        if file.parent != folder or file.suffix != ".png" or not file.is_file():
            raise HTTPException(404, "文件不存在")
        return FileResponse(file, media_type="image/png")

    # --- schedules ---------------------------------------------------
    @app.get("/api/tasks")
    def tasks(request: Request):
        return scheduler.list_all(scope_of(request).projects)

    @app.post("/api/tasks")
    async def create_task(request: Request):
        return scheduler.create(await request.json())

    @app.patch("/api/tasks/{task_id}")
    async def update_task(task_id: str, request: Request):
        body = await request.json()
        if set(body) == {"enabled"}:
            return scheduler.set_enabled(task_id, bool(body["enabled"]))
        return scheduler.update(task_id, body)

    @app.delete("/api/tasks/{task_id}")
    def delete_task(task_id: str):
        scheduler.delete(task_id)
        return {"deleted": task_id}

    @app.post("/api/tasks/{task_id}/run")
    def run_task(task_id: str):
        run = scheduler.run_now(task_id)
        if run["status"] != "Queued":
            raise ValueError(run.get("reason") or "任务没有启动")
        return {"id": run["id"]}

    @app.get("/api/{rest:path}")
    def unknown(rest: str):
        raise HTTPException(404, "没有这个接口")

    ui = paths.APP / "ui"
    if ui.is_dir():
        app.mount("/", StaticFiles(directory=ui, html=True), name="ui")
    return app


def serve(port: int = 8787) -> None:
    import uvicorn

    print(f"autoSkill 仪表盘：http://127.0.0.1:{port}  （Ctrl+C 停止）", flush=True)
    uvicorn.run(create_app(port), host="127.0.0.1", port=port, log_level="warning")
