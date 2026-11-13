"""Download native evidence, rerun frozen assets and execute environment diagnostics asynchronously."""
from __future__ import annotations

import copy
import hashlib
import threading
import uuid
from pathlib import PurePosixPath

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from .. import cli, db, engines, native_run
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


def _diagnose(body: dict) -> dict:
    if body["kind"] == "doctor":
        return cli.doctor(body.get("launch", False))
    if body["kind"] == "verify":
        return cli.verify()
    return {"engines": engines.probe(body.get("engine"))}


def register(app: FastAPI) -> None:
    diagnostics: dict[str, dict] = {}
    diagnostic_lock = threading.Lock()

    def diagnostic(identity: str) -> dict:
        with diagnostic_lock:
            value = diagnostics.get(identity)
            if value is None:
                raise HTTPException(404, "诊断任务不存在，服务重启后请重新检查")
            return copy.deepcopy(value)

    def execute(identity: str, body: dict) -> None:
        try:
            result = {"status": "Completed", "result": _diagnose(body), "error": None}
        except Exception as error:  # noqa: BLE001  # Diagnostics must retain a readable result even when a third-party tool fails.
            result = {"status": "Failed", "result": None, "error": str(error)}
        with diagnostic_lock:
            diagnostics[identity].update(result, finished_at=db.now())

    @app.post("/api/diagnostics")
    def start(body: dict):
        if set(body) - {"kind", "launch", "engine"} or body.get("kind") not in ("doctor", "verify", "engines"):
            raise ValueError("诊断 kind 需要是 doctor、verify 或 engines")
        if "launch" in body and type(body["launch"]) is not bool:
            raise ValueError("launch 必须为布尔值")
        if "engine" in body and not isinstance(body["engine"], str):
            raise ValueError("engine 必须是引擎标识")
        identity = "diagnostic-" + uuid.uuid4().hex
        value = {"id": identity, "kind": body["kind"], "status": "Running", "created_at": db.now(), "finished_at": None, "result": None, "error": None}
        with diagnostic_lock:
            diagnostics[identity] = value
        threading.Thread(target=execute, args=(identity, copy.deepcopy(body)), daemon=True, name=identity).start()
        return diagnostic(identity)

    @app.get("/api/diagnostics/{diagnostic_id}")
    def status(diagnostic_id: str):
        return diagnostic(diagnostic_id)

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
