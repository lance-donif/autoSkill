"""Serve a fresh dashboard entry and a content-versioned module graph after upgrades."""
from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles


class FreshStaticFiles(StaticFiles):
    """Do not reuse validators based only on archive timestamps and file sizes."""

    def is_not_modified(self, _response_headers, _request_headers) -> bool:
        return False

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store"
        return response


def ui_revision(directory: Path) -> str:
    """Include transitive modules and CSS, even when version, size and mtime stay unchanged."""
    digest = hashlib.sha256()
    for file in sorted(path for path in directory.rglob("*") if path.is_file()):
        digest.update(file.relative_to(directory).as_posix().encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(file.read_bytes()).digest())
    return digest.hexdigest()


def mount_ui(app: FastAPI, directory: Path) -> str | None:
    """Version the whole relative-import namespace without changing source or user storage."""
    if not directory.is_dir():
        return None
    revision = ui_revision(directory)
    prefix = "/_ui/" + revision
    html = (directory / "index.html").read_text(encoding="utf-8")
    html = html.replace('href="/css/', f'href="{prefix}/css/').replace('src="/js/', f'src="{prefix}/js/')
    embedded_html = html.replace("<html ", '<html data-embed="true" ', 1)

    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    @app.api_route("/index.html", methods=["GET", "HEAD"], include_in_schema=False)
    def index(request: Request):
        # Match URLSearchParams.get() so the intended shell is present on the first paint.
        embedded = request.query_params.getlist("embed")[:1] == ["1"]
        return HTMLResponse(embedded_html if embedded else html, headers={"Cache-Control": "no-store"})

    app.mount(prefix, FreshStaticFiles(directory=directory), name="ui-version")
    # Keep old entry URLs usable while preventing their stale conditional responses.
    app.mount("/", FreshStaticFiles(directory=directory, html=True), name="ui")
    return revision
