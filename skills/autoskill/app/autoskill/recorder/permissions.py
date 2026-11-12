"""Keep recorder CDP permission grants alive between short-lived CLI commands.

Chrome associates these grants with their CDP client. A detached, private worker
owns that socket until recording stop/cancel, browser/daemon exit, or permission
removal. It never changes browser ownership or operating-system policies.
"""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

from .. import paths
from .agent import AgentError

_children: dict[int, subprocess.Popen] = {}


def _alive(pid: int | None) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            stat = Path(f"/proc/{pid}/stat")
            if stat.is_file() and stat.read_text().rsplit(")", 1)[1].split()[0] == "Z":
                return False
        except OSError:
            return False
        return True
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        return bool(ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _read(file: Path) -> dict:
    try:
        value = json.loads(file.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(file: Path, value: dict) -> None:
    temporary = file.with_name("." + file.name + "-" + uuid.uuid4().hex)
    try:
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
        os.replace(temporary, file)
    finally:
        temporary.unlink(missing_ok=True)


def _folder(holder: dict) -> Path:
    token = holder.get("token", "")
    if not isinstance(token, str) or len(token) != 32 or any(c not in "0123456789abcdef" for c in token):
        raise AgentError("录制权限保持进程的 token 无效")
    root = (paths.local() / "permission-holders").resolve()
    folder = (root / token).resolve()
    if not folder.is_relative_to(root):
        raise AgentError("录制权限保持进程目录超出私有目录")
    return folder


def _daemon(session: str) -> tuple[int | None, str | None]:
    homes = [Path(os.environ["USERPROFILE"])] if os.environ.get("USERPROFILE") else []
    homes.append(Path.home())
    for home in homes:
        file = home / ".agent-browser" / (session + ".pid")
        try:
            pid = int(file.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            continue
        if _alive(pid):
            return pid, str(file)
    return None, None


def _wait(folder: Path, token: str, generation: str, pid: int, timeout: float = 12) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ack = _read(folder / "ack.json")
        if ack.get("token") == token and ack.get("pid") == pid and ack.get("generation") == generation:
            if ack.get("status") == "ready":
                return ack
            if ack.get("status") == "error":
                raise AgentError("无法保持录制浏览器权限：" + str(ack.get("error", "CDP 连接已结束"))[:240])
        if not _alive(pid):
            raise AgentError("录制权限保持进程已经退出；权限尚未准备好")
        time.sleep(0.04)
    raise AgentError("等待录制权限保持进程确认超时；权限尚未准备好")


def ensure_permissions(session: str, endpoint: str, rec: dict) -> None:
    permissions = rec.get("native_permissions", [])
    if not isinstance(permissions, list) or any(not isinstance(value, str) or not value for value in permissions):
        raise AgentError("录制权限名称必须是非空字符串")
    permissions = sorted(set(permissions))
    if not permissions:
        stop_permission_holder(rec)
        return
    if not isinstance(endpoint, str) or not endpoint.startswith(("ws://", "wss://")):
        raise AgentError("录制权限保持需要浏览器 WebSocket CDP 地址")
    holder = rec.get("permission_holder")
    if holder is not None and not isinstance(holder, dict):
        raise AgentError("录制权限保持进程信息格式无效")
    folder, process = None, None
    if isinstance(holder, dict):
        folder = _folder(holder)
        control = _read(folder / "control.json")
        if (not _alive(holder.get("pid")) or control.get("token") != holder.get("token")
                or control.get("endpoint") != endpoint or control.get("action") == "stop"):
            stop_permission_holder(rec)
            holder, folder = None, None
    if holder is None:
        token = uuid.uuid4().hex
        holder = {"token": token, "session": session, "pid": None}
        folder = _folder(holder)
        folder.mkdir(parents=True, exist_ok=False, mode=0o700)
        try:
            folder.parent.chmod(0o700)
        except OSError:
            pass
        rec["permission_holder"] = holder
    token = holder["token"]
    generation = uuid.uuid4().hex
    owner_pid, owner_file = _daemon(session)
    control = {"version": 1, "token": token, "session": session, "endpoint": endpoint, "permissions": permissions,
               "generation": generation, "action": "grant", "owner_pid": owner_pid, "owner_pid_file": owner_file}
    _write(folder / "control.json", control)
    if not _alive(holder.get("pid")):
        try:
            with (folder / "worker.log").open("ab") as log:
                process = subprocess.Popen([sys.executable, "-m", "autoskill.recorder.permissions", "--worker", str(folder)],
                                           stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=paths.child_env(),
                                           start_new_session=os.name != "nt", close_fds=True,
                                           creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                                          | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) if os.name == "nt" else 0)
            holder["pid"] = process.pid
            _children[process.pid] = process
        except OSError as error:
            rec.pop("permission_holder", None)
            shutil.rmtree(folder, ignore_errors=True)
            raise AgentError("无法启动录制权限保持进程：" + type(error).__name__) from error
    ack = _wait(folder, token, generation, holder["pid"])
    holder.update(generation=generation, owner_pid=ack.get("owner_pid"), browser_pid=ack.get("browser_pid"))


def stop_permission_holder(rec: dict) -> None:
    holder = rec.get("permission_holder")
    if not isinstance(holder, dict):
        return
    folder = _folder(holder)
    pid, token = holder.get("pid"), holder["token"]
    control = _read(folder / "control.json")
    if folder.exists() and control.get("token") == token:
        _write(folder / "control.json", {**control, "action": "stop", "generation": uuid.uuid4().hex})
    deadline = time.monotonic() + 5
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.04)
    child = _children.pop(pid, None)
    if child is not None:
        try:
            child.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
    if _alive(pid):
        raise AgentError("录制权限保持进程尚未退出；浏览器未关闭，请稍后重试 record stop/cancel")
    rec.pop("permission_holder", None)
    shutil.rmtree(folder, ignore_errors=True)


def worker(folder: Path) -> int:
    """The worker's only protocol is private control/ack plus the owned CDP socket."""
    from websocket import create_connection, WebSocketTimeoutException

    holder = {"token": folder.name}
    if _folder(holder) != folder.resolve():
        return 2
    token, pid, socket = holder["token"], os.getpid(), None
    started, generation = time.monotonic(), ""
    saw_recording, browser_pid, sequence = False, None, 0
    owner_pid = None

    def ack(status: str, error: str = "") -> None:
        _write(folder / "ack.json", {"token": token, "pid": pid, "generation": generation, "status": status,
                                    "owner_pid": owner_pid, "browser_pid": browser_pid, **({"error": error} if error else {})})

    def rpc(method: str, params: dict) -> dict:
        nonlocal sequence
        sequence += 1
        request_id = sequence
        socket.send(json.dumps({"id": request_id, "method": method, "params": params}))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                raw = socket.recv()
            except WebSocketTimeoutException:
                continue
            if not raw:
                raise ConnectionError("浏览器 CDP 连接已关闭")
            reply = json.loads(raw)
            if reply.get("id") == request_id:
                if reply.get("error"):
                    raise ValueError(str(reply["error"].get("message", "CDP 请求失败"))[:180])
                return reply.get("result", {})
        raise TimeoutError("浏览器 CDP 响应超时")

    try:
        control = _read(folder / "control.json")
        if control.get("token") != token:
            return 2
        generation, owner_pid = control.get("generation", ""), control.get("owner_pid")
        socket = create_connection(control["endpoint"], timeout=0.4, suppress_origin=True,
                                   http_no_proxy=["localhost", "127.0.0.1", "::1"])
        try:
            processes = rpc("SystemInfo.getProcessInfo", {}).get("processInfo", [])
            browser_pid = next((int(item["id"]) for item in processes if item.get("type") == "browser"), None)
        except (ValueError, KeyError, TypeError):
            pass  # remote/older CDP peers may omit process information
        owner_pid = owner_pid or browser_pid
        applied, last_contexts, last_poll = None, set(), 0.0
        while True:
            control = _read(folder / "control.json")
            if control.get("token") != token or control.get("action") == "stop":
                break
            generation = control.get("generation", "")
            if owner_pid is not None and not _alive(owner_pid) or browser_pid is not None and not _alive(browser_pid):
                break
            owner_file = control.get("owner_pid_file")
            if owner_file:
                try:
                    if int(Path(owner_file).read_text(encoding="ascii").strip()) != owner_pid:
                        break
                except (OSError, ValueError):
                    break
            recording = _read(paths.workspace() / ".local/recording.json")
            owned = recording.get("permission_holder", {}).get("token") == token if isinstance(recording.get("permission_holder", {}), dict) else False
            saw_recording = saw_recording or owned
            if not owned and (saw_recording or time.monotonic() - started > 15):
                break
            now = time.monotonic()
            if applied != generation or now - last_poll >= 0.4:
                targets = rpc("Target.getTargets", {}).get("targetInfos", [])
                contexts = {item.get("browserContextId") for item in targets if item.get("type") == "page"} or {None}
                if applied != generation or contexts != last_contexts:
                    for context in contexts:
                        params = {"permissions": control.get("permissions", [])}
                        if context:
                            params["browserContextId"] = context
                        rpc("Browser.grantPermissions", params)
                    applied, last_contexts = generation, contexts
                    ack("ready")
                last_poll = now
            time.sleep(0.05)
    except Exception as error:
        try:
            ack("error", type(error).__name__ + ": " + str(error)[:180])
        except OSError:
            pass
        return 2
    finally:
        if socket is not None:
            socket.close()
    try:
        ack("stopped")
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--worker":
        raise SystemExit(2)
    raise SystemExit(worker(Path(sys.argv[2])))
