"""Call the bundled agent-browser binary under a fixed offline policy."""
from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

from .. import paths


class AgentError(Exception):
    pass


def _discard(file: Path) -> None:
    try:
        file.unlink(missing_ok=True)
    except OSError:
        pass  # the browser daemon started by this command still holds the handle; swept later


def _scratch() -> Path:
    """Machine-private folder for command output; leftovers from earlier calls are swept here."""
    folder = paths.local() / "agent-io"
    folder.mkdir(parents=True, exist_ok=True)
    for stale in folder.iterdir():
        _discard(stale)
    return folder


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
    # A command may contain the literal-input `--` marker. All global flags
    # must precede it, otherwise --json becomes input text and output is plain.
    command = [binary, "--session", session, "--json", *(launch or []), *args]
    folder = _scratch()
    token = uuid.uuid4().hex
    out_path, err_path, in_path = folder / f"{token}.out", folder / f"{token}.err", folder / f"{token}.in"
    try:
        if stdin_text is not None:
            in_path.write_text(stdin_text, encoding="utf-8")
        with out_path.open("wb") as out, err_path.open("wb") as err:
            stdin = in_path.open("rb") if stdin_text is not None else subprocess.DEVNULL
            try:
                done = subprocess.run(command, stdin=stdin, stdout=out, stderr=err, env=paths.child_env(),
                                      timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except subprocess.TimeoutExpired:
                raise AgentError("浏览器命令超时") from None
            finally:
                if stdin_text is not None:
                    stdin.close()
        raw = out_path.read_text(encoding="utf-8", errors="replace").strip()
        problem = err_path.read_text(encoding="utf-8", errors="replace").strip()
    finally:
        for file in (out_path, err_path, in_path):
            _discard(file)
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


def launch_args(allowed_origins: list[str], state_file: Path | None = None, insecure_tls: bool = False,
                *, config: dict | None = None, asset_root: Path | None = None) -> list[str]:
    """Start the browser without a proxy fence, with optional login state."""
    from .. import media

    browser_args = [*paths.QUIET_CHROME_ARGS, *(["--start-maximized"] if paths.headed() else []),
                    *media.chrome_args(config or {}, asset_root)]
    flags = ["--args", "\n".join(browser_args)]
    if paths.chrome():
        flags = ["--executable-path", paths.chrome(), *flags]
    if state_file:
        flags = ["--state", str(state_file), *flags]
    if insecure_tls:
        flags = ["--ignore-https-errors", *flags]
    if paths.headed():
        flags = ["--headed", *flags]
    return flags
