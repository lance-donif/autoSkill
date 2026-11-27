"""Local credential, platform account, environment context and preflight endpoints."""
from __future__ import annotations

import asyncio

from fastapi import FastAPI, Request

from .. import execution_inputs, paths, platform_accounts, projects, runner, secrets, sessions
from ..replay import ir
from . import jobs


def _preflight_check(project_id: str, body: dict) -> dict:
    """Validate base_url, required login account and {{variables}} without starting a run."""
    case_ids = body.get("case_ids")
    if case_ids is not None and (not isinstance(case_ids, list) or any(not isinstance(item, str) for item in case_ids)):
        raise ValueError("case_ids 必须是用例标识数组")
    inputs = body.get("inputs")
    if inputs is not None and not isinstance(inputs, dict):
        raise ValueError("inputs 必须是 JSON 对象")
    environment_id = body.get("environment_id")
    account_id = body.get("account_id")
    cfg = projects.config(project_id)
    environment = projects.environment_of(cfg, environment_id or None)
    config = projects.with_environment(cfg, environment)
    base_url_missing = not bool(config.get("base_url"))
    active = {case["id"]: case for case in projects.cases(project_id)}
    selected = list(dict.fromkeys(case_ids or [key for key, case in active.items() if ir.is_replayable(case)]))
    missing_cases = [key for key in selected if key not in active or active[key]["lifecycle"] != "active"]
    if missing_cases:
        raise ValueError("这些用例不存在或已归档：" + "、".join(missing_cases))
    auth = projects.auth(project_id)
    extras = execution_inputs._auth_cases(auth, active, False)
    included = execution_inputs._closure(list(dict.fromkeys([*selected, *extras])), active) if selected else []
    cases = {key: active[key] for key in included}

    account_full = None
    account_missing = False
    if account_id:
        account_full = platform_accounts.get_account(str(account_id).strip(), include_password=True)
        if account_full is None:
            account_missing = True
    elif environment and environment.get("login_required"):
        account_full = projects.account_of(cfg, environment, None, use_active=True, include_password=True)
        needs_login = any(
            (cases[cid].get("extensions") or {}).get("auth_override", {}).get("mode", "inherit") == "inherit"
            and cid not in (auth.get("ui_case"), auth.get("api_case"))
            for cid in selected
        ) if selected else True
        account_missing = account_full is None and needs_login

    env_id = str((environment or {}).get("id") or "")
    platform_id = str((account_full or {}).get("platform_id") or (environment or {}).get("platform_id") or "")
    resolved_account_id = str((account_full or {}).get("id") or "")
    saved_session = execution_inputs._load_scoped_session(project_id, platform_id, env_id, resolved_account_id)
    var_result = (
        execution_inputs.find_missing_variables(cases, selected, config, inputs, auth=auth, account=account_full, saved_session=saved_session)
        if selected else {"missing": [], "by_case": {}}
    )
    missing_variables = var_result["missing"]
    by_case = var_result["by_case"]

    problems = []
    if base_url_missing:
        problems.append("当前环境未配置目标地址")
    if account_missing:
        problems.append("当前环境需要登录，但未配置可用账号")
    if missing_variables:
        problems.append("缺少变量：" + "、".join(missing_variables))
    return {
        "ok": not problems,
        "missing_variables": missing_variables,
        "by_case": by_case,
        "account_missing": account_missing,
        "base_url_missing": base_url_missing,
        "message": "；".join(problems),
    }


def _register_environment_routes(app: FastAPI) -> None:
    @app.put("/api/projects/{project_id}/environments")
    def put_environments(project_id: str, body: dict):
        """Replace the project's environment list and active selections without triggering a run."""
        if not isinstance(body, dict):
            raise ValueError("环境设置必须是对象")
        if set(body) - {"environments", "active_environment_id", "active_account_id"}:
            raise ValueError("环境设置只接受 environments、active_environment_id 和 active_account_id")
        if not isinstance(body.get("environments"), list):
            raise ValueError("environments 必须是数组")
        return projects.update(
            project_id,
            environments=body["environments"],
            active_environment_id=body.get("active_environment_id"),
            active_account_id=body.get("active_account_id"),
        )

    @app.put("/api/projects/{project_id}/active-context")
    def put_active_context(project_id: str, body: dict):
        """Switch active environment and/or active account for the cases page (no passwords in response)."""
        if not isinstance(body, dict):
            raise ValueError("运行上下文参数必须是对象")
        if set(body) - {"environment_id", "account_id"}:
            raise ValueError("运行上下文只接受 environment_id 和 account_id")
        fields = {}
        if "environment_id" in body:
            fields["active_environment_id"] = body.get("environment_id") or ""
        if "account_id" in body:
            fields["active_account_id"] = body.get("account_id") or ""
        return projects.update(project_id, **fields)

    @app.post("/api/projects/{project_id}/preflight")
    async def preflight(project_id: str, request: Request):
        body = await request.json() if await request.body() else {}
        if not isinstance(body, dict):
            raise ValueError("运行前检查参数必须是对象")
        return await asyncio.to_thread(_preflight_check, project_id, body)


def _register_platform_routes(app: FastAPI) -> None:
    @app.get("/api/platform-accounts")
    def list_platform_accounts():
        """Return shared platforms and accounts with plain-text passwords for project settings."""
        return {
            "platforms": platform_accounts.list_platforms(),
            "accounts": platform_accounts.list_accounts(include_password=True, include_shared=True),
        }

    @app.put("/api/platform-accounts/{account_id}")
    async def put_platform_account(account_id: str, request: Request):
        body = await request.json()
        account = platform_accounts.upsert_account(account_id, body)
        shared = platform_accounts.shared_projects_for(account["id"])
        return {"account": {**account, "shared_projects": shared}, "shared_projects": shared}

    @app.delete("/api/platform-accounts/{account_id}")
    def delete_platform_account(account_id: str):
        platform_accounts.delete_account(account_id)
        return {"deleted": account_id}

    @app.put("/api/platforms/{platform_id}")
    async def put_platform(platform_id: str, request: Request):
        body = await request.json()
        return platform_accounts.upsert_platform(platform_id, body)


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
        run = await asyncio.to_thread(
            runner.create,
            project_id,
            chosen,
            body.get("engine", "playwright"),
            trigger="login",
            force_login=force,
            headed=headed,
            environment_id=body.get("environment_id") or None,
            account_id=body.get("account_id") or None,
        )
        return {"id": run["id"], "status": run["status"], "headed": bool(run["headed"]), "queue_dispatcher": jobs.ensure_worker()}

    _register_environment_routes(app)
    _register_platform_routes(app)


def session_summary_for_project(project_id: str, info: dict) -> dict:
    """Resolve the active scoped session summary for GET /api/projects/{project_id}."""
    env = info.get("environment") or {}
    acc = info.get("account") or {}
    platform_id = str(acc.get("platform_id") or env.get("platform_id") or "")
    env_id = str(env.get("id") or "")
    account_id = str(acc.get("id") or "")
    if platform_id or account_id:
        return sessions.summary(project_id, platform_id=platform_id, environment_id=env_id, account_id=account_id)
    return sessions.summary(project_id)
