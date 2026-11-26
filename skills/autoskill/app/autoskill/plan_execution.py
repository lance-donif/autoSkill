"""Bind complete design rows to frozen candidates and exact runs without requiring publication."""
from __future__ import annotations

import copy
import json
import re
import uuid
from pathlib import Path

from . import db, execution_inputs, paths, plan, projects, runner, task_context, task_report_data, task_reports
from .locking import named_lock


def _file() -> Path:
    return plan._folder() / "execution-bindings.json"


def _read() -> dict | None:
    from . import task_state

    file = _file()
    task_state._watch(file)
    return json.loads(file.read_bytes()) if file.is_file() else None


def _task_file(task_id: str) -> Path:
    if not re.fullmatch(r"plan-[0-9a-f]{32}", task_id):
        raise ValueError("设计任务标识无效，未写入其他任务")
    return plan._folder() / "execution-tasks" / task_id / "bindings.json"


def _save(value: dict) -> None:
    execution_inputs.write_json(_task_file(value["task_id"]), value)
    current = _read()
    if current and current["task_id"] == value["task_id"]:
        execution_inputs.write_json(_file(), value)


def for_run(run_id: str) -> dict | None:
    """Find the original task binding after the active sheet has been replaced."""
    for file in (plan._folder() / "execution-tasks").glob("*/bindings.json"):
        value = json.loads(file.read_bytes())
        if any(entry.get("task_id") == value["task_id"] and entry["run_id"] == run_id for entry in value["bindings"]):
            return value
    return None


def sync_original(run_id: str) -> dict:
    original = for_run(run_id)
    if original:
        return {"updated": 0, "archived_task": True, "note": "只回填此运行原设计任务的正式 Excel；当前任务保持不变",
                **publish(original)}
    return {"updated": 0, "note": "此运行没有绑定到当前设计任务，未回填其他任务结果"}


def _registry() -> Path:
    return paths.local() / "case-plans" / "registered.json"


def register(value: dict) -> None:
    with named_lock("case-plans", "registered"):
        file = _registry()
        rows = json.loads(file.read_bytes()) if file.is_file() else []
        entry = {"project_id": value["project_id"], "session": value["session"], "task_id": value["task_id"],
                 "folder": runner.plan_reference(plan._folder()), "updated_at": db.now()}
        rows = [row for row in rows if (row["project_id"], row["session"]) != (entry["project_id"], entry["session"])]
        execution_inputs.write_json(file, [*rows, entry])


def registered(project_id: str, session: str = "") -> tuple[dict | None, list[dict]]:
    from . import task_state

    file = _registry()
    task_state._watch(file)
    values = json.loads(file.read_bytes()) if file.is_file() else []
    matches = []
    for value in reversed(values):
        folder = runner.plan_folder(value["folder"])
        binding_file = folder / "execution-bindings.json"
        if value["project_id"] != project_id or not binding_file.is_file():
            continue
        stored = json.loads(binding_file.read_bytes())
        if stored.get("task_id") == value["task_id"] and stored.get("project_id") == project_id:
            matches.append({**value, "folder": str(folder)})
    chosen = next((value for value in matches if value["session"] == session), None) if session else next(iter(matches), None)
    return chosen, matches


def existing_delivery(project_id: str, items: list[dict], source_doc_hash: str | None = None) -> dict | None:
    """The same session's active design for the same document version and identical case content."""
    chosen, _ = registered(project_id, task_context.session_id() or "")
    if chosen is None or not source_doc_hash:
        return None
    value = _read()
    if (not value or value.get("task_id") != chosen["task_id"] or value.get("project_id") != project_id
            or value.get("source_doc_hash") != source_doc_hash
            or value.get("content_hash") != execution_inputs.content_hash(items)):
        return None
    return value


