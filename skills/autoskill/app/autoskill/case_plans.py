"""Project/session design sheets with immutable revisions and evidence-derived read-only results."""
from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import shutil
import uuid
from pathlib import Path

from . import case_document, db, domain, execution_inputs, paths, plan, plan_execution, projects, reporting, runner, task_context, task_workbook
from .locking import named_lock, project_lock

EDITABLE = {"id", "module", "title", "type", "priority", "preconditions", "steps", "expected", "description", "author", "remarks", "requirement_ids", "source", "business_path", "prefix"}
BUSINESS = ("id", "module", "title", "type", "preconditions", "steps", "expected", "description", "requirement_ids")
STATUS = {"Pass": "通过", "Fail": "失败", "Blocked": "阻塞", "Cancelled": "已取消", "Unverified": "业务未验证", "NotRun": "未执行"}


def _folder(project_id: str, session: str) -> Path:
    if not isinstance(session, str):
        raise ValueError("会话标识需要是文字")
    registered, _ = plan_execution.registered(project_id, session)
    if registered:
        return Path(registered["folder"])
    identity = hashlib.sha256(session.encode()).hexdigest() if session else "default"
    return projects.path(project_id) / "testcases" / "design-plans" / identity


@contextlib.contextmanager
def _selected(project_id: str, session: str):
    folder = _folder(project_id, session)
    with task_context.use_session(session or None), plan.use_folder(folder), project_lock(project_id), plan._sheet_lock(project_id):
        yield folder


def _metadata(folder: Path) -> dict:
    file = folder / "design.json"
    meta = json.loads(file.read_bytes()) if file.is_file() else {"version": 0, "rows": {}}
    for identity, source in plan.source_metadata().items():
        meta["rows"].setdefault(identity, {}).update(source)
    return meta


def _revision(folder: Path, meta: dict) -> str:
    file = folder / plan._file().name
    return execution_inputs.content_hash({"workbook": hashlib.sha256(file.read_bytes()).hexdigest() if file.is_file() else None, "metadata": meta})


def requirements(project_id: str) -> list[dict]:
    from .core import store

    rows = [{"id": "legacy/" + item["id"], "requirement_id": item["id"], "title": item.get("title", item["id"]), "workflow_id": None}
            for item in store.read(projects.path(project_id) / "analysis/requirements.json")]
    documents = domain.read(project_id)["documents"]
    for name, value in documents.items():
        if name.startswith("workflows/") and name.endswith("/requirements.json"):
            identity = name.split("/")[1]
            rows.extend({"id": identity + "/" + item["id"], "requirement_id": item["id"], "title": item.get("text", item["id"]), "workflow_id": identity} for item in value)
    return rows


def _design(row: dict, extra: dict) -> dict:
    value = {key: copy.deepcopy(extra.get(key, row.get(key, ""))) for key in BUSINESS}
    value["requirement_ids"] = extra.get("requirement_ids", row.get("requirement_ids")) or []
    if extra.get("source"):
        value["source"] = copy.deepcopy(extra["source"])
    if extra.get("business_path"):
        value["business_path"] = copy.deepcopy(extra["business_path"])
    return value


def _history(folder: Path) -> list[dict]:
    records = []
    for file in (folder / "history").glob("*/snapshot.json"):
        data = json.loads(file.read_bytes())
        records.append({"version": data["version"], "saved_at": data["saved_at"], "rows": len(data["rows"]), "revision": data["revision"]})
    return sorted(records, key=lambda row: row["version"], reverse=True)


def _legacy(project_id: str, session: str) -> dict | None:
    with task_context.use_session(session or None), plan.use_folder(paths.case_skill()):
        if not plan._file().is_file():
            return None
        shown = plan.show()
        if shown.get("project") != project_id:
            return None
        file = plan._file()
        return {"file": str(file), "sha256": hashlib.sha256(file.read_bytes()).hexdigest(), "cases": len(shown["cases"])}


