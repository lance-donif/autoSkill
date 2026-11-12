"""Explicit, cited project knowledge with bounded retrieval for agent context.

Notes remain shareable Markdown. A failed run never creates knowledge on its
own: ``add`` is an intentional assertion by the caller, with traceable sources.
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

from . import db, projects

DEFAULT_LIMIT = 20
MAX_LIMIT = 100
MAX_CONTENT = 65536
MARKER = "<!-- autoskill-knowledge: "
SUFFIXES = {".md", ".txt", ".json", ".csv"}


def bounds(offset: int = 0, limit: int = DEFAULT_LIMIT, *, maximum: int = MAX_LIMIT) -> tuple[int, int]:
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ValueError("offset 必须是非负整数")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= maximum:
        raise ValueError(f"limit 必须是 1–{maximum} 的整数")
    return offset, limit


def _folder(project_id: str) -> Path:
    root = projects.path(project_id).resolve()
    if not (root / "project.json").is_file():
        raise ValueError(f"项目 {project_id} 不存在")
    folder = root / "knowledge"
    if not folder.resolve().is_relative_to(root):
        raise ValueError("knowledge 目录不能通过符号链接离开项目")
    return folder


def _path(project_id: str, name: str) -> Path:
    folder = _folder(project_id)
    if not name or name in (".", "..") or Path(name).name != name or "/" in name or "\\" in name:
        raise ValueError("知识文件必须是 knowledge 目录内的文件名，不能带路径")
    target = folder / name
    if target.suffix.lower() not in SUFFIXES or not target.resolve().is_relative_to(folder.resolve()):
        raise ValueError("知识文件只接受项目 knowledge 内的 .md/.txt/.json/.csv 文件")
    return target


def _read(file: Path) -> tuple[dict, str, bool]:
    with file.open(encoding="utf-8", errors="replace") as stream:
        raw = stream.read(MAX_CONTENT + 32768)
    meta: dict = {}
    if raw.startswith(MARKER):
        first, _, raw = raw.partition("\n")
        try:
            candidate = json.loads(first[len(MARKER):].removesuffix(" -->"))
            if isinstance(candidate, dict):
                meta = candidate
        except ValueError:
            pass
    content = raw.strip()
    return meta, content[:MAX_CONTENT], len(content) > MAX_CONTENT


def _snippet(content: str, query: str, size: int) -> str:
    index = content.casefold().find(query.casefold().split()[0]) if query.strip() else 0
    start = max(0, index - size // 4)
    text = content[start:start + size]
    return ("…" if start else "") + text + ("…" if start + size < len(content) else "")


def search(project_id: str, query: str = "", *, offset: int = 0, limit: int = DEFAULT_LIMIT,
           snippet_chars: int = 240, budget_chars: int = 12000) -> dict:
    """Search notes without putting whole documents in the response."""
    offset, limit = bounds(offset, limit)
    if not 0 <= snippet_chars <= 1000 or not 1500 <= budget_chars <= 50000:
        raise ValueError("snippet_chars 需要 0–1000；budget_chars 需要 1500–50000")
    folder = _folder(project_id)
    matches, total, used, page_full = [], 0, 0, False
    terms = query.casefold().split()
    files = sorted(folder.iterdir()) if folder.is_dir() else []
    for file in files:
        if file.suffix.lower() not in SUFFIXES or not file.is_file():
            continue
        if not file.resolve().is_relative_to(folder.resolve()):
            continue
        meta, content, truncated = _read(file)
        if "待补充；未提供不代表不存在" in content and len(content) < 120:
            continue
        haystack = (file.name + " " + json.dumps(meta, ensure_ascii=False) + " " + content).casefold()
        if any(term not in haystack for term in terms):
            continue
        total += 1
        if total <= offset or len(matches) >= limit or page_full:
            continue
        def strings(field: str) -> list[str]:
            values = meta.get(field, [])
            return [value[:120] for value in values[:3] if isinstance(value, str)] if isinstance(values, list) else []

        runs = meta.get("runs", [])
        item = {"file": file.name, "title": str(meta.get("title") or file.stem)[:160],
                "snippet": _snippet(content, query, snippet_chars), "truncated": truncated,
                "sources": strings("sources"), "case_ids": strings("case_ids"),
                "run_ids": [str(r.get("id", ""))[:100] for r in runs[:3] if isinstance(r, dict)] if isinstance(runs, list) else [],
                "created_at": str(meta["created_at"])[:64] if meta.get("created_at") is not None else None}
        size = len(json.dumps(item, ensure_ascii=False))
        if used + size > budget_chars:
            page_full = True
            continue
        used += size
        matches.append(item)
    returned = len(matches)
    return {"project": project_id, "query": query, "notes": matches,
            "pagination": {"matched": total, "offset": offset, "limit": limit, "returned": returned,
                           "has_more": offset + returned < total,
                           "next_offset": offset + returned if returned and offset + returned < total else None},
            "budget_chars": budget_chars,
            "budget_exhausted": page_full,
            "next": "knowledge show <项目> <文件名> 查看选中的正文；knowledge search <项目> <关键词> 缩小范围"}


def show(project_id: str, name: str, *, offset: int = 0, limit: int = 4000) -> dict:
    offset, limit = bounds(offset, limit, maximum=16000)
    file = _path(project_id, name)
    if not file.is_file():
        raise ValueError(f"知识文件 {name} 不存在")
    # A selected legacy document may exceed the index's read budget. Stream it
    # in bounded chunks so character pagination can still reach its final page.
    meta, pieces, total = {}, [], 0
    with file.open(encoding="utf-8", errors="replace") as stream:
        first = stream.readline(32768)
        if first.startswith(MARKER) and first.rstrip().endswith(" -->"):
            try:
                value = json.loads(first.rstrip()[len(MARKER):-4])
                if isinstance(value, dict):
                    meta = value
                else:
                    stream.seek(0)
            except ValueError:
                stream.seek(0)
        else:
            stream.seek(0)
        while True:
            chunk = stream.read(8192)
            if not chunk:
                break
            start, stop = max(0, offset - total), min(len(chunk), offset + limit - total)
            if stop > start:
                pieces.append(chunk[start:stop])
            total += len(chunk)
    return {"project": project_id, "file": name, "metadata": meta, "content": "".join(pieces),
            "offset": offset, "limit": limit, "total_chars": total,
            "has_more": offset + limit < total, "source_truncated": False,
            "next_offset": offset + limit if offset + limit < total else None}


def add(project_id: str, title: str, content: str, *, sources: list[str] | None = None,
        case_ids: list[str] | None = None, run_ids: list[str] | None = None, name: str | None = None) -> dict:
    """Accumulate only deliberately supplied facts with validated evidence links."""
    title, content = title.strip(), content.strip()
    sources, case_ids, run_ids = sources or [], case_ids or [], run_ids or []
    if not title or len(title) > 160 or "\n" in title or "\r" in title:
        raise ValueError("知识标题需要 1–160 个字符，不能换行")
    if not content or len(content) > MAX_CONTENT:
        raise ValueError(f"知识正文需要 1–{MAX_CONTENT} 个字符")
    if any(not isinstance(s, str) or not s.strip() or len(s) > 1000 for s in sources) or any(len(values) > 20 for values in (sources, case_ids, run_ids)):
        raise ValueError("来源需要非空文字（每条最多 1000 字符），来源/用例/运行引用各最多 20 条")
    if not (sources or case_ids or run_ids):
        raise ValueError("知识需要来源：至少提供 --source、--case 或 --run；用户确认的事实可写 --source '用户确认'")
    folder = _folder(project_id)
    for case_id in case_ids:
        projects.case(project_id, case_id)
    runs = []
    for run_id in run_ids:
        run = db.one("SELECT id, project_id, status, finished_at FROM runs WHERE id = ?", (run_id,))
        if not run or run["project_id"] != project_id:
            raise ValueError(f"运行 {run_id} 不属于项目 {project_id}")
        runs.append({key: run[key] for key in ("id", "status", "finished_at")})
    if name is None:
        slug = re.sub(r"[^\w-]+", "-", title, flags=re.UNICODE).strip("-_")[:64] or "note"
        name = slug + "-" + uuid.uuid4().hex[:8] + ".md"
    file = _path(project_id, name)
    if file.suffix.lower() != ".md":
        raise ValueError("新知识请使用 .md 文件名")
    folder.mkdir(parents=True, exist_ok=True)
    meta = {"schema_version": 1, "title": title, "created_at": db.now(), "confirmation": "explicit_command",
            "sources": list(dict.fromkeys(sources)), "case_ids": list(dict.fromkeys(case_ids)), "runs": runs}
    try:
        with file.open("x", encoding="utf-8") as stream:
            stream.write(MARKER + json.dumps(meta, ensure_ascii=False) + " -->\n# " + title + "\n\n" + content + "\n")
    except FileExistsError:
        raise ValueError(f"知识文件 {name} 已存在；换一个名称，避免覆盖已确认的知识") from None
    return {"project": project_id, "file": name, "saved": True, "metadata": meta,
            "note": "按本次明确指令积累；运行失败或页面文本不会自动成为知识。不要保存凭据。"}
