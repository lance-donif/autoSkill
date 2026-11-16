"""Keep generated definitions private until their own complete execution evidence qualifies them."""
from __future__ import annotations

import copy
import json
import re
import uuid
from pathlib import Path

from . import data_refs, db, execution_inputs, paths, projects
from .locking import project_lock


def _root() -> Path:
    return paths.local() / "case-candidates"


def _file(identity: str) -> Path:
    if not isinstance(identity, str) or not re.fullmatch(r"candidate-[a-f0-9]{32}", identity):
        raise ValueError("候选任务标识无效")
    return _root() / identity / "candidate.json"


def identity(project_id: str, source: dict) -> str:
    return "candidate-" + execution_inputs.content_hash({"project_id": project_id, "source": source})[:32]


def read(candidate_id: str) -> dict:
    try:
        return json.loads(_file(candidate_id).read_bytes())
    except FileNotFoundError as error:
        raise ValueError("找不到候选任务；请使用 record stop 或工作流返回的 candidate_id") from error


def next_case_id(project_id: str, kind: str) -> str:
    """Reserve generated identities across unpublished tasks as well as the reusable library."""
    prefix = "TC-UI-" if kind == "ui" else "TC-API-"
    number = int(projects.next_case_id(project_id, kind).removeprefix(prefix))
    for file in _root().glob("candidate-*/candidate.json"):
        data = json.loads(file.read_bytes())
        if data["project_id"] != project_id:
            continue
        for case in data["cases"]:
            suffix = case["id"].removeprefix(prefix)
            if case["id"].startswith(prefix) and suffix.isdigit():
                number = max(number, int(suffix) + 1)
    return f"{prefix}{number:03d}"


def unresolved_credentials() -> set[str]:
    """Captured secrets do not become user-declared credentials merely by being stored."""
    names = set()
    for file in _root().glob("candidate-*/candidate.json"):
        task = json.loads(file.read_bytes())
        for case in task["cases"]:
            names.update(case.get("extensions", {}).get("execution", {}).get("unresolved_recorded_credentials", []))
    return names


def stage(project_id: str, cases: list[dict], auth: dict, source: dict, *, candidate_id: str | None = None) -> dict:
    """Persist immutable candidate bodies and the compare-before-publication baseline."""
    task_id = candidate_id or identity(project_id, source)
    with project_lock(project_id):
        file = _file(task_id)
        if file.is_file():
            existing = read(task_id)
            if existing["cases"] != cases or existing["auth"] != auth:
                raise ValueError("候选任务已有不同内容；修改候选需要新的任务版本")
            return existing
        current = {case["id"]: case for case in projects.cases(project_id, active_only=False)}
        catalog = {**current, **{case["id"]: case for case in cases}}
        from . import runner

        closure = [case_id for case_id, _prerequisite in runner.build_plan([case["id"] for case in cases], catalog)]
        task = {"schema_version": 1, "id": task_id, "project_id": project_id, "created_at": db.now(),
                "cases": copy.deepcopy(cases), "auth": copy.deepcopy(auth), "original_auth": projects.auth(project_id),
                "source": copy.deepcopy(source), "batches": [],
                "expected_hashes": {case_id: execution_inputs.content_hash(current[case_id]) if case_id in current else None for case_id in closure}}
        execution_inputs.write_json(file, task)
        return task


def _attempt_failed(detail: dict) -> bool:
    for reference in detail.get("attempts", []):
        outcome = json.loads(data_refs.resolve(reference["path"]).read_bytes())["outcome"]
        if outcome.get("business_status") == "Fail" or outcome.get("performance_status") == "Fail":
            return True
    return False


def _row_qualification(case: dict, run: dict, row: dict, *, cold_api: bool) -> tuple[bool, str, str]:
    detail = json.loads(row["detail"]) if isinstance(row.get("detail"), str) else row.get("detail", {})
    if row.get("status") != "Pass" or detail.get("business_status") != "Pass":
        return False, row.get("reason") or "业务尚未通过，保留结果但不生成可复用用例", ""
    if detail.get("performance_status") == "Fail":
        return False, "声明的性能检查未通过", ""
    if not execution_inputs.result_evidence(run, row, case):
        return False, "实际执行证据缺失或与候选版本不一致", ""
    if _attempt_failed(detail):
        return False, "本批已有业务或性能失败，后续重试不能覆盖原失败", ""
    manifest = execution_inputs.load(run)
    if cold_api and case["kind"] == "api" and not manifest.get("api_only"):
        return False, "尚无独立 HTTP 冷启动证据；请对这些接口候选执行 --api-only", ""
    return True, "", manifest["scope_hashes"][case["id"]]


