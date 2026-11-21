"""Diagnose the public Windows bundle using its packaged Python and a fresh local workspace."""
from __future__ import annotations

import argparse
import contextlib
import faulthandler
import hashlib
import json
import os
import re
import subprocess
import time
import traceback
from pathlib import Path


def child(trace_file):
    # The embedded Python runs from the downloaded ZIP. Keep its app files untouched.
    from autoskill import cli, platforms, runner

    with trace_file.open("x", encoding="utf-8", buffering=1) as stream:
        def mark(message):
            stream.write(f"{time.monotonic():.3f} {message}\n")

        def observe(obj, name, label):
            original = getattr(obj, name)

            def call(*args, **kwargs):
                target = str(args[0]) if name == "binary_status" and args else ""
                mark(f"begin {label} {name} {target}")
                try:
                    return original(*args, **kwargs)
                finally:
                    mark(f"end {label} {name}")

            setattr(obj, name, call)

        make_driver = runner.make_driver

        def traced_driver(engine, config):
            mark(f"begin make_driver {engine}")
            driver = make_driver(engine, config)
            mark(f"end make_driver {engine}")
            for operation in ("reset", "goto", "title", "close"):
                observe(driver, operation, engine)
            return driver

        runner.make_driver = traced_driver
        for name in ("system_info", "binary_status", "browser_pair"):
            observe(platforms, name, "platform")
        faulthandler.dump_traceback_later(25, repeat=True, file=stream)
        try:
            mark("begin cli doctor")
            return cli.main(["--session", "release-smoke", "doctor", "--launch"])
        finally:
            faulthandler.cancel_dump_traceback_later()
            mark("end cli doctor")


def processes(root_pid, directory, label):
    # Only log this test parent's descendants, their executable paths and local port numbers.
    command = r"""
    $all = @(Get-CimInstance Win32_Process)
    $ids = [System.Collections.Generic.HashSet[uint32]]::new()
    $null = $ids.Add([uint32]$env:DIAG_PARENT_PID)
    do {
      $before = $ids.Count
      foreach ($p in $all) { if ($ids.Contains([uint32]$p.ParentProcessId)) { $null=$ids.Add([uint32]$p.ProcessId) } }
    } while ($ids.Count -ne $before)
    $rows = @($all | Where-Object {$ids.Contains([uint32]$_.ProcessId)} |
      Select-Object ProcessId,ParentProcessId,Name,ExecutablePath,@{n='ChromeDriverPort';e={
        if ($_.Name -eq 'chromedriver.exe' -and $_.CommandLine -match '--port=(\d+)') {$Matches[1]}
      }})
    $rows | ConvertTo-Json -Compress
    """
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        env={**os.environ, "DIAG_PARENT_PID": str(root_pid)}, capture_output=True, timeout=20, check=False,
    )
    (directory / f"{label}-processes.json").write_bytes(result.stdout)


def run_process(cmd, env, cwd, out, label, timeout):
    from published_release_smoke import log

    with (out / f"{label}.stdout").open("wb") as stdout, (out / f"{label}.stderr").open("wb") as stderr:
        proc = subprocess.Popen(cmd, env=env, cwd=cwd, stdout=stdout, stderr=stderr)
        try:
            result = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                processes(proc.pid, out, label)
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], check=False, timeout=20)
            proc.wait(timeout=10)
            result = "timeout"
    log(f"{label} {result}")
    return result


def packaged_command(skill, *args):
    command = (
        "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); $OutputEncoding = [Console]::OutputEncoding; "
        "$argvList = @(ConvertFrom-Json $env:AS_SMOKE_ARGS); & $env:AS_SMOKE_CMD @argvList; exit $LASTEXITCODE"
    )
    settings = {"AS_SMOKE_CMD": str(skill / "autoskill.cmd"), "AS_SMOKE_ARGS": json.dumps(["--session", "release-smoke", *args])}
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command], settings


