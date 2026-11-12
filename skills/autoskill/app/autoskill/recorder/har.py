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
STRICT_ID = re.compile(r"(^|_)id$|[a-z]Id$|(^|_)ID$")  # plainly an identifier: id, user_id, userId
ENUM_KEY = re.compile(r"^(code|status|state|type|role|result|success|ok|errno|error|priority|level)$", re.I)
MAX_DEPTH = 4
SCHEMA_DEPTH = 2  # how deep the structure check goes: deeper detail changes too often to be worth asserting


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
        body = form = None
        post = request.get("postData") or {}
        body_note = ""
        if post.get("text"):
            if "json" in (post.get("mimeType") or "").lower():
                try:
                    body = json.loads(post["text"])
                except ValueError:
                    body_note = "请求体不是有效 JSON"
            elif "x-www-form-urlencoded" in (post.get("mimeType") or "").lower():
                form = dict(parse_qsl(post["text"], keep_blank_values=True))
            else:
                body_note = "请求体不是 JSON 或普通表单（例如文件上传），需人工确认"
        parsed = None
        if "json" in mime and content.get("text"):
            try:
                parsed = json.loads(content["text"])
            except ValueError:
                parsed = None
        found.append({
            "method": request["method"], "path": parts.path or "/", "params": dict(parse_qsl(parts.query, keep_blank_values=True)),
            "headers": _headers(request.get("headers")), "json": body, "form": form, "body_note": body_note,
            "status": response["status"], "mime": mime, "response": parsed, "text": content.get("text") or "",
            "business": kind in BUSINESS_TYPES or (kind not in RESOURCE_TYPES and "json" in mime),
            "resource": kind in RESOURCE_TYPES,
        })
    if skipped_origins:
        notes.append("未生成用例的其他站点请求：" + "、".join(sorted(skipped_origins)))
    return found, notes


WIDE = 12     # an object with more keys than this is treated as a map, not a record
SAMPLE = 50   # how many array items / map values are compared


def merge_schemas(schemas: list[dict]) -> dict:
    """What every sample has in common: a key is required only if all samples have it."""
    types = {schema.get("type") for schema in schemas}
    if len(types) != 1 or None in types:
        return {}  # mixed or unknown types: no constraint rather than a wrong one
    kind = types.pop()
    if kind == "array":
        items = merge_schemas([schema["items"] for schema in schemas if schema.get("items")])
        return {"type": "array", **({"items": items} if items else {})}
    if kind != "object":
        return {"type": kind}
    merged: dict = {"type": "object"}
    required = set.intersection(*[set(schema.get("required", [])) for schema in schemas])
    if required:
        merged["required"] = sorted(required)
    properties = {}
    for key in sorted(required):
        parts = [schema["properties"][key] for schema in schemas if key in schema.get("properties", {})]
        common = merge_schemas(parts) if len(parts) == len(schemas) else {}
        if common:
            properties[key] = common
    if properties:
        merged["properties"] = properties
    values = [schema["additionalProperties"] for schema in schemas if isinstance(schema.get("additionalProperties"), dict)]
    if values and len(values) == len(schemas):
        common = merge_schemas(values)
        if common:
            merged["additionalProperties"] = common
    if all("minProperties" in schema for schema in schemas):
        merged["minProperties"] = 1
    return merged


def infer_schema(value, depth: int = 0) -> dict:
    """Structure of a response: types and the keys that are always present, without any values."""
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, (int, float)):
        return {"type": "number"}  # 10 today may be 10.5 tomorrow
    if isinstance(value, str):
        return {"type": "string"}
    if value is None:
        return {}
    if isinstance(value, list):
        schema: dict = {"type": "array"}
        if value and depth < SCHEMA_DEPTH:
            items = merge_schemas([infer_schema(item, depth + 1) for item in value[:SAMPLE]])
            if items:
                schema["items"] = items
        return schema
    if len(value) > WIDE and all(isinstance(v, dict) for v in value.values()):
        # Many keys whose values are all objects is a map ("id -> record"), not a record: its keys change with
        # the data, so require it to be non-empty and describe what the records share. A record with many
        # scalar fields keeps its required keys below.
        schema = {"type": "object", "minProperties": 1}
        records = list(value.values())[:SAMPLE]
        if depth < SCHEMA_DEPTH:
            common = merge_schemas([infer_schema(v, depth + 1) for v in records])
            if common:
                schema["additionalProperties"] = common
        return schema
    schema = {"type": "object", "required": sorted(value)}
    if depth < SCHEMA_DEPTH:
        # sensitive fields are required to exist, but nothing about them is written down
        properties = {k: infer_schema(v, depth + 1) for k, v in value.items() if v is not None and not SENSITIVE.search(str(k))}
        properties = {k: v for k, v in properties.items() if v}
        if properties:
            schema["properties"] = properties
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
        elif isinstance(value, int) and (ID_KEY.search(key) and value >= 1000 or STRICT_ID.search(key) and value >= 1):
            found.append((pointer, key, value))  # small numbers only where the field is plainly an id
    return found


def drop_typeahead(found: list[dict]) -> list[dict]:
    """Search-as-you-type sends one request per keystroke; only the last says anything about the feature."""
    seen: dict[str, list] = {}
    for item in found:
        if item["method"] == "GET" and item["params"] not in seen.setdefault(item["path"], []):
            seen[item["path"]].append(item["params"])
    noisy = {path: variants[-1] for path, variants in seen.items() if len(variants) >= 3}
    return [item for item in found if item["method"] != "GET" or item["path"] not in noisy or item["params"] == noisy[item["path"]]]
