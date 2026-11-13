"""Durable, retryable spreadsheet delivery for completed test tasks."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import quote

from . import db, execution_inputs, paths, reporting, task_report_data
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


def _unavailable(receipt: dict, message: str) -> dict:
    return {**{key: value for key, value in receipt.items() if key not in ("excel", "download_url", "sha256", "size")},
            "status": "failed", "error": message, "retryable": True}


def _validate(receipt: dict) -> dict:
    if receipt["status"] != "ready":
        return receipt
    folder = _folder(receipt["report_id"])
    file = Path(receipt["excel"])
    try:
        available = file.resolve().parent == folder.resolve() and file.is_file() and _hash(file) == receipt["sha256"]
    except OSError:
        available = False
    if not available:
        return _unavailable(receipt, "报告文件缺失或内容已变化，请重试生成。")
    if receipt["kind"] == "workflow":
        for run_id in receipt.get("run_ids", []):
            run = db.one("SELECT delete_requested FROM runs WHERE id = ?", (run_id,))
            if not run or run["delete_requested"]:
                return _unavailable(receipt, "关联运行的执行证据已删除，请重新生成报告以反映当前证据状态。")
    return receipt


def get(report_id: str) -> dict:
    """Read a receipt without claiming success for a missing or changed workbook."""
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
    receipt = {**base, "status": "failed", "error": "测试用例 Excel 生成失败：" + str(error), "retryable": True,
               "generation": generation, "updated_at": db.now()}
    if not save:
        return {**receipt, "receipt_saved": False}
    try:
        execution_inputs.write_json(_folder(base["report_id"]) / "receipt.json", receipt)
    except OSError:
        receipt["receipt_saved"] = False
    return receipt


def _render(base: dict, snapshot: dict, generation) -> dict:
    folder = _folder(base["report_id"])
    execution_inputs.write_json(folder / "snapshot.json", snapshot)
    digest = execution_inputs.content_hash(snapshot)
    file = folder / ("测试用例-" + digest[:16] + ".xlsx")
    built = reporting.write_task(snapshot, file)
    receipt = {**base, "status": "ready", "excel": str(file.resolve()),
               "download_url": "/api/task-reports/" + quote(base["report_id"], safe="") + "/excel",
               "sha256": _hash(file), "size": file.stat().st_size, "source_hash": digest,
               "generation": generation, "generated_at": db.now(), "retryable": False,
               "run_ids": snapshot.get("run_ids", []), "cases": len(snapshot["cases"]), **built}
    execution_inputs.write_json(folder / "receipt.json", receipt)
    return receipt


def _ensure(base: dict, generation, capture, retry: bool) -> dict:
    try:
        with named_lock("test-report", base["report_id"]):
            try:
                saved = _saved(base["report_id"])
                if saved and saved.get("generation") == generation and not retry:
                    return _validate(saved)
                return _render(base, capture(), generation)
            except Exception as error:  # noqa: BLE001  # Keep failure publication in the same lock as successful delivery.
                return _persist_failure(base, error, generation)
    except Exception as error:  # noqa: BLE001  # Spreadsheet delivery must not erase a completed execution or saved recording.
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
        return {**base, "status": "pending", "reason": "运行尚未结束，完成后生成测试用例 Excel", "retryable": False}
    with deletion.execution_guard(run_id):
        run = db.one("SELECT * FROM runs WHERE id = ?", (run_id,))
        if not run or run.get("delete_requested"):
            return {**base, "status": "skipped", "reason": "运行已删除或正在删除，不再生成报告", "retryable": False}
        if run["status"] not in db.FINISHED:
            return {**base, "status": "pending", "reason": "运行尚未结束，完成后生成测试用例 Excel", "retryable": False}
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


def download(report_id: str) -> Path:
    receipt = get(report_id)
    if receipt["status"] != "ready":
        raise ValueError(receipt.get("error") or "测试报告尚未生成，请先重试")
    return Path(receipt["excel"])
