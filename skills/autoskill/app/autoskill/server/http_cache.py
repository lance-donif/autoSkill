"""Weak comparison of validators for conditional GET and HEAD requests."""


def etag_matches(header: str, tag: str) -> bool:
    return any(item.strip().removeprefix("W/") in ("*", tag) for item in header.split(","))
