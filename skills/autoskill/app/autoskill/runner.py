"""Run cases: resolve prerequisites, reuse or renew the login, record every result.

A run is a row in the local database. `python -m autoskill.runner <run_id>`
executes it in its own process so a browser crash cannot take the dashboard down.
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from . import data_refs, db, execution_inputs, paths, platform_accounts, projects, runtime_identity, secrets, sessions, task_context, task_state
from .core import store
from .replay import api, ir, ui

ENGINES = ("playwright", "selenium")
MAX_PARALLEL = 2
_children: dict[int, subprocess.Popen] = {}
_children_lock = threading.Lock()


def prepare(project_id: str, case_ids: list[str] | None = None, engine: str = "playwright",
            trigger: str = "manual", task_id: str | None = None, batch_id: str | None = None,
            headed: bool | None = None, force_login: bool = False, session_id: str | None = None,
            *, candidate_cases: list[dict] | None = None, plan_path: str | Path | None = None,
            candidate_auth: dict | None = None, api_only: bool = False, inputs: dict | None = None,
            environment_id: str | None = None, account_id: str | None = None,
            strict_variables: bool = False, use_active_context: bool = True) -> dict:
    """Validate and freeze execution inputs without writing any database row."""
    if engine not in ENGINES:
        raise ValueError("回放引擎只能是 playwright 或 selenium")
    frozen, manifest = execution_inputs.freeze(project_id, case_ids, engine, candidate_cases=candidate_cases,
                                               candidate_auth=candidate_auth, api_only=api_only, inputs=inputs,
                                               environment_id=environment_id, account_id=account_id,
                                               strict_variables=strict_variables, use_active_context=use_active_context)
    chosen = manifest["selected"]
    identity = (task_context.session_id() if session_id is None else session_id) or ""
    with task_context.use_session(identity or None):
        selected_plan = str(plan_folder(plan_path) if plan_path else paths.case_skill())
    env = manifest.get("environment") or {}
    acc = manifest.get("account") or {}
    env_id = str(env.get("id") or "")
    resolved_account_id = str(acc.get("account_id") or "")
    platform_id = str(acc.get("platform_id") or env.get("platform_id") or "")
    lock_key = sessions.account_lock_key(platform_id=platform_id, environment_id=env_id, account_id=resolved_account_id)
    prepared = {"id": db.new_id("run"), "batch_id": batch_id, "project_id": project_id, "engine": engine,
                "trigger": trigger, "task_id": task_id, "status": "Queued", "case_ids": chosen,
                "total": len(chosen), "created_at": db.now(), "headed": int(paths.headed() if headed is None else bool(headed)),
                "force_login": int(force_login), "session_id": identity, "family": "replay", "plan_path": selected_plan,
                "environment_id": env_id, "environment_name": str(env.get("name") or ""),
                "account_id": resolved_account_id, "account_label": str(acc.get("label") or ""),
                "account_lock_key": lock_key, **frozen}
    if candidate_cases is None:
        from . import plan

        plan.capture_linked_execution(prepared, manifest)
    return prepared


def enqueue(prepared: dict, connection=None) -> dict:
    """Insert prepared execution intent, optionally inside the caller's scheduling transaction."""
    if connection is None:
        with db.connect() as conn:
            return enqueue(prepared, connection=conn)
    if connection.execute("SELECT 1 FROM deleted_runs WHERE id = ?", (prepared["id"],)).fetchone():
        raise ValueError("此执行记录已删除，请新建运行")
    columns = {row[1] for row in connection.execute("PRAGMA table_info(runs)")}
    stored = {key: value for key, value in prepared.items() if key in columns}
    if stored.get("input_manifest"):
        stored["input_manifest"] = data_refs.store(stored["input_manifest"])
    if stored.get("plan_path"):
        stored["plan_path"] = plan_reference(stored["plan_path"])
    keys = ", ".join(stored)
    marks = ", ".join("?" for _ in stored)
    values = [json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value for value in stored.values()]
    connection.execute(f"INSERT INTO runs ({keys}) VALUES ({marks})", values)
    return public_run(stored)


def plan_folder(value: str | Path) -> Path:
    """Rebase managed sheets; explicit external sheets retain their existing local contract."""
    try:
        return data_refs.resolve(value)
    except data_refs.UnmappedPathError:
        external = Path(value).expanduser()
        if not external.is_absolute():
            raise
        return external.resolve()


def plan_reference(value: str | Path) -> str:
    try:
        return data_refs.store(value)
    except data_refs.UnmappedPathError:
        return str(plan_folder(value))


def public_run(run: dict, *, state: dict | None = None) -> dict:
    """Project stored locations for callers without changing durable rows or frozen evidence."""
    result = dict(run)
    from . import candidate_publication

    publication = candidate_publication.receipt(run["id"])
    if publication is not None:
        result["publication"] = publication
    if result.get("input_manifest"):
        # Unmapped historical locations remain visible instead of being guessed.
        with contextlib.suppress(ValueError):
            result["input_manifest"] = str(data_refs.resolve(result["input_manifest"]))
    if result.get("plan_path"):
        # A foreign external sheet is provenance, not a path on this machine.
        with contextlib.suppress(ValueError):
            result["plan_path"] = str(plan_folder(result["plan_path"]))
    from . import task_state

    if "status" in run and "input_hash" in run and "total" in run:
        result["state"] = state if state is not None else task_state.run(run)
    return result


def create(project_id: str, case_ids: list[str] | None = None, engine: str = "playwright",
           trigger: str = "manual", task_id: str | None = None, batch_id: str | None = None,
           headed: bool | None = None, force_login: bool = False, session_id: str | None = None,
           *, candidate_cases: list[dict] | None = None, plan_path: str | Path | None = None,
           candidate_auth: dict | None = None, api_only: bool = False, inputs: dict | None = None,
           environment_id: str | None = None, account_id: str | None = None,
           strict_variables: bool = False, use_active_context: bool = True) -> dict:
    """Validate, freeze and queue a run through the same API used by transactional schedules."""
    projects.reindex(project_id)
    return enqueue(prepare(project_id, case_ids, engine, trigger, task_id, batch_id, headed, force_login, session_id,
                           candidate_cases=candidate_cases, plan_path=plan_path, candidate_auth=candidate_auth,
                           api_only=api_only, inputs=inputs, environment_id=environment_id, account_id=account_id,
                           strict_variables=strict_variables, use_active_context=use_active_context))


def evidence_dir(run_id: str) -> Path:
    return paths.local() / "runs" / run_id


def make_driver(engine: str, config: dict):
    if engine == "selenium":
        from .replay.selenium_driver import SeleniumDriver

        return SeleniumDriver(config, headless=not paths.headed())
    from .replay.playwright_driver import PlaywrightDriver

    return PlaywrightDriver(config, headless=not paths.headed())


def blocked(reason: str, category: str) -> dict:
    return {"status": "Blocked", "target_started": False, "reason": reason, "category": category, "actual": "", "assertions": [],
            "duration_ms": 0}


def cancelled() -> dict:
    return {**blocked("运行已取消", "cancelled"), "status": "Cancelled"}