def _definition(item: dict, number: int) -> dict:
    identity = item.get("id") or f"TC-{number:03d}"
    document = {key: copy.deepcopy(item[key]) for key in ("description", "preconditions", "steps", "expected", "source", "business_path", "prefix", "author", "remarks") if item.get(key)}
    document.setdefault("description", item["title"])
    return {**{key: copy.deepcopy(value) for key, value in item.items() if key not in ("status", "note", "replay", "execution_source")},
            "id": identity, "kind": "scenario", "module": item.get("module") or "未分组", "extensions": {"test_case_document": document}}


def _design_hash(row: dict) -> str:
    fields = [key for key, _, _ in plan.COLUMNS[:8]] + ["description", "requirement_ids", "source", "business_path"]
    return execution_inputs.content_hash({key: row.get(key) for key in fields})


def execution_sources(rows: list[dict]) -> dict[str, dict]:
    value = _read()
    if not value:
        return {}
    original = {saved["definition"]["id"]: saved for saved in value["rows"]}
    return {row["id"]: {"kind": "case_plan", "file": str(plan._file()), "case_id": row["id"], "task_id": value["task_id"],
                        "business_row_hash": plan._source_hash(row), **({"upstream": copy.deepcopy(row["source"])} if row.get("source") else {})}
            for row in rows if row["id"] in original and _design_hash(row) == original[row["id"]]["design_hash"]}


def initialize(project_id: str, info: dict, items: list[dict], destination: Path, *, task_id: str | None = None,
               source_doc_hash: str | None = None) -> dict:
    task_id = task_id or "plan-" + uuid.uuid4().hex
    initial = destination.with_name(destination.stem + "-initial-" + task_id[-8:] + ".xlsx")
    current = {row["id"]: row for row in plan.show()["cases"]}
    value = {"schema": 1, "task_id": task_id, "project_id": project_id, "session": task_context.session_id() or "",
             "content_hash": execution_inputs.content_hash(items), "source_doc_hash": source_doc_hash,
             "project": {"id": project_id, "name": info["name"], "base_url": info["base_url"]},
             "delivery_excel": runner.plan_reference(destination), "initial_excel": runner.plan_reference(initial), "bindings": [],
             "rows": [{"definition": _definition(item, number), "source_row": current[item.get("id") or f"TC-{number:03d}"],
                       "row_hash": plan._source_hash(current[item.get("id") or f"TC-{number:03d}"]),
                       "design_hash": _design_hash(current[item.get("id") or f"TC-{number:03d}"])}
                      for number, item in enumerate(items, 1)]}
    execution_inputs.write_json(_file(), value)
    _save(value)
    register(value)
    return publish(value, initial=initial)


def _matches(value: dict, row: dict) -> list[dict]:
    return [entry for entry in value["bindings"] if entry.get("task_id") == value["task_id"]
            and entry["plan_case"] == row["id"] and entry["row_hash"] == plan._source_hash(row)
            and entry.get("design_hash") == _design_hash(row)]


def _binding(value: dict, row: dict, candidate: dict, run_id: str | None, candidate_id: str | None = None) -> dict:
    return {"task_id": value["task_id"], "plan_case": row["id"], "row_hash": plan._source_hash(row), "design_hash": _design_hash(row),
            "candidate_id": candidate_id, "case_id": candidate["id"], "candidate_hash": execution_inputs.content_hash(candidate),
            "candidate": copy.deepcopy(candidate), "run_id": run_id, "bound_at": db.now()}


def capture_linked(prepared: dict, manifest: dict) -> None:
    """Bind this task's explicit rows to selected cases and their planned prerequisites."""
    value = _read()
    if not value or value["project_id"] != prepared["project_id"]:
        return
    originals = {saved["definition"]["id"]: saved for saved in value["rows"]}
    planned = {identity for identity, _ in runner.build_plan(manifest["selected"], manifest["cases"])}
    changed = False
    for row in plan.show()["cases"]:
        project_id, _, case_id = row.get("replay", "").partition("/")
        saved = originals.get(row["id"])
        if project_id != prepared["project_id"] or case_id not in planned or not saved or _design_hash(row) != saved["design_hash"]:
            continue
        binding = _binding(value, row, manifest["cases"][case_id], prepared["id"])
        binding["linked_existing"] = True
        if not any(entry["plan_case"] == row["id"] and entry["run_id"] == prepared["id"] for entry in value["bindings"]):
            value["bindings"].append(binding)
            changed = True
    if changed:
        _save(value)


