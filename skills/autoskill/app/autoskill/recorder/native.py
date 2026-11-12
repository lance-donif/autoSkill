"""Execute agent-browser's full native surface without inventing replay support.

Portable interactions are handled by ``session``. This adapter owns native
captures, observations and administration. A successful unsupported mutation
produces an explicit ``native_step`` rather than disappearing from the case.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Callable

from .. import artifacts, paths, secrets
from . import agent


GLOBAL_VALUES = {
    "--session", "--session-name", "--namespace", "--config", "--provider", "-p", "--engine", "--cdp",
    "--cdp-headers", "--profile", "--state", "--executable-path", "--args", "--extension", "--init-script",
    "--proxy", "--proxy-bypass", "--headers", "--user-agent", "--device", "--color-scheme", "--ca-cert",
    "--idle-timeout", "--default-timeout", "--download-path", "--screenshot-dir", "--screenshot-format",
    "--screenshot-quality", "--max-output", "--allowed-domains", "--action-policy", "--confirm-actions",
    "--restore-save", "--restore-check-url", "--restore-check-fn", "--enable", "--model", "--hide-scrollbars",
}
# These options can replace the daemon/browser, change its ownership, or cause
# a launch with different sticky settings. They are usable before recording.
LIFECYCLE_FLAGS = {
    "--session", "--session-name", "--namespace", "--config", "--provider", "-p", "--engine", "--cdp",
    "--cdp-headers", "--auto-connect", "--no-auto-connect", "--profile", "--state", "--executable-path",
    "--args", "--extension", "--init-script", "--proxy", "--proxy-bypass", "--headers", "--user-agent",
    "--device", "--color-scheme", "--ca-cert", "--no-ca-cert", "--restore", "--restore-save", "--capture",
    "--relaunch", "--headed", "--headless", "--webgpu", "--enable", "--ignore-https-errors", "--allow-file-access",
    "--allowed-domains", "--no-auto-dialog", "--pin-tab", "--no-pin-tab", "--hide-scrollbars",
}
OPTION_VALUES = GLOBAL_VALUES | {
    "--threshold", "--fps", "--contact-sheet-threshold", "--timeout", "--categories", "--selector", "-s",
    "--baseline", "-b", "--output", "-o", "--format", "--quality", "--url", "--load", "--fn", "--text", "-t",
    "--llms", "--filter", "--tags", "--depth", "-d", "--wait-until", "--content", "--resource-type", "--body",
    "--allow-origin", "--name", "--label", "--port", "--allowed-origins", "--params", "--frame", "--tools",
    "--curl", "--password", "--username", "--credential-provider", "--item", "--domain", "--path", "--expires",
    "--same-site", "--sameSite", "--password-selector", "--username-selector", "--submit-selector",
}
INPUT_FLAGS = {"--config", "--state", "--init-script", "--extension", "--ca-cert", "--action-policy"}
OUTPUT_FLAGS = {"--download-path", "--screenshot-dir"}
SENSITIVE_FLAG = re.compile(r"(?:password|passwd|username|token|secret|authorization|credential|api[-_]?key|headers|payload|body)", re.I)
SENSITIVE_URL = re.compile(r"(?i)(?:[?&](?:token|password|secret|auth|api[-_]?key|signature|session)[^=]*=|://[^/\s]+:[^/\s]+@)")
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


def split_command(argv: list[str]) -> tuple[list[str], str, list[str]]:
    """Split leading global flags, without consuming command-local --state."""
    index = 0
    while index < len(argv) and argv[index].startswith("-"):
        flag, equal, _ = argv[index].partition("=")
        if flag in ("--help", "-h", "--version", "-V"):
            return argv[:index], flag, argv[index + 1:]
        index += 1
        if flag in GLOBAL_VALUES and not equal:
            index += 1
        elif flag == "--restore" and not equal and index < len(argv) and not argv[index].startswith("-"):
            # The optional restore name is ambiguous; known command words are
            # commands, any other token is its explicit restore name.
            if argv[index] not in COMMANDS:
                index += 1
    if index >= len(argv):
        raise ValueError("缺少 agent-browser 命令")
    return argv[:index], argv[index], argv[index + 1:]


COMMANDS = {
    "open", "goto", "navigate", "read", "back", "forward", "reload", "pushstate", "close", "quit", "exit",
    "snapshot", "click", "dblclick", "focus", "fill", "type", "press", "key", "keydown", "keyup", "keyboard",
    "hover", "check", "uncheck", "select", "scroll", "scrollintoview", "scrollinto", "drag", "upload", "download",
    "get", "is", "screenshot", "pdf", "record", "wait", "mouse", "find", "set", "cookies", "storage", "network",
    "flow", "tab", "window", "frame", "dialog", "eval", "auth", "plugin", "state", "stream", "webmcp",
    "dashboard", "install", "upgrade", "update", "doctor", "mcp", "console", "errors", "highlight", "inspect",
    "trace", "profiler", "react", "vitals", "a11y", "removeinitscript", "session", "profiles", "profile",
    "skills", "device", "tap", "swipe", "clipboard", "diff", "batch", "confirm", "deny", "chat", "connect",
}


def _positionals(args: list[str], start: int = 0) -> list[int]:
    found, index, literal = [], start, False
    while index < len(args):
        value = args[index]
        if value == "--":
            literal = True
        elif literal or not value.startswith("-"):
            found.append(index)
        elif "=" not in value and value in OPTION_VALUES:
            index += 1
        index += 1
    return found


def _flag_value(args: list[str], flag: str) -> tuple[int | None, str | None]:
    return next(_flag_values(args, flag), (None, None))


def _flag_values(args: list[str], flag: str):
    for index, value in enumerate(args):
        if value == "--":
            break
        if value == flag and index + 1 < len(args) and not args[index + 1].startswith("--"):
            yield index + 1, args[index + 1]
        if value.startswith(flag + "="):
            yield index, value.split("=", 1)[1]


def _set_flag(args: list[str], flag: str, value: str) -> None:
    index, _ = _flag_value(args, flag)
    if index is None:
        args.extend([flag, value])
    elif args[index].startswith(flag + "="):
        args[index] = flag + "=" + value
    else:
        args[index] = value


def _screenshot_index(args: list[str]) -> int | None:
    positional = _positionals(args)
    if len(positional) >= 2:
        return positional[1]
    if positional:
        value = args[positional[0]]
        # A single selector is accepted by native 0.42. Images have an
        # extension; an explicit filesystem path is also unambiguous.
        if Path(value).suffix.lower() in IMAGE_SUFFIXES or value.startswith(("/", "~/", "./", "../")) or re.match(r"^[A-Za-z]:", value):
            return positional[0]
    return None


def _batch_items(rest: list[str]):
    for index, item in enumerate(rest):
        if not item.startswith("-"):
            try:
                yield index, shlex.split(item, posix=True)
            except ValueError as error:
                raise ValueError("batch 子命令的引号不完整") from error


def output_operands(argv: list[str]) -> list[str]:
    """Explicit output paths for bridge checks; this function performs no I/O."""
    prefix, command, rest = split_command(list(argv))
    found = [value for flag in sorted(OUTPUT_FLAGS) for _, value in _flag_values(prefix + rest, flag)]
    if command == "batch":
        return found + [value for _, item in _batch_items(rest) for value in output_operands(item)]
    index = None
    if command == "screenshot":
        index = _screenshot_index(rest)
    elif command == "pdf" and rest:
        index = 0
    elif command == "download" and len(rest) >= 2:
        index = 1
    elif command == "record" and len(rest) >= 2 and rest[0] in ("start", "restart"):
        index = 1
    elif command in ("trace", "profiler", "state") and len(rest) >= 2 and rest[0] in ("stop", "save"):
        index = 1
    elif command == "network" and rest[:2] == ["har", "stop"] and len(rest) >= 3:
        index = 2
    if index is not None:
        found.append(rest[index])
    if command == "wait":
        found.extend(value for _, value in _flag_values(rest, "--download"))
    if command == "diff" and rest[:1] == ["screenshot"]:
        for flag in ("--output", "-o"):
            found.extend(value for _, value in _flag_values(rest, flag))
    return found


def upload_input_paths(argv: list[str]) -> list[str]:
    """Explicit native file inputs, including nested batch commands, without I/O."""
    prefix, command, rest = split_command(list(argv))
    found = [value for flag in sorted(INPUT_FLAGS) for _, value in _flag_values(prefix, flag)]
    if command == "batch":
        return found + [value for _, item in _batch_items(rest) for value in upload_input_paths(item)]
    if command == "upload":
        found.extend(rest[i] for i in _positionals(rest)[1:])
    elif command == "state" and rest[:1] == ["load"] and len(rest) >= 2:
        found.append(rest[1])
    elif command == "cookies" and rest[:1] == ["set"]:
        found.extend(value for _, value in _flag_values(rest, "--curl"))
    if command == "diff":
        for flag in ("--baseline", "-b"):
            found.extend(value for _, value in _flag_values(rest, flag))
    if command == "webmcp":
        found.extend(value[1:] for _, value in _flag_values(rest, "--params") if value.startswith("@"))
    if command == "eval":
        found.extend(value for _, value in _flag_values(rest, "--script-file"))
    # Global file options can follow a command too; selector wait --state is
    # intentionally excluded because it denotes an element state there.
    for flag in INPUT_FLAGS - {"--state"}:
        found.extend(value for _, value in _flag_values(rest, flag))
    return list(dict.fromkeys(found))


def file_operands(argv: list[str]) -> dict:
    return {"inputs": upload_input_paths(argv), "outputs": output_operands(argv)}


def map_input_paths(argv: list[str], transform: Callable[[str], str]) -> list[str]:
    """Rewrite only native file operands, preserving selectors, text and output names.

    Used before forwarding to a service with a different working directory.
    The transformation itself decides how to handle credential references.
    """
    prefix, command, rest = split_command(list(argv))

    def flag(values: list[str], name: str, *, at_file: bool = False) -> None:
        for index, value in list(_flag_values(values, name)):
            if at_file and not value.startswith("@"):
                continue
            mapped = ("@" if at_file else "") + transform(value[1:] if at_file else value)
            values[index] = name + "=" + mapped if values[index].startswith(name + "=") else mapped

    for name in INPUT_FLAGS:
        flag(prefix, name)
    for name in INPUT_FLAGS - {"--state"}:
        flag(rest, name)
    if command == "batch":
        for index, item in list(_batch_items(rest)):
            rest[index] = shlex.join(map_input_paths(item, transform))
    elif command == "upload":
        for index in _positionals(rest)[1:]:
            rest[index] = transform(rest[index])
    elif command == "state" and rest[:1] == ["load"] and len(rest) >= 2:
        rest[1] = transform(rest[1])
    elif command == "cookies" and rest[:1] == ["set"]:
        flag(rest, "--curl")
    elif command == "diff":
        flag(rest, "--baseline")
        flag(rest, "-b")
    elif command == "webmcp":
        flag(rest, "--params", at_file=True)
    elif command == "eval":
        flag(rest, "--script-file")
    return [*prefix, command, *rest]


def guard_recording(argv: list[str], rec: dict | None) -> None:
    if rec is None:
        return
    prefix, command, rest = split_command(list(argv))
    if command == "batch":
        for _, item in _batch_items(rest):
            guard_recording(item, rec)
    def check_flags(values: list[str], *, local: bool = False) -> None:
        index = 0
        literal_operands = set()
        if local:
            if command in ("fill", "type") and len(values) == 2:
                literal_operands = {0, 1}
            elif command in ("keyboard", "clipboard") and len(values) == 2 and values[0] in ("type", "inserttext", "write"):
                literal_operands = {1}
            elif command == "cookies" and values[:1] == ["set"] and len(values) >= 3 and not values[1].startswith("-"):
                literal_operands = {1, 2}
            elif command == "storage" and len(values) >= 4 and values[1] == "set":
                literal_operands = {2, 3}
        while index < len(values):
            value = values[index]
            if value == "--":
                break
            if index in literal_operands:
                index += 1
                continue
            flag, equal, _ = value.partition("=")
            if flag in LIFECYCLE_FLAGS:
                # wait's command-local --state denotes an element state.
                if not (local and command == "wait" and flag == "--state"):
                    raise ValueError(f"{flag} 会改变正在录制的浏览器配置；先 record stop 或 record cancel，再设置并重新开始录制")
            if flag in OPTION_VALUES and not equal:
                index += 1
            index += 1

    check_flags(prefix)
    check_flags(rest, local=True)
    if command in ("close", "quit", "exit", "connect", "upgrade", "update") or command == "doctor" and "--fix" in rest:
        raise ValueError(f"{command} 会关闭或替换正在录制的浏览器；先 record stop 或 record cancel")
    if command == "network" and rest[:1] == ["har"] and rest[1:2] in (["start"], ["stop"]):
        raise ValueError("HAR 由当前录制持有；record stop 会保存并结束流量捕获，不能在录制中接管或停止它")


def validate_recording_args(argv: list[str]) -> None:
    """Validate recorder ownership before dispatching even portable actions."""
    guard_recording(argv, {})


def _secret_ref(rec: dict | None, text: str, label: str, callback: Callable | None) -> str:
    if text.startswith("secret:") and secrets.NAME.fullmatch(text[7:]):
        text = "${ENV:" + text[7:] + "}"
    if secrets.references(text):
        if rec is not None:
            rec["secrets"] = sorted({*rec.get("secrets", []), *secrets.references(text)})
        return text
    if callback:
        return callback(text, label)
    project = str((rec or {}).get("project_id", "BROWSER")).upper().replace("-", "_")
    clean = re.sub(r"[^A-Za-z0-9_]", "_", label.upper()).strip("_") or "VALUE"
    base = f"{project}_{clean}"
    name, count = base, 2
    while secrets.get_secret(name) not in (None, text):
        name, count = f"{base}_{count}", count + 1
    secrets.set_secret(name, text)
    if rec is not None:
        rec["secrets"] = sorted({*rec.get("secrets", []), name})
    return "${ENV:" + name + "}"


def _safe_args(command: str, rest: list[str], rec: dict | None, callback: Callable | None) -> list[str]:
    safe = list(rest)
    sensitive = set()
    if command == "set" and rest[:1] == ["headers"]:
        sensitive.update(range(1, len(rest)))
    elif command == "set" and rest[:1] in (["credentials"], ["auth"]):
        sensitive.update(range(1, len(rest)))
    elif command == "cookies" and rest[:1] == ["set"] and len(rest) >= 3 and rest[1] != "--curl":
        sensitive.add(2)
    elif command == "storage" and len(rest) >= 4 and rest[1] == "set":
        sensitive.add(3)
    elif command in ("eval", "chat", "batch"):
        sensitive.update(i for i, value in enumerate(rest) if not value.startswith("-"))
    elif command in ("fill", "type", "select"):
        sensitive.update(_positionals(rest)[1:])
    elif command == "keyboard" and rest[:1] in (["type"], ["inserttext"]):
        sensitive.update(range(1, len(rest)))
    elif command == "dialog" and rest[:1] == ["accept"]:
        sensitive.update(range(1, len(rest)))
    elif command == "find" and any(value in ("fill", "type", "select") for value in rest) and rest:
        sensitive.add(len(rest) - 1)
    elif command == "clipboard" and rest[:1] == ["write"]:
        sensitive.update(range(1, len(rest)))
    for index, value in enumerate(rest):
        flag, equal, content = value.partition("=")
        if flag.startswith("-") and SENSITIVE_FLAG.search(flag):
            if equal:
                safe[index] = flag + "=" + _secret_ref(rec, content, command + "_" + flag, callback)
            elif index + 1 < len(rest):
                sensitive.add(index + 1)
        if SENSITIVE_URL.search(value):
            sensitive.add(index)
    for index in sorted(sensitive):
        safe[index] = _secret_ref(rec, rest[index], command + "_value", callback)
    # Absolute input/output paths are runtime details. Their actual content is
    # separately staged or referenced by the command-specific handlers below.
    for index, value in enumerate(safe):
        if Path(value).is_absolute() or re.match(r"^[A-Za-z]:[\\/]", value):
            safe[index] = Path(value.replace("\\", "/")).name
    return safe


def _observation(command: str, rest: list[str]) -> bool:
    if command in ("read", "get", "is", "snapshot", "screenshot", "pdf", "console", "errors", "highlight", "inspect",
                   "profiles", "skills", "doctor", "install", "--help", "-h", "--version", "-V", "trace", "profiler"):
        return True
    if command in ("a11y", "vitals"):
        return not _positionals(rest)
    if command == "record":
        return len(_positionals(rest, 1)) <= (1 if rest[:1] in (["start"], ["restart"]) else 0)
    if command == "cookies":
        return not rest or rest[0] == "get"
    if command == "storage":
        return len(rest) < 2 or rest[1] not in ("set", "clear")
    if command == "clipboard":
        return not rest or rest[0] in ("read", "copy")
    if command == "network":
        return rest[:1] in (["requests"], ["request"], ["har"])
    if command == "state":
        return rest[:1] in (["save"], ["list"], ["show"])
    if command == "auth":
        return rest[:1] in (["list"], ["show"])
    if command == "plugin":
        return rest[:1] in (["list"], ["show"])
    if command == "flow":
        return rest[:1] in (["list"], ["steps"])
    if command in ("session", "profile"):
        return not rest or rest[0] in ("list", "info", "id", "show")
    if command == "react":
        return True
    if command == "dialog":
        return rest[:1] == ["status"]
    if command == "tab":
        return not rest or rest[:1] == ["list"]
    if command == "stream":
        return rest[:1] == ["status"]
    if command == "diff":
        return rest[:1] != ["url"]
    if command == "webmcp":
        return rest[:1] in (["list"], ["result"])
    if command == "device":
        return rest[:1] == ["list"]
    return False


def _portable(command: str, rest: list[str]) -> bool:
    """The subset jointly implemented by both drivers and code export."""
    from ..replay.primitives import normalize_browser_command

    try:
        normalize_browser_command(command, secrets.resolve(rest))
    except (ValueError, TypeError):
        return False
    return True


def _raw_call(session: str, argv: list[str], *, launch: list[str] | None = None, stdin_text: str | None = None,
              timeout: float = 180) -> dict:
    """Finite native/admin commands may emit text rather than the daemon JSON envelope."""
    binary = paths.agent_browser()
    if not binary:
        raise agent.AgentError("没有找到 agent-browser；请配置 AUTOSKILL_AGENT_BROWSER 或使用完整包")
    folder = paths.local() / "native-io"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    token = uuid.uuid4().hex
    out_file, err_file, in_file = [folder / (token + suffix) for suffix in (".out", ".err", ".in")]
    try:
        if stdin_text is not None:
            in_file.write_text(stdin_text, encoding="utf-8")
            in_file.chmod(0o600)
        with out_file.open("wb") as out, err_file.open("wb") as err:
            input_stream = in_file.open("rb") if stdin_text is not None else subprocess.DEVNULL
            try:
                done = subprocess.run([binary, "--session", session, *(launch or []), *argv, "--json"], stdin=input_stream, stdout=out,
                                      stderr=err, env=paths.child_env(), timeout=timeout,
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except subprocess.TimeoutExpired:
                raise agent.AgentError("原生命令超时；命令可能已产生部分效果，请检查当前状态后再决定是否重试") from None
            finally:
                if stdin_text is not None:
                    input_stream.close()
        text = out_file.read_text(encoding="utf-8", errors="replace").strip()
        error = err_file.read_text(encoding="utf-8", errors="replace").strip()
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict) and "success" in parsed:
            if not parsed["success"]:
                raise agent.AgentError(str(parsed.get("error") or error or "原生命令失败")[:600])
            return parsed.get("data") or {}
        if done.returncode:
            raise agent.AgentError((error or text or f"原生命令退出码 {done.returncode}")[:600])
        if parsed is not None:
            return parsed if isinstance(parsed, dict) else {"data": parsed}
        return {"stdout": text, **({"stderr": error} if error else {})}
    finally:
        for file in (out_file, err_file, in_file):
            agent._discard(file)


def _register(rec: dict | None, items: list[dict]) -> None:
    if rec is None:
        return
    known = {item["path"]: item for item in rec.get("artifacts", [])}
    known.update({item["path"]: item for item in items})
    rec["artifacts"] = list(known.values())


def _local_path(value: str) -> Path:
    target = (paths.local() / artifacts.relative_name(value)).resolve()
    if not target.is_relative_to(paths.local().resolve()):
        raise ValueError("私有证据路径不能离开 .local")
    return target


def _capture_state(session: str, rec: dict | None) -> tuple[dict, Path | None]:
    if rec is not None:
        return rec.setdefault("native_captures", {}), None
    file = artifacts.private_root(session) / "capture-state.json"
    try:
        return json.loads(file.read_text(encoding="utf-8")), file
    except FileNotFoundError:
        return {}, file


def execute(session: str, argv: list[str], rec: dict | None = None, *, store_value: Callable[[str, str], str] | None = None,
            stdin_text: str | None = None) -> dict:
    """Return result, optional recordable step, artifacts, and affects_page.

    The caller owns recording persistence and tab bookkeeping. No recording
    file, HAR session or browser ownership is changed behind its back.
    """
    if not argv:
        raise ValueError("缺少 agent-browser 命令")
    argv = ["${ENV:" + value[7:] + "}" if value.startswith("secret:") and secrets.NAME.fullmatch(value[7:]) else value
            for value in argv]
    guard_recording(argv, rec)
    prefix, command, rest = split_command(list(argv))
    safe = _safe_args(command, rest, rec, store_value)
    send = list(rest)
    capture_paths: list[tuple[Path, str]] = []
    temporary: list[Path] = []
    completed = []
    captures, captures_file = _capture_state(session, rec)
    observation = _observation(command, rest)
    is_admin = command in ("install", "upgrade", "update", "doctor", "skills", "dashboard", "--help", "-h", "--version", "-V")
    run_session = session + "-admin" if is_admin and rec is not None else session

    def output(requested: str | None, suffix: str, kind: str = "capture") -> Path:
        value = artifacts.private_output(session, requested, category="downloads" if kind == "download" else "captures",
                                         suffix=suffix, rec=rec)
        capture_paths.append((value, kind))
        return value

    if command == "batch":
        # Run items sequentially through this same adapter, so nested lifecycle,
        # file and secret handling cannot bypass the recording contract.
        results, nested_steps = [], []
        for index, item in _batch_items(rest):
            try:
                child = execute(session, [*prefix, *item], rec, store_value=store_value)
                results.append({"index": index, **child["result"]})
                completed.extend(child["artifacts"])
                if child.get("step"):
                    nested_steps.append(child["step"])
            except (ValueError, agent.AgentError) as error:
                results.append({"index": index, "error": str(error)})
                if "--bail" in rest:
                    break
        step = None
        if nested_steps:
            step = {"action": "native_step", "command": "batch", "args": [json.dumps(nested_steps, ensure_ascii=False)],
                    "replayable": False, "reason": "原生 batch 已执行；该组合命令尚无跨引擎回放语义"}
        return {"result": {"results": results}, "step": step, "artifacts": completed, "affects_page": bool(nested_steps)}

    if command == "mcp":
        if rest[:1] != ["call"]:
            binary = paths.agent_browser()
            return {"result": {"transport": "stdio", "executed": False,
                               "command": [binary or "agent-browser", "--session", session + "-mcp", *prefix, "mcp", *rest],
                               "note": "MCP 服务器需要客户端保持 stdin/stdout；将此命令配置到 MCP 客户端。单次调用使用 mcp call。"},
                    "step": None, "artifacts": [], "affects_page": False}
        # One-shot tools get an isolated session during an active recording;
        # their nested CLI flags cannot replace its page or steal its HAR.
        if rec is not None and len(send) >= 3:
            payload_text = stdin_text if send[2] == "-" else send[2]
            try:
                payload = json.loads(payload_text or "{}")
            except ValueError as error:
                raise ValueError("mcp call 参数必须是 JSON 对象") from error
            if not isinstance(payload, dict):
                raise ValueError("mcp call 参数必须是 JSON 对象")
            payload["session"] = session + "-mcp"
            send[2] = json.dumps(payload, ensure_ascii=False)
            run_session = session + "-mcp"
        is_admin, observation = True, True

    if command == "chat" and not _positionals(send):
        raise ValueError("交互式 chat 需要终端；请提供单次提示词，例如 browser chat '读取页面标题'")
    if command == "screenshot":
        index = _screenshot_index(send)
        suffix = ".jpg" if _flag_value(send, "--screenshot-format")[1] in ("jpeg", "jpg") else ".png"
        path = output(send[index] if index is not None else None, suffix, "screenshot")
        if index is None:
            send.append(str(path))
        else:
            send[index] = str(path)
    elif command == "pdf":
        path = output(send[0] if send else None, ".pdf", "pdf")
        send = [str(path), *send[1:]]
    elif command == "download" and len(send) >= 2:
        path = output(send[1], "", "download")
        send[1], safe[1] = str(path), path.name
    elif command == "wait" and any(a.split("=", 1)[0] == "--download" for a in send):
        if sum(a.split("=", 1)[0] == "--download" for a in send) != 1:
            raise ValueError("wait --download 只能指定一个输出文件")
        _, requested = _flag_value(send, "--download")
        path = output(requested, ".download", "download")
        _set_flag(send, "--download", str(path))
    elif command == "upload" and len(send) >= 2:
        positions = _positionals(send)[1:]
        if rec is None:
            resolved = [str(Path(secrets.resolve(send[i])).expanduser().resolve()) for i in positions]
            if any(not Path(name).is_file() for name in resolved):
                raise ValueError("上传文件不存在或不是普通文件")
            staged = [Path(name).name for name in resolved]
        else:
            staged = artifacts.stage_upload(rec["project_id"], [secrets.resolve(send[i]) for i in positions])
            from .. import projects
            resolved = artifacts.resolve_upload_assets(projects.path(rec["project_id"]), staged)
        for index, absolute, relative in zip(positions, resolved, staged):
            send[index], safe[index] = absolute, relative
    elif command == "record" and send[:1] in (["start"], ["restart"]):
        if not shutil.which("ffmpeg"):
            raise ValueError("视频录制需要 PATH 中的 ffmpeg（libvpx/libx264）；其他浏览器功能不依赖 ffmpeg")
        path = output(send[1] if len(send) > 1 else None, ".webm", "video")
        if len(send) > 1:
            send[1] = str(path)
        else:
            send.append(str(path))
        capture_paths.clear()  # encoder output is complete only after stop/restart
    elif command in ("trace", "profiler") and send[:1] == ["start"]:
        other = "profiler" if command == "trace" else "trace"
        if captures.get(other):
            raise ValueError(f"{other} 正在使用 CDP Tracing；先停止它再开始 {command}")
    elif command in ("trace", "profiler") and send[:1] == ["stop"]:
        path = output(send[1] if len(send) > 1 else None, ".json", command)
        send = ["stop", str(path), *send[2:]]
    elif command == "network" and send[:2] == ["har", "stop"]:
        path = output(send[2] if len(send) > 2 else None, ".har", "har")
        send = ["har", "stop", str(path), *send[3:]]
    elif command == "diff" and send[:1] == ["screenshot"]:
        requested = _flag_value(send, "--output")[1] or _flag_value(send, "-o")[1]
        path = output(requested, ".png", "diff")
        flag = "-o" if _flag_value(send, "-o")[0] is not None else "--output"
        _set_flag(send, flag, str(path))
        for option in ("--output", "-o"):
            for index, _ in list(_flag_values(send, option)):
                send[index] = option + "=" + str(path) if send[index].startswith(option + "=") else str(path)
    elif command == "state" and send[:1] == ["save"]:
        requested = send[1] if len(send) > 1 else None
        state_output = output(requested, ".json", "state")
        send = ["save", str(state_output), *send[2:]]
    elif command == "state" and send[:1] == ["load"] and len(send) >= 2:
        value = send[1]
        if secrets.references(value):
            content = secrets.resolve(value)
            reference = value
        else:
            source = Path(value).expanduser()
            content = source.read_text(encoding="utf-8")
            if source.suffix == ".enc":
                try:
                    json.loads(content)
                except ValueError:
                    content = secrets.unseal(content)  # compatibility with earlier sealed captures
            reference = _secret_ref(rec, content, "STATE", store_value)
        # Validate before writing an ephemeral plaintext auth-state file.
        if not isinstance(json.loads(content), dict):
            raise ValueError("浏览器状态必须是 JSON 对象")
        plain = artifacts.private_output(session, category="transient", suffix=".json", rec=rec)
        plain.write_text(content, encoding="utf-8")
        plain.chmod(0o600)
        temporary.append(plain)
        send[1], safe[1] = str(plain), reference
    elif command == "cookies" and send[:1] == ["set"]:
        index, filename = _flag_value(send, "--curl")
        if filename is not None:
            if secrets.references(filename):
                content, reference = secrets.resolve(filename), filename
                plain = artifacts.private_output(session, category="transient", suffix=".curl", rec=rec)
                plain.write_text(content, encoding="utf-8")
                plain.chmod(0o600)
                temporary.append(plain)
                _set_flag(send, "--curl", str(plain))
            else:
                content = Path(filename).expanduser().read_text(encoding="utf-8")
                reference = _secret_ref(rec, content, "COOKIES", store_value)
            safe[index] = "--curl=" + reference if send[index].startswith("--curl=") else reference

    # Default directory flags must also stay private, including when the native
    # command writes extra screenshots or downloads beside its main result.
    for flag, category in (("--download-path", "downloads"), ("--screenshot-dir", "captures")):
        for values in (prefix, send):
            for index, requested in list(_flag_values(values, flag)):
                folder = artifacts.private_output(session, requested, category=category, rec=rec)
                folder.mkdir(parents=True, exist_ok=True, mode=0o700)
                values[index] = flag + "=" + str(folder) if values[index].startswith(flag + "=") else str(folder)

    try:
        resolved = secrets.resolve([*prefix, command, *send])
        if is_admin or command in ("auth", "plugin", "state", "session", "profiles", "profile", "chat"):
            launches_browser = command == "chat" or command == "auth" and send[:1] == ["login"] or command == "state" and send[:1] in (["load"], ["save"])
            data = _raw_call(run_session, resolved, launch=agent.launch_args([]) if rec is None and launches_browser else None,
                             stdin_text=stdin_text,
                             timeout=1800 if command in ("install", "upgrade", "update") else 180)
        else:
            data = agent.call(run_session, resolved, stdin_text=stdin_text,
                              launch=agent.launch_args([]) if rec is None else None, timeout=180)
        if not isinstance(data, dict):
            data = {"data": data}
        if command == "state" and send[:1] == ["save"]:
            data["encrypted"] = False
        if command == "record" and send[:1] in (["stop"], ["restart"]):
            previous = captures.pop("record", None)
            if previous and _local_path(previous).is_file():
                completed.append(artifacts.metadata(_local_path(previous), kind="video"))
        if command == "record" and send[:1] in (["start"], ["restart"]):
            captures["record"] = Path(send[1]).resolve().relative_to(paths.local().resolve()).as_posix()
            data["artifact_pending"] = {"path": captures["record"], "kind": "video"}
            if "path" in data:
                data["path"] = captures["record"]
        if command in ("trace", "profiler") and send:
            if send[0] == "start":
                captures[command] = True
            elif send[0] == "stop":
                captures.pop(command, None)
        for file, kind in capture_paths:
            if file.is_file():
                completed.append(artifacts.metadata(file, kind=kind))
        _register(rec, completed)
        if captures_file is not None:
            captures_file.write_text(json.dumps(captures, ensure_ascii=False), encoding="utf-8")
            captures_file.chmod(0o600)
    except (agent.AgentError, ValueError) as error:
        # Native failures can echo arguments. Replace protected literal values
        # before returning the message to logs or a caller.
        problem = str(error)
        for actual, stored in zip(rest, safe):
            if actual and secrets.references(stored):
                problem = problem.replace(actual, stored)
        raise type(error)(problem) from None
    finally:
        for file in temporary:
            file.unlink(missing_ok=True)

    step = None
    if not observation and not is_admin:
        if command == "clipboard" and len(safe) == 2 and safe[0] == "write":
            step = {"action": "clipboard", "operation": "write", "text": safe[1]}
        elif not prefix and _portable(command, rest):
            step = {"action": "browser_command", "command": command, "args": safe}
        else:
            step = {"action": "native_step", "command": command, "args": safe, "replayable": False,
                    "reason": "原生命令已执行；该命令或选项尚无完整的跨引擎回放与代码导出语义"}
            if prefix:
                step["global_args"] = _safe_args("flags", prefix, rec, store_value)
    if completed:
        data["artifacts"] = completed
        # Only the response is returned, but keep its paths portable as well.
        if "path" in data:
            data["path"] = completed[-1]["path"]
    if run_session != session:
        data["isolated_session"] = run_session
    return {"result": data, "step": step, "artifacts": completed,
            "affects_page": bool(step) and command not in ("plugin", "session", "profile", "stream")
                and not (command == "auth" and rest[:1] != ["login"])}


execute_native = execute


def finish_captures(session: str, rec: dict) -> list[dict]:
    """Finish diagnostic encoders before the recorder closes its browser."""
    saved = []
    for command in ("record", "trace", "profiler"):
        if rec.get("native_captures", {}).get(command):
            saved.extend(execute(session, [command, "stop"], rec)["artifacts"])
    return saved


def ensure_permissions(session: str, endpoint: str, rec: dict) -> None:
    from .permissions import ensure_permissions as ensure

    ensure(session, endpoint, rec)


def stop_permission_holder(rec: dict) -> None:
    from .permissions import stop_permission_holder as stop

    stop(rec)