def build_plan(chosen: list[str], cases: dict) -> list[tuple[str, bool]]:
    """Order prerequisites and declared postconditions once, keeping automatic checks distinct from setup."""
    order: list[str] = []
    visiting: set[str] = set()
    targets = list(dict.fromkeys(chosen))

    def visit(case_id: str, trail: tuple) -> None:
        if case_id in order:
            return
        if case_id in visiting:
            raise ValueError("前置用例形成了循环：" + " → ".join([*trail, case_id]))
        if case_id not in cases:
            raise ValueError(f"前置用例 {case_id} 不存在或已归档")
        visiting.add(case_id)
        for dep in projects.depends_on(cases[case_id]):
            visit(dep, (*trail, case_id))
        visiting.discard(case_id)
        order.append(case_id)

    for case_id in targets:
        visit(case_id, ())
        validate_postconditions(cases, order)
        for identity in order:
            for target in cases[identity].get("extensions", {}).get("postconditions", []):
                if target not in targets:
                    targets.append(target)
    return [(case_id, case_id not in targets) for case_id in order]


def needs(case_id: str, target: str | None, cases: dict) -> bool:
    """Does the case depend, directly or not, on `target`?"""
    if not target:
        return False
    stack, seen = list(projects.depends_on(cases[case_id])), set()
    while stack:
        dep = stack.pop()
        if dep == target:
            return True
        if dep not in seen and dep in cases:
            seen.add(dep)
            stack.extend(projects.depends_on(cases[dep]))
    return False


def _api_assertion(case: dict) -> bool:
    assertions = case.get("assertions", {})
    return store.has_real_assertion(assertions) or assertions.get("status", 0) >= 400


def validate_postconditions(cases: dict, selected=None) -> None:
    """A postcondition is an explicit downstream API oracle, never an arbitrary later success."""
    for identity in cases if selected is None else selected:
        case = cases[identity]
        extensions = case.get("extensions", {})
        if "postconditions" not in extensions:
            continue
        targets = extensions["postconditions"]
        if (case["kind"] != "api" or not isinstance(targets, list) or not targets
                or any(not isinstance(target, str) or not target for target in targets) or len(set(targets)) != len(targets)):
            raise ValueError(f"{identity}.extensions.postconditions 必须是非空且不重复的 API 用例编号列表")
        for target in targets:
            check = cases.get(target)
            if target == identity or not check or check.get("lifecycle") != "active" or check.get("kind") != "api":
                raise ValueError(f"{identity} 的后置验证 {target} 必须指向另一个存在且启用的 API 用例")
            if not _api_assertion(check) or check.get("extensions", {}).get("oracle", {}).get("source") == "observation":
                raise ValueError(f"{identity} 的后置验证 {target} 缺少独立业务断言")
            if not needs(target, identity, cases):
                raise ValueError(f"{identity} 的后置验证 {target} 必须通过 depends_on 依赖该动作")


