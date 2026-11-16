"""Browse repository sources and run review rules against isolated validation data."""
from __future__ import annotations

import argparse
import contextlib
import fnmatch
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OMIT = {".git", ".local", ".venv", "__pycache__", "node_modules", "vendor", "workspace"}


def source_path(root: Path, name: str) -> Path:
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("文件必须位于所选仓库内")
    return path


def _fallback_inventory(root: Path) -> list[str]:
    names = []
    for folder, directories, files in os.walk(root, followlinks=False):
        directories[:] = sorted(name for name in directories if name not in OMIT and not (Path(folder) / name).is_symlink())
        names.extend((Path(folder) / name).relative_to(root).as_posix() for name in files)
    return names


def inventory(root: Path, globs: list[str]) -> list[str]:
    if not root.is_dir():
        raise ValueError("仓库目录不存在")
    executable = shutil.which("git")
    names = None
    if executable:
        result = subprocess.run([executable, "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                                cwd=root, capture_output=True, text=True, check=False, shell=False)
        if result.returncode == 0:
            names = result.stdout.split("\0")
        elif "not a git repository" not in result.stderr:
            raise ValueError(result.stderr.strip() or "Git 文件清单读取失败")
    if names is None:
        names = _fallback_inventory(root)
    return sorted({name for name in names if name and (root / name).is_file()
                   and (root / name).resolve().is_relative_to(root.resolve())
                   and (not globs or any(fnmatch.fnmatchcase(name, pattern) for pattern in globs))})


def view(root: Path, name: str, start: int, end: int) -> dict:
    if start < 1 or end < start:
        raise ValueError("行号必须从 1 开始，结束行不能早于开始行")
    lines = source_path(root, name).read_text(encoding="utf-8").splitlines()
    return {"path": name, "total_lines": len(lines), "lines": [
        {"line": index + 1, "text": value} for index, value in enumerate(lines) if start <= index + 1 <= end]}


def _searchable(name: str) -> bool:
    parts = Path(name).parts
    return not set(parts).intersection(OMIT | {"reports"}) and not any(part.startswith(".env") for part in parts)


def _rg_search(root: Path, executable: str, pattern: str, names: list[str], limit: int) -> dict:
    matches = []
    total = 0
    batches = [names[index:index + 128] for index in range(0, len(names), 128)] or [[]]
    for batch in batches:
        result = subprocess.run([executable, "--json", "--line-number", "--sort", "path", "-e", pattern, "--", *(batch or ["-"])],
                                cwd=root, input="", capture_output=True, text=True, check=False, shell=False)
        if result.returncode not in (0, 1):
            raise ValueError(result.stderr.strip() or "rg 搜索失败")
        for line in result.stdout.splitlines():
            item = json.loads(line)
            if item["type"] != "match":
                continue
            data = item["data"]
            if "text" not in data["path"] or "text" not in data["lines"]:
                continue
            total += 1
            if len(matches) < limit:
                matches.append({"path": data["path"]["text"], "line": data["line_number"], "text": data["lines"]["text"].rstrip("\r\n")})
    return {"engine": "rg", "total": total, "matches": matches}


def search(root: Path, pattern: str, globs: list[str], limit: int) -> dict:
    if limit < 0:
        raise ValueError("limit 不能小于 0")
    names = [name for name in inventory(root, globs) if _searchable(name)]
    executable = shutil.which("rg")
    if executable:
        return _rg_search(root, executable, pattern, names, limit)
    expression = re.compile(pattern)
    matches = []
    total = 0
    for name in names:
        try:
            lines = source_path(root, name).read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(lines, 1):
            if expression.search(line):
                total += 1
                if len(matches) < limit:
                    matches.append({"path": name, "line": number, "text": line})
    return {"engine": "python", "total": total, "matches": matches}


def _validate_findings(value: object) -> list[dict]:
    if not isinstance(value, list):
        raise ValueError("规则必须返回 finding 列表")
    for item in value:
        if not isinstance(item, dict) or not all(key in item for key in ("rule", "severity", "path", "line", "message")):
            raise ValueError("finding 缺少 rule/severity/path/line/message")
        if item["severity"] not in {"error", "warning", "info"}:
            raise ValueError("finding 的 severity 必须是 error/warning/info")
        if not all(isinstance(item[key], str) and item[key] for key in ("rule", "path", "message")):
            raise ValueError("finding 的 rule/path/message 必须是非空文本")
        if type(item["line"]) is not int or item["line"] < 1:
            raise ValueError("finding 的 line 必须是正整数")
    return value


def _run_rule(root: Path, file: Path) -> list[dict]:
    spec = importlib.util.spec_from_file_location("review_rule_" + file.stem, source_path(root, file.as_posix()))
    if spec is None or spec.loader is None:
        raise ValueError("无法加载规则模块")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        return _validate_findings(module.run(root))
    finally:
        sys.modules.pop(spec.name, None)


def check(root: Path, selected: list[str]) -> dict:
    folder = root / "scripts/review_rules"
    rules = [file for file in sorted(folder.glob("*.py")) if not file.name.startswith("_") and (not selected or file.stem in selected)]
    unknown = sorted(set(selected) - {file.stem for file in rules})
    if unknown or not rules:
        raise ValueError("没有可运行的规则或规则名称不存在：" + ", ".join(unknown))
    findings = []
    errors = []
    for file in rules:
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                findings.extend(_run_rule(root, file))
        except (Exception, SystemExit) as exc:  # noqa: BLE001  # one broken plugin must fail without hiding other findings
            errors.append({"rule": file.stem, "type": type(exc).__name__, "message": str(exc)})
    return {"rules": [file.stem for file in rules], "findings": findings, "rule_errors": errors,
            "ok": not errors and not any(item["severity"] == "error" for item in findings)}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="用 Python 查看、搜索和检查源码；不改仓库或用户数据，规则仅使用隔离本地验证")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--format", choices=("json", "text"), default="json")
    commands = parser.add_subparsers(dest="command", required=True)
    files = commands.add_parser("files", help="列出 Git 管理或待加入的文件；无 Git 时遍历源码目录")
    files.add_argument("--glob", action="append", default=[])
    files.add_argument("--limit", type=int, default=200)
    read = commands.add_parser("view", help="按行号查看仓库内的代码")
    read.add_argument("path")
    read.add_argument("--start", type=int, default=1)
    read.add_argument("--end", type=int, default=120)
    find = commands.add_parser("search", help="优先调用本机 rg；缺少时使用 Python 正则，不安装工具")
    find.add_argument("pattern")
    find.add_argument("--glob", action="append", default=[])
    find.add_argument("--limit", type=int, default=80)
    rules = commands.add_parser("check", help="运行仓库中的 Python 审查规则")
    rules.add_argument("--rule", action="append", default=[])
    return parser


def _render(result: dict, output_format: str) -> None:
    if output_format == "text" and "lines" in result:
        print("\n".join(f"{result['path']}:{item['line']}: {item['text']}" for item in result["lines"]))
    elif output_format == "text" and "matches" in result:
        print("\n".join(f"{item['path']}:{item['line']}: {item['text']}" for item in result["matches"]))
        print(f"匹配 {result['total']} 行，搜索引擎：{result['engine']}")
    elif output_format == "text" and "files" in result:
        print("\n".join(result["files"]))
        print(f"共 {result['total']} 个文件")
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.root.resolve()
    sys.dont_write_bytecode = True
    try:
        if getattr(args, "limit", 0) < 0:
            raise ValueError("limit 不能小于 0")
        if args.command == "files":
            names = inventory(root, args.glob)
            result = {"total": len(names), "files": names[:args.limit]}
        elif args.command == "view":
            result = view(root, args.path, args.start, args.end)
        elif args.command == "search":
            result = search(root, args.pattern, args.glob, args.limit)
        else:
            result = check(root, args.rule)
    except (OSError, ValueError, re.error, subprocess.SubprocessError) as exc:
        result = {"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}}
        _render(result, args.format)
        return 2
    _render(result, args.format)
    if result.get("rule_errors"):
        return 2
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
