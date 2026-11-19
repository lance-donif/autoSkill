"""Create portable Pages reports from frozen sample inputs, without running any cases."""
from __future__ import annotations

import base64
import hashlib
import json
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from autoskill import execution_inputs, reporting, task_workbook, task_workbook_data

ROOT = Path(__file__).resolve().parents[1]
MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
HISTORIC = "reports/evidence/2026-11-13-dsh-live/runoob-jcyf-final/summary.json"
# Functional designs authored for the two fictional demonstration projects.
SAMPLE_DESIGNS = {
    "ST-UI-001": ("打开库存查询页并查看库存查询入口。", "页面显示库存搜索框。"),
    "ST-API-001": ("查询示例商品的可售库存。", "查询成功，可售库存为 18。"),
    "ST-API-002": ("查询示例商品的安全库存阈值。", "查询成功，安全库存阈值为 20。"),
    "ST-API-003": ("使用准备好的仓库编号查询仓库详情。", "仓库详情查询成功。"),
    "ST-API-004": ("查看仓库列表。", "列表首项为「上海一仓」。"),
    "ST-API-005": ("查询库存服务的健康状态。", "库存服务正常响应健康检查。"),
    "ST-API-006": ("使用示例账号登录库存系统。", "登录成功，返回角色 viewer。"),
    "TC-UI-005": ("在未登录状态下打开登录页面。", "页面显示登录表单的提交按钮。"),
    "TC-API-006": ("在未登录状态下查询工单列表。", "系统拒绝访问，返回 UNAUTHENTICATED。"),
    "TC-API-007": ("在未登录状态下查询工单列表并检查提示。", "系统要求登录，返回 LOGIN_REQUIRED。"),
    "TC-API-008": ("使用准备好的工单编号查询工单详情。", "工单详情查询成功。"),
    "TC-API-009": ("查询公开的工单分类列表。", "列表首项为「售后」。"),
    "TC-API-010": ("查询工单服务的健康状态。", "工单服务正常响应健康检查。"),
}
PREFIXES = {"可售库存": "KSKC", "库存管理": "KCGL", "仓库管理": "CKGL", "账户": "ZH", "接口校验": "JKJY",
            "登录验证": "DLYZ", "工单查询": "GDCX", "库存查询": "KCCX", "库存预警": "KCYJ", "仓库": "CK",
            "服务": "FW", "登录": "DL", "工单": "GD"}


def frozen_observations(data: dict) -> tuple[dict, dict]:
    if "/observations/runoob" not in data:
        return {}, {}
    archive = json.loads((ROOT / "scripts/fixtures/runoob-frozen-definitions.json").read_text(encoding="utf-8"))
    summary = json.loads((ROOT / HISTORIC).read_text(encoding="utf-8"))
    listings = data.get("/observations/runoob", {})
    records = listings.get("previous_cases", []) + listings.get("cases", [])
    observed = {(item["run_id"], item["case_id"]): item for item in records}
    frozen = {}
    for run_id, source in archive.items():
        for case in source["definitions"]:
            record = observed[run_id, case["id"]]
            if record["artifact_hashes"]["case_sha256"] != execution_inputs.content_hash(case):
                raise ValueError(f"Frozen case hash mismatch for {run_id}/{case['id']}")
            frozen[run_id, case["id"]] = {"definition": case, "workbook_sha256": source["workbook_sha256"],
                                         "case_sha256": record["artifact_hashes"]["case_sha256"]}
    # The archived workbook includes workstation paths. Retain public functional fields and
    # original hashes in the display edition, without publishing private source references.
    historical = observed[summary["run_id"], summary["case_id"]]
    source = summary["test_case_document"]
    doc = {key: value for key, value in source.items() if key != "source"}
    case = deepcopy(data[f"/case-definitions/runoob/{summary['case_id']}"]["case"])
    case.update(module=doc["module"], steps=doc["steps"].splitlines(), expected=doc["expected"], preconditions=doc["preconditions"])
    case["extensions"]["test_case_document"] = doc
    frozen[summary["run_id"], summary["case_id"]] = {"definition": case, "workbook_sha256": historical["artifact_hashes"]["workbook_sha256"],
                                                     "case_sha256": summary["case_content_hash"]}
    return observed, frozen


def case_design(data, project, cid, path, evidence, *, frozen_run=None):
    original = data[f"/case-definitions/{project}/{cid}"]["case"]
    case = deepcopy(original)
    if project == "runoob":
        latest = next(row for row in data["/observations/runoob"]["cases"] if row["case_id"] == cid)
        exact = evidence[frozen_run or latest["run_id"], cid]["definition"]
        if frozen_run:
            return deepcopy(exact)
        doc = deepcopy(exact["extensions"]["test_case_document"])
        doc.update(steps=case["steps"], expected=case["expected"])
    else:
        step, expected = SAMPLE_DESIGNS[cid]
        conditions = {"ST-API-003": "已准备示例仓库编号。", "TC-API-008": "已准备示例工单编号。",
                      "ST-API-006": "已准备示例登录账号。"}
        needs_guest = cid in {"TC-UI-005", "TC-API-006", "TC-API-007"}
        doc = {"description": "验证" + case["title"] + "。", "steps": [step], "expected": expected,
               "author": "autoSkill 示例", "remarks": "合成项目的示例功能设计。",
               "preconditions": conditions.get(cid, "用户未登录。" if needs_guest else "可访问示例项目。"),
               "prefix": PREFIXES.get(path[-1], "")}
    case["extensions"]["test_case_document"] = doc
    return case


