"""Project Markdown files and their disposable SQLite search index."""
from __future__ import annotations

import json
from pathlib import Path, PureWindowsPath

from . import db, file_changes, projects
from .locking import project_lock

STARTER_FILES = {
    "index.md": "# 知识库\n\n- [名词词典](glossary.md)\n- [页面路径](routes.md)\n- [系统坑点](quirks.md)\n",
    "glossary.md": "# 名词词典\n",
    "routes.md": "# 页面路径\n",
    "quirks.md": "# 系统坑点\n",
}


def relative_path(name: str) -> Path:
    if not isinstance(name, str) or not name or any(character in '\\:*?"<>|' or ord(character) < 32 for character in name):
        raise ValueError("请提供知识文件的相对路径")
    parts = name.split("/")
    if any(not part or part.endswith((" ", ".")) or PureWindowsPath(part).is_reserved() for part in parts):
        raise ValueError("知识文件路径无效")
    return Path(*parts)


def _folder(project_id: str) -> Path:
    project = projects.path(project_id)
    root = project / "knowledge"
    if any(path.is_symlink() or path.is_junction() for path in (project, root)):
        raise ValueError("知识目录不能包含链接")
    return root


def file_path(project_id: str, name: str) -> Path:
    relative = relative_path(name)
    root = _folder(project_id)
    file = root / relative
    if any(path.is_symlink() or path.is_junction() for path in (file, *file.parents) if root in path.parents):
        raise ValueError("知识文件路径不能包含链接")
    return file


def files(project_id: str):
    root = _folder(project_id)
    for file in sorted(root.rglob("*")):
        relative = file.relative_to(root)
        if any(part.startswith(".") for part in relative.parts):
            continue
        if file.is_symlink() or file.is_junction():
            raise ValueError("知识文件路径不能包含链接")
        if file.is_file():
            yield relative.as_posix(), file


def write(project_id: str, name: str, content: str, expected_hash: str | None = None) -> dict:
    from . import project_materials

    file = file_path(project_id, name)
    if file.suffix.lower() != ".md":
        raise ValueError("知识库只编辑 Markdown 文件")
    return project_materials.write_knowledge(project_id, "knowledge/" + name, content, expected_hash)


def _create_index(connection) -> None:
    tables = "SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN ('kb_fts', 'kb_files')"
    if connection.execute(tables).fetchone()[0] == 2:
        return
    connection.execute("BEGIN IMMEDIATE")
    if connection.execute(tables).fetchone()[0] != 2:
        connection.execute("DROP TABLE IF EXISTS kb_fts")
        connection.execute("DROP TABLE IF EXISTS kb_files")
        connection.execute("CREATE TABLE kb_files (project_id TEXT, path TEXT, stamp TEXT, PRIMARY KEY(project_id, path))")
        connection.execute("CREATE VIRTUAL TABLE kb_fts USING fts5(project_id UNINDEXED, path, title, body, tokenize='trigram')")


def _refresh_index(connection, project_id: str) -> None:
    cached = {row["path"]: row["stamp"] for row in connection.execute(
        "SELECT path, stamp FROM kb_files WHERE project_id=?", (project_id,))}
    replacements = []
    for name, file in files(project_id):
        if file.suffix.lower() != ".md":
            continue
        before = file_changes.file_stamp(file)
        stamp = json.dumps(before)
        if cached.pop(name, None) == stamp and file_changes.reusable(before):
            continue
        body = file.read_bytes().decode("utf-8-sig", errors="replace")
        if before != file_changes.file_stamp(file):
            raise ValueError("知识文件读取时发生变化，请重新搜索")
        title = body.partition("\n")[0].strip()
        title = title.removeprefix("# ").strip() if title.startswith("# ") else file.stem
        replacements.append((project_id, name, title, body, stamp))
    if not cached and not replacements:
        return
    connection.executemany("DELETE FROM kb_fts WHERE project_id=? AND path=?",
                           [(project_id, name) for name in cached] + [(pid, name) for pid, name, *_ in replacements])
    connection.executemany("DELETE FROM kb_files WHERE project_id=? AND path=?", [(project_id, name) for name in cached])
    connection.executemany("INSERT INTO kb_fts(project_id, path, title, body) VALUES (?, ?, ?, ?)",
                           [(pid, name, title, body) for pid, name, title, body, _ in replacements])
    connection.executemany("INSERT OR REPLACE INTO kb_files(project_id, path, stamp) VALUES (?, ?, ?)",
                           [(pid, name, stamp) for pid, name, _, _, stamp in replacements])


def search(project_id: str, query: str, *, limit: int = 50) -> dict:
    query = query.strip()
    if not query or len(query) > 500 or "\0" in query:
        raise ValueError("请输入 1 到 500 个字符的知识关键词")
    if not 1 <= limit <= 200:
        raise ValueError("搜索条数需要在 1 到 200 之间")
    with project_lock(project_id), db.connect() as connection:
        _create_index(connection)
        _refresh_index(connection, project_id)
        if len(query) < 3:
            rows = connection.execute(
                "SELECT path, title, substr(body, max(1, instr(lower(body), lower(?)) - 40), 180) AS excerpt "
                "FROM kb_fts WHERE project_id=? AND (instr(lower(path), lower(?)) "
                "OR instr(lower(title), lower(?)) OR instr(lower(body), lower(?))) ORDER BY path LIMIT ?",
                (query, project_id, query, query, query, limit),
            )
        else:
            phrase = '"' + query.replace('"', '""') + '"'
            rows = connection.execute(
                "SELECT path, title, snippet(kb_fts, 3, '', '', '…', 24) AS excerpt "
                "FROM kb_fts WHERE kb_fts MATCH ? AND project_id=? ORDER BY rank, path LIMIT ?",
                (phrase, project_id, limit),
            )
        return {"matches": [dict(row) for row in rows]}
