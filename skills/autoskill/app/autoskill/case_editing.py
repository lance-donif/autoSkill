"""Edit complete executable definitions without replacing evidence or starting a run."""
from __future__ import annotations

import copy
import json

import jsonschema

from . import case_document, db, domain, execution_inputs, paging, paths, projects, runner
from .core import store
from .locking import project_lock
from .replay import ir

ACTION_VALUE_FIELDS = {"url", "value", "key", "tab", "active", "image", "image_context"}
TARGET_FIELDS = {"locators", "selector", "placeholder", "role", "context"}
PROVENANCE_FIELDS = ("oracle", "functional_design")
DOCUMENT_FIELDS = ("preconditions", "steps", "expected", "module", "priority", "requirement_ids")


class CaseEditConflictError(ValueError):
    """A stale edit cannot overwrite a newer definition."""

    def __init__(self, case: dict):
        self.content_revision = paging.revision(case)
        self.revision = case["revision"]
        super().__init__("用例已被其他任务修改；当前草稿尚未覆盖，请重新读取最新版本后合并")


def _actions(case: dict) -> list[tuple[int, dict]]:
    automation = case.get("extensions", {}).get("automation", {})
    return [(index, step) for index, step in enumerate(automation.get("actions", [])) if isinstance(step, dict) and "action" in step]


def editing(case: dict) -> dict:
    """Expose transport-only identities without mutating a legacy case on a read."""
    revision = paging.revision(case)
    actions = [{"index": index, "id": "action-" + paging.revision([case["id"], revision, index])[:24], "action": step["action"]}
               for index, step in _actions(case)] if case["kind"] == "ui" else []
    return {"actions": actions, "fixed_action_order": True, "assertions_editable": True,
            "read_only": ["id", "kind", "schema_version", "revision", "extensions.oracle", "extensions.functional_design"]}


def get(project_id: str, case_id: str) -> dict:
    with project_lock(project_id):
        case = projects.case(project_id, case_id)
        return {"case": case, "content_revision": paging.revision(case), "editing": editing(case), "verification": verification(project_id, case)}


def verification(project_id: str, case: dict) -> dict:
    """An old execution remains a historical fact until its full dependency scope matches."""
    result = db.one("SELECT r.* FROM results r JOIN runs u ON u.id = r.run_id WHERE r.project_id = ? AND r.case_id = ? "
                    "AND r.prerequisite = 0 AND u.trigger != 'login' AND u.family != 'load' ORDER BY r.id DESC LIMIT 1", (project_id, case["id"]))
    current = bool(result and execution_inputs.result_current(project_id, case, result))
    business = json.loads(result["detail"]).get("business_status", "Unverified") if current else "Unverified"
    status = result["status"] if current else None
    if status == "Pass" and business != "Pass":
        status = "Unverified"
    return {"status": status or "NotRun", "current_status": status, "business_status": business, "current": current,
            "last_status": result["status"] if result else None, "last_run_id": result["run_id"] if result else None,
            "reason": (result.get("reason") or "当前输入已有真实执行证据。") if current else "当前版本或依赖输入尚无有效证据，请手动验证；旧结果作为历史保留。"}


def _shape(candidate: dict, current: dict) -> None:
    if not isinstance(candidate, dict):
        raise ValueError("case 需要是完整用例对象")
    for key in ("id", "kind", "schema_version"):
        if candidate.get(key) != current[key]:
            raise ValueError("已有用例不能修改 " + key)
    if not isinstance(candidate.get("extensions"), dict):
        raise ValueError("extensions 需要是对象")
    candidate["revision"] = current["revision"]


def _clean_action(step: dict, previous: dict) -> dict:
    step.pop("_edit_id", None)
    if step.get("action") != previous.get("action"):
        needs_target, required = ir.ACTIONS.get(step.get("action"), (False, ()))
        allowed = set(required) | ({"image_context"} if step.get("action") == "captcha" else set())
        for key in ACTION_VALUE_FIELDS - allowed:
            step.pop(key, None)
        if not needs_target and step.get("action") != "press":
            for key in TARGET_FIELDS:
                step.pop(key, None)
    return step


def _ui_actions(candidate: dict, current: dict) -> None:
    automation = candidate["extensions"].get("automation")
    if automation is None and "automation" not in candidate["extensions"] and "automation" not in current["extensions"]:
        return
    if not isinstance(automation, dict) or not isinstance(automation.get("actions", []), list):
        raise ValueError("页面用例需要完整的 automation.actions")
    steps = automation.get("actions", [])
    if any(not isinstance(step, dict) or ("action" in step) == ("assertion" in step) for step in steps):
        raise ValueError("每个回放步骤只能是一个动作或一条断言")
    incoming, expected = _actions(candidate), editing(current)["actions"]
    if len(incoming) != len(expected) or [step.get("_edit_id") for _, step in incoming] != [item["id"] for item in expected]:
        raise ValueError("既有动作不能新增、删除、复制或排序；请保留各动作的编辑身份")
    originals = [step for _, step in _actions(current)]
    for (_, step), previous in zip(incoming, originals):
        _clean_action(step, previous)
    ir.validate(automation)
    if any("action" in step for step in automation.get("assertions", [])):
        raise ValueError("结束断言不能包含动作")
    for step in [*steps, *automation.get("assertions", [])]:
        if isinstance(step, dict):
            step.pop("_edit_id", None)


