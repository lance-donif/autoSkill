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


def child_env(extra: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_BROWSER_")}
    env.update(OFFLINE_ENV)
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
