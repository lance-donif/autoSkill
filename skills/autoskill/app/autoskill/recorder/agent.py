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
    command = [binary, "--session", session, *(launch or []), *args, "--json"]
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


def launch_args(allowed_origins: list[str], state_file: Path | None = None) -> list[str]:
    """Flags for the command that starts the browser: bundled Chrome, fenced network, optional login state."""
    flags = ["--args", ",".join([*paths.QUIET_CHROME_ARGS, *paths.fence_args(allowed_origins)])]
    if paths.chrome():
        flags = ["--executable-path", paths.chrome(), *flags]
    if state_file:
        flags = ["--state", str(state_file), *flags]
    return flags
