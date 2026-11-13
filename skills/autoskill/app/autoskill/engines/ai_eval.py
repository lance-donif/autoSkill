"""Concurrent multi-turn AI evaluation keeps answer quality separate from observable transport timing."""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import threading
import time
import uuid
from collections import deque
from pathlib import Path

import httpx

from .. import paths, secrets
from . import ai_audio, ai_journal, ai_judging, ai_oracles, ai_protocols


class AiEvaluationAdapter:
    id = "ai-eval"

    def __init__(self):
        self._cancels = {}
        self._lock = threading.Lock()

    def capabilities(self) -> dict:
        return {"id": self.id, "name": "AI 多轮问数与流式性能评估", "protocols": ["http", "sse", "websocket"],
                "multi_turn_case": True, "native_concurrency": True, "independent_oracles": True,
                "websocket_audio": True, "cancel": True, "events": True, "runtime_download": False}

    def probe(self) -> dict:
        websocket = importlib.util.find_spec("websockets") is not None
        return {"id": self.id, "available": True, "version": "1", "protocols": ["http", "sse", *(["websocket"] if websocket else [])],
                "diagnosis": "可用" if websocket else "HTTP/SSE 可用；WebSocket 需要安装 websockets"}

    def discover(self, root: str) -> list[dict]:
        found = []
        for file in sorted(Path(root).rglob("*.json")):
            try:
                spec = json.loads(file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeError):
                continue
            if isinstance(spec, dict) and "endpoint" in spec and "samples" in spec:
                found.append({"path": str(file), "cases": [{"id": item.get("id", str(n)), "turns": len(item.get("turns", [item]))}
                                                           for n, item in enumerate(spec["samples"], 1)]})
        return found

    def default_entry(self, files: list[str], cwd: str) -> list[str]:
        prefix = "" if cwd == "." else cwd.rstrip("/") + "/"
        candidates = [file[len(prefix):] for file in files if file.startswith(prefix) and file.endswith(".json")]
        if "spec.json" in candidates:
            return ["spec.json"]
        if len(candidates) == 1:
            return candidates
        raise ValueError("AI 评估资产需要指定 JSON 入口 argv，或提供 spec.json")

    def prepare(self, asset: dict, root: Path, evidence: Path, parameters: dict) -> dict:
        base = root / asset.get("cwd", ".")
        spec_file = base / asset["argv"][0]
        spec = json.loads(spec_file.read_text(encoding="utf-8"))
        spec = secrets.resolve({**spec, **parameters})
        _validate(spec)
        if isinstance(spec.get("dataset"), str):
            spec["dataset"] = json.loads((base / spec["dataset"]).read_text(encoding="utf-8"))
        ai_audio.freeze(spec, base)
        evidence.mkdir(parents=True, exist_ok=True)
        return {"engine": self.id, "spec": spec, "evidence": str(evidence), "asset_id": asset["id"],
                "spec_hash": hashlib.sha256(json.dumps(spec, sort_keys=True, ensure_ascii=False).encode()).hexdigest()}

    def execute(self, prepared: dict, *, cancelled=None, emit=None) -> dict:
        with self._lock:
            cancellation = self._cancels.setdefault(prepared["evidence"], threading.Event())

        def should_cancel():
            return cancellation.is_set() or bool(cancelled and cancelled())

        try:
            return asyncio.run(_evaluate(prepared, should_cancel, emit))
        except Exception as error:  # noqa: BLE001  # A later adapter failure must preserve completed assertions and request receipts.
            return self.recover(prepared, error)

    def recover(self, prepared: dict, error) -> dict:
        reason = "评估执行中断：" + str(error)
        cases = ai_journal.recover_cases(prepared, reason)
        spec = prepared["spec"]
        intended = len(spec["samples"]) * spec.get("load", {}).get("iterations", 1)
        result = _summary(cases, sum(case.get("duration_ms", 0) for case in cases) / 1000, spec.get("load", {}).get("concurrency", 1), intended)
        if result["status"] != "Fail":
            result.update(status="Blocked", execution_status="blocked")
        return {**result, "reason": result["reason"] or reason, "interruption": reason, "spec_hash": prepared["spec_hash"]}

    def collect(self, prepared: dict, raw: dict) -> dict:
        file = Path(prepared["evidence"]) / "ai-evaluation.json"
        file.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        data = file.read_bytes()
        return {**raw, "evidence": [{"path": str(file), "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}]}

    def cleanup(self, prepared: dict) -> None:
        with self._lock:
            self._cancels.pop(prepared["evidence"], None)

    def cancel(self, prepared: dict) -> None:
        with self._lock:
            self._cancels.setdefault(prepared["evidence"], threading.Event()).set()

    def events(self, prepared: dict) -> list[dict]:
        file = Path(prepared["evidence"]) / "ai-evaluation.json"
        return json.loads(file.read_text(encoding="utf-8")).get("events", []) if file.is_file() else []

    def heartbeat(self, prepared: dict) -> dict:
        with self._lock:
            event = self._cancels.get(prepared["evidence"])
            return {"engine": self.id, "asset_id": prepared["asset_id"], "cancelled": bool(event and event.is_set())}


def _validate(spec: dict) -> None:
    if not isinstance(spec.get("endpoint"), dict) or not spec["endpoint"].get("url"):
        raise ValueError("AI 评估需要 endpoint.url")
    if spec["endpoint"].get("protocol", "http") not in ("http", "sse", "websocket"):
        raise ValueError("AI 评估协议需要是 http、sse 或 websocket")
    if not isinstance(spec.get("samples"), list) or not spec["samples"]:
        raise ValueError("AI 评估至少需要一条对话用例")
    for name in ("concurrency", "iterations"):
        value = spec.get("load", {}).get(name, 1)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} 需要是正整数")
    for value in spec["endpoint"].get("timeouts", {}).values():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ValueError("响应超时时长需要是正数秒")
    for sample in spec["samples"]:
        turns = sample.get("turns", [sample])
        if not turns or not all(isinstance(turn, dict) and "question" in turn for turn in turns):
            raise ValueError("多轮对话的每轮都需要 question")


def _fill(value, bindings):
    if isinstance(value, dict):
        return {key: _fill(item, bindings) for key, item in value.items()}
    if isinstance(value, list):
        return [_fill(item, bindings) for item in value]
    if isinstance(value, str):
        for key, replacement in bindings.items():
            marker = "{{" + key + "}}"
            if value == marker:
                return replacement
            value = value.replace(marker, str(replacement))
    return value


def _body(endpoint, question, messages, context):
    bindings = {"question": question, "messages": messages, **context}
    return secrets.resolve(_fill(endpoint.get("body", {"messages": "{{messages}}", "question": "{{question}}"}), bindings))


def _quality(observation: dict, turn: dict, spec: dict, *, clarifying: bool = False) -> tuple[list[dict], str]:
    if observation.get("error"):
        return [], "Unverified"
    if clarifying:
        turn = {**turn, "checks": [check for check in turn.get("checks", []) if check.get("when") != "final_answer"]}
        if turn.get("expected_phase") == "final_answer":
            turn = {key: value for key, value in turn.items() if key not in ("expected", "reference")}
    checks = ai_oracles.check_answer(observation["answer"], turn, spec.get("dataset", []))
    status = "Fail" if any(not check["passed"] for check in checks) else ("Pass" if checks else "Unverified")
    return checks, status


def _performance(observation: dict, goals: dict) -> list[dict]:
    checks = []
    for key, maximum in goals.items():
        actual = observation["timings"].get(key)
        checks.append({"label": f"{key} 不超过 {maximum} ms", "expected": maximum, "actual": actual, "source": "performance_target",
                       "passed": actual is not None and actual <= maximum})
    return checks


def _case_result(sample, case_id, turns, elapsed):
    assertions = [check for turn in turns for check in turn["assertions"]]
    business = "Fail" if any(turn["business_status"] == "Fail" for turn in turns) else "Pass"
    if business == "Pass" and any(turn["business_status"] != "Pass" for turn in turns):
        business = "Unverified"
    status = "Fail" if any(turn["status"] == "Fail" for turn in turns) else "Pass"
    if status == "Pass" and any(turn["status"] == "Blocked" for turn in turns):
        status = "Blocked"
    if status in ("Pass", "Blocked") and any(turn["status"] == "Cancelled" for turn in turns):
        status = "Cancelled"
    if business == "Pass" and turns and turns[-1].get("judgement"):
        judgement = turns[-1]["judgement"]
        if judgement.get("verdict") != "pass" or judgement.get("action") != "finish":
            business = "Unverified"
    reason = next((turn["reason"] for turn in turns if turn["status"] in ("Fail", "Cancelled", "Blocked")), "")
    return {"id": case_id, "title": sample.get("title", sample.get("id", "AI 多轮对话")), "status": status, "reason": reason,
            "business_status": business, "verification": "verified" if business != "Unverified" else "unverified",
            "assertions": assertions, "turns": turns, "duration_ms": elapsed, "attempts": 1}


def _turn_result(observation, turn, spec, judgement):
    clarifying = judgement and judgement.get("verdict") == "needs_clarification"
    checks, business = _quality(observation, turn, spec, clarifying=bool(clarifying))
    checks, business = ai_judging.merge(checks, business, judgement)
    performance = _performance(observation, {**spec.get("performance", {}), **turn.get("performance", {})})
    failed = observation.get("error") or business == "Fail" or any(not check["passed"] for check in performance)
    status = "Fail" if failed else "Pass"
    reason = observation.get("error") or ("回答不符合独立预期" if business == "Fail" else ("响应时间不符合目标" if failed else ""))
    if judgement and judgement.get("pending"):
        status = "Fail" if failed else "Blocked"
        reason = reason or judgement["reason"]
    if observation.get("cancelled") or (judgement and judgement.get("cancelled")):
        status, reason = "Cancelled", "运行已取消"
    return {**observation, "status": status, "reason": reason, "business_status": business,
            "assertions": checks, "performance_assertions": performance, "judgement": judgement}


async def _conversation(sample, spec, case_id, user_id, cancelled, emit, prepared):
    started, history, turns = time.monotonic(), [], []
    context = {"user_id": user_id, "case_id": case_id, "conversation_id": uuid.uuid4().hex, **sample.get("context", {})}
    pending = deque(sample.get("turns", [sample]))
    paths.configure_http_environment()
    async with httpx.AsyncClient(timeout=None, follow_redirects=True) as client:
        while pending:
            turn, number = pending.popleft(), len(turns) + 1
            question = _fill(turn["question"], context)
            history.append({"role": "user", "content": question})
            endpoint = secrets.resolve(_fill({**spec["endpoint"], **turn.get("endpoint", {})}, {"question": question, "messages": history, **context}))
            output = hashlib.sha256(f"{case_id}:{number}".encode()).hexdigest() + ".bin"
            endpoint["_audio_output"] = str(Path(prepared["evidence"]) / "audio" / output)
            body = _body(endpoint, question, history, context)
            ai_journal.intent(prepared, sample, case_id, number, question, endpoint, body)
            observation = await ai_protocols.request(client, endpoint, body, cancelled)
            ai_journal.observe(prepared, case_id, number, observation)
            judgement = await ai_judging.assess(prepared, sample, turn, history, observation, case_id, number, cancelled, emit)
            result = {**_turn_result(observation, turn, spec, judgement), "turn": number, "question": question, "dynamic": turn.get("dynamic", False)}
            ai_journal.finish_turn(prepared, case_id, number, result)
            turns.append(result)
            answer = observation["answer"]
            history.append({"role": "assistant", "content": answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)})
            context["previous_answer"] = answer
            context.update(observation["bindings"])
            if emit:
                emit({"event": "turn_completed", "case_id": case_id, "turn": number, "status": result["status"], "business_status": result["business_status"]})
            if result["status"] in ("Cancelled", "Blocked") or observation.get("error"):
                break
            if judgement and judgement.get("action") == "reply":
                pending.appendleft({**turn, "question": judgement["reply"], "dynamic": True})
            elif judgement and judgement.get("action") == "continue" and not pending:
                pending.appendleft({**turn, "question": "请继续按原始目标和已提供的信息作答，不要重复询问已有信息。", "dynamic": True})
            elif judgement and judgement.get("action") == "finish" and sample.get("adaptive", False):
                break
    result = _case_result(sample, case_id, turns, round((time.monotonic() - started) * 1000, 3))
    result.update(user_id=user_id, conversation_id=context["conversation_id"], expected_turns=len(sample.get("turns", [sample])))
    ai_journal.finish_case(prepared, result)
    return result


def _percentile(values, percentile):
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * percentile
    low = int(index)
    return round(ordered[low] + (ordered[min(low + 1, len(ordered) - 1)] - ordered[low]) * (index - low), 3)


def _summary(cases, elapsed, concurrency, intended):
    turns = [turn for case in cases for turn in case["turns"]]
    timings = [turn["timings"]["total_ms"] for turn in turns]
    quality_passed = sum(case["business_status"] == "Pass" for case in cases)
    failed = sum(case["status"] == "Fail" for case in cases)
    cancelled = sum(case["status"] == "Cancelled" for case in cases)
    blocked = sum(case["status"] == "Blocked" for case in cases)
    status = "Fail" if failed else ("Cancelled" if cancelled or len(cases) < intended else ("Blocked" if blocked else "Pass"))
    business = "Fail" if any(case["business_status"] == "Fail" for case in cases) else ("Pass" if quality_passed == intended else "Unverified")
    return {"status": status, "reason": next((case["reason"] for case in cases if case["reason"]), ""),
            "execution_status": "completed" if status == "Pass" else status.lower(), "business_status": business,
            "verification": "verified" if business != "Unverified" else "unverified", "verdict_scope": "quality_and_performance",
            "cases": cases, "metrics": {"planned_cases": intended, "completed_cases": len(cases), "quality_passed": quality_passed,
            "quality_pass_rate": quality_passed / intended, "failed_cases": failed, "blocked_cases": blocked,
            "cancelled_cases": cancelled, "sent_turns": len(turns),
            "concurrency": concurrency, "elapsed_ms": round(elapsed * 1000, 3), "throughput_turns_per_second": len(turns) / elapsed if elapsed else 0,
            "p50_ms": _percentile(timings, 0.5), "p95_ms": _percentile(timings, 0.95), "p99_ms": _percentile(timings, 0.99)}}


async def _evaluate(prepared, cancelled, emit):
    spec = prepared["spec"]
    load = spec.get("load", {})
    concurrency, iterations = load.get("concurrency", 1), load.get("iterations", 1)
    queue = asyncio.Queue()
    for iteration in range(iterations):
        for number, sample in enumerate(spec["samples"], 1):
            queue.put_nowait((sample, f"{sample.get('id', number)}:{iteration + 1}"))
    intended, cases, started = queue.qsize(), [], time.monotonic()

    async def user(number):
        delay = float(load.get("ramp_up_seconds", 0)) * number / concurrency
        if delay:
            await asyncio.sleep(delay)
        while not cancelled():
            try:
                sample, case_id = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            result = await _conversation(sample, spec, case_id, f"user-{number + 1}", cancelled, emit, prepared)
            cases.append(result)

    await asyncio.gather(*(user(number) for number in range(concurrency)))
    return {**_summary(cases, time.monotonic() - started, concurrency, intended), "spec_hash": prepared["spec_hash"]}
