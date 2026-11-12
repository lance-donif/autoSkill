"""Dashboard numbers. Cards, charts and lists are all computed in SQL from one scope, so they always agree.

Definitions (shown next to each figure in the dashboard):
- cases      active cases as of the end of the range
- runs       runs started in the range, automatic logins excluded
- executions case results in the range; prerequisites run on a case's behalf are not counted
- pass rate  passed / (passed + failed); blocked results are shown separately, never in the denominator
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field

from .. import db

COUNTED = "r.prerequisite = 0 AND u.trigger != 'login'"
RESULTS = "FROM results r JOIN runs u ON u.id = r.run_id LEFT JOIN projects p ON p.id = r.project_id"
LATEST = ("r.id = (SELECT MAX(x.id) FROM results x JOIN runs xu ON xu.id = x.run_id WHERE x.project_id = r.project_id "
          "AND x.case_id = r.case_id AND x.prerequisite = 0 AND xu.trigger != 'login')")


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

    @classmethod
    def parse(cls, projects: str | None, start: str | None, end: str | None) -> Scope:
        ids = [p for p in (projects or "").split(",") if p]
        return cls(ids, normalize(start), normalize(end))

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
        return self.end or db.now()


def case_counts(scope: Scope) -> dict:
    where, args = scope.where(None, "project_id")
    moment = scope.as_of()
    row = db.one(
        "SELECT COUNT(*) total, COALESCE(SUM(kind = 'ui'), 0) ui, COALESCE(SUM(kind = 'api'), 0) api, "
        "COALESCE(SUM(replayable), 0) replayable, COALESCE(SUM(created_at >= ?), 0) added "
        f"FROM cases WHERE {where} AND created_at < ? AND (archived_at IS NULL OR archived_at >= ?)",
        [scope.start or "", *args, moment, moment],
    )
    if not scope.start:
        row["added"] = row["total"]
    return row


def result_counts(scope: Scope) -> dict:
    where, args = scope.where("r.started_at", "r.project_id")
    row = db.one(
        "SELECT COUNT(*) executions, COALESCE(SUM(r.status = 'Pass'), 0) passed, COALESCE(SUM(r.status = 'Fail'), 0) failed, "
        f"COALESCE(SUM(r.status = 'Blocked'), 0) blocked {RESULTS} WHERE {COUNTED} AND {where}", args)
    judged = row["passed"] + row["failed"]
    row["judged"] = judged
    row["pass_rate"] = round(row["passed"] / judged, 4) if judged else None
    still = db.one(f"SELECT COUNT(*) n {RESULTS} WHERE {COUNTED} AND {where} AND r.status = 'Fail' AND {LATEST}", args)
    row["still_failing"] = still["n"]
    return row


def run_counts(scope: Scope) -> dict:
    where, args = scope.where("created_at", "project_id")
    return db.one(
        "SELECT COUNT(*) total, COALESCE(SUM(trigger = 'schedule'), 0) scheduled, "
        "COALESCE(SUM(status IN ('Queued', 'Running')), 0) active "
        f"FROM runs WHERE trigger != 'login' AND {where}", args)


def trend(scope: Scope, tz_minutes: int = 480) -> dict:
    """Stacked pass/fail/blocked per day (per hour when the range is one day or less), in the viewer's time zone."""
    where, args = scope.where("r.started_at", "r.project_id")
    hourly = False
    if scope.start:
        begin = datetime.datetime.fromisoformat(scope.start)
        finish = datetime.datetime.fromisoformat(scope.as_of())
        hourly = (finish - begin) <= datetime.timedelta(hours=36)
    pattern = "%Y-%m-%d %H:00" if hourly else "%Y-%m-%d"
    rows = db.rows(
        f"SELECT strftime('{pattern}', r.started_at, ?) bucket, COALESCE(SUM(r.status = 'Pass'), 0) passed, "
        "COALESCE(SUM(r.status = 'Fail'), 0) failed, COALESCE(SUM(r.status = 'Blocked'), 0) blocked "
        f"{RESULTS} WHERE {COUNTED} AND {where} GROUP BY bucket ORDER BY bucket",
        [f"{int(tz_minutes):+d} minutes", *args],
    )
    for row in rows:
        judged = row["passed"] + row["failed"]
        row["pass_rate"] = round(row["passed"] / judged, 4) if judged else None
    return {"unit": "hour" if hourly else "day", "points": rows}


