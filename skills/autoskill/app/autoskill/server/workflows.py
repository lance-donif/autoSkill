"""Workflow overview, requirement change review and host dispatch for the dashboard."""
from __future__ import annotations

from fastapi import FastAPI, Request

from .. import db, failure_context, workflow, workflow_changes, workflow_state


def register(app: FastAPI) -> None:
    @app.get("/api/failure-context/{project_id}")
    def failure(project_id: str, request: Request):
        query = request.query_params
        identifiers = {key: query.get(key) for key in ("run_id", "execution_attempt_id", "case_id")}
        if not all(identifiers.values()):
            raise ValueError("读取失败上下文需要 run_id、execution_attempt_id 和 case_id")
        return failure_context.read(project_id, **identifiers)

    @app.get("/api/workflows/{workflow_id}/diagnostic-context")
    def diagnostic(workflow_id: str):
        return workflow.diagnostic_context(workflow_id)

    @app.get("/api/workflows")
    def workflows(request: Request):
        workflow_state.init()
        project_ids = [value for value in request.query_params.get("projects", "").split(",") if value]
        where = "project_id IN (" + ",".join("?" for _ in project_ids) + ")" if project_ids else "1 = 1"
        rows = db.rows(f"SELECT id FROM workflow_runtime WHERE {where} ORDER BY updated_at DESC", project_ids)
        return {"items": [workflow.status(row["id"]) for row in rows], "total": len(rows)}

    @app.get("/api/workflow-dashboard/{workflow_id}")
    def overview(workflow_id: str):
        return workflow_changes.overview(workflow_id)

    @app.post("/api/workflow-dashboard/{workflow_id}/impact")
    def impact(workflow_id: str, body: dict):
        return workflow_changes.analyze(workflow_id, body)

    @app.post("/api/workflow-dashboard/{workflow_id}/maintenance")
    def maintenance(workflow_id: str, body: dict):
        return workflow_changes.maintain(workflow_id, body, idempotency_key=body.get("idempotency_key", ""))

    @app.post("/api/workflow-dashboard/{workflow_id}/dispatch")
    def dispatch(workflow_id: str, body: dict):
        return workflow_changes.dispatch(workflow_id, host=body.get("host", "dsh"), host_session_id=body.get("host_session_id", ""),
                                         idempotency_key=body.get("idempotency_key", ""))
