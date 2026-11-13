"""Preserve HTTP conversation evidence and build explicitly mapped AI evaluation assets.

cURL input is parsed as text, never run through a shell. Recorded responses are
evidence, not independent answer oracles. Original requests, credentials and SSE
text stay in the captured source; replay substitutions are recorded separately.
"""
from __future__ import annotations

import base64
import copy
import datetime
import json
import re
import shlex
import uuid
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import quote_plus, urlsplit

from . import db, domain, native_assets
from .engines.ai_protocols import pointer
from .locking import project_lock
from .recorder import traffic

CAPTURE_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]*")
BODY_FLAGS = {"-d", "--data", "--data-raw", "--data-binary", "--data-urlencode", "--json"}
VALUE_FLAGS = {"-X", "--request", "-H", "--header", "--url", "-b", "--cookie", "-u", "--user", *BODY_FLAGS}
IGNORED_FLAGS = {"--compressed", "--insecure", "-k", "--location", "-L", "--silent", "-s", "--show-error", "-S", "--verbose", "-v", "--http1.1", "--http2"}


def _json(text: str):
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def _ansi_escape(match):
    token = match.group()[1:]
    simple = {"a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", "\\": "\\", "'": "'", '"': '"'}
    if token in simple:
        return simple[token]
    if token[0] in "xXuU":
        return chr(int(token[1:], 16))
    return chr(int(token, 8)) if token[0] in "01234567" else "\\" + token


def _curl_tokens(text: str) -> list[str]:
    def unquote(match):
        value = re.sub(r"\\(?:x[0-9a-fA-F]{1,2}|u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}|[0-7]{1,3}|.)", _ansi_escape, match.group(1))
        return shlex.quote(value)

    quoted = re.sub(r"\$'((?:[^'\\]|\\.)*)'", unquote, text.replace("\\\r\n", "").replace("\\\n", ""), flags=re.DOTALL)
    return shlex.split(quoted, posix=True)


def _curl_options(tokens: list[str]) -> tuple[list[tuple[str, str]], list[str]]:
    options, urls = [], []
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token.startswith(("http://", "https://")):
            urls.append(token)
        elif token in IGNORED_FLAGS:
            options.append((token, ""))
        elif token in ("-I", "--head"):
            options.append(("--request", "HEAD"))
        else:
            flag, equals, value = token.partition("=") if token.startswith("--") else (token, "", "")
            if flag not in VALUE_FLAGS:
                raise ValueError(f"尚不能映射 cURL 参数 {flag}；请使用 Chrome HAR 或先明确请求映射")
            if not equals:
                index += 1
                if index >= len(tokens):
                    raise ValueError(f"cURL 参数 {flag} 缺少值")
                value = tokens[index]
            options.append((flag, value))
        index += 1
    return options, urls


def _curl_request(options: list[tuple[str, str]], urls: list[str]) -> dict:
    headers, bodies, method = [], [], ""
    for flag, value in options:
        if flag in ("-X", "--request"):
            method = value.upper()
        elif flag == "--url":
            urls.append(value)
        elif flag in ("-H", "--header"):
            name, separator, content = value.partition(":")
            if not separator or not name:
                raise ValueError("cURL 请求头需要使用 名称:值 格式")
            headers.append({"name": name.strip(), "value": content.lstrip()})
        elif flag in BODY_FLAGS:
            if flag != "--data-raw" and value.startswith("@"):
                raise ValueError("cURL 文件请求体尚未附带内容，请导入 HAR 或提供实际请求体")
            bodies.append(_curl_body(flag, value))
            if flag == "--json":
                headers.extend([{"name": "Content-Type", "value": "application/json"}, {"name": "Accept", "value": "application/json"}])
        elif flag in ("-b", "--cookie"):
            if value and "=" not in value:
                raise ValueError("cURL Cookie 文件尚未附带内容，请导入 HAR 或提供实际 Cookie")
            headers.append({"name": "Cookie", "value": value})
        elif flag in ("-u", "--user"):
            headers.append({"name": "Authorization", "value": "Basic " + base64.b64encode(value.encode()).decode()})
    if len(urls) != 1 or urlsplit(urls[0]).scheme not in ("http", "https") or not urlsplit(urls[0]).netloc:
        raise ValueError("cURL 需要且只能包含一个完整 HTTP 地址")
    text = ("" if any(flag == "--json" for flag, _ in options) else "&").join(bodies)
    mime = next((item["value"] for item in reversed(headers) if item["name"].lower() == "content-type"), "")
    return {"method": method or ("POST" if bodies else "GET"), "url": urls[0], "headers": headers,
            **({"postData": {"mimeType": mime, "text": text}} if bodies else {})}


