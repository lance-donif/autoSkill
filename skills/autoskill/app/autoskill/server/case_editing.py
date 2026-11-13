"""Conditional saves for complete editable UI and API definitions."""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .. import case_editing


def register(app: FastAPI) -> None:
    @app.put("/api/projects/{project_id}/cases/{case_id}")
    async def save(project_id: str, case_id: str, request: Request):
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("保存参数需要是对象")
        try:
            return case_editing.save(project_id, case_id, body.get("case"), body.get("expected_content_revision"))
        except case_editing.CaseEditConflictError as error:
            return JSONResponse({"detail": str(error), "error": str(error), "code": "revision_conflict",
                                 "content_revision": error.content_revision, "revision": error.revision}, status_code=409)