def _check_source(declared, row: dict, shown: dict) -> None:
    if not isinstance(declared, dict):
        return
    if declared.get("business_row_hash") and declared["business_row_hash"] != plan._source_hash(row):
        raise ValueError("录制期间设计已经变化；保留候选和运行证据，不将旧结果套到新设计")
    if declared.get("kind") != "case_plan":
        return
    if declared.get("case_id", row["id"]) != row["id"] or (declared.get("file") and runner.plan_folder(declared["file"]) != plan._file().resolve()):
        raise ValueError("录制绑定的设计表或行与当前选择不同，未回填执行结果")
    if declared.get("design_hash") and declared["design_hash"] != _design_hash(row):
        raise ValueError("录制期间功能设计已经变化，未将旧候选绑定到新设计")
    if "design_version" in declared and declared["design_version"] != shown.get("design_version"):
        raise ValueError("录制期间在线设计版本已经变化，未将旧候选绑定到新版本")
    if (declared.get("upstream") or None) != (row.get("source") or None):
        raise ValueError("录制期间设计来源已经变化，未将旧候选绑定为当前设计结果")
    if declared.get("task_id") != (shown.get("task_id") or shown.get("draft_task_id")):
        raise ValueError("录制属于另一份设计任务；保留原候选和运行证据，不回填到当前新任务")


def bind(plan_case: str, project_id: str, candidate_case: dict, run_id: str | None, *, candidate_id: str | None = None) -> dict:
    shown = plan.show(recording=True)
    if shown.get("project") != project_id:
        raise ValueError("用例设计表属于其他项目，未绑定执行")
    row = next((item for item in shown["cases"] if item["id"] == plan_case), None)
    if row is None:
        raise ValueError("设计表中没有该用例，未绑定执行")
    declared = candidate_case.get("extensions", {}).get("test_case_document", {}).get("source", {})
    value = _read()
    digest = execution_inputs.content_hash(candidate_case)
    _check_source(declared, row, shown)
    if run_id:
        run = db.one("SELECT * FROM runs WHERE id = ?", (run_id,))
        if not run or run["project_id"] != project_id:
            raise ValueError("指定运行不存在或属于其他项目")
        frozen = execution_inputs.load(run)["cases"].get(candidate_case["id"])
        if not frozen or execution_inputs.content_hash(frozen) != digest:
            raise ValueError("候选内容与指定运行的冻结输入不同，未绑定设计结果")
    if not value or value["project_id"] != project_id:
        destination = plan._folder() / "exports" / ("functional-design-" + uuid.uuid4().hex[:16] + ".xlsx")
        initialize(project_id, projects.get(project_id), shown["cases"], destination, task_id=shown.get("draft_task_id"))
        value = _read()
    binding = _binding(value, row, candidate_case, run_id, candidate_id)
    identity = (plan_case, digest, run_id)
    if not any((entry["plan_case"], entry["candidate_hash"], entry["run_id"]) == identity for entry in value["bindings"]):
        value["bindings"].append(binding)
    _save(value)
    book, sheet, columns = plan._open()
    try:
        number = plan._find(sheet, columns, plan_case)
        sheet.cell(number, columns["replay"], project_id + "/" + candidate_case["id"]).data_type = "s"
        plan._save(book)
    finally:
        book.close()
    return {"case": plan_case, "task_id": value["task_id"], "candidate_id": candidate_id, "run_id": run_id, "candidate_hash": digest}


