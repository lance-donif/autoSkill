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

from . import db, execution_inputs, paths, projects, secrets, sessions, task_context
from .replay import api, ir, ui

ENGINES = ("playwright", "selenium")
MAX_PARALLEL = 2
_children: dict[int, subprocess.Popen] = {}
_children_lock = threading.Lock()


def prepare(project_id: str, case_ids: list[str] | None = None, engine: str = "playwright",
            trigger: str = "manual", task_id: str | None = None, batch_id: str | None = None,
            headed: bool | None = None, force_login: bool = False, session_id: str | None = None,
            *, candidate_cases: list[dict] | None = None, plan_path: str | Path | None = None) -> dict:
    """Validate and freeze execution inputs without writing any database row."""
    if engine not in ENGINES:
        raise ValueError("回放引擎只能是 playwright 或 selenium")
    frozen, manifest = execution_inputs.freeze(project_id, case_ids, engine, candidate_cases=candidate_cases)
    chosen = manifest["selected"]
    identity = (task_context.session_id() if session_id is None else session_id) or ""
    with task_context.use_session(identity or None):
        selected_plan = str(Path(plan_path).resolve() if plan_path else paths.case_skill())
    return {"id": db.new_id("run"), "batch_id": batch_id, "project_id": project_id, "engine": engine,
           "trigger": trigger, "task_id": task_id, "status": "Queued", "case_ids": chosen,
           "total": len(chosen), "created_at": db.now(), "headed": int(paths.headed() if headed is None else bool(headed)),
           "force_login": int(force_login), "session_id": identity, "family": "replay", "plan_path": selected_plan, **frozen}


def enqueue(prepared: dict, connection=None) -> dict:
    """Insert prepared execution intent, optionally inside the caller's scheduling transaction."""
    if connection is None:
        with db.connect() as conn:
            return enqueue(prepared, connection=conn)
    keys = ", ".join(prepared)
    marks = ", ".join("?" for _ in prepared)
    values = [json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value for value in prepared.values()]
    connection.execute(f"INSERT INTO runs ({keys}) VALUES ({marks})", values)
    return prepared


def create(project_id: str, case_ids: list[str] | None = None, engine: str = "playwright",
           trigger: str = "manual", task_id: str | None = None, batch_id: str | None = None,
           headed: bool | None = None, force_login: bool = False, session_id: str | None = None,
           *, candidate_cases: list[dict] | None = None, plan_path: str | Path | None = None) -> dict:
    """Validate, freeze and queue a run through the same API used by transactional schedules."""
    projects.reindex(project_id)
    return enqueue(prepare(project_id, case_ids, engine, trigger, task_id, batch_id, headed, force_login, session_id,
                           candidate_cases=candidate_cases, plan_path=plan_path))


def evidence_dir(run_id: str) -> Path:
    return paths.local() / "runs" / run_id


def make_driver(engine: str, config: dict):
    if engine == "selenium":
        from .replay.selenium_driver import SeleniumDriver

        return SeleniumDriver(config, headless=not paths.headed())
    from .replay.playwright_driver import PlaywrightDriver

    return PlaywrightDriver(config, headless=not paths.headed())


def blocked(reason: str, category: str) -> dict:
    return {"status": "Blocked", "reason": reason, "category": category, "actual": "", "assertions": [],
            "duration_ms": 0}


