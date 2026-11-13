"""Extensible native execution registry; adapters expose their actual runtime and result capabilities."""
from __future__ import annotations

from .ai_eval import AiEvaluationAdapter
from .native import NativeAdapter

_registry: dict[str, object] = {}


def register(adapter) -> None:
    if adapter.id in _registry:
        raise ValueError(f"执行引擎已登记：{adapter.id}")
    _registry[adapter.id] = adapter


def get(engine: str):
    if engine not in _registry:
        raise ValueError(f"执行引擎不存在：{engine}")
    return _registry[engine]


def list_engines() -> list[dict]:
    return [adapter.capabilities() for adapter in _registry.values()]


def probe(engine: str | None = None) -> list[dict]:
    return [get(engine).probe()] if engine else [adapter.probe() for adapter in _registry.values()]


def discover(engine: str, root: str) -> list[dict]:
    return get(engine).discover(root)


for _id in ("selenium-pytest", "selenium-unittest", "postman-newman", "apifox", "jmeter"):
    register(NativeAdapter(_id))
register(AiEvaluationAdapter())