def _document_value(key: str, value):
    return case_document._steps(value) if key == "steps" else value


def _document(candidate: dict, current: dict) -> None:
    supplied = candidate["extensions"].get("test_case_document")
    if supplied is None:
        return
    if not isinstance(supplied, dict):
        raise ValueError("功能用例设计需要是对象")
    previous = case_document.document(current)
    for key in DOCUMENT_FIELDS:
        root_changed = candidate.get(key) != current.get(key)
        document_changed = supplied.get(key) != previous.get(key)
        if root_changed and document_changed and key in supplied and _document_value(key, supplied[key]) != candidate.get(key):
            raise ValueError("功能设计与用例的 " + key + " 不一致，请保留同一份手工设计")
        if root_changed and not document_changed and (previous or key in supplied):
            supplied[key] = copy.deepcopy(candidate[key])
    for key in ("description", "author", "remarks", "prefix"):
        if key in supplied and not isinstance(supplied[key], str):
            raise ValueError("功能设计的 " + key + " 需要是文字")
    if "source" in supplied and not isinstance(supplied["source"], (str, dict, list)):
        raise ValueError("功能设计来源需要是文字、对象或数组")
    business_path = supplied.get("business_path")
    if business_path is not None and (not isinstance(business_path, list) or not business_path
                                      or not all(isinstance(part, str) and part.strip() for part in business_path)):
        raise ValueError("业务层级需要是非空文字数组")
    if supplied != previous:
        case_document.apply(candidate, supplied)


def _provenance(candidate: dict, current: dict) -> None:
    extensions, previous = candidate["extensions"], current.get("extensions", {})
    for key in PROVENANCE_FIELDS:
        if key in previous:
            extensions[key] = copy.deepcopy(previous[key])
        else:
            extensions.pop(key, None)
    if (candidate.get("expected") != current.get("expected")
            or case_document.document(candidate).get("expected") != case_document.document(current).get("expected")):
        extensions["functional_design"] = {"state": "unmapped", "reason": "业务预期已手工修改；既有技术断言是否覆盖新预期尚未确认"}


def _validate(candidate: dict, project_id: str) -> None:
    errors = list(jsonschema.Draft202012Validator(store.read(paths.SKILL / "references/case.schema.json")).iter_errors(candidate))
    if errors:
        raise ValueError("用例 Schema 不符：" + errors[0].json_path + "：" + errors[0].message)
    requirements = {item["id"] for item in store.read(projects.path(project_id) / "analysis/requirements.json")}
    if not set(candidate["requirement_ids"]).issubset(requirements):
        raise ValueError("用例关联了不存在的需求")
    credentials = store.credential_errors(candidate, candidate["id"])
    if credentials:
        raise ValueError("\n".join(credentials))
    if candidate["kind"] == "api":
        store._validate_saved_api_contract(candidate, jsonschema)
    dependencies = candidate["extensions"].get("depends_on", [])
    if not isinstance(dependencies, list) or any(not isinstance(item, str) or not item for item in dependencies) or len(set(dependencies)) != len(dependencies):
        raise ValueError("前置用例需要是不重复的用例编号数组")
    active = {case["id"]: case for case in projects.cases(project_id)}
    active[candidate["id"]] = candidate
    runner.build_plan([candidate["id"]], active)


def save(project_id: str, case_id: str, candidate: dict, expected_content_revision: str) -> dict:
    if not isinstance(expected_content_revision, str) or not domain.HASH.fullmatch(expected_content_revision):
        raise ValueError("保存需要完整定义 GET 返回的 expected_content_revision")
    with project_lock(project_id):
        current = projects.case(project_id, case_id)
        revision = paging.revision(current)
        if revision != expected_content_revision:
            raise CaseEditConflictError(current)
        candidate = copy.deepcopy(candidate)
        _shape(candidate, current)
        if candidate["kind"] == "ui":
            _ui_actions(candidate, current)
        _document(candidate, current)
        _provenance(candidate, current)
        _validate(candidate, project_id)
        changed = candidate != current
        if changed:
            candidate["revision"] = current["revision"] + 1
            projects.save_cases(project_id, [candidate], expected_hashes={case_id: revision})
        return {"case": candidate, "content_revision": paging.revision(candidate), "editing": editing(candidate), "changed": changed,
                "previous_revision": current["revision"], "verification": {
                    "status": "NotRun" if changed else "unchanged", "business_status": "Unverified" if changed else None,
                    "current": False if changed else verification(project_id, candidate)["current"],
                    "reason": "新版本尚无实际验证，请手动运行；历史证据保留。" if changed else "用例内容未变化；未创建新版本或启动执行。"}}
