"""Deterministic workflow coverage, execution requests and evidence collection."""
from __future__ import annotations

import hashlib
import json

from . import db, projects, runner
from . import workflow_definition as definitions
from . import workflow_state as state


def _kernel_attempt(connection, flow: dict, task: dict, phase: str) -> dict:
    attempt = {"id": state.identifier("wfa"), "lease_token": state.identifier("kernel")}
    generation = connection.execute("SELECT COUNT(*) + 1 FROM workflow_attempts WHERE item_id = ?", (task["id"],)).fetchone()[0]
    connection.execute(
        "INSERT INTO workflow_attempts(id,item_id,workflow_id,generation,owner,lease_token,lease_until,state,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (attempt["id"], task["id"], flow["id"], generation, "kernel", attempt["lease_token"], db.now(), phase, db.now()))
    connection.execute("UPDATE workflow_items SET attempt_id = ? WHERE id = ?", (attempt["id"], task["id"]))
    return {**task, "attempt_id": attempt["id"]}


def _coverage(flow: dict, task: dict) -> None:
    current = definitions.snapshot(flow)
    result = definitions.coverage(current["documents"])
    with db.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        task = _kernel_attempt(connection, flow, task, "checking")
        connection.execute("UPDATE workflow_items SET context = ? WHERE id = ?", (state.packed(result), task["id"]))
        state.complete(connection, task)
        state.item(connection, flow["id"], "completion" if result["missing_requirement_ids"] else "materialize", context=result)


def _requirement_records(flow: dict, current: dict, candidates: list[dict]) -> list[dict]:
    used = {identity for case in candidates for identity in case.get("requirement_ids", [])}
    return [{**record, "module": record.get("module") or "工作流", "title": record.get("title") or record["text"],
             "source": record.get("source") or "workflow:" + flow["id"],
             "workflow_ref": {"workflow_id": flow["id"], "cycle_id": flow["cycle_id"],
                              "domain_head": current["head"], "definition_hash": current["hash"]}}
            for record in current["documents"]["requirements.json"] if record["id"] in used]


def _enqueue(flow: dict, task: dict) -> None:
    from . import execution_inputs

    current = definitions.snapshot(flow)
    materialization = current["documents"]["materialization.json"]
    validation = current["documents"]["definition.json"]["validation"]
    previous = json.loads(task["context"])
    requirement_records = previous["requirement_records"] if previous.get("workflow_input_hash") == current["hash"] and "requirement_records" in previous else \
        _requirement_records(flow, current, materialization["cases"])
    if not previous.get("validation_scope"):
        _requirement_catalog(flow, materialization["cases"])
        projects.sync_requirement_records(flow["project_id"], requirement_records, candidate_cases=materialization["cases"])
    prepared = runner.prepare(flow["project_id"], materialization["case_ids"], engine=validation["engine"], trigger="workflow",
                              session_id=flow["id"], candidate_cases=materialization["cases"])
    manifest = execution_inputs.load(prepared)
    for identity, wanted in materialization["case_hashes"].items():
        if manifest["case_hashes"].get(identity) != wanted:
            raise state.WorkflowError("revision_conflict", "待执行用例与绑定快照不一致，不能执行后贴用例哈希", case_id=identity)
    existing = {case["id"]: case for case in projects.cases(flow["project_id"], active_only=False)}
    expected = {identity: state.digest(existing[identity]) if identity in existing else None for identity in manifest["cases"]}
    expected.update(materialization["base_case_hashes"])
    if previous.get("validation_scope") and previous["validation_scope"] != manifest["scope_hashes"]:
        raise state.WorkflowError("revision_conflict", "重复验证期间依赖或执行环境已变化，本批证据不能混合用于发布")
    context = {**previous, "execution_request_id": state.identifier("execution-request"),
               "input_hash": prepared["input_hash"], "workflow_input_hash": current["hash"],
               "materialization": materialization, "publication_expected_hashes": expected, "requirement_records": requirement_records,
               "validation_id": previous.get("validation_id") or state.identifier("validation"), "validation_scope": manifest["scope_hashes"],
               "round": previous.get("round", 1), "repetitions": validation["repetitions"]}
    with db.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        fresh = state.row(connection, "workflow_items", task["id"])
        if fresh["state"] != "ready":
            return
        context["report_generation"] = state.report_generation(connection, flow["id"], flow["cycle_id"])
        task = _kernel_attempt(connection, flow, task, "waiting_execution")
        runner.enqueue(prepared, connection=connection)
        connection.execute("UPDATE workflow_items SET state = 'waiting_execution', run_id = ?, input_hash = ?, context = ? WHERE id = ?",
                           (prepared["id"], current["hash"], state.packed(context), task["id"]))
        connection.execute(
            "INSERT INTO workflow_operations(id,workflow_id,item_id,attempt_id,request_key,payload_hash,kind,payload,side_effect,state,receipt_token,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (state.identifier("wfo"), flow["id"], task["id"], task["attempt_id"], context["execution_request_id"],
                                               state.digest(context), "execute", state.packed({"run_id": prepared["id"], "input_hash": prepared["input_hash"]}),
                                               1, "intended", state.identifier("receipt"), db.now()))
        state.touch(connection, flow["id"])
    from .server.jobs import ensure_worker

    ensure_worker()
    runner.spawn(prepared["id"])