def _evidence(project_id: str, row: dict, extra: dict, latest: dict, current: dict) -> dict:
    candidate = plan_execution.evidence(project_id, row)
    if candidate is not None:
        return candidate
    replay = row.get("replay", "")
    linked_project, _, identity = replay.partition("/")
    binding = extra.get("binding", {})
    value = {"status": "NotRun", "actual": "", "executor": "", "environment": "", "screenshots": [], "reason": "尚未关联可执行用例", "result": None,
             "run_id": None, "materialized_case_id": identity or None, "binding_current": False, "current": False}
    if not identity:
        if row.get("status") == "阻塞":
            return {**value, "status": "Blocked", "reason": "；".join(filter(None, [row.get("note"), "记录为阻塞；没有本行执行回执"]))}
        return value
    if linked_project != project_id:
        return {**value, "reason": "关联属于其他项目，不能借用其执行结果"}
    case = current.get(identity)
    if not case:
        return {**value, "reason": "关联的可执行用例已不存在，需重新关联"}
    design_hash = execution_inputs.content_hash(_design(row, extra))
    if binding.get("design_hash") != design_hash or binding.get("case_hash") != execution_inputs.content_hash(case):
        return {**value, "reason": "设计或关联用例已修改；需重新关联，并重新回放验证"}
    value["binding_current"] = True
    result = latest.get(identity)
    if not result:
        return {**value, "reason": "关联用例尚无真实执行结果"}
    if binding.get("execution_after") and result["started_at"] < binding["execution_after"]:
        return {**value, "reason": "已有结果早于本次设计关联；请重新回放"}
    if not execution_inputs.result_current(project_id, case, result):
        return {**value, "reason": "关联用例的执行输入或证据已变化，需重新回放"}
    detail = json.loads(result["detail"])
    business = detail.get("business_status", "Unverified")
    mapped = binding.get("design_mapped", False)
    status = result["status"] if result["status"] != "Pass" or (business == "Pass" and mapped) else "Unverified"
    reason = result.get("reason", "")
    if status == "Unverified" and (not mapped or case.get("extensions", {}).get("functional_design", {}).get("state") == "unmapped"):
        reason = "已有断言未证明覆盖本次功能预期；请按设计重新录制独立检查，或提交来源驱动的新候选断言后回归"
    return {**value, "status": status, "actual": result.get("actual", "") or result.get("reason", ""),
            "executor": "autoSkill / " + result["engine"], "environment": projects.config(project_id)["base_url"],
            "screenshots": [f"/api/runs/{result['run_id']}/files/{name}" for name in detail.get("screenshots", [])],
            "reason": reason, "run_id": result["run_id"], "result": {**result, "detail": detail}, "current": True}


def _view(project_id: str, session: str, folder: Path) -> dict:
    meta = _metadata(folder)
    exists = plan._file().is_file()
    shown = plan.show() if exists else {"cases": []}
    if shown.get("project") and shown["project"] != project_id:
        raise ValueError("该设计表属于其他项目，不能在当前项目修改")
    current = {item["id"]: item for item in projects.cases(project_id)}
    latest = reporting.latest_results(project_id)
    rows = []
    project = projects.get(project_id)
    for item in shown["cases"]:
        extra = meta["rows"].get(item["id"], {})
        evidence = _evidence(project_id, item, extra, latest, current)
        business_path = item.get("business_path") or [project["name"], item.get("module") or "未分组"]
        rows.append({**item, "description": extra.get("description", item["title"]), "author": extra.get("author", ""),
                     "requirement_ids": extra.get("requirement_ids", []), "remarks": extra.get("remarks", ""),
                     "source": extra.get("source", ""), "prefix": extra.get("prefix", ""),
                     "name": "-".join(business_path), "trace": business_path[-1], "business_path": business_path,
                     "status": evidence["status"], "conclusion": STATUS.get(evidence["status"], evidence["status"]),
                     "actual": evidence["actual"], "executor": evidence["executor"], "environment": evidence["environment"],
                     "screenshots": evidence["screenshots"], "evidence": evidence})
    return {"project": {"id": project_id, "name": project["name"]}, "session": session, "exists": exists, "version": meta["version"],
            "revision": _revision(folder, meta), "rows": rows, "columns": [{"key": key, "label": label} for key, label in zip(task_workbook.FIELDS, task_workbook.HEADERS)],
            "summary": {status: sum(row["status"] == status for row in rows) for status in STATUS},
            "history": _history(folder), "requirements": requirements(project_id),
            "available_cases": [{"id": item["id"], "title": item["title"], "kind": item["kind"]} for item in current.values()],
            "legacy": _legacy(project_id, session) if not exists else None,
            "folder": str(folder), "note": "设计表不是可执行用例；执行字段只显示所关联用例的真实证据。设计改变后旧通过不会迁入新设计。"}


