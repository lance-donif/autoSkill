"""Projects: one shareable folder each (cases, knowledge, login definition) plus a local index."""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from . import db, paths, secrets
from .core import store
from .replay import ir

ID = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")
# Creating and editing test data is what a test run does; destructive or external effects need opting in.
DEFAULT_APPROVALS = ["write"]
RISKS = {"write": "写入数据", "delete": "删除数据", "payment": "支付", "refund": "退款", "message": "发送消息"}


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


def create(name: str, base_url: str, category: str = "", project_id: str | None = None) -> dict:
    """Create a project. Naming the address to test is what authorises that address."""
    name = (name or "").strip()
    if not name:
        raise ValueError("请填写项目名称")
    origin = ir.origin_of(base_url or "")
    if not origin.startswith(("http://", "https://")) or origin.endswith("://"):
        raise ValueError("目标地址需要是完整网址，例如 http://10.0.0.8:8080")
    project_id = project_id or slug(name, base_url)
    if not ID.fullmatch(project_id):
        raise ValueError("项目标识只能使用小写字母、数字和短横线")
    folder = paths.projects_dir() / project_id
    if folder.exists():
        raise ValueError(f"项目 {project_id} 已存在")
    store.init(folder)
    try:
        meta = store.read(folder / "project.json")
        meta.update(id=project_id, name=name, category=category.strip(), start_url=base_url)
        store.write(folder / "project.json", meta)
        config = store.read(folder / "qa-config.json")
        config.update(mode="auto", execution_authorized=True, base_url=origin, allowed_origins=[origin],
                      approvals=list(DEFAULT_APPROVALS))
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


def meta(project_id: str) -> dict:
    return store.read(path(project_id) / "project.json")


def config(project_id: str) -> dict:
    return store.read(path(project_id) / "qa-config.json")


def auth(project_id: str) -> dict:
    file = path(project_id) / "auth.json"
    return store.read(file) if file.is_file() else {}


def set_auth(project_id: str, value: dict) -> None:
    store.write(path(project_id) / "auth.json", value)
    store.sync_skills(path(project_id))


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
    if fields.get("approvals") is not None:
        unknown = set(fields["approvals"]) - set(RISKS)
        if unknown:
            raise ValueError("不认识的操作类型：" + "、".join(sorted(unknown)))
        cfg["approvals"] = sorted(set(fields["approvals"]))
    if fields.get("ignore_https_errors") is not None:
        cfg["ignore_https_errors"] = bool(fields["ignore_https_errors"])
    if fields.get("timeout_seconds") is not None:
        cfg["timeout_seconds"] = max(3, min(120, int(fields["timeout_seconds"])))
    store.write(folder / "project.json", info)
    store.write(folder / "qa-config.json", cfg)
    store.sync_skills(folder)
    reindex(project_id)
    return get(project_id)


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
    wanted = set()
    for item in cases(project_id):
        wanted |= secrets.references(item)
    return sorted(name for name in wanted if secrets.get_secret(name) is None)


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


def get(project_id: str) -> dict:
    info, cfg, login = meta(project_id), config(project_id), auth(project_id)
    active = cases(project_id)
    return {
        "id": project_id, "name": info.get("name") or project_id, "category": info.get("category", ""),
        "base_url": cfg.get("base_url", ""), "start_url": info.get("start_url") or cfg.get("base_url", ""),
        "allowed_origins": cfg.get("allowed_origins", []), "approvals": cfg.get("approvals", []),
        "timeout_seconds": cfg.get("timeout_seconds", 20), "ignore_https_errors": bool(cfg.get("ignore_https_errors")),
        "cases": {"total": len(active), "ui": sum(c["kind"] == "ui" for c in active),
                  "api": sum(c["kind"] == "api" for c in active)},
        "login": {"ui_case": login.get("ui_case"), "api_case": login.get("api_case")},
        "missing_secrets": missing_secrets(project_id),
    }


def list_all() -> list[dict]:
    """Every project found on disk; the index is refreshed on the way."""
    db.init()
    found = []
    for folder in sorted(paths.projects_dir().iterdir()):
        if (folder / "project.json").is_file() and ID.fullmatch(folder.name):
            try:
                reindex(folder.name)
                found.append(get(folder.name))
            except (ValueError, KeyError, OSError) as error:
                found.append({"id": folder.name, "name": folder.name, "broken": str(error)[:200]})
    on_disk = {p["id"] for p in found}
    for row in db.rows("SELECT id FROM projects"):
        if row["id"] not in on_disk:  # folder removed by hand: drop its index, keep run history
            db.execute("DELETE FROM cases WHERE project_id = ?", (row["id"],))
            db.execute("DELETE FROM projects WHERE id = ?", (row["id"],))
    return found


def save_cases(project_id: str, incoming: list[dict]) -> list[dict]:
    """Add or replace cases by id (others are kept), then refresh the index."""
    folder = path(project_id)
    requirements = store.read(folder / "analysis/requirements.json")
    known = {r["id"] for r in requirements}
    merged = {c["id"]: c for c in cases(project_id, active_only=False)}
    for item in incoming:
        item = dict(item)
        if not item.get("requirement_ids"):
            item["requirement_ids"] = ["REQ-RECORDED"]
        if "REQ-RECORDED" in item["requirement_ids"] and "REQ-RECORDED" not in known:
            requirements.append({"id": "REQ-RECORDED", "module": "录制", "title": "录制得到的回归场景",
                                 "source": "autoskill record"})
            known.add("REQ-RECORDED")
        merged[item["id"]] = item
    ids = set(merged)
    for item in merged.values():
        unknown = [d for d in depends_on(item) if d not in ids]
        if unknown:
            raise ValueError(f'{item["id"]} 依赖的前置用例不存在：{"、".join(unknown)}')
        if item["kind"] == "ui" and "automation" in item.get("extensions", {}):
            ir.validate(item["extensions"]["automation"])
    store.write(folder / "analysis/requirements.json", requirements)
    saved = store.save_cases(folder, list(merged.values()))
    reindex(project_id)
    return saved


def next_case_id(project_id: str, kind: str) -> str:
    prefix = "TC-UI-" if kind == "ui" else "TC-API-"
    numbers = [int(c["id"][len(prefix):]) for c in cases(project_id, active_only=False)
               if c["id"].startswith(prefix) and c["id"][len(prefix):].isdigit()]
    return f"{prefix}{max(numbers, default=0) + 1:03d}"


def delete(project_id: str) -> None:
    folder = path(project_id)
    shutil.rmtree(folder)
    db.execute("DELETE FROM cases WHERE project_id = ?", (project_id,))
    db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    db.execute("DELETE FROM tasks WHERE project_id = ?", (project_id,))
