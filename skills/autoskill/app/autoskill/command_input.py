"""Explicit command inputs, runtime binding and local bridge attachment transport."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import sys
from pathlib import Path


def runtime_context(token: str | None) -> None:
    if not token:
        return
    try:
        data = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    except (ValueError, UnicodeError) as error:
        raise ValueError("运行上下文无效；请重新加载 autoSkill 插件") from error
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("workspace"), str):
        raise ValueError("运行上下文缺少有效的数据目录")
    port = data.get("port")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("运行上下文端口无效")
    os.environ["AUTOSKILL_WORKSPACE"] = str(Path(data["workspace"]).expanduser().resolve())
    os.environ["AUTOSKILL_BRIDGE_PORT"] = str(port)
    host = data.get("host", "generic")
    if not isinstance(host, str) or not host:
        raise ValueError("运行上下文宿主标识无效")
    os.environ["AUTOSKILL_HOST"] = host


def base_directory(args=None) -> Path:
    value = getattr(args, "input_base", None) if args is not None else None
    return Path(value or os.environ.get("AUTOSKILL_CALLER_CWD") or Path.cwd()).resolve()


def resolve_path(value: str, base: Path | None = None) -> str:
    path = Path(value).expanduser()
    original = (path if path.is_absolute() else (base or base_directory()) / path).resolve()
    mapping = json.loads(os.environ.get("AUTOSKILL_INPUT_MAP") or "{}")
    for source, target in sorted(mapping.items(), key=lambda item: len(item[0]), reverse=True):
        if original.is_relative_to(Path(source)):
            return str(Path(target) / original.relative_to(source))
    return str(original)


def normalize_payload(payload, command: str, action: str | None, base: Path):
    """Resolve only declared input fields, never similarly named business data."""
    value = copy.deepcopy(payload)
    if not isinstance(value, dict):
        return value
    if command == "workflow" and action in ("start", "revise"):
        for collection in ("sources", "artifacts"):
            for item in value.get(collection, []):
                if isinstance(item, dict) and isinstance(item.get("file"), str):
                    original = resolve_path(item["file"], base)
                    item.setdefault("uri", item["file"])
                    item["file"] = original
    if command == "asset" and action == "import" and isinstance(value.get("source"), str):
        value.setdefault("source_info", {"original_source": str((base / value["source"]).resolve())})
        value["source"] = resolve_path(value["source"], base)
    return value


def read_json(file: str, *, command: str = "", action: str | None = None, base: Path | None = None):
    root = base or base_directory()
    if file == "-":
        text = sys.stdin.read()
    else:
        path = Path(resolve_path(file, root))
        text = path.read_text(encoding="utf-8-sig")
        root = path.parent
    try:
        payload = json.loads(text.lstrip("\ufeff"))
    except ValueError as error:
        raise ValueError(f"输入不是有效 JSON：{error}") from error
    return normalize_payload(payload, command, action, root)


def _attachment(source: str) -> dict:
    path = Path(source)
    if not path.exists():
        raise ValueError(f"输入文件不存在：{source}")
    files = sorted(file for file in path.rglob("*") if file.is_file()) if path.is_dir() else [path]
    entries = []
    for file in files:
        data = file.read_bytes()
        entries.append({"path": file.relative_to(path).as_posix() if path.is_dir() else file.name, "mode": file.stat().st_mode & 0o777,
                        "sha256": hashlib.sha256(data).hexdigest(), "data": base64.b64encode(data).decode("ascii")})
    return {"source": str(path), "directory": path.is_dir(), "files": entries}


def _file_stdin(argv: list[str]) -> list[str]:
    result = list(argv)
    for index, arg in enumerate(result):
        if arg == "--file" and index + 1 < len(result):
            result[index + 1] = "-"
            break
        if arg.startswith("--file="):
            result[index] = "--file=-"
            break
    return result


def for_bridge(args, argv: list[str]) -> tuple[list[str], str | None, list[dict]]:
    root, action = base_directory(args), getattr(args, "action", None)
    forwarded, stdin_text, files = list(argv), None, set()
    json_commands = {"plan", "workflow", "asset", "native", "schedule", "conversation", "judge"}
    if getattr(args, "file", None) and args.command in json_commands:
        payload = read_json(args.file, command=args.command, action=action, base=root)
        stdin_text = json.dumps(payload, ensure_ascii=False)
        forwarded = _file_stdin(forwarded)
        if args.command == "workflow" and action in ("start", "revise") and isinstance(payload, dict):
            for collection in ("sources", "artifacts"):
                files.update(item["file"] for item in payload.get(collection, []) if isinstance(item, dict) and isinstance(item.get("file"), str))
        if args.command == "asset" and isinstance(payload, dict) and payload.get("source"):
            files.add(payload["source"])
    elif args.command == "import":
        files.add(resolve_path(args.file, root))
    if getattr(args, "source", None):
        files.add(resolve_path(args.source, root))
    if args.command == "secret" and action == "set" and args.value is None:
        stdin_text = sys.stdin.readline()
    return forwarded, stdin_text, [_attachment(file) for file in sorted(files)]


def materialize_attachments(attachments: list, folder: Path) -> dict:
    """Verify transported files and map explicit caller inputs to a private execution directory."""
    mapping = {}
    for index, attachment in enumerate(attachments):
        if not isinstance(attachment, dict) or not isinstance(attachment.get("source"), str) or not isinstance(attachment.get("files"), list):
            raise ValueError("输入附件格式无效")
        target = folder / str(index)
        if attachment.get("directory"):
            target /= Path(attachment["source"]).name or "input"
        target.mkdir(parents=True, exist_ok=True)
        for entry in attachment["files"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not isinstance(entry.get("data"), str):
                raise ValueError("输入附件文件条目无效")
            relative = Path(entry["path"])
            if relative.is_absolute() or ".." in relative.parts or not relative.parts:
                raise ValueError("输入附件的相对路径无效")
            try:
                data = base64.b64decode(entry["data"], validate=True)
            except (ValueError, TypeError) as error:
                raise ValueError("输入附件内容无效") from error
            if hashlib.sha256(data).hexdigest() != entry.get("sha256"):
                raise ValueError("输入附件内容校验失败")
            file = target / relative
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(data)
            if type(entry.get("mode")) is int:
                file.chmod(entry["mode"] & 0o777)
        if not attachment.get("directory"):
            if len(attachment["files"]) != 1:
                raise ValueError("单文件附件需要恰好一个文件")
            target /= attachment["files"][0]["path"]
        mapping[attachment["source"]] = str(target)
    return mapping
