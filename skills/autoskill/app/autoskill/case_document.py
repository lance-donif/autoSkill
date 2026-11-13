"""Keep source-backed functional case prose separate from executable browser and HTTP data."""
from __future__ import annotations

import copy
import json
import re

from .replay import ir

HTTP_STEP = re.compile(r"^(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(?:https?://|/)", re.IGNORECASE)
TECHNICAL_TITLE = re.compile(r"^(?:(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)|查询|提交|修改|删除)\s+(?:https?://|/)", re.IGNORECASE)
NUMBERED = re.compile(r"^\d+\.\s+")
MISSING_STEPS = "【待确认：功能测试步骤尚未提供】；自动化动作和接口请求保留在执行明细。"
MISSING_EXPECTED = "【待确认：有来源支持的预期结果】；技术断言不是已编写的功能测试预期。"


def document(case: dict) -> dict:
    value = case.get("extensions", {}).get("test_case_document", {})
    return value if isinstance(value, dict) else {}


def _steps(value) -> list[str]:
    if isinstance(value, str):
        lines = value.splitlines()
        if not any(NUMBERED.match(line) for line in lines):
            return lines if value.strip() else []
        result = []
        for line in lines:
            if NUMBERED.match(line) or not result:
                result.append(line)
            else:
                result[-1] += "\n" + line
        return result
    return list(value) if isinstance(value, list) and value and all(isinstance(item, str) and item.strip() for item in value) else []


def technical_steps(case: dict, steps: list[str]) -> bool:
    """Recognize actual legacy compiler output; do not infer business meaning from a URL."""
    lines = [NUMBERED.sub("", step).strip() for step in steps]
    if lines and all(HTTP_STEP.match(line) for line in lines):
        return True
    automation = case.get("extensions", {}).get("automation", {})
    if automation:
        generated = ["打开 " + str(automation.get("start_url", "")),
                     *[step.get("name") or ir.describe(step) for step in automation.get("actions", [])]]
        if lines == generated:
            return True
    return False


def functional_steps(case: dict) -> list[str]:
    declared = _steps(document(case).get("steps"))
    if declared:
        return declared
    legacy = _steps(case.get("steps"))
    if legacy and not technical_steps(case, legacy):
        return legacy
    turns = case.get("turns", [])
    if turns:
        return ["向助手提问：“" + turn["question"] + "”。" if isinstance(turn.get("question"), str) else MISSING_STEPS for turn in turns]
    return [MISSING_STEPS]


def _conversation_expected(case: dict) -> str | None:
    turns = case.get("turns", [])
    if not turns:
        return None
    from .engines.ai_oracles import reference_value

    lines = []
    for number, turn in enumerate(turns, 1):
        if "expected" in turn:
            value = turn["expected"]
        elif "reference" in turn and isinstance(case.get("dataset"), list):
            try:
                value = reference_value(case["dataset"], turn["reference"])
            except (KeyError, ValueError, TypeError):
                lines.append(f"{number}. 【待确认：本轮参考资料或计算方式不可用】")
                continue
        else:
            lines.append(f"{number}. 【待确认：本轮有来源支持的功能预期】")
            continue
        shown = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        lines.append(f"{number}. 本轮回答应为：{shown}")
    return "\n".join(lines)


def functional_expected(case: dict) -> str | None:
    declared = document(case).get("expected")
    if isinstance(declared, str) and declared.strip():
        return declared
    legacy = case.get("expected")
    if not isinstance(legacy, str) or not legacy.strip():
        return _conversation_expected(case)
    if case.get("kind") == "api" and re.fullmatch(r"HTTP\s+\d{3}(?:，并满足录制到的字段与结构)?", legacy):
        return None
    automation = case.get("extensions", {}).get("automation", {})
    generated = "；".join(step.get("name") or ir.describe(step) for step in automation.get("assertions", []))
    if generated and legacy == generated:
        return None
    return legacy


def functional_description(case: dict) -> str:
    declared = document(case).get("description")
    if isinstance(declared, str) and declared.strip():
        return declared
    title = case.get("title")
    return title if isinstance(title, str) and title.strip() and not TECHNICAL_TITLE.match(title) else "【待确认：具体功能测试目标】"


def functional_preconditions(case: dict) -> str:
    declared = document(case).get("preconditions")
    if isinstance(declared, str) and declared.strip():
        return declared
    return case.get("preconditions") or "【待确认：必要前置条件】"


def apply(case: dict, value: dict | None) -> dict:
    """Persist supplied functional design verbatim without changing technical replay inputs."""
    if value is None:
        return case
    if not isinstance(value, dict):
        raise ValueError("功能用例设计必须是对象")
    document_copy = copy.deepcopy(value)
    for field in ("description", "preconditions", "expected"):
        if field in value and (not isinstance(value[field], str) or not value[field].strip()):
            raise ValueError(f"功能用例设计的 {field} 必须是非空文字")
    if "steps" in value and not _steps(value["steps"]):
        raise ValueError("功能用例设计的 steps 必须是非空文字或文字数组")
    if "steps" in value and technical_steps(case, _steps(value["steps"])):
        raise ValueError("功能测试步骤需要描述具体业务操作，不能只保存 HTTP 路径或浏览器指令")
    case["extensions"]["test_case_document"] = document_copy
    for field in ("preconditions", "expected", "module", "priority", "requirement_ids"):
        if field in value:
            case[field] = copy.deepcopy(value[field])
    if "steps" in value:
        case["steps"] = _steps(value["steps"])
    return case
