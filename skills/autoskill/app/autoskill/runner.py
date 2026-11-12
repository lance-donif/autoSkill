"""Run cases: resolve prerequisites, reuse or renew the login, record every result.

A run is a row in the local database. `python -m autoskill.runner <run_id>`
executes it in its own process so a browser crash cannot take the dashboard down.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from . import db, paths, projects, sessions, task_context
from .replay import api, ir, ui

ENGINES = ("playwright", "selenium")
MAX_PARALLEL = 2
_children: dict[int, subprocess.Popen] = {}
_children_lock = threading.Lock()


def create(project_id: str, case_ids: list[str] | None = None, engine: str = "playwright",
           trigger: str = "manual", task_id: str | None = None, batch_id: str | None = None,
           headed: bool | None = None, force_login: bool = False, session_id: str | None = None) -> dict:
    """Validate the request and queue a run."""
    if engine not in ENGINES:
        raise ValueError("回放引擎只能是 playwright 或 selenium")
    projects.reindex(project_id)
    active = {c["id"]: c for c in projects.cases(project_id)}
    if case_ids:
        unknown = [c for c in case_ids if c not in active]
        if unknown:
            raise ValueError("这些用例不存在或已归档：" + "、".join(unknown))
        chosen = list(dict.fromkeys(case_ids))
    else:
        chosen = [cid for cid, case in active.items() if ir.is_replayable(case)]
    not_ready = [cid for cid in chosen if not ir.is_replayable(active[cid])]
    if not_ready:
        raise ValueError("这些用例还没有可回放的步骤：" + "、".join(not_ready))
    if not chosen:
        raise ValueError("项目里还没有可回放的用例；先录制一条")
    run = {"id": db.new_id("run"), "batch_id": batch_id, "project_id": project_id, "engine": engine,
           "trigger": trigger, "task_id": task_id, "status": "Queued", "case_ids": chosen,
           "total": len(chosen), "created_at": db.now(), "headed": int(paths.headed() if headed is None else bool(headed)),
           "force_login": int(force_login), "session_id": (task_context.session_id() if session_id is None else session_id) or ""}
    db.insert("runs", run)
    return run


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
        self.config = projects.config(self.pid)
        self.cases = {c["id"]: c for c in projects.cases(self.pid)}
        self.auth = projects.auth(self.pid)
        self.force_login = force_login
        self.saved = sessions.load(self.pid)
        self.rand = uuid.uuid4().hex[:6]  # {{RAND}}: new on every run, so data a case creates never collides with earlier runs
        self.count = 0
        self.variables: dict = {"RAND": self.rand}
        self.driver = None
        self.driver_error = ""
        self.client = api.new_client(self.config, self.saved.get("ui"))
        self.relogged = False

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

    def replay(self, case: dict, state: dict | None = None) -> dict:
        """Replay one UI case in a clean context. A misread captcha is not a defect: try again with a fresh image."""
        actions = case["extensions"]["automation"].get("actions", [])
        for _ in range(3 if any(step.get("action") == "captcha" for step in actions) else 1):
            self.driver.reset(state)
            outcome = ui.run_case(self.driver, case, self.config, evidence_dir(self.run["id"]), self.shot(case["id"]))
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
            try:
                driver.goto(ir.absolute_url(case["extensions"]["automation"]["start_url"], self.config["base_url"]))
                if all(ui.check(driver, step, 3.0)["passed"] for step in probes):
                    return {"status": "Pass", "actual": "已保存的登录态仍然有效，直接复用", "reason": "", "category": "",
                            "assertions": [{"name": "登录态有效", "passed": True, "expected": True, "actual": True}],
                            "steps": [], "screenshots": [], "duration_ms": 0, "reused": True}
            except Exception:  # noqa: BLE001, S110  # An unusable saved login must fall back to replaying the login case.
                pass
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
            if not probe or api.run_case(self.client, probe, self.config, trial)["status"] == "Pass":
                self.variables.update({name: kept[name] for name in wanted})
                return {"status": "Pass", "actual": "已保存的登录信息仍然有效，直接复用", "reason": "", "category": "",
                        "assertions": [{"name": "登录信息有效", "passed": True, "expected": True, "actual": True}],
                        "duration_ms": 0, "reused": True}
        outcome = api.run_case(self.client, case, self.config, self.variables)
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
                    outcome = api.run_case(fresh, case, self.config, self.variables)
                    for cookie in fresh.cookies.jar:
                        self.client.cookies.jar.set_cookie(cookie)
                finally:
                    fresh.close()
                return outcome
            outcome = api.run_case(self.client, case, self.config, self.variables)
            login = self.cases.get(self.auth.get("api_case", ""))
            if outcome["category"] == "session_expired" and login and needs(case_id, login["id"], self.cases) and not self.relogged:
                self.relogged = True
                if self.api_login(login, force=True)["status"] != "Pass":
                    return blocked("登录已过期，重新登录没有成功", "session_expired")
                outcome = api.run_case(self.client, case, self.config, self.variables)
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
        try:
            self.driver.reset(self.saved.get("ui"))
            self.driver.goto(ir.absolute_url(automation["start_url"], self.config["base_url"]))
            return all(ui.check(self.driver, step, 3.0)["passed"] for step in automation.get("assertions", []))
        except Exception:  # noqa: BLE001  # Any failed login probe means the saved session cannot be reused.
            return False


DETAIL_KEYS = ("steps", "assertions", "request", "response", "screenshots", "fallbacks", "reused", "relogin", "extracted")


def record(run: dict, case: dict, outcome: dict, prerequisite: bool, started_at: str) -> None:
    db.insert("results", {
        "run_id": run["id"], "project_id": run["project_id"], "case_id": case["id"], "case_title": case["title"],
        "kind": case["kind"], "engine": "http" if case["kind"] == "api" else run["engine"],
        "status": outcome["status"], "category": outcome.get("category", ""), "reason": outcome.get("reason", ""),
        "actual": outcome.get("actual", ""), "duration_ms": int(outcome.get("duration_ms", 0)),
        "started_at": started_at, "prerequisite": int(prerequisite), "case_revision": case["revision"],
        "detail": {k: outcome[k] for k in DETAIL_KEYS if outcome.get(k) not in (None, [], "")},
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
            conn.execute(
                "UPDATE runs SET status = CASE WHEN cancel = 1 THEN 'Cancelled' ELSE 'Blocked' END, "
                "current = '', finished_at = ?, claim_token = '', "
                "reason = CASE WHEN cancel = 1 THEN '' ELSE ? END "
                "WHERE id = ? AND status = 'Running' AND pid IS ? AND claim_token = ?",
                (db.now(), "运行进程意外结束（可能是电脑休眠或进程被关闭），请重新运行", run["id"], run["pid"], run["claim_token"]))


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
        return _execute_claimed(run)


def _execute_claimed(run: dict) -> dict:
    """Only a process that claimed the row may enter the case loop."""
    run_id = run["id"]
    chosen = json.loads(run["case_ids"])
    os.environ["AUTOSKILL_HEADED"] = "1" if run.get("headed") else "0"  # the choice made when the run was queued
    counts = {"done": 0, "passed": 0, "failed": 0, "blocked": 0}
    final, reason, session = "Pass", "", None
    try:
        if run["cancel"]:
            db.finish_run(run_id, os.getpid(), counts, "Cancelled", "")
            return db.one("SELECT * FROM runs WHERE id = ?", (run_id,))
        session = Session(run, bool(run["force_login"]))
        outcomes: dict[str, str] = {}
        for case_id, prerequisite in build_plan(chosen, session.cases):
            if db.one("SELECT cancel FROM runs WHERE id = ?", (run_id,))["cancel"]:
                final = "Cancelled"
                break
            case = session.cases[case_id]
            label = ("前置：" if prerequisite else "") + case["title"]
            db.update("runs", run_id, {"current": label})
            started_at = db.now()
            broken = next((d for d in projects.depends_on(case) if outcomes.get(d) != "Pass"), None)
            outcome = (blocked(f"前置用例 {broken}「{session.cases[broken]['title']}」未通过，本用例没有执行", "prerequisite_failed")
                       if broken else session.run_one(case))
            outcomes[case_id] = outcome["status"]
            record(run, case, outcome, prerequisite, started_at)
            if not prerequisite:
                counts["done"] += 1
                counts[{"Pass": "passed", "Fail": "failed"}.get(outcome["status"], "blocked")] += 1
                db.update("runs", run_id, counts)
        if final != "Cancelled":
            final = "Fail" if counts["failed"] else "Blocked" if counts["blocked"] else "Pass"
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
        return None
    with _children_lock:
        _children[child.pid] = child
    with db.connect() as conn:
        conn.execute("UPDATE runs SET pid = ? WHERE id = ? AND status = 'Running' AND claim_token = ?", (child.pid, run_id, token))
    return child.pid


if __name__ == "__main__":
    token_arg = sys.argv.index("--claim-token") + 1 if "--claim-token" in sys.argv else None
    execute(sys.argv[1], force_login="--force-login" in sys.argv[2:], claim_token=sys.argv[token_arg] if token_arg else None)
