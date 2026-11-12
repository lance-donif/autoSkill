"""Project API requests and portable HTTP/stream/voice case specifications."""
from __future__ import annotations

import copy
import importlib.util
import json
import math
import re
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import artifacts, builder, paths, projects, secrets, sessions
from .core import store
from .replay import api


TRANSPORTS = ("http", "sse", "websocket")
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
SENSITIVE = re.compile(r"authorization|cookie|token|secret|password|passwd|api[-_]?key|credential|session[-_]?id|signature", re.I)


def register(subparsers) -> None:
    """Register beneath the top-level ``api`` command; no services are opened."""
    subparsers.add_parser("capabilities", help="查询 HTTP、SSE、WebSocket 和语音接口能力")
    request = subparsers.add_parser("request", help="执行项目接口请求，可保存为可回放用例")
    request.add_argument("project")
    request.add_argument("--method", type=str.upper, choices=METHODS)
    request.add_argument("--path", help="项目服务下的相对接口路径，例如 /api/tickets")
    request.add_argument("--transport", choices=TRANSPORTS)
    request.add_argument("--file", help="JSON 请求/流式会话规范；音频文件会复制进项目")
    request.add_argument("--headers", help="请求头 JSON；敏感值只在本机保存，用例保留凭据名")
    request.add_argument("--params", help="查询参数 JSON")
    request.add_argument("--json-body", help="JSON 请求体")
    request.add_argument("--form", help="表单字段 JSON")
    request.add_argument("--expect-status", type=int)
    request.add_argument("--expect-json", help="JSON Pointer 到预期值的 JSON 对象")
    request.add_argument("--expect-text", action="append", default=[])
    request.add_argument("--expect-reasoning", action="append", default=[])
    request.add_argument("--min-events", type=int)
    request.add_argument("--min-messages", type=int)
    request.add_argument("--min-audio-frames", type=int)
    request.add_argument("--min-audio-bytes", type=int)
    request.add_argument("--max-ms", type=int)
    request.add_argument("--timeout-seconds", type=float, help="整个请求/流式会话的时间预算")
    request.add_argument("--idle-timeout-seconds", type=float, help="流式事件之间允许的最长空闲时间")
    request.add_argument("--first-event-timeout-seconds", type=float, help="等待首个流式事件的时间预算")
    request.add_argument("--max-first-event-ms", type=int)
    request.add_argument("--max-first-text-ms", type=int)
    request.add_argument("--max-first-audio-ms", type=int)
    request.add_argument("--audio-input", action="append", default=[], help="添加 send_audio 步骤；具体会话事件可用 --file 设置")
    request.add_argument("--save-case", metavar="TITLE", help="将请求与断言保存到当前项目")
    request.add_argument("--module", help="保存用例所属模块")


def capabilities() -> dict:
    return {"verified": False, "verification": "静态能力说明；实际可用性由依赖和目标服务决定", "capabilities": [
        {"transport": "http", "methods": list(METHODS), "request_bodies": ["json", "form"],
         "assertions": ["status", "headers", "body", "json", "schema", "duration"], "replayable": True, "verified": False},
        {"transport": "sse", "methods": list(METHODS), "features": ["event stream", "text", "reasoning", "completion", "timeouts"],
         "replayable": True, "verified": False},
        {"transport": "websocket", "features": ["ordered send/receive", "text", "reasoning", "audio input", "audio output", "timeouts"],
         "dependency": "websocket-client", "available": importlib.util.find_spec("websocket") is not None, "replayable": True, "verified": False},
    ], "specification": "case.extensions.transport", "secrets": "${ENV:NAME}",
        "upload_assets": "project-relative files/uploads", "outputs": "machine-private .local evidence"}


def _object(value, label: str) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as error:
            raise ValueError(f"{label} 必须是 JSON 对象") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} 必须是 JSON 对象")
    return value


def _positive(value, label: str, *, integer: bool = False):
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
            or value <= 0 or integer and not float(value).is_integer()):
        raise ValueError(f"{label} 必须是正{'整数' if integer else '有限数值'}")
    return int(value) if integer else float(value)