def get(project_id: str, session: str = "") -> dict:
    selected, sessions = plan_execution.registered(project_id, session)
    actual_session = selected["session"] if selected else session
    with _selected(project_id, actual_session) as folder:
        return {**_view(project_id, actual_session, folder), **plan_execution.describe(), "sessions": sessions}


def _check(folder: Path, revision: str | None) -> dict:
    meta = _metadata(folder)
    if revision != _revision(folder, meta):
        raise ValueError("设计表已变化，请重新读取后再保存；原内容未覆盖")
    return meta


def _archive(project_id: str, session: str, folder: Path, meta: dict) -> None:
    if not plan._file().is_file():
        return
    target = folder / "history" / str(meta["version"])
    if (target / "snapshot.json").is_file():
        return
    target.mkdir(parents=True, exist_ok=True)
    (target / "cases.xlsx").write_bytes(plan._file().read_bytes())
    snapshot = _view(project_id, session, folder)
    snapshot["saved_at"] = db.now()
    execution_inputs.write_json(target / "snapshot.json", snapshot)


def _validate(project_id: str, row: dict, *, update: bool = False) -> dict:
    if not isinstance(row, dict) or set(row) - EDITABLE:
        raise ValueError("设计只可修改定义、需求关联和备注；执行结果及证据字段只读")
    if not update and (not isinstance(row.get("id"), str) or not row["id"].strip() or not isinstance(row.get("title"), str) or not row["title"].strip()):
        raise ValueError("设计需要非空用例编号和标题")
    if "title" in row and (not isinstance(row["title"], str) or not row["title"].strip()):
        raise ValueError("用例标题不能为空")
    if any(not isinstance(value, str) for key, value in row.items() if key not in ("requirement_ids", "source", "business_path")):
        raise ValueError("设计字段需要为文字")
    if "source" in row and not isinstance(row["source"], (str, dict, list)):
        raise ValueError("设计来源需要为文字、JSON 对象或数组")
    if "business_path" in row and (not isinstance(row["business_path"], list) or not row["business_path"]
                                   or not all(isinstance(value, str) and value.strip() for value in row["business_path"])):
        raise ValueError("业务层级需要是非空文字数组；保留实际层级，不补造中间分组")
    known = {item["id"] for item in requirements(project_id)}
    if "requirement_ids" in row and (not isinstance(row["requirement_ids"], list) or not all(isinstance(value, str) and value in known for value in row["requirement_ids"])):
        raise ValueError("需求关联必须选择当前项目已存在的需求")
    return copy.deepcopy(row)


def _book(project_id: str, folder: Path, first: dict):
    if not plan._file().is_file():
        source = folder / (".input-" + uuid.uuid4().hex + ".json")
        source.write_text(json.dumps([first], ensure_ascii=False), encoding="utf-8")
        try:
            plan.write(project_id, str(source), delivery=False)
        finally:
            source.unlink(missing_ok=True)
    return plan._open()


