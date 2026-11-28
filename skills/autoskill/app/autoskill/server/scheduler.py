"""Scheduled runs. Rules: every N minutes, daily, weekly on chosen days, or once."""
from __future__ import annotations

import datetime
import hashlib
import json
import re
from collections import defaultdict
from itertools import batched
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .. import data_refs, db, paths, platform_accounts, projects, runner, task_context, task_state
from ..locking import named_lock
from .deliveries import ack_delivery, claim_delivery, pending_deliveries, release_delivery, resolve_delivery  # noqa: F401  # Public scheduler delivery API.

UTC = datetime.UTC
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
        if not isinstance(minutes, int) or isinstance(minutes, bool) or minutes < 1:
            raise ValueError("间隔需要是正整数分钟")
        step = datetime.timedelta(minutes=minutes)
        if previous:
            periods = max(1, int((now - previous) // step) + 1)
            return previous + step * periods
        return now + step
    if kind == "at":
        try:
            value = datetime.datetime.fromisoformat(str(rule.get("at", "")).replace("Z", "+00:00"))  # noqa: FURB162  # Preserve the existing accepted timestamp syntax.
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
        # A weekly wall time may not exist next week. Include the following week's occurrence.
        for offset in range(2 * 7 + 1):
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


def _plan_path(spec: dict) -> str:
    if spec.get("plan_path"):
        return runner.plan_reference(runner.plan_folder(spec["plan_path"]))
    with task_context.use_session(spec.get("session_id", task_context.session_id()) or None):
        return runner.plan_reference(paths.case_skill())


def _headed(spec: dict) -> bool:
    """Explicit task choices survive worker restarts; legacy tasks follow the saved dashboard choice."""
    if spec.get("headed") is not None:
        return bool(spec["headed"])
    value = paths.settings().get("dashboard", {}).get("headed")
    return value if type(value) is bool else paths.headed()


def _resolve_task_context(project_id: str, spec: dict, *, previous: dict | None = None) -> tuple[str, str]:
    """Bind a task's environment and account explicitly, defaulting to the project's default item."""
    cfg = projects.config(project_id)
    raw_env = spec.get("environment_id")
    if "environment_id" not in spec or raw_env is None:
        if previous is not None:
            env_id = str(previous.get("environment_id") or "")
        else:
            default_env = projects.environment_of(cfg, None, use_active=False)
            env_id = str((default_env or {}).get("id") or "")
    else:
        if not isinstance(raw_env, str):
            raise ValueError("environment_id 必须是文本")
        env_id = raw_env.strip()
    env = projects.environment_of(cfg, env_id, use_active=False) if env_id else None

    env_changed = previous is not None and env_id != str(previous.get("environment_id") or "")
    raw_account = spec.get("account_id")
    if "account_id" not in spec or raw_account is None:
        if previous is not None and not env_changed:
            acc_id = str(previous.get("account_id") or "")
        elif env and env.get("login_required"):
            acc_id = str(env.get("default_account_id") or ((env.get("account_ids") or [""])[0]))
        else:
            acc_id = ""
    else:
        if not isinstance(raw_account, str):
            raise ValueError("account_id 必须是文本")
        acc_id = raw_account.strip()
    if acc_id and platform_accounts.get_account(acc_id, include_password=False) is None:
        raise ValueError(f"账号 {acc_id} 不存在")
    return env_id, acc_id


def _clean(spec: dict, *, previous: dict | None = None) -> dict:
    title = str(spec.get("title", "")).strip()
    if not title:
        raise ValueError("任务名称不能为空")
    kind = spec.get("kind", "replay")
    if kind not in ("replay", "native", "maintenance"):
        raise ValueError("任务类型需要是 replay、native 或 maintenance")
    engine = spec.get("engine") or ("playwright" if kind == "replay" else "native")
    if kind == "replay" and engine not in runner.ENGINES:
        raise ValueError("回放引擎只能是 playwright 或 selenium")
    projects.path(spec["project_id"])
    case_ids = list(spec.get("case_ids") or [])
    if kind == "replay":
        known = {c["id"] for c in projects.cases(spec["project_id"])}
        missing = [c for c in case_ids if c not in known]
        if missing:
            raise ValueError("这些用例不存在或已归档：" + "、".join(missing))
    if kind == "native" and not spec.get("asset_id"):
        raise ValueError("原生任务需要 asset_id")
    if kind == "maintenance" and not spec.get("workflow_id"):
        raise ValueError("AI 维护任务需要 workflow_id")
    rule = spec.get("rule")
    if not isinstance(rule, dict):
        raise ValueError("缺少执行规则")
    parameters = spec.get("parameters", {})
    if not isinstance(parameters, dict):
        raise ValueError("任务参数需要是对象")
    overlap = spec.get("overlap", "skip")
    if overlap not in ("skip", "queue"):
        raise ValueError("重叠执行策略需要是 skip 或 queue")
    headed = spec.get("headed")
    if headed is not None and type(headed) is not bool:
        raise ValueError("显示浏览器窗口必须是布尔值")
    environment_id, account_id = _resolve_task_context(spec["project_id"], spec, previous=previous)
    return {"title": title, "engine": engine, "project_id": spec["project_id"], "case_ids": case_ids, "rule": rule,
            "kind": kind, "asset_id": spec.get("asset_id", ""), "parameters": parameters,
            "workflow_id": spec.get("workflow_id", ""), "host": spec.get("host") or "generic", "host_session_id": spec.get("host_session_id") or "", "overlap": overlap,
            "session_id": spec.get("session_id", task_context.session_id()) or "", "plan_path": _plan_path(spec),
            "headed": None if headed is None else int(headed),
            "environment_id": environment_id, "account_id": account_id}


def _task_context_labels(project_id: str, environment_id: str, account_id: str) -> tuple[str, str]:
    env_name = ""
    if environment_id:
        try:
            env = projects.environment_of(projects.config(project_id), environment_id, use_active=False)
            env_name = str((env or {}).get("name") or "")
        except ValueError:
            env_name = ""
    acc_label = ""
    if account_id:
        acc = platform_accounts.get_account(account_id, include_password=False)
        if acc:
            acc_label = str(acc.get("label") or acc.get("username") or account_id)
    return env_name, acc_label


def view(row: dict, *, connection=None, include_state: bool = True, runs=None, results=None) -> dict:
    rule = json.loads(row["rule"])
    row = runner.public_run(row)
    last_state = None
    if include_state and row.get("last_run_id"):
        if runs is not None:
            run = runs.get(row["last_run_id"])
        elif connection is None:
            run = db.one("SELECT * FROM runs WHERE id=?", (row["last_run_id"],))
        else:
            saved = connection.execute("SELECT * FROM runs WHERE id=?", (row["last_run_id"],)).fetchone()
            run = dict(saved) if saved else None
        last_state = task_state.run(run, connection=connection, results=results.get(run["id"], []) if results is not None else None) if run else None
    env_id = str(row.get("environment_id") or "")
    acc_id = str(row.get("account_id") or "")
    env_name, acc_label = _task_context_labels(row["project_id"], env_id, acc_id)
    return {**row, "rule": rule, "case_ids": json.loads(row["case_ids"]), "enabled": bool(row["enabled"]),
            "headed": bool(row["headed"]) if row.get("headed") is not None else None, "effective_headed": _headed(row),
            "parameters": json.loads(row.get("parameters", "{}")), "rule_text": describe(rule), "last_run_state": last_state,
            "environment_id": env_id, "environment_name": env_name, "account_id": acc_id, "account_label": acc_label}


def _packed(value):
    return json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value


def _insert(conn, table: str, values: dict) -> None:
    conn.execute(f"INSERT INTO {table} ({', '.join(values)}) VALUES ({','.join('?' for _ in values)})", [_packed(v) for v in values.values()])


def _payload_hash(payload: dict) -> str:
    value = {k: v for k, v in payload.items() if k != "idempotency_key"}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _receipt(conn, scope: str, key: str, digest: str) -> dict | None:
    if not key:
        return None
    row = conn.execute("SELECT * FROM schedule_requests WHERE scope = ? AND request_key = ?", (scope, key)).fetchone()
    if row and row["payload_hash"] != digest:
        raise ValueError("相同幂等键已用于不同的任务请求")
    return runner.public_run(json.loads(row["response"])) if row else None


def _remember(conn, scope: str, key: str, digest: str, response: dict) -> None:
    if key:
        response = dict(response)
        if response.get("plan_path"):
            response["plan_path"] = runner.plan_reference(response["plan_path"])
        if response.get("input_manifest"):
            response["input_manifest"] = data_refs.store(response["input_manifest"])
        _insert(conn, "schedule_requests", {"scope": scope, "request_key": key, "payload_hash": digest,
                                          "response": response, "created_at": db.now()})


def create(spec: dict) -> dict:
    db.init()
    key, digest = str(spec.get("idempotency_key") or ""), _payload_hash(spec)
    with db.connect() as conn:
        receipt = _receipt(conn, "create", key, digest)
    if receipt:
        return receipt
    task = _clean(spec)
    stamp = db.now()
    task.update(id=db.new_id("task"), enabled=1, revision=1, created_at=stamp, updated_at=stamp,
                next_due=_stamp(next_due(task["rule"], datetime.datetime.now(UTC))))
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        receipt = _receipt(conn, "create", key, digest)
        if receipt:
            return receipt
        _insert(conn, "tasks", task)
        result = view(dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (task["id"],)).fetchone()), connection=conn)
        _remember(conn, "create", key, digest, result)
    return result


