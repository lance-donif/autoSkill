"""Copy only the finished Excel and report files into `<target>/autoSkill交付/`.

Everything else (recordings, screenshots, frozen inputs, evidence, sheets and delivery manifests)
stays inside the autoSkill data directory. Single-requirement projects write directly to
`autoSkill交付/`; adding a second independent requirement migrates the original workbook and all
historical reports into its own subfolder. Hand-edited workbooks are never silently overwritten.
"""
from __future__ import annotations

import datetime
from pathlib import Path

from . import db, delivery_layout, paths, projects

UNSAFE = delivery_layout.UNSAFE


def _safe(name: str) -> str:
    return delivery_layout.safe_name(name)


def _digest(file: Path) -> str:
    return delivery_layout.digest(file)


def _copy(source: Path, destination: Path) -> None:
    delivery_layout.atomic_copy(source, destination)


def _error_entry(destination: Path, error: OSError) -> dict:
    if delivery_layout.is_file_in_use(error, path=destination):
        return {"name": destination.name, "path": str(destination), "code": "file_in_use",
                "reason": delivery_layout.file_in_use_message(destination.name)}
    return {"name": destination.name, "path": str(destination), "code": "io_error",
            "reason": f"{type(error).__name__}：{error}"[:300]}


def _deliver_workbook(source_path: Path, destination: Path, *, last_delivered_sha256: str | None,
                      overwrite: bool) -> tuple[dict | None, dict | None]:
    """Deliver `功能测试用例.xlsx` with manual-edit protection and Windows file-in-use detection."""
    row = {"name": destination.name, "source": str(source_path), "path": str(destination), "kind": "workbook"}
    try:
        if not source_path.is_file():
            raise FileNotFoundError("交付原件不存在")
        new_digest = _digest(source_path)
        row.update(sha256=new_digest, size=source_path.stat().st_size)
        if destination.exists():
            existing_digest = _digest(destination) if destination.is_file() else None
            if existing_digest == new_digest:
                row["unchanged"] = True
                return row, None
            auto_updatable = bool(last_delivered_sha256 and existing_digest == last_delivered_sha256)
            if not overwrite and not auto_updatable:
                comparison = delivery_layout.compare_workbooks(destination, source_path)
                row.update(kept=True, manual_edit_detected=True, requires_confirmation=True,
                           code="manual_edit_conflict", existing_sha256=existing_digest,
                           reason=comparison["diff_summary"], **comparison)
                return row, None
            _copy(source_path, destination)
            row["updated"] = True
        else:
            _copy(source_path, destination)
        return row, None
    except OSError as error:
        return None, _error_entry(destination, error)


def copy(files: list[tuple[str, str | Path]], target: str | Path | None = None, *, overwrite: bool = False) -> dict:
    """Deliver (file name, source) pairs into one folder; never raises for a file error."""
    folder = Path(target).expanduser().resolve() if target else paths.delivery_dir()
    if folder is None:
        return {"folder": None, "files": [], "errors": [],
                "note": "没有交付目录：设置 AUTOSKILL_DELIVERY_DIR，或把 --deliver-to 指向用户项目目录"}
    receipt = {"folder": str(folder), "files": [], "errors": []}
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        return {**receipt, "code": "permission_denied",
                "errors": [{"name": None, "code": "permission_denied", "reason": f"交付目录不可写：{type(error).__name__}"}]}
    for name, source in files:
        source_path, destination = Path(source).expanduser().resolve(), folder / _safe(name)
        if destination.suffix.lower() == ".xlsx":
            row, err = _deliver_workbook(source_path, destination, last_delivered_sha256=None, overwrite=overwrite)
            if err:
                receipt["errors"].append(err)
                if err.get("code") == "file_in_use":
                    receipt["code"] = "file_in_use"
            elif row:
                receipt["files"].append(row)
                if row.get("manual_edit_detected"):
                    receipt.update(code="manual_edit_conflict", requires_confirmation=True,
                                   diff_summary=row["diff_summary"], merge_preview=row["merge_preview"],
                                   merge_preview_rows=row["merge_preview_rows"])
            continue
        row = {"name": destination.name, "source": str(source_path), "path": str(destination)}
        try:
            if not source_path.is_file():
                raise FileNotFoundError("交付原件不存在")
            digest = _digest(source_path)
            row.update(sha256=digest, size=source_path.stat().st_size)
            if destination.exists():
                if destination.is_file() and _digest(destination) == digest:
                    row["unchanged"] = True
                elif not overwrite:
                    row.update(kept=True, existing_sha256=_digest(destination),
                               reason="交付目录已有同名文件且内容不同；保留原文件，确认后用覆盖方式重新交付")
                else:
                    _copy(source_path, destination)
            else:
                _copy(source_path, destination)
        except OSError as error:
            err = _error_entry(destination, error)
            receipt["errors"].append(err)
            if err.get("code") == "file_in_use":
                receipt["code"] = "file_in_use"
            continue
        receipt["files"].append(row)
    return receipt