class _Scrubber:
    def __init__(self, project_id: str):
        self.project_id = project_id
        self.known = {value: name for name in secrets.names() if (value := secrets.get_secret(name))}

    def reference(self, value: str, hint: str) -> str:
        if value in self.known:
            return "${ENV:" + self.known[value] + "}"
        base = re.sub(r"[^A-Za-z0-9_]", "_", f"{self.project_id}_{hint}").upper().strip("_")
        if not base or base[0].isdigit():
            base = "API_" + base
        name, counter = base, 2
        while secrets.get_secret(name) not in (None, value):
            name, counter = f"{base}_{counter}", counter + 1
        secrets.set_secret(name, value)
        self.known[value] = name
        return "${ENV:" + name + "}"

    def scrub(self, value, key: str = ""):
        if isinstance(value, dict):
            return {name: self.scrub(item, name) for name, item in value.items()}
        if isinstance(value, list):
            return [self.scrub(item, key) for item in value]
        if not isinstance(value, str) or not value:
            return value
        if value.startswith("secret:") and secrets.NAME.fullmatch(value[7:]):
            return "${ENV:" + value[7:] + "}"
        if secrets.references(value):
            return value
        if SENSITIVE.search(key):
            return self.reference(value, key)
        if value in self.known:
            return "${ENV:" + self.known[value] + "}"
        for secret, name in sorted(self.known.items(), key=lambda item: len(item[0]), reverse=True):
            if len(secret) >= 8 and secret in value:
                value = value.replace(secret, "${ENV:" + name + "}")
        return value


def _stage_audio(project_id: str, transport: dict, input_root: Path | str | None = None) -> None:
    steps = transport.get("steps", [])
    if not isinstance(steps, list):
        raise ValueError("transport.steps 必须是有序步骤数组")
    for step in steps:
        if not isinstance(step, dict):
            raise ValueError("transport.steps 中每一步必须是对象")
        audio = step.get("send_audio")
        if audio is None:
            continue
        if not isinstance(audio, dict) or not isinstance(audio.get("file"), str) or not audio["file"]:
            raise ValueError("send_audio 需要 file 音频文件")
        source = secrets.resolve(audio["file"])
        if not Path(source).is_absolute() and source.startswith("files/"):
            try:
                artifacts.resolve_upload_assets(projects.path(project_id), [source])
            except ValueError:
                pass
            else:
                continue
        if input_root is not None and not Path(source).is_absolute():
            source = str(Path(input_root) / source)
        audio["file"] = artifacts.stage_upload(project_id, [source])[0]
    audio_output = transport.get("audio_output")
    if audio_output is not None:
        if not isinstance(audio_output, dict):
            raise ValueError("audio_output 必须是对象")
        if "filename" in audio_output:
            audio_output["filename"] = artifacts.relative_name(audio_output["filename"])