class Session:
    """Everything one run shares: browser, HTTP client, variables, login handling."""

    def __init__(self, run: dict, force_login: bool = False):
        self.run = run
        self.pid = run["project_id"]
        self.manifest = execution_inputs.load(run, check_engine=True)
        self.config = self.manifest["config"]
        self.config["_execution_runtime"] = {key: value["path"] if value else None
                                             for key, value in self.manifest["engine"]["runtime"].items()}
        self.cases = self.manifest["cases"]
        self.auth = self.manifest["auth"]
        self.environment = self.manifest.get("environment") or None
        self.account = self.manifest.get("account") or None
        self.platform = self.manifest.get("platform") or None
        self.account_credentials = self.manifest.get("_account_credentials") or {}
        self.override_accounts = self.manifest.get("override_accounts") or {}
        self.api_only = bool(self.manifest.get("api_only"))
        self.force_login = force_login or self.api_only
        self.saved = {} if self.api_only else self.manifest["saved_session"]
        self._override_sessions = {} if self.api_only else copy.deepcopy(self.manifest.get("override_sessions") or {})
        self._override_clients: dict[str, object] = {}
        self._override_logged_api: set[str] = set()
        self._override_logged_ui: set[str] = set()
        env_id = str((self.environment or {}).get("id") or "")
        platform_id = str((self.account or {}).get("platform_id") or (self.environment or {}).get("platform_id") or "")
        acc_id = str((self.account or {}).get("account_id") or "")
        self.session_scope = (platform_id, env_id, acc_id)
        self.rand = self.manifest["variables"]["RAND"]
        self.count = 0
        self.explicit_inputs: dict = copy.deepcopy(self.manifest.get("inputs") or {})
        self.variables: dict = copy.deepcopy(self.manifest["variables"])
        self.driver = None
        self.driver_error = ""
        self.client = api.new_client(self.config, self.saved.get("ui"))
        self.relogged = False
        self.attempts: dict[str, list[dict]] = {}
        self.case_contexts: dict[str, tuple[dict, dict]] = {}
        self._cancelled = bool(run.get("cancel"))

    def _apply_explicit_inputs(self, target: dict | None = None) -> None:
        """Preserve highest priority for explicit run inputs over extracted or environment values."""
        bucket = self.variables if target is None else target
        if self.explicit_inputs:
            bucket.update(copy.deepcopy(self.explicit_inputs))

    def _save_scoped_session(self, *, ui: dict | None = None, variables: dict | None = None,
                             scope: tuple[str, str, str] | None = None) -> None:
        platform_id, env_id, acc_id = scope if scope is not None else self.session_scope
        if platform_id or acc_id:
            sessions.save(self.pid, ui=ui, variables=variables,
                          platform_id=platform_id, environment_id=env_id, account_id=acc_id)
        else:
            sessions.save(self.pid, ui=ui, variables=variables)

    def _env_meta(self) -> dict:
        return {
            "id": str((self.environment or {}).get("id") or ""),
            "name": str((self.environment or {}).get("name") or ""),
            "base_url": str(self.config.get("base_url") or ""),
        }

    def _account_runtime_vars(self, account_id: str) -> dict:
        creds = self.account_credentials.get(account_id) or {}
        extra = {key: copy.deepcopy(value) for key, value in (creds.get("variables") or {}).items() if value not in (None, "")}
        if creds.get("username"):
            extra.setdefault("username", creds["username"])
        if creds.get("password"):
            extra.setdefault("password", creds["password"])
        return extra

    def _effective_auth(self, case: dict) -> tuple[str, dict, tuple[str, str, str] | None, str]:
        override = (case.get("extensions") or {}).get("auth_override")
        if not isinstance(override, dict):
            override = {"mode": "inherit"}
        mode = str(override.get("mode") or "inherit")
        if mode == "none":
            return "none", platform_accounts.summary_for_result(None, mode="none"), None, ""
        if mode == "account":
            oid = str(override.get("account_id") or "").strip()
            if self.account and self.account.get("account_id") == oid:
                return "account", copy.deepcopy(self.account), self.session_scope, ""
            summary = self.override_accounts.get(oid)
            if not summary:
                return "account", {"mode": "account", "account_id": oid, "label": oid, "username": "", "platform_id": ""}, None, f"用例指定的账号 {oid} 不存在"
            env_id = str((self.environment or {}).get("id") or "")
            pid = str(summary.get("platform_id") or (self.environment or {}).get("platform_id") or "")
            return "account", copy.deepcopy(summary), (pid, env_id, oid), ""
        if self.account:
            return "account", copy.deepcopy(self.account), self.session_scope, ""
        return "inherit", platform_accounts.summary_for_result(None, mode="inherit"), self.session_scope, ""

    def _effective_entry(self, case: dict, auth_mode: str) -> dict | None:
        case_entry = (case.get("extensions") or {}).get("entry")
        if isinstance(case_entry, dict):
            return case_entry
        if auth_mode == "none":
            return None
        env_entry = (self.environment or {}).get("entry")
        return env_entry if isinstance(env_entry, dict) else None

    def cancel_requested(self) -> bool:
        if not self._cancelled:
            current = db.one("SELECT cancel FROM runs WHERE id = ?", (self.run["id"],))
            self._cancelled = bool(current and current["cancel"])
        return self._cancelled

    def close(self) -> None:
        self.client.close()
        for client in self._override_clients.values():
            with contextlib.suppress(Exception):
                client.close()
        self._override_clients.clear()
        if self.driver:
            if paths.headed():
                import time

                time.sleep(1.5)  # leave the last page on screen for a moment
            try:
                self.driver.close()
            except Exception:  # noqa: BLE001  # Driver cleanup must not replace the case result with a cleanup failure.
                return

    def browser(self):
        if self.api_only:
            raise ValueError("独立接口运行不能启动浏览器")
        if self.driver is None and not self.driver_error:
            try:
                self.driver = make_driver(self.run["engine"], self.config)
                self.driver.cancel_requested = self.cancel_requested
            except ValueError as error:
                self.driver_error = str(error)
            except Exception as error:  # noqa: BLE001  # Any browser startup failure becomes the run's environment result.
                reason = paths.restricted()
                detail = str(error).strip().splitlines()[0][:200] if str(error).strip() else ""
                self.driver_error = (f"浏览器无法启动：{type(error).__name__}" + (f"（{detail}）" if detail else "")
                                     + (f"。当前进程受限：{reason}" if reason else "。运行 autoskill doctor --launch 可以单独检查浏览器"))
        return self.driver

    def shot(self, case_id: str) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]", "_", case_id) + ".png"

    def absolute(self, url: str) -> str:
        """Resolve a case URL against the frozen environment address, moving the project's own origin."""
        environment = self.config.get("_environment") or {}
        return ir.absolute_url(ir.expand(url), self.config["base_url"], environment.get("recorded_base_url") or None)

    def attempt(self, case: dict, kind: str, invoke) -> dict:
        """Journal before invoking an adapter; each response and screenshot has its own durable identity."""
        attempt_id = "attempt-" + uuid.uuid4().hex
        folder = evidence_dir(self.run["id"])
        file = folder / "attempts" / (attempt_id + ".json")
        receipt = {"schema_version": 1, "id": attempt_id, "run_id": self.run["id"], "case_id": case["id"],
                   "input_hash": self.run["input_hash"], "case_content_hash": self.manifest["case_hashes"][case["id"]],
                   "case_scope_hash": self.manifest["scope_hashes"][case["id"]], "kind": kind,
                   "started_at": db.now(), "state": "started", "outcome": None}
        execution_inputs.write_json(file, receipt)
        def mark_started():
            receipt["target_started"] = True
            execution_inputs.write_json(file, receipt)
        token = execution_inputs.on_target_start.set(mark_started if kind in ("case", "captcha_retry", "relogin_retry") else None)
        try:
            outcome = cancelled() if self.cancel_requested() else invoke(attempt_id)
        except Exception as error:  # noqa: BLE001  # Preserve an attempt receipt even when an adapter unexpectedly exits its structured path.
            outcome = cancelled() if isinstance(error, ui.StepError) and error.category == "cancelled" else blocked("执行器出错：" + type(error).__name__, "environment")
        finally:
            execution_inputs.on_target_start.reset(token)
        if receipt.get("target_started"):
            outcome["target_started"] = True
            if outcome["status"] == "Pass" and self.cancel_requested():
                outcome.update(status="Cancelled", category="cancelled", reason="目标执行期间已取消")
        outcome = {**outcome, **verification(case, outcome, probe=kind in ("login_probe", "session_reuse"))}
        artifacts = _register_screenshots(folder, outcome)
        receipt.update(state="completed", finished_at=db.now(), outcome=outcome, artifacts=artifacts)
        execution_inputs.write_json(file, receipt)
        reference = {"id": attempt_id, "path": data_refs.store(file), "sha256": execution_inputs.content_hash(receipt),
                     "case_id": case["id"], "kind": kind, "status": outcome["status"], "business_status": outcome["business_status"]}
        self.attempts.setdefault(case["id"], []).append({"ref": reference, "outcome": copy.deepcopy(outcome)})
        return outcome

    def api_replay(self, client, case: dict, variables: dict, *, kind: str = "case", managed_session: bool = False) -> dict:
        def replay(_attempt):
            outcome = api.run_case(client, case, self.config, variables)
            self._apply_explicit_inputs(variables)
            status = (outcome.get("response") or {}).get("status")
            if (managed_session and outcome["category"] == "assertion_failed" and status in (401, 403)
                    and case["assertions"]["status"] not in (401, 403)):
                outcome["category"] = "session_expired"
            return outcome
        return self.attempt(case, kind, replay)

    def summarize(self, case: dict, outcome: dict) -> dict:
        """Keep a witnessed business failure even if a later retry or environment result differs."""
        if not self.attempts.get(case["id"]):
            self.attempt(case, "not_executed", lambda _attempt: outcome)
        return summarize_outcomes(case, self.attempts.get(case["id"], []), outcome)

    def replay(self, case: dict, state: dict | None = None, *, entry: dict | None = None) -> dict:
        """Replay one UI case in a clean context. A misread captcha is not a defect: try again with a fresh image."""
        actions = case["extensions"]["automation"].get("actions", [])
        for index in range(3 if any(step.get("action") == "captcha" for step in actions) else 1):
            if self.cancel_requested():
                return cancelled()
            self.driver.reset(state)
            outcome = self.attempt(
                case,
                "captcha_retry" if index else "case",
                lambda attempt_id: ui.run_case(
                    self.driver,
                    case,
                    self.config,
                    evidence_dir(self.run["id"]),
                    self.shot(attempt_id),
                    **({"entry": entry} if entry else {}),
                ),
            )
            if outcome["status"] != "Fail" or self.cancel_requested():
                break
        return outcome

    # --- login -------------------------------------------------------
    def ui_login(self, case: dict, force: bool, *, saved: dict | None = None,
                 scope: tuple[str, str, str] | None = None, update_default_client: bool = True) -> dict:
        driver = self.browser()
        if not driver:
            return blocked(self.driver_error, "environment")
        bucket = self.saved if saved is None else saved
        state = bucket.get("ui")
        probes = case["extensions"]["automation"].get("assertions", [])
        if state and probes and not force:
            driver.reset(state)
            def probe(_attempt):
                driver.goto(self.absolute(case["extensions"]["automation"]["start_url"]))
                rows = [ui.check(driver, step, 3.0) for step in probes]
                return {"status": "Pass" if all(row["passed"] for row in rows) else "Fail", "actual": "检查已保存的登录态",
                        "reason": "", "category": "", "assertions": rows, "duration_ms": 0, "reused": True}
            trial = self.attempt(case, "login_probe", probe)
            if trial["status"] == "Pass":
                return trial
        outcome = self.replay(case)
        if outcome["status"] == "Pass":
            bucket["ui"] = driver.storage_state()
            self._save_scoped_session(ui=bucket["ui"], scope=scope)
            if update_default_client:
                self.client.close()
                self.client = api.new_client(self.config, bucket["ui"])
        return outcome

    def api_login(self, case: dict, force: bool, *, client=None, variables: dict | None = None,
                  saved: dict | None = None, scope: tuple[str, str, str] | None = None) -> dict:
        wanted = list(case.get("extensions", {}).get("extract", {}))
        bucket = self.saved if saved is None else saved
        target_vars = self.variables if variables is None else variables
        target_client = self.client if client is None else client
        kept = bucket.get("variables", {})
        if not force and wanted and all(name in kept for name in wanted):
            probe = self.cases.get(self.auth.get("api_check_case", ""))
            trial = dict(kept)
            if not probe or self.api_replay(target_client, probe, trial, kind="login_probe", managed_session=True)["status"] == "Pass":
                target_vars.update({name: kept[name] for name in wanted})
                self._apply_explicit_inputs(target_vars)
                return self.attempt(case, "session_reuse", lambda _attempt:
                                    {"status": "Pass", "actual": "复用已保存的登录信息", "reason": "", "category": "",
                                     "assertions": [], "duration_ms": 0, "reused": True})
        outcome = self.api_replay(target_client, case, target_vars, managed_session=True)
        if outcome["status"] == "Pass" and wanted:
            bucket["variables"] = {name: target_vars[name] for name in wanted if name in target_vars}
            if not self.api_only:
                self._save_scoped_session(variables=bucket["variables"], scope=scope)
        return outcome

    def state_variables(self, saved: dict | None = None, variables: dict | None = None) -> None:
        """API variables that live in the browser login (for example a token kept in localStorage)."""
        bucket = self.saved if saved is None else saved
        target = self.variables if variables is None else variables
        stored = {item["name"]: item["value"] for origin in bucket.get("ui", {}).get("origins", [])
                  for item in origin.get("localStorage", [])}
        for name, source in self.auth.get("state_variables", {}).items():
            if source.get("name") in stored:
                target[name] = stored[source["name"]]
        self._apply_explicit_inputs(target)

    def _run_override_api_case(self, case: dict, account_meta: dict, scope: tuple[str, str, str]) -> dict:
        case_id = case["id"]
        oid = account_meta["account_id"]
        saved_bucket = self._override_sessions.setdefault(oid, {})
        client = self._override_clients.setdefault(oid, api.new_client(self.config, saved_bucket.get("ui")))
        case_vars = {**self.variables, **self._account_runtime_vars(oid), **saved_bucket.get("variables", {})}
        self._apply_explicit_inputs(case_vars)
        if not self.api_only:
            self.state_variables(saved_bucket, case_vars)
        login = self.cases.get(self.auth.get("api_case", ""))
        needs_api_login = login is not None and needs(case_id, login["id"], self.cases)
        if needs_api_login and oid not in self._override_logged_api:
            login_res = self.api_login(login, self.force_login, client=client, variables=case_vars, saved=saved_bucket, scope=scope)
            if login_res["status"] != "Pass":
                return blocked(f"账号「{account_meta['label']}」登录失败：{login_res.get('reason') or login_res['status']}", "session_expired")
            self._override_logged_api.add(oid)
        outcome = self.api_replay(client, case, case_vars, managed_session=needs_api_login)
        if outcome["category"] == "session_expired" and needs_api_login and not self.cancel_requested():
            if self.api_login(login, force=True, client=client, variables=case_vars, saved=saved_bucket, scope=scope)["status"] != "Pass":
                return blocked("登录已过期，重新登录没有成功", "session_expired")
            outcome = self.api_replay(client, case, case_vars, kind="relogin_retry", managed_session=True)
            outcome["relogin"] = True
        return outcome

    def _run_api_case(self, case: dict, auth_mode: str, account_meta: dict, scope: tuple[str, str, str] | None) -> dict:
        case_id = case["id"]
        if auth_mode == "none":
            anonymous_vars = {**self.variables}
            fresh = api.new_client(self.config)
            try:
                return self.api_replay(fresh, case, anonymous_vars)
            finally:
                fresh.close()
        if scope is not None and scope != self.session_scope:
            return self._run_override_api_case(case, account_meta, scope)
        if not self.api_only:
            self.state_variables()
        if case_id == self.auth.get("api_case"):
            acc_id = account_meta.get("account_id") or ""
            run_vars = self.variables
            if acc_id:
                extra = self._account_runtime_vars(acc_id)
                for key, value in extra.items():
                    run_vars.setdefault(key, value)
                self._apply_explicit_inputs(run_vars)
            return self.api_login(case, self.force_login)
        if not projects.depends_on(case):
            # A case with no prerequisite is sent without the run's cookies: "rejected when not
            # signed in" must stay true after a login earlier in the same run. Cookies it sets
            # are kept for the cases that depend on it.
            fresh = api.new_client(self.config)
            try:
                outcome = self.api_replay(fresh, case, self.variables)
                for cookie in fresh.cookies.jar:
                    self.client.cookies.jar.set_cookie(cookie)
            finally:
                fresh.close()
            return outcome
        login = self.cases.get(self.auth.get("api_case", ""))
        needs_api_login = login is not None and needs(case_id, login["id"], self.cases)
        managed_session = needs_api_login or needs(case_id, self.auth.get("ui_case"), self.cases)
        outcome = self.api_replay(self.client, case, self.variables, managed_session=managed_session)
        if outcome["category"] == "session_expired" and needs_api_login and not self.relogged and not self.cancel_requested():
            self.relogged = True
            if self.api_login(login, force=True)["status"] != "Pass":
                return blocked("登录已过期，重新登录没有成功", "session_expired")
            outcome = self.api_replay(self.client, case, self.variables, kind="relogin_retry", managed_session=True)
            outcome["relogin"] = True
        return outcome

    def _run_ui_case(self, case: dict, auth_mode: str, account_meta: dict, scope: tuple[str, str, str] | None) -> dict:
        case_id = case["id"]
        entry = self._effective_entry(case, auth_mode)
        if case_id == self.auth.get("ui_case"):
            return self.ui_login(case, self.force_login)
        driver = self.browser()
        if not driver:
            return blocked(self.driver_error, "environment")
        if auth_mode == "none":
            return self.replay(case, None, entry=entry)
        login = self.cases.get(self.auth.get("ui_case", ""))
        signed_in = bool(login) and needs(case_id, login["id"], self.cases)
        if scope is not None and scope != self.session_scope:
            oid = account_meta["account_id"]
            saved_bucket = self._override_sessions.setdefault(oid, {})
            if signed_in and oid not in self._override_logged_ui:
                extra = self._account_runtime_vars(oid)
                ir.set_variables({**self.variables, **extra, **self.explicit_inputs})
                login_res = self.ui_login(login, self.force_login, saved=saved_bucket, scope=scope, update_default_client=False)
                if login_res["status"] != "Pass":
                    return blocked(f"账号「{account_meta['label']}」登录失败：{login_res.get('reason') or login_res['status']}", "session_expired")
                self._override_logged_ui.add(oid)
            state = saved_bucket.get("ui") if (signed_in or (self.environment or {}).get("login_required")) else None
            return self.replay(case, state, entry=entry)
        use_state = self.saved.get("ui") if (signed_in or (self.environment or {}).get("login_required")) else None
        outcome = self.replay(case, use_state, entry=entry)
        if outcome["status"] == "Fail" and signed_in and not self.relogged and not self.cancel_requested() and not self.still_signed_in(login):
            self.relogged = True
            if self.ui_login(login, force=True)["status"] != "Pass":
                return blocked("登录已过期，重新登录没有成功", "session_expired")
            outcome = self.replay(case, self.saved.get("ui"), entry=entry)
            outcome["relogin"] = True
        return outcome

    # --- one case ----------------------------------------------------
    def run_one(self, case: dict) -> dict:
        self.count += 1
        auth_mode, account_meta, scope, auth_error = self._effective_auth(case)
        env_meta = self._env_meta()
        safe_account = {k: v for k, v in account_meta.items() if k != "password"}
        self.case_contexts[case["id"]] = (env_meta, safe_account)
        if auth_error:
            return blocked(auth_error, "missing_account")
        is_login_case = case["id"] in (self.auth.get("ui_case"), self.auth.get("api_case"))
        if (self.environment or {}).get("login_required") and auth_mode != "none" and not account_meta.get("account_id") and not is_login_case:
            return blocked("当前环境需要登录，但未配置可用账号", "missing_account")
        ir.set_rand(f"{self.rand}{self.count}")  # page cases each get their own value; API cases share the run's
        acc_id = account_meta.get("account_id") or ""
        ir_vars = {**self._account_runtime_vars(acc_id), **self.variables, **self.explicit_inputs} if acc_id else self.variables
        ir.set_variables(ir_vars)  # inputs, extracted values and the environment; never secrets
        return (self._run_api_case(case, auth_mode, account_meta, scope)
                if case["kind"] == "api"
                else self._run_ui_case(case, auth_mode, account_meta, scope))

    def still_signed_in(self, login: dict) -> bool:
        automation = login["extensions"]["automation"]
        def probe(_attempt):
            self.driver.reset(self.saved.get("ui"))
            self.driver.goto(self.absolute(automation["start_url"]))
            rows = [ui.check(self.driver, step, 3.0) for step in automation.get("assertions", [])]
            return {"status": "Pass" if rows and all(row["passed"] for row in rows) else "Fail", "assertions": rows, "reused": True}
        return self.attempt(login, "login_probe", probe)["status"] == "Pass"


