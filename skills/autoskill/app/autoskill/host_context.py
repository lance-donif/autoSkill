"""Read-only host summaries derive business state from the kernel's existing records."""
from __future__ import annotations

import json
from urllib.parse import quote

from . import host_bindings as bindings
from . import paging, paths, report_locations

ACTIVE = ("Queued", "Running")
OPEN_ITEMS = ("ready", "leased", "publishing", "waiting_execution", "recovering")


def _workflow(connection, binding: dict) -> dict | None:
    workflow_id = binding["workflow_id"]
    if not workflow_id:
        return None
    row = connection.execute("SELECT * FROM workflow_runtime WHERE id = ?", (workflow_id,)).fetchone() if bindings.has_table(connection, "workflow_runtime") else None
    if not row:
        return {"workflow_id": workflow_id, "cycle_id": binding["cycle_id"], "missing": True}
    flow = dict(row)
    states = connection.execute("SELECT state,COUNT(*) n FROM workflow_items WHERE workflow_id = ? AND cycle_id = ? GROUP BY state",
                                (workflow_id, binding["cycle_id"])).fetchall()
    phase = connection.execute("SELECT phase FROM workflow_items WHERE workflow_id = ? AND cycle_id = ? "
                               "AND state IN ('ready','leased','publishing','waiting_execution','recovering') ORDER BY sequence LIMIT 1",
                               (workflow_id, binding["cycle_id"])).fetchone()
    stale = binding["cycle_id"] != flow["cycle_id"]
    return {"workflow_id": workflow_id, "project_id": flow["project_id"], "title": flow["title"], "cycle_id": binding["cycle_id"],
            "current_cycle_id": flow["cycle_id"], "stale_cycle": stale, "revision": flow["revision"], "domain_head": flow["domain_head"],
            "lifecycle": flow["lifecycle"] if not stale else None, "current_lifecycle": flow["lifecycle"],
            "conclusion": flow["conclusion"] if not stale else None, "current_phase": phase["phase"] if phase else None,
            "counts": {row["state"]: row["n"] for row in states}, "updated_at": flow["updated_at"]}


def _run_scope(connection, binding: dict) -> tuple[str, tuple, str]:
    if binding["run_id"]:
        return "r.id = ?", (binding["run_id"],), "explicit_run"
    if binding["workflow_id"]:
        if not bindings.has_table(connection, "workflow_items"):
            return "0", (), "workflow_cycle"
        return ("r.id IN (SELECT run_id FROM workflow_items WHERE workflow_id = ? AND cycle_id = ?)",
                (binding["workflow_id"], binding["cycle_id"]), "workflow_cycle")
    if binding["task_session_id"]:
        project = " AND r.project_id = ?" if binding["project_id"] else ""
        parameters = (binding["task_session_id"], binding["project_id"]) if project else (binding["task_session_id"],)
        return "r.session_id = ?" + project, parameters, "task_session"
    # An explicit legacy default remains usable, but is not proof of ownership of every unnamed run.
    return "0", (), "unbound_legacy_session"


def _run_counts(connection, where: str, parameters: tuple) -> dict:
    rows = connection.execute(f"SELECT status,COUNT(*) n FROM runs r WHERE {where} GROUP BY status", parameters)
    counts = {row["status"]: row["n"] for row in rows}
    progress = dict(connection.execute(
        f"SELECT MAX(r.rowid) newest,COALESCE(SUM(done),0) done,COALESCE(SUM(cancel),0) cancel,"
        f"COALESCE(SUM(delete_requested),0) deleting FROM runs r WHERE {where}", parameters).fetchone())
    return {"states": counts, **progress}


def _run_view(row) -> dict:
    value = dict(row)
    reason = value["reason"]
    value.update(can_cancel=value["status"] in ACTIVE and not value["delete_requested"],
                 execution_status=value["status"], updated_at=value["finished_at"] or value["started_at"] or value["created_at"],
                 reason=reason[:600], reason_truncated=len(reason) > 600,
                 read_url="/api/native-runs/" + quote(value["id"], safe="") if value["family"] == "native" else "/api/runs/" + quote(value["id"], safe=""))
    return value


def _runs(connection, binding: dict, *, limit: int, cursor: str | None) -> tuple[dict, dict, tuple]:
    if type(limit) is not int or limit < 1:
        raise bindings.BindingError("validation_error", "每页运行数量需要是正整数")
    where, parameters, source = _run_scope(connection, binding)
    counts = _run_counts(connection, where, parameters)
    query = {"binding": binding["id"], "revision": binding["revision"], "source": source}
    revision = paging.revision([query, counts])
    offset = paging.position(cursor, revision, query)
    rows = connection.execute(
        "SELECT r.id,r.project_id,r.family,r.status,r.total,r.done,r.passed,r.failed,r.blocked,r.current,r.reason,r.cancel,"
        "r.delete_requested,r.deletion_error,r.created_at,r.started_at,r.finished_at,r.session_id "
        f"FROM runs r WHERE {where} ORDER BY r.created_at DESC,r.id DESC LIMIT ? OFFSET ?", (*parameters, limit, offset))
    items = [_run_view(row) for row in rows]
    page = bindings.page_info(sum(counts["states"].values()), len(items), limit, offset, revision, query)
    return {"items": items, "page": {**page, "consistency": "live"}, "association": source}, counts, (where, parameters)


