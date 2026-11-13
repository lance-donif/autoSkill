"""Deterministic synthetic analytics conversations for HTTP and SSE replay tests."""
from __future__ import annotations

import json
import re
import time
import uuid

DATASET = [
    {"order": "SYN-001", "year": 2025, "city": "北京", "sales": 120},
    {"order": "SYN-002", "year": 2025, "city": "北京", "sales": 80},
    {"order": "SYN-003", "year": 2025, "city": "上海", "sales": 150},
    {"order": "SYN-004", "year": 2025, "city": "上海", "sales": 50},
    {"order": "SYN-005", "year": 2025, "city": "深圳", "sales": 300},
    {"order": "SYN-006", "year": 2024, "city": "北京", "sales": 90},
]


def _question(body: dict) -> str:
    if isinstance(body.get("question"), str) and body["question"].strip():
        return body["question"].strip()
    for message in reversed(body.get("messages", [])):
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            return message["content"].strip()
    raise ValueError("question required")


def _query(question: str, previous: dict) -> dict:
    query = {**previous, "operation": "sum", "field": "sales"}
    year = re.search(r"20\d{2}", question)
    if year:
        query["year"] = int(year.group())
    city = next((city for city in ("北京", "上海", "深圳") if city in question), None)
    if city:
        query["city"] = city
    if "所有年份" in question:
        query.pop("year", None)
    if "所有城市" in question or "按城市" in question:
        query.pop("city", None)
    query["group_by"] = "city" if "按城市" in question else None
    if "平均" in question:
        query["operation"] = "average"
    if "笔" in question or "订单数" in question:
        query["operation"] = "count"
    return query


def _value(rows: list[dict], operation: str):
    if operation == "count":
        return len(rows)
    total = sum(row["sales"] for row in rows)
    return total / len(rows) if operation == "average" and rows else total


def _answer(query: dict):
    rows = [row for row in DATASET if all(key not in query or row[key] == query[key] for key in ("year", "city"))]
    if query.get("group_by") == "city":
        return {city: _value([row for row in rows if row["city"] == city], query["operation"]) for city in sorted({row["city"] for row in rows})}
    return _value(rows, query["operation"])


def _reply(state, body: dict) -> dict:
    question = _question(body)
    with state.lock:
        if not hasattr(state, "conversations"):
            state.conversations = {}
        requested = body.get("conversation_id")
        conversation_id = requested if isinstance(requested, str) and requested in state.conversations else "chat-" + uuid.uuid4().hex
        conversation = state.conversations.setdefault(conversation_id, {"turn": 0, "query": {}})
        query = _query(question, conversation["query"])
        conversation.update(turn=conversation["turn"] + 1, query=query)
        return {"conversation_id": conversation_id, "turn": conversation["turn"], "answer": _answer(query), "query": query, "source": "synthetic_dataset"}


def _config(state, body: dict | None = None) -> dict:
    with state.lock:
        current = {"wrong_answer": False, "delay_ms": 0, "truncate_stream": False, **getattr(state, "chat_config", {})}
        if body is not None:
            if not isinstance(body, dict) or any(key not in current for key in body):
                raise ValueError("unknown chat config")
            for key, value in body.items():
                if key == "delay_ms":
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                        raise ValueError("invalid delay_ms")
                elif not isinstance(value, bool):
                    raise ValueError("invalid fault flag")
            current.update(body)
            state.chat_config = current
        return dict(current)


def _faults(result: dict, config: dict) -> dict:
    if config["delay_ms"]:
        time.sleep(config["delay_ms"] / 1000)
    if config["wrong_answer"]:
        answer = result["answer"]
        result["answer"] = {key: value + 1 for key, value in answer.items()} if isinstance(answer, dict) else answer + 1
    return result


def _stream(handler, result: dict, config: dict) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Connection", "close")
    handler.end_headers()
    answer = json.dumps(result["answer"], ensure_ascii=False)
    events = [("metadata", {"conversation_id": result["conversation_id"], "turn": result["turn"]}),
              ("message", {"delta": answer[:max(1, len(answer) // 2)]}),
              ("message", {"delta": answer[max(1, len(answer) // 2):]}),
              ("done", {"done": True, "conversation_id": result["conversation_id"], "query": result["query"]})]
    if config["truncate_stream"]:
        events = events[:-1]
    try:
        for event, payload in events:
            handler.wfile.write(("event: " + event + "\ndata: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8"))
            handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError):
        handler.close_connection = True
    handler.close_connection = True


def handle(handler, path: str, query: dict) -> bool:
    """Handle this fixture's routes; return False for the main TicketDesk router."""
    del query
    if path not in ("/api/chat", "/api/chat/stream", "/api/chat/dataset", "/api/chat/config"):
        return False
    if path == "/api/chat/config" and handler.command in ("GET", "POST"):
        handler.send_json(200, _config(handler.state, handler.read_json() if handler.command == "POST" else None))
        return True
    if path == "/api/chat/dataset" and handler.command in ("GET", "HEAD"):
        handler.send_json(200, {"rows": DATASET, "description": "仅供测试的固定合成数据，不含真实业务数据"})
        return True
    if path in ("/api/chat/dataset", "/api/chat/config") or handler.command != "POST":
        handler.send_json(405, {"code": "METHOD_NOT_ALLOWED"})
        return True
    body = handler.read_json()
    if not isinstance(body, dict):
        raise ValueError("JSON object required")
    config = _config(handler.state)
    result = _faults(_reply(handler.state, body), config)
    if path.endswith("/stream"):
        _stream(handler, result, config)
    else:
        handler.send_json(200, result)
    return True
