"""Sync folder routes: inspect the configured folders and scan on demand."""
from __future__ import annotations

from fastapi import FastAPI

from .. import sync_import


def register(app: FastAPI) -> None:
    @app.get("/api/sync")
    def sync_status():
        return sync_import.status()

    @app.post("/api/sync/scan")
    def sync_scan():
        return sync_import.scan()
