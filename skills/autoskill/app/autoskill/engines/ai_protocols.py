"""Observable HTTP, SSE, and WebSocket response events with separate answer and protocol deadlines."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import time

import httpx

from . import ai_audio

_MISSING = object()
_NON_CONTENT_EVENTS = ("heartbeat", "ping", "thinking", "reasoning", "response.reasoning.delta")


class EvaluationCancelledError(Exception):
    """The caller cancelled an active evaluation."""


def pointer(value, path: str):
    if path in ("", "/"):
        return value
    for part in path.lstrip("/").split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


class Observation:
    def __init__(self, endpoint: dict, cancelled=None):
        self.started = time.monotonic()
        self.first_content = None
        self.first_byte = None
        self.first_audio = None
        self.last_content = None
        self.last_event = self.started
        self.endpoint = endpoint
        self.cancelled = cancelled
        self.events = []
        self.content = ""
        self.answer = _MISSING
        self.query = None
        self.complete = False
        self.completed_at = None
        self.status_code = None
        self.bindings = {}
        self.audio_sent = {}
        self.audio_received = {"bytes": 0, "chunks": 0}
        self.audio_hasher = hashlib.sha256()

    def extract(self, value):
        for name, path in self.endpoint.get("extract", {}).items():
            try:
                self.bindings[name] = pointer(value, path)
            except (KeyError, IndexError, ValueError, TypeError):
                continue

    def touch(self):
        stamp = time.monotonic()
        self.first_byte = self.first_byte or stamp
        self.last_event = stamp

    def finish(self):
        self.complete = True
        if self.completed_at is None:
            self.completed_at = time.monotonic()

    def _snapshot(self, value, kind: str):
        """A terminal answer field is a full snapshot; delta fields remain incremental."""
        if kind in _NON_CONTENT_EVENTS or value == "[DONE]":
            return _MISSING
        path = self.endpoint.get("final_answer_pointer", "/answer")
        try:
            return pointer(value, path)
        except (KeyError, IndexError, ValueError, TypeError):
            return _MISSING

    def _content(self, value, kind: str, done: bool) -> dict:
        snapshot = self._snapshot(value, kind) if done else _MISSING
        if snapshot is not _MISSING:
            visible = snapshot if isinstance(snapshot, str) else json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
            changed = visible != self.content
            if changed:
                stamp = time.monotonic()
                self.first_content = self.first_content or stamp
                self.last_content = stamp
            self.answer = snapshot
            self.content = visible
            return {"content": "", "content_mode": "snapshot", "answer": snapshot, "content_changed": changed}
        delta = self._delta(value, kind, omit_answer=done and "final_answer_pointer" in self.endpoint)
        if delta:
            stamp = time.monotonic()
            self.first_content = self.first_content or stamp
            self.last_content = stamp
            self.content += delta
        return {"content": delta, "content_mode": "delta"}

    def event(self, value, kind: str = "message"):
        self.touch()
        self.extract(value)
        ai_audio.receive_event(self, value)
        elapsed = round((time.monotonic() - self.started) * 1000, 3)
        done = value == "[DONE]" or kind in self.endpoint.get("done_events", ["done", "completed", "response.completed"])
        if isinstance(value, dict):
            kind = str(value.get("type") or kind)
            done = done or value.get("done") is True or kind in self.endpoint.get("done_events", ["done", "completed", "response.completed"])
        content = self._content(value, kind, done)
        if done:
            self.finish()
        # Evidence contains visible answer content, not private reasoning payloads or heartbeat bodies.
        self.events.append({"kind": kind, "elapsed_ms": elapsed, **content, "complete": done})

    def _delta(self, value, kind: str, *, omit_answer: bool = False) -> str:
        if kind in _NON_CONTENT_EVENTS or value == "[DONE]":
            return ""
        if isinstance(value, str):
            return value
        defaults = ["/choices/0/delta/content", "/delta", "/content", *([] if omit_answer else ["/answer"])]
        paths = [self.endpoint["content_pointer"]] if self.endpoint.get("content_pointer") else defaults
        for path in paths:
            try:
                delta = pointer(value, path)
            except (KeyError, IndexError, ValueError, TypeError):
                continue
            if isinstance(delta, (str, int, float)) and not isinstance(delta, bool):
                return str(delta)
        return ""

    async def wait(self, awaitable, *, connecting: bool = False):
        pending = asyncio.ensure_future(awaitable)
        limits = {"connect": 30, "first_content": 300, "idle": 120, "total": 900, **self.endpoint.get("timeouts", {})}
        try:
            while True:
                if self.cancelled and self.cancelled():
                    raise EvaluationCancelledError("运行已取消")
                elapsed = time.monotonic() - self.started
                if elapsed >= limits["total"]:
                    raise TimeoutError("整体响应超时")
                if self.first_content is None and elapsed >= limits["first_content"]:
                    raise TimeoutError("首个可见回答内容超时")
                if connecting and elapsed >= limits["connect"]:
                    raise TimeoutError("连接超时")
                if not connecting and time.monotonic() - self.last_event >= limits["idle"]:
                    raise TimeoutError("流式响应空闲超时")
                done, _ = await asyncio.wait({pending}, timeout=0.05)
                if done:
                    return pending.result()
        finally:
            if not pending.done():
                pending.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pending

    def result(self):
        def elapsed(value):
            return round((value - self.started) * 1000, 3) if value is not None else None

        return {"answer": self.answer if self.answer is not _MISSING else self.content, "query": self.query,
                "status_code": self.status_code, "protocol_complete": self.complete, "events": self.events, "bindings": self.bindings,
                "audio_sent": self.audio_sent, "audio_received": self.audio_received,
                "timings": {"first_byte_ms": elapsed(self.first_byte), "first_content_ms": elapsed(self.first_content),
                            "first_audio_ms": elapsed(self.first_audio),
                            "last_content_ms": elapsed(self.last_content), "protocol_complete_ms": elapsed(self.completed_at),
                            "total_ms": elapsed(time.monotonic())}}


def _decode(data: str):
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        return data


async def _sse(response, state):
    lines, event = [], "message"
    iterator = response.aiter_lines().__aiter__()
    while not state.complete:
        try:
            line = await state.wait(iterator.__anext__())
        except StopAsyncIteration:
            break
        state.touch()
        if not line:
            if lines:
                state.event(_decode("\n".join(lines)), event)
            lines, event = [], "message"
        elif line.startswith("data:"):
            lines.append(line[5:].lstrip(" "))
        elif line.startswith("event:"):
            event = line[6:].strip()
    if lines and not state.complete:
        state.event(_decode("\n".join(lines)), event)


async def _http(client, endpoint, body, state):
    request = client.build_request(endpoint.get("method", "POST"), endpoint["url"], headers=endpoint.get("headers", {}), json=body)
    response = await state.wait(client.send(request, stream=True), connecting=True)
    try:
        state.touch()
        state.status_code = response.status_code
        response.raise_for_status()
        if endpoint.get("protocol", "http") == "sse":
            await _sse(response, state)
        else:
            raw = await state.wait(response.aread())
            value = _decode(raw.decode(response.encoding or "utf-8"))
            state.extract(value)
            state.answer = pointer(value, endpoint.get("answer_pointer", "/answer")) if isinstance(value, (dict, list)) else value
            if endpoint.get("query_pointer"):
                state.query = pointer(value, endpoint["query_pointer"])
            state.event(state.answer if isinstance(state.answer, str) else json.dumps(state.answer, ensure_ascii=False))
            state.finish()
    finally:
        await response.aclose()


async def _websocket(endpoint, body, state):
    import websockets

    socket = await state.wait(websockets.connect(endpoint["url"], additional_headers=endpoint.get("headers", {}),
                                                open_timeout=None, max_size=None, close_timeout=1), connecting=True)
    audio_task = None
    try:
        await state.wait(socket.send(json.dumps(body, ensure_ascii=False)))
        audio_task = asyncio.create_task(ai_audio.send(socket, state))
        while not state.complete:
            if audio_task.done() and not audio_task.cancelled():
                audio_task.result()
            try:
                message = await state.wait(socket.recv())
            except websockets.exceptions.ConnectionClosed:
                break
            state.touch()
            if isinstance(message, bytes):
                ai_audio.receive(state, message)
                state.events.append({"kind": "audio", "bytes": len(message), "elapsed_ms": round((time.monotonic() - state.started) * 1000, 3)})
            else:
                state.event(_decode(message))
    finally:
        if audio_task is not None:
            if not audio_task.done():
                audio_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await audio_task
        await socket.close()


async def request(client, endpoint: dict, body, cancelled=None) -> dict:
    state = Observation(endpoint, cancelled)
    try:
        if endpoint.get("protocol", "http") == "websocket":
            await _websocket(endpoint, body, state)
        else:
            await _http(client, endpoint, body, state)
    except EvaluationCancelledError:
        return {**state.result(), "cancelled": True, "error": "运行已取消"}
    except (httpx.HTTPError, TimeoutError, OSError, KeyError, IndexError, ValueError, TypeError) as error:
        return {**state.result(), "error": str(error) or type(error).__name__}
    return {**state.result(), "error": "" if state.complete else "响应在协议完成事件之前中断"}