def report_item(case, result, observed, frozen):
    item = {"definition": case, "result": None, "run_id": "", "evidence_valid": False, "environment": [], "note": ""}
    if not result:
        return item
    record = {key: result[key] for key in ("id", "run_id", "project_id", "case_id", "status", "reason", "actual", "duration_ms",
              "started_at", "case_revision", "category", "prerequisite") if key in result}
    detail = result["detail"]
    # Real source files and mock fixture API replies can contain host paths and process IDs.
    # Preserve the relevant public checks and timing in the display edition.
    record["detail"] = {key: detail[key] for key in ("assertions", "performance_assertions", "business_status", "target_started",
                       "execution_only_pass", "artifact_hashes") if key in detail}
    key = record["run_id"], record["case_id"]
    if key in observed:
        observation = observed[key]
        if observation["outcome"] == "pass" and observation["checks"] and all(check["passed"] for check in observation["checks"]):
            record["detail"]["business_status"] = "Pass"
        if screenshot := observation["artifact_hashes"].get("screenshot_sha256"):
            record["screenshot"] = "sha256://" + screenshot
        item["note"] = (f"公开观察展示版；原用例定义 SHA-256 {frozen[key]['case_sha256']}；"
                        f"原报告 SHA-256 {frozen[key]['workbook_sha256']}；来源：{observation['source_url']}")
        item["environment"] = [observation["url"], observation["engine"], observation["checked_at"]]
    else:
        item["note"] = "示例结果来自合成输入。"
        item["environment"] = ["Pages 合成示例", result.get("engine", "")]
    item.update(result=record, run_id=record["run_id"], evidence_valid=result["state"]["evidence"] == "valid")
    return item


def snapshot(project, identity, kind, title, items, *, paths=None, notes=None, metadata=None, sources=None):
    info = {key: project[key] for key in ("id", "name", "base_url")}
    results = [item["result"] for item in items if item["result"]]
    scope = {"report_business_paths": paths} if paths is not None else {}
    return {"schema": 1, "kind": kind, "task_id": identity, "project_id": info["id"], "project": info, "title": title,
            "status": "Exported", "session_id": "", "run_ids": list(dict.fromkeys(row["run_id"] for row in results)),
            "cases": items, "results": results, "sources": {**scope, **(sources or {})}, "notes": notes or [],
            "metadata": metadata or {}}


