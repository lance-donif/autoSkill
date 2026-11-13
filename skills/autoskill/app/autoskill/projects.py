"""Projects: one shareable folder each (cases, knowledge, login definition) plus a local index."""
from __future__ import annotations

import json
import math
import re
import shutil
import uuid
from functools import wraps
from pathlib import Path

from . import db, paths, secrets
from .core import store
from .locking import named_lock, project_lock
from .replay import ir

ID = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")


def _project_locked(operation):
    """Keep each project operation coherent across threads and independent CLI processes."""
    @wraps(operation)
    def locked(project_id, *args, **kwargs):
        with project_lock(project_id):
            return operation(project_id, *args, **kwargs)
    return locked



def path(project_id: str) -> Path:
    if not ID.fullmatch(project_id or ""):
        raise ValueError("项目标识只能使用小写字母、数字和短横线")
    folder = paths.projects_dir() / project_id
    if not (folder / "project.json").is_file():
        raise ValueError(f"项目 {project_id} 不存在")
    return folder


def slug(name: str, base_url: str = "") -> str:
    """A folder-safe id from the name; names without Latin letters fall back to the host."""
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40]
    if not base:
        from urllib.parse import urlsplit

        base = re.sub(r"[^a-z0-9]+", "-", (urlsplit(base_url).hostname or "").lower()).strip("-")[:40] or "project"
    candidate, n = base, 2
    while (paths.projects_dir() / candidate).exists():
        candidate, n = f"{base}-{n}", n + 1
    return candidate


def site_of(url: str) -> str:
    from urllib.parse import urlsplit

    return (urlsplit(url).netloc or "").lower().removeprefix("www.")


def same_site(url: str) -> str | None:
    """The project that already covers this site (host and port, with or without www); the one with most cases wins."""
    found = []
    for folder in sorted(paths.projects_dir().iterdir()):
        if not ID.fullmatch(folder.name):
            continue
        try:
            with project_lock(folder.name):
                if site_of(store.read(folder / "qa-config.json").get("base_url", "")) == site_of(url):
                    found.append((len(cases(folder.name)), folder.name))
        except (OSError, ValueError, KeyError):
            continue
    return max(found)[1] if found else None


def create(name: str, base_url: str, category: str = "", project_id: str | None = None) -> dict:
    """Create a project. Naming the address to test is what authorises that address."""
    name = (name or "").strip()
    if not name:
        raise ValueError("请填写项目名称")
    origin = ir.origin_of(base_url or "")
    if not origin.startswith(("http://", "https://")) or origin.endswith("://"):
        raise ValueError("目标地址需要是完整网址，例如 http://10.0.0.8:8080")
    with named_lock("projects", "catalog"):
        if project_id is None:
            existing = same_site(origin)
            if existing:  # one site is one project; its areas are sub-projects (the module of each case)
                return {**get(existing), "reused": True}
        project_id = project_id or slug(name, base_url)
        if not ID.fullmatch(project_id):
            raise ValueError("项目标识只能使用小写字母、数字和短横线")
        return _create_project(project_id, name, base_url, category, origin)


@_project_locked
def _create_project(project_id: str, name: str, base_url: str, category: str, origin: str) -> dict:
    folder = paths.projects_dir() / project_id
    if folder.exists():
        raise ValueError(f"项目 {project_id} 已存在")
    store.init(folder)
    try:
        meta = store.read(folder / "project.json")
        meta.update(id=project_id, name=name, category=category.strip(), start_url=base_url)
        store.write(folder / "project.json", meta)
        config = store.read(folder / "qa-config.json")
        config.update(mode="auto", execution_authorized=True, base_url=origin, allowed_origins=[origin])
        store.write(folder / "qa-config.json", config)
        store.write(folder / "auth.json", {})
        for junk in ("staging", "testcase-skill", "execution", "output", "input", "versions"):
            shutil.rmtree(folder / junk, ignore_errors=True)
        store.sync_skills(folder)
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    reindex(project_id)
    return get(project_id)


@_project_locked
def meta(project_id: str) -> dict:
    return store.read(path(project_id) / "project.json")


