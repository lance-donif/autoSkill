"""Run a command through the local autoSkill service when this process is not allowed to do the work itself.

Agent harnesses often run commands in a sandbox that cannot write outside the session folder, start a browser
or open TLS connections. The dashboard service is started by the user (or by the harness plugin) outside that
sandbox. It executes the same autoskill command and returns its output, so usage stays identical.

Only autoskill commands can be run this way, and files they write must stay inside the autoSkill data folder.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path, PureWindowsPath

from . import paths

RESTRICTED = (
    "当前命令运行在受限沙箱里（{reason}），在这里不能保存数据，也不能启动浏览器。{why}"
    "解决办法二选一：1) 让 autoSkill 后台服务在沙箱外运行——在 DeepSeek Harness 里启用 autoSkill 插件，"
    "或请用户在自己的终端里运行 {entry} dashboard 并保持窗口不关——然后原样重试本命令，它会自动交给后台服务执行；"
    "2) 给当前会话完全访问权限后重试。"
    "不要改用 AUTOSKILL_WORKSPACE 换目录或改 USERPROFILE：数据会和仪表盘分家，浏览器也仍然起不来。"
)


def restricted_message(reason: str, why: str) -> str:
    return RESTRICTED.format(reason=reason, why=why, entry=paths.SKILL / "autoskill.cmd")


DEFAULT_PORT = 8787


def enabled() -> bool:
    """Server side: may this service run commands on behalf of sandboxed callers?"""
    return os.environ.get("AUTOSKILL_BRIDGE_SERVER", "on") != "off"


def _request(port: int, path: str, body: dict | None, timeout: float) -> dict:
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                                     headers={"Content-Type": "application/json", "X-Autoskill-Bridge": "1"} if data else {})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # loopback never goes through a proxy
    with opener.open(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def service() -> tuple[int | None, str]:
    """(port, "") when our own service is reachable and willing; otherwise (None, why not)."""
    ports = []
    for file in (paths.service_file(), paths.SKILL / "workspace" / ".local" / "service.json"):
        try:
            ports.append(int(json.loads(file.read_text(encoding="utf-8"))["port"]))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    ports.append(DEFAULT_PORT)  # a service started before it learned to announce itself is still found
    same = lambda a, b: os.path.normcase(str(a)) == os.path.normcase(str(b))  # noqa: E731
    for port in dict.fromkeys(ports):
        try:
            status = _request(port, "/api/status", None, 4)
        except (OSError, ValueError):
            continue
        if status.get("name") != "autoSkill" or not same(status.get("skill_path", ""), paths.SKILL):
            continue  # something else, or another copy of autoSkill
        if "bridge" not in status:
            return None, ("正在运行的 autoSkill 后台服务是旧版本，不能代为执行命令；请重启它"
                          "（在 DeepSeek Harness 里停用再启用 autoSkill 插件，或关掉后重新运行 autoskill.cmd dashboard）。")
        if not same(status.get("workspace", ""), paths.workspace()):
            return None, (f"正在运行的 autoSkill 后台服务用的是另一个数据目录（{status.get('workspace')}）；"
                          "去掉自己设置的 AUTOSKILL_WORKSPACE 后重试。")
        if not status.get("bridge"):
            return None, "后台服务关闭了代为执行功能（AUTOSKILL_BRIDGE_SERVER=off）。"
        return port, ""
    return None, "没有发现正在运行的 autoSkill 后台服务。"


def forward(port: int, argv: list[str], stdin_text: str | None = None) -> dict:
    """Ask the service to run the command. Returns {exit, stdout, stderr}."""
    body = {"argv": argv, "stdin": stdin_text, "msys_root": os.environ.get("AUTOSKILL_MSYS_ROOT"),
            "headed": os.environ.get("AUTOSKILL_HEADED")}
    try:
        return _request(port, "/api/bridge", body, 3600)
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read().decode("utf-8")).get("detail")
        except (OSError, ValueError):
            detail = None
        message = detail if isinstance(detail, str) else f"后台服务拒绝了请求（{error.code}）"
    except (OSError, ValueError) as error:
        message = (f"和 autoSkill 后台服务的连接中断了（{type(error).__name__}）；命令可能已经执行了一部分，"
                   "先用 autoskill record status 看看再决定是否重试")
    return {"exit": 2, "stdout": json.dumps({"error": message}, ensure_ascii=False, indent=1) + chr(10), "stderr": ""}


def _parse(argv: list[str]):
    """Match CLI parsing, including native global flags after ``browser``."""
    from . import cli

    values = ["browser", "--", *argv[1:]] if argv[:1] == ["browser"] else argv
    try:
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            args = cli.parser().parse_args(values)
    except SystemExit:
        return None  # the child prints the usage/help without executing a command
    if args.command == "browser" and args.args[:1] == ["--"]:
        args.args = args.args[1:]
    return args


def _reference(value: str) -> bool:
    from . import secrets

    return bool(secrets.references(value) or value.startswith("secret:") and secrets.NAME.fullmatch(value[7:]))


def _project_input(value: str, project_id: str | None) -> Path | None:
    """Preserve a staged asset name when the native recorder resolves it by project."""
    if not value.startswith("files/") or not project_id:
        return None
    from . import artifacts, projects

    try:
        return Path(artifacts.resolve_upload_assets(projects.path(project_id), [value])[0])
    except ValueError:
        return None


def _recording_project() -> str | None:
    try:
        info = json.loads((paths.workspace() / ".local/recording.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return info.get("project_id") if isinstance(info, dict) else None


def prepare_argv(argv: list[str], cwd: Path | str | None = None) -> list[str]:
    """Keep explicit input paths anchored to the caller before service forwarding.

    URLs, selectors, text and output names retain their original spelling.
    Audio paths inside a JSON specification are resolved beside that file by
    ``protocols_cli``; only the specification path itself is rewritten here.
    """
    from .recorder import native

    args = _parse(argv)
    if args is None:
        return list(argv)
    base = Path(cwd).resolve() if cwd is not None else Path.cwd().resolve()
    project_id = getattr(args, "project", None) or (_recording_project() if args.command == "browser" else None)

    def absolute(value: str) -> str:
        if _reference(value) or _project_input(value, project_id) is not None:
            return value
        file = Path(value).expanduser()
        return str((file if file.is_absolute() else base / file).resolve())

    if args.command == "browser":
        if not args.args:
            return list(argv)
        return ["browser", *_prepare_browser_inputs(args.args, absolute)]
    values = list(argv)
    file = getattr(args, "file", None)
    if file is not None:
        if args.command == "import":
            values[1] = absolute(values[1])
        else:
            for index, value in enumerate(values):
                if value == "--file" and index + 1 < len(values):
                    values[index + 1] = absolute(values[index + 1])
                elif value.startswith("--file="):
                    values[index] = "--file=" + absolute(value.split("=", 1)[1])
    for index, value in enumerate(values):
        if value == "--audio-input" and index + 1 < len(values):
            if args.command != "project" or values[index + 1] != "off":
                values[index + 1] = absolute(values[index + 1])
        elif value.startswith("--audio-input="):
            operand = value.split("=", 1)[1]
            if args.command != "project" or operand != "off":
                values[index] = "--audio-input=" + absolute(operand)
    return values


def _prepare_browser_inputs(values: list[str], transform) -> list[str]:
    from .recorder import native

    prefix, command, rest = native.split_command(native.map_input_paths(values, transform))
    if command == "batch":
        for index, item in list(native._batch_items(rest)):
            rest[index] = shlex.join(_prepare_browser_inputs(item, transform))
    elif command == "mcp" and rest[:1] == ["call"] and len(rest) >= 3 and rest[2] != "-":
        try:
            payload = json.loads(rest[2])
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            suffix = rest[1].removeprefix("agent_browser_")
            if suffix in ("state_load", "upload"):
                if isinstance(payload.get("path"), str):
                    payload["path"] = transform(payload["path"])
                if isinstance(payload.get("files"), list):
                    payload["files"] = [transform(value) if isinstance(value, str) else value for value in payload["files"]]
            elif suffix == "diff_screenshot":
                for field in ("baseline", "baselinePath"):
                    if isinstance(payload.get(field), str):
                        payload[field] = transform(payload[field])
            if isinstance(payload.get("extraArgs"), list) and all(isinstance(value, str) for value in payload["extraArgs"]):
                payload["extraArgs"] = native.map_input_paths(["mcp_tool", *payload["extraArgs"]], transform)[1:]
            if suffix == "batch" and isinstance(payload.get("commands"), list):
                for index, item in enumerate(payload["commands"]):
                    if isinstance(item, str):
                        try:
                            payload["commands"][index] = shlex.join(_prepare_browser_inputs(shlex.split(item), transform))
                        except ValueError:
                            pass
            rest[2] = json.dumps(payload, ensure_ascii=False)
    return [*prefix, command, *rest]


def _check_output(value: str, *, private: bool = False) -> None:
    from . import artifacts

    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("输出路径无效")
    target = Path(value).expanduser()
    # A Windows drive on a POSIX host is not a portable relative filename.
    if PureWindowsPath(value).drive and not target.is_absolute():
        raise ValueError("输出路径包含当前系统无法解析的盘符")
    if private and not target.is_absolute():
        artifacts.relative_name(value)
        return  # native.private_output resolves the session's actual directory
    root = (paths.workspace() / ".local" if private else paths.workspace()).resolve()
    if not (root / target).resolve().is_relative_to(root):
        label = ".local 私有目录" if private else "autoSkill 数据目录"
        raise ValueError(f"通过后台服务执行时，输出位置必须在 {label}内（{root}）；使用相对文件名或省略输出位置")


def _check_input(value: str, *, base: Path | None = None, project_id: str | None = None, file_only: bool = False) -> Path | None:
    """Explicit inputs retain the existing bridge's user-specified read policy."""
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("输入文件路径无效")
    if _reference(value):
        return None  # state/cookie inputs may name secret content rather than a file
    staged = _project_input(value, project_id)
    target = staged or Path(value).expanduser()
    if PureWindowsPath(value).drive and not target.is_absolute():
        raise ValueError("输入路径包含当前系统无法解析的盘符")
    target = (target if target.is_absolute() else (base or paths.workspace()) / target).resolve()
    if not target.exists() or file_only and not target.is_file():
        raise ValueError("输入文件不存在或格式不符：" + target.name)
    return target


