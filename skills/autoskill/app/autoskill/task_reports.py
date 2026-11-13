"""Durable, retryable Excel and short Markdown delivery for completed test tasks."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import quote

from . import db, execution_inputs, paths, reporting, task_markdown, task_report_data
from .locking import named_lock, project_lock


def _folder(report_id: str) -> Path:
    if re.fullmatch(r"run_[A-Za-z0-9][A-Za-z0-9_-]*", report_id):
        folder = paths.local() / "runs" / report_id.removeprefix("run_") / "test-report"
    elif re.fullmatch(r"(?:workflow|recording)_[a-f0-9]{64}", report_id):
        folder = paths.local() / "task-reports" / report_id
    else:
        raise ValueError("测试报告标识无效")
    if not folder.resolve().is_relative_to(paths.local().resolve()):
        raise ValueError("测试报告路径不属于当前工作区")
    return folder


def _base(kind: str, task_id: str, report_id: str) -> dict:
    return {"report_id": report_id, "kind": kind, "task_id": task_id,
            "retry_url": "/api/task-reports/" + quote(report_id, safe="") + "/retry"}


def _hash(file: Path) -> str:
    with file.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _saved(report_id: str) -> dict | None:
    file = _folder(report_id) / "receipt.json"
    return json.loads(file.read_bytes()) if file.is_file() else None


def _public(receipt: dict) -> dict:
    return {key: value for key, value in receipt.items() if key != "_retained_excel"}


def _unavailable(receipt: dict, message: str) -> dict:
    artifact_fields = ("excel", "download_url", "sha256", "size", "markdown", "markdown_download_url", "markdown_sha256", "markdown_size")
    return {**{key: value for key, value in _public(receipt).items() if key not in artifact_fields},
            "status": "failed", "error": message, "retryable": True}


def _available(receipt: dict, artifact: str) -> bool:
    prefix = "" if artifact == "excel" else "markdown_"
    try:
        folder = _folder(receipt["report_id"]).resolve()
        file = Path(receipt[artifact])
        return (file.resolve().parent == folder and file.is_file() and _hash(file) == receipt[prefix + "sha256"]
                and file.stat().st_size == receipt[prefix + "size"])
    except (OSError, KeyError, TypeError, ValueError):
        return False


def _validate(receipt: dict) -> dict:
    if receipt["status"] != "ready":
        return _public(receipt)
    if not all(_available(receipt, artifact) for artifact in ("excel", "markdown")):
        return _unavailable(receipt, "Excel 或 Markdown 文件缺失或内容已变化，请从冻结快照重试生成。")
    if receipt["kind"] == "workflow":
        for run_id in receipt.get("run_ids", []):
            run = db.one("SELECT delete_requested FROM runs WHERE id = ?", (run_id,))
            if not run or run["delete_requested"]:
                return _unavailable(receipt, "关联运行的执行证据已删除，请重新生成报告以反映当前证据状态。")
    return _public(receipt)


def get(report_id: str) -> dict:
    """Read a receipt without claiming success for either missing or changed artifact."""
    _folder(report_id)
    if report_id.startswith("run_"):
        run = db.one("SELECT delete_requested FROM runs WHERE id = ?", (report_id.removeprefix("run_"),))
        if not run or run["delete_requested"]:
            raise ValueError("运行已删除或正在删除，测试报告不再提供下载")
    saved = _saved(report_id)
    if not saved:
        raise ValueError("测试报告不存在；任务尚未生成报告或记录已删除")
    return _validate(saved)


def _persist_failure(base: dict, error: Exception, generation, *, save: bool = True) -> dict:
    receipt = {**base, "status": "failed", "error": "测试报告生成失败：" + str(error), "retryable": True,
               "generation": generation, "updated_at": db.now()}
    if not save:
        return {**_public(receipt), "receipt_saved": False}
    try:
        execution_inputs.write_json(_folder(base["report_id"]) / "receipt.json", receipt)
    except OSError:
        receipt["receipt_saved"] = False
    return _public(receipt)


def _retained_excel(receipt: dict | None, digest: str | None) -> dict | None:
    if not receipt or not digest:
        return None
    candidate = receipt.get("_retained_excel") or receipt
    if candidate.get("source_hash") != digest or not _available(candidate, "excel"):
        return None
    keys = ("report_id", "source_hash", "excel", "sha256", "size", "renderer", "template_id", "validation", "cases", "execution_rows")
    return {key: candidate[key] for key in keys if key in candidate}


def _snapshot(base: dict, saved: dict) -> dict | None:
    file = _folder(base["report_id"]) / "snapshot.json"
    if not file.is_file():
        if saved.get("source_hash") or saved.get("status") == "ready" or saved.get("snapshot_required"):
            raise ValueError("历史报告冻结快照缺失，不能读取当前数据替代当时测试")
        return None
    snapshot = json.loads(file.read_bytes())
    if snapshot.get("kind") != base["kind"] or snapshot.get("task_id") != base["task_id"]:
        raise ValueError("报告冻结快照不属于当前任务，不能混用其他任务的数据")
    if saved.get("source_hash") and execution_inputs.content_hash(snapshot) != saved["source_hash"]:
        raise ValueError("报告冻结快照内容校验失败，不能改用当前数据补写历史")
    if base["kind"] == "workflow" and "workflow_" + execution_inputs.content_hash([snapshot["task_id"], snapshot.get("cycle_id")]) != base["report_id"]:
        raise ValueError("报告冻结快照不属于当前工作流周期")
    return snapshot


def _read_workbook(file: Path) -> None:
    from openpyxl import load_workbook

    with file.open("rb") as stream:
        book = load_workbook(stream, read_only=True, data_only=False)
        try:
            if not book.worksheets:
                raise ValueError("Excel 缺少工作表，未交付文件")
            for sheet in book:
                next(sheet.iter_rows(values_only=True), None)
        finally:
            book.close()


def _render(base: dict, snapshot: dict, generation, existing: dict | None = None) -> dict:
    folder = _folder(base["report_id"])
    execution_inputs.write_json(folder / "snapshot.json", snapshot)
    digest = execution_inputs.content_hash(snapshot)
    file = folder / ("测试用例-" + digest[:16] + ".xlsx")
    if existing and existing.get("source_hash") == digest and _available(existing, "excel"):
        file = Path(existing["excel"])
        built = {key: existing[key] for key in ("renderer", "template_id", "validation", "cases", "execution_rows") if key in existing}
    else:
        built = reporting.write_task(snapshot, file)
    _read_workbook(file)
    markdown = folder / ("测试简报-" + digest[:16] + ".md")
    excel_link = quote(file.name, safe="")
    brief = task_markdown.write_task(snapshot, markdown, excel_link=excel_link)
    expected_text, _summary = task_markdown.render(snapshot, excel_link=excel_link)
    if markdown.read_text(encoding="utf-8") != expected_text:
        raise ValueError("Markdown 内容不匹配当前冻结快照，未交付文件")
    validation = {**built.get("validation", {}), "workbook_readable": True, **brief.pop("markdown_validation")}
    receipt = {**base, "status": "ready", "excel": str(file.resolve()),
               "download_url": "/api/task-reports/" + quote(base["report_id"], safe="") + "/excel",
               "markdown": str(markdown.resolve()),
               "markdown_download_url": "/api/task-reports/" + quote(base["report_id"], safe="") + "/markdown",
               "markdown_sha256": _hash(markdown), "markdown_size": markdown.stat().st_size,
               "sha256": _hash(file), "size": file.stat().st_size, "source_hash": digest,
               "generation": generation, "generated_at": db.now(), "retryable": False,
               "run_ids": snapshot.get("run_ids", []), "cases": len(snapshot["cases"]), **built, **brief, "validation": validation}
    execution_inputs.write_json(folder / "receipt.json", receipt)
    return receipt


def _ensure(base: dict, generation, capture, retry: bool) -> dict:
    try:
        with named_lock("test-report", base["report_id"]):
            failure_base = base
            try:
                saved = _saved(base["report_id"])
                same_generation = saved and saved.get("generation") == generation
                if same_generation:
                    failure_base = {**base, **({"source_hash": saved["source_hash"]} if saved.get("source_hash") else {}),
                                    "snapshot_required": bool(saved.get("snapshot_required") or saved.get("status") == "ready"
                                                              or (_folder(base["report_id"]) / "snapshot.json").is_file())}
                    retained = _retained_excel(saved, saved.get("source_hash"))
                    if retained:
                        failure_base["_retained_excel"] = retained
                if same_generation and not retry and (saved.get("status") != "ready" or saved.get("markdown")):
                    return _validate(saved)
                snapshot = _snapshot(base, saved) if same_generation else None
                if snapshot is None:
                    snapshot = capture()
                failure_base = {**base, "source_hash": execution_inputs.content_hash(snapshot)}
                retained = _retained_excel(saved, failure_base["source_hash"]) if same_generation else None
                if retained:
                    failure_base["_retained_excel"] = retained
                return _validate(_render(base, snapshot, generation, retained))
            except Exception as error:  # noqa: BLE001  # Keep failure publication in the same lock as successful delivery.
                return _persist_failure(failure_base, error, generation)
    except Exception as error:  # noqa: BLE001  # Report delivery must not erase a completed execution or saved recording.
        return _persist_failure(base, error, generation, save=False)


def for_run(run_id: str, *, retry: bool = False) -> dict:
    """Serialize export with execution/deletion and never recreate a deleted run directory."""
    from . import deletion

    base = _base("run", run_id, "run_" + run_id)
    _folder(base["report_id"])
    initial = db.one("SELECT status, delete_requested FROM runs WHERE id = ?", (run_id,))
    if not initial or initial["delete_requested"]:
        return {**base, "status": "skipped", "reason": "运行已删除或正在删除，不再生成报告", "retryable": False}
    if initial["status"] not in db.FINISHED:
        return {**base, "status": "pending", "reason": "运行尚未结束，完成后生成 Excel 和 Markdown", "retryable": False}
    with deletion.execution_guard(run_id):
        run = db.one("SELECT * FROM runs WHERE id = ?", (run_id,))
        if not run or run.get("delete_requested"):
            return {**base, "status": "skipped", "reason": "运行已删除或正在删除，不再生成报告", "retryable": False}
        if run["status"] not in db.FINISHED:
            return {**base, "status": "pending", "reason": "运行尚未结束，完成后生成 Excel 和 Markdown", "retryable": False}
        generation = {key: run.get(key) for key in ("input_hash", "finished_at", "status")}
        result = _ensure(base, generation, lambda: task_report_data.run_snapshot(run), retry)
        current = db.one("SELECT delete_requested FROM runs WHERE id = ?", (run_id,))
        if not current or current["delete_requested"]:
            return {**base, "status": "skipped", "reason": "运行正在删除，不再交付报告", "retryable": False}
        return result


def _workflow_ready(flow: dict) -> bool:
    tasks = db.rows("SELECT state, run_id FROM workflow_items WHERE workflow_id = ? AND cycle_id = ?", (flow["id"], flow["cycle_id"]))
    if flow["lifecycle"] != "cancelled" and any(item["state"] in ("ready", "leased", "publishing", "waiting_execution", "recovering") for item in tasks):
        return False
    return all(not item["run_id"] or not db.one("SELECT id FROM runs WHERE id = ? AND status NOT IN ('Pass','Fail','Blocked','Cancelled')", (item["run_id"],)) for item in tasks)


def for_workflow(workflow_id: str, *, retry: bool = False) -> dict:
    from . import workflow_state

    flow = workflow_state.workflow(workflow_id)
    with project_lock(flow["project_id"]):
        flow = workflow_state.workflow(workflow_id)
        identity = execution_inputs.content_hash([workflow_id, flow["cycle_id"]])
        base = _base("workflow", workflow_id, "workflow_" + identity)
        if not _workflow_ready(flow):
            return {**base, "status": "pending", "reason": "本周期尚未结束，保留各次运行报告，周期完成后汇总", "retryable": False}
        return _ensure(base, flow["revision"], lambda: task_report_data.workflow_snapshot(flow), retry)


def for_recording(project_id: str, recording: dict, cases: list[dict]) -> dict:
    """Freeze the just-saved definitions, not another task's latest replay results."""
    identity = task_report_data.recording_identity(project_id, recording, cases)
    base = _base("recording", identity, "recording_" + identity)
    def capture():
        request = {"project_id": project_id, "recording": {key: recording.get(key) for key in
                   ("started_at", "session_id", "rand", "start_url", "workflow_binding", "operations")}, "cases": cases}
        execution_inputs.write_json(_folder(base["report_id"]) / "request.json", request)
        return task_report_data.recording_snapshot(project_id, recording, cases)
    return _ensure(base, identity, capture, False)


