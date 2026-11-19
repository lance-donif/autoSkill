"""Normalize native reports without treating a successful launcher exit as a business assertion."""
from __future__ import annotations

import csv
import json
import math
import xml.etree.ElementTree as ET
from pathlib import Path


def _assertion(name: str, passed: bool, actual: str = "") -> dict:
    return {"name": name, "passed": passed, "actual": actual, "source": "native"}


def junit(file: Path) -> list[dict]:
    root = ET.parse(file).getroot()
    cases = []
    for number, test in enumerate(root.iter("testcase"), 1):
        failure, error, skipped = test.find("failure"), test.find("error"), test.find("skipped")
        result = failure if failure is not None else error if error is not None else skipped
        status = "Fail" if failure is not None else "Blocked" if error is not None or skipped is not None else "Pass"
        reason = "" if result is None else (result.get("message", "") + "\n" + (result.text or "")).strip()
        native_id = test.get("classname", "") + "::" + test.get("name", str(number))
        setup_error = error is not None and (error.get("message") or "").startswith("failed on setup with ")
        cases.append({"target_started": skipped is None and not setup_error, "id": native_id, "title": test.get("name", native_id), "status": status, "reason": reason,
                      "duration_ms": round(float(test.get("time", "0")) * 1000),
                      "assertions": [] if status == "Blocked" else [_assertion("原生测试结果", status == "Pass", reason)],
                      "verdict_scope": "native_test", "native_status": status})
    for number, suite in enumerate(root.iter("testsuite"), 1):
        described_errors = sum(test.find("error") is not None for test in suite.findall("testcase"))
        if int(suite.get("errors", "0")) > described_errors:
            cases.append({"target_started": False, "id": f"suite-error-{number}", "title": suite.get("name", "原生测试组"), "status": "Blocked",
                          "reason": suite.findtext("system-err") or "原生报告记录了未归属测试用例的执行错误", "assertions": [], "duration_ms": 0})
    return cases


def _partial_unittest(journal: Path, reason: str) -> list[dict]:
    records, active = [], None
    with journal.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.endswith("\n"):
                break
            entry = json.loads(line)
            if entry["event"] == "start":
                active = entry
            elif entry["event"] == "stop":
                active = None
            elif entry["event"] == "result":
                records.append(entry["row"])
                if active and entry["row"]["id"] == active["id"]:
                    active = None
    if active:
        records.append({"id": active["id"], "title": active["title"], "status": "Blocked", "reason": reason or "测试执行中断",
                        "target_started": True, "duration_ms": 0})
    return records


def unittest_report(file: Path, reason: str = "") -> list[dict]:
    records = json.loads(file.read_text(encoding="utf-8"))["cases"] if file.is_file() else _partial_unittest(file.with_suffix(".jsonl"), reason)
    return [{**item, "assertions": [] if item["status"] == "Blocked" else
             [_assertion("原生 unittest 测试结果", item["status"] == "Pass", item.get("reason", ""))],
             "verdict_scope": "native_test"} for item in records]


def newman(file: Path) -> list[dict]:
    data = json.loads(file.read_text(encoding="utf-8"))
    cases = []
    native_result = data.get("run", data.get("result", {}))
    for index, execution in enumerate(native_result.get("executions", []), 1):
        item = execution.get("item", {})
        assertions = [_assertion(check.get("assertion", "断言"), not check.get("error"),
                                 check.get("error", {}).get("message", ""))
                      for check in execution.get("assertions", []) if not check.get("skipped")]
        response = execution.get("response") or {}
        error = execution.get("requestError") or response.get("error") or execution.get("scriptErrors")
        failed = [check for check in assertions if not check["passed"]]
        started = bool(response or execution.get("requestError") or assertions)
        if not started and not error:
            error = "原生请求缺少执行回执"
        status = "Fail" if failed else "Blocked" if error else "Pass"
        cases.append({"id": str(item.get("id") or index) + ":" + str(execution.get("cursor", {}).get("iteration", 0)),
                      "title": item.get("name", str(index)), "status": status, "target_started": started,
                      "execution_only_pass": status == "Pass" and bool(response) and not assertions,
                      "reason": str(error) if error else "；".join(check["actual"] for check in failed),
                      "duration_ms": response.get("responseTime", 0), "assertions": assertions,
                      "verdict_scope": "native_assertions" if assertions else "execution"})
    for index, failure in enumerate(native_result.get("failures", []), 1):
        error = failure.get("error", {})
        if error.get("name") == "AssertionError" and any(item["status"] == "Fail" for item in cases):
            continue
        is_assertion = error.get("name") == "AssertionError"
        reason = error.get("message") or str(error)
        cases.append({"id": f"native-failure-{index}", "title": error.get("test") or failure.get("at") or "原生执行错误",
                      "status": "Fail" if is_assertion else "Blocked", "target_started": bool(is_assertion or failure.get("at") == "request"),
                      "reason": reason, "duration_ms": 0,
                      "assertions": [_assertion("原生断言", False, reason)] if is_assertion else [], "verdict_scope": "native_assertions" if is_assertion else "execution"})
    return cases


