"""Read-only evidence and editable design rows over project/session case sheets."""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse

from .. import case_plans


def _session(request: Request) -> str:
    return request.query_params.get("session", "")


def register(app: FastAPI) -> None:
    @app.get("/api/case-plans/{project_id}")
    def get(project_id: str, request: Request):
        return case_plans.get(project_id, _session(request))

    @app.post("/api/case-plans/{project_id}/rows")
    def create(project_id: str, request: Request, body: dict):
        return case_plans.save(project_id, body.get("row"), session=_session(request), expected_revision=body.get("expected_revision"))

    @app.patch("/api/case-plans/{project_id}/rows/{case_id}")
    def update(project_id: str, case_id: str, request: Request, body: dict):
        return case_plans.save(project_id, body.get("row"), case_id=case_id, session=_session(request), expected_revision=body.get("expected_revision"))

    @app.post("/api/case-plans/{project_id}/rows/{case_id}/link")
    def link(project_id: str, case_id: str, request: Request, body: dict):
        return case_plans.link(project_id, case_id, body.get("replay_case_id", ""), session=_session(request), expected_revision=body.get("expected_revision"))

    @app.post("/api/case-plans/{project_id}/rows/{case_id}/apply")
    def apply_design(project_id: str, case_id: str, request: Request, body: dict):
        return case_plans.apply_to_case(project_id, case_id, session=_session(request), expected_revision=body.get("expected_revision"))

    @app.post("/api/case-plans/{project_id}/import-cases")
    def import_cases(project_id: str, request: Request, body: dict):
        return case_plans.import_cases(project_id, body.get("case_ids"), session=_session(request), expected_revision=body.get("expected_revision"))

    @app.post("/api/case-plans/{project_id}/adopt")
    def adopt(project_id: str, request: Request, body: dict):
        return case_plans.adopt(project_id, session=_session(request), expected_revision=body.get("expected_revision"), source_hash=body.get("source_hash", ""))

    @app.post("/api/case-plans/{project_id}/sync")
    def sync(project_id: str, request: Request, body: dict):
        return case_plans.sync(project_id, session=_session(request), expected_revision=body.get("expected_revision"))

    @app.post("/api/case-plans/{project_id}/run")
    def run(project_id: str, request: Request, body: dict):
        return case_plans.run(project_id, body.get("case_ids", []), session=_session(request), engine=body.get("engine", "playwright"), headed=body.get("headed"))

    @app.get("/api/case-plans/{project_id}/excel")
    def excel(project_id: str, request: Request):
        file = case_plans.export(project_id, session=_session(request))
        return FileResponse(file, filename=file.name, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @app.get("/api/case-plans/{project_id}/history/{version}")
    def history(project_id: str, version: int, request: Request):
        return case_plans.history(project_id, version, session=_session(request))

    @app.get("/api/case-plans/{project_id}/history/{version}/excel")
    def historical_excel(project_id: str, version: int, request: Request):
        file = case_plans.export(project_id, session=_session(request), version=version)
        return FileResponse(file, filename=file.name, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