def _result_summary(run: dict, materialization: dict) -> dict:
    from . import execution_inputs

    records = db.rows("SELECT * FROM results WHERE run_id = ? AND prerequisite = 0 ORDER BY id", (run["id"],))
    by_case = {result["case_id"]: result for result in records}
    observed = []
    for identity in materialization["case_ids"]:
        result = by_case.get(identity)
        if not result:
            observed.append({"case_id": identity, "business_status": "Unverified", "performance_status": "Unverified",
                             "evidence_valid": False, "reason": "缺少实际执行结果"})
            continue
        detail = json.loads(result["detail"])
        valid = (result.get("case_content_hash") == materialization["case_hashes"][identity]
                 and execution_inputs.result_evidence(run, result))
        observed.append({"case_id": identity, "result_id": result["id"], "business_status": detail.get("business_status", "Unverified") if valid else "Unverified",
                         "performance_status": detail.get("performance_status", "Unverified") if valid else "Unverified",
                         "evidence_valid": valid, "verification": detail.get("verification"), "status": result["status"], "reason": result.get("reason", "")})
    verdicts = {record["business_status"] for record in observed}
    performance = {record["performance_status"] for record in observed}
    performance_status = "Fail" if "Fail" in performance else "Pass" if "Pass" in performance else "Unverified"
    conclusion = "violated" if "Fail" in verdicts or performance_status == "Fail" else "satisfied" if verdicts == {"Pass"} and run["status"] == "Pass" else "incomplete"
    return {"run_id": run["id"], "run_status": run["status"], "conclusion": conclusion, "results": observed,
            "evidence_valid": all(record["evidence_valid"] for record in observed),
            "business_verified": verdicts == {"Pass"}, "performance_status": performance_status}


def _requirement_catalog(flow: dict, candidates: list[dict]) -> None:
    from .core import store

    folder = projects.path(flow["project_id"])
    requirements = store.read(folder / "analysis/requirements.json")
    known = {item["id"] for item in requirements}
    wanted = {identity for case in candidates for identity in case.get("requirement_ids", [])} - known
    if not wanted:
        return
    if "REQ-RECORDED" in wanted:
        requirements.append({"id": "REQ-RECORDED", "module": "录制", "title": "录制得到的回归场景", "source": "autoskill record"})
        wanted.remove("REQ-RECORDED")
    documents = definitions.snapshot(flow)["documents"]
    declared = {item["id"]: item for item in documents.get("requirements.json", [])}
    if wanted - declared.keys():
        raise state.WorkflowError("validation_error", "候选用例关联的需求未在本工作流声明", requirement_ids=sorted(wanted - declared.keys()))
    requirements.extend({"id": identity, "module": "需求工作流", "title": declared[identity]["text"],
                         "source": "workflow:" + flow["id"] + ":" + flow["domain_head"]} for identity in sorted(wanted))
    store.write(folder / "analysis/requirements.json", requirements)


def _publish_cases(flow: dict, context: dict, summaries: list[dict]) -> str:
    from . import execution_inputs

    candidates = context["materialization"]["cases"]
    if not candidates:
        return "bound_existing"
    current = definitions.snapshot(flow)
    if current["hash"] != context["workflow_input_hash"]:
        return "conflict"
    if any(not summary["evidence_valid"] or any(row["business_status"] == "Unverified" for row in summary["results"]) for summary in summaries):
        return "unverified"
    if context["materialization"].get("origin") in ("maintain", "requirement_change") and any(summary["conclusion"] != "satisfied" for summary in summaries):
        return "rejected"
    try:
        run = db.one("SELECT * FROM runs WHERE id = ?", (summaries[-1]["run_id"],))
        frozen = execution_inputs.load(run)["sources"]["files"]["analysis/requirements.json"]
        source = (projects.path(flow["project_id"]) / "analysis/requirements.json").read_bytes()
        if len(source) != frozen["size"] or hashlib.sha256(source).hexdigest() != frozen["sha256"]:
            return "conflict"
    except (OSError, KeyError, TypeError, ValueError):
        return "unverified"
    requirement_records = context["requirement_records"] if "requirement_records" in context else _requirement_records(flow, current, candidates)
    if not projects.requirement_records_current(flow["project_id"], requirement_records, candidate_cases=candidates):
        return "conflict"
    existing = {case["id"]: case for case in projects.cases(flow["project_id"], active_only=False)}
    if all(state.digest(existing.get(case["id"])) == state.digest(case) for case in candidates):
        return "published"
    try:
        projects.save_cases(flow["project_id"], candidates, expected_hashes=context["publication_expected_hashes"],
                            requirement_records=requirement_records)
    except projects.CaseConflictError:
        return "conflict"
    return "published"


