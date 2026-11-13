"""Frozen audio fixtures and duplex WebSocket frames for reproducible voice transport tests."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from pathlib import Path


def freeze(spec: dict, base: Path) -> None:
    endpoints = [spec["endpoint"], *[turn.get("endpoint", {}) for sample in spec["samples"] for turn in sample.get("turns", [sample])]]
    for endpoint in endpoints:
        audio = endpoint.get("audio_input")
        if not audio:
            continue
        if not isinstance(audio, dict) or not audio.get("file"):
            raise ValueError("语音输入需要 audio_input.file")
        data = (base / audio["file"]).read_bytes()
        if not data:
            raise ValueError("语音输入文件不能为空")
        chunk = audio.get("chunk_bytes", 4096)
        interval = audio.get("interval_ms", 0)
        if isinstance(chunk, bool) or not isinstance(chunk, int) or chunk < 1 or not isinstance(interval, (int, float)) or interval < 0:
            raise ValueError("音频分片字节数需要为正整数，分片间隔不能为负数")
        if audio.get("encoding", "binary") not in ("binary", "base64_json"):
            raise ValueError("音频编码需要是 binary 或 base64_json")
        endpoint["audio_input"] = {**audio, "data_base64": base64.b64encode(data).decode("ascii"),
                                   "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


async def send(socket, state):
    audio = state.endpoint.get("audio_input")
    if not audio:
        return
    data = base64.b64decode(audio["data_base64"], validate=True)
    sent_hash = hashlib.sha256()
    for chunks, offset in enumerate(range(0, len(data), audio.get("chunk_bytes", 4096)), 1):
        block = data[offset:offset + audio.get("chunk_bytes", 4096)]
        message = block
        if audio.get("encoding", "binary") == "base64_json":
            frame = {**audio.get("frame", {}), audio.get("field", "audio"): base64.b64encode(block).decode("ascii")}
            message = json.dumps(frame, ensure_ascii=False)
        await state.wait(socket.send(message))
        sent_hash.update(block)
        state.audio_sent = {"bytes": min(offset + len(block), len(data)), "chunks": chunks, "sha256": sent_hash.hexdigest(), "completed": False}
        if audio.get("interval_ms"):
            await state.wait(asyncio.sleep(audio["interval_ms"] / 1000))
    if audio.get("end_event") is not None:
        await state.wait(socket.send(json.dumps(audio["end_event"], ensure_ascii=False)))
    state.audio_sent["completed"] = True


def receive(state, data: bytes) -> None:
    state.touch()
    stamp = time.monotonic()
    state.first_audio = state.first_audio or stamp
    state.first_content = state.first_content or stamp
    state.last_content = stamp
    file = Path(state.endpoint["_audio_output"])
    file.parent.mkdir(parents=True, exist_ok=True)
    with file.open("ab" if state.audio_received["bytes"] else "xb") as stream:
        stream.write(data)
        stream.flush()
    state.audio_received["bytes"] += len(data)
    state.audio_received["chunks"] += 1
    state.audio_received["path"] = str(file)
    state.audio_hasher.update(data)
    state.audio_received["sha256"] = state.audio_hasher.hexdigest()


def receive_event(state, value):
    path = state.endpoint.get("audio_output_pointer")
    if not path:
        return
    from .ai_protocols import pointer

    try:
        data = pointer(value, path)
    except (KeyError, IndexError, ValueError, TypeError):
        return
    if isinstance(data, str) and data:
        receive(state, base64.b64decode(data, validate=True))