def _item(binding: dict, definition: dict) -> dict:
    observed = binding["candidate"].get("extensions", {}).get("recording_result")
    item = {"definition": definition, "result": None, "run_id": binding["run_id"], "case_id": binding["case_id"], "evidence_valid": False,
            "recording_result": copy.deepcopy(observed), "note": ""}
    if not binding["run_id"]:
        item["recording_result"] = observed or {"status": "Unverified", "reason": "候选尚未回放"}
        return item
    run = db.one("SELECT * FROM runs WHERE id = ?", (binding["run_id"],))
    if not run:
        note = "关联运行已删除，保留此前事实，不能作为当前通过证据" if binding.get("last_result") else "准备的运行尚无执行回执（未入队或已删除），本行仍未执行"
        return {**item, "historical_result": binding.get("last_result"), "note": note}
    item.update(run_status=run["status"], finished_at=run.get("finished_at"))
    rows = db.rows("SELECT * FROM results WHERE run_id = ? AND case_id = ? ORDER BY id", (run["id"], binding["case_id"]))
    if not rows:
        return {**item, "note": "指定运行尚无本行结果：" + (run.get("reason") or run["status"])}
    result = next((row for row in rows if row["status"] == "Fail"), rows[-1])
    result["detail"] = json.loads(result["detail"])
    valid = execution_inputs.result_evidence(run, result, binding["candidate"])
    binding["last_result"] = {"status": result["status"], "business_status": result["detail"].get("business_status"), "reason": result["reason"]}
    return {**item, "result": result, "evidence_valid": valid,
            "note": "" if valid else "指定运行的执行证据不足或已变化，不能作为通过证明"}


def _execution_receipt(item: dict) -> dict:
    from . import candidate_publication

    result = item["result"]
    detail = result.get("detail", {})
    try:
        publication = candidate_publication.receipt(item["run_id"])
    except (OSError, ValueError) as error:
        publication = {"status": "unknown", "complete": False, "published_ids": [], "error": str(error)}
    return {"run_id": item["run_id"], "result_id": result["id"], "case_id": item["case_id"],
            "status": task_report_data.observation_verdict(item), "run_status": item["run_status"],
            "evidence_valid": bool(item["evidence_valid"]), "actual": result.get("actual") or json.dumps(detail.get("assertions") or [], ensure_ascii=False),
            "reason": result.get("reason") or item.get("note", ""), "finished_at": item.get("finished_at"), "publication": publication}


def items_for_row(value: dict, row: dict, definition: dict) -> list[dict]:
    bindings = _matches(value, row)
    if bindings:
        return [_item(binding, definition) for binding in bindings]
    return [unexecuted_item(row, definition)]


def unexecuted_item(row: dict, definition: dict) -> dict:
    """Preserve an explicit blocker without treating a manual mark as execution proof."""
    item = {"definition": definition, "result": None, "run_id": None, "evidence_valid": False}
    if row.get("note"):
        item["note"] = row["note"] + "；没有本行执行回执"
    return item