def _mcp_file_operands(tool: str, payload: dict) -> dict:
    """Known typed MCP operands; arbitrary tools remain explicitly unverified."""
    suffix = tool.removeprefix("agent_browser_")
    outputs, inputs = [], []
    captures = {"screenshot", "pdf", "download", "record_start", "record_restart", "trace_stop", "profiler_stop",
                "network_har_stop", "wait_for_download", "state_save"}
    if suffix in captures and isinstance(payload.get("path"), str):
        outputs.append(payload["path"])
    for field in ("screenshotDir", "downloadPath"):
        if isinstance(payload.get(field), str):
            outputs.append(payload[field])
    if suffix in ("state_load", "upload"):
        if isinstance(payload.get("path"), str):
            inputs.append(payload["path"])
        if isinstance(payload.get("files"), list):
            inputs.extend(value for value in payload["files"] if isinstance(value, str))
    if suffix == "diff_screenshot":
        for field in ("output", "outputPath"):
            if isinstance(payload.get(field), str):
                outputs.append(payload[field])
        for field in ("baseline", "baselinePath"):
            if isinstance(payload.get(field), str):
                inputs.append(payload[field])
    return {"outputs": outputs, "inputs": inputs}


def _check_browser(values: list[str], stdin_text: str | None = None) -> list[dict]:
    from .recorder import native
    from . import secrets

    if not values:
        return []
    _, command, rest = native.split_command(values)
    unverified = []
    if command == "batch":
        for _, item in native._batch_items(rest):
            unverified.extend(_check_browser(item, stdin_text))
    operands = native.file_operands(values)
    if command == "mcp" and rest[:1] == ["call"]:
        payload = None
        if len(rest) >= 3:
            try:
                payload = json.loads(stdin_text if rest[2] == "-" else rest[2])
            except (ValueError, TypeError):
                pass  # the child reports malformed arguments, without a false validation claim
        if isinstance(payload, dict):
            known = _mcp_file_operands(rest[1], payload)
            operands = {name: operands[name] + known[name] for name in ("inputs", "outputs")}
            if isinstance(payload.get("extraArgs"), list) and all(isinstance(value, str) for value in payload["extraArgs"]):
                extra = native.file_operands(["mcp_tool", *payload["extraArgs"]])
                operands = {name: operands[name] + extra[name] for name in ("inputs", "outputs")}
            if rest[1] == "agent_browser_batch" and isinstance(payload.get("commands"), list):
                for item in payload["commands"]:
                    if isinstance(item, str):
                        try:
                            nested = shlex.split(item)
                        except ValueError:
                            continue
                        unverified.extend(_check_browser(nested, stdin_text))
        unverified.append({"command": "mcp call", "reason": "仅检查已识别的文件参数；工具 JSON/extraArgs 的完整本机文件行为未验证"})
    elif command == "plugin" and rest[:1] == ["run"]:
        unverified.append({"command": "plugin run", "reason": "自定义插件协议不提供通用本机文件参数 schema；文件边界未验证"})
    for output in operands["outputs"]:
        reference = "${ENV:" + output[7:] + "}" if output.startswith("secret:") and secrets.NAME.fullmatch(output[7:]) else output
        _check_output(secrets.resolve(reference))
    project_id = _recording_project()
    for source in operands["inputs"]:
        _check_input(source, project_id=project_id)
    return unverified