@_project_locked
def config(project_id: str) -> dict:
    return store.read(path(project_id) / "qa-config.json")


@_project_locked
def auth(project_id: str) -> dict:
    file = path(project_id) / "auth.json"
    return store.read(file) if file.is_file() else {}


@_project_locked
def set_auth(project_id: str, value: dict) -> None:
    store.write(path(project_id) / "auth.json", value)
    store.sync_skills(path(project_id))


@_project_locked
def update(project_id: str, **fields) -> dict:
    """Change name, category, extra allowed origins, approvals or timeout."""
    folder = path(project_id)
    info, cfg = meta(project_id), config(project_id)
    for key in ("name", "category"):
        if fields.get(key) is not None:
            info[key] = str(fields[key]).strip()
    if not info.get("name"):
        raise ValueError("请填写项目名称")
    if fields.get("allowed_origins") is not None:
        origins = []
        for item in fields["allowed_origins"]:
            origin = ir.origin_of(item)
            if not origin.startswith(("http://", "https://")) or origin.endswith("://"):
                raise ValueError(f"「{item}」不是完整网址")
            origins.append(origin)
        cfg["allowed_origins"] = sorted({cfg["base_url"], *origins})

    if fields.get("ignore_https_errors") is not None:
        if type(fields["ignore_https_errors"]) is not bool:
            raise ValueError("忽略 HTTPS 证书错误必须是布尔值")
        cfg["ignore_https_errors"] = fields["ignore_https_errors"]
    for key, label in (("page_load_seconds", "页面加载超时"), ("timeout_seconds", "操作超时")):
        if fields.get(key) is not None:
            value = fields[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(label + "需要是大于零的秒数")
            cfg[key] = value
    store.write(folder / "project.json", info)
    store.write(folder / "qa-config.json", cfg)
    store.sync_skills(folder)
    reindex(project_id)
    return get(project_id)


@_project_locked
def cases(project_id: str, active_only: bool = True) -> list[dict]:
    found = store.load_cases(path(project_id))
    return [c for c in found if c["lifecycle"] == "active"] if active_only else found


def case(project_id: str, case_id: str) -> dict:
    for item in cases(project_id, active_only=False):
        if item["id"] == case_id:
            return item
    raise ValueError(f"用例 {case_id} 不存在")


def depends_on(item: dict) -> list[str]:
    return list(item.get("extensions", {}).get("depends_on", []))


def missing_secrets(project_id: str) -> list[str]:
    """Secret names the project's cases refer to that this machine does not have yet."""
    return [name for name, value in credentials(project_id).items() if value is None]


def credentials(project_id: str) -> dict:
    """Every credential the project's cases refer to, with the value stored on this machine (None when not set)."""
    wanted = set()
    for item in cases(project_id):
        wanted |= secrets.references(item)
    return {name: secrets.get_secret(name) for name in sorted(wanted)}


@_project_locked
def reindex(project_id: str) -> None:
    """Rebuild the local index of one project from its files."""
    db.init()
    info, cfg, stamp = meta(project_id), config(project_id), db.now()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO projects (id, name, category, base_url, created_at, updated_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, category=excluded.category, "
            "base_url=excluded.base_url, updated_at=excluded.updated_at",
            (project_id, info.get("name") or project_id, info.get("category", ""), cfg.get("base_url", ""),
             info.get("created_at") or stamp, stamp),
        )
        known = {r["case_id"]: r for r in conn.execute("SELECT * FROM cases WHERE project_id = ?", (project_id,))}
        seen = set()
        for item in cases(project_id, active_only=False):
            seen.add(item["id"])
            old = known.get(item["id"])
            archived = item["lifecycle"] != "active"
            conn.execute(
                "INSERT OR REPLACE INTO cases (project_id, case_id, kind, title, module, priority, category, revision, "
                "lifecycle, replayable, depends_on, created_at, archived_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (project_id, item["id"], item["kind"], item["title"], item["module"], item["priority"],
                 item.get("extensions", {}).get("category", ""), item["revision"], item["lifecycle"],
                 int(ir.is_replayable(item)), json.dumps(depends_on(item)),
                 old["created_at"] if old else stamp,
                 (old["archived_at"] if old and old["archived_at"] else stamp) if archived else None),
            )
        for gone in set(known) - seen:
            conn.execute("DELETE FROM cases WHERE project_id = ? AND case_id = ?", (project_id, gone))


