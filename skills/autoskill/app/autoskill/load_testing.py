"""Concurrent execution of frozen saved cases with independent per-iteration sessions."""
from __future__ import annotations

import datetime
import hashlib
import importlib.metadata
import json
import math
import os
import subprocess
import sys
from pathlib import Path

from . import db, execution_inputs, load_report, paths, projects, runner, task_context
from .replay import ir


def configuration(spec: dict) -> dict:
    """Validate load without accepting booleans, nonfinite values or silent coercions."""
    if not isinstance(spec, dict):
        raise ValueError("压测参数必须是对象")

    def integer(key, title, default=None):
        value = spec.get(key, default)
        if type(value) is not int or value < 1:
            raise ValueError(title + "需要是正整数")
        return value

    def seconds(key, title, *, zero=False):
        value = spec.get(key, 0 if zero else None)
        if type(value) not in (float, int) or value > sys.float_info.max or value < -sys.float_info.max or not math.isfinite(value) or value < 0 or (not value and not zero):
            raise ValueError(title + ("需要是非负秒数" if zero else "需要是正秒数"))
        return value

    mode = spec.get("mode", "performance")
    if mode not in ("performance", "stress"):
        raise ValueError("测试模式须为 performance 或 stress")
    load = {"mode": mode, "driver": "locust", "driver_version": "2.47.0"}
    if mode == "stress":
        load.update(initial_concurrency=integer("initial_concurrency", "初始并发数"),
                    max_concurrency=integer("max_concurrency", "最大并发数"), step_concurrency=integer("step_concurrency", "每级增加并发数"),
                    step_duration_seconds=seconds("step_duration_seconds", "每级持续时间"))
        if load["max_concurrency"] < load["initial_concurrency"]:
            raise ValueError("最大并发数不能小于初始并发数")
        return load
    load["concurrency"] = integer("concurrency", "并发数", 1 if "iterations" in spec else None)
    if "iterations" in spec and ("mode" not in spec or spec.get("driver") == "locust"):
        load.update(iterations=integer("iterations", "迭代次数"), ramp_up_seconds=seconds("ramp_up_seconds", "渐增时间", zero=True))
        return load
    if spec.get("duration_seconds") is not None:
        load["duration_seconds"] = seconds("duration_seconds", "持续时间")
    if spec.get("target_requests") is not None:
        load["target_requests"] = integer("target_requests", "目标请求数")
    if "target_requests" not in load and "duration_seconds" not in load:
        raise ValueError("请填写持续时间或目标请求数")
    return load


def _worker_hash():
    return hashlib.sha256(Path(__file__).with_name("locust_worker.py").read_bytes()).hexdigest()


def prepare(project_id: str, case_ids: list[str] | None = None, *, engine: str = "locust", headed: bool | None = None,
            inputs: dict | None = None, **spec) -> dict:
    """Pin HTTP-only cases and Locust configuration before enqueue."""
    if headed is not None and type(headed) is not bool:
        raise ValueError("显示浏览器窗口必须为布尔值")
    load = {**configuration(spec), "worker_sha256": _worker_hash()}
    if engine not in {*runner.ENGINES, "locust", "http"}:
        raise ValueError("压测引擎为 Locust")
    if case_ids is not None and (not isinstance(case_ids, list) or not case_ids or not all(isinstance(value, str) and value for value in case_ids)):
        raise ValueError("请选择至少一条有效用例")
    frozen, manifest = execution_inputs.freeze(project_id, case_ids, "locust", load=load, api_only=True, inputs=inputs)
    plan = runner.build_plan(manifest["selected"], manifest["cases"])
    resources = [key for key in manifest["selected"] if manifest["cases"][key].get("extensions", {}).get("traffic") == "resource"]
    if resources:
        raise ValueError("压测需要业务 API 用例；以下是页面资源请求：" + "、".join(resources))
    unavailable = [identity for identity, _ in plan if not ir.is_replayable(manifest["cases"][identity])]
    if unavailable:
        raise ValueError("这些前置用例没有可回放的步骤：" + "、".join(unavailable))
    return {"id": db.new_id("run"), "family": "load", "project_id": project_id, "engine": "locust", "trigger": "manual",
            "status": "Queued", "case_ids": manifest["selected"], "total": len(manifest["selected"]) * load.get("iterations", 0),
            "load_mode": load["mode"],
            "created_at": db.now(), "headed": 0,
            "session_id": task_context.session_id() or "", **frozen}


def enqueue(project_id: str, case_ids: list[str] | None = None, **options) -> dict:
    projects.reindex(project_id)
    return runner.enqueue(prepare(project_id, case_ids, **options))


