"""Dashboard counts and drill-down lists share scope and the central state projection.

Definitions (shown next to each figure in the dashboard):
- cases      active cases as of the end of the range
- runs       runs started in the range, automatic logins excluded
- executions case results in the range; prerequisites run on a case's behalf are not counted
- pass rate  passed / (passed + failed); blocked results are shown separately, never in the denominator
"""
from __future__ import annotations

import contextlib
import datetime
import json
from dataclasses import dataclass, field

from .. import db, projects, task_state

COUNTED = "r.prerequisite = 0 AND u.trigger != 'login' AND u.family != 'load'"
RESULTS = "FROM results r JOIN runs u ON u.id = r.run_id LEFT JOIN projects p ON p.id = r.project_id"


def normalize(stamp: str | None) -> str | None:
    """Any ISO timestamp -> the UTC text format stored in the database."""
    if not stamp:
        return None
    try:
        value = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))  # noqa: FURB162  # Preserve the existing accepted timestamp syntax.
    except ValueError:
        raise ValueError("时间范围格式不正确") from None
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.UTC)
    return value.astimezone(datetime.UTC).isoformat(timespec="milliseconds")


@dataclass
class Scope:
    projects: list[str] = field(default_factory=list)  # empty = all projects
    start: str | None = None
    end: str | None = None
    cycle: str = "current"
    state_filters: dict[str, str] = field(default_factory=dict)
    _snapshot: dict | None = field(default=None, init=False, repr=False, compare=False)

    @classmethod
    def parse(cls, projects: str | None, start: str | None, end: str | None, *, cycle: str = "current", **filters: str | None) -> Scope:
        ids = [p for p in (projects or "").split(",") if p]
        if cycle not in ("current", "all"):
            raise ValueError("周期范围必须为 current 或 all")
        return cls(ids, normalize(start), normalize(end), cycle, {key: value for key, value in filters.items() if value})

    def where(self, time_column: str | None, project_column: str) -> tuple[str, list]:
        clauses, args = [], []
        if self.projects:
            clauses.append(f"{project_column} IN ({','.join('?' * len(self.projects))})")
            args += self.projects
        if time_column and self.start:
            clauses.append(f"{time_column} >= ?")
            args.append(self.start)
        if time_column and self.end:
            clauses.append(f"{time_column} < ?")
            args.append(self.end)
        return " AND ".join(clauses) or "1 = 1", args

    def as_of(self) -> str:
        return self.end or (self._snapshot["as_of"] if self._snapshot else db.now())


@contextlib.contextmanager
def snapshot(scope: Scope):
    """Reuse one read transaction and projections for exactly one public response."""
    if scope._snapshot is not None:
        yield scope._snapshot
        return
    with db.connect() as connection, task_state.projection():
        connection.execute("BEGIN")
        current = {"connection": connection, "as_of": db.now(), "selected": {}, "verdicts": {}, "children": {}, "parents": {}}
        scope._snapshot = current
        try:
            yield current
        finally:
            scope._snapshot = None


@contextlib.contextmanager
def _connection(scope: Scope):
    if scope._snapshot:
        yield scope._snapshot["connection"]
    else:
        with db.connect() as connection:
            yield connection


def _rows(scope: Scope, sql: str, args=()) -> list[dict]:
    with _connection(scope) as connection:
        return [dict(row) for row in connection.execute(sql, args)]


def _one(scope: Scope, sql: str, args=()) -> dict | None:
    rows = _rows(scope, sql, args)
    return rows[0] if rows else None


def _cycle(scope: Scope, alias: str) -> str:
    tables = _one(scope, "SELECT COUNT(*) n FROM sqlite_master WHERE type='table' AND name IN ('workflow_runtime','workflow_items')")["n"]
    if scope.cycle == "all" or tables != 2:
        return "1 = 1"
    return (f"(NOT EXISTS (SELECT 1 FROM workflow_items wi JOIN workflow_runtime wf ON wf.id=wi.workflow_id WHERE wi.run_id={alias}.id) "
            f"OR EXISTS (SELECT 1 FROM workflow_items wi JOIN workflow_runtime wf ON wf.id=wi.workflow_id WHERE wi.run_id={alias}.id AND wi.cycle_id=wf.cycle_id))")


def _matches(state: dict, filters: dict) -> bool:
    for key, value in filters.items():
        actual = state.get("checks", {}).get(key) if key in ("business", "performance", "quality") else state.get(key)
        if actual != value:
            return False
    return True


