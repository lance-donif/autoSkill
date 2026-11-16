"""Durable, retryable Excel, summary and issue-list delivery for completed test tasks."""
from __future__ import annotations

import contextlib
import hashlib
import json
import shutil
import uuid
from pathlib import Path
from urllib.parse import quote

from . import data_refs, db, execution_inputs, report_locations, reporting, task_bugs, task_markdown, task_report_data
from .locking import named_lock, project_lock


def _folder(report_id: str) -> Path:
    return report_locations.folder(report_id)


def _base(kind: str, task_id: str, report_id: str, project_id: str | None = None) -> dict:
    return {"report_id": report_id, "kind": kind, "task_id": task_id, **({"project_id": project_id} if project_id else {}),
            "retry_url": "/api/task-reports/" + quote(report_id, safe="") + "/retry"}


def workflow_report_id(workflow_id: str, cycle_id: str, *, connection=None, generation: int | None = None) -> str:
    """Address the current report head, or a frozen generation independent of later resumes."""
    from . import workflow_state

    if generation is None:
        if connection is None:
            with db.connect() as current:
                generation = workflow_state.report_generation(current, workflow_id, cycle_id)
        else:
            generation = workflow_state.report_generation(connection, workflow_id, cycle_id)
    if type(generation) is not int or generation < 0:
        raise ValueError("工作流报告代次必须为非负整数")
    identity = [workflow_id, cycle_id] if generation == 0 else [workflow_id, cycle_id, generation]
    return "workflow_" + execution_inputs.content_hash(identity)


def _hash(file: Path) -> str:
    with file.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _saved(report_id: str) -> dict | None:
    file = _folder(report_id) / "receipt.json"
    return json.loads(file.read_bytes()) if file.is_file() else None


def _public(receipt: dict) -> dict:
    value = {key: item for key, item in receipt.items() if key not in ("_retained_excel", "_retained_artifacts")}
    for artifact in ("excel", "markdown", "bugs"):
        if value.get(artifact):
            value[artifact] = str(data_refs.resolve(value[artifact]))
    from .runner import plan_folder

    for artifact in ("delivery_excel", "initial_excel"):
        if value.get(artifact):
            value[artifact] = str(plan_folder(value[artifact]))
    return value


def _unavailable(receipt: dict, message: str) -> dict:
    artifact_fields = ("excel", "download_url", "sha256", "size", "markdown", "markdown_download_url", "markdown_sha256", "markdown_size",
                       "bugs", "bugs_download_url", "bugs_sha256", "bugs_size")
    return {**{key: value for key, value in _public(receipt).items() if key not in artifact_fields},
            "status": "failed", "error": message, "retryable": True}


def _available(receipt: dict, artifact: str) -> bool:
    prefix = "" if artifact == "excel" else artifact + "_"
    try:
        folder = _folder(receipt["report_id"]).resolve()
        file = data_refs.resolve(receipt[artifact])
        return (file.resolve().parent == folder and file.is_file() and _hash(file) == receipt[prefix + "sha256"]
                and file.stat().st_size == receipt[prefix + "size"])
    except (OSError, KeyError, TypeError, ValueError):
        return False


def _validate(receipt: dict) -> dict:
    if receipt["status"] == "generating" and receipt.get("render_owner_identity"):
        from . import runner, runtime_identity

        owner = receipt["render_owner_identity"]
        if not (runtime_identity.owns(owner.get("pid"), owner) and runner.process_alive(owner.get("pid"))):
            return _unavailable(receipt, "报告导出进程已中断，请手动重试；原执行不会重跑。")
    if receipt["status"] != "ready":
        return _public(receipt)
    if not all(_available(receipt, artifact) for artifact in ("excel", "markdown", "bugs")):
        return _unavailable(receipt, "Excel、Markdown 或 Bug 清单文件缺失或内容已变化，请从冻结快照重试生成。")
    if receipt["kind"] == "design" and not _design_copy_available(receipt):
        return _unavailable(receipt, "正式用例 Excel 或初始副本缺失或内容已变化，请从冻结快照重试导出。")
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


def peek(report_id: str) -> dict:
    """Read delivery state without creating artifacts or retrying a failed export."""
    from . import task_state

    return task_state.report(report_id)


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


