"""Explain requirement changes through stored traceability and freeze scoped maintenance work."""
from __future__ import annotations

import copy
import json

from . import db, projects, workflow, workflow_definition, workflow_execution, workflow_state
from .locking import project_lock
from .workflow_state import WorkflowError, digest


def overview(workflow_id: str) -> dict:
    flow = workflow_state.workflow(workflow_id)
    current = workflow_definition.snapshot(flow)
    rows = db.rows("SELECT id,cycle_id,phase,state,run_id,context,created_at,finished_at FROM workflow_items WHERE workflow_id = ? ORDER BY sequence", (workflow_id,))
    for row in rows:
        context = json.loads(row["context"])
        summary = context.get("summary", {})
        if "materialization" in context and any("outcome" not in case for case in summary.get("results", [])):
            context["summary"] = workflow_execution._refresh_summary(context)
        row["context"] = context
    delivery = db.rows("SELECT id,state,reason,created_at,updated_at FROM host_outbox WHERE json_extract(payload, '$.workflow_id') = ? ORDER BY created_at DESC", (workflow_id,))
    return {"workflow": workflow.status(workflow_id), "documents": current["documents"], "definition_hash": current["hash"], "head": current["head"],
            "coverage": workflow_definition.coverage(current["documents"]), "items": rows, "deliveries": delivery,
            "traceability": traceability(flow["project_id"], current["documents"])}


def traceability(project_id: str, documents: dict) -> list[dict]:
    requirements = {item["id"]: item for item in documents.get("requirements.json", [])}
    scenarios = {item["id"]: item for item in documents.get("scenarios.json", [])}
    oracles = {item["id"]: item for item in documents.get("oracles.json", [])}
    bindings = documents.get("materialization.json", {}).get("bindings", {})
    rows = []
    for case in projects.cases(project_id):
        attached = [item for identity, item in scenarios.items() if case["id"] in bindings.get(identity, [])]
        identities = set(case.get("requirement_ids", [])) & requirements.keys()
        identities.update(identity for item in attached for identity in item.get("requirement_ids", []))
        identities.update(identity for item in attached for oracle_id in item.get("oracle_ids", [])
                          for identity in oracles.get(oracle_id, {}).get("requirement_ids", []) if identity in requirements)
        rows.append({"case_id": case["id"], "title": case["title"], "kind": case["kind"], "case_hash": digest(case),
                     "requirement_ids": sorted(identities), "scenario_ids": [item["id"] for item in attached],
                     "oracle_ids": sorted({identity for item in attached for identity in item.get("oracle_ids", [])}),
                     "source_ids": sorted({identity for requirement in identities for identity in requirements[requirement].get("source_ids", [])}),
                     "depends_on": projects.depends_on(case), "mapped": bool(identities)})
    return rows


def _selected(values, known: set, label: str) -> set:
    if values is None:
        return set()
    if not isinstance(values, list) or not all(isinstance(value, str) and value in known for value in values):
        raise WorkflowError("validation_error", label + "需要引用现有记录", known=sorted(known))
    return set(values)


def analyze(workflow_id: str, spec: dict) -> dict:
    if not isinstance(spec, dict):
        raise ValueError("影响分析参数必须是对象")
    flow = workflow_state.workflow(workflow_id)
    with project_lock(flow["project_id"]):
        current = workflow_definition.snapshot(flow)
        documents = current["documents"]
        sources = {item["id"]: item for item in documents.get("sources.json", [])}
        requirements = {item["id"]: item for item in documents.get("requirements.json", [])}
        changed_sources = _selected(spec.get("source_ids"), set(sources), "变更来源")
        new_sources = spec.get("sources", [])
        if not isinstance(new_sources, list):
            raise ValueError("新需求来源必须是列表")
        for item in new_sources:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not isinstance(item.get("text"), str) or not item["text"].strip():
                raise ValueError("每份新需求需要 id 和非空正文")
            if item["id"] not in sources or item["text"].replace("\r\n", "\n") != sources[item["id"]].get("text"):
                changed_sources.add(item["id"])
        changed_requirements = _selected(spec.get("requirement_ids"), set(requirements), "变更需求")
        changed_requirements.update(identity for identity, item in requirements.items() if changed_sources.intersection(item.get("source_ids", [])))
        if not changed_sources and not changed_requirements:
            raise ValueError("请选择变更的需求，或提供与原版本不同的新需求正文")
        traces = traceability(flow["project_id"], documents)
        direct = {row["case_id"] for row in traces if changed_requirements.intersection(row["requirement_ids"])}
        impacted = set(direct)
        while True:
            added = {row["case_id"] for row in traces if impacted.intersection(row["depends_on"])} - impacted
            if not added:
                break
            impacted.update(added)
        rows = []
        for row in traces:
            category = "direct" if row["case_id"] in direct else "dependency" if row["case_id"] in impacted else "unmapped" if not row["mapped"] else "unchanged"
            reasons = []
            if category == "direct":
                reasons = ["需求关联：" + identity for identity in sorted(changed_requirements.intersection(row["requirement_ids"]))]
            elif category == "dependency":
                reasons = ["依赖受影响用例：" + identity for identity in row["depends_on"] if identity in impacted]
            elif category == "unmapped":
                reasons = ["未找到本工作流内的有效需求关联，需要人工排查"]
            rows.append({**row, "impact": category, "reasons": reasons})
        result = {"workflow_id": workflow_id, "project_id": flow["project_id"], "head": current["head"], "definition_hash": current["hash"],
                  "changed_source_ids": sorted(changed_sources), "changed_requirement_ids": sorted(changed_requirements), "cases": rows,
                  "direct_case_ids": sorted(direct), "dependency_case_ids": sorted(impacted - direct),
                  "unmapped_case_ids": [row["case_id"] for row in rows if row["impact"] == "unmapped"],
                  "unaffected_case_ids": [row["case_id"] for row in rows if row["impact"] == "unchanged"],
                  "limitation": "影响集合来自已保存的关联和依赖；缺失关联不能证明用例不受影响。新需求新增行为还需要补充设计。"}
        result["analysis_hash"] = digest(result)
        return result