def get(task_id: str) -> dict:
    row = db.one("SELECT t.*, COALESCE(p.name, t.project_id) project_name, u.status last_status, u.finished_at last_finished "
                 "FROM tasks t LEFT JOIN projects p ON p.id = t.project_id LEFT JOIN runs u ON u.id = t.last_run_id "
                 "WHERE t.id = ?", (task_id,))
    if not row:
        raise ValueError("定时任务不存在")
    return view(row)


def list_all(project_ids: list[str] | None = None) -> list[dict]:
    return page(project_ids, all_items=True)["items"]


def _page_views(connection, rows):
    for group in batched(rows, 64):
        ids = sorted({row["last_run_id"] for row in group if row["last_run_id"]})
        runs, results = {}, defaultdict(list)
        if ids:
            marks = ",".join("?" for _ in ids)
            runs = {run["id"]: dict(run) for run in connection.execute(f"SELECT * FROM runs WHERE id IN ({marks})", ids)}
            for result in connection.execute(f"SELECT * FROM results WHERE run_id IN ({marks}) AND prerequisite=0 ORDER BY id", ids):
                results[result["run_id"]].append(dict(result))
        with task_state.projection():
            for row in group:
                yield view(dict(row), connection=connection, runs=runs, results=results)


def page(project_ids: list[str] | None = None, offset: int = 0, limit: int = 50, all_items: bool = False) -> dict:
    if offset < 0 or limit < 1:
        raise ValueError("分页起点不能为负数，每页数量需要大于零")
    where, args = "1 = 1", []
    if project_ids:
        where, args = f"t.project_id IN ({','.join('?' * len(project_ids))})", list(project_ids)
    query = "FROM tasks t LEFT JOIN projects p ON p.id = t.project_id LEFT JOIN runs u ON u.id = t.last_run_id "
    query += f"WHERE {where}"
    with db.connect() as conn:
        conn.execute("BEGIN")
        total = conn.execute("SELECT COUNT(*) " + query, args).fetchone()[0]
        paging = " LIMIT ? OFFSET ?" if not all_items else " LIMIT -1 OFFSET ?"
        bindings = [*args, limit, offset] if not all_items else [*args, offset]
        rows = conn.execute("SELECT t.*, COALESCE(p.name, t.project_id) project_name, u.status last_status, u.finished_at last_finished "
                            + query + " ORDER BY t.enabled DESC, t.next_due IS NULL, t.next_due, t.created_at DESC" + paging, bindings)
        items = list(_page_views(conn, rows))
    more = offset + len(items) < total
    return {"items": items, "total": total, "has_more": more, "next_offset": offset + len(items) if more else None}