@_project_locked
def get(project_id: str) -> dict:
    info, cfg, login = meta(project_id), config(project_id), auth(project_id)
    active, known = cases(project_id), credentials(project_id)
    return {
        "id": project_id, "name": info.get("name") or project_id, "category": info.get("category", ""),
        "base_url": cfg.get("base_url", ""), "start_url": info.get("start_url") or cfg.get("base_url", ""),
        "allowed_origins": cfg.get("allowed_origins", []),
        "timeout_seconds": cfg.get("timeout_seconds", 20), "page_load_seconds": cfg.get("page_load_seconds", 90),
        "ignore_https_errors": bool(cfg.get("ignore_https_errors")),
        "cases": {"total": len(active), "ui": sum(c["kind"] == "ui" for c in active),
                  "api": sum(c["kind"] == "api" for c in active)},
        "login": {"ui_case": login.get("ui_case"), "api_case": login.get("api_case")},
        "missing_secrets": [name for name, value in known.items() if value is None],
    }


def list_all() -> list[dict]:
    """Every project found on disk; the index is refreshed on the way."""
    db.init()
    found = []
    for folder in sorted(paths.projects_dir().iterdir()):
        if not ID.fullmatch(folder.name):
            continue
        with project_lock(folder.name):
            if not (folder / "project.json").is_file():
                continue
            try:
                reindex(folder.name)
                found.append(get(folder.name))
            except (ValueError, KeyError, OSError) as error:
                found.append({"id": folder.name, "name": folder.name, "broken": str(error)[:200]})
    on_disk = {p["id"] for p in found}
    for row in db.rows("SELECT id FROM projects"):
        if row["id"] not in on_disk:  # folder removed by hand: drop its index, keep run history
            with project_lock(row["id"]):
                if (paths.projects_dir() / row["id"] / "project.json").is_file():
                    found.append(get(row["id"]))  # a concurrent import or creation finished after the directory listing
                    continue
                with db.connect() as conn:
                    conn.execute("DELETE FROM cases WHERE project_id = ?", (row["id"],))
                    conn.execute("DELETE FROM projects WHERE id = ?", (row["id"],))
    return found


class CaseConflictError(ValueError):
    """An executable case changed after a candidate captured its base content."""

    def __init__(self, case_id: str, expected_hash: str | None, actual_hash: str | None):
        self.case_id, self.expected_hash, self.actual_hash = case_id, expected_hash, actual_hash
        super().__init__(f"用例 {case_id} 已被其他任务更新，请读取最新版本后重新合并")


def _check_case_hashes(merged: dict, incoming: list[dict], expected_hashes: dict) -> None:
    from .domain import content_hash

    if not isinstance(expected_hashes, dict) or any(item["id"] not in expected_hashes for item in incoming):
        raise ValueError("条件保存必须提供每个待更新用例的原内容哈希")
    for case_id, expected in expected_hashes.items():
        current = content_hash(merged[case_id]) if case_id in merged else None
        if current != expected:
            raise CaseConflictError(case_id, expected, current)
    if any(not item.get("requirement_ids") for item in incoming):
        raise ValueError("条件保存的用例必须包含完整需求关联")


