"""Data management routes operate on complete roots and do not replace the running program."""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from .. import data_store


def register(app: FastAPI) -> None:
    @app.exception_handler(data_store.DataError)
    async def data_error(request: Request, error: data_store.DataError):  # noqa: ARG001  # FastAPI supplies the request to exception handlers.
        status = 409 if error.code in ("data_changed", "data_busy", "target_not_empty") else 400
        return JSONResponse({"detail": str(error), "error": str(error), "code": error.code, "details": error.details}, status_code=status)

    @app.get("/api/data/status")
    def status():
        return data_store.status()

    @app.post("/api/data/import")
    def import_data(body: dict):
        if set(body) - {"source", "target"} or not isinstance(body.get("source"), str) or not body["source"].strip():
            raise ValueError("请提供旧数据目录或 ZIP 的 source 路径")
        if "target" in body and (not isinstance(body["target"], str) or not body["target"].strip()):
            raise ValueError("target 必须是新的空数据目录路径")
        return data_store.import_data(body["source"], target=body.get("target"))

    @app.post("/api/data/export")
    def export_data(body: dict):
        if set(body) - {"destination"}:
            raise ValueError("数据导出只接受 destination 输出路径")
        if "destination" in body and (not isinstance(body["destination"], str) or not body["destination"].strip()):
            raise ValueError("destination 必须是 ZIP 输出路径")
        return data_store.export_data(body.get("destination"))

    @app.get("/api/data/exports/{export_id}")
    def download(export_id: str):
        file = data_store.download_file(export_id)
        return FileResponse(file, filename=file.name, media_type="application/zip")
