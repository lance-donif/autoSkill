"""Download bound native evidence and rerun original asset versions from the dashboard."""
from __future__ import annotations

import hashlib
from pathlib import PurePosixPath

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from .. import native_run
from . import jobs


def _native_file(run_id: str, name: str):
    result = native_run.get(run_id)
    folder = native_run._evidence_folder(result)
    relative = PurePosixPath(name.replace("\\", "/"))
    if relative.is_absolute() or ".." in relative.parts or "input" in relative.parts or "process-spec.json" in relative.parts:
        raise HTTPException(404, "证据文件不存在")
    target = (folder / relative.as_posix()).resolve()
    if not target.is_relative_to(folder) or not target.is_file():
        raise HTTPException(404, "证据文件不存在")
    if relative.as_posix() != "outcome.json":
        reference = next((ref for ref in result["outcome"].get("evidence", []) if isinstance(ref, dict) and ref.get("path") == relative.as_posix()), None)
        if not reference:
            raise HTTPException(404, "文件未包含在本次运行证据中")
        with target.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if reference.get("sha256") != digest or reference.get("size") != target.stat().st_size:
            raise HTTPException(409, "证据文件已变更，不能作为原始运行报告下载")
    return target


def register(app: FastAPI) -> None:
    @app.get("/api/native-runs/{run_id}/files/{name:path}")
    def native_file(run_id: str, name: str):
        file = _native_file(run_id, name)
        return FileResponse(file, headers={"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox allow-scripts allow-downloads"})

    @app.post("/api/native-runs/{run_id}/rerun")
    def rerun(run_id: str, body: dict):
        if body:
            raise ValueError("原生重跑沿用原资产版本和参数；调整参数请从资产页创建新运行")
        original = native_run.get(run_id)
        manifest = native_run._frozen(original)
        context = manifest.get("runtime_context", {})
        created = native_run.enqueue(original["project_id"], manifest["asset_id"], head=manifest["head"], parameters=manifest["parameters"],
                                     session_id=original.get("session_id"), host=context.get("host", "generic"), host_session_id=context.get("host_session_id", ""))
        return {**created, "queue_dispatcher": jobs.ensure_worker()}
