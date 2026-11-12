"""Turn captured traffic into candidate API requests. Raw HAR never leaves `.local` and is deleted after use."""
from __future__ import annotations

import json
import re
from urllib.parse import parse_qsl, urlsplit

from ..replay import ir

BUSINESS_TYPES = ("fetch", "xhr")
RESOURCE_TYPES = ("document", "script", "stylesheet")
SENSITIVE = re.compile(r"(authorization|cookie|token|secret|password|passwd|api[-_]?key|credential|session|csrf|signature)", re.I)
ID_KEY = re.compile(r"(id|token|key|code|uuid|no|sn)$", re.I)
ENUM_KEY = re.compile(r"^(code|status|state|type|role|result|success|ok|errno|error|priority|level)$", re.I)
MAX_DEPTH = 4


def _headers(items: list) -> dict:
    return {h["name"].lower(): h["value"] for h in items or []}


def entries(har: dict, base_origin: str) -> tuple[list[dict], list[str]]:
    """Normalised requests to the project's own origin, in recorded order, plus notes on what was left out."""
    found, notes, skipped_origins = [], [], set()
    for entry in har.get("log", {}).get("entries", []):
        request, response = entry["request"], entry["response"]
        url = request["url"]
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or request["method"] == "OPTIONS" or not response.get("status"):
            continue
        if ir.origin_of(url) != base_origin:
            skipped_origins.add(ir.origin_of(url))
            continue
        content = response.get("content", {})
        mime = (content.get("mimeType") or "").split(";")[0].strip().lower()
        kind = (entry.get("_resourceType") or "").lower()
        body = None
        post = request.get("postData") or {}
        body_note = ""
        if post.get("text"):
            if "json" in (post.get("mimeType") or "").lower():
                try:
                    body = json.loads(post["text"])
                except ValueError:
                    body_note = "请求体不是有效 JSON"
            else:
                body_note = "请求体不是 JSON（表单或文件），需人工确认"
        parsed = None
        if "json" in mime and content.get("text"):
            try:
                parsed = json.loads(content["text"])
            except ValueError:
                parsed = None
        found.append({
            "method": request["method"], "path": parts.path or "/", "params": dict(parse_qsl(parts.query, keep_blank_values=True)),
            "headers": _headers(request.get("headers")), "json": body, "body_note": body_note,
            "status": response["status"], "mime": mime, "response": parsed, "text": content.get("text") or "",
            "business": kind in BUSINESS_TYPES or (kind not in RESOURCE_TYPES and "json" in mime),
            "resource": kind in RESOURCE_TYPES,
        })
    if skipped_origins:
        notes.append("未生成用例的其他站点请求：" + "、".join(sorted(skipped_origins)))
    return found, notes


def infer_schema(value, depth: int = 0) -> dict:
    """Structure of a response: types and required keys, without any values."""
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    if isinstance(value, str):
        return {"type": "string"}
    if value is None:
        return {}
    if isinstance(value, list):
        schema: dict = {"type": "array"}
        if value and depth < MAX_DEPTH:
            schema["items"] = infer_schema(value[0], depth + 1)
        return schema
    schema = {"type": "object", "required": sorted(value)}
    if depth < MAX_DEPTH:
        # sensitive fields are required to exist, but nothing about them is written down
        schema["properties"] = {
            k: infer_schema(v, depth + 1) for k, v in value.items() if v is not None and not SENSITIVE.search(str(k))
        }
    return schema


def leaves(value, pointer: str = "", depth: int = 0):
    """(pointer, key, scalar) for every scalar of a JSON value; arrays contribute their first element only."""
    if isinstance(value, dict) and depth < MAX_DEPTH:
        for key, item in value.items():
            escaped = str(key).replace("~", "~0").replace("/", "~1")
            yield from leaves(item, f"{pointer}/{escaped}", depth + 1)
    elif isinstance(value, list) and depth < MAX_DEPTH:
        if value:
            yield from leaves(value[0], f"{pointer}/0", depth + 1)
    elif isinstance(value, (str, int, bool)) and value is not None:
        yield pointer, pointer.rsplit("/", 1)[-1], value


def stable_fields(response, sent) -> dict:
    """Fields worth asserting from one sample: values echoed from the request, and enum-like fields."""
    echoed = {v for _, _, v in leaves(sent)} if sent is not None else set()
    chosen = {}
    for pointer, key, value in leaves(response):
        if "/0" in pointer or SENSITIVE.search(key) or isinstance(value, str) and len(value) > 80:
            continue
        is_echo = isinstance(value, str) and value in echoed and len(value) >= 2
        is_enum = ENUM_KEY.match(key) and (isinstance(value, (bool, str)) or key.lower() in ("code", "errno", "status"))
        if is_echo or is_enum:
            chosen[pointer] = value
        if len(chosen) >= 6:
            break
    return chosen


def correlatable(response) -> list[tuple[str, str, object]]:
    """Response values a later request may reuse: ids, tokens and long opaque strings."""
    found = []
    for pointer, key, value in leaves(response):
        if isinstance(value, bool):
            continue
        if isinstance(value, str) and (len(value) >= 16 or (ID_KEY.search(key) and len(value) >= 4)):
            found.append((pointer, key, value))
        elif isinstance(value, int) and ID_KEY.search(key) and value >= 1000:
            found.append((pointer, key, value))
    return found
