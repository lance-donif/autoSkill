"""Write request intent, observations, and evaluated turns before subsequent network side effects."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .. import db, execution_inputs


def _folder(prepared):
    folder = Path(prepared["evidence"]) / "conversation-journal"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _key(case_id):
    return hashlib.sha256(case_id.encode()).hexdigest()


def turn_file(prepared, case_id, number):
    return _folder(prepared) / f"{_key(case_id)}-turn-{number}.json"


def intent(prepared, sample, case_id, number, question, endpoint, body):
    value = {"case_id": case_id, "title": sample.get("title", sample.get("id", "AI 多轮对话")),
             "turn": number, "question": question, "expected_turns": len(sample.get("turns", [sample])), "state": "intended", "created_at": db.now(),
             "request": {"method": endpoint.get("method", "POST"), "url": endpoint["url"],
                         "body_hash": hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()},
             "context": prepared.get("context", {}), "spec_hash": prepared["spec_hash"]}
    execution_inputs.write_json(turn_file(prepared, case_id, number), value)


def observe(prepared, case_id, number, observation):
    file = turn_file(prepared, case_id, number)
    value = json.loads(file.read_bytes())
    execution_inputs.write_json(file, {**value, "state": "observed", "observation": observation, "observed_at": db.now()})


def finish_turn(prepared, case_id, number, result):
    file = turn_file(prepared, case_id, number)
    value = json.loads(file.read_bytes())
    execution_inputs.write_json(file, {**value, "state": "completed", "result": result, "finished_at": db.now()})


def finish_case(prepared, result):
    execution_inputs.write_json(_folder(prepared) / f"{_key(result['id'])}-case.json", result)


def recover_cases(prepared, reason):
    folder = _folder(prepared)
    cases = {case["id"]: case for file in folder.glob("*-case.json") if (case := json.loads(file.read_bytes()))}
    pending = {}
    for file in folder.glob("*-turn-*.json"):
        item = json.loads(file.read_bytes())
        if item["case_id"] not in cases:
            pending.setdefault(item["case_id"], []).append(item)
    for case_id, entries in pending.items():
        turns = [item.get("result") or {**item.get("observation", {}), "turn": item["turn"], "question": item["question"],
                        "status": "Blocked", "business_status": "Unverified", "assertions": [], "reason": reason,
                        "timings": item.get("observation", {}).get("timings", {"total_ms": 0}), "operation_state": item["state"]}
                 for item in sorted(entries, key=lambda item: item["turn"])]
        failed = any(turn["status"] == "Fail" for turn in turns)
        business_failed = any(turn["business_status"] == "Fail" for turn in turns)
        cases[case_id] = {"id": case_id, "target_started": True, "title": entries[0]["title"], "status": "Fail" if failed else "Blocked", "reason": reason,
                          "business_status": "Fail" if business_failed else "Unverified", "verification": "verified" if business_failed else "unverified",
                          "assertions": [check for turn in turns for check in turn["assertions"]], "turns": turns,
                          "expected_turns": entries[0]["expected_turns"], "duration_ms": sum(turn["timings"]["total_ms"] for turn in turns), "incomplete": True}
    return list(cases.values())
