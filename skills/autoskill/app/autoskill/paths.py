"""Where things live. The skill directory is the product; `.local` never leaves the machine."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

SKILL = Path(__file__).resolve().parents[2]
APP = SKILL / "app"
VENDOR = SKILL / "vendor" / "windows-x64"

# Applied to every child process: no installers, no update checks, no telemetry.
OFFLINE_ENV = {
    "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD": "1",
    "PIP_NO_INDEX": "1",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "DO_NOT_TRACK": "1",
    "OTEL_SDK_DISABLED": "true",
    "SE_AVOID_STATS": "true",
    "SE_OFFLINE": "true",
    "PYTHONUTF8": "1",
}
# Chrome switches that stop background traffic unrelated to the page under test.
QUIET_CHROME_ARGS = [
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-domain-reliability",
    "--disable-sync",
    "--disable-breakpad",
    "--disable-crash-reporter",
    "--no-pings",
    "--metrics-recording-only",
    "--no-default-browser-check",
    "--no-first-run",
    "--disable-features=OptimizationHints,MediaRouter,Translate",
]


def workspace() -> Path:
    """Shareable data: projects, cases, knowledge."""
    return Path(os.environ.get("AUTOSKILL_WORKSPACE") or SKILL / "workspace").resolve()


def local() -> Path:
    """Machine-private data: sessions, secrets, run history, evidence."""
    path = workspace() / ".local"
    path.mkdir(parents=True, exist_ok=True)
    return path


def service_file() -> Path:
    """Where a running dashboard announces its port (read without creating anything)."""
    return workspace() / ".local" / "service.json"


def _can_write(folder: Path) -> bool:
    probe = folder / f".write-test-{os.getpid()}"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe.write_text("x", encoding="ascii")
        probe.unlink()
    except OSError:
        return False
    return True


def writable() -> bool:
    """Can this process keep its data?"""
    return _can_write(workspace() / ".local")


def restricted() -> str:
    """Why this process cannot do the work itself; "" when it can.

    An agent harness may run commands in a sandbox that only lets them write the session folder. Such a
    process can neither keep autoSkill's data nor start a browser (the browser controller keeps its
    sockets under the user's home folder).
    """
    if not writable():
        return f"数据目录不可写：{workspace()}"
    home = Path(os.environ.get("USERPROFILE") or Path.home()) / ".agent-browser"
    if not _can_write(home):
        return f"浏览器控制目录不可写：{home}"
    return ""


def projects_dir() -> Path:
    path = workspace() / "projects"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _first(*candidates: str | Path | None) -> str | None:
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(candidate)
    return None


def chrome() -> str | None:
    return _first(os.environ.get("AUTOSKILL_CHROMIUM"), VENDOR / "chrome" / "chrome-win64" / "chrome.exe")


def chromedriver() -> str | None:
    return _first(
        os.environ.get("AUTOSKILL_CHROMEDRIVER"),
        VENDOR / "chromedriver" / "chromedriver-win64" / "chromedriver.exe",
    )


def agent_browser() -> str | None:
    """The native binary, never the npm shim: the shim can install or upgrade."""
    bundled = _first(os.environ.get("AUTOSKILL_AGENT_BROWSER"), VENDOR / "agent-browser" / "agent-browser.exe")
    if bundled:
        return bundled
    shim = shutil.which("agent-browser")
    if shim:
        folder = Path(shim).resolve().parent / "node_modules" / "agent-browser" / "bin"
        names = ["agent-browser-win32-x64.exe"] if os.name == "nt" else ["agent-browser"]
        return _first(*[folder / name for name in names])
    return None


def headed() -> bool:
    """Show the browser window (and pace the steps) so a person can watch. Off by default."""
    return os.environ.get("AUTOSKILL_HEADED") == "1"


def child_env(extra: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_BROWSER_")}
    env.update(OFFLINE_ENV)
    env["PYTHONPATH"] = str(APP)  # child processes import this copy of autoskill, wherever it was unpacked
    env.update(extra or {})
    return env


def fence_args(allowed_origins: list[str]) -> list[str]:
    """Chrome switches that make every host outside the allow-list unreachable.

    All traffic is sent to a proxy that does not exist; only the authorised
    hosts bypass it. This also stops the browser's own background requests.
    """
    from urllib.parse import urlsplit

    hosts = []
    for origin in allowed_origins:
        parts = urlsplit(origin)
        if parts.hostname:
            port = parts.port or (443 if parts.scheme == "https" else 80)
            host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
            hosts.append(f"{host}:{port}")
    return ["--proxy-server=http://127.0.0.1:9", "--proxy-bypass-list=" + ";".join(["<-loopback>", *hosts])]