def _register_screenshots(folder: Path, outcome: dict) -> list[dict]:
    """Register each readable screenshot once. Primary receipt and store failures still propagate."""
    artifacts, missing = [], set()
    for name in outcome.get("screenshots", []):
        try:
            file = folder / name
            if Path(name).name != name or file.is_symlink() or not file.is_file():
                raise FileNotFoundError(name)
            with file.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            artifacts.append({"path": name, "sha256": digest})
        except OSError as error:
            missing.add(name)
            outcome.setdefault("screenshot_warnings", []).append({"path": name, "operation": "read", "error": type(error).__name__})
    if missing:
        for result in [outcome, *outcome.get("setup_cases", [])]:
            result["screenshots"] = [name for name in result.get("screenshots", []) if name not in missing]
            for assertion in result.get("assertions", []):
                if assertion.get("screenshot") in missing:
                    del assertion["screenshot"]
    return artifacts


DETAIL_KEYS = ("target_started", "execution_only_pass", "steps", "assertions", "request", "response", "screenshots", "screenshot_warnings", "fallbacks", "reused", "relogin", "extracted",
               "attempts", "verification", "business_status", "business_verified", "performance_status", "performance_assertions",
               "execution_status", "execution_blocker", "last_attempt_status", "load_user", "load_iteration",
               "postcondition_source", "postcondition_results", "setup_cases", "environment", "account")


