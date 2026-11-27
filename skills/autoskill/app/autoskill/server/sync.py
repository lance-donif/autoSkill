"""Sync, conflict resolution and error-pack download HTTP routes."""
from __future__ import annotations

import asyncio
import json
import re
import tempfile
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

from .. import db, execution_inputs, failure_archive, paths, share, sync_conflicts, sync_import

EXPORT_ID = re.compile(r"^[a-f0-9]{32}$")


def _export_index_file(export_id: str) -> Path:
    if not isinstance(export_id, str) or not EXPORT_ID.fullmatch(export_id):
        raise HTTPException(status_code=404, detail="同步导出标识无效")
    return paths.local() / "sync" / "exports-index" / f"{export_id}.json"


def _register_export(result: dict) -> dict:
    export_id = uuid.uuid4().hex
    receipt = {
        **result,
        "export_id": export_id,
        "download_url": f"/api/sync/exports/{export_id}",
        "created_at": db.now(),
    }
    execution_inputs.write_json(_export_index_file(export_id), receipt)
    return receipt


def _normalize_export_request(body: dict) -> tuple[list[str] | None, dict[str, list[str]] | list[str] | None, bool, bool]:
    if not isinstance(body, dict):
        raise ValueError("导出参数必须是 JSON 对象")
    project_ids = body.get("project_ids")
    project_id = body.get("project_id")
    if project_ids is not None:
        if not isinstance(project_ids, list) or any(not isinstance(pid, str) or not pid.strip() for pid in project_ids):
            raise ValueError("project_ids 必须是项目标识数组")
        chosen = [pid.strip() for pid in project_ids]
    elif project_id is not None:
        if not isinstance(project_id, str) or not project_id.strip():
            raise ValueError("project_id 必须是有效的项目标识")
        chosen = [project_id.strip()]
    else:
        chosen = None

    case_ids = body.get("case_ids")
    if case_ids is not None and not isinstance(case_ids, (list, dict)):
        raise ValueError("case_ids 必须是数组或按项目分组的对象")
    include_environments = body.get("include_environments", True)
    include_accounts = body.get("include_accounts", True)
    if type(include_environments) is not bool or type(include_accounts) is not bool:
        raise ValueError("include_environments 与 include_accounts 必须是布尔值")
    return chosen, case_ids, include_environments, include_accounts


def _resolve_export_file(export_id: str) -> Path:
    index = _export_index_file(export_id)
    if not index.is_file():
        raise HTTPException(status_code=404, detail="找不到对应的同步导出包，请重新导出")
    try:
        receipt = json.loads(index.read_bytes())
    except (OSError, ValueError) as error:
        raise HTTPException(status_code=404, detail="同步导出记录已损坏，请重新导出") from error
    file = Path(receipt.get("file") or "")
    if not file.is_file():
        raise HTTPException(status_code=404, detail="同步导出文件已不存在，请重新导出")
    return file


async def _import_multipart(request: Request) -> dict:
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        raise ValueError("请上传名为 file 的同步包文件")
    filename = Path(getattr(upload, "filename", None) or "upload.zip").name
    suffix = Path(filename).suffix.lower() or ".zip"
    folder = paths.local() / "sync" / "uploads"
    folder.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=folder, suffix=suffix, delete=False) as temporary:
        temp_path = Path(temporary.name)
    try:
        data = await upload.read()
        temp_path.write_bytes(data)
        return await asyncio.to_thread(sync_import.import_file, temp_path, source_name=filename)
    finally:
        close = getattr(upload, "close", None)
        if callable(close):
            await close()
        temp_path.unlink(missing_ok=True)


async def _import_json_request(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError as error:
        raise ValueError("请求体必须是有效的 JSON 或 multipart 表单") from error
    if not isinstance(body, dict):
        raise ValueError("导入参数必须是对象")
    source = body.get("file_path") or body.get("source")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("请提供 file_path 文件路径或上传同步包")
    return await asyncio.to_thread(sync_import.import_file, source.strip())


def _register_sync_routes(app: FastAPI) -> None:
    @app.get("/api/sync")
    def sync_status():
        return sync_import.status()

    @app.post("/api/sync/scan")
    def sync_scan():
        return sync_import.scan()

    @app.post("/api/sync/export")
    def sync_export(body: dict | None = None):
        chosen, case_ids, include_environments, include_accounts = _normalize_export_request(body or {})
        packed = share.pack_sync(
            chosen,
            case_ids=case_ids,
            include_environments=include_environments,
            include_accounts=include_accounts,
        )
        return _register_export(packed)

    @app.get("/api/sync/exports/{export_id}")
    def sync_export_download(export_id: str):
        file = _resolve_export_file(export_id)
        return FileResponse(file, filename=file.name, media_type="application/zip")

    @app.post("/api/sync/import")
    async def sync_manual_import(request: Request):
        content_type = (request.headers.get("content-type") or "").lower()
        if "multipart/form-data" in content_type:
            return await _import_multipart(request)
        return await _import_json_request(request)


def _register_conflict_and_error_routes(app: FastAPI) -> None:
    @app.get("/api/sync/conflicts")
    def sync_conflict_list(request: Request):
        return {"items": sync_conflicts.list_conflicts(status=request.query_params.get("status"))}

    @app.get("/api/sync/conflicts/{conflict_id}")
    def sync_conflict_detail(conflict_id: str):
        try:
            return sync_conflicts.get_conflict(conflict_id)
        except sync_conflicts.ConflictNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.post("/api/sync/conflicts/{conflict_id}/resolve")
    def sync_conflict_resolve(conflict_id: str, body: dict):
        try:
            return sync_conflicts.resolve(conflict_id, body)
        except sync_conflicts.ConflictNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.get("/api/sync/conflicts/{conflict_id}/export-both")
    def sync_conflict_export_both(conflict_id: str):
        try:
            file = sync_conflicts.export_both(conflict_id)
        except sync_conflicts.ConflictNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return FileResponse(file, filename=file.name, media_type="application/zip")

    @app.get("/api/runs/{run_id}/error-pack")
    def run_error_pack(run_id: str):
        try:
            file = failure_archive.build_zip(run_id)
        except failure_archive.RunNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return FileResponse(file, filename=file.name, media_type="application/zip")


def register(app: FastAPI) -> None:
    _register_sync_routes(app)
    _register_conflict_and_error_routes(app)
