"""Install a public platform ZIP and verify the bundled application on a clean native runner."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import platform
import runpy
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen

REPOSITORY = "lance-donif/autoSkill"
TARGETS = {
    "windows-x64": ("autoSkill-Windows-x64-Offline.zip", "python/python.exe"),
    "linux-arm64": ("autoSkill-Linux-arm64-Offline.zip", "python/bin/python3"),
}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def log(message):
    sys.stdout.write(message + "\n")
    sys.stdout.flush()


def digest(path):
    with path.open("rb") as reader:
        return hashlib.file_digest(reader, "sha256").hexdigest()


def download(root, target, requested_tag=None):
    archive_name = TARGETS[target][0]
    endpoint = f"tags/{requested_tag}" if requested_tag else "latest"
    request = Request(f"https://api.github.com/repos/{REPOSITORY}/releases/{endpoint}", headers={"User-Agent": "autoSkill-release-smoke"})
    with urlopen(request, timeout=30) as response:
        release = json.load(response)
    tag = release["tag_name"]
    require(not requested_tag or requested_tag == tag, "Release tag does not match the requested version")
    assets = {asset["name"]: asset for asset in release["assets"]}
    archive = assets[archive_name]
    with urlopen(assets["SHA256SUMS.txt"]["browser_download_url"], timeout=30) as response:
        sums = {name: checksum for checksum, name in (line.split() for line in response.read().decode("ascii").splitlines() if line.strip())}
    expected = sums[archive_name]
    require(archive["digest"] == "sha256:" + expected, "Published asset and SHA256SUMS.txt disagree")
    log(f"Downloading {tag} {archive_name}, {archive['size']} bytes")
    path = root / archive_name
    with urlopen(archive["browser_download_url"], timeout=120) as response, path.open("xb") as output:
        shutil.copyfileobj(response, output, length=1024 * 1024)
    require(path.stat().st_size == archive["size"] and digest(path) == expected, "Downloaded archive differs from the published asset")
    install = root / ("程序 (Offline)" if target == "windows-x64" else "bundle")
    with zipfile.ZipFile(path) as bundle:
        entries = bundle.infolist()
        bundle.extractall(install)
    meta = json.loads((install / "package.json").read_text(encoding="utf-8"))
    require("v" + meta["version"] == tag, "Extracted package version differs from its release")
    return install, {"tag": tag, "asset_id": archive["id"], "asset_sha256": expected, "zip_bytes": archive["size"],
                     "entries": len(entries), "unpacked_bytes": sum(entry.file_size for entry in entries)}


def workspace_env(data):
    env = {k: v for k, v in os.environ.items() if not k.startswith("AUTOSKILL_") and k not in {"PYTHONPATH", "PYTHONHOME"}}
    headed = os.name != "nt" and bool(env.get("DISPLAY"))
    env.update(AUTOSKILL_WORKSPACE=str(data), AUTOSKILL_BROWSER="bundled", AUTOSKILL_HEADED="1" if headed else "0",
               AUTOSKILL_BRIDGE="off", PYTHONUTF8="1", PYTHONNOUSERSITE="1")
    return env


def cli(skill, data, *args, expected=0):
    if os.name == "nt":
        # Invoke the installed .cmd in a native shell; arguments containing Chinese text and spaces stay separate.
        invoke = ("[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); $OutputEncoding = [Console]::OutputEncoding; "
                  "$argvList = @(ConvertFrom-Json $env:AS_SMOKE_ARGS); & $env:AS_SMOKE_CMD @argvList; exit $LASTEXITCODE")
        command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", invoke]
        extras = {"AS_SMOKE_CMD": str(skill / "autoskill.cmd"), "AS_SMOKE_ARGS": json.dumps(["--session", "release-smoke", *args])}
    else:
        command, extras = [str(skill / "autoskill"), "--session", "release-smoke", *args], {}
    env = {**workspace_env(data), **extras}
    log("Running packaged command " + args[0])
    done = subprocess.run(command, cwd=data.parent, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=180, check=False)
    try:
        result = json.loads(done.stdout)
    except ValueError as error:
        raise RuntimeError(f"{args[0]} returned {done.returncode} without JSON: {done.stderr[-1500:]}") from error
    if done.returncode != expected:
        problem = result.get("error") or result.get("reason") or result.get("browser_launch") or result.get("missing") or result.get("status")
        failures = [(row.get("case_id"), row.get("reason")) for row in result.get("results", []) if row.get("status") != "Pass"]
        raise RuntimeError(f"{args[0]} returned {done.returncode}: {problem}, {failures}")
    return result


def verify_report(receipt, data):
    from openpyxl import load_workbook

    require(receipt["status"] == "ready", "Task report is not ready")
    verified = {}
    for field, key in (("excel", "sha256"), ("markdown", "markdown_sha256"), ("bugs", "bugs_sha256")):
        file = Path(receipt[field]).resolve()
        require(file.is_relative_to(data) and file.stat().st_size > 0, f"{field} is outside the fresh workspace or empty")
        verified[field] = digest(file)
        require(verified[field] == receipt[key], f"{field} digest differs from its receipt")
        if field != "excel":
            require(file.read_text(encoding="utf-8").strip(), f"{field} text is empty")
    with contextlib.closing(load_workbook(receipt["excel"], read_only=True)) as book:
        sheet = book["功能测试用例"]
        require(sheet.max_column == 14 and sheet.max_row > 1, "Delivered workbook has no 14-column case data")
        contents = json.dumps(list(sheet.values), ensure_ascii=False, default=str).encode("utf-8")
        verified["case_sheet_sha256"] = hashlib.sha256(contents).hexdigest()
    return verified


def execution_detail(database, run_id, case_id):
    row = database.execute("SELECT status, detail FROM results WHERE run_id=? AND case_id=?", (run_id, case_id)).fetchone()
    require(row and row[0] == "Pass", f"Missing passing result for {case_id}")
    return json.loads(row[1])


def completed(skill, data, case, *flags):
    result = cli(skill, data, "run", "ticket-desk", "--case", case, *flags)
    run_id = result["run_id"]
    status = cli(skill, data, "run-status", run_id)
    require(status["status"] == "Pass", f"{case} business run ended as {status['status']}: {status.get('reason')}")
    verify_report(status["test_report"], data)
    return run_id, status["test_report"]


def counts(database):
    return [database.execute("SELECT count(*) FROM runs").fetchone()[0], database.execute("SELECT count(*) FROM results").fetchone()[0]]


def observe_api(site):
    calls = []

    class Observed(site.RequestHandlerClass):
        def log_request(self, code="-", size="-"):
            path = self.path.partition("?")[0]
            if path.startswith("/api/"):
                calls.append((self.command, path, code))

    site.RequestHandlerClass = Observed
    return calls


def verify_recovery(skill, data, database, receipt):
    original = verify_report(receipt, data)
    report_id = receipt["report_id"]
    snapshot = Path(receipt["excel"]).parent / "snapshot.json"
    frozen, before = digest(snapshot), counts(database)
    # Move only this freshly generated fixture's brief aside.
    Path(receipt["markdown"]).rename(data.parent / "held-original-brief.md")
    missing = cli(skill, data, "task-report", report_id, expected=2)
    require(missing["report_id"] == report_id and missing["status"] == "failed", "Missing brief was reported as a completed delivery")
    recovered = cli(skill, data, "task-report", report_id, "--retry")
    require(recovered["report_id"] == report_id and recovered["source_hash"] == receipt["source_hash"], "Retry selected different frozen inputs")
    restored = verify_report(recovered, data)
    # A workbook can have new ZIP timestamps on regeneration. Check its content and the two deterministic Markdown files.
    require(all(original[key] == restored[key] for key in ("case_sheet_sha256", "markdown", "bugs")), "Retry changed the business report content")
    require(counts(database) == before and digest(snapshot) == frozen, "Retry changed the run count, result count or frozen snapshot")
    return {"report_id": report_id, "missing_status": missing["status"], "restored_status": recovered["status"],
            "snapshot_sha256": frozen, "source_hash": recovered["source_hash"], "runs_and_results": before, "files": restored}


def exercise(install, root, target):
    skill = install / "autoskill"
    data = root / "本地数据 (new)"
    require(Path(sys.executable).resolve() == (skill / "vendor" / target / TARGETS[target][1]).resolve(), "Expected the package's Python")
    inventory = cli(skill, data, "verify")
    require(inventory["ok"] and inventory["files"] > 0 and not inventory["missing"] and not inventory["changed"], "Bundled runtime inventory failed")
    doctor = cli(skill, data, "doctor", "--launch")
    vendor = (skill / "vendor").resolve()
    require(doctor["ok"] and doctor["bundled_runtime"] and Path(doctor["workspace"]).resolve() == data, "Doctor selected another workspace or runtime")
    require(all(Path(doctor[k]).resolve().is_relative_to(vendor) for k in ("chrome", "chromedriver", "agent_browser")), "Doctor selected a system tool")
    require(doctor["browser_launch"] == {"playwright": "ok", "selenium": "ok"}, "Both packaged browser engines must launch")
    fixture = runpy.run_path(str(skill / "examples" / "ticket-desk" / "server.py"))
    with fixture["make_server"](port=0) as site:
        calls = observe_api(site)
        thread = threading.Thread(target=site.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{site.server_port}"
            require(cli(skill, data, "demo", "--url", url)["installed"]["id"] == "ticket-desk", "The packaged demo did not install")
            ui_id, _ = completed(skill, data, "TC-UI-002", "--engine", "playwright")
            empty_id, _ = completed(skill, data, "TC-UI-003", "--engine", "selenium")
            api_id, receipt = completed(skill, data, "TC-API-003", "--api-only")
            with contextlib.closing(sqlite3.connect(data / ".local" / "autoskill.db")) as database:
                if target == "linux-arm64":
                    modes = database.execute("SELECT engine, headed FROM runs WHERE id IN (?,?)", (ui_id, empty_id))
                    require(set(modes) == {("playwright", 1), ("selenium", 1)}, "Both native UI runs must use the X display")
                ui = execution_detail(database, ui_id, "TC-UI-002")
                empty = execution_detail(database, empty_id, "TC-UI-003")
                api = execution_detail(database, api_id, "TC-API-003")
                require(any(check.get("actual") == "高" and check["passed"] for check in ui["assertions"]), "UI did not show high priority")
                require(any(check.get("actual") == "请填写标题" and check["passed"] for check in empty["assertions"]), "UI accepted an empty title")
                sent, response = api["request"]["json"], api["response"]
                body = json.loads(response["body"]) if isinstance(response["body"], str) else response["body"]
                require(sent["title"] == body["title"] == "接口回放工单" and sent["priority"] == body["priority"] == "high"
                        and response["status"] == 201 and body["status"] == "open", "API did not create the asserted ticket")
                state = site.RequestHandlerClass.state
                with state.lock:
                    tickets = [(t["title"], t["priority"]) for t in state.tickets]
                for title in ("回放创建的工单", "接口回放工单"):
                    require(tickets.count((title, "high")) == 1, "The fixture did not receive exactly one " + title)
                require(calls.count(("POST", "/api/tickets", 201)) == 2 and all(title for title, _ in tickets), "Unexpected ticket writes")
                requests_before = len(calls)
                retry = verify_recovery(skill, data, database, receipt)
                require(len(calls) == requests_before, "Report retry made an extra business request")
                retry["api_requests_before_and_after"] = [requests_before, len(calls)]
                with state.lock:
                    require(tickets == [(t["title"], t["priority"]) for t in state.tickets], "Report retry changed the fixture")
        finally:
            site.shutdown()
            thread.join(timeout=5)
    return {"system": platform.platform(), "runtime_files_verified": inventory["files"], "python": doctor["python"],
            "tools_in_bundle": {k: Path(doctor[k]).resolve().relative_to(vendor).as_posix() for k in ("chrome", "chromedriver", "agent_browser")},
            "browser_launch": doctor["browser_launch"], "playwright": {"run_id": ui_id, "title": "回放创建的工单", "displayed_priority": "高"},
            "selenium": {"run_id": empty_id, "empty_title_message": "请填写标题"},
            "api": {"run_id": api_id, "title": sent["title"], "priority": body["priority"], "status": response["status"]}, "retry": retry}


def setup_arm64(install, data):
    log("Configuring the extracted ARM64 package with setup-linux.sh")
    result = subprocess.run(["sh", str(install / "setup-linux.sh")], cwd=install, env=workspace_env(data), check=False,
                            capture_output=True, text=True, encoding="utf-8", timeout=60)
    require(result.returncode == 0, "ARM64 package setup failed: " + (result.stderr or result.stdout)[-2500:])
    setup = json.loads(result.stdout)
    require(setup["ok"] and setup["platform"]["target"] == "linux-arm64", "Installer did not configure a native ARM64 runtime")
    require(Path(setup["settings"]).resolve().is_relative_to(data), "Installer wrote settings outside the fresh workspace")
    pair = setup["browser_pair"]
    vendor = (install / "autoskill/vendor/linux-arm64").resolve()
    require(pair["ok"] and all(Path(pair[k]["path"]).resolve().is_relative_to(vendor) for k in ("chrome", "chromedriver")),
            "Installer did not select the bundled ARM64 browser and driver")
    return {"platform": setup["platform"]["target"], "machine": setup["platform"]["machine"], "distribution": setup["platform"]["distribution"],
            "chrome": pair["chrome"]["version"], "driver": pair["chromedriver"]["version"], "display": os.environ["DISPLAY"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=TARGETS, default="windows-x64")
    parser.add_argument("--release-tag", help="Install this published tag; the default uses the latest public release")
    parser.add_argument("--work-dir", type=Path, help="Existing empty temporary directory owned by the runner")
    parser.add_argument("--exercise", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--receipt", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.exercise:
        result = exercise(args.exercise.resolve(), args.receipt.resolve().parent, args.target)
        args.receipt.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return
    if args.target == "linux-arm64":
        require(sys.platform == "linux" and platform.machine().lower() in ("aarch64", "arm64") and os.environ.get("DISPLAY"),
                "The Linux ARM64 bundle requires its native runner and an X display")
    else:
        require(os.name == "nt" and platform.machine().lower() in ("amd64", "x86_64"), "The Windows x64 bundle requires its native runner")
    with contextlib.ExitStack() as stack:
        temp = args.work_dir or stack.enter_context(tempfile.TemporaryDirectory(prefix="autoskill-zip-", dir=os.environ.get("RUNNER_TEMP"),
                                                                                ignore_cleanup_errors=True))
        root = Path(temp).resolve()
        require(root.is_dir() and not any(root.iterdir()), "The installation must start in an empty temporary directory")
        install, artifact = download(root, args.target, args.release_tag)
        setup = setup_arm64(install, root / "本地数据 (new)") if args.target == "linux-arm64" else None
        python = install / "autoskill/vendor" / args.target / TARGETS[args.target][1]
        receipt = root / "receipt.json"
        subprocess.run([str(python), str(Path(__file__).resolve()), "--target", args.target, "--exercise", str(install), "--receipt", str(receipt)],
                       cwd=root, check=True, timeout=600)
        result = {"release": artifact, **json.loads(receipt.read_text(encoding="utf-8"))}
        if setup:
            result["setup"] = setup
        rendered = json.dumps(result, ensure_ascii=False, indent=2)
        log(rendered)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as summary:
                title = "Linux ARM64" if args.target == "linux-arm64" else "Windows"
                summary.write(f"## Published {title} ZIP acceptance\n\n```json\n" + rendered + "\n```\n")


if __name__ == "__main__":
    main()
