"""Project materials, editable knowledge and downloadable project-share endpoints."""
from __future__ import annotations

import asyncio
import base64
import tempfile
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse

from .. import context_service, paths, project_materials, projects, share


def _selected(request: Request) -> list[dict]:
    selected = [value for value in request.query_params.get("projects", "").split(",") if value]
    return [project for project in projects.list_all() if not project.get("broken") and (not selected or project["id"] in selected)]


def _import(spec: dict) -> dict:
    source = spec.get("source")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("请提供运行 autoSkill 的本机 ZIP 路径，或选择文件上传")
    overwrite = spec.get("overwrite", False)
    passphrase = spec.get("passphrase")
    if type(overwrite) is not bool or (passphrase is not None and not isinstance(passphrase, str)):
        raise ValueError("导入参数无效")
    return share.import_pack(str(Path(source).expanduser()), passphrase=passphrase or None, overwrite=overwrite)


def register(app: FastAPI) -> None:
    @app.get("/api/project-materials")
    def index(request: Request):
        return {"projects": _selected(request), "data_root": str(paths.workspace()),
                "layout": "projects/<项目>/knowledge、testcases、reports"}

    @app.get("/api/project-materials/exports/{export_id}")
    def export_download(export_id: str):
        file = project_materials.export_file(export_id)
        return FileResponse(file, filename=file.name, media_type="application/zip")

    @app.get("/api/project-materials/{project_id}")
    def materials(project_id: str):
        return project_materials.inventory(project_id)

    @app.get("/api/project-materials/{project_id}/files/{file_id}")
    def download(project_id: str, file_id: str):
        file = project_materials.file_for(project_id, file_id)
        return FileResponse(file, filename=file.name)

    @app.post("/api/project-materials/{project_id}/export")
    def export_project(project_id: str):
        return project_materials.export_project(project_id)

    @app.get("/api/project-materials/{project_id}/knowledge")
    def read(project_id: str, request: Request):
        query = request.query_params
        file = query.get("file", "")
        project_materials._knowledge_path(project_id, file)
        return context_service.read_knowledge(project_id, file, offset=int(query.get("offset", 0)), length=int(query.get("length", 12000)),
                                              content_revision=query.get("revision"), all_items=query.get("all") == "1")

    @app.put("/api/project-materials/{project_id}/knowledge")
    def write(project_id: str, body: dict):
        return project_materials.write_knowledge(project_id, body.get("file"), body.get("content"), body.get("expected_hash"))

    @app.delete("/api/project-materials/{project_id}/knowledge")
    def remove(project_id: str, body: dict):
        return project_materials.write_knowledge(project_id, body.get("file"), "", body.get("expected_hash"), delete=True)

    @app.post("/api/shares/export")
    def export_share(body: dict):
        return project_materials.export_share(body)

    @app.post("/api/shares/import")
    def import_share(body: dict):
        return _import(body)

    @app.post("/api/shares/upload")
    async def upload(request: Request):
        value = request.headers.get("x-autoskill-passphrase", "")
        try:
            passphrase = base64.b64decode(value, validate=True).decode("utf-8") if value else None
        except (ValueError, UnicodeError) as error:
            raise ValueError("分享包口令编码无效") from error
        folder = paths.local() / "share-imports"
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=folder, suffix=".zip", delete=False) as temporary:
            file = Path(temporary.name)
        try:
            with file.open("wb") as stream:
                async for chunk in request.stream():
                    stream.write(chunk)
            return await asyncio.to_thread(share.import_pack, str(file), passphrase=passphrase,
                                           overwrite=request.headers.get("x-autoskill-overwrite") == "1")
        finally:
            file.unlink(missing_ok=True)