def _completed_replay(case: dict, outcome: dict) -> bool:
    """A completed adapter must account for every configured action and check."""
    if not outcome.get("target_started") or outcome.get("reused") or outcome["status"] != "Pass":
        return False
    assertions = outcome.get("assertions", [])
    if any(row.get("passed") is not True for row in assertions):
        return False
    postconditions = case.get("extensions", {}).get("postconditions", [])
    if postconditions and [row.get("case_id") for row in outcome.get("postcondition_results", [])] != postconditions:
        return False
    if case["kind"] == "api":
        rules = case.get("assertions", {})
        expected_checks = 1 + sum(len(rules.get(key, [])) for key in ("header_contains", "body_contains", "json_equals"))
        expected_checks += int("schema" in rules) + int("max_ms" in rules) + len(postconditions)
        return (bool(outcome.get("request")) and (outcome.get("response") or {}).get("status") == rules.get("status")
                and len(assertions) == expected_checks)
    automation = case.get("extensions", {}).get("automation", {})
    actions = automation.get("actions", [])
    expected_checks = len(automation.get("assertions", [])) + sum("assertion" in step for step in actions)
    steps = outcome.get("steps", [])
    business_steps = [step for step in steps if not step.get("entry")]
    return (len(assertions) == expected_checks and len(business_steps) == 1 + len(actions) + len(automation.get("setup_cases", []))
            and all(step.get("status") == "done" for step in steps))