def build_case(project_id: str, spec: dict, **overrides) -> dict:
    """Build one schema-compatible case, retaining refs and staging voice input."""
    spec = copy.deepcopy(_object(spec, "请求规范"))
    request = _object(spec.get("request", {}), "request")
    for name in ("method", "path", "headers", "params", "json", "form"):
        if name in spec:
            request[name] = spec[name]
        if overrides.get(name) is not None:
            request[name] = overrides[name]
    method = str(request.get("method", "GET")).upper()
    if method not in METHODS:
        raise ValueError("HTTP method 不受支持")
    path = request.get("path")
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//") or "\\" in path:
        raise ValueError("接口路径必须是项目相对路径，例如 /api/tickets")
    parsed = urlsplit(path)
    if parsed.fragment:
        raise ValueError("接口路径不能包含 URL 片段")
    params = {key: values[0] if len(values) == 1 else values for key, values in parse_qs(parsed.query, keep_blank_values=True).items()}
    params.update(_object(request.get("params", {}), "params"))
    headers = _object(request.get("headers", {}), "headers")
    if any(not isinstance(value, str) for value in headers.values()):
        raise ValueError("请求头值必须是字符串")
    if request.get("form") is not None:
        _object(request["form"], "form")
    if request.get("json") is not None and request.get("form") is not None:
        raise ValueError("一个请求只能选择 json 或 form 请求体")

    extensions = _object(spec.get("extensions", {}), "extensions")
    transport = spec.get("transport", extensions.get("transport", {}))
    if isinstance(transport, str):
        transport = {"type": transport}
    transport = copy.deepcopy(_object(transport, "transport"))
    kind = overrides.get("transport") or transport.get("type", "http")
    if kind not in TRANSPORTS:
        raise ValueError("transport.type 必须是 http/sse/websocket")
    transport["type"] = kind
    if kind == "websocket" and method != "GET":
        raise ValueError("WebSocket 握手使用 GET；发送消息放在 transport.steps 中")
    for field in ("timeout_seconds", "idle_timeout_seconds", "first_event_timeout_seconds"):
        value = overrides.get(field, transport.get(field))
        if value is not None:
            transport[field] = _positive(value, field)
    if kind != "http":
        transport.setdefault("timeout_seconds", 600)
        transport.setdefault("idle_timeout_seconds", transport["timeout_seconds"])
    if transport.get("idle_timeout_seconds", 0) > transport.get("timeout_seconds", float("inf")):
        raise ValueError("idle_timeout_seconds 不能大于整个会话的 timeout_seconds")
    for filename in overrides.get("audio_input", []):
        transport.setdefault("steps", []).append({"send_audio": {"file": filename, "event": {"type": "input_audio_buffer.append"},
                                                                     "field": "audio", "chunk_ms": 20, "realtime": True}})
    _stage_audio(project_id, transport, overrides.get("input_root"))

    rules = copy.deepcopy(_object(spec.get("assertions", {}), "assertions"))
    rules.setdefault("status", 101 if kind == "websocket" else 200)
    if overrides.get("expect_status") is not None:
        rules["status"] = overrides["expect_status"]
    if not isinstance(rules["status"], int) or isinstance(rules["status"], bool) or not 100 <= rules["status"] <= 599:
        raise ValueError("expect-status 必须是 100 到 599 的状态码")
    if overrides.get("expect_json") is not None:
        rules["json_equals"] = {**_object(rules.get("json_equals", {}), "assertions.json_equals"),
                                **_object(overrides["expect_json"], "expect-json")}
    if overrides.get("max_ms") is not None:
        rules["max_ms"] = _positive(overrides["max_ms"], "max-ms", integer=True)
    stream_rules = copy.deepcopy(_object(transport.get("assertions", {}), "transport.assertions"))
    for field in ("text_contains", "reasoning_contains"):
        if field in stream_rules and (not isinstance(stream_rules[field], list)
                or any(not isinstance(value, str) or not value for value in stream_rules[field])):
            raise ValueError(f"transport.assertions.{field} 必须是非空字符串数组")
    for field in ("text_regex", "reasoning_regex"):
        if field in stream_rules:
            if not isinstance(stream_rules[field], str) or not stream_rules[field]:
                raise ValueError(f"transport.assertions.{field} 必须是非空正则表达式")
            try:
                re.compile(stream_rules[field])
            except re.error as error:
                raise ValueError(f"transport.assertions.{field} 正则表达式无效") from error
    if "complete" in stream_rules and not isinstance(stream_rules["complete"], bool):
        raise ValueError("transport.assertions.complete 必须是布尔值")
    for field in ("min_events", "min_messages", "min_audio_frames", "min_audio_bytes"):
        value = overrides.get(field, stream_rules.get(field))
        if value is not None:
            stream_rules[field] = _positive(value, field, integer=True)
    for field in ("max_first_event_ms", "max_first_text_ms", "max_first_audio_ms"):
        value = overrides.get(field, stream_rules.get(field))
        if value is not None:
            stream_rules[field] = _positive(value, field, integer=True)
    for option, field in (("expect_text", "text_contains"), ("expect_reasoning", "reasoning_contains")):
        values = overrides.get(option) or []
        if values:
            if kind == "http" and option == "expect_text":
                rules.setdefault("body_contains", []).extend(values)
            else:
                stream_rules.setdefault(field, []).extend(values)
    if kind == "http":
        if not any(key in rules for key in ("json_equals", "schema", "header_contains", "body_contains")):
            rules["header_contains"] = {"content-type": "application/json"}
    elif kind == "websocket" and rules["status"] == 101 or kind == "sse" and 200 <= rules["status"] < 300:
        if "json_equals" in rules:
            _object(rules["json_equals"], "assertions.json_equals")
        rules.setdefault("json_equals", {}).setdefault("/complete", True)
        stream_rules.setdefault("complete", True)
    if stream_rules:
        transport["assertions"] = stream_rules
    scrubber = _Scrubber(project_id)
    title = overrides.get("save_case") or spec.get("title") or f"{method} {parsed.path}"
    case = builder.api_case(projects.next_case_id(project_id, "api"), scrubber.scrub(title), method, scrubber.scrub(parsed.path), scrubber.scrub(rules),
                            module=scrubber.scrub(overrides.get("module") or spec.get("module", "接口")), headers=scrubber.scrub(headers),
                            params=scrubber.scrub(params), json=scrubber.scrub(request.get("json")),
                            form=scrubber.scrub(request.get("form")), source="autoskill api request", api_source="manual",
                            extract=spec.get("extract"), depends_on=extensions.get("depends_on"))
    if kind != "http":
        from .replay import transports
        transports.validate(transport)
        case["extensions"]["transport"] = scrubber.scrub(transport)
    else:
        if any(field in transport for field in ("steps", "audio_output", "assertions", "idle_timeout_seconds", "first_event_timeout_seconds")):
            raise ValueError("HTTP 请求不接受流式会话步骤或断言；请选择 sse/websocket")
        if "timeout_seconds" in transport:
            case["extensions"]["timeout_seconds"] = transport["timeout_seconds"]
    import jsonschema

    problems = list(jsonschema.Draft202012Validator(store.read(paths.SKILL / "references/case.schema.json")).iter_errors(case))
    if problems:
        raise ValueError("接口用例规范无效：" + problems[0].json_path + " " + problems[0].message[:180])
    store.request_url(projects.config(project_id), {"path": secrets.resolve(case["path"])})
    return case


