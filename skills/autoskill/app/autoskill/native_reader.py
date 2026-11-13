"""Bounded native result projections keep full Python results available on explicit request."""
from __future__ import annotations

import contextlib
import hashlib
import json
from pathlib import Path

from . import data_refs, native_run, paging


def _file_ref(path: Path) -> dict:
    if not path.is_file():
        return {"path": str(path), "available": False}
    with path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    return {"path": str(path), "sha256": digest, "size": path.stat().st_size, "available": True}


def _ref(report: dict, pointer: str, value) -> dict:
    return {**report, "json_pointer": pointer, "content_revision": paging.revision(value)}


def _summary(row: dict) -> dict:
    detail = row.get("detail", {})
    item = {key: row[key] for key in ("id", "case_id", "case_title", "status", "reason", "duration_ms", "engine") if key in row}
    if len(item.get("reason", "")) > 1200:
        item.update(reason=item["reason"][:1200], reason_truncated=True)
    if len(item.get("case_title", "")) > 400:
        item.update(case_title=item["case_title"][:400], title_truncated=True)
    item.update({key: detail[key] for key in ("business_status", "business_verified", "verification", "execution_status") if key in detail})
    return {**item, "turn_count": len(detail.get("turns", [])), "assertion_count": len(detail.get("assertions", [])),
            "detail_ref": {"result_id": row["id"], "case_id": row["case_id"], "content_revision": paging.revision(detail)}}


def _outcome(outcome: dict, report: dict) -> dict:
    summary = {}
    for key, value in outcome.items():
        if key in ("cases", "events", "evidence"):
            summary[key + "_ref"] = {**_ref(report, "/" + key, value), "count": len(value)}
        elif len(json.dumps(value, ensure_ascii=False)) > 12000:
            summary[key + "_ref"] = {**_ref(report, "/" + key.replace("~", "~0").replace("/", "~1"), value), "content_loaded": False}
        else:
            summary[key] = value
    return summary


def _case(rows: list[dict], case_id: str) -> dict:
    selected = ([row for row in rows if str(row["id"]) == case_id.removeprefix("result:")]
                if case_id.startswith("result:") else [row for row in rows if row["case_id"] == case_id])
    if not selected:
        raise paging.PageError("本次运行没有该用例结果")
    if len(selected) != 1:
        raise paging.PageError("本次运行存在相同用例编号；请用 result:<结果 id> 读取具体结果")
    return selected[0]


def _audio_paths(value: dict) -> dict:
    """Project only known transport receipts, leaving business payloads and raw evidence intact."""
    result = dict(value)
    audio = result.get("audio_received")
    if isinstance(audio, dict) and isinstance(audio.get("path"), str):
        with contextlib.suppress(ValueError):
            result["audio_received"] = {**audio, "path": str(data_refs.resolve(audio["path"]))}
    for key in ("turns", "attempts"):
        if isinstance(result.get(key), list):
            result[key] = [_audio_paths(item) if isinstance(item, dict) else item for item in result[key]]
    if isinstance(result.get("outcome"), dict):
        result["outcome"] = _audio_paths(result["outcome"])
    return result


def _public_result(result: dict) -> dict:
    outcome = dict(result["outcome"])
    if isinstance(outcome.get("cases"), list):
        outcome["cases"] = [_audio_paths(case) for case in outcome["cases"]]
    return {**result,
            "results": [{**row, "detail": _audio_paths(row["detail"])} for row in result["results"]],
            "outcome": outcome}


def status(run_id: str, *, limit: int = 50, cursor: str | None = None, all_items: bool = False, case_id: str | None = None) -> dict:
    """Return summaries by default; only --all expands the legacy complete result."""
    result = native_run.get(run_id)
    content_revision = paging.revision(result)
    if case_id and (cursor or all_items):
        raise paging.PageError("单用例读取不能同时使用分页游标或 --all")
    rows, page = paging.select(result["results"], content_revision=content_revision, query={"run": run_id},
                               cursor=cursor, limit=limit, all_items=all_items)
    if all_items:
        return {**_public_result(result), "page": page}
    report = _file_ref(Path(result["evidence_dir"]) / "outcome.json")
    summary = {key: value for key, value in result.items() if key not in ("results", "outcome")}
    summary.update(outcome=_outcome(result["outcome"], report), report=report, page=page)
    if case_id:
        row = _case(result["results"], case_id)
        detail = {key: value for key, value in row["detail"].items() if key != "evidence"}
        detail["evidence_ref"] = {**_ref(report, "/evidence", row["detail"].get("evidence", [])), "count": len(row["detail"].get("evidence", []))}
        summary.update(results=[_summary(row)], case=_audio_paths(detail), content_revision=paging.revision(row["detail"]))
        summary["page"] = {**page, "returned": 1, "has_more": False, "next_cursor": None}
    else:
        summary["results"] = [_summary(row) for row in rows]
    return summary
