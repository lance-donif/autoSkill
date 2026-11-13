"""Call the bundled agent-browser binary under a fixed offline policy."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import uuid
from contextlib import suppress
from pathlib import Path

from .. import paths, runtime_identity
from ..core import store
from ..locking import named_lock


class AgentError(Exception):
    pass


def _discard(file: Path) -> None:
    # The browser daemon may still hold this handle; later calls sweep leftovers.
    with suppress(OSError):
        file.unlink(missing_ok=True)


def _scratch(session: str) -> Path:
    """Each call owns its output directory; never sweep another live command's handles."""
    key = hashlib.sha256(session.encode("utf-8")).hexdigest()[:32]
    folder = paths.local() / "agent-io" / key / uuid.uuid4().hex
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _control_directory(session: str) -> Path:
    """Keep controller files in the data root, with a short Unix socket address when needed."""
    folder = paths.browser_control()
    folder.mkdir(parents=True, exist_ok=True)
    # agent-browser 0.42 validates Unix socket paths against its 103-byte portable limit.
    if os.name == "nt" or len(os.fsencode(str(folder / (session + ".sock")))) <= 103:
        return folder
    temporary = Path(tempfile.gettempdir())
    if len(os.fsencode(str(temporary))) > 25:
        temporary = Path("/tmp")
    aliases = temporary / ("asb-" + str(os.getuid()))
    aliases.mkdir(mode=0o700, exist_ok=True)
    if aliases.is_symlink() or aliases.stat().st_uid != os.getuid():
        raise AgentError("浏览器临时连接目录不属于当前用户：" + str(aliases))
    aliases.chmod(0o700)
    identity = hashlib.sha256(os.fsencode(str(folder))).hexdigest()[:20]
    alias = aliases / identity
    try:
        alias.symlink_to(folder, target_is_directory=True)
    except FileExistsError:
        if not alias.is_symlink() or alias.resolve() != folder.resolve():
            raise AgentError("浏览器临时连接地址与当前数据目录不一致：" + str(alias)) from None
    return alias