def save(project_id: str, row: dict, *, session: str = "", expected_revision: str | None, case_id: str | None = None) -> dict:
    row = _validate(project_id, row, update=case_id is not None)
    if case_id is not None and "id" in row and row["id"] != case_id:
        raise ValueError("编辑设计时保留用例编号，避免历史证据错配")
    with _selected(project_id, session) as folder:
        meta = _check(folder, expected_revision)
        if case_id is not None and not plan._file().is_file():
            raise ValueError("设计用例不存在")
        _archive(project_id, session, folder, meta)
        first = plan._file().is_file()
        book, sheet, columns = _book(project_id, folder, row)
        number = plan._find(sheet, columns, case_id or row["id"])
        if case_id is None and first and number is not None:
            raise ValueError("设计用例编号已存在")
        if case_id is not None and number is None:
            raise ValueError("设计用例不存在")
        number = number or sheet.max_row + 1
        identity = case_id or row["id"]
        before = dict(next((item for index, item in plan._rows(sheet, columns) if index == number), {}))
        before_hash = execution_inputs.content_hash(_design(before, meta["rows"].get(identity, {})))
        _write_definition(sheet, columns, number, {"id": identity, **row})
        extra = meta["rows"].setdefault(identity, {})
        extra.update({key: row[key] for key in ("description", "author", "remarks", "requirement_ids", "source", "business_path", "prefix") if key in row})
        if "module" in row and row["module"] != before.get("module") and "business_path" not in row:
            extra.pop("business_path", None)
        after = next(item for index, item in plan._rows(sheet, columns) if index == number)
        if before_hash != execution_inputs.content_hash(_design(after, extra)):
            plan._set_status(sheet, columns, number, "未执行", "设计已变化；原执行结果保留在历史版本，需重新关联并回放")
        meta["version"] += 1
        plan._save(book)
        book.close()
        execution_inputs.write_json(folder / "design.json", meta)
        return _view(project_id, session, folder)


def _write_definition(sheet, columns: dict, number: int, row: dict) -> None:
    from openpyxl.styles import Alignment

    for key, value in row.items():
        if key in columns and key not in ("status", "note", "replay"):
            cell = sheet.cell(number, columns[key], value)
            cell.data_type = "s"
            cell.alignment = Alignment(wrap_text=True, vertical="top")


def link(project_id: str, case_id: str, replay_case_id: str, *, session: str = "", expected_revision: str | None) -> dict:
    current = projects.case(project_id, replay_case_id)
    with _selected(project_id, session) as folder:
        meta = _check(folder, expected_revision)
        _archive(project_id, session, folder, meta)
        book, sheet, columns = plan._open()
        number = plan._find(sheet, columns, case_id)
        if number is None:
            raise ValueError("设计用例不存在")
        row = dict(next(row for index, row in plan._rows(sheet, columns) if index == number))
        sheet.cell(number, columns["replay"], project_id + "/" + replay_case_id).data_type = "s"
        extra = meta["rows"].setdefault(case_id, {})
        extra["binding"] = {"case_hash": execution_inputs.content_hash(current), "design_hash": execution_inputs.content_hash(_design(row, extra)),
                            "execution_after": db.now(), "design_mapped": False}
        meta["version"] += 1
        plan._save(book)
        book.close()
        execution_inputs.write_json(folder / "design.json", meta)
        return _view(project_id, session, folder)


def import_cases(project_id: str, case_ids: list[str], *, session: str = "", expected_revision: str | None) -> dict:
    if not isinstance(case_ids, list) or not case_ids or not all(isinstance(value, str) for value in case_ids):
        raise ValueError("请选择需要导入的可执行用例")
    cases = [projects.case(project_id, identity) for identity in dict.fromkeys(case_ids)]
    with _selected(project_id, session) as folder:
        meta = _check(folder, expected_revision)
        _archive(project_id, session, folder, meta)
        defined = {row["id"] for row in plan.show()["cases"]} if plan._file().is_file() else set()
        if defined.intersection(case_ids):
            raise ValueError("设计中已有同编号用例；请编辑它并保留原历史")
        rows = [_imported(case, projects.get(project_id)["name"]) for case in cases]
        book, sheet, columns = _book(project_id, folder, rows[0])
        for number, row in enumerate(rows):
            index = plan._find(sheet, columns, row["id"]) if number == 0 else None
            index = index or sheet.max_row + 1
            _write_definition(sheet, columns, index, row)
            sheet.cell(index, columns["replay"], project_id + "/" + row["id"]).data_type = "s"
            document = cases[number].get("extensions", {}).get("test_case_document", {})
            known = {item["id"] for item in requirements(project_id)}
            extra = {"description": row["description"], "requirement_ids": ["legacy/" + identity for identity in cases[number].get("requirement_ids", [])
                                                                           if "legacy/" + identity in known],
                     "author": document.get("author", ""), "remarks": document.get("remarks", ""), "source": row["source"]}
            extra.update({key: copy.deepcopy(document[key]) for key in ("business_path", "prefix") if document.get(key)})
            extra["binding"] = {"case_hash": execution_inputs.content_hash(cases[number]), "design_hash": execution_inputs.content_hash(_design(row, extra)),
                                "design_mapped": _complete_document(document)
                                and cases[number].get("extensions", {}).get("functional_design", {}).get("state") != "unmapped"}
            meta["rows"][row["id"]] = extra
        meta["version"] += 1
        plan._save(book)
        book.close()
        execution_inputs.write_json(folder / "design.json", meta)
        return _view(project_id, session, folder)


