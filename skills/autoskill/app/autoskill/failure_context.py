"""Read receipt-bound failures and validate complete host diagnoses without executing work.

Execution attempt IDs name runner receipts; they are never workflow lease attempt IDs.
Receipt hashes use execution_inputs.content_hash (canonical JSON), while artifact hashes
cover raw file bytes. Target IDs hash their complete typed identity without target_id.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath

from . import data_refs, db, execution_inputs, paths
from . import workflow_state as state

DISPOSITIONS = ("incomplete", "business_defect", "repair", "retry")
RECEIPT_TYPES = ("assertion", "execution_error", "incomplete_attempt")


def _unavailable(message: str, **details) -> state.WorkflowError:
    return state.WorkflowError("evidence_unavailable", message, **details)


def _scope(message: str, **details) -> state.WorkflowError:
    return state.WorkflowError("evidence_scope_mismatch", message, **details)


def _object(value) -> dict:
    parsed = json.loads(value) if isinstance(value, str) else value
    if not isinstance(parsed, dict):
        raise ValueError("证据对象格式无效")
    return parsed


def _run(project_id: str, run_id: str) -> dict:
    run = db.one("SELECT * FROM runs WHERE id = ?", (run_id,))
    if not run:
        raise _unavailable("指定运行的本机记录缺失", run_id=run_id)
    if run["project_id"] != project_id:
        raise _scope("运行不属于指定项目", run_id=run_id)
    return run


def _folder(run_id: str):
    return (paths.local() / "runs" / run_id).resolve()


def _recorded_reference(reference, case_id: str) -> dict:
    """Keep valid durable identities even when an indexed receipt reference is malformed."""
    reference = reference if isinstance(reference, dict) else {}
    identity = reference.get("id")
    digest = reference.get("sha256")
    if (isinstance(identity, str) and identity and isinstance(reference.get("path"), str)
            and isinstance(digest, str) and digest):
        return {**reference, "case_id": case_id}
    return {"id": identity if isinstance(identity, str) and identity else None, "case_id": case_id,
            "sha256": digest if isinstance(digest, str) else None, "evidence_unavailable": True}


def _inventory(run: dict, summaries: list[dict]) -> list[dict]:
    """Enumerate durable result references and journal filenames before reading receipts."""
    records = db.rows("SELECT * FROM results WHERE run_id = ? ORDER BY id", (run["id"],))
    cases = {row.get("case_id") for row in summaries if row.get("case_id")}
    selected = json.loads(run["case_ids"]) if isinstance(run.get("case_ids"), str) else run.get("case_ids", [])
    cases.update(selected or [])
    references, represented = [], set()
    for summary in summaries:
        for reference in summary.get("execution_attempts", []):
            references.append(_recorded_reference(reference, summary["case_id"]))
            represented.add(summary["case_id"])
    for result in records:
        cases.add(result["case_id"])
        try:
            attempts = _object(result["detail"]).get("attempts", [])
            if not isinstance(attempts, list) or not attempts or any(not isinstance(attempt, dict) for attempt in attempts):
                raise ValueError("执行尝试列表格式无效")
        except (ValueError, TypeError):
            attempts = []
            references.append({"id": None, "case_id": result["case_id"], "sha256": None, "evidence_unavailable": True})
        for reference in attempts:
            references.append({**_recorded_reference(reference, result["case_id"]), "result": result})
            represented.add(result["case_id"])
    known = {reference.get("id") for reference in references}
    journal_folder = paths.local() / "runs" / run["id"] / "attempts"
    references.extend({"id": file.stem, "path": str(file), "case_id": None, "sha256": None}
                      for file in sorted(journal_folder.glob("*.json")) if file.stem not in known)
    references.extend({"id": None, "case_id": identity, "sha256": None} for identity in sorted(cases - represented))
    return references


def _receipt(run: dict, reference: dict, *, case_id: str | None = None) -> tuple[dict, dict]:
    """Verify primary evidence independently of optional screenshots and attachments."""
    try:
        manifest = execution_inputs.load(run)
        file = data_refs.resolve(reference["path"])
        if not file.resolve().is_relative_to(_folder(run["id"]) / "attempts"):
            raise ValueError("执行凭据路径不属于指定运行")
        receipt = _object(json.loads(file.read_bytes()))
        identity = case_id or reference.get("case_id") or receipt.get("case_id")
        digest = execution_inputs.content_hash(receipt)
        if (receipt.get("id") != reference.get("id") or receipt.get("run_id") != run["id"]
                or receipt.get("case_id") != identity or identity not in manifest["cases"]
                or receipt.get("input_hash") != run["input_hash"]
                or receipt.get("case_content_hash") != manifest["case_hashes"][identity]
                or receipt.get("case_scope_hash") != manifest["scope_hashes"][identity]
                or (reference.get("sha256") is not None and digest != reference["sha256"])):
            raise ValueError("执行凭据与冻结输入或记录摘要不匹配")
        result = reference.get("result")
        if result and (result["input_hash"] != run["input_hash"] or result["case_content_hash"] != receipt["case_content_hash"]
                       or _object(result["detail"]).get("case_scope_hash") != receipt["case_scope_hash"]):
            raise ValueError("结果记录与执行凭据不匹配")
        if (not isinstance(receipt.get("state"), str) or not receipt["state"]
                or (receipt.get("outcome") is not None and not isinstance(receipt["outcome"], dict))
                or (receipt["state"] == "completed" and not isinstance(receipt.get("outcome"), dict))):
            raise ValueError("执行凭据状态或结果缺失")
        outcome = receipt.get("outcome") or {}
        for rows in [outcome.get("assertions", []), outcome.get("steps", []), receipt.get("artifacts", [])]:
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError("执行凭据的断言、步骤或附件格式无效")
        if receipt["state"] == "completed" and reference.get("result"):
            outcome = receipt["outcome"]
            if any(reference.get(key) != outcome.get(key) for key in ("status", "business_status")) or reference.get("kind") != receipt.get("kind"):
                raise ValueError("结果索引与执行凭据不匹配")
        return receipt, manifest
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise _unavailable("执行输入或主要执行凭据缺失、损坏或不匹配", run_id=run["id"],
                           execution_attempt_id=reference.get("id"), case_id=case_id or reference.get("case_id")) from error


def _receipt_ref(receipt: dict, pointer: str) -> dict:
    return {"kind": "receipt", "run_id": receipt["run_id"], "execution_attempt_id": receipt["id"], "case_id": receipt["case_id"],
            "pointer": pointer, "receipt_sha256": execution_inputs.content_hash(receipt)}


def _artifacts(receipt: dict) -> tuple[list[dict], list[dict]]:
    available, missing = [], []
    for artifact in receipt.get("artifacts", []):
        reference = {"kind": "artifact", "run_id": receipt["run_id"], "execution_attempt_id": receipt["id"],
                     "case_id": receipt["case_id"], "artifact_id": artifact.get("path"), "sha256": artifact.get("sha256")}
        try:
            relative = PurePosixPath(artifact["path"])
            folder = _folder(receipt["run_id"])
            file = (folder / relative).resolve()
            if relative.is_absolute() or ".." in relative.parts or not file.is_relative_to(folder):
                raise ValueError("附件引用不是运行目录内的相对路径")
            if hashlib.sha256(file.read_bytes()).hexdigest() != artifact["sha256"]:
                raise ValueError("附件摘要不匹配")
        except (OSError, KeyError, TypeError, ValueError):
            missing.append({**reference, "reason": "附件缺失或摘要不匹配"})
        else:
            available.append(reference)
    return available, missing


def _project(receipt: dict, project_id: str) -> dict:
    outcome = receipt.get("outcome") or {}
    assertions = [{**row, "pointer": f"/outcome/assertions/{index}", "evidence_ref": _receipt_ref(receipt, f"/outcome/assertions/{index}")}
                  for index, row in enumerate(outcome.get("assertions", []))]
    failed_steps = [{**row, "pointer": f"/outcome/steps/{index}", "evidence_ref": _receipt_ref(receipt, f"/outcome/steps/{index}")}
                    for index, row in enumerate(outcome.get("steps", [])) if row.get("status") in ("failed", "running", "blocked")]
    artifacts, missing = _artifacts(receipt)
    pointers = ["", *(["/outcome"] if receipt.get("outcome") is not None else [])]
    evidence = [_receipt_ref(receipt, pointer) for pointer in pointers]
    evidence.extend(row["evidence_ref"] for row in [*failed_steps, *assertions])
    return {"schema_version": 1, "project_id": project_id, "run_id": receipt["run_id"], "execution_attempt_id": receipt["id"],
            "case_id": receipt["case_id"], "input_hash": receipt["input_hash"], "case_scope_hash": receipt["case_scope_hash"],
            "receipt_sha256": execution_inputs.content_hash(receipt), "historical": True, "state": receipt["state"],
            "effects_unknown": _effects_unknown(receipt),
            "outcome": {key: outcome.get(key) for key in ("status", "category", "reason", "business_status", "performance_status", "execution_status")},
            "failed_steps": failed_steps, "assertions": assertions, "evidence_refs": [*evidence, *artifacts], "missing_artifacts": missing}


def read(project_id: str, *, run_id: str, execution_attempt_id: str, case_id: str) -> dict:
    """Return historical receipt context for exactly one execution attempt; never select latest."""
    run = _run(project_id, run_id)
    references = [ref for ref in _inventory(run, []) if ref.get("id") == execution_attempt_id]
    if not references or any(ref.get("case_id") not in (None, case_id) for ref in references):
        raise _scope("执行尝试不属于指定运行与用例", run_id=run_id, execution_attempt_id=execution_attempt_id, case_id=case_id)
    receipt, _manifest = _receipt(run, references[0], case_id=case_id)
    return _project(receipt, project_id)


def _target(kind: str, **identity) -> dict:
    body = {"type": kind, **identity}
    return {**body, "target_id": state.digest(body)}


def _gap(run_id: str | None, case_id: str | None, reference: dict, input_hash: str | None) -> dict:
    return _target("evidence_gap", run_id=run_id, case_id=case_id, execution_attempt_id=reference.get("id"),
                   operation_id=reference.get("operation_id"), input_hash=input_hash, expected_hash=reference.get("sha256"))


def _receipt_targets(receipt: dict) -> list[dict]:
    identity = {"run_id": receipt["run_id"], "execution_attempt_id": receipt["id"], "case_id": receipt["case_id"],
                "input_hash": receipt["input_hash"], "receipt_sha256": execution_inputs.content_hash(receipt)}
    if receipt["state"] != "completed":
        return [_target("incomplete_attempt", **identity, pointer="", state=receipt["state"])]
    outcome = receipt["outcome"]
    category = outcome.get("category") or "unverified"
    blocker = outcome.get("execution_blocker")
    failure_is_assertion = category in ("unverified", "assertion_failed") or outcome.get("business_status") == "Fail"
    assertions = [_target("assertion", **identity, pointer=f"/outcome/assertions/{index}")
                  for index, row in enumerate(outcome.get("assertions", [])) if row.get("passed") is False and failure_is_assertion]
    failed_steps = [index for index, row in enumerate(outcome.get("steps", [])) if row.get("status") in ("failed", "running", "blocked") and not row.get("check")]
    if blocker or (not assertions and (outcome.get("status") != "Pass" or outcome.get("business_status") != "Pass")):
        category = blocker.get("category", category) if isinstance(blocker, dict) else category
        pointers = [f"/outcome/steps/{index}" for index in failed_steps] or ["/outcome"]
        uncertainty = {"effects_unknown": True} if _effects_unknown(receipt) else {}
        assertions.extend(_target("execution_error", **identity, pointer=pointer, failure_category=category, **uncertainty) for pointer in pointers)
    return assertions


def _effects_unknown(receipt: dict) -> bool:
    """A completed transport receipt cannot establish what an unanswered write did."""
    outcome = receipt.get("outcome") or {}
    request = outcome.get("request")
    return (isinstance(request, dict) and isinstance(request.get("method"), str)
            and request["method"].upper() not in ("GET", "HEAD", "OPTIONS")
            and outcome.get("response") is None and outcome.get("category") in ("timeout", "environment"))


def _run_targets(project_id: str, summary: dict) -> list[dict]:
    run_id = summary.get("run_id")
    records = summary.get("results", [])
    try:
        run = _run(project_id, run_id)
    except state.WorkflowError:
        return [_gap(run_id, row.get("case_id"), reference, summary.get("input_hash"))
                for row in records or [{}] for reference in row.get("execution_attempts", []) or [{}]]
    references = _inventory(run, records)
    if summary.get("input_hash") and summary["input_hash"] != run.get("input_hash"):
        return [_gap(run_id, reference.get("case_id"), reference, summary["input_hash"]) for reference in references]
    targets, observed = [], set()
    missing = []
    for reference in references:
        if reference.get("id") is None:
            missing.append(reference)
            continue
        try:
            receipt, _manifest = _receipt(run, reference)
        except state.WorkflowError:
            targets.append(_gap(run_id, reference.get("case_id"), reference, run.get("input_hash")))
        else:
            observed.add(receipt["case_id"])
            targets.extend(_receipt_targets(receipt))
    targets.extend(_gap(run_id, ref["case_id"], ref, run.get("input_hash")) for ref in missing
                   if ref.get("evidence_unavailable") or ref["case_id"] not in observed)
    return targets


def _operation(project_id: str, operation_id: str) -> dict:
    operation = db.one("SELECT o.*, w.project_id FROM workflow_operations o JOIN workflow_runtime w ON w.id = o.workflow_id WHERE o.id = ?", (operation_id,))
    if not operation:
        raise _unavailable("原始操作意图或回执缺失", operation_id=operation_id)
    if operation["project_id"] != project_id:
        raise _scope("操作意图不属于指定项目", operation_id=operation_id)
    try:
        payload = _object(operation["payload"])
        if operation.get("observation") is not None:
            _object(operation["observation"])
        if operation["kind"] == "execute":
            item = db.one("SELECT * FROM workflow_items WHERE id = ?", (operation["item_id"],))
            run = _run(project_id, payload.get("run_id"))
            if (not item or item["phase"] != "execute" or item["workflow_id"] != operation["workflow_id"]
                    or item["run_id"] != run["id"] or payload.get("input_hash") != run["input_hash"]):
                raise ValueError("执行操作与原任务或冻结输入不匹配")
        elif operation["payload_hash"] != state.digest({
                "kind": operation["kind"], "payload": payload, "side_effect": bool(operation["side_effect"])}):
            raise ValueError("操作内容与意图摘要不匹配")
    except (KeyError, TypeError, ValueError) as error:
        raise _unavailable("原始操作意图损坏或摘要不匹配", operation_id=operation_id) from error
    return operation


def _operation_target(operation: dict) -> dict:
    payload = _object(operation["payload"])
    intent_content_hash = state.digest({"kind": operation["kind"], "payload": payload, "side_effect": bool(operation["side_effect"])})
    return _target("interruption", workflow_id=operation["workflow_id"], item_id=operation["item_id"], operation_id=operation["id"],
                   intent_hash=operation["payload_hash"], intent_content_hash=intent_content_hash,
                   run_id=payload.get("run_id"), input_hash=payload.get("input_hash"))


def build_manifest(project_id: str, validation_results: list[dict], *, workflow_id: str | None = None, operations: list[dict] | None = None) -> dict:
    """Freeze all run/attempt failures, including earlier rounds and durable evidence gaps.

    Explicit operations bind interrupted work to original intents. With workflow_id only,
    unresolved execute intents are restricted to the validation runs supplied by the caller.
    The returned object is suitable for merging into the durable diagnose item context.
    """
    targets = [target for summary in validation_results for target in _run_targets(project_id, summary)]
    if operations is None and workflow_id:
        run_ids = {summary.get("run_id") for summary in validation_results}
        candidates = db.rows("SELECT * FROM workflow_operations WHERE workflow_id = ? AND side_effect = 1 AND state IN ('intended','unknown')", (workflow_id,))
        operations = [operation for operation in candidates if _object(operation["payload"]).get("run_id") in run_ids]
    for reference in operations or []:
        try:
            operation = _operation(project_id, reference["id"])
            if workflow_id and operation["workflow_id"] != workflow_id:
                raise _scope("操作不属于指定工作流")
            targets.append(_operation_target(operation))
        except (state.WorkflowError, KeyError, TypeError, ValueError):
            targets.append(_gap(None, None, {"operation_id": reference.get("id"), "sha256": reference.get("payload_hash")}, None))
    unique = {target["target_id"]: target for target in targets}
    ordered = [unique[key] for key in sorted(unique)]
    return {"schema_version": 1, "diagnostic_targets": ordered, "manifest_hash": state.digest({"schema_version": 1, "diagnostic_targets": ordered})}


def _manifest(context: dict) -> list[dict]:
    targets = context.get("diagnostic_targets")
    if not isinstance(targets, list) or context.get("manifest_hash") != state.digest({"schema_version": 1, "diagnostic_targets": targets}):
        raise state.WorkflowError("revision_conflict", "诊断目标清单缺失或摘要已变化，请重新读取当前任务")
    for target in targets:
        if not isinstance(target, dict) or target.get("target_id") != state.digest({key: value for key, value in target.items() if key != "target_id"}):
            raise state.WorkflowError("revision_conflict", "诊断目标身份与摘要不匹配")
    return targets


def _target_evidence(project_id: str, target: dict) -> dict:
    if target["type"] == "evidence_gap":
        raise _unavailable("该目标的主要证据缺失或损坏", target_id=target["target_id"])
    if target["type"] == "interruption":
        operation = _operation(project_id, target["operation_id"])
        if _operation_target(operation) != target:
            raise _scope("原始操作意图已与诊断目标不一致", target_id=target["target_id"])
        reference = {"kind": "operation", **{key: target[key] for key in ("workflow_id", "item_id", "operation_id", "intent_hash")}}
        return {"evidence_refs": [reference], "missing_artifacts": [], "operation": {
            "kind": operation["kind"], "state": operation["state"], "payload": _object(operation["payload"]),
            "observation": _object(operation["observation"]) if operation.get("observation") else None}}
    if target["type"] not in RECEIPT_TYPES:
        raise _scope("未知的诊断目标类型", target_id=target["target_id"])
    context = read(project_id, run_id=target["run_id"], execution_attempt_id=target["execution_attempt_id"], case_id=target["case_id"])
    if context["input_hash"] != target["input_hash"] or context["receipt_sha256"] != target["receipt_sha256"]:
        raise _unavailable("执行凭据已与冻结诊断清单不一致", target_id=target["target_id"])
    reference = {"kind": "receipt", **{key: target[key] for key in ("run_id", "execution_attempt_id", "case_id", "pointer", "receipt_sha256")}}
    if reference not in context["evidence_refs"]:
        raise _scope("诊断目标指针不对应真实执行凭据", target_id=target["target_id"])
    context["evidence_refs"] = [reference, *[ref for ref in context["evidence_refs"] if ref["kind"] == "artifact"]]
    return context


def workflow_context(project_id: str, context: dict) -> dict:
    """Project a frozen item manifest with per-target evidence; never refresh its identities."""
    if "manifest_hash" not in context and "diagnostic_targets" not in context:
        return {"schema_version": 1, "project_id": project_id, "manifest_hash": None, "diagnostic_targets": [],
                "target_contexts": [], "legacy": True, "allowed_dispositions": ["incomplete"]}
    targets = _manifest(context)
    evidence = []
    for target in targets:
        try:
            projection = {"target_id": target["target_id"], **_target_evidence(project_id, target), "evidence_available": True}
        except state.WorkflowError as error:
            projection = {"target_id": target["target_id"], "evidence_available": False, "error": error.code,
                          "message": str(error), "evidence_refs": [], "missing_artifacts": []}
        evidence.append(projection)
    return {"schema_version": 1, "project_id": project_id, "manifest_hash": context["manifest_hash"],
            "diagnostic_targets": targets, "target_contexts": evidence}


def _diagnosis(project_id: str, target: dict, diagnosis: dict) -> dict:
    disposition, reason = diagnosis.get("disposition"), diagnosis.get("reason")
    if disposition not in DISPOSITIONS or not isinstance(reason, str) or not reason.strip():
        raise state.WorkflowError("validation_error", "每个诊断目标都需要有效的 disposition 与非空 reason")
    if (target["type"] in ("interruption", "incomplete_attempt", "evidence_gap") or target.get("effects_unknown")) and disposition != "incomplete":
        raise _unavailable("未完成执行、未知外部影响或缺失证据只能保留 incomplete，不能自动修复或重试", target_id=target["target_id"])
    references = diagnosis.get("evidence_refs", [])
    if not isinstance(references, list) or any(not isinstance(ref, dict) for ref in references):
        raise _scope("证据引用必须使用带执行身份与摘要的结构化对象")
    try:
        context = _target_evidence(project_id, target)
    except state.WorkflowError as error:
        if disposition != "incomplete" or references:
            raise _unavailable("目标证据不可用，只能提交无证据引用的 incomplete 诊断", target_id=target["target_id"]) from error
    else:
        if context.get("effects_unknown") and disposition != "incomplete":
            raise _unavailable("原执行凭据仍不能确认写请求的外部影响，只能保留 incomplete", target_id=target["target_id"])
        if any(ref not in context["evidence_refs"] for ref in references):
            raise _scope("诊断引用超出该目标的执行凭据或附件范围", target_id=target["target_id"])
        primary = [ref for ref in references if ref.get("kind") in ("receipt", "operation")]
        if disposition != "incomplete" and not primary:
            raise _unavailable("诊断必须引用该目标的真实主要凭据", target_id=target["target_id"])
    return {"target_id": target["target_id"], "disposition": disposition, "reason": reason.strip(), "evidence_refs": references, "origin": "host_analysis"}


def validate_diagnoses(project_id: str, context: dict, payload: dict) -> dict:
    """Validate exact manifest coverage and evidence, returning an aggregate diagnosis document.

    The caller supplies the current item's frozen context after checking its workflow lease
    and input revision. Legacy submissions may only expand an explicit incomplete outcome.
    """
    if "manifest_hash" not in context and "diagnostic_targets" not in context:
        if payload.get("disposition") != "incomplete" or not isinstance(payload.get("reason"), str) or not payload["reason"].strip():
            raise state.WorkflowError("validation_error", "旧任务缺少冻结诊断清单，只能提交带原因的 incomplete；请升级客户端后开始新的验证周期")
        return {"schema_version": 1, "manifest_hash": None, "diagnoses": [], "disposition": "incomplete", "reason": payload["reason"].strip(),
                "origin": "host_analysis", "legacy": True}
    targets = _manifest(context)
    if not payload.get("manifest_hash"):
        if payload.get("disposition") != "incomplete" or not isinstance(payload.get("reason"), str) or not payload["reason"].strip():
            raise state.WorkflowError("validation_error", "请升级客户端并提交 manifest_hash 与覆盖全部目标的 diagnoses")
        entries = [{"target_id": target["target_id"], "disposition": "incomplete", "reason": payload.get("reason"), "evidence_refs": []} for target in targets]
    else:
        if payload["manifest_hash"] != context["manifest_hash"]:
            raise state.WorkflowError("revision_conflict", "提交的诊断清单不是当前任务的冻结清单")
        entries = payload.get("diagnoses")
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
        raise state.WorkflowError("validation_error", "diagnoses 必须是诊断对象列表")
    identifiers = [entry.get("target_id") for entry in entries]
    wanted = [target["target_id"] for target in targets]
    if any(not isinstance(identity, str) for identity in identifiers) or len(identifiers) != len(set(identifiers)) or set(identifiers) != set(wanted):
        raise _scope("diagnoses 必须恰好覆盖清单中的每个目标，不能遗漏、重复或增加目标")
    by_id = {entry["target_id"]: entry for entry in entries}
    diagnoses = [_diagnosis(project_id, target, by_id[target["target_id"]]) for target in targets]
    disposition = next((value for value in DISPOSITIONS if any(row["disposition"] == value for row in diagnoses)), "incomplete")
    reason = "；".join(row["reason"] for row in diagnoses) or payload.get("reason") or "没有可用的失败目标，保留未完成结论"
    return {"schema_version": 1, "manifest_hash": context["manifest_hash"], "diagnoses": diagnoses,
            "disposition": disposition, "reason": reason, "origin": "host_analysis"}