def _percentile(values: list[float], quantile: float) -> float:
    return sorted(values)[max(0, math.ceil(len(values) * quantile) - 1)] if values else 0.0


def jmeter(file: Path, thresholds: dict) -> tuple[list[dict], dict]:
    with file.open(encoding="utf-8-sig", newline="") as stream:
        samples = list(csv.DictReader(stream))
    cases, latencies, timestamps, endings, threads = [], [], [], [], []
    for index, sample in enumerate(samples, 1):
        elapsed = float(sample.get("elapsed") or 0)
        timestamp = float(sample.get("timeStamp") or 0)
        success = str(sample.get("success", "")).lower() == "true"
        message = sample.get("failureMessage", "")
        cases.append({"id": f"sample-{index}", "title": sample.get("label", str(index)),
                      "status": "Pass" if success else "Fail", "reason": message or ("" if success else sample.get("responseMessage", "采样失败")),
                      "duration_ms": elapsed, "target_started": True, "execution_only_pass": success and not message, "assertions": [_assertion("原生断言", False, message)] if message else [],
                      "verdict_scope": "native_assertions" if message else "execution", "sample": sample})
        latencies.append(elapsed)
        timestamps.append(timestamp)
        endings.append(timestamp + elapsed)
        threads.append(int(sample.get("allThreads") or sample.get("grpThreads") or 0))
    duration = (max(endings) - min(timestamps)) / 1000 if samples else 0
    metrics = {"samples": len(samples), "failed_samples": sum(case["status"] != "Pass" for case in cases),
               "elapsed_seconds": duration, "throughput_per_second": len(samples) / duration if duration > 0 else 0,
               "p50_ms": _percentile(latencies, 0.5), "p95_ms": _percentile(latencies, 0.95),
               "p99_ms": _percentile(latencies, 0.99), "max_threads": max(threads, default=0)}
    metrics["error_rate"] = metrics["failed_samples"] / len(samples) if samples else 0
    checks = []
    for key, actual_key in (("max_p95_ms", "p95_ms"), ("max_error_rate", "error_rate")):
        if key in thresholds:
            checks.append(_assertion(key, metrics[actual_key] <= float(thresholds[key]), str(metrics[actual_key])))
    if "min_throughput" in thresholds:
        checks.append(_assertion("min_throughput", metrics["throughput_per_second"] >= float(thresholds["min_throughput"]), str(metrics["throughput_per_second"])))
    if checks:
        cases.append({"id": "performance-targets", "title": "性能目标", "status": "Pass" if all(item["passed"] for item in checks) else "Fail",
                      "reason": "", "duration_ms": 0, "assertions": checks, "verdict_scope": "performance", "target_started": True,
                      "execution_only_pass": bool(samples) and all(check["passed"] for check in checks)})
    return cases, metrics


def summarize(cases: list[dict], raw: dict, evidence: Path, metrics: dict | None = None) -> dict:
    failures = any(item["status"] == "Fail" for item in cases)
    blocked = any(item["status"] == "Blocked" for item in cases)
    assertions = [check for item in cases for check in item.get("assertions", [])]
    state = raw.get("state", "blocked")
    reason = raw.get("reason", "")
    if state == "cancelled":
        status = "Cancelled"
    elif failures:
        status = "Fail"
    elif state != "completed" or blocked or not cases or raw.get("exit_code") != 0:
        status = "Blocked"
        reason = reason or ("原生报告没有可核查的测试结果" if not cases else "原生运行未完整完成，请查看日志和逐用例结果")
    else:
        status = "Pass"
    business_cases = [item for item in cases if item.get("verdict_scope") not in ("performance", "execution")]
    business_assertions = [check for item in business_cases for check in item.get("assertions", [])]
    business_failed = any(not check.get("passed") for check in business_assertions)
    verified = status == "Pass" and bool(business_assertions) and all(item.get("assertions") for item in cases) and not business_failed
    return {"status": status, "reason": reason, "cases": cases, "metrics": metrics or {}, "process": raw,
            "execution_status": state, "business_status": "Fail" if business_failed else "Pass" if verified else "Unverified",
            "verification": "verified" if verified else "failed" if business_failed else "unverified",
            "verdict_scope": "native_assertions" if business_assertions else "performance" if assertions else "execution",
            "evidence": [file.relative_to(evidence).as_posix() for file in sorted(evidence.rglob("*"))
                         if file.is_file() and "input" not in file.relative_to(evidence).parts and file.name != "process-spec.json"]}
