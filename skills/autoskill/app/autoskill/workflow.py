"""Recoverable workflow application service shared by CLI, HTTP and recorder hooks."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from . import db, domain, projects, task_context
from . import workflow_definition as definitions
from . import workflow_state as state
from .locking import project_lock
from .workflow_state import WorkflowError

_UNSET = object()
_binding_context = ContextVar("workflow_binding", default=_UNSET)


def selected_binding_token() -> str | None:
    """Expose explicit transport identity without choosing a task by its session name."""
    value = _binding_context.get()
    return (os.environ.get("AUTOSKILL_WORKFLOW_BINDING") if value is _UNSET else value) or None


@contextmanager
def use_binding(token: str | None):
    """Select one workflow attempt explicitly; the lease is checked at every mutation."""
    if token is not None and not isinstance(token, str):
        raise WorkflowError("validation_error", "工作流绑定需要是文字 token")
    marker = _binding_context.set(token or None)
    try:
        yield
    finally:
        _binding_context.reset(marker)


def _binding_token(workflow_id: str, item_id: str, attempt: dict) -> str:
    return base64.urlsafe_b64encode(state.packed({"workflow_id": workflow_id, "item_id": item_id,
                                                "attempt_id": attempt["id"], "lease_token": attempt["lease_token"]}).encode()).decode()


def _summary(flow: dict, connection) -> dict:
    counts = {r["state"]: r["n"] for r in connection.execute(
        "SELECT state,COUNT(*) n FROM workflow_items WHERE workflow_id = ? AND cycle_id = ? GROUP BY state", (flow["id"], flow["cycle_id"]))}
    if flow["lifecycle"] != "active":
        cycle_state = flow["lifecycle"]
    elif counts.get("recovering"):
        cycle_state = "recovering"
    elif counts.get("waiting_execution"):
        cycle_state = "waiting_execution"
    elif counts.get("ready") or counts.get("leased") or counts.get("publishing"):
        cycle_state = "awaiting_host"
    else:
        cycle_state = "settled"
    return {"workflow_id": flow["id"], "project_id": flow["project_id"], "title": flow["title"],
            "cycle_id": flow["cycle_id"], "lifecycle": flow["lifecycle"], "cycle_state": cycle_state,
            "complete": cycle_state == "settled", "conclusion": flow["conclusion"], "revision": flow["revision"],
            "domain_head": flow["domain_head"], "definition_hash": flow["definition_hash"], "counts": counts}


def _view(workflow_id: str) -> dict:
    with db.connect() as connection:
        return _summary(state.row(connection, "workflow_runtime", workflow_id), connection)


def status(workflow_id: str, *, limit: int = 50, cursor: str | None = None) -> dict:
    """Read a bounded keyset page; polling does not claim or execute work."""
    state.init()
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise WorkflowError("validation_error", "分页 limit 必须是正整数")
    after, expected_revision = 0, None
    if cursor:
        try:
            value = json.loads(base64.urlsafe_b64decode(cursor))
            if value["workflow_id"] != workflow_id or not isinstance(value["after"], int):
                raise ValueError
            after = value["after"]
            expected_revision = value["revision"]
        except (ValueError, TypeError, KeyError):
            raise WorkflowError("cursor_expired", "分页游标无效或属于其他工作流") from None
    with db.connect() as connection:
        flow = state.row(connection, "workflow_runtime", workflow_id)
        if expected_revision is not None and flow["revision"] != expected_revision:
            raise WorkflowError("cursor_expired", "工作流状态已更新，请从第一页读取", revision=flow["revision"])
        records = [dict(r) for r in connection.execute(
            "SELECT id,sequence,cycle_id,phase,state,attempt_id,run_id,created_at,finished_at FROM workflow_items "
            "WHERE workflow_id = ? AND sequence > ? ORDER BY sequence LIMIT ?", (workflow_id, after, limit + 1))]
        has_more = len(records) > limit
        records = records[:limit]
        next_cursor = base64.urlsafe_b64encode(state.packed({"workflow_id": workflow_id, "after": records[-1]["sequence"], "revision": flow["revision"]}).encode()).decode() if has_more else None
        total = connection.execute("SELECT COUNT(*) FROM workflow_items WHERE workflow_id = ?", (workflow_id,)).fetchone()[0]
        return {**_summary(flow, connection), "items": records, "total": total, "cursor": cursor, "next_cursor": next_cursor}


def read(workflow_id: str, *, kind: str = "definition", offset: int = 0, limit: int = 50, head: str | None = None,
         text_offset: int | None = None, text_length: int = 12000) -> dict:
    """Read versioned domain documents without exposing local receipts as definitions."""
    flow = state.workflow(workflow_id)
    if offset < 0 or limit < 1 or text_length < 1 or (text_offset is not None and text_offset < 0):
        raise WorkflowError("validation_error", "offset 不能为负数，limit 必须大于零")
    current = definitions.snapshot(flow, head)
    document = current["documents"].get(kind + ".json")
    if document is None:
        raise WorkflowError("not_found", "该阶段还没有已发布的定义", kind=kind)
    common = {"domain_head": current["head"], "head": current["head"], "definition_hash": current["hash"]}
    encoded = state.packed(document)
    if text_offset is not None or len(encoded) > text_length:
        start = text_offset or 0
        end = start + text_length
        return {**common, "text": encoded[start:end], "text_offset": start, "next_text_offset": end if end < len(encoded) else None,
                "total_characters": len(encoded), "truncated": end < len(encoded), "text_ref": {"workflow_id": workflow_id, "kind": kind, "head": current["head"]}}
    if isinstance(document, list):
        return {**common, "items": document[offset:offset + limit], "total": len(document), "next_offset": offset + limit if offset + limit < len(document) else None}
    return {**common, "document": document}


def _journal(connection, scope: str, key: str, journal: dict) -> None:
    connection.execute("UPDATE workflow_requests SET journal = ? WHERE scope = ? AND request_key = ?", (state.packed(journal), scope, key))


def _following(phase: str, documents: dict) -> str | None:
    if phase in ("sources", "requirements", "design", "completion", "materialize", "maintain"):
        return {"sources": "requirements", "requirements": "design", "design": "coverage", "completion": "coverage",
                "materialize": "execute", "maintain": "execute"}[phase]
    if phase == "diagnose":
        return {"repair": "maintain", "retry": "execute", "business_defect": "knowledge", "incomplete": "knowledge"}[documents["diagnosis.json"]["disposition"]]
    return None


def _finish_publication(scope: str, key: str, journal: dict) -> dict:
    flow = state.workflow(journal["workflow_id"])
    published = domain.publish(flow["project_id"], journal["stage_id"], allow_disjoint=True)
    current = definitions.snapshot(flow)
    with db.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        found = connection.execute("SELECT * FROM workflow_requests WHERE scope = ? AND request_key = ?", (scope, key)).fetchone()
        if found["state"] == "completed":
            return json.loads(found["response"])
        connection.execute("UPDATE workflow_runtime SET domain_head = ?, definition_hash = ?, lifecycle = 'active' WHERE id = ?",
                           (published["head"], current["hash"], flow["id"]))
        if journal.get("new_cycle_id"):
            _replace_cycle(connection, flow, journal["new_cycle_id"])
        if journal.get("item_id"):
            task = state.row(connection, "workflow_items", journal["item_id"])
            state.complete(connection, task)
            following = _following(task["phase"], current["documents"])
        else:
            following = journal["first_phase"]
        if following:
            state.item(connection, flow["id"], following)
        state.touch(connection, flow["id"])
        summary = _summary(state.row(connection, "workflow_runtime", flow["id"]), connection)
        result = {**summary, "accepted": True, "change_id": journal["change_id"], "workflow": summary}
        return state.receipt(connection, scope, key, result)


def _request_spec(spec: dict) -> dict:
    if not isinstance(spec, dict):
        raise WorkflowError("validation_error", "工作流定义必须是 JSON 对象")
    canonical = copy.deepcopy(spec)
    for field in ("sources", "artifacts"):
        for source in canonical.get(field, []):
            if isinstance(source, dict) and "file" in source:
                file = Path(source.pop("file"))
                source["file_content_sha256"] = hashlib.sha256(file.read_bytes()).hexdigest()
    return canonical


def create(project_id: str, spec: dict, *, idempotency_key: str) -> dict:
    """Stage the definition and journal its publication before changing domain HEAD."""
    state.init()
    scope = "workflow-create:" + project_id
    canonical = _request_spec(spec)
    with project_lock(project_id):
        projects.path(project_id)
        with db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            found = state.request(connection, scope, idempotency_key, "create", canonical)
            if replayed := state.replay(found):
                return replayed
            journal = json.loads(found["journal"]) if found else {}
            if not journal:
                workflow_id, change_id = state.identifier("wf"), state.identifier("change")
                changes, first_phase = definitions.initial(workflow_id, spec, change_id)
                staged = domain.stage(project_id, changes, domain.manifest(project_id)["head"], reason="workflow.create")
                definition = changes[definitions.prefix(workflow_id) + "definition.json"]
                connection.execute(
                    "INSERT INTO workflow_runtime(id,project_id,title,cycle_id,lifecycle,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (workflow_id, project_id, definition["title"], state.identifier("cycle"), "initializing", db.now(), db.now()))
                journal = {"workflow_id": workflow_id, "stage_id": staged["stage_id"], "change_id": change_id, "first_phase": first_phase}
                _journal(connection, scope, idempotency_key, journal)
        return _finish_publication(scope, idempotency_key, journal)


def _replace_cycle(connection, flow: dict, cycle_id: str) -> None:
    for record in connection.execute(
        "SELECT * FROM workflow_items WHERE workflow_id = ? AND state IN ('ready','leased')", (flow["id"],)).fetchall():
        task = dict(record)
        if state.unresolved(connection, task["id"]):
            connection.execute("UPDATE workflow_items SET state = 'recovering' WHERE id = ?", (task["id"],))
            if task["attempt_id"]:
                connection.execute("UPDATE workflow_attempts SET state = 'obsolete', finished_at = ? WHERE id = ?", (db.now(), task["attempt_id"]))
        else:
            state.complete(connection, task, state="obsolete")
    connection.execute("UPDATE runs SET cancel = 1 WHERE status IN ('Queued','Running') AND id IN "
                       "(SELECT run_id FROM workflow_items WHERE workflow_id = ?)", (flow["id"],))
    connection.execute("UPDATE workflow_runtime SET cycle_id = ?, conclusion = 'unassessed' WHERE id = ?", (cycle_id, flow["id"]))


def revise(workflow_id: str, spec: dict, *, idempotency_key: str, expected_hash: str | None = None) -> dict:
    """Publish a new source generation and cycle; old failures remain attached to their old inputs."""
    flow = state.workflow(workflow_id)
    if not isinstance(spec, dict) or not spec.get("sources"):
        raise WorkflowError("validation_error", "来源修订需要非空 sources；使用新来源重新分析，不修改旧失败的预期")
    canonical = {"spec": _request_spec(spec), "expected_hash": expected_hash}
    with project_lock(flow["project_id"]):
        _recover_publications(flow)
        with db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            found = state.request(connection, workflow_id, idempotency_key, "revise", canonical)
            if replayed := state.replay(found):
                return replayed
            journal = json.loads(found["journal"]) if found else {}
            if not journal:
                current = definitions.snapshot(flow)
                if expected_hash is not None and expected_hash != current["hash"]:
                    raise WorkflowError("revision_conflict", "来源修订的基础定义已变化", actual_hash=current["hash"])
                previous = current["documents"]["definition.json"]
                change_id = state.identifier("change")
                changes, phase = definitions.initial(workflow_id, {**previous, **spec}, change_id)
                definition = changes[definitions.prefix(workflow_id) + "definition.json"]
                definition.update(version=previous["version"] + 1, parent_definition_hash=current["hash"], origin="source_revision")
                obsolete = [definitions.prefix(workflow_id) + key for key in ("requirements.json", "scenarios.json", "oracles.json", "materialization.json", "diagnosis.json")
                            if key in current["documents"]]
                staged = domain.stage(flow["project_id"], changes, current["head"], reason="workflow.revise", remove=obsolete)
                journal = {"workflow_id": workflow_id, "stage_id": staged["stage_id"], "change_id": change_id,
                           "first_phase": phase, "new_cycle_id": state.identifier("cycle")}
                _journal(connection, workflow_id, idempotency_key, journal)
                connection.execute("UPDATE workflow_runtime SET lifecycle = 'revising' WHERE id = ?", (workflow_id,))
        return _finish_publication(workflow_id, idempotency_key, journal)


def _recover_publications(flow: dict) -> None:
    rows = db.rows("SELECT * FROM workflow_requests WHERE state = 'pending' AND action IN ('create','submit','revise')")
    for found in rows:
        journal = json.loads(found["journal"])
        if journal.get("workflow_id") == flow["id"] and journal.get("stage_id"):
            _finish_publication(found["scope"], found["request_key"], journal)


def _expire(flow: dict) -> None:
    with db.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        expired = connection.execute(
            "SELECT i.* FROM workflow_items i JOIN workflow_attempts a ON a.id = i.attempt_id "
            "WHERE i.workflow_id = ? AND i.state = 'leased' AND a.lease_until <= ?", (flow["id"], db.now())).fetchall()
        for record in expired:
            task = dict(record)
            target = "recovering" if state.unresolved(connection, task["id"]) else "ready"
            connection.execute("UPDATE workflow_items SET state = ? WHERE id = ?", (target, task["id"]))
            connection.execute("UPDATE workflow_attempts SET state = 'expired', finished_at = ? WHERE id = ?", (db.now(), task["attempt_id"]))
            state.touch(connection, flow["id"])


def _advance(flow: dict) -> None:
    from . import workflow_execution

    _recover_publications(flow)
    _expire(flow)
    flow = state.workflow(flow["id"])
    if flow["lifecycle"] != "active":
        return
    workflow_execution.advance(flow)


def _task_view(flow: dict, task: dict, attempt: dict, current: dict) -> dict:
    return {"id": task["id"], "item_id": task["id"], "workflow_id": flow["id"], "phase": task["phase"],
            "attempt_id": attempt["id"], "lease_token": attempt["lease_token"], "lease_until": attempt["lease_until"],
            "binding_token": _binding_token(flow["id"], task["id"], attempt),
            "input_hash": current["hash"], "input_refs": {key.removesuffix(".json"): definitions.prefix(flow["id"]) + key for key in current["documents"]},
            "checkpoint": json.loads(attempt["checkpoint"]), "context": json.loads(task["context"]),
            "payload_schema": definitions.instructions(task["phase"])}


def next_task(workflow_id: str, *, idempotency_key: str, owner: str | None = None, lease_seconds: int = 300) -> dict:
    """Reconcile deterministic work and atomically lease the next host operation."""
    flow = state.workflow(workflow_id)
    until = state.deadline(lease_seconds)
    identity = owner or task_context.session_id() or "local"
    payload = {"owner": identity, "lease_seconds": lease_seconds}
    with project_lock(flow["project_id"]):
        with db.connect() as connection:
            found = connection.execute("SELECT * FROM workflow_requests WHERE scope = ? AND request_key = ?", (workflow_id, idempotency_key)).fetchone()
            if found:
                checked = state.request(connection, workflow_id, idempotency_key, "next", payload)
                if replayed := state.replay(checked):
                    return replayed
        _advance(flow)
        current = definitions.snapshot(flow)
        with db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            found = state.request(connection, workflow_id, idempotency_key, "next", payload)
            if replayed := state.replay(found):
                return replayed
            fresh = state.row(connection, "workflow_runtime", workflow_id)
            task = connection.execute("SELECT * FROM workflow_items WHERE workflow_id = ? AND cycle_id = ? AND state = 'ready' ORDER BY sequence LIMIT 1",
                                      (workflow_id, fresh["cycle_id"])).fetchone()
            result = {"task": None, "workflow": _summary(fresh, connection)}
            if task and fresh["lifecycle"] == "active" and task["phase"] in definitions.HOST_PHASES:
                task = dict(task)
                generation = connection.execute("SELECT COUNT(*) + 1 FROM workflow_attempts WHERE item_id = ?", (task["id"],)).fetchone()[0]
                attempt = {"id": state.identifier("wfa"), "lease_token": state.identifier("lease"), "lease_until": until, "checkpoint": "{}"}
                if task["attempt_id"]:
                    attempt["checkpoint"] = state.row(connection, "workflow_attempts", task["attempt_id"])["checkpoint"]
                connection.execute(
                    "INSERT INTO workflow_attempts(id,item_id,workflow_id,generation,owner,session_id,lease_token,lease_until,state,checkpoint,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (attempt["id"], task["id"], workflow_id, generation, identity, task_context.session_id() or "",
                                                     attempt["lease_token"], until, "leased", attempt["checkpoint"], db.now()))
                connection.execute("UPDATE workflow_items SET state = 'leased', attempt_id = ?, input_hash = ? WHERE id = ?",
                                   (attempt["id"], current["hash"], task["id"]))
                state.touch(connection, workflow_id)
                result = {"task": _task_view(flow, task, attempt, current), "workflow": _summary(state.row(connection, "workflow_runtime", workflow_id), connection)}
            return state.receipt(connection, workflow_id, idempotency_key, result)


def submit(workflow_id: str, item_id: str, *, lease_token: str, attempt_id: str, idempotency_key: str, payload: dict) -> dict:
    """Validate a proposal, fence its lease, and publish a staged domain change."""
    flow = state.workflow(workflow_id)
    envelope = {"item_id": item_id, "lease_token": lease_token, "attempt_id": attempt_id, "payload": payload}
    with project_lock(flow["project_id"]):
        with db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            found = state.request(connection, workflow_id, idempotency_key, "submit", envelope)
            if replayed := state.replay(found):
                return replayed
            journal = json.loads(found["journal"]) if found else {}
            if not journal:
                task, _attempt = state.assert_lease(connection, workflow_id, item_id, attempt_id, lease_token)
                if task["phase"] == "investigate":
                    from . import workflow_recovery

                    result = workflow_recovery.finish(connection, task, payload)
                    result["workflow"] = _summary(state.row(connection, "workflow_runtime", workflow_id), connection)
                    return state.receipt(connection, workflow_id, idempotency_key, result)
                if state.unresolved(connection, item_id):
                    raise WorkflowError("uncertain_operation", "存在未确认的外部动作，请先读取真实状态并恢复操作收据", item_id=item_id)
                current = definitions.snapshot(flow)
                if current["hash"] != task["input_hash"]:
                    raise WorkflowError("revision_conflict", "领取后的输入定义已经变化，请重新领取；旧提案未发布")
                change_id = state.identifier("change")
                changes = definitions.proposal(flow, task["phase"], payload, change_id, current)
                staged = domain.stage(flow["project_id"], changes, current["head"], reason="workflow." + task["phase"], metadata={"workflow_id": workflow_id})
                journal = {"workflow_id": workflow_id, "item_id": item_id, "stage_id": staged["stage_id"], "change_id": change_id}
                _journal(connection, workflow_id, idempotency_key, journal)
                connection.execute("UPDATE workflow_items SET state = 'publishing' WHERE id = ?", (item_id,))
        return _finish_publication(workflow_id, idempotency_key, journal)


def _control(workflow_id: str, action: str, idempotency_key: str) -> dict:
    flow = state.workflow(workflow_id)
    with project_lock(flow["project_id"]):
        _recover_publications(flow)
        with db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            found = state.request(connection, workflow_id, idempotency_key, action, {})
            if replayed := state.replay(found):
                return replayed
            if action == "cancel":
                connection.execute("UPDATE workflow_runtime SET lifecycle = 'cancelled' WHERE id = ?", (workflow_id,))
                for task in connection.execute("SELECT * FROM workflow_items WHERE workflow_id = ? AND state = 'leased'", (workflow_id,)).fetchall():
                    target = "recovering" if state.unresolved(connection, task["id"]) else "ready"
                    connection.execute("UPDATE workflow_items SET state = ? WHERE id = ?", (target, task["id"]))
                    connection.execute("UPDATE workflow_attempts SET state = 'cancelled', finished_at = ? WHERE id = ?", (db.now(), task["attempt_id"]))
                connection.execute("UPDATE runs SET cancel = 1 WHERE id IN (SELECT run_id FROM workflow_items WHERE workflow_id = ?)", (workflow_id,))
            elif action == "resume":
                connection.execute("UPDATE workflow_runtime SET lifecycle = 'active' WHERE id = ?", (workflow_id,))
            state.touch(connection, workflow_id)
            response = {"workflow": _summary(state.row(connection, "workflow_runtime", workflow_id), connection)}
            state.receipt(connection, workflow_id, idempotency_key, response)
        if action != "cancel":
            _advance(flow)
        return response


def cancel(workflow_id: str, *, idempotency_key: str) -> dict:
    return _control(workflow_id, "cancel", idempotency_key)


def resume(workflow_id: str, *, idempotency_key: str) -> dict:
    return _control(workflow_id, "resume", idempotency_key)


def recover(workflow_id: str, *, idempotency_key: str) -> dict:
    flow = state.workflow(workflow_id)
    with project_lock(flow["project_id"]):
        with db.connect() as connection:
            found = connection.execute("SELECT * FROM workflow_requests WHERE scope = ? AND request_key = ?", (workflow_id, idempotency_key)).fetchone()
            if found:
                checked = state.request(connection, workflow_id, idempotency_key, "recover", {})
                if replayed := state.replay(checked):
                    return replayed
        _advance(flow)
        with db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state.request(connection, workflow_id, idempotency_key, "recover", {})
            uncertain = [dict(r) for r in connection.execute(
                "SELECT id,item_id,attempt_id,kind,state,payload FROM workflow_operations "
                "WHERE workflow_id = ? AND side_effect = 1 AND state IN ('intended','unknown')", (workflow_id,))]
            return state.receipt(connection, workflow_id, idempotency_key, {"workflow": _summary(state.row(connection, "workflow_runtime", workflow_id), connection),
                                                                         "uncertain_operations": uncertain})


def _lease_change(workflow_id, item_id, lease_token, attempt_id, idempotency_key, action, seconds, checkpoint):
    flow = state.workflow(workflow_id)
    payload = {"item_id": item_id, "lease_token": lease_token, "attempt_id": attempt_id, "lease_seconds": seconds, "checkpoint": checkpoint}
    until = state.deadline(seconds) if action == "heartbeat" else None
    with project_lock(flow["project_id"]), db.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        found = state.request(connection, workflow_id, idempotency_key, action, payload)
        if replayed := state.replay(found):
            return replayed
        state.assert_lease(connection, workflow_id, item_id, attempt_id, lease_token)
        if checkpoint is not None:
            connection.execute("UPDATE workflow_attempts SET checkpoint = ? WHERE id = ?", (state.packed(checkpoint), attempt_id))
        if action == "heartbeat":
            connection.execute("UPDATE workflow_attempts SET lease_until = ? WHERE id = ?", (until, attempt_id))
        else:
            target = "recovering" if state.unresolved(connection, item_id) else "ready"
            connection.execute("UPDATE workflow_items SET state = ? WHERE id = ?", (target, item_id))
            connection.execute("UPDATE workflow_attempts SET state = 'released', finished_at = ? WHERE id = ?", (db.now(), attempt_id))
        state.touch(connection, workflow_id)
        return state.receipt(connection, workflow_id, idempotency_key,
                             {"item_id": item_id, "attempt_id": attempt_id, "lease_until": until,
                              "workflow": _summary(state.row(connection, "workflow_runtime", workflow_id), connection)})


def heartbeat(workflow_id: str, item_id: str, *, lease_token: str, attempt_id: str, idempotency_key: str,
              lease_seconds: int = 300, checkpoint: dict | None = None) -> dict:
    return _lease_change(workflow_id, item_id, lease_token, attempt_id, idempotency_key, "heartbeat", lease_seconds, checkpoint)


def release(workflow_id: str, item_id: str, *, lease_token: str, attempt_id: str, idempotency_key: str, checkpoint: dict | None = None) -> dict:
    return _lease_change(workflow_id, item_id, lease_token, attempt_id, idempotency_key, "release", None, checkpoint)


def current_binding() -> dict | None:
    state.init()
    token = selected_binding_token()
    with db.connect() as connection:
        if token:
            try:
                binding = json.loads(base64.urlsafe_b64decode(token))
                identities = [binding[key] for key in ("workflow_id", "item_id", "attempt_id", "lease_token")]
            except (KeyError, TypeError, ValueError):
                raise WorkflowError("validation_error", "工作流 binding_token 格式无效") from None
            task, _attempt = state.assert_lease(connection, *identities)
            return {**binding, "phase": task["phase"]}
        found = connection.execute(
            "SELECT a.*,i.phase FROM workflow_attempts a JOIN workflow_items i ON i.attempt_id = a.id "
            "JOIN workflow_runtime w ON w.id = a.workflow_id WHERE a.session_id = ? AND a.state = 'leased' "
            "AND i.state = 'leased' AND w.lifecycle = 'active' AND a.lease_until > ? ORDER BY a.created_at DESC LIMIT 2",
            (task_context.session_id() or "", db.now())).fetchall()
        if not found:
            return None
        if len(found) > 1:
            raise WorkflowError("ambiguous_binding", "当前会话领取了多个任务，请使用 next 返回的 binding_token 指明本次操作归属")
        found = found[0]
        return {"workflow_id": found["workflow_id"], "item_id": found["item_id"], "attempt_id": found["id"], "lease_token": found["lease_token"], "phase": found["phase"]}


def operation_intent(kind: str, payload: dict, *, binding: dict | None = None, side_effect: bool = True,
                     idempotency_key: str | None = None) -> dict | None:
    """Recorder hook: commit intent before touching the target system."""
    binding = binding or current_binding()
    if not binding:
        return None
    if not side_effect:
        from . import workflow_recovery

        binding = workflow_recovery.probe_binding(binding) or binding
    flow = state.workflow(binding["workflow_id"])
    key = idempotency_key or state.identifier("operation-request")
    fingerprint = state.digest({"kind": kind, "payload": payload, "side_effect": side_effect})
    with project_lock(flow["project_id"]), db.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        state.assert_lease(connection, binding["workflow_id"], binding["item_id"], binding["attempt_id"], binding["lease_token"])
        previous = connection.execute("SELECT * FROM workflow_operations WHERE attempt_id = ? AND request_key = ?", (binding["attempt_id"], key)).fetchone()
        if previous:
            if previous["payload_hash"] != fingerprint:
                raise WorkflowError("idempotency_conflict", "操作幂等键已用于不同动作")
            return {"operation_id": previous["id"], "receipt_token": previous["receipt_token"], "state": previous["state"], "duplicate": True}
        identity, token = state.identifier("wfo"), state.identifier("receipt")
        connection.execute(
            "INSERT INTO workflow_operations(id,workflow_id,item_id,attempt_id,request_key,payload_hash,kind,payload,side_effect,state,receipt_token,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (identity, flow["id"], binding["item_id"], binding["attempt_id"], key, fingerprint,
                                               kind, state.packed(payload), int(side_effect), "intended", token, db.now()))
        return {"operation_id": identity, "receipt_token": token, "state": "intended", "duplicate": False}


def operation_receipt(intent: dict | None, observation: dict, *, status: str = "succeeded") -> dict | None:
    """Append the actual outcome even if its old lease expired; never mutate a new attempt."""
    if intent is None:
        return None
    if status not in ("succeeded", "failed", "unknown"):
        raise WorkflowError("validation_error", "操作收据状态必须为 succeeded/failed/unknown")
    state.init()
    with db.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        operation = state.row(connection, "workflow_operations", intent["operation_id"])
        if operation["receipt_token"] != intent.get("receipt_token"):
            raise WorkflowError("stale_lease", "操作收据不属于这次操作")
        if operation["state"] in ("succeeded", "failed"):
            if operation["state"] != status or operation["observation"] != state.packed(observation):
                raise WorkflowError("idempotency_conflict", "已有操作收据不能被另一结果覆盖")
        else:
            connection.execute("UPDATE workflow_operations SET state = ?, observation = ?, finished_at = ? WHERE id = ?",
                               (status, state.packed(observation), db.now(), operation["id"]))
        return {"operation_id": operation["id"], "state": status, "observation": observation}