def evidence(project_id: str, row: dict) -> dict | None:
    value = _read()
    if not value or value["project_id"] != project_id or not any(entry["plan_case"] == row["id"] for entry in value["bindings"]):
        return None
    if not _matches(value, row):
        return {"status": "NotRun", "actual": "", "executor": "", "environment": "", "screenshots": [],
                "reason": "设计已变化；历史候选和结果保留在原任务报告，新设计需要重新执行", "result": None,
                "run_id": None, "materialized_case_id": None, "binding_current": False, "current": False, "candidate_bound": True, "items": []}
    group = task_report_data.logical_cases({"project_id": project_id, "cases": items_for_row(value, row, _definition(row, 1))})[0]
    history = [_execution_receipt(item) for item in group["items"] if item.get("result") and item.get("run_id")]
    history_fields = {"execution_history": history, "latest_execution": history[-1] if history else None,
                      "has_historical_failure": any(item["status"] == "Fail" for item in history[:-1])
                      or any((item.get("recording_result") or {}).get("status") == "Fail" or (item.get("historical_result") or {}).get("status") == "Fail"
                             for item in group["items"])}
    last = next((item for item in reversed(group["items"]) if item["result"]), group["items"][-1])
    result = last["result"] or {}
    latest_binding = _matches(value, row)[-1]
    if latest_binding.get("linked_existing") and result:
        current = plan._current_cases(project_id).get(latest_binding["case_id"])
        if not current or not execution_inputs.result_scope_current(project_id, current, result):
            return {"status": "NotRun", "actual": "", "executor": "", "environment": "", "screenshots": [],
                    "reason": "关联用例已修改或不再有效，需重新回放；历史结果仍保留在原任务报告", "result": last["result"],
                    "run_id": last["run_id"], "materialized_case_id": latest_binding["case_id"], "binding_current": False,
                    "current": False, "candidate_bound": True, "items": [], **history_fields}
    detail = result.get("detail", {})
    reason = "；".join(dict.fromkeys(filter(None, [*(item.get("note", "") for item in group["items"]),
                                                      *(str((item.get("result") or {}).get("reason", "")) for item in group["items"])])))
    observed = [item["recording_result"] for item in group["items"] if item.get("recording_result")]
    if observed:
        reason = "；".join(filter(None, [reason, "录制观察保留，回放结果单独判定"]))
    actual = "\n".join(f"{entry['run_id']}：{plan.STATUS[entry['status']]}；{entry['actual'] or entry['reason']}" for entry in history)
    return {"status": group["status"], "actual": actual or json.dumps(observed, ensure_ascii=False),
            "executor": "autoSkill" if result else "", "environment": value["project"].get("base_url", ""),
            "screenshots": [f"/api/runs/{last['run_id']}/files/{name}" for name in detail.get("screenshots", [])],
            "reason": reason, "result": last["result"], "run_id": last["run_id"],
            "materialized_case_id": latest_binding["case_id"], "binding_current": True,
            "current": bool(last["evidence_valid"]), "candidate_bound": True, "items": group["items"], **history_fields}


def publish(value: dict, *, initial: Path | None = None) -> dict:
    active = _read()
    shown = plan.show() if active and active["task_id"] == value["task_id"] else {}
    current = {row["id"]: row for row in shown.get("cases", [])}
    cases = []
    for saved in value["rows"]:
        definition = saved["definition"]
        row = current.get(definition["id"])
        original = row if row and _design_hash(row) == saved["design_hash"] else saved.get("source_row")
        if original is row and row:
            saved["source_row"] = copy.deepcopy(row)
        if original:
            cases.extend(items_for_row(value, original, definition))
        else:
            cases.append({"definition": definition, "result": None, "run_id": None, "evidence_valid": False,
                          "note": "当前设计已变化；本任务仍保留最初设计，不把其他版本结果套入"})
    snapshot = {"schema": 1, "kind": "design", "task_id": value["task_id"], "project_id": value["project_id"], "project": value["project"],
                "title": "本次完整用例表与实际结果", "status": "Design", "cases": cases,
                "results": [item["result"] for item in cases if item["result"]],
                "run_ids": list(dict.fromkeys(item["run_id"] for item in cases if item["run_id"])),
                "sources": {"design_rows": [{"id": saved["definition"]["id"], "row_hash": saved["row_hash"]} for saved in value["rows"]]},
                "notes": ["完整保留初始设计、原步骤与预期；只有绑定候选和指定运行的证据写入结果，失败不会因未入库而消失。"], "metadata": {}}
    head = _task_file(value["task_id"]).with_name("delivery-head.json")
    receipt = task_reports.for_design(snapshot, runner.plan_folder(value["delivery_excel"]), head, initial=initial)
    value["report_id"] = receipt["report_id"]
    if initial is not None:
        value["initial_report_id"] = receipt["report_id"]
    elif shown.get("initial_report_id"):
        value["initial_report_id"] = shown["initial_report_id"]
    _save(value)
    return _delivery_view(value, receipt)