def adopt(project_id: str, *, session: str = "", expected_revision: str | None, source_hash: str) -> dict:
    with _selected(project_id, session) as folder:
        _check(folder, expected_revision)
        if plan._file().is_file():
            raise ValueError("当前项目和会话已有设计表，不能覆盖；请保留或使用另一个会话")
        source = _legacy(project_id, session)
        if not source or source["sha256"] != source_hash:
            raise ValueError("原会话设计表已变化或属于其他项目，请重新读取")
        target = folder / "cases.xlsx"
        temporary = folder / (".adopt-" + uuid.uuid4().hex + ".tmp")
        try:
            shutil.copyfile(source["file"], temporary)
            if hashlib.sha256(temporary.read_bytes()).hexdigest() != source_hash:
                raise ValueError("原会话设计表在复制期间变化，请重试")
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        meta = {"version": 1, "rows": {}, "adopted_from": source}
        execution_inputs.write_json(folder / "design.json", meta)
        return _view(project_id, session, folder)


def _paragraphs(value) -> str:
    if isinstance(value, list):
        return "\n".join(str(item) for item in value)
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _complete_document(document: dict) -> bool:
    return all(document.get(key) and "【待确认" not in _paragraphs(document[key]) for key in ("description", "preconditions", "steps", "expected", "source"))


def _imported(case: dict, project_name: str) -> dict:
    document = case_document.document(case)
    path = document.get("business_path", [])
    module = "/".join(path[1:] if path and path[0] == project_name else path) if path else case.get("module", "未分组")
    return {"id": case["id"], "title": case_document.functional_description(case), "module": document.get("module") or module,
            "description": case_document.functional_description(case), "source": copy.deepcopy(document.get("source", "")),
            "type": "页面" if case["kind"] == "ui" else "接口", "priority": document.get("priority") or case.get("priority", "P1"),
            "preconditions": _paragraphs(case_document.functional_preconditions(case)),
            "steps": _paragraphs(case_document.functional_steps(case)), "expected": case_document.functional_expected(case) or "1. " + case_document.MISSING_EXPECTED}


def apply_to_case(project_id: str, case_id: str, *, session: str = "", expected_revision: str | None) -> dict:
    """Version an explicitly selected functional design onto its case, preserving every technical input."""
    with _selected(project_id, session) as folder:
        meta = _check(folder, expected_revision)
        view = _view(project_id, session, folder)
        row = next((item for item in view["rows"] if item["id"] == case_id), None)
        if not row or not row["evidence"]["materialized_case_id"]:
            raise ValueError("请先选择这条设计所关联的可执行用例")
        for field in ("description", "preconditions", "steps", "expected", "source"):
            if not row.get(field) or "【待确认：" in _paragraphs(row[field]):
                raise ValueError("功能设计仍有待补充的 " + field + "；先依据来源完善，原用例未修改")
        current = projects.case(project_id, row["evidence"]["materialized_case_id"])
        current_hash = execution_inputs.content_hash(current)
        extra = meta["rows"].setdefault(case_id, {})
        if extra.get("binding", {}).get("case_hash") != current_hash:
            raise ValueError("关联用例的版本已变化，请重新关联后再应用设计")
        _archive(project_id, session, folder, meta)
        document = _row_document(row)
        candidate = case_document.apply(copy.deepcopy(current), {**case_document.document(current), **document})
        candidate["title"] = row["description"]
        candidate["extensions"]["functional_design"] = {"state": "unmapped", "reason": "功能设计已保存，既有技术断言是否覆盖新预期尚未确认"}
        if candidate != current:
            candidate["revision"] = current["revision"] + 1
        projects.save_cases(project_id, [candidate], expected_hashes={current["id"]: current_hash})
        extra["binding"] = {"case_hash": execution_inputs.content_hash(candidate), "design_hash": execution_inputs.content_hash(_design(row, extra)),
                            "execution_after": db.now(), "design_mapped": False}
        meta["version"] += 1
        execution_inputs.write_json(folder / "design.json", meta)
        return {**_view(project_id, session, folder), "case_id": current["id"], "case_revision": candidate["revision"]}


