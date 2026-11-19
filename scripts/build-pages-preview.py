"""Build the Pages application entries and their isolated, browser-local sample data."""
from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import json
import os
import sys
from collections import defaultdict
from contextlib import ExitStack
from html import escape
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import quote

import httpx
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))
ORIGIN = "http://127.0.0.1:8787"
PAGES = {"runs?view=results": "results", **{name: name for name in (
    "dashboard", "cases", "workflows", "native-assets", "conversations", "schedules",
    "project-materials", "load-tests", "diagnostics", "data")}}

def seed(root: Path) -> dict:
    from autoskill import builder, case_folders, db, demo, plan, projects, runner, sessions
    from autoskill.server import scheduler

    tickets, stock = "ticket-desk", "stock-room"
    db.init()
    examples = {
        tickets: ("示例工单系统", "工单", [
            *[case for case in demo.cases() if case["id"] in ("TC-API-006", "TC-UI-005")],
            builder.api_case("TC-API-007", "校验未登录提示", "GET", "/api/tickets", {"status": 401, "json_equals": {"/code": "LOGIN_REQUIRED"}}, module="登录"),
            builder.api_case("TC-API-008", "查看指定工单", "GET", "/api/tickets/{{ticket_id}}", {"status": 200}, module="工单"),
            builder.api_case("TC-API-009", "查询公开工单分类", "GET", "/api/categories", {"status": 200, "json_equals": {"/items/0": "售后"}}, module="工单"),
            builder.api_case("TC-API-010", "工单服务健康检查", "GET", "/api/health", {"status": 200}, module="服务"),
        ]),
        stock: ("示例库存中心", "供应链", [
            builder.api_case("ST-API-001", "读取可售库存", "GET", "/api/stock", {"status": 200, "json_equals": {"/available": 18}}, module="库存查询"),
            builder.api_case("ST-API-002", "核对安全库存阈值", "GET", "/api/stock", {"status": 200, "json_equals": {"/threshold": 20}}, module="库存预警"),
            builder.api_case("ST-API-003", "读取指定仓库", "GET", "/api/warehouses/{{warehouse_id}}", {"status": 200}, module="仓库"),
            builder.api_case("ST-API-004", "读取仓库列表", "GET", "/api/warehouses", {"status": 200, "json_equals": {"/items/0": "上海一仓"}}, module="仓库"),
            builder.api_case("ST-API-006", "登录库存系统", "POST", "/api/login", {"status": 200, "json_equals": {"/role": "viewer"}},
                             json={"user": "demo-viewer"}, module="账户"),
            builder.api_case("ST-API-005", "库存服务健康检查", "GET", "/api/health", {"status": 200}, module="服务"),
            builder.ui_case("ST-UI-001", "显示库存搜索框", {"start_url": "/stock", "actions": [],
                            "assertions": [{"assertion": "visible", "locators": [{"by": "testid", "value": "stock-search"}]}]}, module="库存查询", risk="read"),
        ]),
    }
    # Establish the two projects before the first sample run in their history.
    now = datetime.datetime.now(datetime.UTC)
    with patch.object(db, "now", return_value=(now - datetime.timedelta(days=9)).isoformat(timespec="milliseconds")):
        for pid, (title, category, cases) in examples.items():
            domain = "tickets" if pid == tickets else "stock"
            projects.create(title, f"https://{domain}.example.invalid", category=category, project_id=pid)
            projects.save_cases(pid, cases)
            source = root / (pid + ".json")
            rows = [{"id": case["id"], "title": case["title"], "module": case["module"], "steps": case["steps"],
                     "expected": case["expected"]} for case in cases]
            source.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
            with plan.use_folder(root / "data" / ".local" / "designs" / pid):
                plan.write(pid, str(source))

    projects.set_auth(stock, {"api_case": "ST-API-006"})
    sessions.save(stock, variables={"role": "viewer"})

    def folder(pid, name, case_ids=(), parent=None):
        tree = case_folders.create(pid, name, parent_id=parent, expected_revision=case_folders.list_folders(pid)["revision"])
        key = next(item["id"] for item in tree["folders"] if item["name"] == name)
        if case_ids:
            case_folders.move_cases(pid, list(case_ids), key, expected_revision=tree["revision"])
        return key

    login = folder(tickets, "登录验证", ["TC-UI-005"])
    folder(tickets, "接口校验", ["TC-API-006", "TC-API-007"], login)
    folder(tickets, "工单查询", ["TC-API-008", "TC-API-009"])
    folder(tickets, "待整理")
    inventory = folder(stock, "库存管理", ["ST-UI-001"])
    folder(stock, "可售库存", ["ST-API-001", "ST-API-002"], inventory)
    folder(stock, "仓库管理", ["ST-API-003", "ST-API-004"])
    folder(stock, "待补用例")

    for pid, cid in ((tickets, "TC-API-010"), (stock, "ST-API-005")):
        case_folders.move_cases(pid, [cid], None, expected_revision=case_folders.list_folders(pid)["revision"])

    hourly = scheduler.create({"project_id": tickets, "title": "登录接口巡检", "case_ids": ["TC-API-006"],
                               "rule": {"kind": "interval", "minutes": 60}})
    scheduler.create({"project_id": stock, "title": "库存每日检查", "case_ids": ["ST-API-001", "ST-API-004"],
                      "rule": {"kind": "daily", "time": "09:00", "timezone": "Asia/Shanghai"}})
    paused = scheduler.create({"project_id": stock, "title": "库存预警复查", "case_ids": ["ST-API-002"],
                               "rule": {"kind": "interval", "minutes": 30}})
    scheduler.update(paused["id"], {"enabled": False, "expected_revision": paused["revision"]})

    # Absolute instants order the batches across days and keep the last batch's 2/3 rate reproducible.
    samples = [
        (7, tickets, "TC-API-010", "Pass", "baseline", "", None),
        (4, stock, "ST-API-003 ST-API-004 ST-API-002", "Fail", "midweek", "", None),
        (4, tickets, "TC-API-008", "Blocked", "midweek", "run-blocked", None),
        (2, tickets, "TC-API-009 TC-API-006", "Pass", "checks", "run-pass", hourly),
        (1, stock, "ST-API-005 ST-API-003", "Blocked", "inventory", "", None),
        (0, tickets, "TC-API-006", "Pass", "acceptance", "", None),
        (0, stock, "ST-API-001 ST-API-002", "Fail", "acceptance", "run-fail", None),
    ]
    details = {}
    for index, (days, pid, ids, expected, batch, alias, task) in enumerate(samples, 1):
        stamp = (now - datetime.timedelta(days=days, seconds=len(samples)-index)).isoformat(timespec="milliseconds")
        with patch.object(db, "now", return_value=stamp), plan.use_folder(root / "data" / ".local" / "designs" / pid):
            prepared = runner.prepare(pid, ids.split(), headed=False, plan_path=root / "data" / ".local" / "designs" / pid,
                                      batch_id="batch-" + batch, trigger="schedule" if task else "manual", task_id=task["id"] if task else None)
            prepared["id"] = f"run-demo-{index:02}"
            run = runner.enqueue(prepared)
            result = runner.execute(run["id"])
            if result["status"] != expected:
                raise RuntimeError(f"Sample {ids} produced {result['status']}, expected {expected}")
            if task:
                db.update("tasks", task["id"], {"last_run_id": run["id"]})
            if alias:
                details["runs/" + run["id"]] = alias
    return details


