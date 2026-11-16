"""HTTP adapters for canonical knowledge settings, readers and portable packages."""
from __future__ import annotations

from fastapi import FastAPI, Request

from .. import knowledge_entries, knowledge_transfer


async def _body(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError as error:
        raise ValueError("请求正文不是有效 JSON") from error
    if not isinstance(body, dict):
        raise ValueError("请求正文需要是 JSON 对象")
    return body


def _boolean(value: str | None) -> bool:
    if value is None or value in ("false", "0"):
        return False
    if value in ("true", "1"):
        return True
    raise ValueError("team 需要是 true 或 false")


def register(app: FastAPI) -> None:
    @app.get("/api/knowledge/{project_id}/settings")
    def settings(project_id: str):
        return knowledge_entries.settings(project_id)

    @app.put("/api/knowledge/{project_id}/settings")
    async def update_settings(project_id: str, request: Request):
        return knowledge_entries.settings(project_id, await _body(request))

    @app.get("/api/knowledge/{project_id}/search")
    def search(project_id: str, request: Request):
        query = request.query_params
        return knowledge_entries.search(project_id, query=query.get("q", ""), team=_boolean(query.get("team")),
                                        limit=int(query.get("limit", 50)), cursor=query.get("cursor"))

    @app.get("/api/knowledge/{project_id}/entries/{entry_id}")
    def get_entry(project_id: str, entry_id: str, request: Request):
        revision = request.query_params.get("revision")
        if revision is None:
            raise ValueError("读取知识需要明确提供 revision 修订号")
        return knowledge_entries.get(project_id, entry_id, revision=int(revision))

    @app.post("/api/knowledge/{project_id}/export")
    async def export_package(project_id: str, request: Request):
        body = await _body(request)
        if set(body) not in ({"entries"}, {"all"}) or ("all" in body and body["all"] is not True):
            raise ValueError("导出需要且只能提供 entries 数组或 all: true")
        if "entries" in body and not isinstance(body["entries"], list):
            raise ValueError("entries 需要是知识 id 与 revision 的数组")
        return knowledge_transfer.export_package(project_id, entries=body.get("entries"), all_items=body.get("all", False))

    @app.post("/api/knowledge/{project_id}/import")
    async def import_package(project_id: str, request: Request):
        body = await _body(request)
        if set(body) - {"package", "idempotency_key", "as_new"} or not {"package", "idempotency_key"}.issubset(body):
            raise ValueError("导入需要 package、idempotency_key，及可选的 as_new")
        if type(body.get("as_new", False)) is not bool:
            raise ValueError("as_new 需要是布尔值")
        return knowledge_transfer.import_package(project_id, body["package"], idempotency_key=body["idempotency_key"], as_new=body.get("as_new", False))