def _curl_body(flag: str, value: str) -> str:
    if flag != "--data-urlencode":
        return value
    name, separator, content = value.partition("=")
    if separator:
        return name + "=" + quote_plus(content)
    if "@" in value:
        raise ValueError("cURL 文件表单尚未附带内容，请导入 HAR 或提供实际请求体")
    return quote_plus(value)


def parse_curl(text: str) -> dict:
    """Parse a single bash-style Chrome cURL command without evaluating expansions."""
    tokens = _curl_tokens(text)
    if not tokens or Path(tokens[0]).name not in ("curl", "curl.exe"):
        raise ValueError("请输入以 curl 开头的请求命令")
    options, urls = _curl_options(tokens)
    return _curl_request(options, urls)


def _normal_request(entry: dict, number: int) -> dict:
    request, response = copy.deepcopy(entry["request"]), copy.deepcopy(entry.get("response", {}))
    post = request.get("postData") or {}
    content = response.get("content") or {}
    mime = content.get("mimeType", "").lower()
    protocol = traffic.protocol(entry)
    websocket = protocol == "websocket"
    body = _json(post.get("text", ""))
    response_json = _json(content.get("text", "")) if "json" in mime and not content.get("encoding") else None
    return {"number": number, "request": request, "response": response, "body_json": body, "response_json": response_json,
            "resource_type": entry.get("_resourceType", ""), "started_at": entry.get("startedDateTime"),
            "protocol": protocol, "transport_status": response.get("status"),
            "capture_source": entry.get("_capture_source", "har"), "request_headers_source": entry.get("_request_headers_source", entry.get("_capture_source", "har")),
            "response_body_source": entry.get("_body_source", entry.get("_capture_source", "har")), "body_complete": entry.get("_body_complete"),
            "request_body_complete": entry.get("_request_body_complete"), "request_body_note": entry.get("_request_body_note", ""),
            "capture_errors": copy.deepcopy(entry.get("_capture_errors", [])),
            **({"websocket": copy.deepcopy(entry.get("_websocket") or {"frames": entry.get("_webSocketMessages", [])})} if websocket else {}),
            "cdp": copy.deepcopy(entry.get("_cdp")), "source_links": copy.deepcopy(entry.get("_source_links", [])),
            "business_status": "Unverified"}


def _business(entry: dict) -> bool:
    return traffic.category(entry) in ("business", "authentication")


def _question(body, mapping: dict) -> tuple[str, str, bool]:
    path = mapping.get("question_pointer")
    if path:
        value = pointer(body, path)
        if not isinstance(value, str):
            raise ValueError("问题字段必须是文本，请提供明确 question_pointer")
        return value, path, False
    if not isinstance(body, dict):
        raise ValueError("无法从请求体识别对话问题，请提供 JSON 请求映射")
    matches = [(value, "/" + key, False) for key, value in body.items() if key in ("question", "prompt", "query") and isinstance(value, str)]
    if len(matches) == 1:
        return matches[0]
    if not matches and isinstance(body.get("messages"), list):
        users = [item for item in body["messages"] if isinstance(item, dict) and item.get("role") == "user" and isinstance(item.get("content"), str)]
        if users:
            return users[-1]["content"], "/messages", True
    raise ValueError("无法唯一识别问题字段，请提供 question_pointer")


def _candidates(requests: list[dict]) -> list[int]:
    found = []
    for request in requests:
        try:
            _question(request["body_json"], {})
        except ValueError:
            continue
        found.append(request["number"])
    return found


