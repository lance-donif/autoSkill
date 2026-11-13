"""Concurrent execution of frozen saved cases with independent per-iteration sessions."""
from __future__ import annotations

import contextlib
import datetime
import json
import math
import os
import queue
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from . import db, execution_inputs, paths, projects, runner, secrets, task_context
from .replay import api, ir, ui


def configuration(spec: dict) -> dict:
    """Validate load without accepting booleans, nonfinite values or silent coercions."""
    if not isinstance(spec, dict):
        raise ValueError("压测参数必须是对象")
    result = {}
    for name, label in (("concurrency", "并发数"), ("iterations", "迭代次数")):
        value = spec.get(name, 1)
        if type(value) is not int or value < 1:
            raise ValueError(label + "需要是正整数")
        result[name] = value
    ramp = spec.get("ramp_up_seconds", 0)
    if isinstance(ramp, bool) or not isinstance(ramp, (int, float)) or not math.isfinite(ramp) or ramp < 0:
        raise ValueError("渐增时间需要是大于等于零的秒数")
    result["ramp_up_seconds"] = ramp
    return result


def prepare(project_id: str, case_ids: list[str] | None = None, *, concurrency: int = 1, iterations: int = 1,
            ramp_up_seconds: float = 0, engine: str = "playwright", headed: bool | None = None) -> dict:
    """Pin load parameters and the complete saved-case dependency closure before enqueue."""
    load = configuration({"concurrency": concurrency, "iterations": iterations, "ramp_up_seconds": ramp_up_seconds})
    if engine not in runner.ENGINES:
        raise ValueError("回放引擎只能是 playwright 或 selenium")
    if case_ids is not None and (not isinstance(case_ids, list) or not case_ids or not all(isinstance(value, str) and value for value in case_ids)):
        raise ValueError("请选择至少一条有效用例")
    frozen, manifest = execution_inputs.freeze(project_id, case_ids, engine, load=load)
    plan = runner.build_plan(manifest["selected"], manifest["cases"])
    unavailable = [identity for identity, _ in plan if not ir.is_replayable(manifest["cases"][identity])]
    if unavailable:
        raise ValueError("这些前置用例没有可回放的步骤：" + "、".join(unavailable))
    return {"id": db.new_id("run"), "family": "load", "project_id": project_id, "engine": engine, "trigger": "manual",
            "status": "Queued", "case_ids": manifest["selected"], "total": len(manifest["selected"]) * iterations,
            "created_at": db.now(), "headed": int(paths.headed() if headed is None else bool(headed)),
            "session_id": task_context.session_id() or "", **frozen}


def enqueue(project_id: str, case_ids: list[str] | None = None, **options) -> dict:
    projects.reindex(project_id)
    return runner.enqueue(prepare(project_id, case_ids, **options))


class LoadSession(runner.Session):
    """Reuse replay primitives, keeping all login state local and executing each attempt once."""

    def __init__(self, run: dict, user: int, iteration: int):
        super().__init__(run, force_login=True)
        self.client.close()
        self.saved = {}
        self.client = api.new_client(self.config)
        self.rand = uuid.uuid4().hex[:12]
        self.variables = {"RAND": self.rand}
        self.user, self.iteration = user, iteration

    def close(self) -> None:
        self.client.close()
        if self.driver:
            self.driver.close()

    def replay(self, case: dict, state: dict | None = None) -> dict:
        self.driver.reset(state)
        return self.attempt(case, "case", lambda attempt_id:
                            ui.run_case(self.driver, case, self.config, runner.evidence_dir(self.run["id"]), self.shot(attempt_id)))

    def run_one(self, case: dict) -> dict:
        self.count += 1
        ir.set_rand(f"{self.rand}{self.count}")
        if case["kind"] == "api":
            self.state_variables()
            if projects.depends_on(case) or case["id"] == self.auth.get("api_case"):
                return self.api_replay(self.client, case, self.variables)
            with api.new_client(self.config) as fresh:
                outcome = self.api_replay(fresh, case, self.variables)
                for cookie in fresh.cookies.jar:
                    self.client.cookies.jar.set_cookie(cookie)
                return outcome
        driver = self.browser()
        if not driver:
            return runner.blocked(self.driver_error, "environment")
        login = self.cases.get(self.auth.get("ui_case", ""))
        signed_in = bool(login) and runner.needs(case["id"], login["id"], self.cases)
        outcome = self.replay(case, self.saved.get("ui") if signed_in else None)
        if case["id"] == self.auth.get("ui_case") and outcome["status"] == "Pass":
            self.saved["ui"] = driver.storage_state()
            self.client.close()
            self.client = api.new_client(self.config, self.saved["ui"])
        return outcome


