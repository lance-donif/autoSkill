"""Versioned workflow proposals and deterministic traceability checks."""
from __future__ import annotations

import copy
import json
from pathlib import Path

from . import case_document, domain, paths, projects
from .core import store
from .workflow_state import WorkflowError, digest, identifier

HOST_PHASES = {"sources", "requirements", "design", "completion", "materialize", "diagnose", "maintain", "knowledge", "investigate"}


def prefix(workflow_id: str) -> str:
    return "workflows/" + workflow_id + "/"


def snapshot(flow: dict, head: str | None = None) -> dict:
    current = domain.read(flow["project_id"], head)
    root = prefix(flow["id"])
    documents = {key.removeprefix(root): value for key, value in current["documents"].items() if key.startswith(root)}
    files = {key: value for key, value in current["files"].items() if key.startswith(root)}
    return {"head": current["head"], "documents": documents, "hash": digest({"documents": documents, "files": files})}


def _records(value, label: str) -> list[dict]:
    if not isinstance(value, list) or not value:
        raise WorkflowError("validation_error", f"{label}需要是非空列表")
    seen = set()
    for record in value:
        if not isinstance(record, dict) or not isinstance(record.get("id"), str) or not record["id"].strip():
            raise WorkflowError("validation_error", f"{label}的每项需要非空 id")
        if record["id"] in seen:
            raise WorkflowError("validation_error", f"{label}包含重复 id", id=record["id"])
        seen.add(record["id"])
    return copy.deepcopy(value)


def _references(record: dict, key: str, known: set) -> None:
    values = record.get(key)
    if not isinstance(values, list) or not values or not all(isinstance(value, str) and value in known for value in values):
        raise WorkflowError("validation_error", f"{record['id']} 的 {key} 必须引用已有条目", known=sorted(known))


def _text(record: dict, key: str = "text") -> None:
    if not isinstance(record.get(key), str) or not record[key].strip():
        raise WorkflowError("validation_error", f"{record['id']} 缺少 {key}")


