"""Capture task-scoped report facts independently of spreadsheet templates."""
from __future__ import annotations

import contextlib
import copy
import hashlib
import json
from pathlib import Path, PurePosixPath

from . import data_refs, db, execution_inputs, paths

VERDICTS = ("Pass", "Fail", "Blocked", "Cancelled", "Unverified", "NotRun", "Unknown")


def case_verdict(item: dict) -> str:
    """Separate observed execution from verified business success without erasing a failure."""
    result = item.get("result") or {}
    historical = item.get("historical_result") or {}
    if historical.get("business_status") == "Fail" or historical.get("status") == "Fail":
        return "Fail"
    status = result.get("status")
    if not result:
        return "NotRun"
    if result.get("detail", {}).get("business_status") == "Fail":
        return "Fail"
    if status == "Pass" and (not item.get("evidence_valid") or result.get("detail", {}).get("business_status") != "Pass"):
        return "Unverified"
    return status if status in VERDICTS else "Unknown"


def logical_cases(snapshot: dict) -> list[dict]:
    """Group repeated observations by original identity and exact frozen definition."""
    groups = {}
    for item in snapshot["cases"]:
        definition = item["definition"]
        version = execution_inputs.content_hash(definition)
        key = execution_inputs.content_hash([snapshot["project_id"], definition["id"], version])
        group = groups.setdefault(key, {"key": key, "definition_hash": version, "definition": definition, "items": []})
        group["items"].append(item)
    for group in groups.values():
        verdicts = [case_verdict(item) for item in group["items"]]
        group.update(status="Fail" if "Fail" in verdicts else verdicts[-1], first_status=verdicts[0], final_status=verdicts[-1],
                     run_ids=list(dict.fromkeys(item["run_id"] for item in group["items"] if item.get("run_id"))))
    return list(groups.values())


def task_summary(snapshot: dict) -> dict:
    """Expose one verdict and concise facts per definition to every delivery renderer."""
    groups = logical_cases(snapshot)
    counts = {status: sum(group["status"] == status for group in groups) for status in VERDICTS}
    issues, environments = [], []
    for group in groups:
        reasons = []
        for item in group["items"]:
            if reason := (item.get("result") or {}).get("reason") or item.get("note"):
                reasons.append(reason)
            if (item.get("recording_result") or {}).get("status") == "Fail":
                reasons.append("录制时已观察到断言失败，尚未回放，原预期和实际观察保留。")
            environments.extend(item.get("environment", []))
        if group["status"] != "Pass" or reasons:
            issues.append({"case_id": group["definition"]["id"], "status": group["status"],
                           "reason": "；".join(dict.fromkeys(reasons)), "run_ids": group["run_ids"]})
    environments.extend(snapshot.get("environment", []))
    if not environments:
        if base_url := snapshot.get("project", {}).get("base_url"):
            environments.append("被测地址：" + str(base_url))
        if engine := snapshot.get("metadata", {}).get("engine"):
            environments.append("任务引擎：" + str(engine))
    return {"cases": len(groups), "execution_rows": len(snapshot["cases"]), "counts": counts, "issues": issues,
            "run_ids": snapshot.get("run_ids", []), "environment": list(dict.fromkeys(environments))}


def _json(value, default):
    return json.loads(value) if isinstance(value, str) else value if value is not None else default


def _rows(run_id: str) -> list[dict]:
    rows = db.rows("SELECT * FROM results WHERE run_id = ? ORDER BY id", (run_id,))
    for row in rows:
        row["detail"] = _json(row.get("detail"), {})
    return rows


def _project(project_id: str) -> dict:
    from . import projects

    try:
        info = projects.get(project_id)
        return {key: info.get(key, "") for key in ("id", "name", "base_url")}
    except (OSError, ValueError):
        return {"id": project_id, "name": project_id, "base_url": ""}


