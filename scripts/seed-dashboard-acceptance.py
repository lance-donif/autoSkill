"""Seed explicitly selected isolated data for real DSH acceptance without claiming host execution."""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))


def submit(workflow_id: str, payload: dict) -> None:
    from autoskill import workflow

    task = workflow.next_task(workflow_id, idempotency_key=uuid.uuid4().hex)["task"]
    workflow.submit(workflow_id, task["item_id"], lease_token=task["lease_token"], attempt_id=task["attempt_id"],
                    idempotency_key=uuid.uuid4().hex, payload=payload)


def requirement_fixture(site_url: str) -> dict:
    from autoskill import builder, db, projects, workflow
    from autoskill.core import store

    identity = "acceptance-requirements"
    found = db.one("SELECT id FROM workflow_runtime WHERE project_id = ? ORDER BY updated_at DESC LIMIT 1", (identity,))
    if found:
        return {"project_id": identity, "workflow_id": found["id"], "existing": True}
    url = urlsplit(site_url)
    alternate = "localhost" if url.hostname == "127.0.0.1" else "127.0.0.1"
    address = urlunsplit((url.scheme, alternate + (":" + str(url.port) if url.port else ""), "", "", ""))
    project_id = projects.create("需求迭代100例", address, project_id=identity)["id"]
    store.write(projects.path(project_id) / "analysis/requirements.json", [
        {"id": f"REQ-{number:03}", "module": "认证", "title": "未认证访问应被拒绝", "source": "合成独立认证规则"} for number in range(100)])
    cases = []
    for number in range(100):
        case = builder.api_case(f"CASE-{number:03}", f"认证业务规则 {number}", "GET", "/api/tickets",
                                {"status": 401, "json_equals": {"/code": "UNAUTHENTICATED"}})
        case["requirement_ids"] = [f"REQ-{number:03}"]
        cases.append(case)
    projects.save_cases(project_id, cases)
    flow = workflow.create(project_id, {"title": "认证规则原需求（前20项可变更）", "sources": [
        {"id": "changed", "text": "前20项：未认证访问工单接口应返回401和UNAUTHENTICATED"},
        {"id": "stable", "text": "其余80项：认证拒绝规则保持不变"}]}, idempotency_key="acceptance-baseline")["workflow_id"]
    submit(flow, {"requirements": [{"id": f"REQ-{number:03}", "text": "未认证请求返回401和UNAUTHENTICATED",
                                     "source_ids": ["changed" if number < 20 else "stable"]} for number in range(100)]})
    submit(flow, {"scenarios": [{"id": f"SC-{number:03}", "title": "未认证请求验证", "requirement_ids": [f"REQ-{number:03}"],
                                 "oracle_ids": [f"O-{number:03}"]} for number in range(100)],
                  "oracles": [{"id": f"O-{number:03}", "kind": "api", "requirement_ids": [f"REQ-{number:03}"],
                               "checks": case["assertions"]} for number, case in enumerate(cases)]})
    submit(flow, {"cases": [], "bindings": {f"SC-{number:03}": [case["id"]] for number, case in enumerate(cases)}})
    return {"project_id": project_id, "workflow_id": flow, "changed_source": "changed", "expected_direct_cases": 20, "expected_stable_cases": 80}


def failure_fixtures(project_id: str) -> list[dict]:
    from autoskill import builder, projects, runner

    known = {case["id"] for case in projects.cases(project_id)}
    if "TC-SEED-FAIL" not in known:
        case = copy.deepcopy(projects.case(project_id, "TC-API-006"))
        case.update(id="TC-SEED-FAIL", title="合成反例：声明402但目标实际401")
        case["assertions"]["status"] = 402
        blocked = builder.api_case("TC-SEED-BLOCKED", "合成阻塞：缺少前置变量", "GET", "/api/tickets",
                                   {"status": 200, "json_equals": {"/ok": True}}, headers={"Authorization": "Bearer {{SEED_MISSING_VARIABLE}}"})
        projects.save_cases(project_id, [case, blocked])
    results = []
    for case_id in ("TC-SEED-FAIL", "TC-SEED-BLOCKED"):
        run = runner.create(project_id, [case_id], headed=False)
        finished = runner.execute(run["id"])
        results.append({"case_id": case_id, "run_id": run["id"], "status": finished["status"]})
    return results


def native_fixture(project_id: str) -> dict:
    from autoskill import native_assets, paths

    asset_id = "acceptance-unittest"
    if any(item["id"] == asset_id for item in native_assets.list_assets(project_id)):
        return {"id": asset_id, "existing": True}
    folder = paths.local() / "acceptance-fixtures" / "unittest"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "test_business.py").write_text(
        "import unittest\n\nclass BusinessRule(unittest.TestCase):\n    def test_fixed_amount(self):\n        self.assertEqual(40 + 60, 100)\n", encoding="utf-8")
    asset = native_assets.import_asset(project_id, str(folder), "selenium-unittest", name="合成原生unittest", asset_id=asset_id)
    return {"id": asset["id"], "engine": asset["engine"], "source": str(folder)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed a deliberately isolated data root for real DSH acceptance")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--site-url", required=True, help="Running synthetic Ticket Desk site URL")
    args = parser.parse_args()
    os.environ["AUTOSKILL_WORKSPACE"] = str(Path(args.workspace).expanduser().resolve())
    os.environ["AUTOSKILL_HEADED"] = "0"
    from autoskill import db, demo, workflow_state

    db.init()
    workflow_state.init()
    project = demo.install(args.site_url)
    native = native_fixture(project["id"])
    failures = failure_fixtures(project["id"])
    requirements = requirement_fixture(args.site_url)
    print(json.dumps({"workspace": os.environ["AUTOSKILL_WORKSPACE"], "demo_project": project["id"], "native": native,
                      "controlled_runs": failures, "requirements": requirements,
                      "scope": "合成fixture预置；不代表DSH、模型或页面验收已完成"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
