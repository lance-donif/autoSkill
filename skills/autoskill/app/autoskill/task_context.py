"""Task identity shared by CLI commands and direct Python callers, independent of project storage."""
from __future__ import annotations

import hashlib
import os
from contextlib import contextmanager
from contextvars import ContextVar

_UNSET = object()
_session = ContextVar("autoskill_session", default=_UNSET)


def session_id() -> str | None:
    """An explicit context overrides the environment, including an explicit default session."""
    value = _session.get()
    if value is _UNSET:
        value = os.environ.get("AUTOSKILL_SESSION")
    return value or None


@contextmanager
def use_session(value: str | None):
    """Select a task for this context without changing other threads or process environment."""
    if value is not None and not isinstance(value, str):
        raise ValueError("会话标识需要是文字")
    token = _session.set(value or None)
    try:
        yield
    finally:
        _session.reset(token)


def session_key() -> str:
    """A filesystem-safe key; identifiers themselves never become directory or socket names."""
    value = session_id()
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32] if value is not None else "default"