def execute_claimed(run: dict) -> dict:
    """Keep Locust's gevent patch and lifetime isolated from the dashboard and queue owner."""
    reason = ""
    try:
        stored = execution_inputs.load(run)
        if stored["load"].get("driver") != "locust":
            raise ValueError("此排队记录使用旧压测引擎，请选择接口用例并创建新的 Locust 测试")
        if stored["load"].get("worker_sha256") != _worker_hash():
            raise ValueError("Locust 执行器在排队期间已更新，请重新创建压测")
        execution_inputs.load(run, check_engine=True)
        try:
            version = importlib.metadata.version("locust")
        except importlib.metadata.PackageNotFoundError:
            version = None
        if version != stored["load"]["driver_version"]:
            raise ValueError("需要 Locust " + stored["load"]["driver_version"] + "，请检查当前运行环境")
        environment = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
        environment.pop("LOCUST_SKIP_MONKEY_PATCH", None)
        child = subprocess.run([sys.executable, "-m", "autoskill.locust_worker", run["id"], str(os.getpid())],
                               cwd=paths.APP, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, check=False)
        reason = ("Locust 工作进程异常退出，请核对运行环境和已安装的引擎版本" if child.returncode
                  else json.loads(child.stdout.decode())["reason"])
    except (KeyError, OSError, ValueError) as error:
        reason = str(error) if isinstance(error, ValueError) else "无法读取冻结输入或启动 Locust 工作进程"
    row = db.one("SELECT * FROM runs WHERE id=?", (run["id"],))
    counts = {key: row[key] for key in ("done", "passed", "failed", "blocked")}
    status = ("Cancelled" if row["cancel"] else "Fail" if counts["failed"] else "Blocked"
              if reason or counts["blocked"] or not counts["done"] or counts["done"] < row["total"] else "Pass")
    db.finish_run(run["id"], os.getpid(), counts, status, reason)
    return db.one("SELECT * FROM runs WHERE id=?", (run["id"],))


def _elapsed(run: dict) -> float:
    if not run.get("started_at"):
        return 0
    start = datetime.datetime.fromisoformat(run["started_at"])
    finish = datetime.datetime.fromisoformat(run["finished_at"]) if run.get("finished_at") else datetime.datetime.now(datetime.UTC)
    return max(0, (finish - start).total_seconds())


def _percentiles(values: list, *, jmeter: bool) -> tuple:
    if not values:
        return None, None, None
    ordered = sorted(values[-20_000:] if jmeter else values)
    size = len(ordered)

    def percentile(fraction):
        if not jmeter:
            return ordered[math.ceil(size * fraction) - 1]
        position = (size + 1) * fraction
        if position <= 1:
            return ordered[0]
        if position >= size:
            return ordered[-1]
        lower = math.floor(position)
        return ordered[lower - 1] + (position - lower) * (ordered[lower] - ordered[lower - 1])

    return percentile(.5), percentile(.95), percentile(.99)


def metrics(run: dict, *, results: list[dict] | None = None, samples=None) -> dict:
    """Count target executions, sent HTTP attempts and elapsed time as distinct measures."""
    rows = results if results is not None else db.rows("SELECT * FROM results WHERE run_id = ?", (run["id"],))
    if "not_run" not in run:
        from . import task_state

        run = task_state.run_values(run, results=rows)
    completed = run["passed"] + run["failed"]
    if samples is not None:
        requests = len(samples)
        targets = [sample["elapsed_ms"] for sample in samples]
        elapsed = ((max(s["start_ms"] + s["elapsed_ms"] for s in samples) - min(s["start_ms"] for s in samples)) / 1000
                   if samples else 0)
        throughput = requests / elapsed if elapsed else None
        error_rate = sum(not s["success"] for s in samples) / requests if requests else None
    else:
        targets, requests = [], 0
        for row in rows:
            detail = row["detail"] if isinstance(row["detail"], dict) else json.loads(row["detail"])
            requests += int(bool(detail.get("request")))
            if not row["prerequisite"] and (detail.get("request") or detail.get("steps")):
                targets.append(row["duration_ms"])
        elapsed = _elapsed(run)
        throughput = completed / elapsed if elapsed else 0
        error_rate = run["failed"] / completed if completed else None
    median, p95, p99 = _percentiles(targets, jmeter=samples is not None)
    return {"planned_cases": run["total"], "completed_cases": completed, "unexecuted_cases": run["not_run"],
            "request_count": requests, "elapsed_seconds": elapsed, "throughput_per_second": throughput, "error_rate": error_rate,
            "latency_samples": len(targets), "p50_ms": median, "p95_ms": p95, "p99_ms": p99}


def summary(run: dict, *, results=None, result_states=None, connection=None, samples=None, include_metrics=True) -> dict:
    from . import task_state

    with task_state.projection():
        if connection is None:
            with db.connect() as current:
                current.execute("BEGIN")
                return summary(run, results=results, result_states=result_states, connection=current, samples=samples,
                               include_metrics=include_metrics)
        rows = results if results is not None else [dict(row) for row in connection.execute("SELECT * FROM results WHERE run_id=? ORDER BY id", (run["id"],))]
        targets = [row for row in rows if not row["prerequisite"]]
        values = task_state.run_values(run, results=targets, result_states=result_states, connection=connection)
        manifest = execution_inputs.load(run)
        project = connection.execute("SELECT name FROM projects WHERE id = ?", (run["project_id"],)).fetchone()
        public = {**runner.public_run(values, state=values["state"]), "case_ids": manifest["selected"],
                  "load": {"mode": "performance", **manifest["load"]}, "project_name": project["name"] if project else run["project_id"]}
        if not include_metrics:
            return public
        if samples is None and manifest["load"].get("driver") == "locust":
            for row in targets:
                if "state" not in row:
                    row["state"] = task_state.result(row, connection=connection)
            samples = load_report.project(run, targets, manifest)["samples"]
        return {**public, "metrics": metrics(values, results=rows, samples=samples)}


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
        manifest = execution_inputs.load(run)
        report = load_report.project(run, rows, manifest)
        samples = report["samples"] if manifest["load"].get("driver") == "locust" else None
        return {**summary(run, results=rows, result_states=states, connection=connection, samples=samples), "results": rows, "load_report": report,
                "test_report": task_reports.peek("run_" + run_id)}