def verification(case: dict, outcome: dict, *, probe: bool = False) -> dict:
    """Apply configured checks, or require a completed record of every configured action."""
    extensions = case.get("extensions", {})
    automation = extensions.get("automation", {})
    declared = (_api_assertion(case) if case["kind"] == "api" else
                bool(automation.get("assertions") or any("assertion" in step for step in automation.get("actions", []))))
    independent = extensions.get("oracle", {}).get("source") != "observation"
    trusted = declared and independent
    performance = [row for row in outcome.get("assertions", []) if row.get("scope") == "performance"]
    performance_status = ("Fail" if any(row.get("passed") is False for row in performance) else "Pass") if performance else "Unverified"
    rows = [row for row in outcome.get("assertions", []) if row.get("scope") != "performance"]
    checked = trusted and rows and not (probe or outcome.get("reused") or outcome.get("category") == "session_expired")
    business = "Unverified"
    if checked:
        if outcome.get("business_status") == "Fail" or (outcome.get("category") in ("", "assertion_failed", None) and any(row.get("passed") is False for row in rows)):
            business = "Fail"
        elif (outcome["status"] == "Pass" or (case["kind"] == "api" and performance_status == "Fail"
                                             and outcome.get("category") == "assertion_failed")) and all(row.get("passed") is True for row in rows):
            business = "Pass"
    complete = not probe and _completed_replay(case, outcome)
    if complete and not declared and independent:
        business = "Pass"
    if extensions.get("postconditions"):
        source = outcome.get("postcondition_source")
        results = outcome.get("postcondition_results")
        if source is not None and isinstance(results, list):
            business = _postcondition_business(case, source, results)
        elif business == "Pass":
            business = "Unverified"
    if (outcome.get("target_started") and outcome["status"] == "Pass" and not complete and not probe and not outcome.get("reused")
            and (not extensions.get("postconditions") or outcome.get("postcondition_results") is not None)):
        business = "Fail"
    interrupted = outcome.get("category") == "cancelled" or outcome.get("execution_blocker", {}).get("category") == "cancelled"
    execution = "cancelled" if interrupted or outcome["status"] == "Cancelled" else "blocked" if outcome.get("execution_blocker") or outcome["status"] == "Blocked" else "completed"
    if business == "Pass" and extensions.get("functional_design", {}).get("state") == "unmapped":
        business = "Unverified"
    return {**({"execution_only_pass": True} if complete and business == "Unverified" else {}),
            "verification": "verified" if business != "Unverified" else "unverified", "business_status": business,
            "business_verified": business == "Pass", "execution_status": execution,
            "performance_status": performance_status, "performance_assertions": performance}


def summarize_outcomes(case: dict, attempts: list[dict], outcome: dict) -> dict:
    """Project immutable attempt outcomes without discarding an earlier witnessed failure."""
    result = {**outcome, **verification(case, outcome), "attempts": [item["ref"] for item in attempts]}
    failed = next((item["outcome"] for item in attempts if item["outcome"]["business_status"] == "Fail"), None)
    if failed:
        result.update(last_attempt_status=outcome["status"], status="Fail", business_status="Fail", business_verified=False,
                      verification="verified", reason=failed.get("reason", "业务断言失败"), category="assertion_failed",
                      assertions=failed.get("assertions", []), actual=failed.get("actual", ""))
    if attempts:
        result["duration_ms"] = sum(item["outcome"].get("duration_ms", 0) for item in attempts)
        result["screenshots"] = [name for item in attempts for name in item["outcome"].get("screenshots", [])]
    return result


def _postcondition_detail(result: dict) -> dict:
    return json.loads(result["detail"]) if isinstance(result.get("detail"), str) else result.get("detail", {})


def _postcondition_business(case: dict, source: dict, results: list[dict]) -> str:
    own_case = {**case, "extensions": {key: value for key, value in case.get("extensions", {}).items() if key != "postconditions"}}
    own = verification(own_case, source)
    if own["business_status"] == "Fail" or any(_postcondition_detail(row).get("business_status") == "Fail" for row in results):
        return "Fail"
    expected = case.get("extensions", {}).get("postconditions", [])
    if ([row.get("case_id") for row in results] != expected or source.get("status") != "Pass" or source.get("reused")
            or source.get("category") == "session_expired"
            or case.get("extensions", {}).get("oracle", {}).get("source") == "observation"):
        return "Unverified"
    return "Pass" if all(_postcondition_detail(row).get("business_status") == "Pass" for row in results) else "Unverified"


def postcondition_outcome(case: dict, source: dict, results: list[dict]) -> dict:
    """Derive a fresh verdict from complete same-run result snapshots without changing HTTP attempts."""
    outcome = copy.deepcopy(source)
    outcome.pop("attempts", None)
    outcome["duration_ms"] = 0
    outcome["screenshots"] = []
    outcome["postcondition_source"] = copy.deepcopy(source)
    outcome["postcondition_results"] = copy.deepcopy(results)
    checks = [{"name": "后置验证 " + row["case_id"], "expected": "独立业务验证通过",
               "actual": _postcondition_detail(row).get("business_status", "Unverified"),
               "passed": True if task_state.case_outcome(row, _postcondition_detail(row), evidence_valid=True) == "pass" else
                         False if _postcondition_detail(row).get("business_status") == "Fail" else None,
               "scope": "business"} for row in results]
    outcome["assertions"] = [*outcome.get("assertions", []), *checks]
    if source["status"] == "Pass":
        failed = [row for row in results if row.get("status") == "Fail"]
        incomplete = [row for row in results if task_state.case_outcome(row, _postcondition_detail(row), evidence_valid=True) != "pass"]
        if failed:
            outcome.update(status="Fail", category="assertion_failed", reason="后置验证失败：" + "、".join(row["case_id"] for row in failed))
        elif incomplete:
            outcome.update(status="Blocked", category="postcondition_unverified", reason="后置业务尚未验证：" + "、".join(row["case_id"] for row in incomplete))
        else:
            outcome["actual"] = source.get("actual", "") + "；后置业务验证通过：" + "、".join(row["case_id"] for row in results)
    outcome.update(verification(case, outcome))
    return outcome


def record(run: dict, case: dict, outcome: dict, prerequisite: bool, started_at: str, *,
           case_context: tuple[dict, dict] | None = None) -> None:
    manifest = execution_inputs.load(run)
    if execution_inputs.content_hash(case) != manifest["case_hashes"][case["id"]]:
        raise ValueError("结果用例内容与实际冻结输入不一致")
    detail = {k: outcome[k] for k in DETAIL_KEYS if outcome.get(k) not in (None, [], "")}
    if case_context is not None:
        detail["environment"] = case_context[0]
        detail["account"] = {k: v for k, v in case_context[1].items() if k != "password"}
    env = manifest.get("environment") or {}
    detail.setdefault("environment", {
        "id": str(env.get("id") or ""),
        "name": str(env.get("name") or ""),
        "base_url": str((manifest.get("config") or {}).get("base_url") or ""),
    })
    if "account" not in detail:
        acc = manifest.get("account")
        detail["account"] = (
            {k: v for k, v in acc.items() if k != "password"}
            if isinstance(acc, dict)
            else platform_accounts.summary_for_result(None, mode="inherit")
        )
    elif isinstance(detail["account"], dict):
        detail["account"] = {k: v for k, v in detail["account"].items() if k != "password"}
    detail.setdefault("verification", verification(case, outcome)["verification"])
    for key, value in verification(case, outcome).items():
        detail.setdefault(key, value)
    detail["case_scope_hash"] = manifest["scope_hashes"][case["id"]]
    db.insert("results", {
        "run_id": run["id"], "project_id": run["project_id"], "case_id": case["id"], "case_title": case["title"],
        "kind": case["kind"], "engine": "http" if case["kind"] == "api" else run["engine"],
        "status": outcome["status"], "category": outcome.get("category", ""), "reason": outcome.get("reason", ""),
        "actual": outcome.get("actual", ""), "duration_ms": int(outcome.get("duration_ms", 0)),
        "started_at": started_at, "prerequisite": int(prerequisite), "case_revision": case["revision"],
        "case_content_hash": manifest["case_hashes"][case["id"]], "input_hash": run["input_hash"], "detail": detail,
    })


