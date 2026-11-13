"""Prepare immutable native requests and execute them through the shared run ownership and queue."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import uuid
from pathlib import Path

from . import data_refs, db, domain, engines, native_assets, paths, projects, secrets, task_context
from .engines.process import write_json
from .locking import named_lock, project_lock


def _capture_files(project_id: str, asset: dict, parameters: dict) -> tuple[list[dict], dict]:
    names = secrets.references(parameters) | secrets.references(asset["argv"])
    frozen = []
    objects = paths.local() / "native-inputs" / "objects"
    objects.mkdir(parents=True, exist_ok=True)
    for ref in asset["files"]:
        data = domain.read_file(project_id, f"native-assets/{asset['id']}/files/{ref['path']}", head=asset["head"])
        if len(data) != ref["size"] or hashlib.sha256(data).hexdigest() != ref["sha256"]:
            raise ValueError(f"原生资产校验失败：{ref['path']}")
        target = objects / ref["sha256"]
        if target.exists():
            if target.read_bytes() != data:
                raise ValueError("冻结对象内容校验失败")
        else:
            temporary = objects / ("." + uuid.uuid4().hex + ".tmp")
            try:
                temporary.write_bytes(data)
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
        frozen.append(dict(ref))
        names.update(secrets.references(data.decode("utf-8", errors="ignore")))
    with named_lock("credentials", "store"):
        values = {name: secrets.get_secret(name) for name in names}
    return frozen, values


def _probe(adapter, asset: dict) -> dict:
    return adapter.probe_asset(asset) if hasattr(adapter, "probe_asset") else adapter.probe()


def prepare(project_id: str, asset_id: str, *, head: str | None = None, parameters: dict | None = None,
            trigger: str = "manual", task_id: str | None = None, session_id: str | None = None,
            host: str = "generic", host_session_id: str = "") -> dict:
    """Freeze domain and runtime identities before enqueue; no subprocess test or run write occurs here."""
    with project_lock(project_id), named_lock("input-store", "native"):
        return _prepare(project_id, asset_id, head=head, parameters=parameters, trigger=trigger, task_id=task_id,
                        session_id=session_id, host=host, host_session_id=host_session_id)


def _prepare(project_id: str, asset_id: str, *, head: str | None, parameters: dict | None,
             trigger: str, task_id: str | None, session_id: str | None, host: str, host_session_id: str) -> dict:
    projects.path(project_id)
    asset = native_assets.get(project_id, asset_id, head)
    adapter = engines.get(asset["engine"])
    if parameters is not None and not isinstance(parameters, dict):
        raise ValueError("原生执行参数必须是对象")
    options = json.loads(json.dumps(parameters or {}, ensure_ascii=False, allow_nan=False))
    if not isinstance(host, str) or not host or not isinstance(host_session_id, str):
        raise ValueError("宿主类型和宿主会话标识需要是文本")
    frozen, credentials = _capture_files(project_id, asset, options)
    manifest = {"schema": 1, "family": "native", "project_id": project_id, "asset_id": asset_id,
                "head": asset["head"], "asset_version": asset["asset_version"], "asset": asset["manifest"],
                "parameters": options, "runtime": _probe(adapter, asset), "credential_values": credentials, "frozen_files": frozen,
                "runtime_context": {"host": host, "host_session_id": host_session_id},
                "planning": {"planned_unknown": True, "unit": "native_case", "reason": "原生测试数量由执行器发现和报告，不用文件数冒充用例数"},
                "freeze_scope": "domain 文件、入口 argv、显式参数及凭据引用；继承的系统环境与资产外部文件不是冻结输入"}
    input_hash = native_assets.digest(manifest)
    file = paths.local() / "native-inputs" / (input_hash + "-" + uuid.uuid4().hex + ".json")
    write_json(file, manifest)
    return {"id": db.new_id("run"), "family": "native", "project_id": project_id, "engine": asset["engine"],
            "trigger": trigger, "task_id": task_id, "status": "Queued", "case_ids": [asset_id], "total": 0,
            "created_at": db.now(), "headed": int(paths.headed()), "session_id": (session_id if session_id is not None else task_context.session_id()) or "",
            "input_manifest": str(file), "input_hash": input_hash}


def enqueue(project_id: str, asset_id: str, **options) -> dict:
    from . import runner

    return runner.enqueue(prepare(project_id, asset_id, **options))


def get(run_id: str) -> dict:
    from . import runner, task_reports, task_state

    with db.connect() as connection, task_state.projection():
        connection.execute("BEGIN")
        saved = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if saved is None or saved["family"] != "native":
            raise ValueError("原生运行不存在")
        run = dict(saved)
        rows = [dict(row) for row in connection.execute("SELECT * FROM results WHERE run_id = ? ORDER BY id", (run_id,))]
        for row in rows:
            row["detail"] = json.loads(row["detail"])
            row["state"] = task_state.result(row, connection=connection)
        targets = [row for row in rows if not row["prerequisite"]]
        values = task_state.run_values(run, results=targets, result_states=[row["state"] for row in targets], connection=connection)
        summary = paths.local() / "runs" / run_id / "native" / "outcome.json"
        outcome = json.loads(summary.read_text(encoding="utf-8")) if summary.is_file() else {}
        manifest = _frozen(run)
        return {**runner.public_run(values, state=values["state"]), "results": rows, "outcome": outcome, "evidence_dir": str(summary.parent),
                "test_report": task_reports.peek("run_" + run_id),
                "asset_id": manifest["asset_id"], "asset_version": manifest["asset_version"], "head": manifest["head"],
                "planned_unknown": manifest.get("planning", {}).get("planned_unknown", True) and outcome.get("metrics", {}).get("planned_cases") is None}


def execute(run_id: str, *, claim_token: str | None = None) -> dict:
    from . import runner

    return runner.execute(run_id, claim_token=claim_token)


def _cancelled(run_id: str) -> bool:
    row = db.one("SELECT cancel, status FROM runs WHERE id = ?", (run_id,))
    return not row or bool(row["cancel"]) or row["status"] != "Running"


def _frozen(run: dict, *, manifest: dict | None = None) -> dict:
    manifest = run["input_manifest"] if manifest is None else manifest
    if isinstance(manifest, str):
        manifest = json.loads(data_refs.resolve(manifest).read_text(encoding="utf-8"))
    if native_assets.digest(manifest) != run["input_hash"]:
        raise ValueError("原生执行输入清单校验失败")
    if manifest["project_id"] != run["project_id"] or manifest["asset"]["engine"] != run["engine"]:
        raise ValueError("原生执行输入与运行记录不匹配")
    return manifest


def _materialize(manifest: dict, destination: Path) -> dict:
    destination.mkdir(parents=True, exist_ok=True)
    for ref in manifest["frozen_files"]:
        data = (paths.local() / "native-inputs" / "objects" / ref["sha256"]).read_bytes()
        if len(data) != ref["size"] or hashlib.sha256(data).hexdigest() != ref["sha256"]:
            raise ValueError(f"冻结执行文件校验失败：{ref['path']}")
        target = destination / native_assets.relative(ref["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(ref.get("mode", 0o644))
    return {**manifest["asset"], "head": manifest["head"], "asset_version": manifest["asset_version"]}


def _run_adapter(run: dict, manifest: dict, folder: Path) -> dict:
    adapter = engines.get(run["engine"])
    if not manifest["runtime"]["available"]:
        raise ValueError(manifest["runtime"]["diagnosis"])
    current = _probe(adapter, manifest["asset"])
    if not current["available"]:
        raise ValueError(current["diagnosis"])
    if any(current.get(key) != manifest["runtime"].get(key) for key in ("command", "version")):
        raise ValueError("原生工具在入队后发生变化，请重新准备本次执行")
    asset = _materialize(manifest, folder / "input")
    if native_assets.digest(manifest["asset"]) != manifest["asset_version"]:
        raise ValueError("原生资产版本校验失败")
    with secrets.use_values(manifest.get("credential_values", {})):
        prepared = adapter.prepare({**asset, "_runtime": current}, folder / "input", folder, manifest["parameters"])
        runtime_context = manifest.get("runtime_context", {})
        prepared["context"] = {"run_id": run["id"], "project_id": run["project_id"], "session_id": run.get("session_id", ""), "input_hash": run["input_hash"],
                               "host": runtime_context.get("host", "generic"), "parameters": {"host_session_id": runtime_context.get("host_session_id", "")}}
        try:
            raw = adapter.execute(prepared, cancelled=lambda: _cancelled(run["id"]))
            return adapter.collect(prepared, raw)
        finally:
            adapter.cleanup(prepared)


def _evidence_folder(run: dict) -> Path:
    folder = paths.local().resolve() / "runs" / run["id"] / "native"
    if folder.resolve() != folder:
        raise ValueError("原生证据目录不属于当前运行，不能通过链接替换运行目录")
    return folder


def _file_identity(stat) -> tuple:
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _evidence_reference(folder: Path, reference) -> dict:
    supplied = {"path": reference} if isinstance(reference, str) else reference
    if not isinstance(supplied, dict) or not isinstance(supplied.get("path"), str) or not supplied["path"]:
        raise ValueError("原生证据引用需要非空文件路径")
    path = data_refs.resolve(supplied["path"]) if data_refs.is_absolute(supplied["path"]) else Path(supplied["path"])
    if ".." in path.parts:
        raise ValueError("原生证据引用不能离开当前运行目录")
    file = (path if path.is_absolute() else folder / path).resolve(strict=True)
    if not file.is_relative_to(folder) or file == folder / "outcome.json" or not file.is_file():
        raise ValueError("原生证据必须是当前运行目录内的报告或附件，不能引用汇总文件自身")
    name = file.relative_to(folder).as_posix()
    with file.open("rb") as stream:
        before = os.fstat(stream.fileno())
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        after = os.fstat(stream.fileno())
    if _file_identity(before) != _file_identity(after) or _file_identity(after) != _file_identity(file.stat()):
        raise ValueError("原生证据在绑定期间发生变化：" + name)
    if "sha256" in supplied and supplied["sha256"] != digest:
        raise ValueError("原生证据摘要与适配器记录不一致：" + name)
    if "size" in supplied and (type(supplied["size"]) is not int or supplied["size"] != after.st_size):
        raise ValueError("原生证据大小与适配器记录不一致：" + name)
    return {**supplied, "path": name, "sha256": digest, "size": after.st_size}


def _invalid_evidence(outcome: dict, error: Exception) -> dict:
    reason = "原生执行证据未能完整绑定：" + str(error)
    return {**outcome, "reported_status": outcome.get("reported_status", outcome["status"]),
            "reported_evidence": outcome.get("reported_evidence", outcome.get("evidence", [])),
            "status": "Blocked" if outcome["status"] == "Pass" else outcome["status"],
            "reason": "；".join(filter(None, [outcome.get("reason"), reason])),
            "evidence": [], "evidence_status": "invalid", "evidence_error": reason}


def _bind_evidence(run: dict, outcome: dict) -> dict:
    """Bind completed adapter evidence once; readers must never repair historical references."""
    try:
        references = outcome.get("evidence", [])
        if not isinstance(references, list):
            raise ValueError("原生证据引用需要是数组")
        if not references and outcome["status"] == "Pass":
            raise ValueError("成功执行没有提供可核查的原生报告")
        folder = _evidence_folder(run)
        frozen = [_evidence_reference(folder, reference) for reference in references]
        return {**outcome, "evidence": frozen, "evidence_status": "bound" if frozen else "unavailable"}
    except (OSError, ValueError, TypeError, RuntimeError) as error:
        return _invalid_evidence(outcome, error)


def _record(run: dict, outcome: dict) -> dict:
    cases = outcome.get("cases", [])
    if not cases and outcome["status"] == "Blocked":
        cases = [{"id": "runtime", "title": "原生执行环境", "status": "Blocked", "reason": outcome.get("reason", ""), "assertions": []}]
    counts = {"done": 0, "passed": 0, "failed": 0, "blocked": 0}
    for index, case in enumerate(cases, 1):
        status = case.get("status", "Blocked")
        counts["done"] += 1
        counts[{"Pass": "passed", "Fail": "failed"}.get(status, "blocked")] += 1
        assertions = case.get("assertions", [])
        verified = (bool(assertions) and status == "Pass" and all(item.get("passed") for item in assertions)
                    and case.get("verdict_scope") not in ("performance", "execution"))
        business_failed = assertions and status == "Fail" and case.get("verdict_scope") not in ("performance", "execution")
        business = case.get("business_status", "Pass" if verified else "Fail" if business_failed else "Unverified")
        detail = {**case, "input_hash": run["input_hash"], "engine": run["engine"],
                  "business_status": business, "business_verified": business == "Pass",
                  "verification": case.get("verification", "verified" if business == "Pass" else "failed" if business == "Fail" else "unverified"),
                  "evidence": outcome.get("evidence", []),
                  **{key: outcome[key] for key in ("evidence_status", "evidence_error") if key in outcome}}
        db.insert("results", {"run_id": run["id"], "project_id": run["project_id"], "case_id": str(case.get("id", index)),
                              "case_title": case.get("title", str(index)), "kind": "native", "engine": run["engine"],
                              "status": status, "reason": case.get("reason", ""), "category": "native", "actual": "",
                              "duration_ms": round(case.get("duration_ms", 0)), "started_at": run.get("started_at") or db.now(),
                              "case_revision": 1, "case_content_hash": run["input_hash"], "input_hash": run["input_hash"], "detail": detail})
    planned = outcome.get("metrics", {}).get("planned_cases") or 0
    db.update("runs", run["id"], {"total": max(len(cases), planned), **counts})
    return counts


def execute_claimed(run: dict) -> dict:
    """Called only after runner acquired the row and installed the task context."""
    folder = None
    try:
        destination = _evidence_folder(run)
        destination.mkdir(parents=True, exist_ok=True)
        folder = destination
        outcome = ({"status": "Cancelled", "reason": "", "cases": []} if _cancelled(run["id"])
                   else _run_adapter(run, _frozen(run), folder))
    except (OSError, ValueError, KeyError, TypeError) as error:
        outcome = {"status": "Blocked", "reason": str(error), "cases": [], "execution_status": "blocked", "verification": "unverified"}
    except Exception as error:  # noqa: BLE001  # A third-party adapter failure must finish the durable run and preserve diagnostics.
        outcome = {"status": "Blocked", "reason": "原生适配器出错：" + type(error).__name__, "cases": [], "execution_status": "blocked"}
    outcome = _bind_evidence(run, outcome)
    if folder is not None:
        try:
            write_json(_evidence_folder(run) / "outcome.json", outcome)
        except (OSError, ValueError, RuntimeError) as error:
            outcome = _invalid_evidence(outcome, error)
    counts = _record(run, outcome)
    db.finish_run(run["id"], os.getpid(), counts, outcome["status"], outcome.get("reason", ""))
    return db.one("SELECT * FROM runs WHERE id = ?", (run["id"],))


def cancel(run_id: str) -> dict:
    from . import deletion
    from .server import jobs

    get(run_id)
    jobs.cancel(run_id)
    with contextlib.suppress(OSError):
        deletion._native_active(run_id)
    return get(run_id)
