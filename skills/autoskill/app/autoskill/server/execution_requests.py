"""HTTP admission and read-only recovery for idempotent replay and native execution requests."""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .. import execution_requests


def register(app: FastAPI) -> None:
    @app.exception_handler(execution_requests.ExecutionRequestError)
    async def rejected(request: Request, error: execution_requests.ExecutionRequestError):  # noqa: ARG001  # FastAPI supplies the failing request.
        return JSONResponse({"code": error.code, "error": str(error), "detail": str(error), "details": error.details}, status_code=error.status_code)

    @app.post("/api/execution-requests")
    def start(body: dict):
        if set(body) - {"scope", "idempotency_key", "request"}:
            raise execution_requests.ExecutionRequestError("validation_error", "执行请求只接受 scope、idempotency_key 和 request 字段")
        return execution_requests.start(body.get("scope"), body.get("idempotency_key"), body.get("request"))

    @app.get("/api/execution-requests")
    def lookup(scope: str, idempotency_key: str):
        return execution_requests.lookup(scope, idempotency_key)
