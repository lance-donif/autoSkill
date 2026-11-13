"""Independent deterministic answer checks against explicit expectations or frozen tabular data."""
from __future__ import annotations

import contextlib
import json
import math
import operator

from .ai_protocols import pointer

OPERATORS = {"eq": operator.eq, "ne": operator.ne, "gt": operator.gt, "ge": operator.ge, "lt": operator.lt, "le": operator.le,
             "in": lambda actual, expected: actual in expected, "contains": lambda actual, expected: expected in actual}


def _matches(row: dict, conditions: list[dict]) -> bool:
    for condition in conditions:
        actual = row.get(condition["field"])
        compare = OPERATORS.get(condition.get("op", "eq"))
        if compare is None:
            raise ValueError("参考数据筛选操作不支持：" + str(condition.get("op")))
        if not compare(actual, condition.get("value")):
            return False
    return True


def reference_value(rows: list[dict], reference: dict):
    filtered = [row for row in rows if _matches(row, reference.get("where", []))]
    if reference.get("group_by"):
        groups = {}
        for row in filtered:
            groups.setdefault(str(row.get(reference["group_by"])), []).append(row)
        nested = {key: value for key, value in reference.items() if key not in ("group_by", "where")}
        return {key: reference_value(values, nested) for key, values in groups.items()}
    field = reference.get("field")
    values = [row.get(field) for row in filtered] if field else filtered
    if reference.get("exclude_null", True) and field:
        values = [value for value in values if value is not None]
    if reference.get("distinct"):
        unique = {json.dumps(value, ensure_ascii=False, sort_keys=True): value for value in values}
        values = list(unique.values())
    operation = reference.get("operation", "count")
    if operation == "count":
        return len(values)
    if operation == "values":
        return values
    if operation == "sum":
        return sum(values)
    if operation == "average":
        return sum(values) / len(values) if values else None
    if operation == "min":
        return min(values) if values else None
    if operation == "max":
        return max(values) if values else None
    raise ValueError("参考计算不支持：" + str(operation))


def _answer(actual, path: str):
    if isinstance(actual, str):
        with contextlib.suppress(json.JSONDecodeError):
            actual = json.loads(actual)
    return pointer(actual, path) if path else actual


def check_answer(actual, turn: dict, dataset: list[dict]) -> list[dict]:
    checks = list(turn.get("checks", []))
    if "expected" in turn:
        checks.insert(0, {"kind": "equals", "expected": turn["expected"], "path": turn.get("answer_path", ""), "source": "explicit"})
    if "reference" in turn:
        checks.insert(0, {"kind": "equals", "expected": reference_value(dataset, turn["reference"]),
                          "path": turn.get("answer_path", ""), "source": "frozen_dataset", "reference": turn["reference"]})
    results = []
    for check in checks:
        expected = check.get("expected")
        try:
            observed = _answer(actual, check.get("path", ""))
            passed = _compare(observed, expected, check)
            error = ""
        except (KeyError, IndexError, ValueError, TypeError) as failure:
            observed, passed, error = actual, False, str(failure)
        results.append({"label": check.get("label", "回答符合独立预期"), "expected": expected, "actual": observed, "passed": passed,
                        "source": check.get("source", "explicit"), "reference": check.get("reference"), "error": error})
    return results


def _compare(actual, expected, check: dict) -> bool:
    kind = check.get("kind", "equals")
    if kind == "equals":
        return actual == expected
    if kind == "contains":
        return expected in actual
    if kind == "numeric":
        return math.isclose(float(actual), float(expected), rel_tol=float(check.get("relative_tolerance", 0)), abs_tol=float(check.get("tolerance", 0)))
    if kind == "subset":
        return all(key in actual and actual[key] == value for key, value in expected.items())
    raise ValueError("答案检查类型不支持：" + str(kind))
