"""Serve a fresh dashboard entry and a content-versioned module graph after upgrades."""
from __future__ import annotations

import hashlib
from pathlib import Path

from anyio import to_thread
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from .http_cache import etag_matches


class FreshStaticFiles(StaticFiles):
    """Do not reuse validators based only on archive timestamps and file sizes."""

    def is_not_modified(self, _response_headers, _request_headers) -> bool:
        return False

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store"
        return response


class VersionedStaticFiles(FreshStaticFiles):
    """Only send cacheable bytes that match the startup content namespace."""

    def __init__(self, directory: Path, hashes: dict[str, str]):
        super().__init__(directory=directory)
        self.hashes = hashes

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if not isinstance(response, FileResponse) or response.status_code != 200:
            return response
        expected = self.hashes.get(Path(path).as_posix())
        if expected is None:
            return Response(status_code=404, headers={"Cache-Control": "no-store"})
        tag = f'"{expected}"'
        headers = {"Cache-Control": "public, max-age=31536000, immutable", "ETag": tag}
        if etag_matches(Request(scope).headers.get("if-none-match", ""), tag):
            return Response(status_code=304, headers=headers)
        try:
            content = await to_thread.run_sync(Path(response.path).read_bytes)
        except FileNotFoundError:
            return Response(status_code=404, headers={"Cache-Control": "no-store"})
        if hashlib.sha256(content).hexdigest() != expected:
            return Response(status_code=404, headers={"Cache-Control": "no-store"})
        headers["Content-Length"] = str(len(content))
        return Response(content if scope["method"] != "HEAD" else b"", media_type=response.media_type, headers=headers)


def _inventory(directory: Path) -> tuple[str, dict[str, str]]:
    """Include transitive modules and CSS, even when version, size and mtime stay unchanged."""
    digest = hashlib.sha256()
    hashes = {}
    for file in sorted(path for path in directory.rglob("*") if path.is_file()):
        name = file.relative_to(directory).as_posix()
        checksum = hashlib.sha256(file.read_bytes())
        hashes[name] = checksum.hexdigest()
        digest.update(name.encode("utf-8") + b"\0")
        digest.update(checksum.digest())
    return digest.hexdigest(), hashes


def ui_revision(directory: Path) -> str:
    return _inventory(directory)[0]


def mount_ui(app: FastAPI, directory: Path) -> str | None:
    """Version the whole relative-import namespace without changing source or user storage."""
    if not directory.is_dir():
        return None
    revision, hashes = _inventory(directory)
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

    app.mount(prefix, VersionedStaticFiles(directory, hashes), name="ui-version")
    # Keep old entry URLs usable while preventing their stale conditional responses.
    app.mount("/", FreshStaticFiles(directory=directory, html=True), name="ui")
    return revision
