"""Map frozen test definitions to the supplied fourteen-column document without inventing facts."""
from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from pathlib import Path

from . import case_document, data_refs, execution_inputs, task_report_data

STATUS = {"Pass": "通过", "Fail": "失败", "Blocked": "阻塞", "Cancelled": "已取消", "Unverified": "业务未验证", "NotRun": "未执行", "Unknown": "未知"}
NUMBERED = re.compile(r"^(\d+)\.\s+(.+)$")
CHAPTER = re.compile(r"^\s*(?:第[一二三四五六七八九十百\d]+[章节]\s*|\d+(?:\.\d+)*(?:[、.)）]\s*|\s+))")
PENDING_FUNCTION = "【待确认：功能项】"
PENDING_FACT = re.compile(r"【待(?:确认|补充)[^】]*】")


def text(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def display_lines(value: str, width: int) -> int:
    return sum(max(1, math.ceil(len(line) * 1.8 / width)) for line in value.splitlines())


def text_fragments(value: str) -> list[str]:
    """Preserve every character while keeping each auxiliary cell below the row-height ceiling."""
    pieces, pending = [], ""
    for line in value.splitlines(keepends=True) or [value]:
        for start in range(0, len(line), 800):
            part = line[start:start + 800]
            combined = pending + part
            if pending and (len(combined) > 1000 or display_lines(combined, 110) > 18):
                pieces.append(pending)
                pending = part
            else:
                pending = combined
    if pending:
        pieces.append(pending)
    return pieces or [""]


def document(case: dict) -> dict:
    value = case.get("extensions", {}).get("test_case_document", {})
    return value if isinstance(value, dict) else {}


def _path(case: dict, project: dict) -> tuple[list[str], list[str]]:
    declared = document(case).get("business_path")
    if isinstance(declared, list) and declared and all(isinstance(part, str) and part.strip() for part in declared):
        return list(declared), []
    root = project.get("name") or "【待确认：业务系统】"
    module = case.get("module")
    if module and module != "未分组":
        return [root, module], ["仅已保存的项目/模块归属；更细业务层级未提供，不补造层级。"]
    return [root, PENDING_FUNCTION], ["【待确认：真实业务功能归属】；占位分组不代表已确认需求。"]


def _paths(groups: list[dict], project: dict, display_paths: dict | None = None) -> None:
    for group in groups:
        group["business_path"], group["gaps"] = _path(group["definition"], project)
        group["original_business_path"] = list(group["business_path"])
        if display_paths is not None and group["definition"]["id"] in display_paths:
            declared = display_paths[group["definition"]["id"]]
            if not isinstance(declared, list) or not declared or not all(isinstance(part, str) and part for part in declared):
                raise ValueError("报告文件夹路径格式无效")
            group["folder_path"] = list(declared)
            if not document(group["definition"]).get("business_path"):
                group["business_path"] = list(declared)
                group["gaps"] = ["按本次导出时冻结的文件夹完整路径分类；功能设计、原始业务归属及定义哈希见编号映射。"]
            else:
                group["gaps"].append("业务层级使用原功能设计；文件夹完整组织路径另见编号映射，不将组织标签冒认为业务功能。")
    roots = {group["business_path"][0] for group in groups}
    if len(roots) > 1:
        for group in groups:
            group["business_path"].insert(0, project.get("name") or "【待确认：业务系统】")
            group["gaps"].append("项目包含多个已声明业务根，统一置于本项目归属下，原始完整路径见编号映射。")
    parents = {tuple(group["business_path"][:depth]) for group in groups for depth in range(1, len(group["business_path"]))}
    for group in groups:
        path = group["business_path"]
        if tuple(path) in parents:
            if display_paths is not None and group["definition"]["id"] in display_paths:
                group["business_path"] = [*path, "【本级用例】"]
                group["gaps"].append("本级用例是直接归属此文件夹的用例，不属于子文件夹；仅为导出分组标签。")
            else:
                group["business_path"] = [*path, PENDING_FUNCTION]
                group["gaps"].append("【待确认：此用例所属末级功能项】；已有父分组下包含其他功能，未推断本用例归属。")


def _prefixes(groups: list[dict]) -> dict[tuple, str]:
    requested = defaultdict(set)
    for group in groups:
        prefix = document(group["definition"]).get("prefix", "")
        if group["business_path"][-1] in ("【本级用例】", "【项目根目录用例】"):
            prefix = ""
        if isinstance(prefix, str) and re.fullmatch(r"[A-Z0-9]+", prefix):
            requested[tuple(group["business_path"])].add(prefix)
    counts = defaultdict(int)
    for choices in requested.values():
        for prefix in choices:
            counts[prefix] += 1
    prefixes = {}
    for group in groups:
        path = tuple(group["business_path"])
        choices = requested[path]
        prefix = next(iter(choices)) if len(choices) == 1 else ""
        if not prefix or counts[prefix] != 1:
            prefix = "WQR" + execution_inputs.content_hash(path)[:12].upper()
            group["gaps"].append("【待确认：功能项拼音缩写】；WQR 编号仅为本报告临时显示标识，原用例标识未修改。")
        prefixes[path] = prefix
    return prefixes


def _button_names(value: str) -> str:
    return re.sub(r"点击(搜索|新增|提交)(?:按钮)?(?=$|[，。；、\s])", r"点击【\1】", value)


def _steps(case: dict) -> list[str]:
    return case_document.functional_steps(case)


def _number_steps(original: list[str]) -> tuple[list[str], dict[int, int]]:
    steps, mapping, leading = [], {}, []
    for old, original_step in enumerate(original, 1):
        source = NUMBERED.sub(r"\2", original_step, count=1)
        if re.match(r"^等待\s*\d+(?:\.\d+)?\s*(?:毫秒|秒|分钟|ms|s)\s*$", source):
            if steps:
                steps[-1] += "（" + source + "）"
            else:
                leading.append(source)
            mapping[old] = max(1, len(steps))
            continue
        if source.startswith(("等待出现", "等待消失")):
            source = "检查" + source[2:]
        source = "，".join([*leading, source])
        leading.clear()
        steps.append(_button_names(source))
        mapping[old] = len(steps)
    if not steps:
        steps = ["检查等待条件【待确认：业务检查动作】；原始时序：" + "，".join(leading)]
    return steps, mapping


def _expected_parts(value: str) -> list[tuple[int | None, str]]:
    lines = value.splitlines()
    if not any(NUMBERED.match(line) for line in lines):
        return [(None, value)]
    parts = []
    for line in lines:
        match = NUMBERED.match(line)
        if match:
            parts.append((int(match[1]), match[2]))
        elif parts:
            number, previous = parts[-1]
            parts[-1] = (number, previous + "\n" + line)
        else:
            parts.append((None, line))
    return parts


def _expected(case: dict, mapping: dict[int, int], last: int, gaps: list[str]) -> dict[int, str]:
    declared = case_document.functional_expected(case)
    parts = _expected_parts(declared) if declared is not None else [(None, case_document.MISSING_EXPECTED)]
    expected = defaultdict(list)
    for old, original_value in parts:
        value = original_value
        target = mapping.get(old, last)
        if old is not None and old not in mapping:
            value = f"【待确认：原预期编号 {old} 与步骤的对应关系】" + value
        expected[target].append(value)
    if any("【待确认" in value for values in expected.values() for value in values):
        gaps.append("预期存在待确认项；结构校验不代表验收判据或业务覆盖已确认。")
    return {number: "；".join(values) for number, values in sorted(expected.items())}


def _numbered(values: dict[int, str], identity: str, field: str, data: list[dict]) -> str:
    lines = []
    for number, original_value in values.items():
        value = original_value
        if "\n" in value or "\r" in value or len(value) > 4000:
            reference = f"{identity}/{field}/{number}"
            data.append({"id": reference, "value": value})
            value = value.splitlines()[0][:160] + f"（完整原文见「测试数据」：{reference}）"
        lines.append(f"{number}. {value}")
    return "\n".join(lines)


def _procedure(case: dict, identity: str, gaps: list[str], data: list[dict]) -> tuple[str, str, dict]:
    steps, mapping = _number_steps(_steps(case))
    expected = _expected(case, mapping, len(steps), gaps)
    step_text = _numbered(dict(enumerate(steps, 1)), identity, "步骤", data)
    expected_text = _numbered(expected, identity, "预期", data)
    gaps.extend(PENDING_FACT.findall(step_text + "\n" + expected_text))
    if display_lines(step_text, 52) > 20 or display_lines(expected_text, 52) > 20:
        data.extend([{"id": identity + "/完整步骤", "value": step_text}, {"id": identity + "/完整预期", "value": expected_text}])
        gaps.append("完整操作链超过主表可读行高，原编号与对应关系保留在「测试数据」，没有删减步骤或预期。")
        step_text = f"1. 按「测试数据」{identity}/完整步骤 执行本用例的完整操作链。"
        expected_text = f"1. 按「测试数据」{identity}/完整预期 核对对应步骤的原始判据。"
    return step_text, expected_text, mapping


def _actual(group: dict) -> str:
    results = []
    observations = []
    replayed = any(item.get("result") and item.get("run_id") for item in group["items"])
    for item in group["items"]:
        result = item.get("result") or {}
        observed = item.get("recording_result")
        if observed:
            observations.append(("录制观察（与回放结果分开保留）：" if replayed else "录制观察（尚未回放）：") + text(observed))
        if result:
            detail = result.get("detail", {})
            value = result.get("actual")
            if value is None or value == "":
                value = detail.get("assertions") or result.get("reason") or "详见执行明细"
            verdict = task_report_data.observation_verdict(item)
            results.append(f"{item.get('run_id', '')}：{STATUS[verdict]}；{text(value)}")
    return "\n".join([*results, *observations])


def _screenshots(group: dict) -> list[str]:
    found = []
    for item in group["items"]:
        result = item.get("result") or {}
        detail = result.get("detail", {})
        attempts = detail.get("attempts", [])
        for record in [result, detail, *(attempts if isinstance(attempts, list) else [])]:
            if not isinstance(record, dict):
                continue
            value = record.get("screenshot") or record.get("screenshots")
            if value:
                found.extend(_screenshot_reference(source, item) for source in (value if isinstance(value, list) else [value]))
    return list(dict.fromkeys(found))


def _screenshot_reference(value, item: dict) -> str:
    source = text(value)
    if "://" not in source:
        try:
            if data_refs.is_absolute(source):
                source = data_refs.store(source)
            elif item.get("evidence_directory"):
                source = data_refs.store(data_refs.resolve(item["evidence_directory"]) / source)
        except (OSError, ValueError):
            if not data_refs.is_absolute(source) and item.get("evidence_directory"):
                source = str(Path(item["evidence_directory"]) / source)
            source += "（原始路径，当前数据目录无法解析）"
    return f"运行 {item.get('run_id', '未记录')}；截图来源：{source}（来源证据未随 Excel 嵌入，请在对应运行中读取）"


def _row(group: dict, identity: str, data: list[dict]) -> dict:
    case, gaps = group["definition"], group["gaps"]
    metadata = document(case)
    steps, expected, mapping = _procedure(case, identity, gaps, data)
    author = metadata.get("author") or "【待确认：用例编制人】"
    if not metadata.get("author"):
        gaps.append(author)
    preconditions = case_document.functional_preconditions(case)
    description = case_document.functional_description(case)
    gaps.extend(PENDING_FACT.findall(preconditions + "\n" + description))
    if not case.get("preconditions"):
        gaps.append(preconditions)
    verdict = STATUS[group["status"]]
    if group["status"] == "Fail" and group["final_status"] == "Pass":
        verdict += "（曾失败，后续通过）"
    remarks = [text(metadata["remarks"])] if metadata.get("remarks") else []
    remarks += [f"原始标识：{case['id']}；冻结定义：{group['definition_hash']}", *gaps]
    remarks += [str(item["note"]) for item in group["items"] if item.get("note")]
    remarks += ["本表每个冻结定义一行；全部轮次、重试和证据引用见「执行明细」。"]
    if len(group["items"]) > 1:
        remarks.append("各轮结论：" + " → ".join(STATUS[task_report_data.observation_verdict(item)] for item in group["items"]))
    path = group["business_path"]
    environment = list(dict.fromkeys(value for item in group["items"] for value in item.get("environment", [])))
    return {"id": identity, "name": "-".join(CHAPTER.sub("", part).strip() for part in path), "trace": path[-1],
            "description": description, "preconditions": preconditions,
            "steps": steps, "expected": expected,
            "author": author, "actual": _actual(group), "conclusion": verdict, "executor": metadata.get("executor", ""),
            "environment": "\n".join(environment), "remarks": "\n".join(dict.fromkeys(remarks)), "screenshots": _screenshots(group),
            "group": group, "step_mapping": mapping}


def rows(snapshot: dict) -> tuple[list[dict], list[dict]]:
    groups = task_report_data.logical_cases(snapshot)
    _paths(groups, snapshot["project"], snapshot.get("sources", {}).get("report_business_paths"))
    prefixes = _prefixes(groups)
    sequences, data, result = defaultdict(int), [], []
    for group in groups:
        path = tuple(group["business_path"])
        sequences[path] += 1
        identity = f"FT-{prefixes[path]}-{sequences[path]:03d}"
        result.append(_row(group, identity, data))
    return result, data
