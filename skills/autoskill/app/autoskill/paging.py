"""Content-bound pagination shared by command line and HTTP readers."""
from __future__ import annotations

import base64
import hashlib
import json


class PageError(ValueError):
    """A cursor or page request that cannot represent the requested collection."""

    def __init__(self, message: str, code: str = "validation_error"):
        super().__init__(message)
        self.code = code
        self.details = {}


def revision(value) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _encode(value: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode().rstrip("=")


def position(cursor: str | None, content_revision: str, query: dict) -> int:
    if not cursor:
        return 0
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    except (ValueError, UnicodeError) as error:
        raise PageError("分页游标无效；请从第一页重新读取") from error
    if not isinstance(data, dict) or data.get("version") != 1 or type(data.get("offset")) is not int or data["offset"] < 0:
        raise PageError("分页游标无效；请从第一页重新读取")
    if data.get("revision") != content_revision or data.get("query") != revision(query):
        raise PageError("内容或查询条件已变化；请从第一页重新读取", "cursor_expired")
    return data["offset"]


def select(items: list, *, content_revision: str, query: dict, cursor: str | None = None,
           limit: int = 50, all_items: bool = False) -> tuple[list, dict]:
    if type(limit) is not int or limit < 1:
        raise PageError("每页数量需要是正整数")
    offset = position(cursor, content_revision, query)
    if all_items and cursor:
        raise PageError("--all 不能与分页游标同时使用")
    selected = items[offset:] if all_items else items[offset:offset + limit]
    end = offset + len(selected)
    more = end < len(items)
    next_cursor = _encode({"version": 1, "revision": content_revision, "query": revision(query), "offset": end}) if more else None
    return selected, {"total": len(items), "returned": len(selected), "limit": None if all_items else limit,
                      "has_more": more, "next_cursor": next_cursor, "content_revision": content_revision}