def _case_qualification(case: dict, runs: list[dict], repetitions: int, recording_observations: bool, cold_api: bool) -> str:
    if recording_observations and case.get("extensions", {}).get("recording_result", {}).get("status") == "Fail":
        return "本批录制已观察到业务失败，后续通过不能覆盖原失败"
    passed, scopes = 0, set()
    for run in runs:
        manifest = execution_inputs.load(run)
        from . import runner

        executed = {identity for identity, _ in runner.build_plan(manifest["selected"], manifest["cases"])}
        rows = db.rows("SELECT * FROM results WHERE run_id = ? AND case_id = ? ORDER BY id", (run["id"], case["id"]))
        if case["id"] not in executed and not rows:
            continue
        if run["status"] not in db.FINISHED:
            return "等待本批全部执行完成"
        if run["status"] == "Cancelled":
            return "本批执行已取消，保留已执行结果但不发布候选"
        if not rows:
            return "本批缺少这条用例的实际执行结果"
        if cold_api and case["kind"] == "api" and not manifest.get("api_only"):
            for row in rows:
                detail = json.loads(row["detail"])
                if detail.get("business_status") == "Fail" or detail.get("performance_status") == "Fail" or _attempt_failed(detail):
                    return row.get("reason") or "本批已有失败结果，不能被后续冷启动通过覆盖"
            continue
        for row in rows:
            ok, reason, scope = _row_qualification(case, run, row, cold_api=cold_api)
            if not ok:
                return reason
            passed += 1
            scopes.add(scope)
    if passed < repetitions:
        if not passed and cold_api and case["kind"] == "api":
            return "尚无独立 HTTP 冷启动通过证据；请对这些接口候选执行 --api-only"
        return f"需要 {repetitions} 次通过的实际执行证据，目前只有 {passed} 次"
    if len(scopes) != 1:
        return "本批执行输入范围变化，不能合并为同一次验证"
    return ""


def _dependency_closure(cases: dict, chosen: set[str], eligible: set[str], rejected: dict) -> set[str]:
    remaining = set(chosen)
    while True:
        blocked = {identity: next((dep for dep in [*projects.depends_on(cases[identity]),
                                                  *cases[identity].get("extensions", {}).get("postconditions", [])]
                                   if dep not in eligible), None) for identity in remaining}
        removed = {identity for identity, dependency in blocked.items() if dependency}
        if not removed:
            return remaining
        for identity in removed:
            relation = "前置用例" if blocked[identity] in projects.depends_on(cases[identity]) else "后置验证"
            rejected[identity] = f"{relation} {blocked[identity]} 未满足本批通过条件：" + rejected.get(blocked[identity], "依赖未完整验证")
        remaining -= removed
        eligible -= removed


def publish(project_id: str, candidates: list[dict], run_ids: list[str], *, repetitions: int = 1,
            expected_hashes: dict, recording_observations: bool = True, cold_api: bool = True,
            ordered_candidates: list[dict] | None = None) -> dict:
    """Publish only a passing dependency-closed subset; keep all other rows and evidence."""
    with project_lock(project_id):
        runs = [db.one("SELECT * FROM runs WHERE id = ?", (run_id,)) for run_id in dict.fromkeys(run_ids)]
        if any(run is None for run in runs):
            return {"published_ids": [], "unpublished": {case["id"]: "本批执行记录缺失" for case in candidates}}
        current = {case["id"]: case for case in projects.cases(project_id, active_only=False)}
        definitions = {**current, **{case["id"]: case for case in candidates}}
        candidate_ids = {case["id"] for case in candidates}
        from . import runner

        required = {identity for identity, _ in runner.build_plan(sorted(candidate_ids), definitions)}
        reasons = {}
        for identity in required:
            case = definitions[identity]
            try:
                reason = _case_qualification(case, runs, repetitions, recording_observations and identity in candidate_ids, cold_api)
            except (ValueError, OSError, KeyError, TypeError):
                reason = "本批冻结输入或执行证据不可用"
            if reason:
                reasons[case["id"]] = reason
        eligible = required - set(reasons)
        chosen = _dependency_closure(definitions, candidate_ids - set(reasons), eligible, reasons)
        closure = {identity for identity, _ in runner.build_plan(sorted(chosen), definitions)}
        expected = {identity: expected_hashes[identity] for identity in closure if identity in expected_hashes}
        ready = [case for case in candidates if case["id"] in chosen]
        unchanged = {case["id"] for case in ready if current.get(case["id"]) == case}
        changed = [case for case in ready if case["id"] not in unchanged]
        source = {case["id"]: case for case in ordered_candidates or candidates}
        if ordered_candidates is not None:
            source_order = [identity for identity, _ in runner.build_plan(list(source), {**current, **source}) if identity in source]
        else:
            source_order = list(dict.fromkeys(identity for run in runs for identity, _ in
                                              runner.build_plan(execution_inputs.load(run)["selected"], execution_inputs.load(run)["cases"])
                                              if identity in source))
        after = {**current, **{case["id"]: case for case in ready}}
        case_order = [identity for identity in source_order if after.get(identity) == source[identity]]
        reordered = [identity for identity in after if identity in case_order] != case_order
        for identity in unchanged:
            expected[identity] = execution_inputs.content_hash(current[identity])
        conflict = False
        try:
            if changed or reordered:
                projects.save_cases(project_id, changed, expected_hashes=expected, case_order=case_order)
        except (projects.CaseConflictError, ValueError) as error:
            reasons.update({case["id"]: str(error) for case in changed})
            chosen = unchanged
            conflict = isinstance(error, projects.CaseConflictError)
        return {"published_ids": sorted(chosen), "unpublished": {identity: reason for identity, reason in reasons.items() if identity in candidate_ids}, "conflict": conflict}


