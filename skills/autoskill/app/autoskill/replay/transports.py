"""Finite, explicitly completed SSE and WebSocket conversations, including PCM audio.

Requests are never retried. A partial answer or a closed connection is not a completed conversation.
"""
from __future__ import annotations

import asyncio
import base64
from contextlib import suppress
import json
import math
import os
from pathlib import Path
import re
import time
import wave
from urllib.parse import urlencode, urlsplit, urlunsplit

from .. import paths
from ..artifacts import resolve_upload_assets
from ..core import store


class ProtocolError(ValueError):
    def __init__(self, message: str, category: str = "protocol_error"):
        super().__init__(message)
        self.category = category


def kind(case: dict) -> str:
    return case.get("extensions", {}).get("transport", {}).get("type", "http")


def positive(value, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(name + " 必须是正数")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(name + " 必须是有限正数") from error
    if not math.isfinite(number) or number <= 0:
        raise ValueError(name + " 必须是有限正数")
    return number


def validate(spec: dict) -> None:
    if not isinstance(spec, dict) or spec.get("type") not in ("sse", "websocket"):
        raise ValueError("transport.type 必须是 sse 或 websocket")
    for name in ("timeout_seconds", "idle_timeout_seconds", "first_event_timeout_seconds"):
        if name in spec:
            positive(spec[name], name)
    if "max_bytes" in spec and positive(spec["max_bytes"], "max_bytes") < 1:
        raise ValueError("max_bytes 必须至少为 1")
    def until(value):
        if not isinstance(value, (str, dict)) or not value:
            raise ValueError("until 必须是非空事件名或匹配条件对象")
        if isinstance(value, dict) and "pointer" in value and (not isinstance(value["pointer"], str) or "equals" not in value):
            raise ValueError("until.pointer 需要字符串路径及 equals")
    if "until" in spec:
        until(spec["until"])
    audio_output = spec.get("audio_output", {})
    if not isinstance(audio_output, dict):
        raise ValueError("audio_output 必须是对象")
    for name in ("sample_rate", "channels"):
        if name in audio_output and positive(audio_output[name], "audio_output." + name) % 1:
            raise ValueError("audio_output." + name + " 必须是整数")
    if audio_output.get("format", "pcm16") not in ("pcm16", "raw"):
        raise ValueError("audio_output.format 必须是 pcm16 或 raw")
    if audio_output.get("channels", 1) not in (1, 2):
        raise ValueError("audio_output.channels 必须是 1 或 2")
    if not 8000 <= positive(audio_output.get("sample_rate", 16000), "audio_output.sample_rate") <= 192000:
        raise ValueError("audio_output.sample_rate 必须在 8000–192000 Hz 范围内")
    if "subprotocols" in spec and (not isinstance(spec["subprotocols"], list) or not spec["subprotocols"]
            or any(not isinstance(value, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", value) for value in spec["subprotocols"])):
        raise ValueError("subprotocols 必须是非空的有效协议名数组")
    if spec["type"] == "websocket":
        steps = spec.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ValueError("WebSocket 需要 steps：send、send_audio、receive 或 close")
        for step in steps:
            if not isinstance(step, dict) or len(step) != 1 or next(iter(step)) not in ("send", "send_audio", "receive", "close"):
                raise ValueError("WebSocket 每一步只能包含 send / send_audio / receive / close 之一")
            if "receive" in step and not isinstance(step["receive"], dict):
                raise ValueError("receive 必须是对象")
            if "receive" in step:
                receive = step["receive"]
                if "until" in receive:
                    until(receive["until"])
                count = positive(receive.get("min_messages", 1), "receive.min_messages")
                if count % 1:
                    raise ValueError("receive.min_messages 必须是整数")
                if not isinstance(receive.get("expect_json", {}), dict):
                    raise ValueError("receive.expect_json 必须是对象")
            if "send_audio" in step and (not isinstance(step["send_audio"], dict) or not isinstance(step["send_audio"].get("file"), str)):
                raise ValueError("send_audio 需要 file 指向项目内音频素材")
            if "send_audio" in step:
                audio = step["send_audio"]
                if audio.get("encoding", "base64") not in ("base64", "binary"):
                    raise ValueError("send_audio.encoding 必须是 base64 或 binary")
                if not isinstance(audio.get("event", {}), dict):
                    raise ValueError("send_audio.event 必须是对象")
                for name in ("sample_rate", "chunk_ms"):
                    if name in audio:
                        positive(audio[name], "send_audio." + name)
                if "sample_rate" in audio and (positive(audio["sample_rate"], "send_audio.sample_rate") % 1
                        or not 8000 <= float(audio["sample_rate"]) <= 192000):
                    raise ValueError("send_audio.sample_rate 必须是 8000–192000 Hz 的整数")
                if audio.get("channels", 1) not in (1, 2):
                    raise ValueError("send_audio.channels 必须是 1 或 2")
                if "realtime" in audio and not isinstance(audio["realtime"], bool):
                    raise ValueError("send_audio.realtime 必须是布尔值")
    if "assertions" in spec and not isinstance(spec["assertions"], dict):
        raise ValueError("transport.assertions 必须是对象")
    known = {"text_contains", "reasoning_contains", "text_regex", "reasoning_regex", "min_events", "min_messages",
             "min_audio_frames", "min_audio_bytes", "min_sent_audio_bytes", "complete", "max_first_event_ms", "max_first_text_ms", "max_first_audio_ms"}
    for name, value in spec.get("assertions", {}).items():
        if name not in known:
            raise ValueError("不支持的 transport 断言：" + str(name))
        if name.endswith("_contains") and (not isinstance(value, list) or any(not isinstance(item, str) for item in value)):
            raise ValueError(name + " 必须是字符串数组")
        if name.endswith("_regex"):
            try:
                re.compile(value)
            except (TypeError, re.error) as error:
                raise ValueError(name + " 不是有效正则表达式") from error
        if name.startswith("max_"):
            positive(value, name)
        if name.startswith("min_") and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise ValueError(name + " 必须是非负整数")
        if name == "complete" and not isinstance(value, bool):
            raise ValueError("complete 断言必须是布尔值")


def checks(spec: dict, parsed, *, handshake_only=False) -> list[dict]:
    """Conversation-level assertions run only after the configured terminal event."""
    rules = spec.get("assertions", {})
    if not isinstance(parsed, dict):
        if not handshake_only:
            return []
        parsed = {}
    rows = []
    def append(name, actual, expected, passed):
        rows.append({"name": name, "actual": actual, "expected": expected, "passed": bool(passed)})
    if handshake_only:
        for name, expected in rules.items():
            append("未建立会话：" + name, False if name == "complete" else "不可用", expected,
                   name == "complete" and expected is False)
        return rows
    for field in ("text", "reasoning"):
        for text in rules.get(field + "_contains", []):
            append(field + " 包含", "已包含" if str(text) in parsed.get(field, "") else "未包含", text, str(text) in parsed.get(field, ""))
        pattern = rules.get(field + "_regex")
        if pattern is not None:
            matched = re.search(pattern, parsed.get(field, ""), re.S) is not None
            append(field + " 正则", "匹配" if matched else "不匹配", pattern, matched)
    for name, actual in (("events", len(parsed.get("events", []))), ("messages", len(parsed.get("messages", []))),
                         ("audio_frames", parsed.get("audio_frames", 0)), ("audio_bytes", parsed.get("audio_bytes", 0)),
                         ("sent_audio_bytes", parsed.get("metrics", {}).get("sent_audio_bytes", 0))):
        if "min_" + name in rules:
            wanted = int(rules["min_" + name])
            append(name + " 至少", actual, wanted, actual >= wanted)
    if "complete" in rules:
        append("对话完成", parsed.get("complete"), rules["complete"], parsed.get("complete") is rules["complete"])
    for field in ("first_event_ms", "first_text_ms", "first_audio_ms"):
        if "max_" + field in rules:
            actual = parsed.get("metrics", {}).get(field)
            append(field + " 不超过", actual, rules["max_" + field], actual is not None and actual <= rules["max_" + field])
    return rows


def matches(value, condition) -> bool:
    if isinstance(condition, str):
        return value == condition or (isinstance(value, dict) and condition in (value.get("event"), value.get("type"), value.get("data")))
    if not isinstance(condition, dict):
        return False
    candidate = value
    if "pointer" in condition:
        try:
            candidate = store.pointer(value, condition["pointer"])
        except (KeyError, IndexError, TypeError, ValueError):
            return False
    if "equals" in condition:
        return candidate == condition["equals"]
    return bool(condition) and isinstance(value, dict) and all(value.get(k) == v for k, v in condition.items())


class Conversation:
    def __init__(self, spec: dict, config: dict):
        self.spec, self.config = spec, config
        self.started = self.last_event = time.monotonic()
        self.budget = positive(spec.get("timeout_seconds", config.get("conversation_timeout_seconds", 600)), "timeout_seconds")
        self.idle = positive(spec.get("idle_timeout_seconds", self.budget), "idle_timeout_seconds")
        self.first = positive(spec.get("first_event_timeout_seconds", self.budget), "first_event_timeout_seconds")
        self.events, self.text, self.reasoning, self.audio = [], [], [], []
        self.complete, self.sent_audio_bytes = False, 0
        self.metrics = {"first_event_ms": None, "first_text_ms": None, "first_audio_ms": None}
        self.max_bytes = int(positive(spec.get("max_bytes", 64 * 1024 * 1024), "max_bytes"))
        self.bytes = 0

    def tick(self):
        now = time.monotonic()
        check = self.config.get("_cancel_check")
        if callable(check) and check():
            raise ProtocolError("对话执行已取消", "cancelled")
        if now - self.started >= self.budget:
            raise ProtocolError("对话未在总等待时间内完成", "timeout")
        if now - self.last_event >= self.idle:
            raise ProtocolError("对话流等待后续事件超时", "timeout")
        if self.metrics["first_event_ms"] is None and now - self.started >= self.first:
            raise ProtocolError("等待第一个对话事件超时", "timeout")

    def remaining(self):
        self.tick()
        now = time.monotonic()
        limits = [self.started + self.budget - now, self.last_event + self.idle - now]
        if self.metrics["first_event_ms"] is None:
            limits.append(self.started + self.first - now)
        return max(.001, min(limits))

    def pause(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            time.sleep(min(.1, self.remaining(), max(0, deadline - time.monotonic())))
        self.tick()

    def _text(self, text, reasoning=False):
        if not isinstance(text, str) or not text:
            return
        (self.reasoning if reasoning else self.text).append(text)
        key = "first_text_ms"
        if not reasoning and self.metrics[key] is None:
            self.metrics[key] = round((time.monotonic() - self.started) * 1000)

    def add(self, value, event: str = "message", data: str | None = None):
        self.tick()
        if event in ("error", "failed", "response.failed", "response.error", "response.cancelled", "response.incomplete"):
            raise ProtocolError("对话服务返回错误事件", "remote_error")
        self.last_event = time.monotonic()
        elapsed = round((self.last_event - self.started) * 1000)
        if self.metrics["first_event_ms"] is None:
            self.metrics["first_event_ms"] = elapsed
        self.events.append(value)
        if isinstance(value, dict):
            label = str(value.get("type", event))
            response_status = value.get("response", {}).get("status") if isinstance(value.get("response"), dict) else None
            if (label in ("error", "response.failed", "response.error") or value.get("error")
                    or (label == "response.done" and response_status in ("failed", "cancelled", "incomplete"))):
                raise ProtocolError("对话服务返回错误事件", "remote_error")
            for choice in value.get("choices", []) if isinstance(value.get("choices", []), list) else []:
                delta = choice.get("delta", {}) if isinstance(choice, dict) else {}
                if isinstance(delta, dict):
                    self._text(delta.get("content"))
                    self._text(delta.get("reasoning_content") or delta.get("reasoning"), True)
            text_field = self.spec.get("text_pointer")
            if text_field:
                try:
                    self._text(store.pointer(value, text_field))
                except (KeyError, TypeError, IndexError, ValueError):
                    pass
            elif label in ("response.output_text.delta", "response.text.delta", "text.delta", "text"):
                self._text(value.get("delta") if isinstance(value.get("delta"), str) else value.get("text"))
            elif "text" in value and not value.get("choices"):
                self._text(value["text"], label.startswith("reasoning"))
            audio = self.spec.get("audio_output", {})
            audio_types = audio.get("event", ["response.audio.delta", "response.output_audio.delta"])
            if isinstance(audio_types, str):
                audio_types = [audio_types]
            if label in audio_types:
                try:
                    encoded = store.pointer(value, audio.get("field", "/delta"))
                    self.add_audio(base64.b64decode(encoded, validate=True))
                except (KeyError, TypeError, ValueError) as error:
                    raise ProtocolError("返回的音频事件没有有效 base64 数据") from error
        elif isinstance(value, str) and data != "[DONE]":
            self._text(value, event.startswith("reasoning"))
        if event.startswith("reasoning") and isinstance(value, dict) and isinstance(value.get("delta"), str):
            self._text(value["delta"], True)

    def add_audio(self, value: bytes):
        if not value:
            return  # empty deltas are not audio frames or a first-audio milestone
        self.audio.append(value)
        if sum(map(len, self.audio)) > self.max_bytes:
            raise ProtocolError("音频响应超过配置的 max_bytes")
        if self.metrics["first_audio_ms"] is None:
            self.metrics["first_audio_ms"] = round((time.monotonic() - self.started) * 1000)

    def finish(self) -> tuple[dict, list[dict]]:
        self.tick()
        if not self.complete:
            raise ProtocolError("连接已结束，但没有收到配置的对话完成信号", "stream_incomplete")
        self.metrics.update(total_ms=round((time.monotonic() - self.started) * 1000),
                            event_count=len(self.events), sent_audio_bytes=self.sent_audio_bytes)
        result = {"messages": self.events, "events": self.events, "text": "".join(self.text),
                  "reasoning": "".join(self.reasoning), "complete": True,
                  "audio_frames": len(self.audio), "audio_bytes": sum(map(len, self.audio)), "metrics": self.metrics}
        artifacts = []
        if self.audio:
            from ..artifacts import metadata, relative_name
            audio = self.spec.get("audio_output", {})
            name = audio.get("filename", "reply.wav")
            if not isinstance(name, str) or Path(name).name != name or relative_name(name) != name:
                raise ValueError("音频输出 filename 必须是文件名")
            root = Path(self.config.get("_output_root") or paths.local() / "protocol-artifacts").resolve()
            root.mkdir(parents=True, exist_ok=True)
            output = root / name
            if output.is_symlink():
                raise ValueError("音频输出不能是符号链接")
            from .primitives import output_file
            output = output_file(root, name)
            if audio.get("format", "pcm16") == "pcm16":
                if sum(map(len, self.audio)) % (2 * int(audio.get("channels", 1))):
                    raise ProtocolError("音频响应不是完整的 PCM16 采样帧")
                with wave.open(str(output), "wb") as stream:
                    stream.setnchannels(int(audio.get("channels", 1)))
                    stream.setsampwidth(2)
                    stream.setframerate(int(audio.get("sample_rate", 16000)))
                    stream.writeframes(b"".join(self.audio))
            else:
                output.write_bytes(b"".join(self.audio))
            artifacts.append(metadata(output, root=root, kind="audio"))
        self.tick()
        return result, artifacts


async def _sse_chunks(response, conversation):
    """Poll deadlines/cancellation even when the server sends no network data."""
    iterator = response.aiter_text().__aiter__()
    pending = None
    try:
        while True:
            conversation.tick()
            if pending is None:
                pending = asyncio.create_task(anext(iterator))
            done, _ = await asyncio.wait({pending}, timeout=min(.1, conversation.remaining()))
            if not done:
                continue
            current, pending = pending, None
            try:
                yield current.result()
            except StopAsyncIteration:
                return
    finally:
        if pending is not None:
            pending.cancel()
            with suppress(asyncio.CancelledError):
                await pending


async def _sse(client, case, url: str, sent: dict, config: dict, conversation: Conversation):
    import httpx
    spec = conversation.spec
    until = spec.get("until", "[DONE]")
    request = dict(sent)
    request.pop("timeout", None)
    headers = httpx.Headers(request.pop("headers", {}))
    headers.setdefault("Accept", "text/event-stream")
    timeout = httpx.Timeout(conversation.idle, connect=min(20, conversation.budget))
    response_headers, status = {}, 0
    async with httpx.AsyncClient(cookies=client.cookies, follow_redirects=False, trust_env=True,
                                verify=not config.get("ignore_https_errors"), timeout=timeout) as stream_client:
        async with asyncio.timeout(conversation.remaining()) as active_deadline:
            async with stream_client.stream(case["method"], url, headers=headers, **request) as response:
                response_headers, status = dict(response.headers), response.status_code
                client.cookies.update(stream_client.cookies)
                if not 200 <= status < 300:
                    await response.aread()
                    return httpx.Response(status, headers=response_headers, content=response.content), {}, []
                if "text/event-stream" not in response.headers.get("content-type", "").lower():
                    raise ProtocolError("SSE 接口没有返回 text/event-stream")
                event, data, identifier = "message", [], ""
                buffer = ""
                # iter_text preserves incremental UTF-8 decoding across network chunks.
                async for chunk in _sse_chunks(response, conversation):
                    conversation.tick()
                    conversation.bytes += len(chunk.encode("utf-8"))
                    if conversation.bytes > conversation.max_bytes:
                        raise ProtocolError("事件流超过配置的 max_bytes")
                    buffer += chunk
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line = line.rstrip("\r")
                        if line == "":
                            if data:
                                payload = "\n".join(data)
                                try:
                                    value = json.loads(payload)
                                except ValueError:
                                    value = payload
                                conversation.add(value, event, payload)
                                if matches(value, until) or matches({"event": event, "data": payload, "id": identifier}, until):
                                    conversation.complete = True
                                    break
                            event, data = "message", []
                        elif not line.startswith(":"):
                            field, separator, value = line.partition(":")
                            if value.startswith(" "):
                                value = value[1:]
                            if field == "event":
                                event = value
                            elif field == "data":
                                data.append(value)
                            elif field == "id" and "\x00" not in value:
                                identifier = value
                        conversation.tick()
                    if conversation.complete:
                        break
                    active_deadline.reschedule(time.monotonic() + conversation.remaining())
    result, artifacts = conversation.finish()
    return httpx.Response(status, headers=response_headers, json=result), conversation.metrics, artifacts


def _audio_chunks(spec: dict, config: dict):
    file = resolve_upload_assets(config.get("_asset_root") or paths.workspace(), [spec["file"]])[0]
    sent = 0
    if Path(file).suffix.lower() == ".wav":
        try:
            with wave.open(file, "rb") as stream:
                rate, channels, width = stream.getframerate(), stream.getnchannels(), stream.getsampwidth()
                if width != 2 or stream.getcomptype() != "NONE" or channels not in (1, 2) or not 8000 <= rate <= 192000:
                    raise ValueError("测试输入需要 8–192 kHz、单/双声道的未压缩 PCM16 WAV")
                expected = stream.getnframes() * channels * width
                if expected <= 0:
                    raise ValueError("测试音频不能是空文件")
                if "sample_rate" in spec and int(spec["sample_rate"]) != rate:
                    raise ValueError("音频 sample_rate 与 WAV 文件不一致")
                if "channels" in spec and int(spec["channels"]) != channels:
                    raise ValueError("音频 channels 与 WAV 文件不一致")
                frames = max(1, int(min(stream.getnframes(), rate * positive(spec.get("chunk_ms", 20), "chunk_ms") / 1000)))
                while chunk := stream.readframes(frames):
                    if len(chunk) % (channels * width):
                        raise ValueError("测试 WAV 缺少完整的 PCM16 采样帧")
                    sent += len(chunk)
                    yield chunk, len(chunk) / (rate * channels * width)
                if sent != expected:
                    raise ValueError("测试 WAV 数据被截断")
        except (wave.Error, EOFError) as error:
            raise ValueError("测试音频不是有效的 PCM16 WAV") from error
    else:
        rate = int(positive(spec.get("sample_rate", 16000), "sample_rate"))
        channels = int(spec.get("channels", 1))
        total = Path(file).stat().st_size
        if not total or total % (channels * 2):
            raise ValueError("测试 PCM 音频必须包含非空、完整的 PCM16 采样帧")
        size = max(1, int(min(total // (channels * 2), rate * positive(spec.get("chunk_ms", 20), "chunk_ms") / 1000))) * channels * 2
        with open(file, "rb") as stream:
            while chunk := stream.read(size):
                if len(chunk) % (channels * 2):
                    raise ValueError("测试 PCM 音频缺少完整的采样帧")
                sent += len(chunk)
                yield chunk, len(chunk) / (rate * channels * 2)
        if sent != total:
            raise ValueError("测试 PCM 音频在读取期间发生变化")
    if not sent:
        raise ValueError("测试音频没有可发送的采样帧")


class _SocketGuard:
    """Abort one existing socket on deadline/cancel, including a blocked send.

    An interrupted send is never retried: the peer may have received a prefix.
    """
    def __init__(self, connection, conversation):
        import threading

        self.connection, self.conversation = connection, conversation
        self.failure = None
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._watch, daemon=True, name="autoskill-protocol-deadline")
        self.thread.start()

    def _watch(self):
        import socket

        while not self.stopped.wait(.025):
            try:
                self.conversation.tick()
            except ProtocolError as error:
                self.failure = error
                sock = self.connection.sock
                if sock is not None:
                    with suppress(OSError):
                        sock.shutdown(socket.SHUT_RDWR)
                self.connection.shutdown()
                return

    def check(self):
        if self.failure is not None:
            raise self.failure
        self.conversation.tick()

    def close(self):
        self.stopped.set()
        self.thread.join(timeout=.2)


def _socket_read(sock, amount, conversation, *, eof=False):
    """Read exact HTTP handshake bytes without consuming WebSocket frames."""
    value = bytearray()
    while len(value) < amount:
        conversation.tick()
        sock.settimeout(min(.1, conversation.remaining()))
        try:
            chunk = sock.recv(min(16384, amount - len(value)))
        except TimeoutError:
            continue
        if not chunk:
            if eof:
                break
            raise ProtocolError("WebSocket 握手响应在完整读取前关闭", "stream_incomplete")
        value.extend(chunk)
    conversation.tick()
    return bytes(value)


def _socket_line(sock, conversation, limit=65536):
    value = bytearray()
    while not value.endswith(b"\r\n"):
        value.extend(_socket_read(sock, 1, conversation))
        if len(value) > limit:
            raise ProtocolError("WebSocket HTTP 响应头超过限制")
    return bytes(value[:-2])


def _handshake_body(sock, headers, conversation):
    """Keep rejected handshakes available for ordinary status/body assertions."""
    output = bytearray()
    if "chunked" in headers.get("transfer-encoding", "").lower():
        while True:
            try:
                size = int(_socket_line(sock, conversation).split(b";", 1)[0], 16)
            except ValueError:
                raise ProtocolError("WebSocket 握手响应的 chunked 长度无效") from None
            if size < 0 or len(output) + size > conversation.max_bytes:
                raise ProtocolError("WebSocket 握手响应超过 max_bytes")
            if size == 0:
                while _socket_line(sock, conversation):
                    pass
                return bytes(output)
            output.extend(_socket_read(sock, size, conversation))
            if _socket_read(sock, 2, conversation) != b"\r\n":
                raise ProtocolError("WebSocket 握手响应的 chunked 数据无效")
    if "content-length" in headers:
        try:
            size = int(headers["content-length"])
        except ValueError:
            raise ProtocolError("WebSocket 握手响应的 content-length 无效") from None
        if size < 0 or size > conversation.max_bytes:
            raise ProtocolError("WebSocket 握手响应超过 max_bytes")
        return _socket_read(sock, size, conversation)
    # Error/redirect responses without a framing header end at EOF.
    while True:
        chunk = _socket_read(sock, min(16384, conversation.max_bytes - len(output) + 1), conversation, eof=True)
        output.extend(chunk)
        if len(output) > conversation.max_bytes:
            raise ProtocolError("WebSocket 握手响应超过 max_bytes")
        if len(chunk) < 16384:
            return bytes(output)


def _open_websocket(client, url, options, headers, conversation):
    """Use websocket-client's TCP/proxy/TLS/frame support with an isolated handshake.

    Its default handshake follows redirects and uses a module-global CookieJar.
    This handshake sends exactly one request and uses only this HTTP client's
    cookie jar. Reading headers bytewise preserves any immediately queued frame.
    """
    import hashlib
    import hmac
    import httpx
    import threading
    from types import SimpleNamespace
    import websocket
    from websocket._http import connect, proxy_info

    connection = websocket.WebSocket(sslopt=options.get("sslopt", {}), enable_multithread=True)
    connection.settimeout(min(20, conversation.remaining()))
    guard = _SocketGuard(connection, conversation)
    completed = threading.Event()
    outcome = {}

    def connect_once():
        try:
            connection.sock, (hostname, port, resource) = connect(
                url, connection.sock_opt, proxy_info(http_proxy_timeout=min(20, conversation.remaining())), None)
            guard.check()
            host = "[" + hostname + "]" if ":" in hostname else hostname
            hostport = host if port in (80, 443) else host + ":" + str(port)
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            origin = options.get("origin", ("https://" if url.startswith("wss:") else "http://") + hostport)
            request = [("Host", options.get("host", hostport)), ("Upgrade", "websocket"), ("Connection", "Upgrade"),
                       ("Origin", origin), ("Sec-WebSocket-Key", key), ("Sec-WebSocket-Version", "13")]
            if options.get("subprotocols"):
                request.append(("Sec-WebSocket-Protocol", ", ".join(options["subprotocols"])))
            request.extend(headers.items())
            if options.get("cookie"):
                request.append(("Cookie", options["cookie"]))
            for name, value in request:
                if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name) or "\r" in value or "\n" in value:
                    raise ValueError("WebSocket 请求头名称或值无效")
            payload = "GET " + resource + " HTTP/1.1\r\n" + "\r\n".join(name + ": " + value for name, value in request) + "\r\n\r\n"
            connection.sock.settimeout(conversation.remaining())
            connection.sock.sendall(payload.encode("utf-8"))
            guard.check()
            status_line = _socket_line(connection.sock, conversation).decode("latin-1")
            match = re.fullmatch(r"HTTP/1\.[01] ([0-9]{3})(?: .*)?", status_line)
            if not match:
                raise ProtocolError("WebSocket 握手响应状态行无效")
            status = int(match[1])
            pairs, size = [], len(status_line)
            while line := _socket_line(connection.sock, conversation):
                size += len(line)
                if size > 65536:
                    raise ProtocolError("WebSocket HTTP 响应头超过限制")
                name, separator, value = line.partition(b":")
                if not separator or not re.fullmatch(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
                    raise ProtocolError("WebSocket 握手响应头无效")
                pairs.append((name.decode("ascii"), value.strip().decode("latin-1")))
            response_headers = httpx.Headers(pairs)
            # A rejection is an HTTP result, never permission to follow Location.
            if status != 101:
                body = b"" if status in (204, 304) or 100 <= status < 200 else _handshake_body(connection.sock, response_headers, conversation)
                connection.handshake_response = SimpleNamespace(status=status, headers=response_headers, body=body, subprotocol=None)
                connection.shutdown()
                return
            accepted = response_headers.get("sec-websocket-accept", "")
            expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()).decode("ascii")
            subprotocol = response_headers.get("sec-websocket-protocol")
            if ("websocket" not in [value.strip().lower() for value in response_headers.get("upgrade", "").split(",")]
                    or "upgrade" not in [value.strip().lower() for value in response_headers.get("connection", "").split(",")]
                    or not hmac.compare_digest(accepted, expected)
                    or (options.get("subprotocols") and subprotocol not in options["subprotocols"])
                    or (subprotocol and not options.get("subprotocols"))):
                raise ProtocolError("WebSocket 握手响应校验失败")
            connection.handshake_response = SimpleNamespace(status=status, headers=response_headers, body=b"", subprotocol=subprotocol)
            connection.connected = True
        except Exception as error:
            outcome["error"] = error
            connection.shutdown()
        finally:
            completed.set()

    threading.Thread(target=connect_once, daemon=True, name="autoskill-protocol-connect").start()
    try:
        while not completed.wait(.025):
            guard.check()
        guard.check()
        if "error" in outcome:
            raise outcome["error"]
        handshake = connection.handshake_response
        http_url = "https:" + url[4:] if url.startswith("wss:") else "http:" + url[3:]
        client.cookies.extract_cookies(httpx.Response(handshake.status, headers=handshake.headers, request=httpx.Request("GET", http_url)))
        return connection, guard
    except Exception:
        connection.shutdown()
        guard.close()
        raise


def _websocket(client, case, url: str, sent: dict, config: dict, conversation: Conversation):
    import httpx
    import websocket
    parts = urlsplit(url)
    supplied = urlencode(sent.get("params", {}), doseq=True)
    query = "&".join(value for value in (parts.query, supplied) if value)
    url = urlunsplit(("wss" if parts.scheme == "https" else "ws", parts.netloc, parts.path, query, ""))
    headers = httpx.Headers(sent.get("headers", {}))
    for name in ("cookie", "origin", "host"):
        if len(headers.get_list(name)) > 1:
            raise ValueError("WebSocket 请求头重复：" + name)
    cookie = headers.pop("cookie", "")
    if not cookie:
        request = httpx.Request("GET", "https://" + parts.netloc + parts.path if parts.scheme == "https" else "http://" + parts.netloc + parts.path)
        client.cookies.set_cookie_header(request)
        cookie = request.headers.get("cookie", "")
    options = {"redirect_limit": 0}
    for name in ("origin", "host"):
        if name in headers:
            options[name] = headers.pop(name)
    if any(name in headers for name in ("upgrade", "connection", "sec-websocket-key", "sec-websocket-version", "sec-websocket-protocol", "sec-websocket-accept")):
        raise ValueError("WebSocket 保留握手请求头不能覆盖；子协议请使用 transport.subprotocols")
    if cookie:
        options["cookie"] = cookie
    if config.get("ignore_https_errors"):
        import ssl
        options["sslopt"] = {"cert_reqs": ssl.CERT_NONE}
    elif os.environ.get("SSL_CERT_FILE"):
        options["sslopt"] = {"ca_certs": os.environ["SSL_CERT_FILE"]}
    subprotocols = conversation.spec.get("subprotocols")
    if subprotocols:
        options["subprotocols"] = subprotocols
    connection, guard = _open_websocket(client, url, options, headers, conversation)
    try:
        handshake = connection.handshake_response
        if handshake.status != 101:
            return httpx.Response(handshake.status, headers=handshake.headers, content=handshake.body), {}, []
        for step in conversation.spec["steps"]:
            guard.check()
            if "send" in step:
                conversation.complete = False
                connection.settimeout(conversation.remaining())
                value = step["send"]
                connection.send(json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value)
            elif "send_audio" in step:
                conversation.complete = False
                audio = step["send_audio"]
                for chunk, duration in _audio_chunks(audio, config):
                    guard.check()
                    connection.settimeout(conversation.remaining())
                    if audio.get("encoding", "base64") == "binary":
                        connection.send_binary(chunk)
                    else:
                        message = dict(audio.get("event", {"type": "input_audio_buffer.append"}))
                        message[audio.get("field", "audio")] = base64.b64encode(chunk).decode("ascii")
                        connection.send(json.dumps(message))
                    conversation.sent_audio_bytes += len(chunk)
                    if audio.get("realtime", False):
                        conversation.pause(duration)
            elif "receive" in step:
                receive = step["receive"]
                count, value = 0, None
                until = receive.get("until", conversation.spec.get("until"))
                minimum = int(receive.get("min_messages", 1))
                if minimum < 1:
                    raise ValueError("receive.min_messages 必须大于零")
                while True:
                    conversation.tick()
                    connection.settimeout(min(.1, conversation.remaining()))
                    try:
                        opcode, raw = connection.recv_data()
                    except websocket.WebSocketTimeoutException:
                        guard.check()
                        continue
                    except websocket.WebSocketConnectionClosedException:
                        guard.check()
                        raise ProtocolError("WebSocket 在满足接收条件前关闭", "stream_incomplete") from None
                    if opcode == websocket.ABNF.OPCODE_CLOSE or raw in (None, ""):
                        raise ProtocolError("WebSocket 在满足接收条件前关闭", "stream_incomplete")
                    if opcode == websocket.ABNF.OPCODE_BINARY:
                        conversation.bytes += len(raw)
                        if conversation.bytes > conversation.max_bytes:
                            raise ProtocolError("WebSocket 响应超过配置的 max_bytes")
                        conversation.last_event = time.monotonic()
                        conversation.add_audio(raw)
                        value = {"binary_bytes": len(raw)}
                        conversation.add(value)
                    else:
                        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
                        conversation.bytes += len(text.encode("utf-8"))
                        if conversation.bytes > conversation.max_bytes:
                            raise ProtocolError("WebSocket 响应超过配置的 max_bytes")
                        try:
                            value = json.loads(text)
                        except ValueError:
                            value = text
                        conversation.add(value)
                    count += 1
                    if count >= minimum and (until is None or matches(value, until)):
                        break
                for pointer, expected in receive.get("expect_json", {}).items():
                    try:
                        actual = store.pointer(value, pointer)
                    except (KeyError, IndexError, TypeError, ValueError):
                        raise ProtocolError("WebSocket 事件缺少字段 " + pointer) from None
                    if actual != expected:
                        raise ProtocolError("WebSocket 事件字段不符合期望：" + pointer)
                conversation.complete = True
            elif "close" in step:
                connection.close(timeout=min(.1, conversation.remaining()))
                guard.check()
        result, artifacts = conversation.finish()
        return httpx.Response(101, headers=handshake.headers, json=result), conversation.metrics, artifacts
    except Exception:
        guard.check()  # preserve deadline/cancellation over its socket-abort symptom
        raise
    finally:
        # Cleanup never starts another blocking close handshake after a result.
        connection.shutdown()
        guard.close()


def execute(client, case: dict, url: str, sent: dict, config: dict):
    spec = case.get("extensions", {}).get("transport", {})
    validate(spec)
    conversation = Conversation(spec, config)
    if spec["type"] == "websocket":
        return _websocket(client, case, url, sent, config, conversation)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_sse(client, case, url, sent, config, conversation))
    # Synchronous callers inside an async host still get an isolated, cancellable event loop.
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(_sse(client, case, url, sent, config, conversation))).result()
