"""Human source selection supplements the existing capture detail and mapping endpoints."""
from __future__ import annotations

from fastapi import FastAPI

from .. import conversation_capture


def register(app: FastAPI) -> None:
    @app.get("/api/conversations/{project_id}")
    def captures(project_id: str, limit: int = 50, offset: int = 0):
        return conversation_capture.list_captures(project_id, limit=limit, offset=offset)

    @app.post("/api/conversations/{project_id}/import-content")
    def import_content(project_id: str, body: dict):
        if set(body) - {"content", "format", "name", "expected_head"}:
            raise ValueError("来源导入只接受 content、format、name 和 expected_head")
        return conversation_capture.import_content(project_id, body.get("content"), source_format=body.get("format"),
                                                   name=body.get("name", ""), expected_head=body.get("expected_head"))