def _publish_auth(task: dict, published: set[str]) -> None:
    """Never point saved login configuration at a candidate that was not published."""
    current = projects.auth(task["project_id"])
    if current not in (task["original_auth"], task.get("published_auth")):
        return
    configured = copy.deepcopy(current)
    for key in ("ui_case", "api_case", "api_check_case"):
        if task["auth"].get(key) in published:
            configured[key] = task["auth"][key]
    if configured != current:
        projects.set_auth(task["project_id"], configured)
    task["published_auth"] = configured


def publish_auth(candidate_id: str, published_ids: list[str]) -> None:
    task = read(candidate_id)
    with project_lock(task["project_id"]):
        task = read(candidate_id)
        _publish_auth(task, set(published_ids))
        execution_inputs.write_json(_file(candidate_id), task)


def complete_run(run: dict) -> dict | None:
    link_file = _root() / "run-links" / (run["id"] + ".json")
    if not link_file.is_file():
        return None
    link = json.loads(link_file.read_bytes())
    task = read(link["candidate_id"])
    with project_lock(task["project_id"]):
        task = read(task["id"])
        batch = next(item for item in task["batches"] if item["id"] == link["batch_id"])
        candidates = [case for case in task["cases"] if case["id"] in batch["case_ids"]]
        receipt = publish(task["project_id"], candidates, batch["run_ids"], repetitions=batch["repetitions"],
                          expected_hashes=task["expected_hashes"], recording_observations=batch["recording_observations"], ordered_candidates=task["cases"])
        receipt["unpublished"].update(batch["preparation_errors"])
        complete = all(db.one("SELECT status FROM runs WHERE id = ?", (identity,))["status"] in db.FINISHED for identity in batch["run_ids"])
        receipt.update(status="complete" if complete else "pending", complete=complete)
        batch["publication"] = receipt
        _publish_auth(task, set(receipt["published_ids"]))
        execution_inputs.write_json(_file(task["id"]), task)
        return {"candidate_id": task["id"], "batch_id": batch["id"], **receipt}


def receipt(run_id: str) -> dict | None:
    link_file = _root() / "run-links" / (run_id + ".json")
    if not link_file.is_file():
        return None
    link = json.loads(link_file.read_bytes())
    task = read(link["candidate_id"])
    batch = next(item for item in task["batches"] if item["id"] == link["batch_id"])
    return {"candidate_id": task["id"], "batch_id": batch["id"], **batch["publication"]}


def pending_completions() -> list[dict]:
    """Find terminal runs whose deterministic completion receipt was interrupted."""
    pending = []
    for file in _root().glob("candidate-*/candidate.json"):
        task = json.loads(file.read_bytes())
        for batch in task["batches"]:
            if batch["publication"].get("complete") or not batch["run_ids"]:
                continue
            runs = [db.one("SELECT * FROM runs WHERE id = ?", (identity,)) for identity in batch["run_ids"]]
            if all(run and run["status"] in db.FINISHED for run in runs):
                pending.extend(runs)
    return pending


