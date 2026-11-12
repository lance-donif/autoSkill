"""Scheduled runs. Rules: every N minutes, daily, weekly on chosen days, or once."""
from __future__ import annotations

import datetime
import json
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .. import db, projects, runner

UTC = datetime.timezone.utc
CLOCK = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
DAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _zone(rule: dict) -> ZoneInfo:
    try:
        return ZoneInfo(rule.get("timezone") or "Asia/Shanghai")
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValueError("时区不正确") from None


def next_due(rule: dict, now: datetime.datetime, previous: datetime.datetime | None = None) -> datetime.datetime:
    """The next time a rule fires after `now`. Missed occurrences are merged into the next one."""
    now = now.astimezone(UTC)
    kind = rule.get("kind")
    if kind == "interval":
        minutes = rule.get("minutes")
        if not isinstance(minutes, int) or isinstance(minutes, bool) or not 1 <= minutes <= 7 * 24 * 60:
            raise ValueError("间隔需要是 1 分钟到 7 天之间的整数分钟")
        step = datetime.timedelta(minutes=minutes)
        if previous:
            periods = max(1, int((now - previous) // step) + 1)
            return previous + step * periods
        return now + step
    if kind == "at":
        try:
            value = datetime.datetime.fromisoformat(str(rule.get("at", "")).replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("执行时间格式不正确") from None
        if value.tzinfo is None or value <= now:
            raise ValueError("一次性任务需要一个将来的时间")
        return value.astimezone(UTC)
    if kind in ("daily", "weekly"):
        zone = _zone(rule)
        clock = rule.get("time", "")
        if not isinstance(clock, str) or not CLOCK.fullmatch(clock):
            raise ValueError("时间需要写成 HH:mm")
        days = list(range(7))
        if kind == "weekly":
            days = rule.get("days")
            if not isinstance(days, list) or not days or any(d not in range(7) for d in days):
                raise ValueError("每周任务至少选择一天")
        hour, minute = map(int, clock.split(":"))
        today = now.astimezone(zone).date()
        for offset in range(9):
            day = today + datetime.timedelta(days=offset)
            if day.weekday() not in days:
                continue
            candidate = datetime.datetime.combine(day, datetime.time(hour, minute), zone)
            # skip wall-clock times that do not exist on that day (daylight-saving gaps)
            if candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != candidate.replace(tzinfo=None):
                continue
            if candidate > now:
                return candidate.astimezone(UTC)
        raise ValueError("无法计算下一次执行时间")
    raise ValueError("不支持的执行规则")


def describe(rule: dict) -> str:
    kind = rule.get("kind")
    if kind == "interval":
        minutes = rule["minutes"]
        return f"每 {minutes // 60} 小时" if minutes % 60 == 0 else f"每 {minutes} 分钟"
    if kind == "daily":
        return f"每天 {rule['time']}"
    if kind == "weekly":
        days = sorted(rule["days"])
        label = "工作日" if days == [0, 1, 2, 3, 4] else "、".join(DAY_NAMES[d] for d in days)
        return f"每周{'' if label == '工作日' else ' '}{label} {rule['time']}"
    return "仅一次"


def _stamp(value: datetime.datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds")


def _clean(spec: dict) -> dict:
    title = str(spec.get("title", "")).strip()
    if not title or len(title) > 60:
        raise ValueError("任务名称需要 1–60 个字")
    if spec.get("engine") not in runner.ENGINES:
        raise ValueError("回放引擎只能是 playwright 或 selenium")
    projects.path(spec["project_id"])
    case_ids = list(spec.get("case_ids") or [])
    known = {c["id"] for c in projects.cases(spec["project_id"])}
    missing = [c for c in case_ids if c not in known]
    if missing:
        raise ValueError("这些用例不存在或已归档：" + "、".join(missing))
    rule = spec.get("rule")
    if not isinstance(rule, dict):
        raise ValueError("缺少执行规则")
    return {"title": title, "engine": spec["engine"], "project_id": spec["project_id"], "case_ids": case_ids, "rule": rule}


def view(row: dict) -> dict:
    rule = json.loads(row["rule"])
    return {**row, "rule": rule, "case_ids": json.loads(row["case_ids"]), "enabled": bool(row["enabled"]),
            "rule_text": describe(rule)}


def create(spec: dict) -> dict:
    task = _clean(spec)
    stamp = db.now()
    task.update(id=db.new_id("task"), enabled=1, created_at=stamp, updated_at=stamp,
                next_due=_stamp(next_due(task["rule"], datetime.datetime.now(UTC))))
    db.insert("tasks", task)
    return get(task["id"])


def get(task_id: str) -> dict:
    row = db.one("SELECT t.*, COALESCE(p.name, t.project_id) project_name, u.status last_status, u.finished_at last_finished "
                 "FROM tasks t LEFT JOIN projects p ON p.id = t.project_id LEFT JOIN runs u ON u.id = t.last_run_id "
                 "WHERE t.id = ?", (task_id,))
    if not row:
        raise ValueError("定时任务不存在")
    return view(row)


def list_all(project_ids: list[str] | None = None) -> list[dict]:
    where, args = "1 = 1", []
    if project_ids:
        where, args = f"t.project_id IN ({','.join('?' * len(project_ids))})", list(project_ids)
    rows = db.rows(
        "SELECT t.*, COALESCE(p.name, t.project_id) project_name, u.status last_status, u.finished_at last_finished "
        "FROM tasks t LEFT JOIN projects p ON p.id = t.project_id LEFT JOIN runs u ON u.id = t.last_run_id "
        f"WHERE {where} ORDER BY t.enabled DESC, t.next_due IS NULL, t.next_due, t.created_at DESC", args)
    return [view(row) for row in rows]


def update(task_id: str, spec: dict) -> dict:
    current = get(task_id)
    merged = _clean({**{k: current[k] for k in ("title", "engine", "project_id", "case_ids", "rule")}, **spec})
    values = {**merged, "updated_at": db.now()}
    if current["enabled"]:
        values["next_due"] = _stamp(next_due(merged["rule"], datetime.datetime.now(UTC)))
    db.update("tasks", task_id, values)
    return get(task_id)


def set_enabled(task_id: str, enabled: bool) -> dict:
    current = get(task_id)
    values = {"enabled": int(enabled), "updated_at": db.now()}
    if enabled:  # resuming never fires missed occurrences; it schedules the next one
        values["next_due"] = _stamp(next_due(current["rule"], datetime.datetime.now(UTC)))
    else:
        values["next_due"] = None
    db.update("tasks", task_id, values)
    return get(task_id)


def delete(task_id: str) -> None:
    get(task_id)
    db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))


def fire(task: dict, trigger: str = "schedule") -> dict:
    """Queue a run for a task. A task whose project can no longer run leaves a Blocked run saying why."""
    try:
        run = runner.create(task["project_id"], task["case_ids"] or None, task["engine"], trigger=trigger, task_id=task["id"])
    except ValueError as error:
        stamp = db.now()
        run = {"id": db.new_id("run"), "project_id": task["project_id"], "engine": task["engine"], "trigger": trigger,
               "task_id": task["id"], "status": "Blocked", "reason": str(error), "case_ids": task["case_ids"],
               "created_at": stamp, "finished_at": stamp}
        db.insert("runs", run)
    db.update("tasks", task["id"], {"last_run_id": run["id"]})
    return run


def run_now(task_id: str) -> dict:
    return fire(get(task_id), trigger="manual")


def tick(now: datetime.datetime | None = None) -> list[dict]:
    """Claim every due task once and queue its run. Returns the runs that should start."""
    now = now or datetime.datetime.now(UTC)
    started = []
    for row in db.rows("SELECT * FROM tasks WHERE enabled = 1 AND next_due IS NOT NULL AND next_due <= ?", (_stamp(now),)):
        task = view(row)
        due = datetime.datetime.fromisoformat(task["next_due"])
        once = task["rule"].get("kind") == "at"
        following = None if once else _stamp(next_due(task["rule"], now, due))
        with db.connect() as conn:  # the claim: only one caller moves next_due forward
            claimed = conn.execute(
                "UPDATE tasks SET next_due = ?, enabled = ? WHERE id = ? AND next_due = ?",
                (following, int(not once), task["id"], task["next_due"])).rowcount
        if not claimed:
            continue
        busy = db.one("SELECT id FROM runs WHERE task_id = ? AND status IN ('Queued', 'Running')", (task["id"],))
        if busy:
            continue  # the previous run of this task is still going; skip this occurrence
        run = fire(task)
        if run["status"] == "Queued":
            started.append(run)
    return started