def register_observed_cases(source: dict) -> None:
    """Expose only the public, frozen case definitions in the transient fixture project."""
    from autoskill import builder, domain, projects

    project = source["project_id"]
    projects.create(source["project_name"], "https://www.runoob.com", category="公开站点实测", project_id=project)
    previous = {}
    records = source.get("previous_cases", []) + source["cases"]
    for observation in sorted(records, key=lambda row: row["checked_at"]):
        case = builder.ui_case(observation["case_id"], observation["title"], observation["automation"],
                               module="公开页面", risk="read", source=observation["url"])
        case.update(steps=observation["steps"], expected=observation["expected"], revision=observation["revision"])
        cid = case["id"]
        projects.save_cases(project, [case], expected_hashes={cid: previous.get(cid)})
        previous[cid] = domain.content_hash(projects.case(project, cid))


def add_observations(data: dict, source: dict) -> None:
    """Project public receipts into the browser's result lists without replaying a target."""
    project = source["project_id"]
    labels = {"pass": "Pass", "fail": "Fail", "not_run": "NotRun"}
    data["/observations/" + project] = source
    observed_runs = defaultdict(list)
    records = source.get("previous_cases", []) + source["cases"]
    records.sort(key=lambda observation: observation["checked_at"])
    for observation in records:
        outcome = observation["outcome"]
        cid, run_id = observation["case_id"], observation["run_id"]
        checked_at, started_at = observation["checked_at"], observation["started_at"]
        label = "历史实测" if observation["source_kind"] == "historical" else "现场实测"
        provenance = {"source_kind": observation["source_kind"], "observed_at": checked_at,
                      "source_url": observation.get("source_url") or observation["url"]}
        actual = f"{label}（{checked_at}） · " + observation["actual"]
        reason = "" if outcome == "pass" else actual
        scope = {"project_id": project, "run_id": run_id, "case_id": cid}
        state = {"phase": "settled", "lifecycle": "completed", "outcome": outcome, "scope": scope,
                 "checks": {"business": outcome if observation["checks"] else "not_applicable", "performance": "not_applicable", "quality": "not_applicable"},
                 "evidence": "valid", "report": "ready", "delivery": "not_applicable", "reason": reason}
        item = next(item for item in data["/cases"]["items"] if item["project_id"] == project and item["case_id"] == cid)
        revision = observation["revision"]
        # Preserve observed facts, including unsuccessful probes and their actual timestamps.
        row = {**scope, **provenance, "id": run_id + "/" + cid, "kind": "ui", "case_title": observation["title"],
               "case_revision": revision, "category": observation.get("category", ""), "project_name": source["project_name"], "engine": observation["engine"],
               "reason": reason, "actual": actual, "status": labels[outcome], "history_status": labels[outcome], "prerequisite": False,
               "started_at": started_at, "duration_ms": observation["duration_ms"], "state": state,
               "detail": {"assertions": observation["checks"], "steps": observation.get("timeline", []),
                          "attempts": [], "artifact_hashes": observation["artifact_hashes"]}}
        observed_runs[run_id].append(row)
        current = {"state": state, "current_status": labels[outcome], "current": True, "last_status": labels[outcome],
                   "last_state": state, "last_run_at": started_at, **provenance}
        item.update(current, revision=revision)
        data["/projects/" + project + "/cases/" + cid].update(current)

    def append_history(key, entity):
        status = entity["history_status"]
        for path in (key, key + "?status=" + status):
            response = data[path]
            response["items"].append(entity)
            response["total"] += 1
            response["state_counts"][entity["state"]["outcome"]] += 1

    for run_id, rows in observed_runs.items():
        counts = {verdict: sum(row["state"]["outcome"] == verdict for row in rows) for verdict in labels}
        outcome = "fail" if counts["fail"] else "pass" if counts["pass"] == len(rows) else "not_run"
        last = max(rows, key=lambda row: row["observed_at"])
        started = min(row["started_at"] for row in rows)
        report_id = "run_" + run_id
        link = "/api/task-reports/" + report_id
        report = {"report_id": report_id, "status": "ready", "download_url": link + "/excel", "markdown_download_url": link + "/markdown",
                  "bugs_download_url": link + "/bugs"}
        state = {**last["state"], "scope": {"project_id": project, "run_id": run_id}, "outcome": outcome}
        reason = next((row["reason"] for row in rows if row["reason"]), "")
        run = {"id": run_id, "source_kind": last["source_kind"], "observed_at": last["observed_at"], "source_url": last["source_url"],
               "project_id": project, "project_name": source["project_name"], "trigger": "manual", "family": "replay", "engine": last["engine"],
               "case_ids": [row["case_id"] for row in rows], "total": len(rows), "done": len(rows) - counts["not_run"],
               "passed": counts["pass"], "failed": counts["fail"], "not_run": counts["not_run"], "state_counts": counts,
               "status": labels[outcome], "history_status": labels[outcome], "state": state, "reason": reason,
               "created_at": started, "started_at": started, "finished_at": last["observed_at"], "test_report": report}
        data["/runs/" + run_id] = {**run, "results": rows}
        data["/task-reports/" + report_id] = report
        append_history("/runs", run)
        for row in rows:
            append_history("/results", row)
    for path, response in data.items():
        if path in ("/runs", "/results") or path.startswith(("/runs?status=", "/results?status=")):
            response["items"].sort(key=lambda item: item.get("created_at") or item.get("started_at") or "", reverse=True)