def count_plan(skill, folder):
    from openpyxl import load_workbook
    from published_release_smoke import cli

    data = folder / "plan-count-data"
    project = cli(skill, data, "project", "create", "--name", "单目标验证", "--url", "http://127.0.0.1:9")["created"]["id"]
    definition = folder / "design.json"
    design = [{"id": "FLOW", "title": "取得目标内容截图", "steps": ["打开首页", "找到目标内容", "截取目标内容"],
               "expected": ["1. 首页可见", "2. 内容可见", "3. 截图完成"]}]
    definition.write_text(json.dumps(design, ensure_ascii=False), encoding="utf-8")
    cmd = ("plan", "write", project, "--file", str(definition), "--expected-cases", "1")
    saved = cli(skill, data, *cmd)
    book = Path(saved["initial_excel"])
    original = hashlib.sha256(book.read_bytes()).hexdigest()
    with contextlib.closing(load_workbook(book, read_only=True)) as workbook:
        sheet = workbook["功能测试用例"]
        rows = [row for row in sheet.values if row[9] == "未执行"]
        assert len(rows) == 1 and sheet.max_column == 14 and all(len(str(cell).splitlines()) == 3 for cell in rows[0][5:7])
    definition.write_text(json.dumps([{"id": key, "title": key} for key in ("OPEN", "FIND", "CAPTURE")]), encoding="utf-8")
    error = cli(skill, data, *cmd, expected=2)
    assert error["code"] == "validation_error" and "期望 1 条" in error["error"] and "实际有 3 条" in error["error"]
    assert hashlib.sha256(book.read_bytes()).hexdigest() == original and cli(skill, data, "plan", "show")["task_id"] == saved["task_id"]
    return {"target": 1, "rows": len(rows), "columns": 14, "ordered_steps": 3, "rejected_split_rows": 3, "rejection_exit": 2, "initial_sha256": original}


def run(tag):
    from published_release_smoke import cli, download, log, workspace_env

    folder = Path(os.environ["RUNNER_TEMP"]) / f"doctor-probe-{tag}"
    folder.mkdir()
    outputs = Path(os.environ["GITHUB_WORKSPACE"]) / f"doctor-proof-{tag}"
    outputs.mkdir()
    install, asset = download(folder, "windows-x64", tag)
    log(json.dumps(asset))
    skill = install / "autoskill"
    plan_ok = True
    if tag == "v2.5.3":
        try:
            plan_receipt = count_plan(skill, folder)
        except Exception:  # noqa: BLE001  # Preserve the failed plan receipt and continue to the independent browser checks.
            plan_receipt = {"error": traceback.format_exc()}
            plan_ok = False
        report = json.dumps(plan_receipt)
        log(report)
        (outputs / "plan-receipt.json").write_text(report + "\n", encoding="utf-8")
    py = skill / "vendor/windows-x64/python/python.exe"
    data = folder / "本地数据 (new)"
    inventory = cli(skill, data, "verify")
    assert inventory["ok"] and not inventory["missing"] and not inventory["changed"]
    log(f"Runtime files verified {inventory['files']}")
    env = workspace_env(data)
    env.update(AUTOSKILL_HOME=str(skill) + "\\", AUTOSKILL_PY=str(py), PYTHONPATH=str(skill / "app"), PYTHONDONTWRITEBYTECODE="1")
    command, settings = packaged_command(skill, "doctor", "--launch")
    normal = run_process(command, {**env, **settings}, folder, outputs, "normal", 120)
    try:
        answer = json.loads((outputs / "normal.stdout").read_text(encoding="utf-8-sig", errors="replace"))
        summary = {key: answer.get(key) for key in ("ok", "bundled_runtime", "browser_versions", "browser_launch", "missing")}
        log(f"Normal packaged doctor: {json.dumps(summary, ensure_ascii=False)}")
    except ValueError:
        stderr = (outputs / "normal.stderr").read_text(encoding="utf-8-sig", errors="replace")
        log(f"Normal packaged command stderr: {stderr[-1000:]}")
    trace = outputs / "python-stacks.log"
    traced = run_process([str(py), str(Path(__file__).resolve()), "--child", str(trace)], env, folder, outputs, "traced", 100)
    if trace.exists():
        log(trace.read_text(encoding="utf-8", errors="replace")[-13000:])
    for file in sorted(outputs.glob("*-processes.json")):
        log(f"{file.name} {file.read_text(errors='replace')[:2800]}")
    result = {"asset": asset, "normal": normal, "traced": traced, "runtime_files": inventory["files"]}
    if normal == 0:
        receipt = folder / "full-receipt.json"
        command = [str(py), str(Path(__file__).with_name("published_release_smoke.py")),
                   "--target", "windows-x64", "--exercise", str(install), "--receipt", str(receipt)]
        result["business"] = run_process(command, env, folder, outputs, "unmodified-full", 300)
        if receipt.is_file():
            log(receipt.read_text(encoding="utf-8"))
            (outputs / "business-receipt.json").write_bytes(receipt.read_bytes())
    (outputs / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return 0 if normal == result.get("business") == 0 and plan_ok else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--tag")
    group.add_argument("--child", type=Path)
    args = parser.parse_args()
    if args.tag is not None and not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", args.tag):
        parser.error("--tag must be a release version such as v2.5.3")
    raise SystemExit(child(args.child) if args.child else run(args.tag))