def _check_api_request(args) -> None:
    """Inspect the consumed JSON file fields without executing or staging inputs."""
    from . import artifacts

    spec, input_root = {}, paths.workspace()
    if args.file:
        source = _check_input(args.file, file_only=True)
        if source is None:
            raise ValueError("--file 需要明确的 JSON 文件路径；凭据引用用于请求内容")
        try:
            spec = json.loads(source.read_text(encoding="utf-8-sig"))
        except ValueError as error:
            raise ValueError("--file 必须是 JSON 请求规范") from error
        if not isinstance(spec, dict):
            raise ValueError("--file 的 JSON 请求规范必须是对象")
        input_root = source.parent
    extensions = spec.get("extensions", {})
    if not isinstance(extensions, dict):
        raise ValueError("extensions 必须是对象")
    transport = spec.get("transport", extensions.get("transport", {}))
    if isinstance(transport, str):
        transport = {"type": transport}
    if not isinstance(transport, dict):
        raise ValueError("transport 必须是对象")
    steps = transport.get("steps", [])
    if not isinstance(steps, list):
        raise ValueError("transport.steps 必须是数组")
    for step in steps:
        if isinstance(step, dict) and "send_audio" in step:
            audio = step["send_audio"]
            if not isinstance(audio, dict) or not isinstance(audio.get("file"), str):
                raise ValueError("send_audio 需要明确的 file 音频文件")
            _check_input(audio["file"], base=input_root, project_id=args.project, file_only=True)
    output = transport.get("audio_output")
    if output is not None:
        if not isinstance(output, dict):
            raise ValueError("audio_output 必须是对象")
        if "filename" in output:
            artifacts.relative_name(output["filename"])
    for source in args.audio_input or []:
        _check_input(source, project_id=args.project, file_only=True)


