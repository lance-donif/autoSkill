"""Folder routes expose project organization with explicit concurrent modification checks."""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from .. import case_folders, reporting
from ..locking import project_lock


def register(app: FastAPI) -> None:
    @app.exception_handler(case_folders.FolderConflictError)
    async def conflict(request: Request, error: case_folders.FolderConflictError):  # noqa: ARG001  # FastAPI supplies the request to exception handlers.
        return JSONResponse({"detail": str(error), "error": str(error), "code": error.code, "details": error.details}, status_code=409)

    @app.get("/api/projects/{project_id}/folders")
    def folders(project_id: str):
        return case_folders.list_folders(project_id)

    @app.get("/api/projects/{project_id}/folders/export")
    def export(project_id: str, folder_id: str | None = None, recursive: bool = True, expected_revision: str | None = None):
        with project_lock(project_id):
            selected = case_folders.selection(project_id, folder_id, recursive=recursive, expected_revision=expected_revision)
            organization = {key: selected[key] for key in ("folder_id", "folder_name", "recursive", "revision", "folders", "assignments")}
            receipt = reporting.export_cases(project_id, selected["case_ids"], organization=organization)
        return FileResponse(receipt["excel"], filename=f"{project_id}-cases.xlsx", media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            headers={"X-AutoSkill-Cases": str(len(selected["case_ids"]))})

    @app.post("/api/projects/{project_id}/folders")
    def create(project_id: str, body: dict):
        if set(body) - {"name", "parent_id", "expected_revision"}:
            raise ValueError("创建文件夹只接受 name、parent_id 和 expected_revision")
        return case_folders.create(project_id, body.get("name"), parent_id=body.get("parent_id"), expected_revision=body.get("expected_revision"))

    @app.post("/api/projects/{project_id}/folders/move-cases")
    def move_cases(project_id: str, body: dict):
        if set(body) - {"case_ids", "folder_id", "expected_revision"} or "folder_id" not in body:
            raise ValueError("移动用例需要 case_ids、folder_id（根目录为 null）和 expected_revision")
        return case_folders.move_cases(project_id, body.get("case_ids"), body["folder_id"], expected_revision=body.get("expected_revision"))

    @app.patch("/api/projects/{project_id}/folders/{folder_id}")
    def update(project_id: str, folder_id: str, body: dict):
        if set(body) - {"name", "parent_id", "expected_revision"}:
            raise ValueError("更新文件夹只接受 name、parent_id 和 expected_revision")
        changes = {key: body[key] for key in ("name", "parent_id") if key in body}
        return case_folders.update(project_id, folder_id, changes, expected_revision=body.get("expected_revision"))