def binary_report(path, state):
    reporting.write_task(state, path)
    raw = path.read_bytes()
    return {"filename": path.name, "media_type": MIME, "base64": base64.b64encode(raw).decode("ascii"),
            "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}


def collect_sources(data, state):
    rows, aux = task_workbook_data.rows(state)
    sources = {}
    pid = state["project_id"]
    for row in rows:
        group = row["group"]
        case = group["definition"]
        case_id = case["id"]
        document = task_workbook_data.document(case)
        record = data[f"/case-definitions/{pid}/{case_id}"]
        sources[case_id] = {key: row[key] for key in ("description", "preconditions", "steps", "expected", "author", "step_mapping")}
        sources[case_id].update(document=deepcopy(document), original_path=group["original_business_path"], business_path=document.get("business_path", []),
                       definition_hash=execution_inputs.content_hash(record["case"]), content_revision=record["content_revision"],
                       report_definition_hash=group["definition_hash"], revision=case["revision"], prefix=document.get("prefix", ""),
                       remarks=document.get("remarks", ""), data=[d for d in aux if d["id"].startswith(row["id"] + "/")])
    data["/workbook-sources/" + pid] = {"cases": sources}


def project_report(data, project, observed, frozen, dest):
    pid = project["id"]
    cases = [c for c in data["/cases"]["items"] if c["project_id"] == pid]
    organization = data[f"/projects/{pid}/folders"]
    paths = reporting._export_paths(project, [c["case_id"] for c in cases], organization)
    items = []
    for c in cases:
        cid = c["case_id"]
        case = case_design(data, pid, cid, paths[cid], frozen)
        run_id = ((c.get("state") or {}).get("scope") or {}).get("run_id")
        run = data.get(f"/runs/{run_id}", {})
        result = next((r for r in run.get("results", []) if r["case_id"] == cid and r["case_revision"] == case["revision"]), None)
        items.append(report_item(case, result if c["current"] else None, observed, frozen))
    state = snapshot(project, "export-" + pid, "project_export", "项目测试用例导出", items, paths=paths,
                     sources={"organization": organization})
    collect_sources(data, state)
    path = dest / (project["name"] + "-测试用例.xlsx")
    data[f"/projects/{pid}/report"] = binary_report(path, state)
    return path


def plan_report(data, project, observed, frozen, dest):
    pid = project["id"]
    plan = data["/case-plans/" + pid]
    if not plan.get("exists"):
        return
    report_id = plan["test_report"]["report_id"]
    items = []
    for row in plan["rows"]:
        cid = row["id"]
        case = case_design(data, pid, cid, [row["module"]], frozen)
        items.append(report_item(case, None, observed, frozen))
    state = snapshot(project, plan["task_id"], "design", "功能测试用例设计", items,
                     notes=["冻结的示例设计稿，未执行任何用例。"])
    book = binary_report(dest / (project["name"] + "-用例设计.xlsx"), state)
    data["/case-plans/" + pid + "/excel"] = book
    data[f"/task-reports/{report_id}/excel"] = book


def run_report(data, run, project, observed, frozen, dest):
    run_id, pid = run["id"], project["id"]
    items, original_hashes = [], {}
    for result in run["results"]:
        cid = result["case_id"]
        path = [data[f"/case-definitions/{pid}/{cid}"]["case"]["module"]]
        case = case_design(data, pid, cid, path, frozen, frozen_run=run_id)
        items.append(report_item(case, result, observed, frozen))
        if observation := observed.get((run_id, cid)):
            original_hashes[cid] = {**observation["artifact_hashes"], "case_sha256": frozen[run_id, cid]["case_sha256"]}
    source = {}
    if original_hashes:
        receipt = frozen[run_id, items[0]["definition"]["id"]]
        source = {"原报告 SHA-256": receipt["workbook_sha256"], "原始证据": original_hashes}
    metadata = {key: run.get(key, "") for key in ("engine", "started_at", "finished_at")}
    note = "基于公开回执重排的展示版；时间、版本、执行结论取自原观察。" if original_hashes else "仅含此轮合成示例结果。"
    state = snapshot(project, run_id, "run", project["name"] + " · " + run_id, items, notes=[note], metadata=metadata, sources=source)
    state["status"] = run["status"]
    report_id = run["test_report"]["report_id"]
    data[f"/task-reports/{report_id}/excel"] = binary_report(dest / (run_id + "-执行报告.xlsx"), state)


def sync_report_downloads(data):
    """Keep material downloads and receipts consistent with each rendered display edition."""
    reports = {key.split("/")[-1]: value for key, value in data.items()
               if key.startswith("/task-reports/") and key.count("/") == 2}
    for identity, receipt in reports.items():
        book = data["/task-reports/" + identity + "/excel"]
        receipt.update(sha256=book["sha256"], size=book["size"], renderer=task_workbook.TEMPLATE_ID,
                       template_id=task_workbook.TEMPLATE_ID, edition="Pages 展示版")
    for value in data.values():
        embedded = value.get("test_report") if isinstance(value, dict) else None
        if isinstance(embedded, dict) and (receipt := reports.get(embedded.get("report_id"))):
            embedded.update(receipt)
    for project in data["/projects"]:
        for item in data["/project-materials/" + project["id"]]["items"]:
            identity, _, name = item["name"].partition("/")
            if item["category"] != "reports" or identity not in reports:
                continue
            key = item["download_url"].removeprefix("/api")
            if name.endswith(".xlsx"):
                content = data["/task-reports/" + identity + "/excel"]
                data[key] = {**content, "filename": name}
                item.update(bytes=content["size"], sha256=content["sha256"])
            elif name == "receipt.json":
                receipt = reports[identity]
                public = {k: receipt[k] for k in ("report_id", "status", "template_id", "edition", "sha256", "size",
                          "summary_counts", "download_url", "generated_at") if k in receipt}
                raw = (json.dumps(public, ensure_ascii=False, indent=2) + "\n").encode()
                data[key] = {"filename": name, "base64": base64.b64encode(raw).decode("ascii"), "media_type": "application/json"}
                item.update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())


def enrich_workbooks(data):
    observed, frozen = frozen_observations(data)
    template = {name.lower(): getattr(task_workbook, name) for name in ("HEADERS", "FIELDS", "WIDTHS", "FILLS", "COLORS", "TEMPLATE_ID")}
    with TemporaryDirectory() as tmp:
        dest = Path(tmp)
        for project in data["/projects"]:
            path = project_report(data, project, observed, frozen, dest)
            if project["id"] == "stock-room":
                with ZipFile(path) as archive:
                    template["overviewParts"] = {n: archive.read(n).decode() for n in archive.namelist()
                                                 if n.startswith(("xl/drawings/", "xl/charts/")) and "comments" not in n}
            plan_report(data, project, observed, frozen, dest)
        projects = {project["id"]: project for project in data["/projects"]}
        for summary in data["/runs"]["items"]:
            run = data["/runs/" + summary["id"]]
            run_report(data, run, projects[run["project_id"]], observed, frozen, dest)
    sync_report_downloads(data)
    data["/workbook-template"] = template