def _cancelled(run_id: str) -> bool:
    run = db.one("SELECT cancel, status FROM runs WHERE id = ?", (run_id,))
    return not run or bool(run["cancel"]) or run["status"] != "Running"


def _iteration(run: dict, user: int, iteration: int, counts: dict, lock: threading.Lock) -> None:
    session = None
    try:
        session = LoadSession(run, user, iteration)
        outcomes = {}
        with secrets.use_values(session.manifest["credentials"]):
            for case_id, prerequisite in runner.build_plan(session.manifest["selected"], session.cases):
                if _cancelled(run["id"]):
                    return
                case, started = session.cases[case_id], db.now()
                broken = next((dep for dep in projects.depends_on(case) if outcomes.get(dep) != "Pass"), None)
                outcome = (runner.blocked(f"前置用例 {broken} 未通过，本用例没有执行", "prerequisite_failed")
                           if broken else session.run_one(case))
                outcome = {**session.summarize(case, outcome), "load_user": user, "load_iteration": iteration}
                outcomes[case_id] = outcome["status"]
                with lock:
                    runner.record(run, case, outcome, prerequisite, started)
                    if not prerequisite:
                        counts["done"] += 1
                        counts[{"Pass": "passed", "Fail": "failed"}.get(outcome["status"], "blocked")] += 1
                        db.update("runs", run["id"], {**counts, "current": f"用户 {user} · 第 {iteration} 次 · {case['title']}"})
    finally:
        if session:
            with contextlib.suppress(Exception):  # A cleanup error must not discard completed attempt receipts.
                session.close()


def execute_claimed(run: dict) -> dict:
    """Run independent virtual users while retaining the queue's process ownership contract."""
    os.environ["AUTOSKILL_HEADED"] = "1" if run.get("headed") else "0"
    counts = {"done": 0, "passed": 0, "failed": 0, "blocked": 0}
    lock = threading.Lock()
    errors = []
    started = time.monotonic()
    try:
        manifest = execution_inputs.load(run, check_engine=True)
        load = configuration(manifest["load"])
        work = queue.Queue()
        for number in range(1, load["iterations"] + 1):
            work.put_nowait(number)

        def user(number):
            delay = load["ramp_up_seconds"] * number / load["concurrency"]
            while time.monotonic() - started < delay:
                if _cancelled(run["id"]):
                    return
                time.sleep(min(0.05, max(0, delay - (time.monotonic() - started))))
            while not _cancelled(run["id"]):
                try:
                    iteration = work.get_nowait()
                except queue.Empty:
                    return
                _iteration(run, number + 1, iteration, counts, lock)

        with ThreadPoolExecutor(max_workers=min(load["concurrency"], load["iterations"]), thread_name_prefix="autoskill-load") as pool:
            futures = [pool.submit(user, number) for number in range(min(load["concurrency"], load["iterations"]))]
            for future in futures:
                try:
                    future.result()
                except Exception as error:  # noqa: BLE001  # Other virtual users must finish and retain evidence when one worker fails.
                    errors.append(str(error) or type(error).__name__)
    except (ValueError, KeyError, OSError) as error:
        errors.append(str(error))
    cancelled = _cancelled(run["id"])
    final = ("Cancelled" if cancelled else "Fail" if counts["failed"] else "Blocked"
             if errors or counts["blocked"] or counts["done"] < run["total"] else "Pass")
    reason = "；".join(dict.fromkeys(errors))
    db.finish_run(run["id"], os.getpid(), counts, final, reason)
    return db.one("SELECT * FROM runs WHERE id = ?", (run["id"],))