def _row_document(row: dict) -> dict:
    return {"description": row["description"], "preconditions": row.get("preconditions", ""), "steps": row.get("steps", "").splitlines(),
            "expected": row.get("expected", ""), "source": row["source"], "module": row.get("module", "未分组"), "priority": row.get("priority", "P1"),
            "author": row["author"], "remarks": row["remarks"], "business_path": row["business_path"], "prefix": row["prefix"]}


def recorded_binding(project_id: str, case_id: str, replay_case: str, assertions: int) -> None:
    """Bind the compiler's real recording to its source design; the subsequent run still supplies evidence."""
    folder = plan._folder()
    if not (folder / "design.json").is_file():
        return
    meta = _metadata(folder)
    view = _view(project_id, task_context.session_id() or "", folder)
    row = next((item for item in view["rows"] if item["id"] == case_id), None)
    if not row:
        return
    current = projects.case(project_id, replay_case)
    document = case_document.document(current)
    source = document.get("source", {})
    without_digest = copy.deepcopy(document)
    if isinstance(source, dict):
        without_digest["source"].pop("design_sha256", None)
    source_matches = (isinstance(source, dict) and source.get("kind") == "case_plan" and source.get("case_id") == case_id
                      and isinstance(source.get("file"), str) and Path(source["file"]).resolve() == plan._file().resolve()
                      and source.get("upstream", "") == row["source"] and source.get("design_sha256") == execution_inputs.content_hash(without_digest))
    automation = current.get("extensions", {}).get("automation", {})
    checks = [step for step in [*automation.get("actions", []), *automation.get("assertions", [])] if "assertion" in step]
    explicit = any(step.get("oracle", {}).get("source") == "explicit" for step in checks)
    matches_fields = all(document.get(key, "") == row.get(key, "") for key in ("description", "preconditions", "expected", "module", "priority", "author", "remarks", "prefix"))
    matched = (bool(assertions) and explicit and source_matches and matches_fields and _complete_document(_row_document(row)) and document.get("business_path") == row["business_path"]
               and case_document.functional_steps(current) == case_document.functional_steps({"extensions": {"test_case_document": {"steps": row["steps"]}}}))
    extra = meta["rows"].setdefault(case_id, {})
    extra["binding"] = {"case_hash": execution_inputs.content_hash(current), "design_hash": execution_inputs.content_hash(_design(row, extra)),
                        "execution_after": db.now(), "design_mapped": matched, "materialization": "recorded_with_plan"}
    meta["version"] += 1
    execution_inputs.write_json(folder / "design.json", meta)


def sync(project_id: str, *, session: str = "", expected_revision: str | None) -> dict:
    with _selected(project_id, session) as folder:
        _check(folder, expected_revision)
        plan.sync(folder=folder)
        view = _view(project_id, session, folder)
        book, sheet, columns = plan._open()
        for row in view["rows"]:
            number = plan._find(sheet, columns, row["id"])
            plan._set_status(sheet, columns, number, STATUS[row["status"]], row["evidence"]["reason"])
        plan._save(book)
        book.close()
        return _view(project_id, session, folder)