def project_fixtures(project: dict, read, file, report_files) -> None:
    pid = quote(project["id"], safe="")
    source = "/projects/" + pid
    for path in (source, source + "/folders", "/case-definitions/" + pid,
                 "/assets/" + pid, "/assets/" + pid + "?all=1", "/conversations/" + pid,
                 "/case-plans/" + pid + "/deliveries",
                 "/knowledge/" + pid + "/settings", "/knowledge/" + pid + "/search"):
        read(path)
    design = read("/case-plans/" + pid)
    if design.get("exists"):
        file("/case-plans/" + pid + "/excel", pid + "-design.xlsx")
    if (design.get("test_report") or {}).get("report_id"):
        report_files(design["test_report"]["report_id"])
    for item in read("/project-materials/" + pid)["items"]:
        file(item["download_url"].removeprefix("/api"), item["name"].split("/")[-1])
        if item["category"] == "knowledge" and item["text"]:
            read(f"/project-materials/{pid}/knowledge?file={quote(item['name'])}&all=1")
    if project["cases"]["api"]:
        for part in ("collection", "environment"):
            read(source + "/postman?part=" + part)
    for listing in ("/cases", "/load-tests/options", "/workflows"):
        read(listing + "?projects=" + pid)


def fixtures(client: TestClient) -> dict:
    data = {}

    def read(path: str):
        response = client.get("/api" + path)
        response.raise_for_status()
        value = response.json()
        data[path] = value
        return value

    def file(path: str, filename: str):
        response = client.get("/api" + path)
        response.raise_for_status()
        data[path] = {"filename": filename, "media_type": response.headers["content-type"],
                      "base64": base64.b64encode(response.content).decode("ascii")}

    for path in ("/status", "/state-revision", "/settings", "/data/status", "/summary", "/dashboard",
                 "/dashboard?current=true", "/projects", "/cases", "/runs", "/runs?limit=1&status=active",
                 "/results", "/tasks", "/engines", "/secrets", "/load-tests", "/load-tests/options",
                 "/workflows", "/project-materials"):
        read(path)
    for task in data["/tasks"]:
        read("/tasks/" + task["id"])
    for status in ("Pass", "Fail", "NotRun"):
        for listing in ("/runs", "/results"):
            read(listing + "?status=" + status)
    def report_files(report_id):
        report = "/task-reports/" + report_id
        read(report)
        for name, suffix in (("excel", "xlsx"), ("markdown", "md"), ("bugs", "md")):
            file(report + "/" + name, report_id + "-" + name + "." + suffix)

    for entry in data["/runs"]["items"]:
        run = read("/runs/" + entry["id"])
        report_files(run["test_report"]["report_id"])
    for project in data["/projects"]:
        project_fixtures(project, read, file, report_files)
    for case in data["/cases"]["items"]:
        pid, cid = quote(case["project_id"], safe=""), quote(case["case_id"], safe="")
        path = "/projects/" + pid + "/cases/" + cid
        read(path)
        read("/case-definitions/" + pid + "/" + cid)
        if case["kind"] == "ui" and case["replayable"]:
            for engine in ("playwright", "selenium"):
                script = path + "/script?engine=" + engine
                response = client.get("/api" + script)
                response.raise_for_status()
                data[script] = response.text
    return data