def _state_ids(scope: Scope, entity: str) -> list:
    return [row["id"] for row in _selected(scope, entity)]


def _where(scope: Scope, time_column: str, project_column: str, entity: str) -> tuple[str, list]:
    where, args = scope.where(time_column, project_column)
    where += " AND " + _cycle(scope, "u")
    if scope.state_filters:
        alias = "r" if entity == "result" else "u"
        where += f" AND {alias}.id IN (SELECT value FROM json_each(?))"
        args.append(json.dumps(_state_ids(scope, entity)))
    return where, args


def case_counts(scope: Scope) -> dict:
    if scope.state_filters:
        items = cases(scope)["items"]
        return {"total": len(items), "ui": sum(row["kind"] == "ui" for row in items), "api": sum(row["kind"] == "api" for row in items),
                "replayable": sum(bool(row["replayable"]) for row in items), "added": sum(not scope.start or row["created_at"] >= scope.start for row in items)}
    where, args = scope.where(None, "project_id")
    moment = scope.as_of()
    row = _one(scope,
        "SELECT COUNT(*) total, COALESCE(SUM(kind = 'ui'), 0) ui, COALESCE(SUM(kind = 'api'), 0) api, "
        "COALESCE(SUM(replayable), 0) replayable, COALESCE(SUM(created_at >= ?), 0) added "
        f"FROM cases WHERE {where} AND created_at < ? AND (archived_at IS NULL OR archived_at >= ?)",
        [scope.start or "", *args, moment, moment],
    )
    if not scope.start:
        row["added"] = row["total"]
    return row


def _latest(scope: Scope) -> str:
    return ("r.id = (SELECT MAX(x.id) FROM results x JOIN runs xu ON xu.id=x.run_id WHERE x.project_id=r.project_id "
            "AND x.case_id=r.case_id AND x.prerequisite=0 AND xu.trigger!='login' AND xu.family!='load' AND " + _cycle(scope, "xu") + ")")


def _result_verdict(scope: Scope, row: dict, parent=None) -> dict:
    cache = scope._snapshot["verdicts"]
    if row["id"] not in cache:
        if parent is None:
            parents = scope._snapshot["parents"]
            identity = row["run_id"]
            if identity not in parents:
                parents[identity] = _one(scope, "SELECT * FROM runs WHERE id=?", (identity,))
            parent = parents[identity]
        cache[row["id"]] = task_state.result_verdict(row, parent)
    return cache[row["id"]]


def _children(scope: Scope, row: dict, connection) -> list[dict]:
    cache = scope._snapshot["children"]
    if row["id"] not in cache:
        rows = connection.execute("SELECT * FROM results WHERE run_id=? AND prerequisite=0 ORDER BY id", (row["id"],))
        cache[row["id"]] = [_result_verdict(scope, dict(child), row) for child in rows]
    return cache[row["id"]]


def _verdict(scope: Scope, entity: str, row: dict) -> dict:
    connection = scope._snapshot["connection"]
    if entity == "result":
        return _result_verdict(scope, row)
    return task_state.run_verdict(row, _children(scope, row, connection))


def _history_item(scope: Scope, entity: str, row: dict) -> dict:
    connection = scope._snapshot["connection"]
    if entity == "run":
        return run_values(row, result_states=_children(scope, row, connection), connection=connection)
    state = task_state.result(row, connection=connection, verdict=_result_verdict(scope, row))
    return {**{key: value for key, value in row.items() if key != "detail"}, "state": state}


def _prefilter(scope: Scope, entity: str, status: str | None, where: str, args: list) -> tuple[str, list]:
    if entity != "run":
        return where, args
    if status == "active":
        where += " AND u.status IN ('Queued','Running')"
    elif status == "Running":
        where += " AND u.status = 'Running'"
    phases = {"queued": ("Queued",), "running": ("Running",), "blocked": ("Blocked",),
              "cancelled": ("Cancelled",), "settled": ("Pass", "Fail")}
    if phase := phases.get(scope.state_filters.get("phase")):
        where += " AND u.status IN (" + ",".join("?" for _ in phase) + ")"
        args.extend(phase)
    return where, args