def run(project_id: str, case_ids: list[str], *, session: str = "", engine: str = "playwright", headed: bool | None = None) -> dict:
    from .server import jobs

    if not isinstance(case_ids, list) or not case_ids or not all(isinstance(value, str) and value for value in case_ids):
        raise ValueError("请选择要回放的设计用例编号")
    with _selected(project_id, session) as folder:
        view = _view(project_id, session, folder)
        selected = [row for row in view["rows"] if row["id"] in case_ids]
        if not selected or len(selected) != len(set(case_ids)) or any(not row["evidence"]["binding_current"] for row in selected):
            raise ValueError("请选择已关联且设计未变化的设计行；草稿不能直接执行")
        made = runner.create(project_id, list(dict.fromkeys(row["evidence"]["materialized_case_id"] for row in selected)), engine,
                             headed=headed, session_id=session, plan_path=folder)
        return {**made, "queue_dispatcher": jobs.ensure_worker()}


def history(project_id: str, version: int, *, session: str = "") -> dict:
    with _selected(project_id, session) as folder:
        file = folder / "history" / str(version) / "snapshot.json"
        if not file.is_file():
            raise ValueError("设计历史版本不存在")
        return json.loads(file.read_bytes())


def previous_deliveries(project_id: str, session: str = "") -> dict:
    selected, _ = plan_execution.registered(project_id, session)
    actual_session = selected["session"] if selected else session
    with _selected(project_id, actual_session):
        return {"deliveries": plan_execution.previous_deliveries(project_id, actual_session)}


def export(project_id: str, *, session: str = "", version: int | None = None) -> Path:
    view = history(project_id, version, session=session) if version is not None else get(project_id, session)
    if version is None and (view.get("test_report") or {}).get("report_id"):
        from . import task_reports

        return task_reports.download(view["test_report"]["report_id"])
    folder = _folder(project_id, session) / "exports"
    destination = folder / ("design-" + str(view["version"]) + "-" + view["revision"][:12] + ".xlsx")
    snapshot = _snapshot(view)
    with named_lock("case-plan-export", str(destination)):
        reporting.write_task(snapshot, destination)
    return destination


def _snapshot(view: dict) -> dict:
    cases = []
    for row in view["rows"]:
        result = row["evidence"].get("result") if row["evidence"].get("current") else None
        definition = {"id": row["id"], "kind": "scenario", "title": row["description"], "module": row.get("module", "未分组"), "priority": row.get("priority", "P1"),
                      "preconditions": row.get("preconditions", ""), "steps": row.get("steps", "").splitlines(), "expected": row.get("expected", ""),
                      "requirement_ids": row["requirement_ids"], "extensions": {"test_case_document": _row_document(row)}}
        if row["evidence"].get("items"):
            cases.extend({**item, "definition": definition} for item in row["evidence"]["items"])
            continue
        if not row["evidence"].get("result") and not row["evidence"].get("run_id"):
            cases.append({**plan_execution.unexecuted_item(row, definition), "note": row["evidence"]["reason"]})
            continue
        cases.append({"definition": definition, "result": result, "run_id": row["evidence"].get("run_id"),
                      "evidence_valid": row["evidence"].get("current", False) and row["status"] != "Unverified", "environment": [row["environment"]] if row["environment"] else [],
                      "note": row["evidence"]["reason"]})
    sources = {"version": view["version"], "session": view["session"], "requirements": view["requirements"],
               "materialization": [{"design_id": row["id"], "case_id": row["evidence"].get("materialized_case_id"),
                                    "current_evidence": row["evidence"]["current"], "run_id": row["evidence"].get("run_id")} for row in view["rows"]]}
    return {"schema": 1, "kind": "design", "task_id": "design-" + view["revision"], "project_id": view["project"]["id"], "project": view["project"],
            "title": "设计用例表", "status": "Design", "cases": cases, "results": [row["evidence"]["result"] for row in view["rows"] if row["evidence"].get("current")],
            "run_ids": list(dict.fromkeys(item["run_id"] for item in cases if item["run_id"])), "sources": sources,
            "notes": [view["note"]], "metadata": {"version": view["version"], "revision": view["revision"]}}