def _retained_artifacts(receipt: dict | None, digest: str | None) -> dict | None:
    if not receipt or not digest:
        return None
    candidate = receipt.get("_retained_artifacts") or receipt.get("_retained_excel") or receipt
    if candidate.get("source_hash") != digest or not _available(candidate, "excel"):
        return None
    keys = ("report_id", "source_hash", "excel", "sha256", "size", "renderer", "template_id", "validation", "cases", "execution_rows")
    retained = {key: candidate[key] for key in keys if key in candidate}
    for artifact in ("markdown", "bugs"):
        if _available(candidate, artifact):
            retained.update({key: candidate[key] for key in (artifact, artifact + "_sha256", artifact + "_size")})
    return retained


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
    if base["kind"] == "workflow" and workflow_report_id(snapshot["task_id"], snapshot.get("cycle_id"),
                                                       generation=snapshot.get("report_generation", 0)) != base["report_id"]:
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


def _markdown_artifact(snapshot: dict, folder: Path, digest: str, excel_link: str, artifact: str, existing: dict | None) -> tuple[Path, dict]:
    module = task_markdown if artifact == "markdown" else task_bugs
    filename = ("测试简报-" if artifact == "markdown" else "Bug清单-") + digest[:16] + ".md"
    file = folder / filename
    expected, summary = module.render(snapshot, excel_link=excel_link)
    if existing and _available(existing, artifact):
        file = data_refs.resolve(existing[artifact])
        file.read_text(encoding="utf-8")
    else:
        module.write_task(snapshot, file, excel_link=excel_link)
        if file.read_text(encoding="utf-8") != expected:
            raise ValueError("Markdown 内容不匹配当前冻结快照，未交付文件")
    if artifact == "markdown":
        return file, {"summary_counts": summary["counts"], "summary_cases": summary["cases"], "summary_execution_rows": summary["execution_rows"]}
    return file, {"bugs_count": summary["issues"], "bugs_counts": summary["counts"]}


def _render(base: dict, snapshot: dict, generation, existing: dict | None = None) -> dict:
    from . import runtime_identity

    base = {**base, "render_owner_identity": runtime_identity.current(), "render_started_at": db.now()}
    if base["kind"] == "workflow":
        base.update(cycle_id=snapshot["cycle_id"], report_generation=snapshot.get("report_generation", 0))
    folder = _folder(base["report_id"])
    digest = execution_inputs.content_hash(snapshot)
    snapshot_file = folder / "snapshot.json"
    if not snapshot_file.is_file() or execution_inputs.content_hash(json.loads(snapshot_file.read_bytes())) != digest:
        execution_inputs.write_json(snapshot_file, snapshot)
    file = folder / ("测试用例-" + digest[:16] + ".xlsx")
    if existing and existing.get("source_hash") == digest and _available(existing, "excel"):
        file = data_refs.resolve(existing["excel"])
        built = {key: existing[key] for key in ("renderer", "template_id", "validation", "cases", "execution_rows") if key in existing}
    else:
        built = reporting.write_task(snapshot, file)
    _read_workbook(file)
    partial = {**base, "status": "generating", "generation": generation, "source_hash": digest, "excel": data_refs.store(file),
               "sha256": _hash(file), "size": file.stat().st_size, **built}
    execution_inputs.write_json(folder / "receipt.json", partial)
    excel_link = quote(file.name, safe="")
    markdown, brief = _markdown_artifact(snapshot, folder, digest, excel_link, "markdown", existing)
    partial.update(markdown=data_refs.store(markdown), markdown_sha256=_hash(markdown), markdown_size=markdown.stat().st_size)
    execution_inputs.write_json(folder / "receipt.json", partial)
    bugs, issues = _markdown_artifact(snapshot, folder, digest, excel_link, "bugs", existing)
    validation = {**built.get("validation", {}), "workbook_readable": True, "markdown_readable": True, "bugs_readable": True}
    receipt = {**base, "status": "ready", "excel": data_refs.store(file),
               "download_url": "/api/task-reports/" + quote(base["report_id"], safe="") + "/excel",
               "markdown": data_refs.store(markdown),
               "markdown_download_url": "/api/task-reports/" + quote(base["report_id"], safe="") + "/markdown",
               "markdown_sha256": _hash(markdown), "markdown_size": markdown.stat().st_size,
               "bugs": data_refs.store(bugs), "bugs_download_url": "/api/task-reports/" + quote(base["report_id"], safe="") + "/bugs",
               "bugs_sha256": _hash(bugs), "bugs_size": bugs.stat().st_size,
               "sha256": _hash(file), "size": file.stat().st_size, "source_hash": digest,
               "generation": generation, "generated_at": db.now(), "retryable": False,
               "run_ids": snapshot.get("run_ids", []), "cases": len(snapshot["cases"]), **built, **brief, **issues, "validation": validation}
    if base["kind"] == "design":
        receipt.update(_publish_design_copy(base, file))
    execution_inputs.write_json(folder / "receipt.json", receipt)
    return receipt