def _update_definition(task_id: str, spec: dict, values: dict, expected: int) -> dict:
    key, digest = str(spec.get("idempotency_key") or ""), _payload_hash(spec)
    scope = "update:" + task_id
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        receipt = _receipt(conn, scope, key, digest)
        if receipt:
            return receipt
        changed = conn.execute("UPDATE tasks SET " + ", ".join(f"{key} = ?" for key in values)
                               + ", revision = revision + 1 WHERE id = ? AND revision = ?",
                               [*[_packed(value) for value in values.values()], task_id, expected]).rowcount
        if not changed:
            raise ValueError("定时任务已被其他会话修改，请读取最新版本后重试")
        result = view(dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()), connection=conn)
        _remember(conn, scope, key, digest, result)
    return result


def update(task_id: str, spec: dict) -> dict:
    with named_lock("schedule-occurrence", task_id):
        current = get(task_id)
        expected = spec.get("expected_revision", current["revision"])
        if spec.get("enabled") is False and set(spec) <= {"enabled", "expected_revision", "idempotency_key"}:
            return _update_definition(task_id, spec, {"enabled": 0, "next_due": None, "updated_at": db.now()}, expected)
        merged = _clean({**current, **spec}, previous=current)
        values = {**merged, "updated_at": db.now()}
        enabled = bool(spec.get("enabled", current["enabled"]))
        values["enabled"] = int(enabled)
        timing_changed = merged["rule"] != current["rule"] or enabled != current["enabled"]
        if timing_changed:
            values["next_due"] = _stamp(next_due(merged["rule"], datetime.datetime.now(UTC))) if enabled else None
        return _update_definition(task_id, spec, values, expected)