def parallel_limit() -> int:
    """The default resource budget can be increased for independent concurrent tasks."""
    try:
        return max(1, int(os.environ.get("AUTOSKILL_MAX_PARALLEL", MAX_PARALLEL)))
    except ValueError:
        return MAX_PARALLEL


def reap_children() -> None:
    """Poll every owned process, including successful runs, so exited children never accumulate."""
    with _children_lock:
        for pid, child in list(_children.items()):
            if child.poll() is not None:
                del _children[pid]


def process_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        from .win32 import kernel

        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # Access denied does not prove a process exited.
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if sys.platform.startswith("linux"):
        try:
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except (OSError, IndexError):
            return False  # The process may have exited between kill(0) and the stat read.
        return state not in ("Z", "X")
    return True


def _recover_interrupted_targets(conn, run: dict) -> None:
    """Materialize only dispatched targets with no durable result after a worker stopped."""
    if run.get("family") != "replay" or not run.get("input_manifest"):
        return
    try:
        manifest = execution_inputs.load(run)
    except (OSError, ValueError):
        return
    missing = set(manifest["selected"]) - {row[0] for row in conn.execute("SELECT case_id FROM results WHERE run_id=?", (run["id"],))}
    for file in sorted((evidence_dir(run["id"]) / "attempts").glob("*.json")):
        if not missing:
            break
        try:
            receipt = json.loads(file.read_bytes())
            if not isinstance(receipt, dict) or not isinstance(identity := receipt.get("case_id"), str) or identity not in missing:
                continue
            if (receipt.get("target_started") is not True or receipt.get("run_id") != run["id"] or file.stem != receipt.get("id")
                    or receipt.get("input_hash") != run["input_hash"] or receipt.get("case_content_hash") != manifest["case_hashes"][identity]
                    or receipt.get("case_scope_hash") != manifest["scope_hashes"][identity] or not isinstance(receipt.get("started_at"), str)):
                continue
        except (OSError, ValueError, KeyError):
            continue
        case = manifest["cases"][identity]
        detail = {"target_started": True, "case_scope_hash": receipt["case_scope_hash"], "interrupted_attempt": receipt["id"]}
        conn.execute("INSERT INTO results (run_id,project_id,case_id,case_title,kind,engine,status,category,reason,started_at,"
                     "case_revision,case_content_hash,input_hash,detail) VALUES (?,?,?,?,?,?,'Fail','environment',?,?,?,?,?,?)",
                     (run["id"], run["project_id"], identity, case["title"], case["kind"], "http" if case["kind"] == "api" else run["engine"],
                      "执行中断，缺少完整结果记录", receipt["started_at"], case["revision"], receipt["case_content_hash"], run["input_hash"],
                      json.dumps(detail)))
        conn.execute("UPDATE runs SET done=(SELECT count(*) FROM results WHERE run_id=? AND prerequisite=0), "
                     "failed=(SELECT count(*) FROM results WHERE run_id=? AND prerequisite=0 AND status='Fail') WHERE id=?",
                     (run["id"], run["id"], run["id"]))
        missing.remove(identity)


def recover_runs() -> None:
    """Recover dead executors without overwriting a child that just accepted its reservation or finished."""
    reap_children()
    for run in db.rows("SELECT * FROM runs WHERE status = 'Running'"):
        if process_alive(run["pid"]) and runtime_identity.owns(run["pid"], run["owner_identity"]):
            continue
        with db.connect() as conn:
            recovered = conn.execute(
                "UPDATE runs SET status = CASE WHEN cancel = 1 THEN 'Cancelled' ELSE 'Blocked' END, "
                "current = '', finished_at = ?, claim_token = '', "
                "reason = CASE WHEN cancel = 1 THEN '' ELSE ? END "
                "WHERE id = ? AND status = 'Running' AND pid IS ? AND claim_token = ? AND owner_identity = ?",
                (db.now(), "运行进程已结束或不属于当前数据位置，未自动重放未确认的操作", run["id"], run["pid"], run["claim_token"], run["owner_identity"])).rowcount
            if recovered:
                _recover_interrupted_targets(conn, run)
        if recovered:
            current = db.one("SELECT * FROM runs WHERE id = ?", (run["id"],))
            if current and not current["delete_requested"]:
                sync_plan(current)
                from . import task_reports

                task_reports.for_run(run["id"])
                archive_failures(run["id"])
    from . import candidate_publication, task_reports

    for completed in candidate_publication.pending_completions():
        sync_plan(completed)
        task_reports.for_run(completed["id"])
        archive_failures(completed["id"])


def _acquire(run_id: str, token: str | None) -> tuple[dict, bool]:
    """Try ownership once while guarded; joining must wait after releasing the execution lock."""
    claimed = (db.activate_run(run_id, os.getpid(), token) if token is not None
               else db.claim_run(run_id, os.getpid(), parallel_limit()))
    run = db.one("SELECT * FROM runs WHERE id = ?", (run_id,))
    if not run:
        raise ValueError(f"运行 {run_id} 不存在")
    return run, claimed


def execute(run_id: str, force_login: bool = False, *, claim_token: str | None = None) -> dict:
    """Execute a run once, or wait for the process that already owns it to return its final row."""
    from . import deletion, task_reports

    db.init()
    while True:
        with deletion.execution_guard(run_id):
            result = _execute_guarded(run_id, force_login, claim_token)
            if result is not None:
                result = {**result, "test_report": task_reports.for_run(run_id)}
                break
        # A dispatcher may already have reserved this row for a child waiting on
        # the same guard. Keep joining/recovery outside that child's execution lock.
        recover_runs()
        time.sleep(0.1)
    archive_failures(run_id)
    deletion._cleanup_result(run_id)
    if not db.one("SELECT id FROM runs WHERE id = ?", (run_id,)):
        result["test_report"] = {"status": "skipped", "reason": "运行已删除，报告已随记录清理", "retryable": False}
    return public_run(result)


def _execute_guarded(run_id: str, force_login: bool, claim_token: str | None) -> dict | None:
    if force_login:
        db.execute("UPDATE runs SET force_login = 1 WHERE id = ? AND status = 'Queued'", (run_id,))
    run, claimed = _acquire(run_id, claim_token)
    if not claimed:
        return run if claim_token is not None or run["status"] in db.FINISHED else None
    with task_context.use_session(run["session_id"] or None):
        try:
            if run.get("family") == "load":
                from . import load_testing

                return load_testing.execute_claimed(run)
            if run.get("family") == "native":
                from . import native_run

                return native_run.execute_claimed(run)
            return _execute_claimed(run)
        finally:
            sync_plan(run)


def archive_failures(run_id: str) -> None:
    """Copy failed and blocked results to the configured error folder; never fail the run for it."""
    from . import failure_archive

    with contextlib.suppress(Exception):
        failure_archive.for_run(run_id)