def build_plan(chosen: list[str], cases: dict) -> list[tuple[str, bool]]:
    """Chosen cases with their prerequisites first: [(case_id, is_prerequisite)]. Each case appears once."""
    order: list[str] = []
    visiting: set[str] = set()

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

    for case_id in chosen:
        visit(case_id, ())
    return [(case_id, case_id not in chosen) for case_id in order]


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
        self.force_login = force_login
        self.saved = self.manifest["saved_session"]
        self.rand = self.manifest["variables"]["RAND"]
        self.count = 0
        self.variables: dict = {"RAND": self.rand}
        self.driver = None
        self.driver_error = ""
        self.client = api.new_client(self.config, self.saved.get("ui"))
        self.relogged = False
        self.attempts: dict[str, list[dict]] = {}

    def close(self) -> None:
        self.client.close()
        if self.driver:
            if paths.headed():
                import time

                time.sleep(1.5)  # leave the last page on screen for a moment
            try:
                self.driver.close()
            except Exception:  # noqa: BLE001  # Driver cleanup must not replace the case result with a cleanup failure.
                return

    def browser(self):
        if self.driver is None and not self.driver_error:
            try:
                self.driver = make_driver(self.run["engine"], self.config)
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

    def attempt(self, case: dict, kind: str, invoke) -> dict:
        """Journal before invoking an adapter; each response and screenshot has its own durable identity."""
        attempt_id = "attempt-" + uuid.uuid4().hex
        file = evidence_dir(self.run["id"]) / "attempts" / (attempt_id + ".json")
        receipt = {"schema_version": 1, "id": attempt_id, "run_id": self.run["id"], "case_id": case["id"],
                   "input_hash": self.run["input_hash"], "case_content_hash": self.manifest["case_hashes"][case["id"]],
                   "case_scope_hash": self.manifest["scope_hashes"][case["id"]], "kind": kind,
                   "started_at": db.now(), "state": "started", "outcome": None}
        execution_inputs.write_json(file, receipt)
        try:
            outcome = invoke(attempt_id)
        except Exception as error:  # noqa: BLE001  # Preserve an attempt receipt even when an adapter unexpectedly exits its structured path.
            outcome = blocked("执行器出错：" + type(error).__name__, "environment")
        outcome = {**outcome, **verification(case, outcome, probe=kind in ("login_probe", "session_reuse"))}
        artifacts = []
        for name in outcome.get("screenshots", []):
            artifact = evidence_dir(self.run["id"]) / name
            if artifact.is_file():
                artifacts.append({"path": name, "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()})
        receipt.update(state="completed", finished_at=db.now(), outcome=outcome, artifacts=artifacts)
        execution_inputs.write_json(file, receipt)
        reference = {"id": attempt_id, "path": str(file), "sha256": execution_inputs.content_hash(receipt),
                     "case_id": case["id"], "kind": kind, "status": outcome["status"], "business_status": outcome["business_status"]}
        self.attempts.setdefault(case["id"], []).append({"ref": reference, "outcome": copy.deepcopy(outcome)})
        return outcome

    def api_replay(self, client, case: dict, variables: dict, *, kind: str = "case") -> dict:
        return self.attempt(case, kind, lambda _attempt: api.run_case(client, case, self.config, variables))

    def summarize(self, case: dict, outcome: dict) -> dict:
        """Keep a witnessed business failure even if a later retry or environment result differs."""
        if not self.attempts.get(case["id"]):
            self.attempt(case, "not_executed", lambda _attempt: outcome)
        attempts = self.attempts.get(case["id"], [])
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

    def replay(self, case: dict, state: dict | None = None) -> dict:
        """Replay one UI case in a clean context. A misread captcha is not a defect: try again with a fresh image."""
        actions = case["extensions"]["automation"].get("actions", [])
        for index in range(3 if any(step.get("action") == "captcha" for step in actions) else 1):
            self.driver.reset(state)
            outcome = self.attempt(case, "captcha_retry" if index else "case", lambda attempt_id:
                                   ui.run_case(self.driver, case, self.config, evidence_dir(self.run["id"]), self.shot(attempt_id)))
            if outcome["status"] != "Fail":
                break
        return outcome

    # --- login -------------------------------------------------------
    def ui_login(self, case: dict, force: bool) -> dict:
        driver = self.browser()
        if not driver:
            return blocked(self.driver_error, "environment")
        state = self.saved.get("ui")
        probes = case["extensions"]["automation"].get("assertions", [])
        if state and probes and not force:
            driver.reset(state)
            def probe(_attempt):
                driver.goto(ir.absolute_url(case["extensions"]["automation"]["start_url"], self.config["base_url"]))
                rows = [ui.check(driver, step, 3.0) for step in probes]
                return {"status": "Pass" if all(row["passed"] for row in rows) else "Fail", "actual": "检查已保存的登录态",
                        "reason": "", "category": "", "assertions": rows, "duration_ms": 0, "reused": True}
            trial = self.attempt(case, "login_probe", probe)
            if trial["status"] == "Pass":
                return trial
        outcome = self.replay(case)
        if outcome["status"] == "Pass":
            self.saved["ui"] = driver.storage_state()
            sessions.save(self.pid, ui=self.saved["ui"])
            self.client.close()
            self.client = api.new_client(self.config, self.saved["ui"])
        return outcome

    def api_login(self, case: dict, force: bool) -> dict:
        wanted = list(case.get("extensions", {}).get("extract", {}))
        kept = self.saved.get("variables", {})
        if not force and wanted and all(name in kept for name in wanted):
            probe = self.cases.get(self.auth.get("api_check_case", ""))
            trial = dict(kept)
            if not probe or self.api_replay(self.client, probe, trial, kind="login_probe")["status"] == "Pass":
                self.variables.update({name: kept[name] for name in wanted})
                return self.attempt(case, "session_reuse", lambda _attempt:
                                    {"status": "Pass", "actual": "复用已保存的登录信息", "reason": "", "category": "",
                                     "assertions": [], "duration_ms": 0, "reused": True})
        outcome = self.api_replay(self.client, case, self.variables)
        if outcome["status"] == "Pass" and wanted:
            self.saved["variables"] = {name: self.variables[name] for name in wanted}
            sessions.save(self.pid, variables=self.saved["variables"])
        return outcome

    def state_variables(self) -> None:
        """API variables that live in the browser login (for example a token kept in localStorage)."""
        stored = {item["name"]: item["value"] for origin in self.saved.get("ui", {}).get("origins", [])
                  for item in origin.get("localStorage", [])}
        for name, source in self.auth.get("state_variables", {}).items():
            if source.get("name") in stored:
                self.variables[name] = stored[source["name"]]

    # --- one case ----------------------------------------------------
    def run_one(self, case: dict) -> dict:
        case_id = case["id"]
        self.count += 1
        ir.set_rand(f"{self.rand}{self.count}")  # page cases each get their own value; API cases share the run's
        if case["kind"] == "api":
            self.state_variables()
            if case_id == self.auth.get("api_case"):
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
            outcome = self.api_replay(self.client, case, self.variables)
            login = self.cases.get(self.auth.get("api_case", ""))
            if outcome["category"] == "session_expired" and login and needs(case_id, login["id"], self.cases) and not self.relogged:
                self.relogged = True
                if self.api_login(login, force=True)["status"] != "Pass":
                    return blocked("登录已过期，重新登录没有成功", "session_expired")
                outcome = self.api_replay(self.client, case, self.variables, kind="relogin_retry")
                outcome["relogin"] = True
            return outcome
        if case_id == self.auth.get("ui_case"):
            return self.ui_login(case, self.force_login)
        driver = self.browser()
        if not driver:
            return blocked(self.driver_error, "environment")
        login = self.cases.get(self.auth.get("ui_case", ""))
        signed_in = bool(login) and needs(case_id, login["id"], self.cases)
        outcome = self.replay(case, self.saved.get("ui") if signed_in else None)
        if outcome["status"] == "Fail" and signed_in and not self.relogged and not self.still_signed_in(login):
            self.relogged = True
            if self.ui_login(login, force=True)["status"] != "Pass":
                return blocked("登录已过期，重新登录没有成功", "session_expired")
            outcome = self.replay(case, self.saved.get("ui"))
            outcome["relogin"] = True
        return outcome

    def still_signed_in(self, login: dict) -> bool:
        automation = login["extensions"]["automation"]
        def probe(_attempt):
            self.driver.reset(self.saved.get("ui"))
            self.driver.goto(ir.absolute_url(automation["start_url"], self.config["base_url"]))
            rows = [ui.check(self.driver, step, 3.0) for step in automation.get("assertions", [])]
            return {"status": "Pass" if rows and all(row["passed"] for row in rows) else "Fail", "assertions": rows, "reused": True}
        return self.attempt(login, "login_probe", probe)["status"] == "Pass"


DETAIL_KEYS = ("steps", "assertions", "request", "response", "screenshots", "fallbacks", "reused", "relogin", "extracted",
               "attempts", "verification", "business_status", "business_verified", "execution_status", "execution_blocker", "last_attempt_status")


def verification(case: dict, outcome: dict, *, probe: bool = False) -> dict:
    """Business evaluation requires declared checks, never an action-only or saved-session success."""
    extensions = case.get("extensions", {})
    automation = extensions.get("automation", {})
    declared = (bool(case.get("assertions")) if case["kind"] == "api" else
                bool(automation.get("assertions") or any("assertion" in step for step in automation.get("actions", []))))
    trusted = declared and extensions.get("oracle", {}).get("source") != "observation"
    rows = outcome.get("assertions", [])
    checked = trusted and rows and not (probe or outcome.get("reused") or outcome.get("category") == "session_expired")
    business = "Unverified"
    if checked:
        if outcome.get("business_status") == "Fail" or (outcome.get("category") in ("", "assertion_failed", None) and any(row.get("passed") is False for row in rows)):
            business = "Fail"
        elif outcome["status"] == "Pass" and all(row.get("passed") is True for row in rows):
            business = "Pass"
    execution = "blocked" if outcome.get("execution_blocker") or outcome["status"] == "Blocked" else "completed"
    return {"verification": "verified" if business != "Unverified" else "unverified", "business_status": business,
            "business_verified": business == "Pass", "execution_status": execution}


def record(run: dict, case: dict, outcome: dict, prerequisite: bool, started_at: str) -> None:
    manifest = execution_inputs.load(run)
    if execution_inputs.content_hash(case) != manifest["case_hashes"][case["id"]]:
        raise ValueError("结果用例内容与实际冻结输入不一致")
    detail = {k: outcome[k] for k in DETAIL_KEYS if outcome.get(k) not in (None, [], "")}
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

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
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


def recover_runs() -> None:
    """Recover dead executors without overwriting a child that just accepted its reservation or finished."""
    reap_children()
    for run in db.rows("SELECT id, pid, claim_token FROM runs WHERE status = 'Running'"):
        if process_alive(run["pid"]):
            continue
        with db.connect() as conn:
            recovered = conn.execute(
                "UPDATE runs SET status = CASE WHEN cancel = 1 THEN 'Cancelled' ELSE 'Blocked' END, "
                "current = '', finished_at = ?, claim_token = '', "
                "reason = CASE WHEN cancel = 1 THEN '' ELSE ? END "
                "WHERE id = ? AND status = 'Running' AND pid IS ? AND claim_token = ?",
                (db.now(), "运行进程意外结束（可能是电脑休眠或进程被关闭），请重新运行", run["id"], run["pid"], run["claim_token"])).rowcount
        if recovered:
            sync_plan(db.one("SELECT * FROM runs WHERE id = ?", (run["id"],)))


def _acquire(run_id: str, token: str | None) -> tuple[dict, bool]:
    """Join an existing run or become its sole executor; only the designated child can use a reservation."""
    while True:
        claimed = (db.activate_run(run_id, os.getpid(), token) if token is not None
                   else db.claim_run(run_id, os.getpid(), parallel_limit()))
        run = db.one("SELECT * FROM runs WHERE id = ?", (run_id,))
        if not run:
            raise ValueError(f"运行 {run_id} 不存在")
        if claimed or token is not None or run["status"] in db.FINISHED:
            return run, claimed
        recover_runs()
        time.sleep(0.1)


def execute(run_id: str, force_login: bool = False, *, claim_token: str | None = None) -> dict:
    """Execute a run once, or wait for the process that already owns it to return its final row."""
    db.init()
    if force_login:
        db.execute("UPDATE runs SET force_login = 1 WHERE id = ? AND status = 'Queued'", (run_id,))
    run, claimed = _acquire(run_id, claim_token)
    if not claimed:
        return run
    with task_context.use_session(run["session_id"] or None):
        try:
            if run.get("family") == "native":
                from . import native_run

                return native_run.execute_claimed(run)
            return _execute_claimed(run)
        finally:
            sync_plan(run)


def sync_plan(run: dict) -> None:
    """All executors write back to the sheet selected when the run was prepared."""
    from . import plan

    folder = Path(run["plan_path"]) if run.get("plan_path") else paths.case_skill()
    if folder.is_dir() and any(folder.glob("*.xlsx")):
        with contextlib.suppress(OSError, ValueError):
            plan.sync(quiet=True, folder=folder)


def _run_cases(run: dict, chosen: list[str], session: Session, counts: dict) -> str:
    outcomes: dict[str, str] = {}
    for case_id, prerequisite in build_plan(chosen, session.cases):
        if db.one("SELECT cancel FROM runs WHERE id = ?", (run["id"],))["cancel"]:
            return "Cancelled"
        case = session.cases[case_id]
        label = ("前置：" if prerequisite else "") + case["title"]
        db.update("runs", run["id"], {"current": label})
        started_at = db.now()
        broken = next((dep for dep in projects.depends_on(case) if outcomes.get(dep) != "Pass"), None)
        outcome = (blocked(f"前置用例 {broken}「{session.cases[broken]['title']}」未通过，本用例没有执行", "prerequisite_failed")
                   if broken else session.run_one(case))
        outcome = session.summarize(case, outcome)
        outcomes[case_id] = outcome["status"]
        record(run, case, outcome, prerequisite, started_at)
        if not prerequisite:
            counts["done"] += 1
            counts[{"Pass": "passed", "Fail": "failed"}.get(outcome["status"], "blocked")] += 1
            db.update("runs", run["id"], counts)
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
            db.update("runs", run_id, frozen)
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
        sync_plan(db.one("SELECT * FROM runs WHERE id = ?", (run_id,)))
        return None
    with _children_lock:
        _children[child.pid] = child
    with db.connect() as conn:
        conn.execute("UPDATE runs SET pid = ? WHERE id = ? AND status = 'Running' AND claim_token = ?", (child.pid, run_id, token))
    return child.pid


if __name__ == "__main__":
    token_arg = sys.argv.index("--claim-token") + 1 if "--claim-token" in sys.argv else None
    execute(sys.argv[1], force_login="--force-login" in sys.argv[2:], claim_token=sys.argv[token_arg] if token_arg else None)
