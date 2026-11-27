"""Manage the user-facing `autoSkill交付/` layout, multi-requirement migration and manual-edit diffs.

Delivery tracking manifests live under `<data-root>/.local/deliveries/` so internal state never
scatters into the user's development repository. Single-requirement projects deliver directly into
`autoSkill交付/`; when a second independent requirement arrives, the original workbook and all of
its historical reports migrate together into that requirement's subfolder.
"""
from __future__ import annotations

import datetime
import errno
import hashlib
import json
import os
import re
import uuid
from pathlib import Path

from . import db, execution_inputs, paths
from .locking import named_lock

DELIVERY_FOLDER_NAME = "autoSkill交付"
WORKBOOK_NAME = "功能测试用例.xlsx"
REPORT_PREFIX = "测试报告-"
BUGS_PREFIX = "Bug清单-"
UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
COMPARE_FIELDS = (
    ("title", "测试说明"),
    ("preconditions", "前置条件"),
    ("steps", "测试步骤"),
    ("expected", "预期结果"),
    ("author", "用例编制人"),
    ("conclusion", "测试结论"),
    ("remarks", "备注"),
)


def safe_name(name: str, fallback: str = "交付文件") -> str:
    """Sanitize a display name into a portable filename or folder name."""
    return (UNSAFE.sub("-", str(name or "")).strip().strip(".") or fallback)[:120]


def delivery_root(target: str | Path | None = None) -> Path | None:
    """Resolve `<target>/autoSkill交付` without nesting `autoSkill交付/autoSkill交付`."""
    base = Path(target).expanduser().resolve() if target else paths.delivery_dir()
    if base is None:
        return None
    return base if base.name == DELIVERY_FOLDER_NAME else base / DELIVERY_FOLDER_NAME


def manifest_key(root: Path) -> str:
    """Stable key for one delivery root stored inside `.local/deliveries/`."""
    return hashlib.sha256(str(root.resolve()).lower().encode("utf-8")).hexdigest()[:24]


def manifest_path(root: Path) -> Path:
    """Internal tracking file location; never written inside the user's project directory."""
    return paths.local() / "deliveries" / f"{manifest_key(root)}.json"


def load_manifest(root: Path) -> dict:
    """Read an existing delivery manifest or return an empty single-mode manifest."""
    file = manifest_path(root)
    if file.is_file():
        try:
            data = json.loads(file.read_bytes())
            if isinstance(data, dict) and isinstance(data.get("requirements"), dict):
                data.setdefault("schema", 1)
                data.setdefault("root", str(root.resolve()))
                data.setdefault("mode", "multi" if len(data["requirements"]) > 1 else "single")
                return data
        except (OSError, ValueError):
            pass
    return {"schema": 1, "root": str(root.resolve()), "mode": "single", "requirements": {}}


def save_manifest(root: Path, manifest: dict) -> None:
    """Persist delivery tracking metadata under `.local/deliveries/`."""
    manifest["root"] = str(root.resolve())
    manifest["updated_at"] = db.now()
    execution_inputs.write_json(manifest_path(root), manifest)


def delivery_lock(root: Path):
    """Serialize layout checks, migrations and writes targeting the same delivery root."""
    return named_lock("deliveries", manifest_key(root))


def is_file_in_use(error: BaseException, *, path: Path | None = None) -> bool:
    """Recognize Windows sharing/lock violations and busy-file errors."""
    if not isinstance(error, OSError):
        return False
    winerror = getattr(error, "winerror", None)
    if winerror in (32, 33):
        return True
    if getattr(error, "errno", None) == errno.EBUSY:
        return True
    return isinstance(error, PermissionError) and (path is None or path.exists())


def file_in_use_message(filename: str) -> str:
    """Actionable Chinese guidance when Excel or another process holds a lock on the target file."""
    return f"文件「{filename}」正被其他程序占用（可能正在 Excel 中打开）。请先关闭正在打开的 {filename} 后再继续。"