def _capture(project_id: str, requests: list[dict], source: bytes, source_name: str, details: dict, expected_head: str | None = None,
             *, attachments: dict[str, bytes] | None = None, primary: str | None = None, capture_scope: dict | None = None) -> dict:
    capture_id = "capture-" + uuid.uuid4().hex
    prefix = f"conversations/{capture_id}"
    blobs = {source_name: source, **(attachments or {})}
    sources = [{"path": f"{prefix}/{name}", "sha256": domain.content_hash(data), "size": len(data)} for name, data in blobs.items()]
    primary_source = next(ref for ref in sources if ref["path"] == f"{prefix}/{primary or source_name}")
    value = {"schema": 1, "id": capture_id, "project_id": project_id, "created_at": db.now(), "requests": requests,
             "source": primary_source, "sources": sources,
             "origin": details, "business_status": "Unverified", "conversation_candidates": _candidates(requests),
             **({"capture_scope": capture_scope} if capture_scope else {})}
    with project_lock(project_id):
        current = domain.manifest(project_id)
        expected = current["head"] if expected_head is None else expected_head
        changes = {f"{prefix}/{name}": data for name, data in blobs.items()}
        changes[f"{prefix}/capture.json"] = value
        committed = domain.commit(project_id, changes, expected,
                                  reason="保存业务通信请求与响应来源证据")
    return {"id": capture_id, "head": committed["head"], "path": f"{prefix}/capture.json", "requests": len(requests),
            "conversation_candidates": value["conversation_candidates"], "source_sha256": value["source"]["sha256"],
            **({"capture_scope": capture_scope} if capture_scope else {})}


def _request_key(entry: dict) -> tuple[str, str, str]:
    request = entry.get("request", {})
    body = request.get("postData", {}).get("text", "")
    try:
        normalized = domain.canonical_bytes(json.loads(body))
    except (json.JSONDecodeError, TypeError):
        normalized = str(body).encode("utf-8")
    url = urlsplit(request.get("url", ""))._replace(fragment="").geturl()
    return request.get("method", "").upper(), url, domain.content_hash(normalized)


def _body_known(entry: dict) -> bool:
    post = entry.get("request", {}).get("postData") or {}
    if "text" not in post:
        return False
    return "multipart/form-data" not in str(post.get("mimeType", "")).lower()


def _combined_entries(har: dict, evidence: dict, *, business_only: bool = True) -> list[dict]:
    """Join deterministic request occurrences while keeping each original source immutable."""
    cdp = []
    by_key = defaultdict(deque)
    by_address = defaultdict(list)
    for index, raw in enumerate(evidence.get("entries", [])):
        if business_only and not _business(raw):
            continue
        entry = copy.deepcopy(raw)
        entry["_source_links"] = [{"source": "source.cdp.json", "entry": index}]
        cdp.append(entry)
        by_key[_request_key(entry)].append(entry)
        by_address[_request_key(entry)[:2]].append(entry)
    result, used = [], set()
    for index, entry in enumerate(har.get("log", {}).get("entries", [])):
        if business_only and not _business(entry):
            continue
        identity = _request_key(entry)
        candidates = by_key[identity]
        while candidates and id(candidates[0]) in used:
            candidates.popleft()
        selected = candidates.popleft() if candidates else next((candidate for candidate in by_address[identity[:2]] if id(candidate) not in used and
                                                                 (not _body_known(entry) or not _body_known(candidate))), None)
        if selected is not None:
            used.add(id(selected))
            selected["_source_links"].append({"source": "source.har", "entry": index})
            if "postData" not in selected["request"] and "postData" in entry["request"]:
                selected["request"]["postData"] = copy.deepcopy(entry["request"]["postData"])
                selected["_request_body_source"] = "har"
            content = selected.get("response", {}).get("content", {})
            original = entry.get("response", {}).get("content", {})
            if not content.get("text") and original.get("text"):
                selected.setdefault("response", {})["content"] = copy.deepcopy(original)
                selected["_body_source"] = "har"
                selected["_body_complete"] = None
        else:
            selected = copy.deepcopy(entry)
            selected["_source_links"] = [{"source": "source.har", "entry": index}]
        result.append(selected)
    result.extend(entry for entry in cdp if id(entry) not in used)
    return _ordered_entries(result)


def compiler_source(har: dict, evidence: dict | None = None) -> dict:
    """Build a derived compiler input, including document authentication, without rewriting either raw source."""
    log = copy.deepcopy(har.get("log", {}))
    log["entries"] = _combined_entries(har, evidence or {}, business_only=False)
    return {"log": log}


def _ordered_entries(entries: list[dict]) -> list[dict]:
    try:
        stamps = [datetime.datetime.fromisoformat(entry["startedDateTime"]) for entry in entries]
    except (KeyError, ValueError, TypeError):
        return entries
    if any(stamp.tzinfo is None for stamp in stamps):
        return entries
    return [entry for _, entry in sorted(zip(stamps, entries), key=lambda pair: pair[0])]


