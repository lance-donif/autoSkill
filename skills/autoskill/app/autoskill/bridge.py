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
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

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
    port = None
    for file in (paths.service_file(), paths.SKILL / "workspace" / ".local" / "service.json"):
        try:
            port = int(json.loads(file.read_text(encoding="utf-8"))["port"])
            break
        except (OSError, ValueError, KeyError, TypeError):
            continue
    if port is None:
        return None, "没有发现正在运行的 autoSkill 后台服务。"
    try:
        status = _request(port, "/api/status", None, 4)
    except (OSError, ValueError):
        return None, "autoSkill 后台服务没有响应（可能已经关闭）。"
    same = lambda a, b: os.path.normcase(str(a)) == os.path.normcase(str(b))  # noqa: E731
    if status.get("name") != "autoSkill" or not same(status.get("skill_path", ""), paths.SKILL):
        return None, "端口上运行的不是这份 autoSkill 的后台服务。"
    if "bridge" not in status:
        return None, "正在运行的 autoSkill 后台服务是旧版本，不能代为执行命令；请重启它（在 DeepSeek Harness 里停用再启用 autoSkill 插件）。"
    if not same(status.get("workspace", ""), paths.workspace()):
        return None, (f"正在运行的 autoSkill 后台服务用的是另一个数据目录（{status.get('workspace')}）；"
                      "去掉自己设置的 AUTOSKILL_WORKSPACE 后重试。")
    if not status.get("bridge"):
        return None, "后台服务关闭了代为执行功能（AUTOSKILL_BRIDGE_SERVER=off）。"
    return port, ""


def forward(port: int, argv: list[str], stdin_text: str | None = None) -> dict:
    """Ask the service to run the command. Returns {exit, stdout, stderr}."""
    body = {"argv": argv, "stdin": stdin_text, "msys_root": os.environ.get("AUTOSKILL_MSYS_ROOT"),
            "headed": os.environ.get("AUTOSKILL_HEADED") == "1"}
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


def check(argv: list[str]) -> None:
    """Server side: refuse what must not be done on someone else's behalf."""
    from . import cli

    try:
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            args = cli.parser().parse_args(argv)
    except SystemExit:
        return  # not a valid command line (or --help): the child prints the usage text
    if args.command == "dashboard":
        raise ValueError("后台服务已经在运行，不需要再启动 dashboard")
    if args.command == "pack" and getattr(args, "full", False):
        raise ValueError("pack --full 需要在沙箱外直接运行（它要把文件写到 autoSkill 目录之外）")
    if args.command == "pack" and getattr(args, "include_secrets", False):
        raise ValueError("带凭据的分享包需要在沙箱外直接运行 autoskill pack --include-secrets")
    outputs = [args.out] if getattr(args, "out", None) else []
    if args.command == "browser" and args.args[:1] == ["screenshot"]:
        outputs += [a for a in args.args[1:] if not a.startswith("-")]
    root = paths.workspace().resolve()
    for out in outputs:
        if not (root / out).resolve().is_relative_to(root):
            raise ValueError(f"通过后台服务执行时，输出位置必须在 autoSkill 数据目录内（{root}）；省略输出位置即可使用默认位置")


def execute(argv: list[str], stdin_text: str | None = None, msys_root: str | None = None, headed: bool = False) -> dict:
    """Server side: run one autoskill command in its own process and return what it printed."""
    check(argv)
    extra = {"AUTOSKILL_BRIDGE": "off", "AUTOSKILL_WORKSPACE": str(paths.workspace())}
    if headed:
        extra["AUTOSKILL_HEADED"] = "1"
    if msys_root:
        extra["AUTOSKILL_MSYS_ROOT"] = msys_root
    folder = paths.local() / "agent-io"
    folder.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    out_path, err_path, in_path = folder / f"b{token}.out", folder / f"b{token}.err", folder / f"b{token}.in"
    in_path.write_text(stdin_text or "", encoding="utf-8")
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
    return {"exit": code, "stdout": stdout, "stderr": stderr}


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
