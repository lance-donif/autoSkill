"""HTTP transport for shared host binding references and read-only context."""
from __future__ import annotations

from fastapi import FastAPI

from .. import host_bindings, host_context


def register(app: FastAPI) -> None:
    @app.get("/api/host/bindings")
    def bindings(host: str, host_session_id: str, limit: int = 20, cursor: str | None = None):
        return host_bindings.list_bindings(host, host_session_id, limit=limit, cursor=cursor)

    @app.post("/api/host/bindings")
    def bind(body: dict):
        return host_bindings.upsert(body)

    @app.post("/api/host/bindings/select")
    def select(body: dict):
        return host_bindings.select(body)

    @app.get("/api/host/context")
    def context(host: str, host_session_id: str, binding_id: str | None = None, limit: int = 20, cursor: str | None = None,
                runs_limit: int = 10, runs_cursor: str | None = None, revision_only: bool = False):
        return host_context.read(host, host_session_id, binding_id=binding_id, limit=limit, cursor=cursor,
                                 runs_limit=runs_limit, runs_cursor=runs_cursor, revision_only=revision_only)

    @app.get("/api/host/requests")
    def request_receipt(host: str, host_session_id: str, kind: str, key: str, scope: str = ""):
        return host_bindings.request_receipt(host, host_session_id, kind=kind, key=key, scope=scope)