def _network_sources(evidence: dict) -> dict[str, bytes]:
    if not evidence.get("source") and not evidence.get("entries") and not evidence.get("events_path"):
        return {}
    sources = {"source.cdp.json": domain.canonical_bytes(evidence)}
    if evidence.get("events_path"):
        keys, replies = traffic.event_keys(evidence.get("entries", []))
        with Path(evidence["events_path"]).open(encoding="utf-8") as events:
            sources["source.cdp.jsonl"] = "".join(line for line in events if traffic.selected_event(json.loads(line), keys, replies)).encode("utf-8")
    return sources


def capture(project_id: str, rec: dict, har: dict, *, source_har: bytes | None = None) -> dict:
    """Archive business projections and original received sources before temporary files are removed."""
    original_har = source_har if source_har is not None else domain.canonical_bytes(har)
    observed = rec.get("conversation_evidence") or {}
    original_entries = observed.get("original_entries", observed.get("entries", []))
    har = traffic.filtered_har(har)
    evidence = traffic.filtered_cdp({key: value for key, value in observed.items() if key not in ("original_entries", "original_events_path")})
    scope = {"policy": traffic.POLICY, "complete_network_log": False,
             "sources": {"har": har["log"]["_autoskill_capture_scope"], "cdp": evidence["capture_scope"]},
             "original_sources_preserved": True, "original_sources_scope": "录制器实际提供的 HAR/CDP，不承诺覆盖浏览器全部网络"}
    entries = _combined_entries(har, evidence, business_only=False)
    if not entries and not original_entries and not har["log"]["_autoskill_capture_scope"]["observed_entries"]:
        return {"requests": 0, "conversation_candidates": [], "capture_scope": scope}
    requests = [_normal_request(entry, number) for number, entry in enumerate(entries, 1)]
    details = {"kind": "recording", "session_id": rec.get("session_id"), "started_at": rec.get("started_at"), "start_url": rec.get("start_url"),
               "steps": copy.deepcopy(rec.get("steps", [])),
               "network_capture": {key: evidence.get(key) for key in ("source", "available", "error")}}
    attachments = _network_sources(evidence)
    attachments["source.original.har"] = original_har
    if observed.get("source") or original_entries:
        original_cdp = {key: value for key, value in observed.items() if key not in ("entries", "events_path", "original_entries", "original_events_path")}
        attachments["source.original.cdp.json"] = domain.canonical_bytes({**original_cdp, "entries": original_entries})
    original_events = observed.get("original_events_path") or observed.get("events_path")
    if original_events and Path(original_events).is_file():
        attachments["source.original.cdp.jsonl"] = Path(original_events).read_bytes()
    primary = "source.cdp.json" if any(entry.get("_capture_source") == "cdp_network" for entry in entries) else None
    return _capture(project_id, requests, domain.canonical_bytes(har), "source.har", details, attachments=attachments, primary=primary, capture_scope=scope)


def import_source(project_id: str, source: str | Path, *, source_format: str | None = None, name: str = "", expected_head: str | None = None) -> dict:
    file = Path(source)
    data = file.read_bytes()
    kind = source_format or ("har" if file.suffix.lower() in (".har", ".json") else "curl")
    if kind == "har":
        value = traffic.filtered_har(json.loads(data))
        entries = value["log"]["entries"]
        data = domain.canonical_bytes(value)
    elif kind == "curl":
        entries = [{"request": parse_curl(data.decode("utf-8")), "response": {}}]
    else:
        raise ValueError("对话来源格式需要是 curl 或 har")
    if not entries:
        raise ValueError("来源中没有可保存的业务 HTTP 请求")
    requests = [_normal_request(entry, number) for number, entry in enumerate(entries, 1)]
    scope = {"policy": traffic.POLICY, "complete_network_log": False, "sources": {"har": value["log"]["_autoskill_capture_scope"]}} if kind == "har" else None
    return _capture(project_id, requests, data, "source." + kind, {"kind": kind, "name": name or file.name}, expected_head, capture_scope=scope)


def get(project_id: str, capture_id: str, head: str | None = None) -> dict:
    if not CAPTURE_ID.fullmatch(capture_id or ""):
        raise ValueError("对话捕获标识无效")
    return domain.read_document(project_id, f"conversations/{capture_id}/capture.json", head)