def _candidates(scope: Scope, entity: str, status, trigger, current_only, run_time, exclude_load):
    result = entity == "result"
    clock = "u.created_at" if not result or run_time else "r.started_at"
    where, args = scope.where(clock, "r.project_id" if result else "u.project_id")
    where += " AND " + _cycle(scope, "u") + " AND " + (COUNTED if result else "u.trigger!='login'")
    if exclude_load and not result:
        where += " AND u.family!='load'"
    if trigger:
        where += " AND u.trigger=?"
        args.append(trigger)
    if current_only:
        where += " AND " + _latest(scope)
    where, args = _prefilter(scope, entity, status, where, args)
    source = RESULTS if result else "FROM runs u LEFT JOIN projects p ON p.id=u.project_id"
    alias = "r" if result else "u"
    order = "r.started_at DESC,r.id DESC" if result else "u.created_at DESC,u.id DESC"
    return alias, f"{source} WHERE {where}", args, order


def _selected(scope: Scope, entity: str, *, status: str | None = None, trigger: str | None = None,
              current_only: bool = False, run_time: bool = False, exclude_load: bool = False) -> list[dict]:
    if scope._snapshot is None:
        with snapshot(scope):
            return _selected(scope, entity, status=status, trigger=trigger, current_only=current_only, run_time=run_time, exclude_load=exclude_load)
    cache = scope._snapshot["selected"]
    key = (entity, status, trigger, current_only, run_time, exclude_load)
    if key in cache:
        return cache[key]
    alias, source, args, order = _candidates(scope, entity, status, trigger, current_only, run_time, exclude_load)
    rows = scope._snapshot["connection"].execute(f"SELECT {alias}.*,COALESCE(p.name,{alias}.project_id) project_name {source} ORDER BY {order}", args)
    items = []
    needs_delivery = bool(scope.state_filters.keys() & {"report", "delivery"})
    for saved in rows:
        row = dict(saved)
        state = _history_item(scope, entity, row)["state"] if needs_delivery else _verdict(scope, entity, row)
        if status == "active":
            matched = state["lifecycle"] in ("pending", "active")
        elif status == "Running" and entity == "run":
            matched = state["phase"] == "running"
        else:
            matched = not status or state["outcome"] == status.lower()
        if matched and _matches(state, scope.state_filters):
            items.append({**row, "state": state})
    cache[key] = items
    return items


def _counts(items: list[dict]) -> dict:
    return task_state.counts([item["state"] for item in items])


def run_values(row: dict, *, results=None, result_states=None, connection=None) -> dict:
    """Expose target-case counts using the same current outcomes as result drill-downs."""
    return task_state.run_values(row, results=results, result_states=result_states, connection=connection)


def result_counts(scope: Scope) -> dict:
    rows = _selected(scope, "result")
    return {"executions": len(rows), **_counts(rows),
            "still_failing": sum(row["state"]["outcome"] == "fail" for row in _selected(scope, "result", current_only=True)),
            "reliability": reliability(scope)}


def reliability(scope: Scope) -> dict:
    """First and final business checks share a denominator including unfinished target executions."""
    where, args = _where(scope, "u.created_at", "u.project_id", "run")
    planned = _one(scope, "SELECT COALESCE(SUM(MAX(u.total, u.done)), 0) n, "
                     "COALESCE(SUM(CASE WHEN u.status = 'Cancelled' THEN MAX(u.total - u.done, 0) ELSE 0 END), 0) cancelled "
                     ", COALESCE(SUM(u.family = 'native' AND u.total = 0), 0) unknown_runs "
                     f"FROM runs u WHERE u.trigger != 'login' AND u.family != 'load' AND {where}", args)
    first = "COALESCE(json_extract(r.detail, '$.attempts[0].business_status'), json_extract(r.detail, '$.business_status'), 'Unverified')"
    final = "COALESCE(json_extract(r.detail, '$.attempts[#-1].business_status'), json_extract(r.detail, '$.business_status'), 'Unverified')"
    row = _one(scope, "SELECT COUNT(*) observed, "
                 f"COALESCE(SUM({first} = 'Pass'), 0) first_passed, COALESCE(SUM({final} = 'Pass'), 0) final_passed, "
                 f"COALESCE(SUM({first} = 'Fail' AND {final} = 'Pass'), 0) recovered_after_failure, "
                 f"COALESCE(SUM({final} = 'Unverified'), 0) unverified, "
                 "COALESCE(SUM(r.status = 'Blocked'), 0) blocked, COALESCE(SUM(r.status = 'Cancelled'), 0) cancelled "
                 f"{RESULTS} WHERE {COUNTED} AND {where}", args)
    denominator = max(planned["n"], row["observed"])
    row.update(denominator=denominator, unexecuted=max(denominator - row["observed"], 0),
               cancelled=row["cancelled"] + planned["cancelled"],
               unknown_target_runs=planned["unknown_runs"],
               first_pass_rate=round(row["first_passed"] / denominator, 4) if denominator and not planned["unknown_runs"] else None,
               final_pass_rate=round(row["final_passed"] / denominator, 4) if denominator and not planned["unknown_runs"] else None,
               window="run_created_at", basis="historical_attempts",
               denominator_definition="本范围创建的运行内全部目标用例，含阻塞、取消、未完成和无业务断言",
               verification_note="首轮与末次业务结果为历史尝试事实；当前有效通过数使用独立核验后的 state.outcome")
    return row