def _read_control(file: Path) -> dict:
    try:
        value = json.loads(file.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _controller_pid(session: str) -> int | None:
    try:
        return int((paths.browser_control() / (session + ".pid")).read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


def _prepare_controller(session: str) -> str | None:
    """Detach copied control handles without signalling their PIDs or replaying their commands."""
    folder = paths.browser_control()
    with named_lock("browser-controller", session):
        owner_file, launch_file = folder / (session + ".owner.json"), folder / (session + ".launch.json")
        pid = _controller_pid(session)
        if runtime_identity.owns(pid, _read_control(owner_file)):
            return None
        launch = _read_control(launch_file)
        starter = runtime_identity.decode(launch.get("owner"))
        if runtime_identity.owns(starter.get("pid"), starter):
            return None  # Another current command is starting this same task's controller.
        if (folder / (session + ".pid")).exists() or owner_file.exists() or launch_file.exists():
            retained = paths.local() / "relocations" / ("browser-" + uuid.uuid4().hex) / "runtime" / "browser-control"
            retained.mkdir(parents=True)
            for file in folder.glob(session + ".*"):
                file.rename(retained / file.name)
            store.write(retained.parent / "state.json", {"state": "unknown", "session": session,
                        "reason": "原浏览器控制记录的机器、数据位置或进程出生信息不匹配；已保留，未发送命令或进程信号"})
        token = uuid.uuid4().hex
        store.write(launch_file, {"owner": runtime_identity.current(), "token": token})
        return token


def _remember_controller(session: str, token: str | None) -> None:
    """Bind the actual daemon PID, never the short-lived command process, to its birth identity."""
    folder = paths.browser_control()
    with named_lock("browser-controller", session):
        owner_file, launch_file = folder / (session + ".owner.json"), folder / (session + ".launch.json")
        pid = _controller_pid(session)
        if pid is not None:
            store.write(owner_file, runtime_identity.current(pid))
        else:
            owner_file.unlink(missing_ok=True)
        if token is not None and _read_control(launch_file).get("token") == token:
            launch_file.unlink(missing_ok=True)


def _strip(value):
    """Drop the launch bookkeeping agent-browser adds to every reply."""
    if isinstance(value, dict):
        return {k: _strip(v) for k, v in value.items() if k != "lifecycle"}
    if isinstance(value, list):
        return [_strip(v) for v in value]
    return value


def call(session: str, args: list[str], *, launch: list[str] | None = None, stdin_text: str | None = None,
         timeout: float = 60, check: bool = True):
    """Run one agent-browser command and return its `data`.

    Output goes to files, not pipes: the first command starts a background
    daemon that inherits the handles, and a pipe would never reach end-of-file.
    """
    binary = paths.agent_browser()
    if not binary:
        raise AgentError("没有找到随包的 agent-browser；请使用完整离线包（不会联网下载）")
    command = [binary, "--session", session, *(launch or []), *args, "--json"]
    environment = paths.child_env({"AGENT_BROWSER_SOCKET_DIR": str(_control_directory(session))})
    launch_token = _prepare_controller(session)
    folder = _scratch(session)
    token = uuid.uuid4().hex
    out_path, err_path, in_path = folder / f"{token}.out", folder / f"{token}.err", folder / f"{token}.in"
    try:
        if stdin_text is not None:
            in_path.write_text(stdin_text, encoding="utf-8")
        with out_path.open("wb") as out, err_path.open("wb") as err:
            stdin = in_path.open("rb") if stdin_text is not None else subprocess.DEVNULL
            try:
                done = subprocess.run(command, stdin=stdin, stdout=out, stderr=err, env=environment, check=False,
                                      timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except subprocess.TimeoutExpired:
                raise AgentError("浏览器命令超时") from None
            finally:
                if stdin_text is not None:
                    stdin.close()
        raw = out_path.read_text(encoding="utf-8", errors="replace").strip()
        problem = err_path.read_text(encoding="utf-8", errors="replace").strip()
    finally:
        try:
            _remember_controller(session, launch_token)
        finally:
            for file in (out_path, err_path, in_path):
                _discard(file)
            with suppress(OSError):
                folder.rmdir()
    try:
        reply = json.loads(raw) if raw else None
    except ValueError:
        reply = None
    if isinstance(reply, list):  # batch
        return _strip(reply)
    if not isinstance(reply, dict):
        if check:
            raise AgentError((problem or raw or f"浏览器命令失败（退出码 {done.returncode}）")[:400])
        return None
    if not reply.get("success"):
        if check:
            raise AgentError(str(reply.get("error") or problem or "浏览器命令失败")[:400])
        return None
    return _strip(reply.get("data"))


LAUNCH_SIGNS = ("crashpad", "openprocess", "failed to launch", "chrome exited", "browser process", "os error 5",
                "access is denied", "拒绝访问", "socket", "daemon")


def launch_problem(error: Exception) -> str:
    """Explain a failed browser start in terms the caller can act on."""
    text = str(error)
    reason = paths.restricted()
    if reason or any(sign in text.lower() for sign in LAUNCH_SIGNS):
        hint = (f"当前进程受限（{reason}），浏览器在这里起不来；让 autoSkill 后台服务保持运行后重试，或给当前会话完全访问权限" if reason else
                "浏览器没有启动成功。最常见的原因是命令运行在受限沙箱里（启动进程、证书或用户目录被拦）："
                "让 autoSkill 后台服务保持运行（DeepSeek Harness 里启用插件，或用户在自己的终端运行 autoskill.cmd dashboard），"
                "把环境变量 AUTOSKILL_BRIDGE 设为 on 后重试，命令就会交给后台服务执行；或者给当前会话完全访问权限。"
                "如果不在沙箱里，运行 autoskill doctor --launch 检查随包文件")
        return f"{hint}。原始信息：{text[:240]}"
    return text


def launch_args(allowed_origins: list[str], state_file: Path | None = None, insecure_tls: bool = False,  # noqa: ARG001  # Keep the legacy keyword without restricting origins.
                browser: str | None = None) -> list[str]:
    """Start the browser without a proxy fence, with optional login state."""
    # Let agent-browser create about:blank through CDP. Chrome 151 can crash in its
    # automatic new-tab startup path before the recording observer attaches.
    flags = ["--args", ",".join(["--no-startup-window", *paths.QUIET_CHROME_ARGS,
                               *(["--start-maximized"] if paths.headed() else [])])]
    browser = browser or paths.chrome()
    if browser:
        flags = ["--executable-path", browser, *flags]
    if state_file:
        flags = ["--state", str(state_file), *flags]
    if insecure_tls:
        flags = ["--ignore-https-errors", *flags]
    if paths.headed():
        flags = ["--headed", *flags]
    return flags