def list_captures(project_id: str, *, limit: int = 50, offset: int = 0) -> dict:
    """List source captures from one immutable project generation without exposing request bodies."""
    if limit < 1 or offset < 0:
        raise ValueError("分页数量需要大于零，起点不能为负数")
    with project_lock(project_id):
        manifest = domain.manifest(project_id)
        items = []
        for name in manifest["files"]:
            parts = name.split("/")
            if len(parts) != 3 or parts[0] != "conversations" or parts[2] != "capture.json":
                continue
            capture = domain.read_document(project_id, name, manifest["head"])
            items.append({key: capture.get(key) for key in ("id", "created_at", "origin", "conversation_candidates", "business_status")}
                         | {"requests": len(capture["requests"]), "head": manifest["head"]})
        items.sort(key=lambda item: (item.get("created_at") or "", item["id"]), reverse=True)
        selected = items[offset:offset + limit]
        return {"items": selected, "total": len(items), "offset": offset, "limit": limit, "head": manifest["head"],
                "has_more": offset + len(selected) < len(items)}


def import_content(project_id: str, content: str, *, source_format: str, name: str = "", expected_head: str | None = None) -> dict:
    """Parse an uploaded or pasted source as data and preserve its exact UTF-8 source bytes."""
    if not isinstance(content, str) or not content.strip():
        raise ValueError("请上传或粘贴 cURL/HAR 内容")
    data = content.encode("utf-8")
    scope = None
    if source_format == "har":
        try:
            value = traffic.filtered_har(json.loads(data))
        except (json.JSONDecodeError, TypeError, KeyError, AttributeError) as error:
            raise ValueError("HAR 内容格式无效") from error
        entries = value["log"]["entries"]
        scope = {"policy": traffic.POLICY, "complete_network_log": False, "source_preserved": "uploaded-original",
                 "sources": {"har": value["log"]["_autoskill_capture_scope"]}}
    elif source_format == "curl":
        entries = [{"request": parse_curl(content), "response": {}}]
    else:
        raise ValueError("对话来源格式需要是 curl 或 har")
    if not entries:
        raise ValueError("来源中没有可保存的业务 HTTP 请求")
    requests = [_normal_request(entry, number) for number, entry in enumerate(entries, 1)]
    return _capture(project_id, requests, data, "source." + source_format,
                    {"kind": source_format, "name": name or "页面上传来源", "transport": "content"}, expected_head, capture_scope=scope)


def _replace(value, path: str, replacement):
    if path in ("", "/"):
        return replacement
    parts = [part.replace("~1", "/").replace("~0", "~") for part in path.lstrip("/").split("/")]
    parent = value
    for part in parts[:-1]:
        parent = parent[int(part)] if isinstance(parent, list) else parent[part]
    parent[int(parts[-1]) if isinstance(parent, list) else parts[-1]] = replacement
    return value


def _conversation_path(value) -> str | None:
    if not isinstance(value, dict):
        return None
    candidates = ["/" + key for key in ("conversation_id", "conversationId") if key in value]
    return candidates[0] if len(candidates) == 1 else None


def _headers(request: dict) -> dict:
    headers = {}
    for item in request.get("headers", []):
        key, value = item["name"], item["value"]
        if key.lower() in ("host", "content-length", "connection", "transfer-encoding") or key.startswith(":"):
            continue
        headers[key] = (headers[key] + ("; " if key.lower() == "cookie" else ", ") + value) if key in headers else value
    return headers


