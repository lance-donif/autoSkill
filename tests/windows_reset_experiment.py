"""Compare two cookie-reset commands in a privately extracted public Windows package."""
from __future__ import annotations

import argparse
import contextlib
import faulthandler
import hashlib
import json
import os
import runpy
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# The Windows embeddable runtime ignores PYTHONPATH. Only add this directory for the test helpers.
sys.path.append(str(Path(__file__).resolve().parent))
import published_release_smoke as smoke
from windows_doctor_diagnostic import packaged_command, run_process

ORIGINAL = "a740c6c695dcbc23c5f9ae2ddc2dcb4ffd1ef46c1a9f91d09093a51ada602a2e"


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def isolate(out, label):
    from autoskill.replay.selenium_driver import SeleniumDriver

    class Page(BaseHTTPRequestHandler):
        def do_GET(self):
            page = b"<!doctype html><title>Local session</title>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)

        def log_message(self, *_):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Page) as server, (out / f"{label}.trace").open("x", encoding="utf-8", buffering=1) as trace:
        origins = [f"http://{host}:{server.server_port}" for host in ("127.0.0.1", "localhost")]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        faulthandler.dump_traceback_later(25, repeat=True, file=trace)

        def mark(phase):
            trace.write(f"{time.monotonic():.3f} {phase}\n")
            smoke.log(phase)

        driver = None
        try:
            mark("create-driver")
            driver = SeleniumDriver({"base_url": origins[0], "allowed_origins": origins, "timeout_seconds": 15, "page_load_seconds": 20}, headless=True)
            mark("fresh reset")
            driver.reset()
            for index, origin in enumerate(origins):
                driver.goto(origin + "/")
                driver.wd.add_cookie({"name": "stale", "value": str(index), "path": "/", "httpOnly": True})
                driver.wd.execute_script("localStorage.setItem('owner',arguments[0])", f"stale-{index}")
                assert driver.wd.get_cookie("stale")["value"] == str(index)
            for active, host in enumerate(("127.0.0.1", "localhost")):
                owner = f"account-{active}"
                saved = {
                    "cookies": [{"name": "AS_AUTH", "value": owner, "domain": host, "path": "/", "httpOnly": True}],
                    "origins": [{"origin": origins[active], "localStorage": [{"name": "owner", "value": owner}]}],
                }
                mark(f"reset to {owner}")
                driver.reset(saved)
                for index, origin in enumerate(origins):
                    driver.goto(origin + "/")
                    values = {cookie["name"]: cookie["value"] for cookie in driver.wd.get_cookies()}
                    assert "stale" not in values, values
                    expected = owner if index == active else None
                    assert values.get("AS_AUTH") == expected, (index, values, expected)
                    observed = driver.wd.execute_script("return localStorage.getItem('owner')")
                    assert observed == expected, (index, observed, expected)
                mark(f"verified both origins for {owner}")
        finally:
            try:
                if driver is not None:
                    mark("close-driver")
                    driver.close()
            finally:
                server.shutdown()
                faulthandler.cancel_dump_traceback_later()
        mark("session isolation passed")


