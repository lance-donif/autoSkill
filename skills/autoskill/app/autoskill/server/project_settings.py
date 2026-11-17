"""Local credential administration and asynchronous explicit project login."""
from __future__ import annotations

import asyncio

from fastapi import FastAPI, Request

from .. import paths, projects, runner, secrets
from . import jobs


def register(app: FastAPI) -> None:
    @app.get("/api/secrets")
    def credentials():
        return {"items": [{"name": name, "value": secrets.get_secret(name)} for name in secrets.names()]}

    @app.delete("/api/secrets/{name}")
    def delete_credential(name: str):
        if not secrets.NAME.fullmatch(name):
            raise ValueError("凭据名称格式不正确")
        secrets.delete_secret(name)
        return {"deleted": name}

    @app.post("/api/projects/{project_id}/login")
    async def login(project_id: str, request: Request):
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("登录参数需要是对象")
        force = body.get("force", False)
        headed = body.get("headed", paths.headed())
        if type(force) is not bool or type(headed) is not bool:
            raise ValueError("强制登录与显示浏览器窗口必须是布尔值")
        auth = projects.auth(project_id)
        chosen = list(dict.fromkeys(auth[key] for key in ("ui_case", "api_case") if auth.get(key)))
        if not chosen:
            raise ValueError("项目还没有登录用例；请让 AI 录制一次登录流程并保存")
        run = await asyncio.to_thread(runner.create, project_id, chosen, body.get("engine", "playwright"),
                                      trigger="login", force_login=force, headed=headed)
        return {"id": run["id"], "status": run["status"], "headed": bool(run["headed"]), "queue_dispatcher": jobs.ensure_worker()}