def render(root: Path, pages: dict, observed: dict | None) -> None:
    from autoskill.server.app import create_app

    output = ROOT / "preview"
    with TestClient(create_app(start_worker=False), base_url=ORIGIN) as client:
        data = fixtures(client)
    if observed:
        add_observations(data, observed)
    from preview_workbooks import enrich_workbooks

    enrich_workbooks(data)
    # Distribute only portable sample paths, including paths inside serialized JSON strings.
    bundle = json.dumps(data, ensure_ascii=False, indent=2).replace(str(root), "/示例/autoSkill")
    bundle = bundle.replace(str(ROOT), "/示例/程序").replace(str(Path.home()), "/示例")
    (output / "fixtures.json").write_text(bundle + "\n", encoding="utf-8")

    template = (ROOT / "skills/autoskill/app/ui/index.html").read_text(encoding="utf-8")
    template = template.replace('href="/css/', 'href="../skills/autoskill/app/ui/css/')
    importmap = json.dumps({"imports": {"../skills/autoskill/app/ui/js/pages/data.js": "./data-page.js",
                                         "../skills/autoskill/app/ui/js/pages/project-materials.js": "./materials-page.js"}}, separators=(",", ":"))
    digest = base64.b64encode(hashlib.sha256(importmap.encode()).digest()).decode()
    policy = ("default-src 'self'; script-src 'self' 'sha256-" + digest + "'; style-src 'self' 'unsafe-inline'; "
              "img-src 'self' data: blob:; connect-src 'self' blob: data:; object-src 'none'; base-uri 'none'")
    template = template.replace("<head>", '<head>\n  <meta http-equiv="Content-Security-Policy" content="' + policy + '">')
    template = template.replace('</head>', '  <link rel="stylesheet" href="./preview.css">\n</head>')
    template = template.replace('<title>autoSkill</title>', '<title>autoSkill 仪表盘 · 交互预览</title>')
    template = template.replace('<script type="module" src="/js/main.js"></script>',
                                '<script type="importmap">' + importmap + '</script>\n  <script type="module" src="./app.js"></script>')
    entries = {name: route for route, name in pages.items()}
    entries["index"] = "dashboard"
    for name, route in entries.items():
        html = template.replace("<body>", '<body data-route="' + escape(route, quote=True) + '">')
        (output / (name + ".html")).write_text(html, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workbooks-only", action="store_true", help="Update reports from existing fixtures, preserving sample IDs and clocks.")
    if parser.parse_args().workbooks_only:
        from preview_workbooks import enrich_workbooks

        path = ROOT / "preview/fixtures.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        enrich_workbooks(data)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return
    with TemporaryDirectory() as temporary, ExitStack() as resources:
        root = Path(temporary)
        resources.enter_context(patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(root / "data"), "AUTOSKILL_CASE_SKILL": str(root / "design"),
            "AUTOSKILL_BRIDGE": "off", "AUTOSKILL_HEADED": "0", "AUTOSKILL_RUNTIME_CONTEXT": "", "AUTOSKILL_SESSION": "", "AUTOSKILL_WORKFLOW_BINDING": ""}))
        def samples(request: httpx.Request) -> httpx.Response:
            examples = {
                ("tickets.example.invalid", "/api/tickets"): (401, {"code": "UNAUTHENTICATED"}),
                ("tickets.example.invalid", "/api/categories"): (200, {"items": ["售后", "咨询"]}),
                ("tickets.example.invalid", "/api/health"): (200, {"status": "ok"}),
                ("stock.example.invalid", "/api/stock"): (200, {"available": 18, "threshold": 10}),
                ("stock.example.invalid", "/api/warehouses"): (200, {"items": ["上海一仓", "杭州二仓"]}),
                ("stock.example.invalid", "/api/health"): (200, {"status": "ok"}),
            }
            if request.method == "POST" and (request.url.host, request.url.path) == ("stock.example.invalid", "/api/login"):
                return httpx.Response(200, json={"role": "viewer"})
            if request.method != "GET" or (request.url.host, request.url.path) not in examples:
                raise AssertionError(f"Unexpected preview fixture request: {request.method} {request.url}")
            status, body = examples[(request.url.host, request.url.path)]
            return httpx.Response(status, json=body)

        # Construct all sample results using in-memory fixture responses.
        with patch("autoskill.replay.api.new_client",
                   side_effect=lambda *_args, **_kwargs: httpx.Client(transport=httpx.MockTransport(samples))):
            pages = {**PAGES, **seed(root)}
        evidence = ROOT / "scripts/fixtures/runoob-observations.json"
        observed = json.loads(evidence.read_text(encoding="utf-8")) if evidence.is_file() else None
        if observed:
            register_observed_cases(observed)
        render(root, pages, observed)


if __name__ == "__main__":
    main()