def business(install, folder, out, label):
    skill = install / "autoskill"
    data = folder / "business-data"
    sequence = 0

    def cli(skill, data, *args, expected=0):
        nonlocal sequence
        sequence += 1
        entry = f"{label}-command-{sequence:02d}-{args[0]}"
        command, settings = packaged_command(skill, *args)
        env = {**smoke.workspace_env(data), **settings}
        code = run_process(command, env, folder, out, entry, 85)
        stdout = (out / f"{entry}.stdout").read_text(encoding="utf-8-sig", errors="replace")
        stderr = (out / f"{entry}.stderr").read_text(encoding="utf-8-sig", errors="replace")
        if code != expected:
            raise RuntimeError(f"{entry} returned {code}: {(stderr or stdout)[-450:]}")
        return json.loads(stdout)

    smoke.cli = cli
    fixture = runpy.run_path(str(skill / "examples/ticket-desk/server.py"))
    result = {}
    with fixture["make_server"](port=0) as server:
        calls = smoke.observe_api(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            verify = cli(skill, data, "verify")
            assert verify["ok"] and not verify["missing"] and not verify["changed"]
            result["runtime_files_verified"] = verify["files"]
            assert cli(skill, data, "demo", "--url", f"http://127.0.0.1:{server.server_port}")["installed"]["id"] == "ticket-desk"
            cases = (
                ("api", "TC-API-003", ["--api-only"]),
                ("playwright", "TC-UI-002", ["--engine", "playwright"]),
                ("selenium", "TC-UI-003", ["--engine", "selenium"]),
            )
            for key, case, flags in cases:
                try:
                    run, receipt = smoke.completed(skill, data, case, *flags)
                    with contextlib.closing(sqlite3.connect(data / ".local/autoskill.db")) as database:
                        detail = smoke.execution_detail(database, run, case)
                        if key == "api":
                            response = detail["response"]
                            body = response["body"]
                            body = json.loads(body) if isinstance(body, str) else body
                            assert response["status"] == 201 and body["priority"] == "high" and body["title"] == "接口回放工单"
                            before = len(calls)
                            retry = smoke.verify_recovery(skill, data, database, receipt)
                            assert len(calls) == before
                            result["retry"] = {
                                "report_id": retry["report_id"], "missing_status": retry["missing_status"], "restored_status": retry["restored_status"],
                                "runs_and_results": retry["runs_and_results"], "requests_before_after": [before, len(calls)], "snapshot_sha256": retry["snapshot_sha256"],
                            }
                            result[key] = {"run": run, "status": 201, "title": body["title"], "priority": body["priority"]}
                        else:
                            actual = "高" if key == "playwright" else "请填写标题"
                            assert any(item.get("passed") and item.get("actual") == actual for item in detail["assertions"])
                            result[key] = {"run": run, "assertion": actual, "report_id": receipt["report_id"]}
                except Exception as exc:  # noqa: BLE001  # A failed case must not suppress the other engines' receipts.
                    result[key] = {"error": str(exc) or type(exc).__name__}
                write_json(out / f"{label}-business.json", result)
                smoke.log(f"Business receipt {key} {json.dumps(result[key], ensure_ascii=False)}")
        finally:
            server.shutdown()
    smoke.log(f"Independent business results {json.dumps(result, ensure_ascii=False)}")
    return all(key in result and "error" not in result[key] for key in ("api", "retry", "playwright", "selenium"))


def controller():
    root = Path(os.environ["RUNNER_TEMP"]) / "reset-experiment"
    root.mkdir()
    out = Path(os.environ["GITHUB_WORKSPACE"]) / "reset-evidence"
    out.mkdir()
    install, asset = smoke.download(root, "windows-x64", "v2.5.3")
    smoke.log(f"Public attachment {json.dumps(asset)}")
    py = install / "autoskill/vendor/windows-x64/python/python.exe"
    source = install / "autoskill/app/autoskill/replay/selenium_driver.py"
    before = source.read_bytes()
    assert hashlib.sha256(before).hexdigest() == ORIGINAL
    old = b'cdp("Network.clearBrowserCookies", {})'
    new = b'cdp("Storage.clearCookies", {})'
    assert before.count(old) == 1
    versions = {"original": before, "candidate": before.replace(old, new)}
    script = str(Path(__file__).resolve())
    stats = {key: [] for key in versions}
    results = {"attachment": asset, "isolation": stats, "business": {}}

    def observe(mode, phase, label, timeout):
        source.write_bytes(versions[mode])
        assert source.read_bytes() == versions[mode]
        # The report-recovery fixture holds the original brief next to its workspace.
        folder = root / label
        folder.mkdir()
        env = {**smoke.workspace_env(folder / "workspace"), "PYTHONDONTWRITEBYTECODE": "1"}
        command = [str(py), script, "--phase", phase, "--install", str(install), "--folder", str(folder), "--out", str(out), "--label", label]
        code = run_process(command, env, folder, out, label, timeout)
        stdout = (out / f"{label}.stdout").read_text(encoding="utf-8-sig", errors="replace")
        stderr = (out / f"{label}.stderr").read_text(encoding="utf-8-sig", errors="replace")
        smoke.log(f"{label} receipt {code} {stdout[-3200:]} {stderr[-1500:]}")
        trace = out / f"{label}.trace"
        if code != 0 and trace.is_file():
            frames = [line.strip() for line in trace.read_text(encoding="utf-8", errors="replace").splitlines()
                      if "selenium_driver.py" in line or "Timeout (" in line]
            smoke.log(f"{label} trace excerpt {frames[-6:]}")
        return code

    try:
        results["business"]["original"] = observe("original", "business", "original-business", 340)
        write_json(out / "experiment.json", results)
        for index, mode in enumerate(("original", "candidate", "candidate", "original", "original", "candidate"), 1):
            stats[mode].append(observe(mode, "isolation", f"{index}-{mode}", 65))
            write_json(out / "experiment.json", results)
        results["business"]["candidate"] = observe("candidate", "business", "candidate-business", 340)
    finally:
        source.write_bytes(before)
        assert hashlib.sha256(source.read_bytes()).hexdigest() == ORIGINAL
        write_json(out / "experiment.json", results)
        smoke.log(f"Bounded comparison {json.dumps(results, ensure_ascii=False)}")
    return 0 if stats["candidate"] == [0, 0, 0] and results["business"]["candidate"] == 0 else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["business", "isolation"])
    parser.add_argument("--out", type=Path)
    parser.add_argument("--folder", type=Path)
    parser.add_argument("--install", type=Path)
    parser.add_argument("--label")
    args = parser.parse_args()
    if args.phase == "isolation":
        isolate(args.out, args.label)
    elif args.phase == "business":
        raise SystemExit(0 if business(args.install, args.folder, args.out, args.label) else 1)
    else:
        raise SystemExit(controller())