@_project_locked
def save_cases(project_id: str, incoming: list[dict], *, expected_hashes: dict[str, str | None] | None = None) -> list[dict]:
    """Add or replace cases by id (others are kept), then refresh the index."""
    folder = path(project_id)
    requirements = store.read(folder / "analysis/requirements.json")
    known = {r["id"] for r in requirements}
    merged = {c["id"]: c for c in cases(project_id, active_only=False)}
    if expected_hashes is not None:
        _check_case_hashes(merged, incoming, expected_hashes)
    for incoming_item in incoming:
        item = dict(incoming_item)
        if not item.get("requirement_ids"):
            item["requirement_ids"] = ["REQ-RECORDED"]
        if "REQ-RECORDED" in item["requirement_ids"] and "REQ-RECORDED" not in known:
            requirements.append({"id": "REQ-RECORDED", "module": "录制", "title": "录制得到的回归场景",
                                 "source": "autoskill record"})
            known.add("REQ-RECORDED")
        merged[item["id"]] = item
    ids = set(merged)
    incoming_ids = {item["id"] for item in incoming}
    reserved_file = folder / "deleted-case-ids.json"
    removed_ids = set(store.read(reserved_file)) if reserved_file.is_file() else set()
    for item in merged.values():
        unknown = [d for d in depends_on(item) if d not in ids]
        if unknown and (item["id"] in incoming_ids or not set(unknown).issubset(removed_ids)):
            raise ValueError(f'{item["id"]} 依赖的前置用例不存在：{"、".join(unknown)}')
        if item["kind"] == "ui" and "automation" in item.get("extensions", {}):
            ir.validate(item["extensions"]["automation"])
    store.write(folder / "analysis/requirements.json", requirements)
    saved = store.save_cases(folder, list(merged.values()), preserve_revisions=expected_hashes is not None)
    reindex(project_id)
    return saved


@_project_locked
def next_case_id(project_id: str, kind: str) -> str:
    prefix = "TC-UI-" if kind == "ui" else "TC-API-"
    db.init()
    identities = [c["id"] for c in cases(project_id, active_only=False)]
    identities += [row["case_id"] for row in db.rows("SELECT case_id FROM deleted_cases WHERE project_id = ?", (project_id,))]
    reserved = path(project_id) / "deleted-case-ids.json"
    if reserved.is_file():
        identities += store.read(reserved)
    numbers = [int(identity[len(prefix):]) for identity in identities
               if identity.startswith(prefix) and identity[len(prefix):].isdigit()]
    return f"{prefix}{max(numbers, default=0) + 1:03d}"


@_project_locked
def delete_case(project_id: str, case_id: str) -> dict:
    """Remove one current definition, retaining history and explicit dependent-case contracts."""
    if not isinstance(case_id, str) or not case_id:
        raise ValueError("用例标识不能为空")
    folder = path(project_id)
    current = cases(project_id, active_only=False)
    found = any(item["id"] == case_id for item in current)
    db.init()
    # Reserve the identity before touching files, so a failed/retried deletion cannot recycle it.
    if found:
        reserved = folder / "deleted-case-ids.json"
        reserved_ids = store.read(reserved) if reserved.is_file() else []
        store.write(reserved, sorted(set(reserved_ids) | {case_id}))
        db.execute("INSERT OR IGNORE INTO deleted_cases(project_id,case_id,deleted_at) VALUES(?,?,?)", (project_id, case_id, db.now()))
        snapshot = folder / "versions" / ("SNAPSHOT-" + uuid.uuid4().hex)
        for filename in store.FILES.values():
            file = folder / "testcases" / filename
            items = store.read(file)
            if any(item["id"] == case_id for item in items):
                destination = snapshot / "testcases" / filename
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(file, destination)
                store.write(file, [item for item in items if item["id"] != case_id])
    login = auth(project_id)
    kept = {key: value for key, value in login.items() if key not in ("ui_case", "api_case", "api_check_case") or value != case_id}
    if kept != login:
        store.write(folder / "auth.json", kept)
    store.sync_skills(folder)
    reindex(project_id)
    return {"project_id": project_id, "case_id": case_id, "already_deleted": not found,
            "dependent_cases": [item["id"] for item in current if item["id"] != case_id and case_id in depends_on(item)]}


@_project_locked
def delete(project_id: str) -> None:
    folder = path(project_id)
    shutil.rmtree(folder)
    with db.connect() as conn:
        conn.execute("DELETE FROM cases WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        conn.execute("DELETE FROM tasks WHERE project_id = ?", (project_id,))
