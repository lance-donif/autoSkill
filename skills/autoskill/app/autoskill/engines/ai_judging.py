"""Ask the active host agent to review an answer without introducing a second model configuration."""
from __future__ import annotations

import asyncio
import json
import time


def settings(spec: dict, sample: dict, turn: dict) -> dict:
    value = {**spec.get("judge", {}), **sample.get("judge", {}), **turn.get("judge", {})}
    return value if value.get("enabled", bool(value)) else {}


async def assess(prepared: dict, sample: dict, turn: dict, history: list, observation: dict,
                 case_id: str, number: int, cancelled, emit=None) -> dict | None:
    spec = prepared["spec"]
    options = settings(spec, sample, turn)
    if not options or observation.get("error"):
        return None
    from .. import dialogue_judge

    context = {**prepared.get("context", {}), "case_id": case_id, "spec_hash": prepared["spec_hash"],
               "facts": {**sample.get("context", {}), **sample.get("facts", {})}}
    context["host"] = options.get("host", context.get("host", "generic"))
    if not context.get("run_id"):
        context["run_id"] = "evaluation:" + prepared["evidence"]
    key = f"{context['run_id']}:{case_id}:{number}"
    answer = observation["answer"] if isinstance(observation["answer"], str) else json.dumps(observation["answer"], ensure_ascii=False)
    request = dialogue_judge.submit_request(context, {**sample, "id": case_id}, number,
            sample.get("goal", sample.get("turns", [sample])[0]["question"]), history, answer,
            options.get("rubric", {}), {"dataset": spec.get("dataset", []), "reference": turn.get("reference"),
                                       "expected": turn.get("expected"), "checks": turn.get("checks", [])}, idempotency_key=key)
    if emit:
        emit({"event": "awaiting_judge", "judge_id": request["judge_id"], "case_id": case_id, "turn": number})
    started = time.monotonic()
    timeout = options.get("timeout_seconds", 900)
    while True:
        if cancelled():
            return {"judge_id": request["judge_id"], "pending": True, "cancelled": True, "reason": "运行已取消"}
        result = dialogue_judge.get(request["judge_id"])
        if result.get("judgement"):
            return {"judge_id": request["judge_id"], "wait_ms": round((time.monotonic() - started) * 1000, 3), **result["judgement"]}
        if time.monotonic() - started >= timeout:
            return {"judge_id": request["judge_id"], "pending": True, "reason": "等待宿主 AI 评审超时，回答证据与待评审任务已保留"}
        await asyncio.sleep(0.1)


def merge(checks: list, business: str, judgement: dict | None) -> tuple[list, str]:
    if judgement is None:
        return checks, business
    if business == "Fail":
        return checks, business
    verdict = judgement.get("verdict")
    if judgement.get("pending") or verdict == "uncertain":
        return checks, "Fail" if business == "Fail" else "Unverified"
    if verdict == "needs_clarification":
        return checks, "Pass" if judgement.get("action") == "reply" else "Unverified"
    passed = verdict == "pass"
    row = {"label": "宿主 AI 评审回答符合对话目标", "source": "host_ai_judge", "passed": passed,
           "expected": "满足独立评审标准", "actual": judgement.get("reason", ""), "judge_id": judgement["judge_id"]}
    return [*checks, row], "Pass" if passed and business != "Fail" else "Fail"