def digest(file: Path) -> str:
    """Compute SHA-256 hex digest of a file."""
    with file.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def atomic_copy(source: Path, destination: Path) -> None:
    """Copy bytes through a sibling temp file so partial writes never replace a live file."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name("." + destination.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with source.open("rb") as src, temporary.open("wb") as out:
            while chunk := src.read(1024 * 1024):
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def _unique_folder_name(name: str, used: set[str]) -> str:
    base = safe_name(name, "需求")
    candidate = base
    index = 2
    lowered = {item.lower() for item in used}
    while candidate.lower() in lowered:
        candidate = f"{base}-{index}"
        index += 1
    return candidate


def resolve_requirement(manifest: dict, *, requirement: str | None = None, project: dict | None = None,
                        task_id: str | None = None) -> tuple[str, str]:
    """Resolve stable requirement key and display name from explicit args or existing tracking."""
    if isinstance(requirement, str) and requirement.strip():
        clean = requirement.strip()
        return clean, clean
    project_id = (project or {}).get("id") or ""
    requirements = manifest.get("requirements", {})
    if task_id:
        for key, entry in requirements.items():
            if task_id in (entry.get("task_ids") or []):
                return key, entry.get("name") or key
    if project_id:
        matching = [(key, entry) for key, entry in requirements.items() if entry.get("project_id") == project_id]
        if len(matching) == 1:
            return matching[0][0], matching[0][1].get("name") or matching[0][0]
    fallback = ((project or {}).get("name") or project_id or "默认需求").strip()
    return fallback, fallback


def _root_files_for_requirement(root: Path, entry: dict) -> list[Path]:
    """Collect the workbook and every historical report belonging to the single root requirement."""
    candidates: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        if path.is_file() and path not in seen:
            seen.add(path)
            candidates.append(path)

    add(root / (entry.get("excel", {}).get("filename") or WORKBOOK_NAME))
    add(root / WORKBOOK_NAME)
    for report in entry.get("reports", []):
        for key in ("markdown", "bugs"):
            if report.get(key):
                add(root / report[key])
    if root.is_dir():
        for item in sorted(root.iterdir()):
            if item.is_file() and (item.name.startswith((REPORT_PREFIX, BUGS_PREFIX)) and item.suffix == ".md"):
                add(item)
    return candidates


def prepare_requirement_folder(root: Path, manifest: dict, req_key: str, req_name: str, *,
                               project_id: str = "", task_id: str | None = None) -> dict:
    """Resolve the destination folder for a requirement, migrating single -> multi on a 2nd requirement."""
    root.mkdir(parents=True, exist_ok=True)
    requirements = manifest["requirements"]
    migrated = None

    if not requirements:
        manifest["mode"] = "single"
        requirements[req_key] = {
            "key": req_key, "name": req_name, "folder_name": safe_name(req_name, "需求"),
            "project_id": project_id, "task_ids": [task_id] if task_id else [], "excel": {}, "reports": [],
        }
        return {"folder": root, "entry": requirements[req_key], "migrated": None}

    if req_key in requirements:
        entry = requirements[req_key]
        if project_id and not entry.get("project_id"):
            entry["project_id"] = project_id
        if task_id and task_id not in entry.setdefault("task_ids", []):
            entry["task_ids"].append(task_id)
        folder = root if manifest.get("mode") == "single" else root / entry["folder_name"]
        folder.mkdir(parents=True, exist_ok=True)
        return {"folder": folder, "entry": entry, "migrated": None}

    used_folders = {entry.get("folder_name", "") for entry in requirements.values() if entry.get("folder_name")}
    if manifest.get("mode") == "single" and len(requirements) == 1:
        existing_key, existing_entry = next(iter(requirements.items()))
        existing_folder_name = _unique_folder_name(existing_entry.get("name") or existing_key, set())
        existing_dest = root / existing_folder_name
        existing_dest.mkdir(parents=True, exist_ok=True)
        files_to_move = _root_files_for_requirement(root, existing_entry)
        moved: list[tuple[Path, Path]] = []
        try:
            for source_file in files_to_move:
                dest_file = existing_dest / source_file.name
                source_file.replace(dest_file)
                moved.append((source_file, dest_file))
        except OSError as error:
            for original_file, dest_file in reversed(moved):
                try:
                    if dest_file.exists():
                        dest_file.replace(original_file)
                except OSError:
                    pass
            try:
                if existing_dest.is_dir() and not any(existing_dest.iterdir()):
                    existing_dest.rmdir()
            except OSError:
                pass
            failing = source_file
            code = "file_in_use" if is_file_in_use(error, path=failing) else "permission_denied"
            reason = (file_in_use_message(failing.name) if code == "file_in_use"
                      else f"无法将原需求文件「{failing.name}」归入子文件夹：{type(error).__name__}：{error}")
            return {"error": {"name": failing.name, "path": str(failing), "code": code, "reason": reason}}
        existing_entry["folder_name"] = existing_folder_name
        manifest["mode"] = "multi"
        used_folders = {existing_folder_name}
        migrated = {"requirement": existing_entry.get("name") or existing_key, "from": str(root),
                    "to": str(existing_dest), "files": [dest.name for _, dest in moved]}

    new_folder_name = _unique_folder_name(req_name, used_folders)
    new_folder = root / new_folder_name
    new_folder.mkdir(parents=True, exist_ok=True)
    requirements[req_key] = {
        "key": req_key, "name": req_name, "folder_name": new_folder_name,
        "project_id": project_id, "task_ids": [task_id] if task_id else [], "excel": {}, "reports": [],
    }
    manifest["mode"] = "multi"
    return {"folder": new_folder, "entry": requirements[req_key], "migrated": migrated}


def allocate_report_names(folder: Path, *, now: datetime.datetime | None = None, stamp: str | None = None) -> tuple[str, str, str, str]:
    """Pick paired minute-precision names; reruns in the same minute append `-2`, `-3`, etc."""
    current = now or datetime.datetime.now()  # noqa: DTZ005  # Report filenames reflect local wall-clock time to the minute.
    minute_stamp = stamp or current.strftime("%Y%m%d-%H%M")
    index = 1
    while True:
        suffix = "" if index == 1 else f"-{index}"
        markdown_name = f"{REPORT_PREFIX}{minute_stamp}{suffix}.md"
        bugs_name = f"{BUGS_PREFIX}{minute_stamp}{suffix}.md"
        if not (folder / markdown_name).exists() and not (folder / bugs_name).exists():
            return markdown_name, bugs_name, minute_stamp, suffix
        index += 1


def _snippet(value: str, limit: int = 40) -> str:
    clean = re.sub(r"\s+", " ", str(value or "")).strip()
    if not clean:
        return "（空）"
    return clean if len(clean) <= limit else clean[:limit] + "…"


def _extract_workbook_cases(file: Path) -> list[dict]:
    """Read functional test rows from a 14-column or 11-column workbook without failing on edits."""
    from openpyxl import load_workbook

    book = load_workbook(file, read_only=True, data_only=True)
    try:
        mapping: dict[str, str] = {}
        if "编号映射" in book.sheetnames:
            for row in book["编号映射"].iter_rows(min_row=2, values_only=True):
                if row and row[0] and len(row) > 1 and row[1]:
                    mapping[str(row[0]).strip()] = str(row[1]).strip()
        sheet = book["功能测试用例"] if "功能测试用例" in book.sheetnames else book.worksheets[0]
        rows_iter = sheet.iter_rows(values_only=True)
        header_row = next(rows_iter, None)
        if not header_row:
            return []
        headers = [str(cell or "").strip() for cell in header_row]
        col_map = {
            "id": _find_col(headers, ("用例标识", "用例编号", "编号")),
            "name": _find_col(headers, ("用例名称",)),
            "title": _find_col(headers, ("测试说明", "用例标题", "标题")),
            "preconditions": _find_col(headers, ("前置条件",)),
            "steps": _find_col(headers, ("测试步骤", "操作步骤", "步骤")),
            "expected": _find_col(headers, ("预期结果", "期望结果")),
            "author": _find_col(headers, ("用例编制人",)),
            "actual": _find_col(headers, ("实际结果",)),
            "conclusion": _find_col(headers, ("测试结论", "执行结果", "结果")),
            "remarks": _find_col(headers, ("备注",)),
        }
        cases: list[dict] = []
        for raw in rows_iter:
            values = list(raw)
            case_id = _cell_text(values, col_map["id"])
            title = _cell_text(values, col_map["title"])
            steps = _cell_text(values, col_map["steps"])
            expected = _cell_text(values, col_map["expected"])
            # Skip group separator rows that only carry a merged group label in column A.
            if not case_id or (not title and not steps and not expected):
                continue
            original_id = mapping.get(case_id) or case_id
            cases.append({
                "key": original_id,
                "id": case_id,
                "original_id": original_id,
                "name": _cell_text(values, col_map["name"]),
                "title": title or _cell_text(values, col_map["name"]) or case_id,
                "preconditions": _cell_text(values, col_map["preconditions"]),
                "steps": steps,
                "expected": expected,
                "author": _cell_text(values, col_map["author"]),
                "actual": _cell_text(values, col_map["actual"]),
                "conclusion": _cell_text(values, col_map["conclusion"]),
                "remarks": _cell_text(values, col_map["remarks"]),
            })
        return cases
    finally:
        book.close()


def _find_col(headers: list[str], candidates: tuple[str, ...]) -> int | None:
    for candidate in candidates:
        if candidate in headers:
            return headers.index(candidate)
    return None


def _cell_text(values: list, index: int | None) -> str:
    if index is None or index >= len(values) or values[index] is None:
        return ""
    return str(values[index]).strip()


def _diff_case_lists(existing_cases: list[dict], incoming_cases: list[dict]) -> dict:
    existing_by_key = {row["key"]: row for row in existing_cases}
    incoming_by_key = {row["key"]: row for row in incoming_cases}
    edited, user_only, incoming_only, unchanged = [], [], [], 0
    for key, current in existing_by_key.items():
        if key not in incoming_by_key:
            user_only.append({"id": current["id"], "original_id": key, "title": current["title"]})
            continue
        target = incoming_by_key[key]
        changed_fields = [
            {"field": field, "label": label, "existing": current.get(field, ""), "incoming": target.get(field, "")}
            for field, label in COMPARE_FIELDS if current.get(field, "") != target.get(field, "")
        ]
        if changed_fields:
            edited.append({"id": current["id"], "original_id": key, "title": current["title"], "fields": changed_fields})
        else:
            unchanged += 1
    for key, target in incoming_by_key.items():
        if key not in existing_by_key:
            incoming_only.append({"id": target["id"], "original_id": key, "title": target["title"],
                                  "conclusion": target.get("conclusion") or "未执行"})
    return {"edited": edited, "user_only": user_only, "incoming_only": incoming_only, "unchanged": unchanged}


def _format_diff_summary(filename: str, existing_count: int, incoming_count: int, details: dict) -> str:
    edited, user_only, incoming_only = details["edited"], details["user_only"], details["incoming_only"]
    lines = [f"检测到你手工修改过「{filename}」，我已先保留你当前的表格，没有直接覆盖。", "具体改动如下："]
    if edited:
        lines.append(f"- 你手工修改了 {len(edited)} 条用例：")
        for item in edited[:4]:
            notes = "；".join(
                f"「{chg['label']}」你改成了“{_snippet(chg['existing'])}”（新版是“{_snippet(chg['incoming'])}”）"
                for chg in item["fields"][:2]
            )
            lines.append(f"  · {item['id']}（{_snippet(item['title'], 24)}）：{notes}")
        if len(edited) > 4:
            lines.append(f"  · 另外还有 {len(edited) - 4} 条你手工改过的用例。")
    if user_only:
        names = "、".join(f"{item['id']}（{_snippet(item['title'], 18)}）" for item in user_only[:3])
        lines.append(f"- 你在表里手工保留/新增了 {len(user_only)} 条用例：{names}。")
    if incoming_only:
        names = "、".join(f"{item['id']}（{_snippet(item['title'], 18)}）" for item in incoming_only[:3])
        lines.append(f"- 本次新版准备补充 {len(incoming_only)} 条用例：{names}。")
    if not edited and not user_only and not incoming_only:
        lines.append(f"- 用例行内容基本一致（当前 {existing_count} 条，新版 {incoming_count} 条），但表格样式或附表有手工修改。")
    merged_total = incoming_count + len(user_only)
    lines.append(
        f"如果按下面的示例合并更新，你会看到保留你手工修改内容并合入本次更新的最新版「{filename}」（共 {merged_total} 条用例）。"
        "要按这样更新吗？确认后可带上 --overwrite-delivery 完成更新。"
    )
    return "\n".join(lines)


def _format_merge_preview(existing_count: int, details: dict) -> tuple[str, list[dict]]:
    preview_rows: list[dict] = []
    lines = ["合并后你会看到的用例示例："]
    for item in details["edited"][:2]:
        labels = "、".join(chg["label"] for chg in item["fields"][:3])
        first = item["fields"][0]
        entry = {
            "id": item["id"], "title": item["title"], "action": "保留人工修改并合并",
            "detail": f"保留你修改的「{labels}」（例如「{first['label']}」保持为“{_snippet(first['existing'])}”），其余未改列同步新版",
        }
        preview_rows.append(entry)
        lines.append(f"{len(preview_rows)}. [保留人工修改] {entry['id']}（{_snippet(entry['title'], 24)}）：{entry['detail']}。")
    for item in details["user_only"][:2]:
        entry = {"id": item["id"], "title": item["title"], "action": "保留人工新增", "detail": "原样保留你手工添加的这条用例"}
        preview_rows.append(entry)
        lines.append(f"{len(preview_rows)}. [保留人工新增] {entry['id']}（{_snippet(entry['title'], 24)}）：{entry['detail']}。")
    for item in details["incoming_only"][:2]:
        entry = {"id": item["id"], "title": item["title"], "action": "合入新版新增",
                 "detail": f"补充到用例表，当前结论为「{item['conclusion']}」"}
        preview_rows.append(entry)
        lines.append(f"{len(preview_rows)}. [本次新增] {entry['id']}（{_snippet(entry['title'], 24)}）：{entry['detail']}。")
    if not preview_rows:
        lines.append(f"1. [更新新版] 保留原有 {existing_count} 条用例并同步本次最新生成结果。")
    return "\n".join(lines), preview_rows


def compare_workbooks(existing_path: Path, incoming_path: Path) -> dict:
    """Produce a conversational Chinese diff summary and merge preview for a hand-edited workbook."""
    try:
        existing_cases = _extract_workbook_cases(existing_path)
        incoming_cases = _extract_workbook_cases(incoming_path)
    except Exception:  # noqa: BLE001  # Corrupted or non-standard edited workbooks must still stay protected.
        summary = (
            f"检测到你手工修改过「{existing_path.name}」，我已先保留你当前的文件，没有直接覆盖。\n"
            "因为当前文件无法按标准用例表逐行比对，建议先核对你手工改过的内容；"
            "如果确认直接替换成最新生成的版本，请告诉我，或重新执行并加上 --overwrite-delivery。"
        )
        preview = f"合并后示例：确认覆盖后，「{existing_path.name}」将更新为本次生成的最新版用例表。"
        return {"diff_summary": summary, "merge_preview": preview, "merge_preview_rows": [],
                "diff_details": {"edited": [], "user_only": [], "incoming_only": [], "unchanged": 0, "unreadable": True}}

    details = _diff_case_lists(existing_cases, incoming_cases)
    summary = _format_diff_summary(existing_path.name, len(existing_cases), len(incoming_cases), details)
    preview, preview_rows = _format_merge_preview(len(existing_cases), details)
    return {"diff_summary": summary, "merge_preview": preview, "merge_preview_rows": preview_rows, "diff_details": details}