def _copy_workbook(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and _hash(destination) == _hash(source):
        return
    temporary = destination.with_name(".plan-delivery-" + uuid.uuid4().hex + ".xlsx")
    try:
        shutil.copyfile(source, temporary)
        _read_workbook(temporary)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def _design_copy_available(receipt: dict) -> bool:
    from .runner import plan_folder

    try:
        if receipt.get("initial_excel") and _hash(plan_folder(receipt["initial_excel"])) != receipt["sha256"]:
            return False
        if not receipt.get("delivery_excel"):
            return True
        head = json.loads(plan_folder(receipt["delivery_head"]).read_bytes())
        return head.get("report_id") != receipt["report_id"] or _hash(plan_folder(receipt["delivery_excel"])) == receipt["sha256"]
    except (OSError, ValueError):
        return False


def _restore_design_artifact(receipt: dict) -> None:
    """A retained exact copy can restore missing report bytes without rewriting the initial design."""
    from .runner import plan_folder

    if not receipt.get("excel") or not receipt.get("sha256") or _available(receipt, "excel"):
        return
    for key in ("initial_excel", "delivery_excel"):
        if not receipt.get(key):
            continue
        file = plan_folder(receipt[key])
        if file.is_file() and _hash(file) == receipt["sha256"]:
            _copy_workbook(file, data_refs.resolve(receipt["excel"]))
            return


def _publish_design_copy(base: dict, file: Path) -> dict:
    """Update the selected sheet only; retrying an older snapshot cannot overwrite its successor."""
    from .runner import plan_folder

    if base.get("initial_excel"):
        initial = plan_folder(base["initial_excel"])
        _copy_workbook(file, initial)
    if not base.get("delivery_excel"):
        return {}
    head = plan_folder(base["delivery_head"])
    selected = json.loads(head.read_bytes()) if head.is_file() else {}
    if selected.get("report_id") != base["report_id"]:
        return {"delivery_updated": False}
    _copy_workbook(file, plan_folder(base["delivery_excel"]))
    return {"delivery_updated": True}


def for_design(snapshot: dict, destination: Path, head: Path, *, initial: Path | None = None) -> dict:
    """Freeze each complete sheet revision and publish its public workbook with retry receipts."""
    from .runner import plan_reference

    digest = execution_inputs.content_hash(snapshot)
    base = {**_base("design", snapshot["task_id"], "design_" + digest, snapshot["project_id"]),
            "delivery_excel": plan_reference(destination), "delivery_head": plan_reference(head)}
    if initial is not None:
        base["initial_excel"] = plan_reference(initial)
    execution_inputs.write_json(head, {"report_id": base["report_id"]})
    result = _ensure(base, digest, lambda: snapshot, False)
    if result["status"] == "ready":
        try:
            result.update(_publish_design_copy(base, Path(result["excel"])))
        except OSError as error:
            return _persist_failure({**base, "source_hash": digest, "_retained_artifacts": _retained_artifacts(result, digest)}, error, digest)
    return result


def _ensure(base: dict, generation, capture, retry: bool) -> dict:
    try:
        with project_lock(base["project_id"]) if base.get("project_id") and base["kind"] != "workflow" else contextlib.nullcontext(), named_lock("test-report", base["report_id"]):
            if base.get("project_id") and base["kind"] != "workflow":
                report_locations.bind(base["report_id"], base["project_id"])
            failure_base = base
            try:
                saved = _saved(base["report_id"])
                if saved and saved.get("status") == "failed" and not retry:
                    return _validate(saved)
                same_generation = saved and saved.get("generation") == generation
                if same_generation:
                    failure_base = {**base, **({"source_hash": saved["source_hash"]} if saved.get("source_hash") else {}),
                                    "snapshot_required": bool(saved.get("snapshot_required") or saved.get("status") == "ready"
                                                              or (_folder(base["report_id"]) / "snapshot.json").is_file())}
                    retained = _retained_artifacts(saved, saved.get("source_hash"))
                    if retained:
                        failure_base["_retained_artifacts"] = retained
                if same_generation and not retry and (saved.get("status") != "ready" or (saved.get("markdown") and saved.get("bugs"))):
                    return _validate(saved)
                snapshot = _snapshot(base, saved) if same_generation else None
                if snapshot is None:
                    snapshot = capture()
                failure_base = {**base, "source_hash": execution_inputs.content_hash(snapshot)}
                retained = _retained_artifacts(saved, failure_base["source_hash"]) if same_generation else None
                if retained:
                    failure_base["_retained_artifacts"] = retained
                return _validate(_render(base, snapshot, generation, retained))
            except Exception as error:  # noqa: BLE001  # Keep failure publication in the same lock as successful delivery.
                retained = _retained_artifacts(_saved(base["report_id"]), failure_base.get("source_hash"))
                if retained:
                    failure_base["_retained_artifacts"] = retained
                return _persist_failure(failure_base, error, generation)
    except Exception as error:  # noqa: BLE001  # Report delivery must not erase a completed execution or saved recording.
        return _persist_failure(base, error, generation, save=False)


def for_run(run_id: str, *, retry: bool = False) -> dict:
    """Serialize export with execution/deletion and never recreate a deleted run directory."""
    from . import deletion

    base = _base("run", run_id, "run_" + run_id)
    _folder(base["report_id"])
    initial = db.one("SELECT status, delete_requested, project_id FROM runs WHERE id = ?", (run_id,))
    if not initial or initial["delete_requested"]:
        return {**base, "status": "skipped", "reason": "运行已删除或正在删除，不再生成报告", "retryable": False}
    if initial["status"] not in db.FINISHED:
        return {**base, "status": "pending", "reason": "运行尚未结束，完成后生成 Excel、简报和 Bug 清单", "retryable": False}
    base = _base("run", run_id, "run_" + run_id, initial["project_id"])
    with deletion.execution_guard(run_id):
        run = db.one("SELECT * FROM runs WHERE id = ?", (run_id,))
        if not run or run.get("delete_requested"):
            return {**base, "status": "skipped", "reason": "运行已删除或正在删除，不再生成报告", "retryable": False}
        if run["status"] not in db.FINISHED:
            return {**base, "status": "pending", "reason": "运行尚未结束，完成后生成 Excel、简报和 Bug 清单", "retryable": False}
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


def prepare_workflow(workflow_id: str, *, cancelled_boundary: bool = False) -> dict:
    """Freeze a completed cycle before returning to a caller or starting a new cycle."""
    from . import workflow_state

    flow = workflow_state.workflow(workflow_id)
    with project_lock(flow["project_id"]):
        flow = workflow_state.workflow(workflow_id)
        with db.connect() as connection:
            report_generation = workflow_state.report_generation(connection, workflow_id, flow["cycle_id"])
        base = {**_base("workflow", workflow_id, workflow_report_id(workflow_id, flow["cycle_id"], generation=report_generation), flow["project_id"]),
                "cycle_id": flow["cycle_id"], "report_generation": report_generation}
        boundary = cancelled_boundary and flow["lifecycle"] == "cancelled"
        if not boundary and not _workflow_ready(flow):
            return {**base, "status": "pending", "reason": "本周期尚未结束，保留各次运行报告，周期完成后汇总", "retryable": False}
        with named_lock("test-report", base["report_id"]):
            report_locations.bind(base["report_id"], flow["project_id"])
            saved = _saved(base["report_id"])
            if saved:
                return _validate(saved)
            try:
                snapshot = task_report_data.workflow_snapshot(flow, report_generation=report_generation, cancelled_boundary=boundary)
                digest = execution_inputs.content_hash(snapshot)
                execution_inputs.write_json(_folder(base["report_id"]) / "snapshot.json", snapshot)
                receipt = {**base, "status": "generating", "generation": flow["revision"], "source_hash": digest,
                           "snapshot_required": True, "updated_at": db.now(), "retryable": False}
                execution_inputs.write_json(_folder(base["report_id"]) / "receipt.json", receipt)
                return receipt
            except Exception as error:  # noqa: BLE001  # Capture failures are durable and require an explicit user retry.
                return _persist_failure(base, error, flow["revision"])


def finish_workflow_report(report_id: str) -> dict:
    """Render a prepared cycle once, outside the project lock; failures never auto-retry."""
    with named_lock("test-report", report_id):
        saved = _saved(report_id)
        if not saved:
            return peek(report_id)
        if saved.get("status") != "generating":
            return _validate(saved)
        if saved.get("render_owner_identity"):
            return fail_interrupted_export(report_id)
        from . import runtime_identity

        base = {**_base("workflow", saved["task_id"], report_id, saved.get("project_id")),
                "render_owner_identity": runtime_identity.current(), "render_started_at": db.now()}
        try:
            execution_inputs.write_json(_folder(report_id) / "receipt.json", {**saved, **base})
            snapshot = _snapshot(base, saved)
            if snapshot is None:
                raise ValueError("工作流报告冻结快照缺失，不能用当前周期替代")
            return _validate(_render(base, snapshot, saved["generation"], _retained_artifacts(saved, saved.get("source_hash"))))
        except Exception as error:  # noqa: BLE001  # Preserve failed first generation for manual retry.
            return _persist_failure({**base, "source_hash": saved.get("source_hash"), "snapshot_required": True}, error, saved["generation"])


def fail_interrupted_export(report_id: str) -> dict:
    """An abandoned rendering attempt requires manual retry, including after worker restart."""
    from . import runner, runtime_identity

    def abandoned(receipt):
        owner = (receipt or {}).get("render_owner_identity")
        return bool(receipt and receipt.get("status") == "generating" and owner
                    and not (runtime_identity.owns(owner.get("pid"), owner) and runner.process_alive(owner.get("pid"))))

    saved = _saved(report_id)
    if not abandoned(saved):
        return _validate(saved) if saved else peek(report_id)
    with named_lock("test-report", report_id):
        saved = _saved(report_id)
        if not abandoned(saved):
            return _validate(saved) if saved else peek(report_id)
        base = {**_base("workflow", saved["task_id"], report_id, saved.get("project_id")), "source_hash": saved.get("source_hash"),
                "snapshot_required": True, "render_owner_identity": saved["render_owner_identity"], "render_started_at": saved.get("render_started_at")}
        return _persist_failure(base, RuntimeError("报告导出进程已中断，请手动重试；原执行不会重跑"), saved["generation"])


def for_workflow(workflow_id: str, *, retry: bool = False) -> dict:
    receipt = prepare_workflow(workflow_id)
    if receipt["status"] == "generating":
        return finish_workflow_report(receipt["report_id"])
    if retry and receipt["status"] not in ("pending", "skipped"):
        saved = _saved(receipt["report_id"])
        base = _base("workflow", workflow_id, receipt["report_id"], saved.get("project_id"))
        return _ensure(base, saved["generation"], lambda: _snapshot(base, saved), True)
    return receipt


def for_recording(project_id: str, recording: dict, cases: list[dict]) -> dict:
    """Freeze the just-saved definitions, not another task's latest replay results."""
    identity = task_report_data.recording_identity(project_id, recording, cases)
    base = _base("recording", identity, "recording_" + identity, project_id)
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
    if saved["kind"] == "design":
        _restore_design_artifact(saved)
        base = {**_base("design", saved["task_id"], report_id, saved.get("project_id")),
                **{key: saved[key] for key in ("delivery_excel", "delivery_head", "initial_excel") if key in saved}}
        return _ensure(base, saved["generation"], lambda: _snapshot(base, saved), True)
    if saved["kind"] == "workflow":
        from . import workflow_state

        flow = workflow_state.workflow(saved["task_id"])
        if (_folder(report_id) / "snapshot.json").is_file():
            base = _base(saved["kind"], saved["task_id"], report_id, saved.get("project_id"))
            return _ensure(base, saved["generation"], lambda: _snapshot(base, saved), True)
        expected = workflow_report_id(flow["id"], flow["cycle_id"])
        if expected != report_id:
            raise ValueError("工作流已开始新报告代次或新周期；请读取当前报告，不能用当前数据补写已缺失的历史快照")
        return for_workflow(saved["task_id"], retry=True)
    snapshot_file = _folder(report_id) / "snapshot.json"
    base = _base(saved["kind"], saved["task_id"], report_id, saved.get("project_id"))
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
    if format not in ("excel", "markdown", "bugs"):
        raise ValueError("报告格式需要是 excel、markdown 或 bugs")
    receipt = get(report_id)
    if receipt["status"] != "ready":
        raise ValueError(receipt.get("error") or "测试报告尚未生成，请先重试")
    return Path(receipt[format])