def _delivery_view(value: dict, report: dict | None) -> dict:
    initial_report_id = value.get("initial_report_id")
    if not initial_report_id and report:
        initial_report_id = (report["report_id"] if report.get("initial_excel") and report.get("task_id") == value["task_id"]
                             else _legacy_initial_report(value))
    return {"delivery_excel": str(runner.plan_folder(value["delivery_excel"])), "initial_excel": str(runner.plan_folder(value["initial_excel"])),
            "initial_report_id": initial_report_id, "test_report": report, "task_id": value["task_id"]}


def _legacy_initial_report(value: dict) -> str | None:
    """Identify a pre-pointer first delivery from its own retained receipt, including failed exports."""
    project_id = value["project_id"]
    if not projects.ID.fullmatch(project_id):
        return None
    expected = runner.plan_folder(value["initial_excel"])
    matches = set()
    for folder in (paths.projects_dir() / project_id / "reports", paths.local() / "task-reports"):
        for directory in folder.glob("design_*"):
            try:
                receipt = task_reports._saved(directory.name)
                if (isinstance(receipt, dict) and receipt.get("report_id") == directory.name and receipt.get("kind") == "design"
                        and receipt.get("task_id") == value["task_id"] and receipt.get("project_id") == project_id
                        and receipt.get("initial_excel") and runner.plan_folder(receipt["initial_excel"]) == expected):
                    matches.add(directory.name)
            except (OSError, TypeError, ValueError):
                continue
    return matches.pop() if len(matches) == 1 else None


def _selected_report(value: dict) -> dict | None:
    """The selected export can outlive a writer that never updated the task binding."""
    recorded = value.get("report_id")
    try:
        head = task_reports._design_head(_task_file(value["task_id"]).with_name("delivery-head.json"))
    except (OSError, ValueError):
        head = {}
    if head and head["report_id"] != recorded and (not recorded or head.get("previous_report_id") == recorded):
        try:
            selected = task_reports.get(head["report_id"])
        except (OSError, ValueError) as error:
            return {"report_id": head["report_id"], "status": "failed", "retryable": True, "error": str(error)}
        if selected.get("task_id") != value["task_id"] or selected.get("project_id") != value["project_id"]:
            return {"report_id": head["report_id"], "status": "failed", "retryable": False, "error": "设计表报告与原任务不匹配"}
        return selected
    return task_reports.get(recorded) if recorded else None


def describe() -> dict:
    value = _read()
    if not value:
        return {}
    try:
        report = _selected_report(value)
    except ValueError as error:
        report = {"status": "failed", "error": str(error), "report_id": value.get("report_id")}
    return _delivery_view(value, report)


def previous_deliveries(project_id: str, session: str) -> list[dict]:
    """Read archived task receipts on demand; never render reports or borrow another session's task."""
    active = _read()
    rows = []
    for file in (plan._folder() / "execution-tasks").glob("*/bindings.json"):
        value = json.loads(file.read_bytes())
        if value.get("project_id") != project_id or value.get("session") != session or (active and value.get("task_id") == active["task_id"]):
            continue
        report_id = value.get("report_id")
        title = None
        try:
            report = _selected_report(value)
            if not report:
                continue
            report_id = report["report_id"]
            snapshot = json.loads((task_reports._folder(report_id) / "snapshot.json").read_bytes())
            if report.get("task_id") != value["task_id"] or snapshot.get("task_id") != value["task_id"] or snapshot.get("project_id") != project_id:
                raise ValueError("报告归属与原设计任务不一致，未提供其他任务的文件")
            title = snapshot.get("title")
        except (ValueError, OSError) as error:
            report = {"report_id": report_id, "status": "failed", "error": str(error)}
        rows.append({"task_id": value["task_id"], "title": title, "report": report})
    return sorted(rows, key=lambda row: row["report"].get("generated_at", ""), reverse=True)