def run_counts(scope: Scope) -> dict:
    rows = _selected(scope, "run", exclude_load=True)
    return {"total": len(rows), "scheduled": sum(row["trigger"] == "schedule" for row in rows),
            "active": sum(row["state"]["lifecycle"] in ("pending", "active") for row in rows), **_counts(rows)}


def trend(scope: Scope, tz_minutes: int = 480) -> dict:
    """Bucket the same independently verified outcomes shown in the result list."""
    hourly = bool(scope.start and datetime.datetime.fromisoformat(scope.as_of()) - datetime.datetime.fromisoformat(scope.start) <= datetime.timedelta(hours=36))
    pattern = "%Y-%m-%d %H:00" if hourly else "%Y-%m-%d"
    groups = {}
    for row in _selected(scope, "result"):
        stamp = datetime.datetime.fromisoformat(row["started_at"]) + datetime.timedelta(minutes=int(tz_minutes))
        groups.setdefault(stamp.strftime(pattern), []).append(row)
    return {"unit": "hour" if hourly else "day", "points": [{"bucket": key, **_counts(rows)} for key, rows in sorted(groups.items())]}


def task_status(scope: Scope) -> dict:
    if scope._snapshot is None:
        with snapshot(scope):
            return task_status(scope)
    where, args = scope.where(None, "t.project_id")
    tasks = _rows(scope,
        "SELECT t.id, t.title, t.project_id, COALESCE(p.name, t.project_id) project_name, t.enabled, t.next_due, "
        "t.engine, t.last_run_id, u.status last_status, u.finished_at last_finished "
        f"FROM tasks t LEFT JOIN projects p ON p.id = t.project_id LEFT JOIN runs u ON u.id = t.last_run_id WHERE {where} "
        "ORDER BY t.enabled DESC, t.next_due IS NULL, t.next_due", args)
    run_where, run_args = _where(scope, "u.created_at", "u.project_id", "run")
    fired = _one(scope, f"SELECT COUNT(*) n FROM runs u WHERE trigger = 'schedule' AND {run_where}", run_args)["n"]
    enabled = sum(1 for t in tasks if t["enabled"])
    for task in tasks[:5]:
        run = _one(scope, "SELECT * FROM runs WHERE id=?", (task["last_run_id"],)) if task["last_run_id"] else None
        task["last_run_state"] = _history_item(scope, "run", run)["state"] if run else None
    upcoming = [t for t in tasks if t["enabled"] and t["next_due"]]
    return {"total": len(tasks), "enabled": enabled, "paused": len(tasks) - enabled, "fired_in_range": fired,
            "next": upcoming[0] if upcoming else None, "tasks": tasks[:5]}


def summary(scope: Scope, tz_minutes: int = 480) -> dict:
    with snapshot(scope):
        return {
            "scope": {"projects": scope.projects, "start": scope.start, "end": scope.end, "cycle": scope.cycle, "filters": scope.state_filters},
            "cases": case_counts(scope), "runs": run_counts(scope), "results": result_counts(scope),
            "trend": trend(scope, tz_minutes), "tasks": task_status(scope),
        }


def dashboard(scope: Scope, tz_minutes: int = 480, *, current_only: bool = False, limit: int = 6) -> dict:
    """Read dashboard figures and lists from one snapshot and one evidence projection."""
    with snapshot(scope):
        totals = summary(scope, tz_minutes)
        recent = runs(scope, limit=limit, exclude_load=True)
        outcomes = ("fail",) if current_only else ("fail", "blocked")
        rows = [row for row in _selected(scope, "result", current_only=current_only) if row["state"]["outcome"] in outcomes]
        items = [_history_item(scope, "result", row) for row in rows[:limit]]
        return {"summary": totals, "recent": recent, "failures": {"total": len(rows), "items": items}}