def set_enabled(task_id: str, enabled: bool, expected_revision: int | None = None) -> dict:
    spec = {"enabled": bool(enabled)}
    if expected_revision is not None:
        spec["expected_revision"] = expected_revision
    return update(task_id, spec)


def delete(task_id: str) -> None:
    with named_lock("schedule-occurrence", task_id):
        get(task_id)
        db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))


def _prepare(task: dict, trigger: str) -> dict:
    try:
        if task["kind"] == "native":
            from .. import native_run

            return native_run.prepare(task["project_id"], task["asset_id"], parameters=task["parameters"],
                                      trigger=trigger, task_id=task["id"], session_id=task.get("session_id"), host=task["host"], host_session_id=task["host_session_id"])
        return runner.prepare(task["project_id"], task["case_ids"] or None, task["engine"], trigger=trigger,
                              task_id=task["id"], session_id=task.get("session_id"), plan_path=task.get("plan_path") or None,
                              headed=_headed(task), environment_id=task.get("environment_id") or None,
                              account_id=task.get("account_id") or None, strict_variables=True, use_active_context=False)
    except ValueError as error:
        stamp = db.now()
        env_id = str(task.get("environment_id") or "")
        acc_id = str(task.get("account_id") or "")
        env_name, acc_label = _task_context_labels(task["project_id"], env_id, acc_id)
        return {"id": db.new_id("run"), "project_id": task["project_id"], "engine": task["engine"], "trigger": trigger,
                "task_id": task["id"], "status": "Blocked", "reason": str(error), "case_ids": task["case_ids"],
                "total": len(task["case_ids"]) if task["kind"] == "replay" else 0,
                "environment_id": env_id, "environment_name": env_name,
                "account_id": acc_id, "account_label": acc_label,
                "created_at": stamp, "finished_at": stamp}


def _maintenance(conn, task: dict, occurrence_id: str) -> dict:
    stamp, delivery_id = db.now(), db.new_id("delivery")
    cycle = None
    if task["workflow_id"] and conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_runtime'").fetchone():
        flow = conn.execute("SELECT cycle_id FROM workflow_runtime WHERE id=?", (task["workflow_id"],)).fetchone()
        cycle = flow["cycle_id"] if flow else None
    payload = {"project_id": task["project_id"], "workflow_id": task["workflow_id"], "task_id": task["id"],
               "cycle_id": cycle,
               "occurrence_id": occurrence_id, "schedule_revision": task["revision"],
               "session_id": task["session_id"] or occurrence_id, "parameters": {**task["parameters"], "host_session_id": task["host_session_id"]}}
    delivery = {"id": delivery_id, "task_id": task["id"], "occurrence_id": occurrence_id,
                "host": task["host"], "payload": payload, "state": "pending", "created_at": stamp, "updated_at": stamp}
    _insert(conn, "host_outbox", delivery)
    return {**delivery, "status": "awaiting_host"}


def _check_revision(conn, task: dict) -> None:
    if not conn.execute("SELECT 1 FROM tasks WHERE id = ? AND revision = ?", (task["id"], task["revision"])).fetchone():
        raise ValueError("定时任务已被其他会话修改，请读取最新版本后重试")


