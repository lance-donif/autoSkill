"""Durable host delivery receipts; an uncertain delivery is investigated before it is retried."""
from __future__ import annotations

import datetime
import json
import uuid

from .. import db


def _view(row) -> dict:
    return {**dict(row), "payload": json.loads(row["payload"]), "receipt": json.loads(row["receipt"])}


def _get(conn, delivery_id: str) -> dict:
    row = conn.execute("SELECT * FROM host_outbox WHERE id = ?", (delivery_id,)).fetchone()
    if not row:
        raise ValueError("宿主投递不存在")
    return _view(row)


def pending_deliveries(host: str = "dsh", offset: int = 0, limit: int = 50) -> dict:
    if offset < 0 or limit < 1:
        raise ValueError("分页起点不能为负数，每页数量需要大于零")
    with db.connect() as conn:
        conn.execute("UPDATE host_outbox SET state = 'unknown', reason = '投递租约已过期，需要查询宿主收据', updated_at = ? "
                     "WHERE state = 'dispatching' AND lease_until <= ?", (db.now(), db.now()))
        total = conn.execute("SELECT COUNT(*) FROM host_outbox WHERE host = ? AND state != 'delivered'", (host,)).fetchone()[0]
        rows = conn.execute("SELECT * FROM host_outbox WHERE host = ? AND state != 'delivered' ORDER BY created_at, id LIMIT ? OFFSET ?", (host, limit, offset))
        items = [_view(row) for row in rows]
    more = offset + len(items) < total
    return {"items": items, "total": total, "has_more": more, "next_offset": offset + len(items) if more else None}


def claim_delivery(delivery_id: str, owner: str, lease_seconds: float = 120) -> dict:
    if not owner or lease_seconds <= 0:
        raise ValueError("投递需要宿主标识和正数租约时长")
    stamp = db.now()
    deadline = (datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=lease_seconds)).isoformat(timespec="milliseconds")
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _get(conn, delivery_id)
        if row["state"] == "delivered":
            return row
        if row["state"] == "dispatching" and row["lease_until"] <= stamp:
            conn.execute("UPDATE host_outbox SET state = 'unknown', updated_at = ?, reason = ? WHERE id = ?",
                         (stamp, "投递租约已过期，需要查询宿主收据", delivery_id))
            return _get(conn, delivery_id)
        if row["state"] != "pending":
            return row if row["owner"] == owner else {**row, "token": ""}
        conn.execute("UPDATE host_outbox SET state = 'dispatching', owner = ?, token = ?, lease_until = ?, "
                     "attempts = attempts + 1, updated_at = ? WHERE id = ?", (owner, uuid.uuid4().hex, deadline, stamp, delivery_id))
        return _get(conn, delivery_id)


def ack_delivery(delivery_id: str, token: str, receipt: dict) -> dict:
    if not isinstance(receipt, dict) or not receipt:
        raise ValueError("需要宿主的实际投递收据")
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _get(conn, delivery_id)
        if row["state"] == "delivered":
            if row["receipt"] != receipt:
                raise ValueError("投递已经确认，收据与已有记录不同")
            return row
        if not token or token != row["token"] or row["state"] not in ("dispatching", "unknown"):
            raise ValueError("投递租约不匹配")
        conn.execute("UPDATE host_outbox SET state = 'delivered', receipt = ?, reason = '', lease_until = NULL, updated_at = ? WHERE id = ?",
                     (json.dumps(receipt, ensure_ascii=False), db.now(), delivery_id))
        conn.execute("UPDATE schedule_occurrences SET status = 'host_delivered' WHERE id = ?", (row["occurrence_id"],))
        return _get(conn, delivery_id)


def release_delivery(delivery_id: str, token: str, reason: str) -> dict:
    """Release a claim only when host preparation failed before any prompt was dispatched."""
    if not reason.strip():
        raise ValueError("需要记录调用宿主之前失败的原因")
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _get(conn, delivery_id)
        if not token or row["state"] != "dispatching" or row["token"] != token:
            raise ValueError("只有当前投递者能释放尚未派发的任务")
        conn.execute("UPDATE host_outbox SET state = 'pending', owner = '', token = '', lease_until = NULL, reason = ?, updated_at = ? WHERE id = ?",
                     (reason, db.now(), delivery_id))
        return _get(conn, delivery_id)


def resolve_delivery(delivery_id: str, receipt: dict | None = None, retry: bool = False, reason: str = "") -> dict:
    """Recovery supplies host evidence; delivery completion never asserts workflow success."""
    if receipt and retry:
        raise ValueError("已有投递收据时不能同时重发")
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _get(conn, delivery_id)
        if row["state"] != "unknown":
            raise ValueError("只有结果未知的投递需要恢复调查")
        if not reason.strip() or (not receipt and not retry):
            raise ValueError("需要恢复调查依据，以及已投递收据或明确重发决定")
        state = "delivered" if receipt else "pending"
        conn.execute("UPDATE host_outbox SET state = ?, receipt = ?, reason = ?, owner = '', token = '', lease_until = NULL, updated_at = ? WHERE id = ?",
                     (state, json.dumps(receipt or {}, ensure_ascii=False), reason, db.now(), delivery_id))
        conn.execute("UPDATE schedule_occurrences SET status = ? WHERE id = ?", ("host_delivered" if receipt else "awaiting_host", row["occurrence_id"]))
        return _get(conn, delivery_id)
