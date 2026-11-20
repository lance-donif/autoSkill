"""Project dispatched target samples and measured virtual-user activity for the load dashboard."""
from __future__ import annotations

import json
import math
import time
from http.client import responses

from . import execution_inputs, paths, task_state

_ERRORS = {
    "assertion_failed": "断言失败", "timeout": "请求超时", "cancelled": "执行已取消",
    "environment": "执行环境错误", "element_not_found": "未找到元素",
}


def timed_send(client, timing: dict, request):
    """Time only the underlying HTTP exchange; never retain its URL, headers or payload."""
    timing["start_ms"] = time.time_ns() / 1_000_000
    start = time.monotonic_ns()
    try:
        return client.send(request)
    finally:
        timing["elapsed_ms"] = (time.monotonic_ns() - start) / 1_000_000


def _timeline_file(run_id):
    return paths.local() / "runs" / run_id / "load-timeline.json"


def write_timeline(run_id: str, *, threads: list, stages: list) -> None:
    """Atomically publish measured worker counts and stage boundaries for this run."""
    events = _timeline_events({"threads": threads, "stages": stages})
    execution_inputs.write_json(_timeline_file(run_id), {"run_id": run_id, **events})


def _nonnegative(value) -> bool:
    return type(value) is int and value >= 0


def _time_ms(value) -> bool:
    return type(value) in (float, int) and math.isfinite(value) and value >= 0


def _timeline_events(content: dict) -> dict:
    if not isinstance(content.get("threads"), list) or not isinstance(content.get("stages"), list):
        return {"threads": [], "stages": []}
    threads = [{"time_ms": item["time_ms"], "count": item["count"]} for item in content["threads"]
               if isinstance(item, dict) and _time_ms(item.get("time_ms")) and _nonnegative(item.get("count"))]
    stages = [{key: item[key] for key in ("index", "concurrency", "start_ms", "end_ms")}
              for item in content["stages"] if isinstance(item, dict)
              and _nonnegative(item.get("index")) and item["index"] > 0
              and _nonnegative(item.get("concurrency")) and item["concurrency"] > 0
              and _time_ms(item.get("start_ms")) and "end_ms" in item
              and (item["end_ms"] is None or (_time_ms(item["end_ms"]) and item["end_ms"] >= item["start_ms"]))]
    return {"threads": threads, "stages": stages}


def _timeline(run_id: str) -> dict:
    try:
        content = json.loads(_timeline_file(run_id).read_bytes())
    except (OSError, ValueError):
        content = None
    if not isinstance(content, dict) or content.get("run_id") != run_id:
        return {"threads": [], "stages": []}
    return _timeline_events(content)


def _legacy_elapsed(kind, outcome):
    """Older receipts time the operation but do not identify its absolute dispatch instant."""
    elapsed = outcome.get("duration_ms")
    if not _time_ms(elapsed) or outcome.get("target_started") is False:
        return None
    if kind == "api":
        return elapsed if outcome.get("response") or (outcome.get("request") and outcome.get("target_started")) else None
    setup = outcome.get("setup_cases") or []
    if len(outcome.get("steps") or []) <= len(setup):
        return None
    # The old UI duration included inline prerequisite flows, each with its own measured duration.
    if any(not isinstance(item, dict) or not _nonnegative(item.get("duration_ms")) for item in setup):
        return None
    target = elapsed - sum(item["duration_ms"] for item in setup)
    return target if target >= 0 else None


def _sample_error(outcome, code):
    # Group the original failed check without exposing request data or assertion values.
    for check in outcome.get("assertions") or []:
        if isinstance(check, dict) and check.get("passed") is False:
            name = check.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()[:200]
    if type(code) is int and code >= 400:
        return f"{code}/{responses[code]}" if code in responses else f"HTTP {code}"
    return _ERRORS.get(outcome.get("category"), "执行失败")


def _postcondition_verdict(row, attempts):
    """Bind the downstream verdict to the last request in its verified source."""
    if attempts and attempts[-1].get("kind") == "postcondition":
        verdict = task_state.attempt_record(attempts[-1], result=row)
        source = (verdict or {}).get("postcondition_source") or {}
        if requests := source.get("attempts"):
            return requests[-1], verdict
    return None, None


def _sample(case, outcome, verdict, *, legacy):
    timing = outcome.get("load_sample")
    stage = None
    if isinstance(timing, dict):
        start, elapsed, stage = (timing.get(key) for key in ("start_ms", "elapsed_ms", "stage"))
        if case["kind"] != "api" or not (_time_ms(start) and _time_ms(elapsed)) or outcome.get("target_started") is not True:
            return None
    elif legacy:
        start, elapsed = None, _legacy_elapsed(case["kind"], outcome)
        if elapsed is None:
            return None
    else:
        return None
    success = task_state.case_outcome(verdict, verdict, evidence_valid=True) == "pass"
    sample = {"label": case["id"], "kind": case["kind"], "start_ms": start, "elapsed_ms": elapsed, "success": success}
    if _nonnegative(stage) and stage > 0:
        sample["stage"] = stage
    response = outcome.get("response") or {}
    code = response.get("status") if case["kind"] == "api" and isinstance(response, dict) else None
    if type(code) is int and 100 <= code <= 599:
        sample["code"] = code
    if not success:
        sample["error"] = _sample_error(verdict, sample.get("code"))
    return sample


def project(run: dict, rows: list, manifest: dict) -> dict:
    """Use hash-checked per-attempt evidence, excluding prerequisites and unexecuted targets."""
    samples, seen = [], set()
    selected = set(manifest.get("selected", []))
    legacy = "mode" not in manifest.get("load", {})
    for row in rows:
        case = manifest["cases"].get(row["case_id"])
        if (row.get("prerequisite") or row["case_id"] not in selected or not case
                or case["kind"] not in ("api", "ui") or case.get("extensions", {}).get("traffic") == "resource"):
            continue
        if row.get("state", {}).get("evidence") != "valid":
            continue
        detail = row["detail"]
        if isinstance(detail, str):
            detail = json.loads(detail)
        attempts = detail.get("attempts", [])
        checked_request, verdict = _postcondition_verdict(row, attempts)
        for ref in attempts:
            if ref.get("kind") not in ("case", "captcha_retry", "relogin_retry") or ref.get("id") in seen:
                continue
            outcome = task_state.attempt_record(ref, result=row)
            if outcome is not None:
                sample = _sample(case, outcome, verdict if ref == checked_request else outcome, legacy=legacy)
                if sample is not None:
                    samples.append(sample)
                    seen.add(ref["id"])
    report = {"samples": samples, **_timeline(run["id"])}
    if manifest.get("load", {}).get("driver") == "locust":
        report["thread_events"] = True
        # Keep short runs legible; longer observations use the standard JMeter report interval.
        first = min((sample["start_ms"] for sample in samples if sample["start_ms"] is not None), default=0)
        last = max((sample["start_ms"] + sample["elapsed_ms"] for sample in samples if sample["start_ms"] is not None), default=first)
        report["granularity_ms"] = 1000 if last - first < 600_000 else 60_000
    return report