def dispatch(args) -> dict:
    if args.api_action == "capabilities":
        return capabilities()
    project_id = args.project
    config = projects.config(project_id)
    spec = {}
    if args.file:
        spec_file = Path(args.file).expanduser().resolve()
        try:
            spec = json.loads(spec_file.read_text(encoding="utf-8-sig"))
        except ValueError as error:
            raise ValueError("--file 必须指向 JSON 请求规范") from error
    values = vars(args).copy()
    if args.file:
        values["input_root"] = spec_file.parent
    # Explicit CLI audio operands are relative to the invoking process, while
    # audio paths inside a JSON specification are relative to that file.
    cli_audio = []
    for name in args.audio_input or []:
        if name.startswith("files/"):
            try:
                cli_audio.extend(artifacts.resolve_upload_assets(projects.path(project_id), [name]))
            except ValueError:
                cli_audio.append(str(Path(name).expanduser().resolve()))
        else:
            cli_audio.append(str(Path(name).expanduser().resolve()))
    values["audio_input"] = cli_audio
    for cli_name, field in (("headers", "headers"), ("params", "params"), ("json_body", "json"), ("form", "form")):
        value = values.get(cli_name)
        if value is not None:
            try:
                values[field] = json.loads(value)
            except ValueError as error:
                raise ValueError(f"--{cli_name.replace('_', '-')} 需要有效 JSON") from error
    case = build_case(project_id, spec, **values)
    transport = case.get("extensions", {}).get("transport", {})
    timeout = transport.get("timeout_seconds", case.get("extensions", {}).get("timeout_seconds"))
    if timeout:
        config = {**config, "timeout_seconds": timeout}
    run_token = "api-" + uuid.uuid4().hex[:16]
    config = {**config, "_asset_root": projects.path(project_id), "_output_root": artifacts.private_root(run_token)}
    saved = None
    if args.save_case:
        projects.save_cases(project_id, [case])
        saved = {"id": case["id"], "title": case["title"], "replay": f"autoskill run {project_id} --case {case['id']}"}
    state = sessions.load(project_id)
    variables = dict(state.get("variables", {}))
    client = api.new_client(config, state.get("ui"))
    try:
        outcome = api.run_case(client, case, config, variables)
    finally:
        client.close()
    result = {"project": project_id, "transport": transport.get("type", "http"), **outcome}
    if saved:
        result["saved_case"] = saved
    return result