def sync_plan(run: dict) -> None:
    """All executors write back to the sheet selected when the run was prepared."""
    if run.get("family") == "load":
        return
    from . import candidate_publication, plan

    publication = candidate_publication.complete_run(run)
    if publication is not None:
        run["publication"] = publication

    folders = {plan_folder(run["plan_path"]) if run.get("plan_path") else paths.case_skill()}
    if run.get("family") == "replay":
        with contextlib.suppress(OSError, ValueError):
            folders.update(folder for _identity, _case, folder in candidate_publication.design_bindings(run))
    for folder in folders:
        if folder.is_dir() and any(folder.glob("*.xlsx")):
            with contextlib.suppress(OSError, ValueError):
                plan.sync(quiet=True, folder=folder, run_id=run["id"])


def _run_cases(run: dict, chosen: list[str], session: Session, counts: dict) -> str:
    outcomes: dict[str, dict] = {}
    pending = {}
    interrupted = False
    plan = build_plan(chosen, session.cases)
    for case_id, prerequisite in plan:
        if db.one("SELECT cancel FROM runs WHERE id = ?", (run["id"],))["cancel"]:
            interrupted = True
            break
        case = session.cases[case_id]
        label = ("前置：" if prerequisite else "后置验证：" if case_id not in chosen else "") + case["title"]
        db.update("runs", run["id"], {"current": label})
        started_at = db.now()
        broken = next((dep for dep in projects.depends_on(case) if outcomes.get(dep, {}).get("status") != "Pass"), None)
        outcome = (blocked(f"前置用例 {broken}「{session.cases[broken]['title']}」未通过，本用例没有执行", "prerequisite_failed")
                   if broken else session.run_one(case))
        outcome = session.summarize(case, outcome)
        outcomes[case_id] = outcome
        if case.get("extensions", {}).get("postconditions"):
            pending[case_id] = (prerequisite, started_at)
        else:
            record(run, case, outcome, prerequisite, started_at, case_context=session.case_contexts.get(case_id))
        if case_id in chosen:
            counts["done"] += 1
            counts[{"Pass": "passed", "Fail": "failed"}.get(outcome["status"], "blocked")] += 1
            db.update("runs", run["id"], counts)
    for case_id, _ in reversed(plan):
        if case_id not in pending:
            continue
        case = session.cases[case_id]
        source = outcomes[case_id]
        rows = [db.one("SELECT * FROM results WHERE run_id = ? AND case_id = ? ORDER BY id DESC LIMIT 1", (run["id"], target))
                for target in case["extensions"]["postconditions"]]
        if all(row is not None for row in rows):
            derived = session.attempt(case, "postcondition", lambda _attempt, c=case, s=source, r=rows: postcondition_outcome(c, s, r))
            outcomes[case_id] = session.summarize(case, derived)
        prerequisite, started_at = pending[case_id]
        record(run, case, outcomes[case_id], prerequisite, started_at, case_context=session.case_contexts.get(case_id))
    counts.update(done=sum(identity in outcomes for identity in chosen), passed=0, failed=0, blocked=0)
    for identity in chosen:
        if identity in outcomes:
            counts[{"Pass": "passed", "Fail": "failed"}.get(outcomes[identity]["status"], "blocked")] += 1
    db.update("runs", run["id"], counts)
    if interrupted:
        return "Cancelled"
    return "Fail" if counts["failed"] else "Blocked" if counts["blocked"] else "Pass"


def _execute_claimed(run: dict) -> dict:
    """Only a process that claimed the row may enter the case loop."""
    run_id = run["id"]
    chosen = json.loads(run["case_ids"]) if isinstance(run["case_ids"], str) else run["case_ids"]
    os.environ["AUTOSKILL_HEADED"] = "1" if run.get("headed") else "0"  # the choice made when the run was queued
    counts = {"done": 0, "passed": 0, "failed": 0, "blocked": 0}
    final, reason, session = "Pass", "", None
    try:
        if run["cancel"]:
            db.finish_run(run_id, os.getpid(), counts, "Cancelled", "")
            return db.one("SELECT * FROM runs WHERE id = ?", (run_id,))
        if not run.get("input_hash"):
            frozen, _manifest = execution_inputs.freeze(run["project_id"], chosen, run["engine"])
            db.update("runs", run_id, {**frozen, "input_manifest": data_refs.store(frozen["input_manifest"])})
            run.update(frozen)
        session = Session(run, bool(run["force_login"]))
        with secrets.use_values(session.manifest["credentials"]):
            final = _run_cases(run, chosen, session, counts)
    except ValueError as error:
        final, reason = "Blocked", str(error)
    except Exception as error:  # noqa: BLE001  # Unexpected runner failures must finalize the durable run instead of leaving it Running.
        final, reason = "Blocked", "运行器出错：" + type(error).__name__
    finally:
        if session:
            with contextlib.suppress(Exception):  # Cleanup must not prevent the durable run from reaching its terminal state.
                session.close()
    if reason:
        with db.connect() as conn:
            _recover_interrupted_targets(conn, run)
            counts.update(dict(conn.execute("SELECT done, failed FROM runs WHERE id=?", (run_id,)).fetchone()))
    db.finish_run(run_id, os.getpid(), counts, final, reason)
    return db.one("SELECT * FROM runs WHERE id = ?", (run_id,))


def spawn(run_id: str, force_login: bool = False) -> int | None:
    """Reserve a queued run for one child, retaining its handle until it is reaped."""
    token = uuid.uuid4().hex
    if force_login:
        db.execute("UPDATE runs SET force_login = 1 WHERE id = ? AND status = 'Queued'", (run_id,))
    if not db.claim_run(run_id, os.getpid(), parallel_limit(), token):
        return None
    command = [sys.executable, "-m", "autoskill.runner", run_id, "--claim-token", token]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        child = subprocess.Popen(command, cwd=str(paths.APP), env=paths.child_env(), creationflags=flags,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as error:
        with db.connect() as conn:
            conn.execute(
                "UPDATE runs SET status = CASE WHEN cancel = 1 THEN 'Cancelled' ELSE 'Blocked' END, "
                "reason = CASE WHEN cancel = 1 THEN '' ELSE ? END, finished_at = ?, claim_token = '', pid = NULL "
                "WHERE id = ? AND status = 'Running' AND claim_token = ?",
                ("无法启动运行进程：" + type(error).__name__, db.now(), run_id, token))
        current = db.one("SELECT * FROM runs WHERE id = ?", (run_id,))
        if current and not current["delete_requested"]:
            sync_plan(current)
            from . import task_reports

            task_reports.for_run(run_id)
        return None
    with _children_lock:
        _children[child.pid] = child
    with db.connect() as conn:
        conn.execute("UPDATE runs SET pid = ?, owner_identity = ? WHERE id = ? AND status = 'Running' AND claim_token = ?",
                     (child.pid, json.dumps(runtime_identity.current(child.pid)), run_id, token))
    return child.pid


if __name__ == "__main__":
    token_arg = sys.argv.index("--claim-token") + 1 if "--claim-token" in sys.argv else None
    execute(sys.argv[1], force_login="--force-login" in sys.argv[2:], claim_token=sys.argv[token_arg] if token_arg else None)