def _elapsed(run: dict) -> float:
    if not run.get("started_at"):
        return 0
    start = datetime.datetime.fromisoformat(run["started_at"])
    finish = datetime.datetime.fromisoformat(run["finished_at"]) if run.get("finished_at") else datetime.datetime.now(datetime.UTC)
    return max(0, (finish - start).total_seconds())


def _percentile(values: list, fraction: float):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def metrics(run: dict, *, results: list[dict] | None = None) -> dict:
    """Count target executions, sent HTTP attempts and elapsed time as distinct measures."""
    rows = results if results is not None else db.rows("SELECT prerequisite, duration_ms, detail FROM results WHERE run_id = ?", (run["id"],))
    targets, requests = [], 0
    for row in rows:
        detail = row["detail"] if isinstance(row["detail"], dict) else json.loads(row["detail"])
        requests += int(bool(detail.get("request")))
        if not row["prerequisite"] and (detail.get("request") or detail.get("steps")):
            targets.append(row["duration_ms"])
    elapsed = _elapsed(run)
    return {"planned_cases": run["total"], "completed_cases": run["done"], "unexecuted_cases": max(0, run["total"] - run["done"]),
            "request_count": requests, "elapsed_seconds": elapsed, "throughput_per_second": run["done"] / elapsed if elapsed else 0,
            "error_rate": (run["failed"] + run["blocked"]) / run["done"] if run["done"] else None,
            "latency_samples": len(targets), "p50_ms": _percentile(targets, .5), "p95_ms": _percentile(targets, .95), "p99_ms": _percentile(targets, .99)}


def summary(run: dict, *, results=None, result_states=None, connection=None) -> dict:
    from . import task_state

    with task_state.projection():
        if connection is None:
            with db.connect() as current:
                current.execute("BEGIN")
                return summary(run, results=results, result_states=result_states, connection=current)
        rows = results if results is not None else [dict(row) for row in connection.execute("SELECT * FROM results WHERE run_id=? ORDER BY id", (run["id"],))]
        targets = [row for row in rows if not row["prerequisite"]]
        values = task_state.run_values(run, results=targets, result_states=result_states, connection=connection)
        manifest = execution_inputs.load(run)
        project = connection.execute("SELECT name FROM projects WHERE id = ?", (run["project_id"],)).fetchone()
        return {**runner.public_run(values, state=values["state"]), "case_ids": manifest["selected"], "load": manifest["load"],
                "project_name": project["name"] if project else run["project_id"], "metrics": metrics(run, results=rows)}


def get(run_id: str) -> dict:
    from . import task_reports, task_state

    with db.connect() as connection, task_state.projection():
        connection.execute("BEGIN")
        saved = connection.execute("SELECT * FROM runs WHERE id = ? AND family = 'load'", (run_id,)).fetchone()
        if saved is None:
            raise ValueError("压测运行不存在")
        run = dict(saved)
        rows = [dict(row) for row in connection.execute("SELECT * FROM results WHERE run_id = ? ORDER BY id", (run_id,))]
        for row in rows:
            row["detail"] = json.loads(row["detail"])
            row["state"] = task_state.result(row, connection=connection)
        states = [row["state"] for row in rows if not row["prerequisite"]]
        return {**summary(run, results=rows, result_states=states, connection=connection), "results": rows,
                "test_report": task_reports.peek("run_" + run_id)}