def task_status(scope: Scope) -> dict:
    where, args = scope.where(None, "t.project_id")
    tasks = db.rows(
        "SELECT t.id, t.title, t.project_id, COALESCE(p.name, t.project_id) project_name, t.enabled, t.next_due, "
        "t.engine, u.status last_status, u.finished_at last_finished "
        f"FROM tasks t LEFT JOIN projects p ON p.id = t.project_id LEFT JOIN runs u ON u.id = t.last_run_id WHERE {where} "
        "ORDER BY t.enabled DESC, t.next_due IS NULL, t.next_due", args)
    run_where, run_args = scope.where("created_at", "project_id")
    fired = db.one(f"SELECT COUNT(*) n FROM runs WHERE trigger = 'schedule' AND {run_where}", run_args)["n"]
    enabled = sum(1 for t in tasks if t["enabled"])
    upcoming = [t for t in tasks if t["enabled"] and t["next_due"]]
    return {"total": len(tasks), "enabled": enabled, "paused": len(tasks) - enabled, "fired_in_range": fired,
            "next": upcoming[0] if upcoming else None, "tasks": tasks[:5]}


def summary(scope: Scope, tz_minutes: int = 480) -> dict:
    return {
        "scope": {"projects": scope.projects, "start": scope.start, "end": scope.end},
        "cases": case_counts(scope), "runs": run_counts(scope), "results": result_counts(scope),
        "trend": trend(scope, tz_minutes), "tasks": task_status(scope),
    }


def runs(scope: Scope, trigger: str | None = None, status: str | None = None, limit: int = 50, offset: int = 0) -> dict:
    where, args = scope.where("u.created_at", "u.project_id")
    if trigger:
        where, args = where + " AND u.trigger = ?", [*args, trigger]
    if status == "active":
        where += " AND u.status IN ('Queued', 'Running')"
    elif status:
        where, args = where + " AND u.status = ?", [*args, status]
    base = f"FROM runs u LEFT JOIN projects p ON p.id = u.project_id WHERE u.trigger != 'login' AND {where}"
    total = db.one(f"SELECT COUNT(*) n {base}", args)["n"]
    items = db.rows(
        "SELECT u.id, u.batch_id, u.project_id, COALESCE(p.name, u.project_id) project_name, u.engine, u.trigger, u.status, "
        "u.total, u.done, u.passed, u.failed, u.blocked, u.current, u.reason, u.created_at, u.started_at, u.finished_at "
        f"{base} ORDER BY u.created_at DESC LIMIT ? OFFSET ?", [*args, int(limit), int(offset)])
    return {"total": total, "items": items}


def results(scope: Scope, status: str | None = None, current_only: bool = False, limit: int = 50, offset: int = 0) -> dict:
    """Case results in scope: the drill-down behind the executions, pass-rate, failed and blocked figures."""
    where, args = scope.where("r.started_at", "r.project_id")
    if status:
        where, args = where + " AND r.status = ?", [*args, status]
    if current_only:
        where += f" AND {LATEST}"
    base = f"{RESULTS} WHERE {COUNTED} AND {where}"
    total = db.one(f"SELECT COUNT(*) n {base}", args)["n"]
    items = db.rows(
        "SELECT r.id, r.run_id, r.project_id, COALESCE(p.name, r.project_id) project_name, r.case_id, r.case_title, "
        "r.kind, r.engine, r.status, r.category, r.reason, r.actual, r.duration_ms, r.started_at "
        f"{base} ORDER BY r.started_at DESC, r.id DESC LIMIT ? OFFSET ?", [*args, int(limit), int(offset)])
    return {"total": total, "items": items}


def cases(scope: Scope, kind: str | None = None, added_only: bool = False) -> dict:
    """Active cases as of the end of the range, each with its latest counted result."""
    where, args = scope.where(None, "c.project_id")
    moment = scope.as_of()
    where += " AND c.created_at < ? AND (c.archived_at IS NULL OR c.archived_at >= ?)"
    args += [moment, moment]
    if kind:
        where, args = where + " AND c.kind = ?", [*args, kind]
    if added_only and scope.start:
        where, args = where + " AND c.created_at >= ?", [*args, scope.start]
    items = db.rows(
        "SELECT c.project_id, COALESCE(p.name, c.project_id) project_name, c.case_id, c.kind, c.title, c.module, c.priority, "
        "c.category, c.replayable, c.depends_on, c.created_at, "
        "(SELECT r.status FROM results r JOIN runs u ON u.id = r.run_id WHERE r.project_id = c.project_id AND r.case_id = c.case_id "
        " AND r.prerequisite = 0 AND u.trigger != 'login' ORDER BY r.id DESC LIMIT 1) last_status, "
        "(SELECT r.started_at FROM results r JOIN runs u ON u.id = r.run_id WHERE r.project_id = c.project_id AND r.case_id = c.case_id "
        " AND r.prerequisite = 0 AND u.trigger != 'login' ORDER BY r.id DESC LIMIT 1) last_run_at "
        f"FROM cases c LEFT JOIN projects p ON p.id = c.project_id WHERE {where} ORDER BY c.project_id, c.kind DESC, c.case_id", args)
    return {"total": len(items), "items": items}