def _aggregate(connection, workflow_id: str) -> str:
    conclusions = {json.loads(r["context"]).get("summary", {}).get("conclusion") for r in connection.execute(
        "SELECT context FROM workflow_items WHERE workflow_id = ? AND cycle_id = (SELECT cycle_id FROM workflow_runtime WHERE id = ?) "
        "AND phase = 'execute' AND state = 'completed'", (workflow_id, workflow_id))}
    if "violated" in conclusions:
        return "mixed" if "satisfied" in conclusions else "violated"
    return "incomplete" if "incomplete" in conclusions or not conclusions else "satisfied"


def _collect(flow: dict, task: dict, run: dict) -> None:
    context = json.loads(task["context"])
    summary = _result_summary(run, context["materialization"])
    historical = task["cycle_id"] != flow["cycle_id"]
    interrupted = run["status"] == "Cancelled" or (run["status"] == "Blocked" and run["done"] < run["total"])
    earlier = [json.loads(record["context"]) for record in db.rows(
        "SELECT context FROM workflow_items WHERE workflow_id = ? AND phase = 'execute' AND state = 'completed'", (flow["id"],))]
    summaries = [_result_summary(db.one("SELECT * FROM runs WHERE id = ?", (record["summary"]["run_id"],)), record["materialization"])
                 for record in earlier if record.get("validation_id") == context["validation_id"]] + [summary]
    more = context["round"] < context["repetitions"]
    publication = "historical" if historical else "interrupted" if interrupted else "pending_validation" if more else _publish_cases(flow, context, summaries)
    context.update(summary=summary, publication=publication)
    with db.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("UPDATE workflow_items SET context = ? WHERE id = ?", (state.packed(context), task["id"]))
        connection.execute("UPDATE workflow_operations SET state = ?, observation = ?, finished_at = ? WHERE item_id = ? AND kind = 'execute'",
                           ("unknown" if interrupted else "succeeded", state.packed(summary), db.now(), task["id"]))
        if interrupted:
            connection.execute("UPDATE workflow_items SET state = 'recovering' WHERE id = ?", (task["id"],))
            state.touch(connection, flow["id"])
            return
        if historical:
            state.complete(connection, task, state="obsolete")
            return
        state.complete(connection, task)
        connection.execute("UPDATE workflow_runtime SET conclusion = ? WHERE id = ?", (_aggregate(connection, flow["id"]), flow["id"]))
        if more:
            state.item(connection, flow["id"], "execute", context={"round": context["round"] + 1, "validation_id": context["validation_id"],
                                                                  "validation_scope": context["validation_scope"], "workflow_input_hash": context["workflow_input_hash"],
                                                                  "requirement_records": context.get("requirement_records", [])})
        else:
            following = "knowledge" if all(result["conclusion"] == "satisfied" for result in summaries) and publication not in ("conflict", "unverified", "rejected") else "diagnose"
            state.item(connection, flow["id"], following, context={"execution": summary, "validation_results": summaries, "publication": publication})


def _investigations(flow: dict) -> None:
    with db.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        for task in connection.execute("SELECT * FROM workflow_items WHERE workflow_id = ? AND state = 'recovering'", (flow["id"],)).fetchall():
            pending = state.unresolved(connection, task["id"])
            if not pending:
                connection.execute("UPDATE workflow_items SET state = 'ready' WHERE id = ?", (task["id"],))
                state.touch(connection, flow["id"])
                for investigation in connection.execute(
                    "SELECT * FROM workflow_items WHERE workflow_id = ? AND phase = 'investigate' AND state IN ('ready','leased')", (flow["id"],)).fetchall():
                    if json.loads(investigation["context"]).get("recover_item_id") == task["id"]:
                        state.complete(connection, dict(investigation), state="obsolete")
                continue
            existing = [json.loads(row["context"]).get("recover_item_id") for row in connection.execute(
                "SELECT context FROM workflow_items WHERE workflow_id = ? AND phase = 'investigate' AND state IN ('ready','leased')", (flow["id"],))]
            if task["id"] not in existing:
                state.item(connection, flow["id"], "investigate", context={"recover_item_id": task["id"], "operations": pending,
                                                                         "instruction": "先观察目标真实状态。旧动作可能已经执行，不得直接重播。"})


def advance(flow: dict) -> None:
    """Advance only deterministic phases; host work remains explicitly claimable."""
    _investigations(flow)
    records = db.rows("SELECT * FROM workflow_items WHERE workflow_id = ? AND (state = 'waiting_execution' OR (state = 'ready' AND cycle_id = ?)) ORDER BY sequence",
                      (flow["id"], flow["cycle_id"]))
    for task in records:
        if task["state"] == "waiting_execution":
            run = db.one("SELECT * FROM runs WHERE id = ?", (task["run_id"],))
            if not run:
                raise state.WorkflowError("evidence_unavailable", "执行请求的本机运行记录丢失", run_id=task["run_id"])
            if run["status"] in db.FINISHED:
                _collect(flow, task, run)
            elif run["status"] == "Queued":
                from .server.jobs import ensure_worker

                ensure_worker()
                runner.spawn(run["id"])
        elif task["phase"] == "coverage":
            _coverage(flow, task)
        elif task["phase"] == "execute":
            _enqueue(flow, task)