def retry(report_id: str) -> dict:
    saved = _saved(report_id)
    if not saved:
        if report_id.startswith("run_"):
            return for_run(report_id.removeprefix("run_"), retry=True)
        raise ValueError("找不到报告重试快照，无法用其他任务的数据补写")
    if saved["kind"] == "run":
        return for_run(saved["task_id"], retry=True)
    if saved["kind"] == "workflow":
        from . import workflow_state

        flow = workflow_state.workflow(saved["task_id"])
        if (_folder(report_id) / "snapshot.json").is_file():
            base = _base(saved["kind"], saved["task_id"], report_id)
            with project_lock(flow["project_id"]):
                return _ensure(base, saved["generation"], lambda: _snapshot(base, saved), True)
        expected = "workflow_" + execution_inputs.content_hash([flow["id"], flow["cycle_id"]])
        if expected != report_id:
            raise ValueError("工作流已开始新周期；请读取当前周期报告，不能覆盖旧周期的快照")
        return for_workflow(saved["task_id"], retry=True)
    snapshot_file = _folder(report_id) / "snapshot.json"
    base = _base(saved["kind"], saved["task_id"], report_id)
    def capture():
        if snapshot_file.is_file():
            return json.loads(snapshot_file.read_bytes())
        request_file = _folder(report_id) / "request.json"
        if not request_file.is_file():
            raise ValueError("录制报告快照缺失，不能读取当前用例冒充当时定义")
        request = json.loads(request_file.read_bytes())
        return task_report_data.recording_snapshot(request["project_id"], request["recording"], request["cases"])
    return _ensure(base, saved["generation"], capture, True)


def download(report_id: str, format: str = "excel") -> Path:  # noqa: A002  # Keep the public format keyword used by HTTP callers.
    if format not in ("excel", "markdown"):
        raise ValueError("报告格式需要是 excel 或 markdown")
    receipt = get(report_id)
    if receipt["status"] != "ready":
        raise ValueError(receipt.get("error") or "测试报告尚未生成，请先重试")
    return Path(receipt[format])