def _replay(run: dict, results: list[dict]) -> tuple[list[dict], dict, list[str]]:
    notes = []
    try:
        manifest = execution_inputs.load(run)
    except (OSError, ValueError):
        manifest = {"selected": _json(run.get("case_ids"), []), "cases": {}, "sources": {}}
        notes.append("冻结输入或来源证据不可用；只列本次选择与保留结果，不用当前项目定义替代历史输入。")
    selected = manifest["selected"]
    by_case = {row["case_id"]: row for row in results if not row["prerequisite"]}
    cases = []
    for identity in selected:
        result = by_case.get(identity)
        definition = manifest["cases"].get(identity, {"id": identity, "title": result["case_title"] if result else identity})
        valid = bool(result) and execution_inputs.result_evidence(run, result)
        cases.append({"definition": definition, "result": result, "run_id": run["id"], "evidence_valid": valid,
                      "note": "任务结束时没有此用例的实际结果；" + (run.get("reason") or run["status"]) if not result else ""})
    sources = {**manifest.get("sources", {}), "execution_environment": {
        "base_url": manifest.get("config", {}).get("base_url"), "engine": manifest.get("engine", {}).get("adapter"),
        "python": manifest.get("engine", {}).get("python")}}
    return cases, sources, notes


def _load(run: dict, results: list[dict]) -> tuple[list[dict], dict, list[str]]:
    cases, sources, notes = _replay(run, results)
    try:
        manifest = execution_inputs.load(run)
    except (OSError, ValueError):
        return cases, sources, notes
    cases = []
    observed = {}
    for row in results:
        if row["prerequisite"]:
            continue
        detail = row.get("detail", {})
        observed[(row["case_id"], detail.get("load_iteration"))] = row
    for iteration in range(1, manifest["load"]["iterations"] + 1):
        for identity in manifest["selected"]:
            result = observed.get((identity, iteration))
            cases.append({"definition": manifest["cases"][identity], "result": result, "run_id": run["id"],
                          "evidence_valid": bool(result) and execution_inputs.result_evidence(run, result),
                          "note": f"压测第 {iteration} 次迭代没有此用例的实际结果；" + run["status"] if not result else ""})
    sources["load"] = manifest["load"]
    notes.append("压测逐次保留全部目标执行，较早失败不会被后续成功覆盖；前置用例不计为压测目标。")
    return cases, sources, notes


def _native(run: dict, results: list[dict]) -> tuple[list[dict], dict, list[str]]:
    from . import native_run

    notes = []
    manifest = {}
    try:
        manifest = native_run._frozen(run)
        sources = {key: manifest.get(key) for key in ("asset_id", "asset_version", "head", "asset", "frozen_files", "planning")}
        sources["execution_environment"] = {"engine": run["engine"], "runtime_version": manifest.get("runtime", {}).get("version")}
    except (OSError, ValueError):
        sources = {}
        notes.append("原生冻结输入不可用；保留实际结果，不能据此宣称输入证据完整。")
    outcome_file = paths.local() / "runs" / run["id"] / "native" / "outcome.json"
    outcome = json.loads(outcome_file.read_bytes()) if outcome_file.is_file() else {}
    evidence_valid = _native_evidence(run, manifest, outcome, results)
    if not evidence_valid:
        notes.append("原生输入或引用证据未通过完整性检查；执行器保留结果仍展示，不能据此宣称业务已验证。")
    designed = _ai_definitions(manifest) if run["engine"] == "ai-eval" and manifest else {}
    cases = [{"definition": designed.get(row["case_id"], {"id": row["case_id"], "title": row["case_title"], "kind": "native",
                              "engine": run["engine"], "native_asset": {key: sources.get(key) for key in ("asset_id", "asset_version", "head")}}),
              "result": row, "run_id": run["id"], "evidence_valid": evidence_valid} for row in results]
    observed = {row["case_id"] for row in results}
    cases.extend({"definition": definition, "result": None, "run_id": run["id"], "evidence_valid": False}
                 for identity, definition in designed.items() if identity not in observed)
    planned = outcome.get("metrics", {}).get("planned_cases")
    if planned is None:
        notes.append("原生引擎未提供可枚举的完整计划用例数；表中只包含已发现结果，不把文件数或空表当作全部覆盖。")
    elif planned > len(results):
        notes.append(f"本次计划 {planned} 条，保留 {len(results)} 条结果；其余 {planned - len(results)} 条未产生实际执行结果。")
    if not cases:
        cases.append({"definition": {"id": sources.get("asset_id") or "native-task", "title": "原生任务（尚未发现用例）", "kind": "native"},
                      "result": None, "run_id": run["id"], "evidence_valid": False,
                      "note": "未产生用例结果；任务状态：" + run["status"] + "；" + (run.get("reason") or "")})
    sources["outcome"] = {key: value for key, value in outcome.items() if key != "cases"}
    return cases, sources, notes


def _file_matches(file: Path, reference: dict) -> bool:
    if "size" in reference and file.stat().st_size != reference["size"]:
        return False
    with file.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest() == reference["sha256"]


