"""Run explicitly selected existing validation tools with isolated data and retained logs."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON_SUITES = {
    "conventions": ["test_conventions.py"],
    "workflow": ["test_workflow.py", "test_cli_workflow_loop.py", "test_host_context.py", "test_native_evidence.py"],
    "knowledge": ["test_failure_context.py", "test_knowledge_entries.py", "test_knowledge_workflow.py",
                  "test_knowledge_transfer.py", "test_knowledge_transport.py"],
    "review-tools": ["test_code_review.py"],
}
NODE_SUITES = {"host": ["tests/plugin.test.mjs", "tests/host-adapter.test.mjs", "tests/host-backend.test.mjs",
                        "tests/host-commands.test.mjs", "tests/host-state-summary.test.mjs"]}
SUITES = ["lint", "ruff", "biome", *PYTHON_SUITES, *NODE_SUITES, "review-rules"]

# Discovery must not turn an empty or misspelled test selection into a green result.
UNITTEST_RUNNER = """import pathlib, sys, unittest
root = pathlib.Path(sys.argv[1])
pattern = sys.argv[2]
if not list((root / 'tests').glob(pattern)):
    raise SystemExit('Missing test selection: ' + pattern)
suite = unittest.defaultTestLoader.discover(str(root / 'tests'), pattern=pattern)
if not suite.countTestCases():
    raise SystemExit('Empty test selection: ' + pattern)
result = unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(0 if result.wasSuccessful() else 1)
"""


def _existing_tool(name: str, supplied: str | None, root: Path) -> str:
    if supplied:
        candidate = Path(supplied).expanduser()
        if candidate.is_file():
            return str(candidate.resolve())
        found = shutil.which(supplied)
    else:
        suffix = ".exe" if os.name == "nt" else ""
        local = root / "node_modules/.bin" / (name + suffix)
        found = str(local) if local.is_file() else shutil.which(name)
    if not found:
        raise FileNotFoundError(f"缺少 {name}；请提供已有工具路径，不会自动安装")
    return found


def build_commands(root: Path, suites: list[str], python_tests: list[str], node_tests: list[str],
                   python: str, ruff: str | None = None, biome: str | None = None, node: str | None = None) -> list[dict]:
    commands = []
    python_tests = list(python_tests)
    node_tests = list(node_tests)
    expanded = list(dict.fromkeys(value for suite in suites for value in (["ruff", "biome"] if suite == "lint" else [suite])))
    for suite in expanded:
        if suite in {"ruff", "biome"}:
            try:
                executable = _existing_tool(suite, ruff if suite == "ruff" else biome, root)
                args = [executable, "check", "."] if suite == "ruff" else [executable, "lint"]
                commands.append({"name": suite, "argv": args, "version_argv": [executable, "--version"]})
            except FileNotFoundError as exc:
                commands.append({"name": suite, "argv": [], "error": str(exc)})
        elif suite in PYTHON_SUITES:
            python_tests.extend(PYTHON_SUITES[suite])
        elif suite in NODE_SUITES:
            node_tests.extend(NODE_SUITES[suite])
        elif suite == "review-rules":
            commands.append({"name": suite, "argv": [python, str(root / "scripts/code_review.py"), "--root", str(root), "check"]})
        else:
            raise ValueError("未知检查范围：" + suite)
    for pattern in dict.fromkeys(python_tests):
        if Path(pattern).name != pattern or not pattern.endswith(".py"):
            raise ValueError("Python 测试必须是 tests/ 下的文件名或文件名通配符")
        commands.append({"name": "python-" + pattern, "argv": [python, "-c", UNITTEST_RUNNER, str(root), pattern], "test_kind": "unittest"})
    if node_tests:
        paths = list(dict.fromkeys(node_tests))
        if any(not (root / name).resolve().is_relative_to(root) or not (root / name).is_file() for name in paths):
            raise ValueError("Node 测试路径必须指向仓库内的现有文件")
        try:
            executable = _existing_tool("node", node, root)
            commands.append({"name": "node-contracts", "argv": [executable, "--test", "--test-reporter=tap", *paths], "version_argv": [executable, "--version"], "test_kind": "node"})
        except FileNotFoundError as exc:
            commands.append({"name": "node-contracts", "argv": [], "error": str(exc)})
    return commands


def validation_environment(output: Path, python: str) -> dict[str, str]:
    runtime_keys = {"AUTOSKILL_CHROMIUM", "AUTOSKILL_CHROMEDRIVER", "AUTOSKILL_AGENT_BROWSER", "AUTOSKILL_BROWSER", "AUTOSKILL_DSH_SDK_ROOT"}
    env = {key: value for key, value in os.environ.items() if not key.startswith("AUTOSKILL_") or key in runtime_keys or key.startswith("AUTOSKILL_REVIEW_")}
    temporary = output / "tmp"
    temporary.mkdir(parents=True, exist_ok=True)
    env.update(TMPDIR=str(temporary), TEMP=str(temporary), TMP=str(temporary),
               PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", AUTOSKILL_WORKSPACE=str(output / "workspace"),
               AUTOSKILL_CASE_SKILL=str(output / "case-sheet"), AUTOSKILL_BRIDGE="off", AUTOSKILL_HEADED="0",
               AUTOSKILL_VALIDATION_PYTHON=python, RUFF_CACHE_DIR=str(output / "ruff-cache"))
    return env


def _stop_process(process: subprocess.Popen) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
    else:
        process.kill()
    process.wait()


def _run_process(argv: list[str], root: Path, env: dict[str, str], log, timeout: float) -> int:
    process = subprocess.Popen(argv, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT,
                               shell=False, start_new_session=os.name == "posix")
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _stop_process(process)
        log.write(f"\n检查超过 {timeout:g} 秒，已终止。\n".encode())
        return 124
    except BaseException:
        _stop_process(process)
        raise


def _skip_count(command: dict, log_path: Path) -> int:
    if not command.get("test_kind"):
        return 0
    log = log_path.read_text(encoding="utf-8", errors="replace")
    expression = r"^# skipped (\d+)" if command["test_kind"] == "node" else r"^(?:OK|FAILED) \([^\n)]*skipped=(\d+)"
    counts = re.findall(expression, log, re.MULTILINE)
    return int(counts[-1]) if counts else 0


@contextlib.contextmanager
def _command_log(path: Path, name: str, show_logs: bool):
    try:
        with path.open("wb") as log:
            yield log
    finally:
        if show_logs and path.is_file():
            content = path.read_text(encoding="utf-8", errors="replace")
            print(f"\n检查日志：{name}")
            print(content, end="" if content.endswith("\n") else "\n", flush=True)


def _github_error(result: dict) -> None:
    path = Path(result["log"])
    with path.open("rb") as log:
        log.seek(max(0, path.stat().st_size - 2000))
        tail = log.read().decode("utf-8", errors="replace")
    message = f"检查失败：{result['name']}（退出码 {result['exit_code']}，跳过 {result.get('skipped', 0)}）\n{tail}"
    escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::error::{escaped}", flush=True)


def run_commands(root: Path, commands: list[dict], output: Path, timeout: float, python: str, fail_on_skip: bool = False, *, show_logs: bool = False) -> dict:
    if output.exists() and any(output.iterdir()):
        raise ValueError("输出目录非空，请使用新目录保留前次结果")
    output.mkdir(parents=True, exist_ok=True)
    env = validation_environment(output, python)
    results = []
    for index, command in enumerate(commands, 1):
        started = time.monotonic()
        log_path = output / f"{index:02d}-{command['name'].replace('*', 'all').replace('?', 'any')}.log"
        with _command_log(log_path, command["name"], show_logs) as log:
            try:
                if command.get("error"):
                    log.write((command["error"] + "\n").encode())
                    code = 127
                else:
                    if command.get("version_argv"):
                        version_code = _run_process(command["version_argv"], root, env, log, min(timeout, 15))
                        if version_code:
                            results.append({**command, "exit_code": version_code, "log": str(log_path), "seconds": round(time.monotonic() - started, 3)})
                            continue
                    code = _run_process(command["argv"], root, env, log, timeout)
            except OSError as exc:
                log.write((str(exc) + "\n").encode())
                code = 127
        skipped = _skip_count(command, log_path)
        results.append({**command, "exit_code": code, "log": str(log_path), "seconds": round(time.monotonic() - started, 3),
                        "skipped": skipped, "coverage_ok": not (fail_on_skip and skipped)})
    if show_logs and env.get("GITHUB_ACTIONS") == "true":
        for item in results:
            if item["exit_code"] or not item.get("coverage_ok", True):
                _github_error(item)
    failed = next((item["exit_code"] or 1 for item in results if item["exit_code"] or not item.get("coverage_ok", True)), 0)
    result = {"ok": failed == 0, "root": str(root), "output_dir": str(output), "results": results,
              "exit_code": failed if failed >= 0 else 128 - failed}
    (output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="用 Python 调度现有检查；显式选择范围，保留退出码和日志，不安装依赖")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--suite", choices=SUITES, action="append", default=[])
    parser.add_argument("--python-test", action="append", default=[])
    parser.add_argument("--node-test", action="append", default=[])
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--ruff")
    parser.add_argument("--biome")
    parser.add_argument("--node")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--fail-on-skip", action="store_true", help="出现跳过的测试时整体失败，保留子进程原始退出码")
    parser.add_argument("--show-logs", action="store_true", help="每项检查结束后输出完整日志，最后仍输出 JSON 摘要并保留失败退出码")
    args = parser.parse_args(argv)
    if args.list:
        print(json.dumps({"suites": SUITES, "python_suites": PYTHON_SUITES, "node_suites": NODE_SUITES}, ensure_ascii=False, indent=2))
        return 0
    try:
        root = args.root.resolve()
        if not root.is_dir() or args.timeout <= 0:
            raise ValueError("仓库必须存在，timeout 必须大于 0")
        if not args.suite and not args.python_test and not args.node_test:
            raise ValueError("必须显式选择 --suite、--python-test 或 --node-test")
        commands = build_commands(root, args.suite, args.python_test, args.node_test, args.python, args.ruff, args.biome, args.node)
        if args.dry_run:
            result = {"ok": not any(command.get("error") for command in commands), "commands": commands}
        else:
            if args.output_dir is None:
                raise ValueError("必须指定 --output-dir；可位于仓库外")
            result = run_commands(root, commands, args.output_dir.resolve(), args.timeout, args.python, args.fail_on_skip, show_logs=args.show_logs)
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result.get("exit_code", 0 if result["ok"] else 2)


if __name__ == "__main__":
    raise SystemExit(main())