def fire(task: dict, trigger: str = "schedule", idempotency_key: str | None = None) -> dict:
    """Queue an immediate occurrence; retrying the same request returns its durable receipt."""
    key = idempotency_key or ""
    scope, digest = "fire:" + task["id"], _payload_hash({"trigger": trigger})
    with named_lock("schedule-occurrence", task["id"]), db.connect() as conn:
        receipt = _receipt(conn, scope, key, digest)
        if receipt:
            return receipt
        _check_revision(conn, task)
        prepared = None if task["kind"] == "maintenance" else _prepare(task, trigger)
        conn.execute("BEGIN IMMEDIATE")
        receipt = _receipt(conn, scope, key, digest)
        if receipt:
            return receipt
        _check_revision(conn, task)
        occurrence_id = db.new_id("occurrence")
        result = _maintenance(conn, task, occurrence_id) if prepared is None else runner.enqueue(prepared, connection=conn)
        maintenance = prepared is None
        _insert(conn, "schedule_occurrences", {"id": occurrence_id, "task_id": task["id"], "revision": task["revision"],
                "due_at": "manual:" + occurrence_id, "status": "awaiting_host" if maintenance else result["status"],
                "run_id": None if maintenance else result["id"], "delivery_id": result["id"] if maintenance else None, "created_at": db.now()})
        if not maintenance:
            conn.execute("UPDATE tasks SET last_run_id = ? WHERE id = ?", (result["id"], task["id"]))
        _remember(conn, scope, key, digest, result)
    return result


def run_now(task_id: str, idempotency_key: str | None = None) -> dict:
    return fire(get(task_id), trigger="manual", idempotency_key=idempotency_key)


def _busy(conn, task: dict) -> bool:
    if task["overlap"] != "skip":
        return False
    if task["kind"] == "maintenance":
        return bool(conn.execute("SELECT 1 FROM host_outbox WHERE task_id = ? AND state != 'delivered'", (task["id"],)).fetchone())
    return bool(conn.execute("SELECT 1 FROM runs WHERE task_id = ? AND status IN ('Queued', 'Running')", (task["id"],)).fetchone())


def _claim_occurrence(task: dict, now: datetime.datetime) -> dict | None:
    due = datetime.datetime.fromisoformat(task["next_due"])
    once = task["rule"].get("kind") == "at"
    following = None if once else _stamp(next_due(task["rule"], now, due))
    with named_lock("schedule-occurrence", task["id"]), db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        prepared = None
        if task["kind"] != "maintenance" and not _busy(conn, task):
            # Another tick may have already advanced this occurrence while we waited.
            if not conn.execute("SELECT 1 FROM tasks WHERE id = ? AND next_due = ? AND revision = ? AND enabled = 1",
                                (task["id"], task["next_due"], task["revision"])).fetchone():
                return None
            conn.commit()
            prepared = _prepare(task, "schedule")
            conn.execute("BEGIN IMMEDIATE")
        claimed = conn.execute("UPDATE tasks SET next_due = ?, enabled = ? WHERE id = ? AND next_due = ? AND revision = ? AND enabled = 1",
                               (following, int(not once), task["id"], task["next_due"], task["revision"])).rowcount
        if not claimed:
            return None
        occurrence = {"id": db.new_id("occurrence"), "task_id": task["id"], "revision": task["revision"],
                      "due_at": task["next_due"], "created_at": db.now()}
        if _busy(conn, task):
            _insert(conn, "schedule_occurrences", {**occurrence, "status": "skipped", "reason": "前次执行仍未结束，按重叠策略跳过"})
            return None
        if task["kind"] == "maintenance":
            result = _maintenance(conn, task, occurrence["id"])
            _insert(conn, "schedule_occurrences", {**occurrence, "status": "awaiting_host", "delivery_id": result["id"]})
            return None
        result = runner.enqueue(prepared, connection=conn)
        _insert(conn, "schedule_occurrences", {**occurrence, "status": result["status"], "run_id": result["id"]})
        conn.execute("UPDATE tasks SET last_run_id = ? WHERE id = ?", (result["id"], task["id"]))
    return result


def tick(now: datetime.datetime | None = None) -> list[dict]:
    """Freeze inputs outside SQLite, then enqueue and advance the clock in one transaction."""
    now = now or datetime.datetime.now(UTC)
    started = []
    for row in db.rows("SELECT * FROM tasks WHERE enabled = 1 AND next_due IS NOT NULL AND next_due <= ?", (_stamp(now),)):
        task = view(row, include_state=False)
        result = _claim_occurrence(task, now)
        if result and result["status"] == "Queued":
            started.append(result)
    return started
