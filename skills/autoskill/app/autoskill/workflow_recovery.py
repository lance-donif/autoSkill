"""Resolve interrupted work from kernel observations, never a host's success claim."""
from __future__ import annotations

import json

from . import db
from . import workflow_state as state


def probe_binding(binding: dict) -> dict | None:
    """An investigation may read an old recording without inheriting its expired write lease."""
    from .workflow import current_binding

    current = current_binding()
    if not current or current["workflow_id"] != binding["workflow_id"]:
        return None
    with db.connect() as connection:
        task = state.row(connection, "workflow_items", current["item_id"])
        if task["phase"] == "investigate" and json.loads(task["context"]).get("recover_item_id") == binding["item_id"]:
            return current
    return None


def _observations(connection, task: dict, payload: dict) -> list[dict]:
    identities = payload.get("observation_operation_ids")
    if not isinstance(identities, list) or not identities:
        raise state.WorkflowError("evidence_unavailable", "恢复调查需要实际 check 的操作收据，不能只提交文字结论")
    observations = []
    for identity in identities:
        operation = state.row(connection, "workflow_operations", identity)
        if (operation["attempt_id"] != task["attempt_id"] or operation["kind"] != "check" or operation["side_effect"]
                or operation["state"] not in ("succeeded", "failed") or not operation["observation"]):
            raise state.WorkflowError("evidence_unavailable", "调查引用不是本次领取产生的真实只读检查收据", operation_id=identity)
        observations.append({"operation_id": identity, **json.loads(operation["observation"])})
    return observations


def finish(connection, task: dict, payload: dict) -> dict:
    """Keep unknown side effects explicit or resume only after matching a declared postcondition."""
    original = state.row(connection, "workflow_items", json.loads(task["context"])["recover_item_id"])
    observations = _observations(connection, task, payload)
    resolution = payload.get("resolution", "incomplete")
    if resolution not in ("observed", "incomplete"):
        raise state.WorkflowError("validation_error", "恢复 resolution 为 observed 或 incomplete")
    pending = state.unresolved(connection, original["id"])
    if resolution == "observed":
        _resolve_observed(connection, pending, observations)
    state.complete(connection, task)
    context = {**json.loads(original["context"]), "recovery": {"resolution": resolution, "observations": observations,
               "instruction": "已观察目标状态；从检查点继续，不得重播已发出的动作。"}}
    connection.execute("UPDATE workflow_items SET context = ? WHERE id = ?", (state.packed(context), original["id"]))
    flow = state.row(connection, "workflow_runtime", task["workflow_id"])
    if original["cycle_id"] != flow["cycle_id"]:
        state.complete(connection, original, state="obsolete" if resolution == "observed" else "interrupted")
        return {"accepted": True, "resolution": resolution, "observations": observations, "historical": True}
    if resolution == "observed" and original["phase"] != "execute":
        connection.execute("UPDATE workflow_items SET state = 'ready' WHERE id = ?", (original["id"],))
    else:
        state.complete(connection, original, state="interrupted")
        state.item(connection, task["workflow_id"], "knowledge", context={"recovery": context["recovery"], "unresolved_operation_ids": [op["id"] for op in pending]})
        connection.execute("UPDATE workflow_runtime SET conclusion = CASE WHEN conclusion IN ('violated','mixed') THEN conclusion ELSE 'incomplete' END WHERE id = ?",
                           (task["workflow_id"],))
    return {"accepted": True, "resolution": resolution, "observations": observations}


def _resolve_observed(connection, pending: list[dict], observations: list[dict]) -> None:
    verified = []
    for operation in pending:
        intent = json.loads(operation["payload"])
        predicate = intent.get("postcondition") or intent.get("step", {}).get("postcondition")
        probe = next((record for record in observations if predicate and record.get("check") == predicate and record.get("passed") is True), None)
        if probe is None:
            raise state.WorkflowError("evidence_unavailable", "缺少与原动作声明的 postcondition 相同且成立的真实检查；不能推断动作未执行", operation_id=operation["id"])
        verified.append((operation, probe))
    for operation, probe in verified:
        connection.execute("UPDATE workflow_operations SET state = 'succeeded', observation = ?, finished_at = ? WHERE id = ?",
                           (state.packed({"recovered_by": probe["operation_id"], "type": "postcondition_observation", "observation": probe}), db.now(), operation["id"]))