def _initial_sources(sources: list[dict], root: str, changes: dict) -> None:
    for source in sources:
        if "file" in source:
            file = Path(source.pop("file"))
            if not file.is_absolute():
                raise WorkflowError("validation_error", "来源 file 需要绝对路径，CLI 会解析相对路径")
            raw = file.read_bytes()
            target = root + "artifacts/" + identifier("source")
            changes[target] = raw
            source["raw_ref"] = target
            source["filename"] = file.name
            if "text" not in source and file.suffix.lower() in (".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".html", ".xml"):
                try:
                    source["text"] = raw.decode("utf-8")
                except UnicodeDecodeError:
                    source["extraction_required"] = True
            elif "text" not in source:
                source["extraction_required"] = True
        if isinstance(source.get("text"), str):
            source["text"] = source["text"].replace("\r\n", "\n").replace("\r", "\n")


def initial(workflow_id: str, spec: dict, change_id: str) -> tuple[dict, str]:
    if not isinstance(spec, dict):
        raise WorkflowError("validation_error", "工作流定义必须是 JSON 对象")
    title = spec.get("title") or spec.get("goal")
    if not isinstance(title, str) or not title.strip():
        raise WorkflowError("validation_error", "工作流需要 title 或 goal")
    root, changes = prefix(workflow_id), {}
    sources = _records(spec.get("sources"), "sources") if spec.get("sources") else []
    _initial_sources(sources, root, changes)
    artifacts = copy.deepcopy(spec.get("artifacts", []))
    if not isinstance(artifacts, list):
        raise WorkflowError("validation_error", "artifacts 必须是列表")
    for artifact in artifacts:
        if "file" in artifact:
            file = Path(artifact.pop("file"))
            if not file.is_absolute():
                raise WorkflowError("validation_error", "artifact file 需要绝对路径")
            target = root + "artifacts/" + identifier("asset")
            changes[target] = file.read_bytes()
            artifact.update(ref=target, filename=file.name)
    validation = {"engine": "playwright", "repetitions": 1, **spec.get("validation", {})}
    if validation.get("engines"):
        validation["engine"] = validation["engines"][0]
    if validation["engine"] not in ("playwright", "selenium"):
        raise WorkflowError("validation_error", "当前 autoskill.ir 执行引擎为 playwright 或 selenium")
    repetitions = validation["repetitions"]
    if isinstance(repetitions, bool) or not isinstance(repetitions, int) or repetitions < 1:
        raise WorkflowError("validation_error", "repetitions 必须是正整数")
    definition = {"id": workflow_id, "title": title, "goal": spec.get("goal", title), "version": 1,
                  "change_id": change_id, "validation": validation, "artifacts": artifacts,
                  "maintenance": spec.get("maintenance", {}), "extensions": spec.get("extensions", {})}
    changes.update({root + "definition.json": definition, root + "sources.json": sources})
    phase = "requirements" if sources and all(s.get("text", "").strip() for s in sources) else "sources"
    return changes, phase


def _sources(payload: dict, documents: dict) -> dict:
    sources = _records(payload.get("sources"), "sources")
    old = {source["id"]: source for source in documents.get("sources.json", [])}
    for source in sources:
        _text(source)
        if source["id"] in old and old[source["id"]].get("raw_ref"):
            source["raw_ref"] = old[source["id"]]["raw_ref"]
        source["text"] = source["text"].replace("\r\n", "\n").replace("\r", "\n")
    return {"sources.json": sources}


def _requirements(payload: dict, documents: dict) -> dict:
    records = _records(payload.get("requirements"), "requirements")
    known = {source["id"] for source in documents["sources.json"]}
    for record in records:
        _text(record)
        _references(record, "source_ids", known)
    return {"requirements.json": records}


def _merge(old: list[dict], new: list[dict]) -> list[dict]:
    return list({record["id"]: record for record in [*old, *new]}.values())


def _design(payload: dict, documents: dict, completion: bool) -> dict:
    scenarios = _records(payload.get("scenarios"), "scenarios")
    oracles = _records(payload.get("oracles"), "oracles")
    if completion:
        scenarios = _merge(documents.get("scenarios.json", []), scenarios)
        oracles = _merge(documents.get("oracles.json", []), oracles)
    requirements = {record["id"] for record in documents["requirements.json"]}
    known = {oracle["id"] for oracle in oracles}
    for scenario in scenarios:
        _text(scenario, "title")
        _references(scenario, "requirement_ids", requirements)
        _references(scenario, "oracle_ids", known)
    for oracle in oracles:
        _references(oracle, "requirement_ids", requirements)
        if oracle.get("kind") not in ("ui", "api") or not isinstance(oracle.get("checks"), (dict, list)) or not oracle["checks"]:
            raise WorkflowError("validation_error", "oracle 需要 kind=ui/api 和非空 checks", id=oracle["id"])
    return {"scenarios.json": scenarios, "oracles.json": oracles}


def coverage(documents: dict) -> dict:
    required = {record["id"] for record in documents.get("requirements.json", [])}
    covered = {identity for scenario in documents.get("scenarios.json", []) for identity in scenario.get("requirement_ids", [])}
    return {"required": len(required), "covered": len(required & covered), "missing_requirement_ids": sorted(required - covered)}


def _ui_checks(automation: dict) -> list[dict]:
    return [{key: value for key, value in step.items() if key in ("assertion", "expected", "attribute")}
            for step in [*automation.get("actions", []), *automation.get("assertions", [])] if "assertion" in step]


def oracle_of(case: dict):
    return case.get("assertions", {}) if case["kind"] == "api" else _ui_checks(case.get("extensions", {}).get("automation", {}))


def _binding_only(before: dict, after: dict) -> bool:
    def without_locators(case):
        copied = copy.deepcopy(case)
        for key in ("revision", "updated_at"):
            copied.pop(key, None)
        automation = copied.get("extensions", {}).get("automation", {})
        for step in [*automation.get("actions", []), *automation.get("assertions", [])]:
            for key in ("locators", "selector", "placeholder", "role"):
                step.pop(key, None)
        return copied
    return without_locators(before) == without_locators(after)


def _change_scope(selected: set, current: dict, scope: dict) -> None:
    allowed = set(scope["case_ids"])
    if selected - allowed:
        raise WorkflowError("scope_conflict", "维护候选和回归绑定只能包含本次已选择的用例", case_ids=sorted(selected - allowed))
    if allowed - selected:
        raise WorkflowError("scope_incomplete", "维护回归必须覆盖全部已选择用例，不能将未处理的用例标记完成", case_ids=sorted(allowed - selected))
    for identity in selected:
        if digest(current.get(identity)) != scope["case_hashes"].get(identity):
            raise WorkflowError("revision_conflict", "维护范围内的用例已变化，请重新分析后建立维护任务", case_id=identity)


def _functional_candidates(candidates: list[dict], *, maintain: bool) -> list[dict]:
    for case in candidates:
        document = case_document.document(case)
        if document:
            case_document.apply(case, document)
            if not maintain:
                case["title"] = case_document.functional_description(case)
    return candidates


def _functional_mapping(case: dict, original: dict | None, expected: list[dict], scenarios: list[dict], definition: dict) -> None:
    """An explicit host mapping and changed executable checks precede new functional verification."""
    document = case_document.document(case)
    if not document:
        return
    mapping = case["extensions"].get("functional_design", {})
    changed_design = original is not None and case_document.document(original) != document
    originally_unmapped = original is not None and original.get("extensions", {}).get("functional_design", {}).get("state") == "unmapped"
    if not (changed_design or mapping.get("state") == "unmapped" or originally_unmapped or original is None):
        return
    matching = [oracle for oracle in expected if oracle["kind"] == case["kind"] and oracle["checks"] == oracle_of(case)
                and oracle.get("functional_expected") == document.get("expected") and document.get("expected")]
    changed_checks = original is None or oracle_of(original) != oracle_of(case)
    complete = all(document.get(field) for field in ("description", "preconditions", "steps", "expected", "source")) and "【待确认" not in json.dumps(document, ensure_ascii=False)
    if not matching or not changed_checks or not complete:
        case["extensions"]["functional_design"] = {"state": "unmapped", "reason": "新功能预期尚未映射到来源驱动且实际更新的检查条件"}
        return
    case["extensions"]["functional_design"] = {
        "state": "compiled_from_workflow_oracles", "workflow_id": definition["id"], "scenario_ids": [scenario["id"] for scenario in scenarios],
        "oracle_ids": [oracle["id"] for oracle in matching], "functional_expected": document["expected"],
        "scope": "宿主依据需求来源声明上述业务预期与检查的对应关系；执行通过仍需真实回归证据",
    }


def _functional_mappings(candidates: list[dict], current: dict, documents: dict, bindings: dict) -> None:
    oracles = {oracle["id"]: oracle for oracle in documents["oracles.json"]}
    for case in candidates:
        scenarios = [scenario for scenario in documents["scenarios.json"] if case["id"] in bindings.get(scenario["id"], [])]
        expected = [oracles[identity] for scenario in scenarios for identity in scenario["oracle_ids"]]
        _functional_mapping(case, current.get(case["id"]), expected, scenarios, documents["definition.json"])


def _validate_materialized_candidates(candidates: list[dict], requirement_ids: set[str], originals: dict, maintain: bool) -> None:
    import jsonschema

    from .replay import ir

    for case in candidates:
        errors = list(jsonschema.Draft202012Validator(store.read(paths.SKILL / "references/case.schema.json")).iter_errors(case))
        if errors:
            raise WorkflowError("validation_error", "候选用例结构错误：" + errors[0].message)
        if not set(case["requirement_ids"]).issubset(requirement_ids):
            raise WorkflowError("validation_error", "候选用例关联未知需求", case_id=case["id"])
        if case["kind"] == "ui":
            ir.validate(case.get("extensions", {}).get("automation"))
        if maintain:
            original = originals.get(case["id"])
            if not original or not _binding_only(original, case):
                raise WorkflowError("oracle_change", "维护候选只能修复定位；业务动作和原始检查条件必须保留，需求变化应建立新版本", case_id=case["id"])


def _materialize(payload: dict, documents: dict, project_id: str, maintain: bool) -> dict:
    current = {case["id"]: case for case in projects.cases(project_id, active_only=False)}
    change_scope = documents["definition.json"].get("extensions", {}).get("change_scope")
    previous = documents.get("materialization.json", {})
    candidates = _functional_candidates(_records(payload["cases"], "cases"), maintain=maintain) if payload.get("cases") else []
    old_candidates = {case["id"]: case for case in previous.get("cases", [])}
    requirement_ids = {record["id"] for record in documents["requirements.json"]}
    requirement_ids.update(record["id"] for record in store.read(projects.path(project_id) / "analysis/requirements.json"))
    requirement_ids.add("REQ-RECORDED")
    _validate_materialized_candidates(candidates, requirement_ids, {**current, **old_candidates}, maintain)
    available = {**current, **{case["id"]: case for case in candidates}}
    bindings = payload.get("bindings", previous.get("bindings") if maintain else None)
    if not isinstance(bindings, dict):
        raise WorkflowError("validation_error", "materialize 需要 bindings：场景 id 到用例 id 列表")
    oracles = {oracle["id"]: oracle for oracle in documents["oracles.json"]}
    selected = set()
    for scenario in documents["scenarios.json"]:
        identities = bindings.get(scenario["id"])
        if not isinstance(identities, list) or not identities:
            raise WorkflowError("validation_error", "每个场景都需要可执行用例", scenario_id=scenario["id"])
        expected = [oracles[identity] for identity in scenario["oracle_ids"]]
        for identity in identities:
            case = available.get(identity)
            if not case:
                raise WorkflowError("validation_error", "绑定的用例不存在", case_id=identity)
            actual = oracle_of(case)
            if not any(oracle["kind"] == case["kind"] and oracle["checks"] == actual for oracle in expected):
                raise WorkflowError("oracle_change", "用例检查条件与设计 oracle 不一致，不能用实际结果替换预期", case_id=identity)
            selected.add(identity)
    if any(case["id"] not in selected for case in candidates):
        raise WorkflowError("validation_error", "候选用例必须绑定到本工作流场景")
    if change_scope:
        _change_scope(selected, current, change_scope)
    _functional_mappings(candidates, current, documents, bindings)
    materialized = {"origin": "maintain" if maintain else "materialize", "cases": candidates, "bindings": bindings, "case_ids": sorted(selected),
                    "case_hashes": {identity: digest(available[identity]) for identity in selected},
                    "base_case_hashes": {case["id"]: digest(current[case["id"]]) if case["id"] in current else None for case in candidates}}
    if change_scope:
        materialized["origin"] = "requirement_change"
    return {"materialization.json": materialized}


def proposal(flow: dict, phase: str, payload: dict, change_id: str, current: dict) -> dict:
    if not isinstance(payload, dict):
        raise WorkflowError("validation_error", "提交内容必须是 JSON 对象")
    documents = current["documents"]
    if phase == "sources":
        changed = _sources(payload, documents)
    elif phase == "requirements":
        changed = _requirements(payload, documents)
    elif phase in ("design", "completion"):
        changed = _design(payload, documents, phase == "completion")
    elif phase in ("materialize", "maintain"):
        changed = _materialize(payload, documents, flow["project_id"], phase == "maintain")
    elif phase == "diagnose":
        if payload.get("disposition") not in ("business_defect", "repair", "retry", "incomplete"):
            raise WorkflowError("validation_error", "diagnose disposition 为 business_defect/repair/retry/incomplete")
        if not isinstance(payload.get("reason"), str) or not payload["reason"].strip():
            raise WorkflowError("validation_error", "诊断需要 reason 和证据引用")
        changed = {"diagnosis.json": {**payload, "origin": "host_analysis"}}
    elif phase == "knowledge":
        entries = _records(payload.get("entries"), "entries")
        changed = {"knowledge.json": [{**entry, "origin": "host_analysis", "verification": "hypothesis"} for entry in entries]}
    else:
        raise WorkflowError("validation_error", "该阶段不接受宿主结果", phase=phase)
    definition = {**documents["definition.json"], "version": documents["definition.json"]["version"] + 1, "change_id": change_id}
    root = prefix(flow["id"])
    return {root + key: value for key, value in {**changed, "definition.json": definition}.items()}


def instructions(phase: str) -> dict:
    examples = {
        "sources": {"sources": [{"id": "source-1", "text": "原文提取结果"}]},
        "requirements": {"requirements": [{"id": "req-1", "text": "独立业务预期", "source_ids": ["source-1"]}]},
        "design": {"scenarios": [{"id": "scenario-1", "title": "验证业务预期", "requirement_ids": ["req-1"], "oracle_ids": ["oracle-1"]}],
                   "oracles": [{"id": "oracle-1", "kind": "api", "requirement_ids": ["req-1"], "checks": {"status": 200, "json_equals": {"/priority": "high"}}}]},
        "materialize": {"cases": [], "bindings": {"scenario-1": ["TC-API-001"]}},
        "diagnose": {"disposition": "business_defect", "reason": "原预期与实际不一致，保留失败", "evidence_refs": []},
        "knowledge": {"entries": [{"id": "knowledge-1", "text": "可复用的定位、环境或业务知识", "evidence_refs": []}]},
        "investigate": {"resolution": "incomplete", "observation_operation_ids": [], "note": "先取得真实 check 收据；observed 需匹配原 postcondition，否则保留 incomplete"},
    }
    key = {"completion": "design", "maintain": "materialize"}.get(phase, phase)
    return {"example": examples.get(key, {}),
            "functional_design_fields": ["description", "preconditions", "steps", "expected", "source", "business_path", "author", "remarks"],
            "functional_oracle_mapping": "新功能预期用 oracle.functional_expected 原文声明覆盖范围，并实际更新检查；旧检查沿用不等于新预期已验证。宿主应依据来源逐项审阅，再运行真实回归。",
            "rule": "预期来自需求，保留真实失败；候选先暂存和验证。UI/API 用例自身保存真人功能步骤和真实【按钮】、必要前置条件、可观察预期及来源，"
                    "放入 extensions.test_case_document 并同步描述字段；自动化 IR、请求和断言保留为独立执行定义，缺少设计依据时标记待确认，不用 GET 地址冒充功能操作。"}
