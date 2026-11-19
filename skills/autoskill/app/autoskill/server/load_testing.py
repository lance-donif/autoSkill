"""Dashboard routes for load runs over saved browser and HTTP cases."""
from __future__ import annotations

import asyncio

from fastapi import FastAPI, Request

from .. import db, load_testing, projects, runner
from . import jobs, metrics


def register(app: FastAPI) -> None:
    @app.get("/api/load-tests/options")
    def options(request: Request):
        scope = metrics.Scope.parse(request.query_params.get("projects"), None, None)
        data = metrics.cases(scope, kind="api")
        grouped = {}
        for item in data["items"]:
            grouped.setdefault(item["project_id"], []).append(item)
        available = []
        for pid, items in grouped.items():
            cases = {case["id"]: case for case in projects.cases(pid)}
            for item in items:
                identity = item["case_id"]
                if identity not in cases or cases[identity].get("extensions", {}).get("traffic") == "resource":
                    continue
                try:
                    plan = runner.build_plan([identity], cases)
                except ValueError:
                    continue
                if all(cases[key]["kind"] == "api" for key, _ in plan):
                    available.append(item)
        data.update(items=available, total=len(available))
        eligible = {item["project_id"] for item in available}
        pool = projects.list_all()
        return {**data, "projects": [{"id": project["id"], "name": project["name"]} for project in pool
                                    if project["id"] in eligible]}

    @app.get("/api/load-tests")
    def history(request: Request, limit: int = 50, offset: int = 0):
        scope = metrics.Scope.parse(request.query_params.get("projects"), request.query_params.get("start"), request.query_params.get("end"))
        where, args = scope.where("created_at", "project_id")
        mode = request.query_params.get("mode")
        if mode is not None:
            if mode not in ("performance", "stress"):
                raise ValueError("测试模式须为 performance 或 stress")
            where += " AND load_mode = ?"
            args = [*args, mode]
        base = f"FROM runs WHERE family = 'load' AND {where}"
        total = db.one(f"SELECT COUNT(*) n {base}", args)["n"]
        rows = db.rows(f"SELECT * {base} ORDER BY created_at DESC LIMIT ? OFFSET ?", [*args, max(1, min(limit, 200)), max(0, offset)])
        return {"total": total, "items": [load_testing.summary(run) for run in rows]}

    @app.post("/api/load-tests")
    async def start(request: Request):
        body = await request.json()
        return await asyncio.to_thread(_start, body)

    @app.get("/api/load-tests/{run_id}")
    def detail(run_id: str):
        return load_testing.get(run_id)

    @app.post("/api/load-tests/{run_id}/cancel")
    def cancel(run_id: str):
        load_testing.get(run_id)
        jobs.cancel(run_id)
        return load_testing.get(run_id)


def _start(body: dict) -> dict:
    if not isinstance(body, dict):
        raise ValueError("压测请求必须是对象")
    items = body.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("请选择要压测的项目和用例")
    load = load_testing.configuration(body)
    engine = body.get("engine", "locust")
    if "headed" in body and type(body["headed"]) is not bool:
        raise ValueError("显示浏览器窗口必须为布尔值")
    prepared = []
    seen = set()
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("project_id"), str) or not item["project_id"]:
            raise ValueError("每组压测用例需要 project_id")
        if item["project_id"] in seen:
            raise ValueError("同一项目的用例请放在同一组")
        seen.add(item["project_id"])
        projects.reindex(item["project_id"])
        prepared.append(load_testing.prepare(item["project_id"], item.get("case_ids"), **load, engine=engine, headed=body.get("headed"), inputs=item.get("inputs", body.get("inputs"))))
    with db.connect() as connection:
        runs = [runner.enqueue(run, connection=connection) for run in prepared]
    return {"runs": runs, "skipped": [], "queue_dispatcher": jobs.ensure_worker()}