def _native_evidence(run: dict, manifest: dict, outcome: dict, results: list[dict]) -> bool:
    if not manifest or not outcome.get("evidence"):
        return False
    folder = (paths.local() / "runs" / run["id"] / "native").resolve()
    try:
        for reference in manifest["frozen_files"]:
            if not _file_matches(paths.local() / "native-inputs" / "objects" / reference["sha256"], reference):
                return False
        for reference in outcome["evidence"]:
            if not isinstance(reference, dict):
                return False
            value = reference["path"]
            file = (data_refs.resolve(value) if data_refs.is_absolute(value) else folder / value).resolve()
            if not file.is_relative_to(folder) or not _file_matches(file, reference):
                return False
        originals = {str(case["id"]): case for case in outcome.get("cases", [])}
        for row in results:
            original = originals.get(row["case_id"])
            if (not original or row["detail"].get("input_hash") != run["input_hash"]
                    or row["detail"].get("evidence") != outcome["evidence"]):
                return False
            if any(row["detail"].get(key) != value for key, value in original.items()):
                return False
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return True


def _ai_definitions(manifest: dict) -> dict:
    asset = manifest["asset"]
    name = (PurePosixPath(asset.get("cwd", ".")) / asset["argv"][0]).as_posix()
    reference = next((ref for ref in manifest["frozen_files"] if ref["path"] == name), None)
    if not reference:
        return {}
    data = (paths.local() / "native-inputs" / "objects" / reference["sha256"]).read_bytes()
    if hashlib.sha256(data).hexdigest() != reference["sha256"]:
        raise ValueError("AI 对话用例冻结定义校验失败")
    spec = {**json.loads(data), **manifest.get("parameters", {})}
    definitions = {}
    for iteration in range(spec.get("load", {}).get("iterations", 1)):
        for number, sample in enumerate(spec.get("samples", []), 1):
            identity = f"{sample.get('id', number)}:{iteration + 1}"
            turns = sample.get("turns", [sample])
            definitions[identity] = {"id": identity, "title": sample.get("title", sample.get("id", "AI 多轮对话")), "kind": "native",
                                     "turns": turns, "assertions": [{key: turn[key] for key in ("expected", "reference", "checks", "performance") if key in turn} for turn in turns],
                                     "context": sample.get("context", {}), "prompt": spec.get("prompt", ""), "dataset": spec.get("dataset"),
                                     "extensions": {"test_case_document": sample.get("test_case_document", {})}}
    return definitions


def run_snapshot(run: dict) -> dict:
    results = _rows(run["id"])
    capture = {"native": _native, "load": _load}.get(run.get("family"), _replay)
    cases, sources, notes = capture(run, results)
    labels = {"base_url": "被测地址", "engine": "任务引擎", "python": "Python", "runtime_version": "运行时版本"}
    environment = [labels[key] + "：" + str(value) for key, value in sources.get("execution_environment", {}).items() if value]
    for item in cases:
        item["environment"] = environment
        item["evidence_directory"] = data_refs.store(paths.local() / "runs" / run["id"])
    metadata = {key: run.get(key) for key in ("id", "family", "engine", "trigger", "task_id", "created_at", "started_at",
                                           "finished_at", "reason", "total", "done", "input_hash", "input_manifest")}
    if metadata.get("input_manifest"):
        # Missing legacy mappings remain provenance; a report must not invent an input location.
        with contextlib.suppress(ValueError):
            metadata["input_manifest"] = data_refs.store(metadata["input_manifest"])
    return {"schema": 1, "kind": "run", "task_id": run["id"], "project_id": run["project_id"], "project": _project(run["project_id"]),
            "title": "本次测试任务", "status": run["status"], "session_id": run.get("session_id", ""),
            "run_ids": [run["id"]], "cases": cases, "results": results, "sources": sources, "notes": notes, "environment": environment,
            "metadata": metadata}


def recording_identity(project_id: str, recording: dict, cases: list[dict]) -> str:
    return execution_inputs.content_hash({"project_id": project_id, "started_at": recording.get("started_at"),
                                          "session_id": recording.get("session_id"), "rand": recording.get("rand"), "cases": [case["id"] for case in cases]})