def _project_info(project: dict | None, project_id: str | None) -> dict:
    if isinstance(project, dict) and (project.get("id") or project.get("name")):
        return project
    if project_id:
        try:
            return projects.meta(project_id)
        except (OSError, ValueError):
            return {"id": project_id, "name": project_id}
    return {}


def _attach_workbook_outcome(receipt: dict, entry: dict, row: dict | None, err: dict | None, *, report_id: str | None = None) -> None:
    if err:
        receipt["errors"].append(err)
        if err.get("code") == "file_in_use":
            receipt["code"] = "file_in_use"
        return
    if not row:
        return
    receipt["files"].append(row)
    if row.get("manual_edit_detected"):
        receipt.update(code="manual_edit_conflict", requires_confirmation=True,
                       diff_summary=row["diff_summary"], merge_preview=row["merge_preview"],
                       merge_preview_rows=row["merge_preview_rows"])
    else:
        entry["excel"] = {
            "filename": row["name"], "sha256": row["sha256"],
            "source_report_id": report_id or "", "updated_at": db.now(),
        }


def plan_receipt(project: dict, receipt: dict, target: str | Path | None = None, *,
                 requirement: str | None = None, overwrite: bool = False) -> dict:
    """Deliver only the latest 14-column workbook into `<target>/autoSkill交付/`."""
    root = delivery_layout.delivery_root(target)
    if root is None:
        return {"folder": None, "files": [], "errors": [],
                "note": "没有交付目录：设置 AUTOSKILL_DELIVERY_DIR，或把 --deliver-to 指向用户项目目录"}
    with delivery_layout.delivery_lock(root):
        manifest = delivery_layout.load_manifest(root)
        req_name_hint = requirement or receipt.get("requirement")
        req_key, req_name = delivery_layout.resolve_requirement(
            manifest, requirement=req_name_hint, project=project, task_id=receipt.get("task_id"),
        )
        try:
            prepared = delivery_layout.prepare_requirement_folder(
                root, manifest, req_key, req_name, project_id=project.get("id", ""), task_id=receipt.get("task_id"),
            )
        except OSError as error:
            return {"folder": str(root), "root": str(root), "requirement": req_name, "files": [],
                    "code": "permission_denied",
                    "errors": [{"name": None, "code": "permission_denied", "reason": f"交付目录不可写：{type(error).__name__}"}]}
        if prepared.get("error"):
            err = prepared["error"]
            return {"folder": str(root), "root": str(root), "mode": manifest.get("mode", "single"),
                    "requirement": req_name, "files": [], "code": err.get("code"), "errors": [err]}

        folder, entry = prepared["folder"], prepared["entry"]
        result = {"folder": str(folder), "root": str(root), "mode": manifest["mode"],
                  "requirement": entry["name"], "files": [], "errors": []}
        if prepared.get("migrated"):
            result["migrated"] = prepared["migrated"]

        source_excel = receipt.get("delivery_excel")
        if source_excel:
            destination = folder / delivery_layout.WORKBOOK_NAME
            row, err = _deliver_workbook(
                Path(source_excel).expanduser().resolve(), destination,
                last_delivered_sha256=entry.get("excel", {}).get("sha256"), overwrite=overwrite,
            )
            report_id = (receipt.get("test_report") or {}).get("report_id") or receipt.get("initial_report_id")
            _attach_workbook_outcome(result, entry, row, err, report_id=report_id)

        delivery_layout.save_manifest(root, manifest)
        return result


def _deliver_markdown_file(source: Path, destination: Path, kind: str) -> tuple[dict | None, dict | None]:
    row = {"name": destination.name, "source": str(source), "path": str(destination), "kind": kind}
    try:
        if not source.is_file():
            raise FileNotFoundError("交付原件不存在")
        file_digest = _digest(source)
        row.update(sha256=file_digest, size=source.stat().st_size)
        if destination.exists() and destination.is_file() and _digest(destination) == file_digest:
            row["unchanged"] = True
        else:
            _copy(source, destination)
        return row, None
    except OSError as error:
        return None, _error_entry(destination, error)


