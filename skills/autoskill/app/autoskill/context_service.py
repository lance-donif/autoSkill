"""Bounded project, case and knowledge reads independent of presentation adapters."""
from __future__ import annotations

import codecs
import hashlib
from pathlib import Path

from . import knowledge as knowledge_files
from . import knowledge_entries, paging, projects, secrets, sessions
from .locking import project_lock
from .replay import ir


def _case_summary(item: dict) -> dict:
    return {**{key: item.get(key) for key in ("id", "kind", "title", "module", "revision", "requirement_ids", "lifecycle")},
            "depends_on": projects.depends_on(item), "replayable": ir.is_replayable(item),
            "content_revision": paging.revision(item)}


def cases(project_id: str, *, limit: int = 50, cursor: str | None = None, all_items: bool = False,
          kind: str | None = None, module: str | None = None, requirement: str | None = None, query: str = "") -> dict:
    with project_lock(project_id):
        pool = projects.cases(project_id)
    return _case_page(project_id, pool, limit=limit, cursor=cursor, all_items=all_items,
                      kind=kind, module=module, requirement=requirement, query=query)


def _case_page(project_id: str, pool: list[dict], *, limit: int = 50, cursor: str | None = None, all_items: bool = False,
               kind: str | None = None, module: str | None = None, requirement: str | None = None, query: str = "") -> dict:
    selected = [item for item in pool if (not kind or item["kind"] == kind) and (module is None or item["module"] == module)
                and (not requirement or requirement in item.get("requirement_ids", []))
                and (not query or query.casefold() in (item["id"] + " " + item["title"]).casefold())]
    selected.sort(key=lambda item: item["id"])
    filters = {"project": project_id, "kind": kind, "module": module, "requirement": requirement, "query": query}
    rows, page = paging.select(selected, content_revision=paging.revision(selected), query=filters,
                               cursor=cursor, limit=limit, all_items=all_items)
    return {"cases": [_case_summary(item) for item in rows], "page": page}


def case(project_id: str, case_id: str) -> dict:
    with project_lock(project_id):
        item = projects.case(project_id, case_id)
    return {"case": item, "content_revision": paging.revision(item)}


def _knowledge_index(project_id: str) -> list[dict]:
    found = []
    for name, file in knowledge_files.files(project_id):
        with file.open("rb") as stream:
            content_hash = hashlib.file_digest(stream, "sha256").hexdigest()
        readable = file.suffix.lower() in (".md", ".txt", ".json", ".csv")
        if readable and file.stat().st_size < 400:
            text = file.read_text(encoding="utf-8", errors="replace").strip()
            if "待补充；未提供不代表不存在" in text and len(text) < 120:
                continue
        found.append({"file": "knowledge/" + name, "bytes": file.stat().st_size,
                      "content_revision": content_hash, "text": readable, "content_loaded": False})
    return found


def knowledge(project_id: str, *, limit: int = 50, cursor: str | None = None, all_items: bool = False,
              query: str = "", preview_chars: int = 0) -> dict:
    pool = [row for row in _knowledge_index(project_id) if query.casefold() in row["file"].casefold()]
    rows, page = paging.select(pool, content_revision=paging.revision(pool), query={"project": project_id, "query": query},
                               limit=limit, cursor=cursor, all_items=all_items)
    if preview_chars:
        for row in rows:
            if row["text"]:
                read = read_knowledge(project_id, row["file"], length=preview_chars, content_revision=row["content_revision"])
                row.update(content=read["content"], truncated=read["has_more"], content_loaded=True)
    return {"knowledge": rows, "page": page}


def read_knowledge(project_id: str, file: str, *, offset: int = 0, length: int = 12000,
                   content_revision: str | None = None, all_items: bool = False) -> dict:
    if type(offset) is not int or offset < 0 or type(length) is not int or length < 1:
        raise paging.PageError("正文偏移需非负，读取长度需要是正整数")
    relative = file.removeprefix("knowledge/")
    path = knowledge_files.file_path(project_id, relative)
    if not path.is_file():
        raise ValueError("知识文件不存在；请先用 knowledge list 查看文件路径")
    chunks, total, digest = [], 0, hashlib.sha256()
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    with path.open("rb") as stream:
        while True:
            data = stream.read(65536)
            digest.update(data)
            chunk = decoder.decode(data, final=not data)
            start = max(0, offset - total)
            end = len(chunk) if all_items else min(len(chunk), offset + length - total)
            if end > start:
                chunks.append(chunk[start:end])
            total += len(chunk)
            if not data:
                break
    current_revision = digest.hexdigest()
    if content_revision and current_revision != content_revision:
        raise paging.PageError("知识文件内容已变化；请从开头重新读取", "cursor_expired")
    content = "".join(chunks)
    next_offset = offset + len(content)
    more = next_offset < total
    return {"file": "knowledge/" + Path(relative).as_posix(), "content": content, "offset": offset,
            "next_offset": next_offset if more else None, "total_length": total, "has_more": more,
            "content_revision": current_revision}


def context(project_id: str, *, limit: int = 50, cursor: str | None = None, knowledge_limit: int = 5,
            knowledge_cursor: str | None = None, all_items: bool = False) -> dict:
    with project_lock(project_id):
        meta, config, auth = projects.meta(project_id), projects.config(project_id), projects.auth(project_id)
        pool = projects.cases(project_id)
    login = next((item for item in pool if item["id"] == auth.get("ui_case")), None)
    wanted = set().union(*(secrets.references(item) for item in pool))
    missing = [name for name in sorted(wanted) if secrets.get_secret(name) is None]
    saved = sessions.summary(project_id)
    case_page = _case_page(project_id, pool, limit=limit, cursor=cursor, all_items=all_items)
    knowledge_page = knowledge(project_id, limit=knowledge_limit, cursor=knowledge_cursor, all_items=all_items, preview_chars=400)
    entries_page = knowledge_entries.search(project_id, limit=knowledge_limit)
    steps = (["先补齐凭据：" + "；".join(f"autoskill secret set {name}" for name in missing)] if missing else [])
    steps.append(f"回放已有用例：autoskill run {project_id}；用 cases 和 knowledge 分页读取详细资料")
    if not login:
        steps.insert(0, "尚未定义登录用例；需要登录时先 record start --as-login，使用 check 验证后保存")
    return {"project": {"id": project_id, "name": meta.get("name") or project_id, "category": meta.get("category", ""),
                        "base_url": config.get("base_url", ""), "start_url": meta.get("start_url") or config.get("base_url", ""),
                        "allowed_origins": config.get("allowed_origins", [])},
            "login": {"ui_case": auth.get("ui_case"), "api_case": auth.get("api_case"), "title": login["title"] if login else None,
                      "saved_session": saved["saved"], "saved_at": saved["saved_at"],
                      "note": "优先复用已有登录；人工认证后保留原会话继续" if login else "尚未定义登录方式"},
            "missing_secrets": missing, "cases": case_page["cases"], "knowledge": knowledge_page["knowledge"],
            "knowledge_entries": entries_page["entries"], "next_steps": steps,
            "page": {"cases": case_page["page"], "knowledge": knowledge_page["page"], "knowledge_entries": entries_page["pagination"]}}
