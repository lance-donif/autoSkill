"""Run cases: resolve prerequisites, reuse or renew the login, record every result.

A run is a row in the local database. `python -m autoskill.runner <run_id>`
executes it in its own process so a browser crash cannot take the dashboard down.
"""
from __future__ import annotations

import json
import os
import re
import sys
import uuid
from pathlib import Path

from . import db, paths, projects, sessions
from .replay import api, ir, ui

ENGINES = ("playwright", "selenium")


def can_queue(case: dict) -> bool:
    """Recorded native mutations need a visible blocked result, even without replay support."""
    return ir.is_replayable(case) or (case.get("kind") == "ui" and bool(ir.replay_blockers(case)))


def create(project_id: str, case_ids: list[str] | None = None, engine: str = "playwright",
           trigger: str = "manual", task_id: str | None = None, batch_id: str | None = None,
           headed: bool | None = None) -> dict:
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
        chosen = [cid for cid, case in active.items() if can_queue(case)]
    not_ready = [cid for cid in chosen if not can_queue(active[cid])]
    if not_ready:
        raise ValueError("这些用例还没有可回放的步骤：" + "、".join(not_ready))
    if not chosen:
        raise ValueError("项目里还没有可回放的用例；先录制一条")
    run = {"id": db.new_id("run"), "batch_id": batch_id, "project_id": project_id, "engine": engine,
           "trigger": trigger, "task_id": task_id, "status": "Queued", "case_ids": chosen,
           "total": len(chosen), "created_at": db.now(), "headed": int(paths.headed() if headed is None else bool(headed))}
    db.insert("runs", run)
    return run


def evidence_dir(run_id: str) -> Path:
    return paths.local() / "runs" / run_id


def make_driver(engine: str, config: dict, *, asset_root: Path | None = None, output_root: Path | None = None):
    if engine == "selenium":
        from .replay.selenium_driver import SeleniumDriver

        return SeleniumDriver(config, headless=not paths.headed(), asset_root=asset_root, output_root=output_root)
    from .replay.playwright_driver import PlaywrightDriver

    return PlaywrightDriver(config, headless=not paths.headed(), asset_root=asset_root, output_root=output_root)


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
        self.config.update(_asset_root=str(projects.path(self.pid)),
                           _output_root=str(evidence_dir(run["id"]) / "artifacts"),
                           _cancel_check=lambda: bool((db.one("SELECT cancel FROM runs WHERE id = ?", (run["id"],)) or {}).get("cancel")))
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
            except Exception:
                pass

    def browser(self):
        if self.driver is None and not self.driver_error:
            try:
                self.driver = make_driver(self.run["engine"], self.config, asset_root=projects.path(self.pid),
                                          output_root=evidence_dir(self.run["id"]) / "artifacts")
            except ValueError as error:
                self.driver_error = str(error)
            except Exception as error:
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
            except Exception:
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
            if not probe or self.api_replay(self.client, probe, trial)["status"] == "Pass":
                self.variables.update({name: kept[name] for name in wanted})
                return {"status": "Pass", "actual": "已保存的登录信息仍然有效，直接复用", "reason": "", "category": "",
                        "assertions": [{"name": "登录信息有效", "passed": True, "expected": True, "actual": True}],
                        "duration_ms": 0, "reused": True}
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

    def api_replay(self, client, case: dict, variables: dict) -> dict:
        """Keep each case's protocol/audio artifacts distinct within a run."""
        config = {**self.config, "_output_root": str(evidence_dir(self.run["id"]) / "artifacts" / self.shot(case["id"])[:-4])}
        return api.run_case(client, case, config, variables)

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
                outcome = self.api_replay(self.client, case, self.variables)
                outcome["relogin"] = True
            return outcome
        blockers = ir.replay_blockers(case)
        if blockers:
            # Preflight before login reuse or browser startup: neither can make an
            # unsupported recorded mutation disappear or turn it into a Pass.
            return blocked("用例包含尚无可移植回放实现的原生命令：" + "；".join(blockers), ui.UNSUPPORTED)
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
        except Exception:
            return False


DETAIL_KEYS = ("steps", "assertions", "request", "response", "screenshots", "artifacts", "transport", "metrics", "fallbacks", "reused", "relogin", "extracted")


def record(run: dict, case: dict, outcome: dict, prerequisite: bool, started_at: str) -> None:
    db.insert("results", {
        "run_id": run["id"], "project_id": run["project_id"], "case_id": case["id"], "case_title": case["title"],
        "kind": case["kind"], "engine": "http" if case["kind"] == "api" else run["engine"],
        "status": outcome["status"], "category": outcome.get("category", ""), "reason": outcome.get("reason", ""),
        "actual": outcome.get("actual", ""), "duration_ms": int(outcome.get("duration_ms", 0)),
        "started_at": started_at, "prerequisite": int(prerequisite), "case_revision": case["revision"],
        "detail": {k: outcome[k] for k in DETAIL_KEYS if outcome.get(k) not in (None, [], "")},
    })


def execute(run_id: str, force_login: bool = False) -> dict:
    """Run a queued run to completion in this process and return its final row."""
    db.init()
    run = db.one("SELECT * FROM runs WHERE id = ?", (run_id,))
    if not run:
        raise ValueError(f"运行 {run_id} 不存在")
    chosen = json.loads(run["case_ids"])
    os.environ["AUTOSKILL_HEADED"] = "1" if run.get("headed") else "0"  # the choice made when the run was queued
    db.update("runs", run_id, {"status": "Running", "started_at": db.now(), "pid": os.getpid()})
    counts = {"done": 0, "passed": 0, "failed": 0, "blocked": 0}
    final, reason, session = "Pass", "", None
    try:
        session = Session(run, force_login)
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
            if broken:
                outcome = blocked(f"前置用例 {broken}「{session.cases[broken]['title']}」未通过，本用例没有执行", "prerequisite_failed")
            else:
                outcome = session.run_one(case)
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
    except Exception as error:  # never leave a run stuck in Running
        final, reason = "Blocked", "运行器出错：" + type(error).__name__
    finally:
        if session:
            session.close()
    db.update("runs", run_id, {**counts, "status": final, "reason": reason, "current": "", "finished_at": db.now()})
    return db.one("SELECT * FROM runs WHERE id = ?", (run_id,))


def spawn(run_id: str, force_login: bool = False) -> int:
    """Execute a run in its own process and return that process id."""
    import subprocess

    command = [sys.executable, "-m", "autoskill.runner", run_id, *(["--force-login"] if force_login else [])]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    child = subprocess.Popen(command, cwd=str(paths.APP), env=paths.child_env(), creationflags=flags,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return child.pid


if __name__ == "__main__":
    execute(sys.argv[1], force_login="--force-login" in sys.argv[2:])