def design_bindings(run: dict, plan_case: str | None = None) -> list[tuple[str, dict, Path]]:
    from . import runner

    manifest = execution_inputs.load(run)
    bindings = []
    for identity, prerequisite in runner.build_plan(manifest["selected"], manifest["cases"]):
        case = manifest["cases"][identity]
        source = case.get("extensions", {}).get("test_case_document", {}).get("source", {})
        source = source if isinstance(source, dict) else {}
        design_id = source.get("case_id") if source.get("kind") == "case_plan" else plan_case if case["kind"] == "ui" and not prerequisite else None
        if design_id:
            folder = runner.plan_folder(source["file"]).parent if source.get("file") else runner.plan_folder(run["plan_path"])
            bindings.append((design_id, case, folder))
    return bindings


def bind_run(run: dict, *, candidate_id: str | None = None, plan_case: str | None = None) -> list[dict]:
    from . import plan

    receipts = []
    for design_id, case, folder in design_bindings(run, plan_case):
        receipts.append(plan.bind_execution(design_id, run["project_id"], case, run["id"], folder=folder, candidate_id=candidate_id))
        plan.sync(quiet=True, folder=folder, run_id=run["id"])
    return receipts


def queue(candidate_id: str, *, case_ids: list[str] | None = None, engine: str = "playwright", inputs: dict | None = None,
          api_only: bool = False, repetitions: int = 1, plan_case: str | None = None, start: bool = True,
          headed: bool | None = None, session_id: str | None = None, plan_path: str | Path | None = None) -> dict:
    """Use the existing queue for separate UI and independent HTTP candidate validation."""
    from . import runner

    task = read(candidate_id)
    with project_lock(task["project_id"]):
        task = read(candidate_id)
        selected = [case for case in task["cases"] if (not case_ids or case["id"] in case_ids) and (not api_only or case["kind"] == "api")]
        if not selected:
            raise ValueError("没有符合条件的候选用例")
        if case_ids and set(case_ids) - {case["id"] for case in selected}:
            raise ValueError("选择包含不在当前候选任务中的用例")
        requested_ids = {case["id"] for case in selected}
        catalog = {case["id"]: case for case in [*projects.cases(task["project_id"]), *task["cases"]]}
        required = {identity for identity, _ in runner.build_plan([case["id"] for case in selected], catalog)}
        selected = [case for case in task["cases"] if case["id"] in required and (not api_only or case["kind"] == "api")]
        if not isinstance(repetitions, int) or repetitions < 1:
            raise ValueError("验证次数必须为正整数")
        projects.sync_requirement_records(task["project_id"], [], candidate_cases=task["cases"])
        batch = {"id": "validation-" + uuid.uuid4().hex, "case_ids": [case["id"] for case in selected], "run_ids": [],
                 "repetitions": repetitions, "recording_observations": not task["batches"], "preparation_errors": {}}
        prepared = []
        for kind in ("ui", "api"):
            targets = [case["id"] for case in selected if case["kind"] == kind and case["id"] in requested_ids]
            if not targets:
                continue
            ready = []
            for identity in targets:
                try:
                    execution_inputs.capture(task["project_id"], [identity], engine, candidate_cases=task["cases"],
                                             candidate_auth=task["auth"], api_only=kind == "api", inputs=inputs, persist=False)
                    ready.append(identity)
                except ValueError as error:
                    batch["preparation_errors"][identity] = str(error)
            if not ready:
                continue
            try:
                prepared.extend(runner.prepare(task["project_id"], ready, engine, candidate_cases=task["cases"],
                                               candidate_auth=task["auth"], api_only=kind == "api", inputs=inputs, force_login=True,
                                               headed=headed, session_id=session_id, plan_path=plan_path) for _ in range(repetitions))
            except ValueError as error:
                batch["preparation_errors"].update({identity: str(error) for identity in ready})
        batch["run_ids"] = [run["id"] for run in prepared]
        batch["publication"] = {"status": "pending" if prepared else "complete", "complete": not prepared,
                                "published_ids": [], "unpublished": dict(batch["preparation_errors"])}
        task["batches"].append(batch)
        execution_inputs.write_json(_file(candidate_id), task)
        for run in prepared:
            execution_inputs.write_json(_root() / "run-links" / (run["id"] + ".json"), {"candidate_id": candidate_id, "batch_id": batch["id"]})
            runner.enqueue(run)
            bind_run(run, candidate_id=candidate_id, plan_case=plan_case)
    if start and prepared:
        from .server.jobs import ensure_worker

        ensure_worker()
    return {"candidate_id": candidate_id, "batch_id": batch["id"], "run_id": batch["run_ids"][0] if prepared else None,
            "run_ids": batch["run_ids"], **batch["publication"], "publication": batch["publication"],
            "note": "候选已保留；只有实际业务验证通过且依赖完整的用例才会进入可复用库"}
