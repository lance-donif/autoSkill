"""Immutable local execution inputs and content-bound receipts for replay evidence."""
from __future__ import annotations

import copy
import hashlib
import importlib.metadata
import json
import os
import re
import sys
import uuid
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path

from . import __version__, data_refs, db, paths, projects, secrets, sessions
from .core import store
from .locking import named_lock, project_lock
from .replay import ir

# The adapter signals dispatch after preflight and any inline prerequisite, just before the target action.
on_target_start = ContextVar("on_target_start", default=None)


def mark_target_started() -> None:
    handler = on_target_start.get()
    if handler:
        handler()


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def content_hash(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def _folder() -> Path:
    return paths.local() / "execution-inputs"


def write_json(file: Path, value) -> None:
    """Publish a complete receipt atomically; independent attempts always have distinct names."""
    file.parent.mkdir(parents=True, exist_ok=True)
    temporary = file.with_name(f".{file.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(file)
    finally:
        temporary.unlink(missing_ok=True)


def _object(data: bytes) -> dict:
    digest = hashlib.sha256(data).hexdigest()
    file = _folder() / "objects" / digest
    if not file.is_file():
        file.parent.mkdir(parents=True, exist_ok=True)
        temporary = file.with_name(f".{digest}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(file)
        finally:
            temporary.unlink(missing_ok=True)
    elif hashlib.sha256(file.read_bytes()).hexdigest() != digest:
        raise ValueError("冻结输入文件已损坏，请重新生成执行输入")
    return {"sha256": digest, "size": len(data)}


@lru_cache(maxsize=64)
def _binary_hash(file: str, size: int, mtime: int, ctime: int) -> str:  # noqa: ARG001  # Metadata is the cache invalidation key.
    with Path(file).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _binary(file: str | None) -> dict | None:
    if not file:
        return None
    source = Path(file).resolve()
    stat = source.stat()
    return {"path": str(source), "sha256": _binary_hash(str(source), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)}


def _playwright_browser() -> str | None:
    chosen = paths.chrome()
    if chosen:
        return chosen
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            file = Path(playwright.chromium.executable_path)
            return str(file) if file.is_file() else None
    except (ImportError, OSError):
        return None


def engine_identity(engine: str, browser: bool) -> dict:
    """Pin adapter implementation, installed libraries and selected executable bytes."""
    package = Path(__file__).parent
    implementations = ["runner.py", "load_testing.py", "secrets.py", "execution_inputs.py", "replay/api.py", "replay/ir.py", "core/store.py"]
    libraries = ["httpx", "jsonschema"]
    runtime = {}
    if browser:
        implementations += ["replay/ui.py", "replay/tabs.py", f"replay/{engine}_driver.py"]
        libraries.append(engine)
        runtime = {"browser": _binary(_playwright_browser() if engine == "playwright" else paths.chrome(bundled=True)),
                   "fallback_browser": _binary(paths.chrome(bundled=True)) if engine == "playwright" else None,
                   "chromedriver": _binary(paths.chromedriver()) if engine == "selenium" else None}
        if engine == "selenium":
            implementations.append("replay/dom.js")
    versions = {}
    for name in libraries:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return {"adapter": engine if browser else "http", "autoskill": __version__, "python": sys.version,
            "packages": versions, "implementation": {name: hashlib.sha256((package / name).read_bytes()).hexdigest() for name in implementations},
            "runtime": runtime}


def _closure(chosen: list[str], cases: dict) -> list[str]:
    from .runner import build_plan

    return [case_id for case_id, _ in build_plan(chosen, cases)]


def _validate_candidates(items: list[dict]) -> None:
    import jsonschema

    schema = store.read(paths.SKILL / "references/case.schema.json")
    seen = set()
    for case in items:
        errors = list(jsonschema.Draft202012Validator(schema).iter_errors(case))
        if errors:
            raise ValueError("候选用例 Schema 不符：" + ", ".join(error.json_path for error in errors))
        if case["id"] in seen:
            raise ValueError("候选用例编号重复")
        seen.add(case["id"])
        if case["kind"] == "ui":
            ir.validate(case.get("extensions", {}).get("automation"))
        else:
            store._validate_saved_api_contract(case, jsonschema)


def _source_scope(name: str, ref: dict, data: bytes | None) -> dict | None:
    """Derived knowledge must not invalidate the execution that produced it."""
    parts = Path(name).parts
    if parts and parts[0] == "knowledge":
        return None
    if len(parts) >= 3 and parts[0] == "workflows":
        if parts[2] in ("knowledge.json", "diagnosis.json"):
            return None
        fields = {"definition.json": ("goal", "validation"), "materialization.json": ("bindings",)}.get(parts[2])
        if fields is not None:
            document = json.loads(data)
            return {"sha256": content_hash({key: document[key] for key in fields if key in document})}
    return {"sha256": ref["sha256"], "size": ref["size"]}


def _sources(project_id: str, persist: bool) -> dict:
    from . import domain

    generation = domain.manifest(project_id)
    head = generation.get("head")
    files, scope = {}, {}
    for name, ref in generation.get("files", {}).items():
        if name == "case-folders.json":
            continue
        projected = name.startswith("workflows/") and Path(name).name in ("definition.json", "materialization.json")
        data = domain.read_file(project_id, name, head=head) if persist or projected else None
        if persist:
            frozen = _object(data)
            if frozen["sha256"] != ref["sha256"]:
                raise ValueError("来源内容与指定版本不一致")
        files["domain/" + name] = {"sha256": ref["sha256"], "size": ref["size"]}
        relevant = _source_scope(name, ref, data)
        if relevant is not None:
            scope["domain/" + name] = relevant
    folder = projects.path(project_id)
    for relative in ("analysis", "knowledge"):
        directory = folder / relative
        if not directory.is_dir():
            continue
        for file in sorted(directory.rglob("*")):
            if file.is_file():
                data = file.read_bytes()
                files[file.relative_to(folder).as_posix()] = (_object(data) if persist else
                                                            {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})
                if relative != "knowledge":
                    scope[file.relative_to(folder).as_posix()] = files[file.relative_to(folder).as_posix()]
    return {"head": head, "files": files, "scope": scope}


def _auth_cases(auth: dict, cases: dict, api_only: bool) -> list[str]:
    keys = ("api_case", "api_check_case") if api_only else ("ui_case", "api_case", "api_check_case")
    return [auth[key] for key in keys if auth.get(key) in cases]


def _json_mapping(value, label: str) -> dict:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(label + "必须是 JSON 对象")
    try:
        return json.loads(canonical(value))
    except (TypeError, ValueError) as error:
        raise ValueError(label + "必须包含有效的 JSON 值") from error


def _inputs(value: dict | None) -> dict:
    values = _json_mapping(value, "运行输入") if value is not None else {}
    if "RAND" in values:
        raise ValueError("RAND 是每次运行自动生成的变量，运行输入不能覆盖它")
    return values


def _default_api_only(selected: list[str], cases: dict) -> bool:
    def declared(case):
        execution = case.get("extensions", {}).get("execution", {})
        return case["kind"] == "api" and isinstance(execution, dict) and execution.get("api_only") is True

    return bool(selected) and all(declared(cases[key]) for key in selected)


def _capture_ids(value) -> set[str]:
    """Read explicit capture identities or paths from declared source metadata only."""
    if isinstance(value, dict):
        return set().union(*(_capture_ids(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_capture_ids(item) for item in value))
    if not isinstance(value, str):
        return set()
    if re.fullmatch(r"capture-[A-Za-z0-9_-]+", value):
        return {value}
    parts = value.replace("\\", "/").split("/")
    for index, part in enumerate(parts[:-1]):
        if part == "conversations" and (index == 0 or parts[index - 1] == "domain"):
            return _capture_ids(parts[index + 1])
    return set()


def _case_sources(cases: dict, scope: dict) -> dict:
    referenced = set()
    for case in cases.values():
        extensions = case.get("extensions", {})
        document = extensions.get("test_case_document", {})
        declared = [case.get("source_capture"), case.get("source"), case.get("source_ref"), extensions.get("source_capture"),
                    document.get("source") if isinstance(document, dict) else None]
        referenced.update(_capture_ids(declared))
    return {name: ref for name, ref in scope.items()
            if not name.startswith("domain/conversations/") or name.split("/")[2] in referenced}


def _scope(cases: dict, case_id: str, config: dict, auth: dict, sources: dict, engines: dict, credentials: dict,
           *, api_only: bool = False, inputs: dict | None = None) -> str:
    selected = _closure([case_id], cases)
    extras = _auth_cases(auth, cases, api_only)
    selected = _closure(list(dict.fromkeys([*selected, *extras])), cases)
    subset = {key: cases[key] for key in selected}
    names = secrets.references([subset, config, auth, inputs])
    engine = engines["ui" if any(case["kind"] == "ui" for case in subset.values()) else "api"]
    scope = {"cases": subset, "config": config, "auth": auth, "sources": _case_sources(subset, sources["scope"]), "engine": engine,
             "credentials": {key: credentials.get(key) for key in sorted(names)}}
    if api_only or inputs:
        scope["execution"] = {"api_only": api_only, "inputs": inputs or {}}
    return content_hash(scope)


def capture(project_id: str, chosen: list[str] | None, engine: str, *, candidate_cases: list[dict] | None = None,
            candidate_auth: dict | None = None, api_only: bool = False, inputs: dict | None = None,
            persist: bool = True) -> dict:
    """Read one project generation under its lock; no runtime database mutation occurs here."""
    if not isinstance(api_only, bool):
        raise ValueError("api_only 必须是布尔值")
    values = _inputs(inputs)
    with project_lock(project_id):
        active = {case["id"]: case for case in projects.cases(project_id)}
        if candidate_cases is not None:
            _validate_candidates(candidate_cases)
            active.update({case["id"]: copy.deepcopy(case) for case in candidate_cases})
        selected = list(dict.fromkeys(chosen or [key for key, case in active.items()
                                                if ir.is_replayable(case) and (not api_only or case["kind"] == "api")]))
        missing = [key for key in selected if key not in active or active[key]["lifecycle"] != "active"]
        if missing:
            raise ValueError("这些用例不存在或已归档：" + "、".join(missing))
        not_ready = [key for key in selected if not ir.is_replayable(active[key])]
        if not_ready:
            raise ValueError("这些用例还没有可回放的步骤：" + "、".join(not_ready))
        if not selected:
            raise ValueError("项目里还没有可回放的用例；先录制一条")
        config = projects.config(project_id)
        auth = projects.auth(project_id) if candidate_auth is None else _json_mapping(candidate_auth, "候选登录配置")
        api_only = api_only or _default_api_only(selected, active)
        if api_only:
            auth = {key: value for key, value in auth.items() if key not in ("ui_case", "state_variables")}
        extras = _auth_cases(auth, active, api_only)
        included = _closure(list(dict.fromkeys([*selected, *extras])), active)
        cases = {key: active[key] for key in included}
        has_ui = any(case["kind"] == "ui" for case in cases.values())
        if api_only and has_ui:
            ui_cases = [key for key, case in cases.items() if case["kind"] == "ui"]
            raise ValueError("独立接口运行包含页面用例或页面依赖：" + "、".join(ui_cases) + "；请先补齐对应的 HTTP 登录或前置请求")
        if api_only:
            unresolved = [key for key in _closure(selected, active)
                          if isinstance(execution := active[key].get("extensions", {}).get("execution"), dict)
                          and execution.get("unresolved_recorded_credentials")]
            if unresolved:
                raise ValueError("独立接口运行仍依赖录制捕获的临时凭据：" + "、".join(unresolved) + "；请补齐 API 登录或明确输入来源后重新生成候选")
        identity = engine_identity("http" if api_only else engine, has_ui)
        identities = {"ui": identity, "api": engine_identity(engine, False) if has_ui else identity}
        sources = _sources(project_id, persist)
        names = secrets.references([cases, config, auth, values])
        credentials = {name: secrets.get_secret(name) for name in sorted(names)}
        scopes = {key: _scope(cases, key, config, auth, sources, identities, credentials, api_only=api_only, inputs=values) for key in cases}
        return {"schema_version": 1, "project_id": project_id, "selected": selected, "cases": cases,
                "case_hashes": {key: content_hash(case) for key, case in cases.items()}, "scope_hashes": scopes,
                "config": config, "auth": auth, "sources": sources, "engine": identity,
                "credentials": credentials, "saved_session": sessions.load(project_id) if persist and not api_only else {},
                "api_only": api_only, "inputs": values,
                "variables": {**copy.deepcopy(values), **({"RAND": uuid.uuid4().hex[:6]} if persist else {})}}


def freeze(project_id: str, chosen: list[str] | None, engine: str, *, candidate_cases: list[dict] | None = None,
           candidate_auth: dict | None = None, api_only: bool = False, inputs: dict | None = None,
           load: dict | None = None) -> tuple[dict, dict]:
    with project_lock(project_id), named_lock("input-store", "replay"):
        manifest = capture(project_id, chosen, engine, candidate_cases=candidate_cases, candidate_auth=candidate_auth,
                           api_only=api_only, inputs=inputs)
        if load is not None:
            manifest["load"] = copy.deepcopy(load)
        digest = content_hash(manifest)
        file = _folder() / (digest + "-" + uuid.uuid4().hex + ".json")
        write_json(file, manifest)
        return {"input_manifest": str(file), "input_hash": digest}, manifest


def load(run: dict, *, check_engine: bool = False) -> dict:
    """Reject missing or altered evidence; a hash alone never proves that execution happened."""
    try:
        manifest = json.loads(data_refs.resolve(run["input_manifest"]).read_bytes())
        if content_hash(manifest) != run["input_hash"] or manifest["project_id"] != run["project_id"]:
            raise ValueError("执行输入摘要不匹配")
        chosen = json.loads(run["case_ids"]) if isinstance(run.get("case_ids"), str) else run.get("case_ids")
        if chosen is not None and chosen != manifest["selected"]:
            raise ValueError("运行选择的用例与冻结输入不一致")
        for ref in manifest["sources"]["files"].values():
            data = (_folder() / "objects" / ref["sha256"]).read_bytes()
            if len(data) != ref["size"] or hashlib.sha256(data).hexdigest() != ref["sha256"]:
                raise ValueError("执行来源证据已损坏")
        if check_engine:
            current = engine_identity(run["engine"], any(case["kind"] == "ui" for case in manifest["cases"].values()))
            current["runtime"] = {key: _binary(ref["path"]) if ref else None for key, ref in manifest["engine"]["runtime"].items()}
            if current != manifest["engine"]:
                raise ValueError("执行引擎已在排队期间变更，请用当前引擎重新创建运行")
        return manifest
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("执行输入或来源证据缺失，不能将旧历史作为当前验证") from error


def _execution_interval(references: list[dict]):
    from datetime import datetime

    actual = [json.loads(data_refs.resolve(reference["path"]).read_bytes()) for reference in references
              if reference.get("kind") in {"case", "relogin_retry"}]
    if not actual:
        return None
    intervals = [(datetime.fromisoformat(receipt["started_at"]), datetime.fromisoformat(receipt["finished_at"])) for receipt in actual]
    if any(start > finish for start, finish in intervals):
        raise ValueError("执行回执的结束时间早于开始时间")
    return min(start for start, _ in intervals), max(finish for _, finish in intervals)


def _postcondition_order(source: list[dict], targets: list[dict], outcome: dict) -> bool:
    original = _execution_interval(source)
    following = []
    for target in targets:
        detail = json.loads(target["detail"]) if isinstance(target["detail"], str) else target["detail"]
        following.append(_execution_interval(detail["attempts"]))
    if outcome.get("business_status") == "Pass" and (original is None or any(interval is None for interval in following)):
        return False
    return original is None or all(interval is None or interval[0] >= original[1] for interval in following)


def _postcondition_evidence(run: dict, case: dict, manifest: dict, outcome: dict, references: list[dict],
                            outcomes: list[dict], seen: frozenset[str]) -> bool:
    from . import runner

    declared = case.get("extensions", {}).get("postconditions")
    targets = outcome.get("postcondition_results")
    source = outcome.get("postcondition_source")
    if not declared or not outcomes or not isinstance(source, dict) or not isinstance(targets, list):
        return False
    if any(not isinstance(target, dict) for target in targets) or [target.get("case_id") for target in targets] != declared:
        return False
    runner.validate_postconditions(manifest["cases"], selected=[case["id"]])
    attempts = [{"ref": reference, "outcome": previous} for reference, previous in zip(references, outcomes)]
    expected_source = runner.summarize_outcomes(case, attempts, outcomes[-1])
    if content_hash(source) != content_hash(expected_source):
        return False
    if any(not result_evidence(run, target, manifest["cases"][target["case_id"]], _seen=seen) for target in targets):
        return False
    return (_postcondition_order(references, targets, outcome)
            and content_hash(outcome) == content_hash(runner.postcondition_outcome(case, source, targets)))


def _attempt_outcomes(run: dict, result: dict, manifest: dict, attempts: list[dict], seen: frozenset[str]) -> list[dict]:
    frozen = manifest["cases"][result["case_id"]]
    folder = (paths.local() / "runs" / run["id"]).resolve()
    outcomes = []
    for position, ref in enumerate(attempts):
        file = data_refs.resolve(ref["path"])
        if not file.resolve().is_relative_to(folder / "attempts"):
            return []
        receipt = json.loads(file.read_bytes())
        if not isinstance(receipt, dict):
            return []
        if (content_hash(receipt) != ref["sha256"] or receipt["run_id"] != run["id"] or receipt["id"] != ref["id"]
                or receipt["input_hash"] != run["input_hash"] or receipt["case_content_hash"] != result["case_content_hash"]
                or receipt["case_id"] != result["case_id"] or receipt["state"] != "completed"
                or receipt.get("case_scope_hash") != manifest["scope_hashes"][result["case_id"]]):
            return []
        outcome = receipt["outcome"]
        if not isinstance(outcome, dict):
            return []
        if (ref.get("status") != outcome.get("status") or ref.get("business_status") != outcome.get("business_status")
                or ref.get("kind") != receipt.get("kind")):
            return []
        if receipt.get("kind") == "postcondition":
            if not _postcondition_evidence(run, frozen, manifest, outcome, attempts[:position], outcomes, seen):
                return []
        elif "postcondition_source" in outcome or "postcondition_results" in outcome:
            return []
        outcomes.append(outcome)
        for artifact in receipt.get("artifacts", []):
            file = (folder / artifact["path"]).resolve()
            if not file.is_relative_to(folder) or hashlib.sha256(file.read_bytes()).hexdigest() != artifact["sha256"]:
                return []
    return outcomes


def result_evidence(run: dict, result: dict, case: dict | None = None, *, _seen: frozenset[str] = frozenset()) -> bool:
    """Verify actual local attempt receipts, including candidate runs before their cases are published."""
    try:
        if result.get("run_id") != run["id"] or not result.get("input_hash") or result["input_hash"] != run["input_hash"]:
            return False
        manifest = load(run)
        frozen = manifest["cases"][result["case_id"]]
        if result["case_id"] in _seen:
            return False
        seen = _seen | {result["case_id"]}
        if result.get("case_content_hash") != content_hash(case or frozen) or content_hash(frozen) != result["case_content_hash"]:
            return False
        detail = json.loads(result["detail"]) if isinstance(result["detail"], str) else result["detail"]
        if not isinstance(detail, dict):
            return False
        if detail.get("case_scope_hash") != manifest["scope_hashes"][result["case_id"]]:
            return False
        attempts = detail.get("attempts", [])
        if not isinstance(attempts, list) or not attempts or any(not isinstance(ref, dict) for ref in attempts):
            return False
        outcomes = _attempt_outcomes(run, result, manifest, attempts, seen)
        if not outcomes:
            return False
        failed = next((outcome for outcome in outcomes if outcome.get("business_status") == "Fail"), None)
        last = outcomes[-1]
        selected = failed or last
        expected_status = "Fail" if failed else last.get("status")
        expected_business = "Fail" if failed else last.get("business_status", "Unverified")
        return (result.get("status") == expected_status and detail.get("business_status") == expected_business
                and detail.get("business_verified") == (expected_business == "Pass")
                and detail.get("execution_status") == last.get("execution_status")
                and detail.get("target_started") == last.get("target_started")
                and detail.get("execution_only_pass") == last.get("execution_only_pass")
                and (last.get("target_started") is None or all(detail.get(key) == (last.get(key) or None)
                     for key in ("steps", "request", "response")))
                and detail.get("performance_status") == last.get("performance_status")
                and detail.get("assertions", []) == selected.get("assertions", [])
                and detail.get("performance_assertions", []) == last.get("performance_assertions", [])
                and all(content_hash(detail.get(key)) == content_hash(last.get(key))
                        for key in ("postcondition_source", "postcondition_results", "setup_cases")))
    except (ValueError, KeyError, OSError, TypeError):
        return False


def result_scope_current(project_id: str, case: dict, result: dict, *, current: dict | None = None, run: dict | None = None) -> bool:
    """Compare input identity separately from the integrity of the attempt evidence."""
    try:
        run = run or db.one("SELECT * FROM runs WHERE id = ?", (result["run_id"],))
        if not run or result.get("case_content_hash") != content_hash(case):
            return False
        detail = json.loads(result["detail"]) if isinstance(result["detail"], str) else result["detail"]
        current = current or capture(project_id, [case["id"]], run["engine"], persist=False)
        expected = current["scope_hashes"].get(case["id"])
        return bool(expected and expected == detail.get("case_scope_hash"))
    except (ValueError, KeyError, OSError, TypeError):
        return False


def result_current(project_id: str, case: dict, result: dict, *, current: dict | None = None) -> bool:
    """Qualify retained evidence against both the receipt and the current input scope."""
    run = db.one("SELECT * FROM runs WHERE id = ?", (result["run_id"],))
    return bool(run and result_evidence(run, result, case) and result_scope_current(project_id, case, result, current=current, run=run))