def _history_page(scope: Scope, entity: str, status, limit, offset, *, trigger=None, current_only=False, exclude_load=False):
    with snapshot(scope) as snap:
        key = (entity, status, trigger, current_only, False, exclude_load)
        sql_status = not status or (entity == "run" and status in ("active", "Running"))
        if scope.state_filters or not sql_status or key in snap["selected"]:
            selected = _selected(scope, entity, status=status, trigger=trigger, current_only=current_only, exclude_load=exclude_load)
            rows = selected[offset:offset + limit]
            total, counts = len(selected), _counts(selected)["state_counts"]
        else:
            alias, source, args, order = _candidates(scope, entity, status, trigger, current_only, False, exclude_load)
            totals = _one(scope, f"SELECT COUNT(*) total, COUNT(*) FILTER (WHERE {alias}.status='Fail') failed {source}", args)
            total = totals["total"]
            rows = _rows(scope, f"SELECT {alias}.*,COALESCE(p.name,{alias}.project_id) project_name {source} ORDER BY {order} LIMIT ? OFFSET ?",
                         [*args, limit, offset])
            # A recorded failure always fails. Every other outcome still needs the shared verdict.
            counts = _counts([])["state_counts"]
            counts["fail"] = totals["failed"]
            if total > counts["fail"]:
                if len(rows) == total:
                    remaining = (row for row in rows if row["status"] != "Fail")
                else:
                    candidates = snap["connection"].execute(f"SELECT {alias}.* {source} AND {alias}.status!='Fail'", args)
                    remaining = map(dict, candidates)
                for row in remaining:
                    counts[_verdict(scope, entity, row)["outcome"]] += 1
        return {"items": [_history_item(scope, entity, row) for row in rows], "total": total, "state_counts": counts}


def runs(scope: Scope, trigger: str | None = None, status: str | None = None, limit: int = 50, offset: int = 0, exclude_load: bool = False) -> dict:
    return _history_page(scope, "run", status, int(limit), int(offset), trigger=trigger, exclude_load=exclude_load)


def results(scope: Scope, status: str | None = None, current_only: bool = False, limit: int = 50, offset: int = 0) -> dict:
    return _history_page(scope, "result", status, int(limit), int(offset), current_only=current_only)


def cases(scope: Scope, kind: str | None = None, added_only: bool = False) -> dict:
    """Active cases as of the end of the range, each with its latest counted result."""
    if scope._snapshot is None:
        with snapshot(scope):
            return cases(scope, kind, added_only)
    where, args = scope.where(None, "c.project_id")
    moment = scope.as_of()
    where += " AND c.created_at < ? AND (c.archived_at IS NULL OR c.archived_at >= ?)"
    args += [moment, moment]
    if kind:
        where, args = where + " AND c.kind = ?", [*args, kind]
    if added_only and scope.start:
        where, args = where + " AND c.created_at >= ?", [*args, scope.start]
    items = _rows(scope,
        "SELECT c.project_id, COALESCE(p.name, c.project_id) project_name, c.case_id, c.kind, c.title, c.module, c.priority, "
        "c.category, c.replayable, c.depends_on, c.created_at "
        f"FROM cases c LEFT JOIN projects p ON p.id = c.project_id WHERE {where} ORDER BY c.project_id, c.kind DESC, c.case_id", args)
    cycle_where = _cycle(scope, "u")
    with _connection(scope) as connection:
        for item in items:
            latest = connection.execute("SELECT r.* FROM results r JOIN runs u ON u.id=r.run_id WHERE r.project_id=? AND r.case_id=? "
                                        "AND r.prerequisite=0 AND u.trigger!='login' AND u.family!='load' AND " + cycle_where + " ORDER BY r.id DESC LIMIT 1",
                                        (item["project_id"], item["case_id"])).fetchone()
            if latest:
                try:
                    definition = projects.case(item["project_id"], item["case_id"])
                except (OSError, ValueError):
                    definition = None
                state = task_state.current_result(item["project_id"], definition, dict(latest), connection=connection)
            else:
                state = None
            current = bool(state and state["current"])
            item.update(state=state, last_state=state, last_status=latest["status"] if latest else None,
                        last_run_at=latest["started_at"] if latest else None, current=current,
                        current_status=state["outcome"].capitalize() if current else None)
    if scope.state_filters:
        items = [item for item in items if item["state"] and _matches(item["state"], scope.state_filters)]
    return {"total": len(items), "items": items}