def _matching_existing_report(folder: Path, entry: dict, report_id: str | None,
                              markdown_source: Path | None, bugs_source: Path | None) -> dict | None:
    """Reuse existing delivered report names only when the exact report_id and bytes match."""
    if not report_id:
        return None
    for record in reversed(entry.get("reports", [])):
        if record.get("report_id") != report_id:
            continue
        md_ok = (not markdown_source or (record.get("markdown") and (folder / record["markdown"]).is_file()
                                         and _digest(folder / record["markdown"]) == _digest(markdown_source)))
        bug_ok = (not bugs_source or (record.get("bugs") and (folder / record["bugs"]).is_file()
                                      and _digest(folder / record["bugs"]) == _digest(bugs_source)))
        if md_ok and bug_ok:
            return record
    return None


def _deliver_report_markdowns(folder: Path, entry: dict, receipt: dict, result: dict,
                              now: datetime.datetime | None) -> None:
    md_src = Path(receipt["markdown"]).expanduser().resolve() if isinstance(receipt.get("markdown"), str) and receipt.get("markdown") else None
    bug_src = Path(receipt["bugs"]).expanduser().resolve() if isinstance(receipt.get("bugs"), str) and receipt.get("bugs") else None
    if not md_src and not bug_src:
        return
    try:
        reused = _matching_existing_report(folder, entry, receipt.get("report_id"), md_src, bug_src)
    except OSError:
        reused = None
    if reused:
        md_name, bug_name = reused.get("markdown"), reused.get("bugs")
        stamp, suffix = reused.get("stamp", ""), reused.get("suffix", "")
    else:
        md_name, bug_name, stamp, suffix = delivery_layout.allocate_report_names(folder, now=now)
    delivered = {}
    for kind, src, name in (("markdown", md_src, md_name), ("bugs", bug_src, bug_name)):
        if not src or not name:
            continue
        row, err = _deliver_markdown_file(src, folder / name, kind)
        if err:
            result["errors"].append(err)
            if err.get("code") == "file_in_use":
                result["code"] = "file_in_use"
        elif row:
            result["files"].append(row)
            delivered[kind] = row["name"]
    if not reused and delivered:
        entry.setdefault("reports", []).append({
            "report_id": receipt.get("report_id") or "", "stamp": stamp, "suffix": suffix,
            "markdown": delivered.get("markdown"), "bugs": delivered.get("bugs"), "delivered_at": db.now(),
        })


def report_receipt(receipt: dict, target: str | Path | None = None, *, requirement: str | None = None,
                   project: dict | None = None, overwrite: bool = False,
                   now: datetime.datetime | None = None) -> dict:
    """Deliver the latest workbook plus paired minute-stamped report and bug list Markdown files."""
    root = delivery_layout.delivery_root(target)
    if root is None:
        return {"folder": None, "files": [], "errors": [],
                "note": "没有交付目录：设置 AUTOSKILL_DELIVERY_DIR，或把 --deliver-to 指向用户项目目录"}
    with delivery_layout.delivery_lock(root):
        manifest = delivery_layout.load_manifest(root)
        project_info = _project_info(project, receipt.get("project_id"))
        req_key, req_name = delivery_layout.resolve_requirement(
            manifest, requirement=requirement or receipt.get("requirement"),
            project=project_info, task_id=receipt.get("task_id"),
        )
        try:
            prepared = delivery_layout.prepare_requirement_folder(
                root, manifest, req_key, req_name, project_id=project_info.get("id", ""), task_id=receipt.get("task_id"),
            )
        except OSError as error:
            return {"folder": str(root), "root": str(root), "requirement": req_name, "files": [],
                    "code": "permission_denied",
                    "errors": [{"name": None, "code": "permission_denied", "reason": f"交付目录不可写：{type(error).__name__}"}]}
        if prepared.get("error"):
            err = prepared["error"]
            return {"folder": str(root), "root": str(root), "mode": manifest.get("mode", "single"),
                    "requirement": req_name, "files": [], "code": err.get("code"), "errors": [err]}

        folder, entry = prepared["folder"], prepared["entry"]
        result = {"folder": str(folder), "root": str(root), "mode": manifest["mode"],
                  "requirement": entry["name"], "files": [], "errors": []}
        if prepared.get("migrated"):
            result["migrated"] = prepared["migrated"]

        excel_src = receipt.get("excel") if isinstance(receipt.get("excel"), str) and receipt.get("excel") else None
        if excel_src:
            row, err = _deliver_workbook(
                Path(excel_src).expanduser().resolve(), folder / delivery_layout.WORKBOOK_NAME,
                last_delivered_sha256=entry.get("excel", {}).get("sha256"), overwrite=overwrite,
            )
            _attach_workbook_outcome(result, entry, row, err, report_id=receipt.get("report_id"))

        _deliver_report_markdowns(folder, entry, receipt, result, now)
        delivery_layout.save_manifest(root, manifest)
        return result
