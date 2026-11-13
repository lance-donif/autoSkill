"""Select observed business exchanges without changing their payloads or sending requests."""
from __future__ import annotations

import copy
import json
import re
from collections import Counter, defaultdict
from urllib.parse import urljoin, urlsplit

POLICY = "business-traffic-v1"
BUSINESS_TYPES = {"xhr", "fetch", "eventsource", "websocket"}
RESOURCE_TYPES = {"script", "stylesheet", "image", "font", "media", "manifest", "texttrack", "prefetch", "signedexchange"}
RESOURCE_DESTINATIONS = {"script", "style", "image", "font", "audio", "video", "track", "manifest", "audioworklet", "paintworklet", "xslt"}
AUTH_PATH = re.compile(r"(?:^|/)(?:login|signin|sign-in|auth|oauth2?|sso|session|token)(?:/|[._-]|$)", re.IGNORECASE)


def headers(values) -> dict[str, str]:
    if isinstance(values, dict):
        return {str(name).lower(): str(value) for name, value in values.items()}
    return {str(item.get("name", "")).lower(): str(item.get("value", "")) for item in values or []}


def _source_map(parts, response: dict) -> bool:
    if not parts.path.lower().endswith(".map"):
        return False
    try:
        value = json.loads(response.get("content", {}).get("text", ""))
    except (ValueError, TypeError):
        return False
    return isinstance(value, dict) and value.get("version") == 3 and isinstance(value.get("sources"), list) and "mappings" in value


def protocol(entry: dict) -> str:
    request, response = entry.get("request", {}), entry.get("response", {})
    received = headers(response.get("headers"))
    if (urlsplit(request.get("url", "")).scheme in ("ws", "wss") or str(entry.get("_resourceType", "")).lower() == "websocket"
            or received.get("upgrade", "").lower() == "websocket" or "_webSocketMessages" in entry):
        return "websocket"
    mime = response.get("content", {}).get("mimeType") or received.get("content-type", "")
    return "sse" if "text/event-stream" in mime.lower() else "http"


def category(entry: dict) -> str:
    """Prefer browser destination evidence to names; an explicit fetch may return any media type."""
    request, response = entry.get("request", {}), entry.get("response", {})
    parts = urlsplit(request.get("url", ""))
    kind = str(entry.get("_resourceType", "")).lower()
    method = str(request.get("method", "GET")).upper()
    sent, received = headers(request.get("headers")), headers(response.get("headers"))
    mime = str(response.get("content", {}).get("mimeType") or received.get("content-type", "")).lower().split(";", 1)[0].strip()
    if parts.scheme in ("ws", "wss") or kind == "websocket":
        return "business"
    if parts.scheme not in ("http", "https"):
        return "non_http"
    if kind == "preflight" or (method == "OPTIONS" and "access-control-request-method" in sent):
        return "cors_preflight"
    if kind in BUSINESS_TYPES or sent.get("x-requested-with", "").lower() == "xmlhttprequest":
        return "business"
    if received.get("upgrade", "").lower() == "websocket":
        return "business"
    destination = sent.get("sec-fetch-dest", "").lower()
    if kind in RESOURCE_TYPES or destination in RESOURCE_DESTINATIONS:
        return "page_resource"
    if mime == "text/event-stream" or "attachment" in received.get("content-disposition", "").lower():
        return "business"
    if kind in ("document", "iframe") or destination in ("document", "iframe", "frame"):
        if method not in ("GET", "HEAD", "OPTIONS"):
            return "authentication" if AUTH_PATH.search(parts.path) else "business"
        if response.get("status") in (401, 407) or (AUTH_PATH.search(parts.path) and ("set-cookie" in received or response.get("cookies"))):
            return "authentication"
        return "page_navigation"
    if method != "GET":
        return "business"
    if mime.startswith(("image/", "font/", "audio/", "video/")) or mime in (
            "text/css", "text/javascript", "application/javascript", "application/x-javascript", "application/wasm",
            "application/font-woff", "application/vnd.ms-fontobject", "application/manifest+json"):
        return "page_resource"
    if mime in ("text/html", "application/xhtml+xml"):
        return "page_navigation"
    if _source_map(parts, response):
        return "page_resource"
    # Missing browser metadata is not proof that an arbitrary suffix is a static resource.
    return "business"


def selected(entries: list[dict]) -> tuple[list[dict], dict]:
    """Keep complete observed redirect chains connected to business/authentication entries."""
    kinds = [category(entry) for entry in entries]
    kept = {index for index, kind in enumerate(kinds) if kind in ("business", "authentication")}
    links = defaultdict(set)
    groups = defaultdict(list)
    by_url = defaultdict(list)
    for index, entry in enumerate(entries):
        cdp = entry.get("_cdp") or {}
        if cdp.get("request_id") is not None:
            groups[(cdp.get("session_id"), cdp["request_id"])].append(index)
        by_url[entry.get("request", {}).get("url", "")].append(index)
    for group in groups.values():
        for index in group:
            links[index].update(group)
    for index, entry in enumerate(entries):
        response = entry.get("response", {})
        if not 300 <= (response.get("status") or 0) < 400:
            continue
        location = response.get("redirectURL") or headers(response.get("headers")).get("location")
        if not location:
            continue
        target = urljoin(entry.get("request", {}).get("url", ""), location)
        following = next((candidate for candidate in by_url[target] if candidate > index and
                          (not entry.get("pageref") or not entries[candidate].get("pageref") or entry["pageref"] == entries[candidate]["pageref"])), None)
        if following is not None:
            links[index].add(following)
            links[following].add(index)
    pending = list(kept)
    while pending:
        for related in links[pending.pop()] - kept:
            if kinds[related] not in ("cors_preflight", "non_http"):
                kept.add(related)
                pending.append(related)
    excluded = Counter(kind for index, kind in enumerate(kinds) if index not in kept)
    return [entry for index, entry in enumerate(entries) if index in kept], {
        "policy": POLICY, "complete_network_log": False, "observed_entries": len(entries),
        "retained_entries": len(kept), "excluded_entries": len(entries) - len(kept), "excluded_by_reason": dict(excluded),
    }


def filtered_har(value: dict) -> dict:
    result = copy.deepcopy(value)
    log = result.setdefault("log", {})
    log["entries"], scope = selected(log.get("entries", []))
    previous = log.get("_autoskill_capture_scope", {})
    log["_autoskill_capture_scope"] = previous if previous.get("policy") == POLICY and not scope["excluded_entries"] else scope
    # HAR page descriptors are navigation metadata, not an API exchange.
    log.pop("pages", None)
    return result


def filtered_cdp(value: dict) -> dict:
    result = copy.deepcopy(value)
    result["entries"], scope = selected(result.get("entries", []))
    previous = result.get("capture_scope", {})
    result["capture_scope"] = previous if previous.get("policy") == POLICY and not scope["excluded_entries"] else scope
    return result


def event_keys(entries: list[dict]) -> tuple[set[tuple], set[int]]:
    keys, replies = set(), set()
    for entry in entries:
        cdp = entry.get("_cdp") or {}
        if cdp.get("request_id") is not None:
            keys.add((cdp.get("session_id", ""), cdp["request_id"]))
        replies.update(entry.get("_capture_reply_ids", []))
    return keys, replies


def selected_event(message: dict, keys: set[tuple], replies: set[int]) -> bool:
    if "id" in message:
        return message["id"] in replies
    params = message.get("params", {})
    return (message.get("sessionId", ""), params.get("requestId")) in keys