def check(argv: list[str], stdin_text: str | None = None) -> dict | None:
    """Server side: refuse what must not be done on someone else's behalf."""
    args = _parse(argv)
    if args is None:
        return  # not a valid command line (or --help): the child prints the usage text
    if args.command == "dashboard":
        raise ValueError("后台服务已经在运行，不需要再启动 dashboard")
    if args.command == "pack" and getattr(args, "full", False):
        raise ValueError("pack --full 需要在沙箱外直接运行（它要把文件写到 autoSkill 目录之外）")
    if args.command == "pack" and getattr(args, "include_secrets", False):
        raise ValueError("带凭据的分享包需要在沙箱外直接运行 autoskill pack --include-secrets")
    if getattr(args, "out", None):
        _check_output(args.out)
    if args.command == "browser":
        unverified = _check_browser(args.args, stdin_text)
        if unverified:
            return {"status": "unverified", "details": unverified}
    elif args.command == "api" and args.api_action == "request":
        _check_api_request(args)
    elif args.command == "project" and getattr(args, "audio_input", None) not in (None, "off"):
        _check_input(args.audio_input, project_id=args.project, file_only=True)


def execute(argv: list[str], stdin_text: str | None = None, msys_root: str | None = None, headed: str | bool | None = None) -> dict:
    """Server side: run one autoskill command in its own process and return what it printed."""
    boundary = check(argv, stdin_text)
    extra = {"AUTOSKILL_BRIDGE": "off", "AUTOSKILL_WORKSPACE": str(paths.workspace())}
    if headed in ("0", "1", True):  # the caller chose; otherwise the service's own setting applies
        extra["AUTOSKILL_HEADED"] = "0" if headed == "0" else "1"
    if msys_root:
        extra["AUTOSKILL_MSYS_ROOT"] = msys_root
    # The recorder sweeps agent-io. POSIX permits unlinking our still-open output files,
    # so sharing that folder loses the command result as soon as a browser is launched.
    folder = paths.local() / "bridge-io"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        folder.chmod(0o700)
    except OSError:
        pass
    token = uuid.uuid4().hex
    out_path, err_path, in_path = folder / f"b{token}.out", folder / f"b{token}.err", folder / f"b{token}.in"
    in_path.write_text(stdin_text or "", encoding="utf-8")
    try:
        in_path.chmod(0o600)
    except OSError:
        pass
    try:
        # files, not pipes: the command may leave a browser daemon behind that would keep a pipe open forever
        with out_path.open("wb") as out, err_path.open("wb") as err, in_path.open("rb") as stdin:
            try:
                done = subprocess.run([sys.executable, "-m", "autoskill", *argv], stdin=stdin, stdout=out, stderr=err,
                                      env=paths.child_env(extra), cwd=str(paths.workspace()), timeout=3500,
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                code = done.returncode
            except subprocess.TimeoutExpired:
                code = None
        stdout = out_path.read_text(encoding="utf-8", errors="replace")
        if code is None:
            code, stdout = 2, json.dumps({"error": "命令执行超过一小时，已被后台服务终止"}, ensure_ascii=False, indent=1) + chr(10)
        stderr = err_path.read_text(encoding="utf-8", errors="replace")[-2000:]
    finally:
        for file in (out_path, err_path, in_path):
            try:
                file.unlink(missing_ok=True)
            except OSError:
                pass
    if boundary:
        try:
            result = json.loads(stdout)
        except ValueError:
            result = None
        if isinstance(result, dict):
            result["file_boundary_validation"] = boundary
            stdout = json.dumps(result, ensure_ascii=False, indent=1) + chr(10)
    return {"exit": code, "stdout": stdout, "stderr": stderr, **({"file_boundary_validation": boundary} if boundary else {})}


def announce(port: int) -> None:
    """Server side: tell sandboxed callers where the service is."""
    file = paths.local() / "service.json"
    file.write_text(json.dumps({"port": port, "pid": os.getpid()}), encoding="utf-8")


def withdraw() -> None:
    try:
        info = json.loads(paths.service_file().read_text(encoding="utf-8"))
        if info.get("pid") == os.getpid():
            paths.service_file().unlink(missing_ok=True)
    except (OSError, ValueError):
        pass