def _pending(connection, binding: dict, workflow: dict | None, counts: dict, scope: tuple) -> dict:
    where, parameters = scope
    judges = {}
    if bindings.has_table(connection, "dialogue_judgements"):
        judges = {row["state"]: row["n"] for row in connection.execute(
            f"SELECT state,COUNT(*) n FROM dialogue_judgements WHERE run_id IN (SELECT r.id FROM runs r WHERE {where}) GROUP BY state", parameters)}
    operations = 0
    if binding["workflow_id"] and bindings.has_table(connection, "workflow_operations"):
        operations = connection.execute("SELECT COUNT(*) FROM workflow_operations o JOIN workflow_items i ON i.id=o.item_id "
                                        "WHERE i.workflow_id=? AND i.cycle_id=? AND o.side_effect=1 AND o.state IN ('intended','unknown')",
                                        (binding["workflow_id"], binding["cycle_id"])).fetchone()[0]
    item_counts = (workflow or {}).get("counts", {})
    return {"runs": sum(counts["states"].get(status, 0) for status in ACTIVE), "deletions": counts["deleting"],
            "workflow_items": sum(item_counts.get(status, 0) for status in OPEN_ITEMS), "uncertain_operations": operations,
            "judgements": {key: count for key, count in judges.items() if key in ("queued", "leased")}}


def _report(report_id: str, *, run: dict | None = None) -> dict:
    result = {"report_id": report_id, "receipt_status": "not_generated", "verification": "not_checked",
              "read_url": "/api/task-reports/" + quote(report_id, safe="")}
    if run and run["delete_requested"]:
        return {**result, "receipt_status": "deleting"}
    try:
        file = report_locations.folder(report_id) / "receipt.json"
        if not file.resolve().is_relative_to(paths.workspace().resolve()):
            return {**result, "receipt_status": "unavailable", "error": "报告路径不属于当前工作区"}
    except (OSError, ValueError, TypeError):
        return {**result, "receipt_status": "unavailable", "error": "现有报告位置无法读取"}
    try:
        receipt = json.loads(file.read_bytes())
    except FileNotFoundError:
        return result
    except (OSError, ValueError):
        return {**result, "receipt_status": "unavailable", "error": "现有报告收据无法读取"}
    if not isinstance(receipt, dict) or receipt.get("report_id") != report_id:
        return {**result, "receipt_status": "unavailable", "error": "现有报告收据身份不匹配"}
    result.update(receipt_status=receipt.get("status", "unavailable"),
                  **{key: receipt[key] for key in ("kind", "task_id", "generated_at", "updated_at", "source_hash", "error", "retryable") if key in receipt})
    result["artifacts"] = [{"format": kind, "declared_sha256": receipt.get(prefix + "sha256"), "declared_size": receipt.get(prefix + "size"),
                            "download_url": result["read_url"] + "/" + kind} for kind, prefix in (("excel", ""), ("markdown", "markdown_"), ("bugs", "bugs_")) if receipt.get(kind)]
    return result


def _references(binding: dict, workflow: dict | None, runs: dict) -> dict:
    refs = {"runs": [{"run_id": run["id"], "read_url": run["read_url"], "cancel_url": "/api/runs/" + quote(run["id"], safe="") + "/cancel"}
                     for run in runs["items"]]}
    if binding["project_id"]:
        project = quote(binding["project_id"], safe="")
        refs.update(cases="/api/case-definitions/" + project, knowledge="/api/knowledge/" + project)
    if workflow and not workflow.get("missing"):
        refs["workflow"] = {"workflow_id": workflow["workflow_id"], "cycle_id": binding["cycle_id"],
                            "read_url": "/api/workflows/" + quote(workflow["workflow_id"], safe="") + "/read",
                            "next_requires_current_cycle": bool(workflow.get("stale_cycle"))}
    return refs


def _empty(binding_page: dict, selected: dict) -> dict:
    return {"status": "selection_required", "binding": None, "selection": selected, "bindings": binding_page, "workflow": None,
            "runs": {"items": [], "page": {"total": 0, "returned": 0, "has_more": False, "next_cursor": None}},
            "pending": {}, "reports": [], "refs": {}, "cli_context": None}


def read(host: str, host_session_id: str, *, binding_id: str | None = None, limit: int = 20, cursor: str | None = None,
         runs_limit: int = 10, runs_cursor: str | None = None) -> dict:
    """Return bounded existing state; a selected reference never implies a lease or permission."""
    bindings.identity(host, host_session_id)
    with bindings.snapshot() as connection:
        page = bindings.binding_page(connection, host, host_session_id, limit=limit, cursor=cursor)
        selected = bindings.selection(connection, host, host_session_id)
        target = binding_id if binding_id is not None else selected["binding_id"]
        result = _empty(page, selected)
        if target is not None:
            value = bindings.binding(connection, host, host_session_id, target)
            workflow = _workflow(connection, value)
            runs, counts, scope = _runs(connection, value, limit=runs_limit, cursor=runs_cursor)
            reports = [_report("run_" + run["id"], run=run) for run in runs["items"]]
            if workflow:
                reports.insert(0, _report("workflow_" + paging.revision([value["workflow_id"], value["cycle_id"]])))
            status = "stale_cycle" if workflow and workflow.get("stale_cycle") else "bound"
            if (workflow and workflow.get("missing")) or (value["run_id"] and not runs["page"]["total"]):
                status = "reference_missing"
            result.update(status=status, binding=value, workflow=workflow, runs=runs, pending=_pending(connection, value, workflow, counts, scope),
                          reports=reports, refs=_references(value, workflow, runs),
                          cli_context={"session_id": value["task_session_id"], "host": host, "host_session_id": host_session_id,
                                       "workflow_id": value["workflow_id"], "cycle_id": value["cycle_id"], "workflow_binding": None})
        return {**result, "context_revision": paging.revision(result)}