def maintain(workflow_id: str, spec: dict, *, idempotency_key: str) -> dict:
    flow = workflow_state.workflow(workflow_id)
    request_hash = digest(spec)
    receipt = db.one("SELECT response FROM workflow_requests WHERE scope = ? AND request_key = ? AND state = 'completed'",
                     ("workflow-create:" + flow["project_id"], idempotency_key))
    if receipt:
        previous = json.loads(receipt["response"])
        old = workflow_definition.snapshot(workflow_state.workflow(previous["workflow_id"]))["documents"]["definition.json"]
        scope = old.get("extensions", {}).get("change_scope", {})
        if scope.get("request_hash") != request_hash:
            raise WorkflowError("idempotency_conflict", "同一维护请求标识不能用于不同内容")
        return {**previous, "impact": scope["analysis"], "selected_case_ids": scope["case_ids"]}
    analysis = analyze(workflow_id, spec)
    if spec.get("analysis_hash") != analysis["analysis_hash"]:
        raise WorkflowError("revision_conflict", "影响分析已变化，请重新分析后再创建维护任务")
    selected = _selected(spec.get("case_ids"), {row["case_id"] for row in analysis["cases"]}, "维护用例")
    if not selected:
        raise ValueError("请选择至少一条需要维护的用例")
    sources = spec.get("sources")
    if not sources:
        raise ValueError("维护工作流需要新的需求正文，不能用旧执行结果生成预期")
    with project_lock(analysis["project_id"]):
        if analyze(workflow_id, spec)["analysis_hash"] != analysis["analysis_hash"]:
            raise WorkflowError("revision_conflict", "创建维护任务时需求或用例已变化，请重新分析")
        candidates = {case["id"]: case for case in projects.cases(analysis["project_id"])}
        scope = {"baseline_workflow_id": workflow_id, "baseline_head": analysis["head"], "analysis_hash": analysis["analysis_hash"],
                 "request_hash": request_hash,
                 "case_ids": sorted(selected), "case_hashes": {identity: digest(candidates[identity]) for identity in selected},
                 "analysis": analysis, "publication": "only_after_satisfied_regression"}
        definition = {"title": spec.get("title") or "需求维护：" + workflow_state.workflow(workflow_id)["title"],
                      "goal": "按新需求维护所选用例；只更新已选用例，保留旧预期和失败证据。真实回归全部满足后发布候选。",
                      "sources": copy.deepcopy(sources), "validation": spec.get("validation", {"engine": "playwright", "repetitions": 1}),
                      "extensions": {"change_scope": scope}}
        made = workflow.create(analysis["project_id"], definition, idempotency_key=idempotency_key)
        return {**made, "impact": analysis, "selected_case_ids": sorted(selected)}


def dispatch(workflow_id: str, *, host: str, host_session_id: str, idempotency_key: str) -> dict:
    flow = workflow_state.workflow(workflow_id)
    if host not in ("dsh", "generic") or not isinstance(host_session_id, str):
        raise ValueError("宿主需要为 dsh 或 generic，宿主会话为文字")
    if flow["lifecycle"] != "active":
        raise ValueError("请先恢复工作流再交给 AI 处理")
    scope = "workflow-dashboard:" + workflow_id
    payload = {"host": host, "host_session_id": host_session_id, "cycle_id": flow["cycle_id"]}
    with db.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        found = workflow_state.request(connection, scope, idempotency_key, "dispatch", payload)
        if replayed := workflow_state.replay(found):
            return replayed
        identity, occurrence, stamp = db.new_id("delivery"), db.new_id("occurrence"), db.now()
        content = {"project_id": flow["project_id"], "workflow_id": workflow_id, "task_id": workflow_id, "occurrence_id": occurrence,
                   "session_id": workflow_id, "parameters": {"host_session_id": host_session_id}}
        connection.execute("INSERT INTO host_outbox (id,task_id,occurrence_id,host,payload,state,created_at,updated_at) VALUES (?,?,?,?,?,'pending',?,?)",
                           (identity, workflow_id, occurrence, host, workflow_state.packed(content), stamp, stamp))
        return workflow_state.receipt(connection, scope, idempotency_key, {"id": identity, "state": "pending", "status": "awaiting_host", "workflow_id": workflow_id})