def recording_snapshot(project_id: str, recording: dict, cases: list[dict]) -> dict:
    identity = recording_identity(project_id, recording, cases)
    return {"schema": 1, "kind": "recording", "task_id": identity, "project_id": project_id, "project": _project(project_id),
            "title": "本次录制生成的测试用例", "status": "NotRun", "session_id": recording.get("session_id") or "",
            "run_ids": [], "cases": [{"definition": copy.deepcopy(case), "result": None, "evidence_valid": False,
                                       "recording_result": case.get("extensions", {}).get("recording_result")} for case in cases],
            "results": [], "sources": {key: recording.get(key) for key in ("started_at", "start_url", "workflow_binding", "operations")},
            "notes": ["这是本次录制保存的定义快照，尚未回放。录制中的断言观察单独保留，不借用其他任务的最近结果。"],
            "metadata": {"started_at": recording.get("started_at"), "case_ids": [case["id"] for case in cases]}}


def _missing_cases(context: dict, run_id: str) -> list[dict]:
    materialized = context.get("materialization", {})
    definitions = {case["id"]: case for case in materialized.get("cases", [])}
    old = {row["case_id"]: row for row in context.get("summary", {}).get("results", [])}
    return [{"definition": definitions.get(identity, {"id": identity, "title": identity}), "result": None, "run_id": run_id,
             "evidence_valid": False, "evidence_deleted": True, "historical_result": old.get(identity),
             "note": "关联运行或执行证据已删除，历史摘要不能作为当前通过证据。"} for identity in materialized.get("case_ids", [])]


def _unexecuted(documents: dict, covered: set[str]) -> list[dict]:
    materialized = documents.get("materialization.json", {})
    cases = {case["id"]: case for case in materialized.get("cases", [])}
    rows = [{"definition": cases.get(identity, {"id": identity, "title": identity}), "result": None, "evidence_valid": False}
            for identity in materialized.get("case_ids", []) if identity not in covered]
    rows.extend({"definition": {"id": scenario["id"], "title": scenario["title"], "kind": "scenario"},
                 "result": None, "evidence_valid": False, "note": "设计场景尚未关联可执行用例。", "scenario_ids": [scenario["id"]]}
                for scenario in documents.get("scenarios.json", []) if not materialized.get("bindings", {}).get(scenario["id"]))
    return rows


def workflow_snapshot(flow: dict) -> dict:
    from . import workflow_definition

    generation = workflow_definition.snapshot(flow, flow["domain_head"])
    documents = generation["documents"]
    cases, results, run_ids, notes = [], [], [], []
    items = db.rows("SELECT * FROM workflow_items WHERE workflow_id = ? AND cycle_id = ? ORDER BY sequence", (flow["id"], flow["cycle_id"]))
    for item in items:
        if item["phase"] != "execute":
            continue
        context = _json(item["context"], {})
        identity = item["run_id"] or context.get("evidence_deleted", {}).get("run_id")
        if not identity:
            continue
        run = db.one("SELECT * FROM runs WHERE id = ?", (identity,))
        if not run or run.get("delete_requested"):
            rows = _missing_cases(context, identity)
        else:
            snapshot = run_snapshot(run)
            rows = snapshot["cases"]
            results.extend(snapshot["results"])
            notes.extend(snapshot["notes"])
            run_ids.append(identity)
        bindings = context.get("materialization", {}).get("bindings", {})
        for row in rows:
            row.update(workflow_item_id=item["id"], validation_round=context.get("round"),
                       scenario_ids=[key for key, identities in bindings.items() if row["definition"]["id"] in identities])
        cases.extend(rows)
    cases.extend(_unexecuted(documents, {row["definition"]["id"] for row in cases}))
    if not cases:
        notes.append("本周期尚未形成测试用例；此报告保留任务来源与未完成状态，不表示测试覆盖已经完成。")
    return {"schema": 1, "kind": "workflow", "task_id": flow["id"], "cycle_id": flow["cycle_id"], "project_id": flow["project_id"],
            "project": _project(flow["project_id"]), "title": flow["title"], "status": flow["conclusion"], "run_ids": run_ids,
            "cases": cases, "results": results, "sources": {"head": generation["head"], "definition_hash": generation["hash"], "documents": documents},
            "notes": list(dict.fromkeys([*notes, "只汇总本工作流当前周期的任务输入和实际结果；各轮分别保留，后续成功不会覆盖先前失败。"])),
            "metadata": {key: flow.get(key) for key in ("id", "cycle_id", "lifecycle", "conclusion", "revision", "created_at", "updated_at")}}