def _turn(request: dict, mapping: dict, number: int) -> dict:
    body = copy.deepcopy(mapping.get("body", request["body_json"]))
    question, question_path, history = _question(body, mapping)
    body = _replace(body, question_path, "{{messages}}" if history else "{{question}}")
    conversation_path = mapping.get("conversation_pointer") or _conversation_path(body)
    if conversation_path:
        body = _replace(body, conversation_path, "{{conversation_id}}")
    endpoint = {"url": request["request"]["url"], "method": request["request"]["method"], "headers": _headers(request["request"]),
                "body": body, "protocol": mapping.get("protocol", request["protocol"])}
    response_pointer = mapping.get("conversation_response_pointer") or _conversation_path(request["response_json"])
    if response_pointer:
        endpoint["extract"] = {"conversation_id": response_pointer}
    if mapping.get("answer_pointer"):
        endpoint["answer_pointer"] = mapping["answer_pointer"]
    elif endpoint["protocol"] == "http" and isinstance(request["response_json"], dict) and "answer" in request["response_json"]:
        endpoint["answer_pointer"] = "/answer"
    elif endpoint["protocol"] == "http" and request["response_json"] is not None:
        raise ValueError("无法识别回答字段，请提供 answer_pointer；录制响应不会自动变成预期答案")
    endpoint.update(copy.deepcopy(mapping.get("endpoint", {})))
    result = {"question": question, "endpoint": endpoint, "source_request": request["number"]}
    overrides = mapping.get("turn_overrides", [])
    if number < len(overrides):
        result.update(copy.deepcopy(overrides[number]))
    return result


def _mapping(value: dict | None) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("对话映射必须是对象")
    for key in ("question_pointer", "answer_pointer", "conversation_pointer", "conversation_response_pointer"):
        if key in value and (not isinstance(value[key], str) or not value[key].startswith("/")):
            raise ValueError(f"{key} 必须是以 / 开始的 JSON Pointer")
    overrides = value.get("turn_overrides", [])
    if not isinstance(overrides, list) or any(not isinstance(item, dict) for item in overrides):
        raise ValueError("turn_overrides 必须是对象数组")
    if not isinstance(value.get("endpoint", {}), dict):
        raise ValueError("endpoint 映射必须是对象")
    return copy.deepcopy(value)


def build_asset(project_id: str, capture_id: str, *, mapping: dict | None = None, asset_id: str | None = None,
                name: str = "", expected_head: str | None = None) -> dict:
    """Publish an AI evaluation spec and its source refs without inventing oracles."""
    mapping = _mapping(mapping)
    with project_lock(project_id):
        current = domain.manifest(project_id)
        expected = current["head"] if expected_head is None else expected_head
        captured = get(project_id, capture_id, current["head"])
        selected = mapping.get("turns", captured["conversation_candidates"])
        by_number = {request["number"]: request for request in captured["requests"]}
        if not selected:
            raise ValueError("没有可自动识别的对话轮次，请提供 turns 和 question_pointer 映射")
        if not isinstance(selected, list) or any(type(number) is not int or number not in by_number for number in selected):
            raise ValueError("turns 必须是来源中从 1 开始的请求序号数组")
        turns = [_turn(by_number[number], mapping, index) for index, number in enumerate(selected)]
        spec = {"endpoint": copy.deepcopy(turns[0]["endpoint"]), "samples": [{"id": capture_id, "title": name or "捕获的多轮对话", "turns": turns}],
                "load": mapping.get("load", {"concurrency": 1, "iterations": 1}), "source_capture": {"id": capture_id, "head": current["head"]}}
        for key in ("dataset", "performance"):
            if key in mapping:
                spec[key] = copy.deepcopy(mapping[key])
        asset_id = asset_id or "conversation-" + uuid.uuid4().hex
        if not native_assets.ASSET_ID.fullmatch(asset_id):
            raise ValueError("对话资产标识无效")
        prefix = f"native-assets/{asset_id}"
        data = domain.canonical_bytes(spec)
        asset = {"schema": 1, "id": asset_id, "project_id": project_id, "name": name or "捕获的多轮对话", "engine": "ai-eval", "argv": ["spec.json"], "cwd": ".",
                 "files": [{"path": "spec.json", "sha256": domain.content_hash(data), "size": len(data), "mode": 0o644}],
                 "origin": {"kind": "conversation_capture", "capture_id": capture_id, "head": current["head"], "source_sha256": captured["source"]["sha256"]},
                 "semantics": {"mode": "mapped", "converted": True, "mapping": mapping,
                               "notes": "保留原始请求证据；问题与会话标识动态绑定；录制响应不是独立预期"}}
        committed = domain.commit(project_id, {f"{prefix}/asset.json": asset, f"{prefix}/files/spec.json": data}, expected, reason="建立多轮对话验证与压测资产")
    return {**asset, "head": committed["head"], "asset_version": native_assets.digest(asset), "manifest": asset,
            "turns": len(turns), "verification": "unverified", "next": "补充独立预期或参考数据后，可执行同一资产的多轮验证与并发压测"}
