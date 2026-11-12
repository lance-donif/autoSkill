"""Where things live. The skill directory is the product; `.local` never leaves the machine."""
from __future__ import annotations

import os
import platform
import json
import shutil
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parents[2]
APP = SKILL / "app"

def runtime_target(system: str | None = None, machine: str | None = None) -> str:
    system, machine = system or sys.platform, (machine or platform.machine()).lower()
    architecture = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64"}.get(machine, machine)
    return ("linux-" if system == "linux" else "windows-" if system == "win32" else system + "-") + architecture


VENDOR = SKILL / "vendor" / runtime_target()

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
        if candidate and Path(candidate).is_file() and (os.name == "nt" or os.access(candidate, os.X_OK)):
            return str(candidate)
    return None


def _configured_binary(name: str, variable: str) -> tuple[bool, str | None]:
    if os.environ.get(variable):
        return True, _first(os.environ[variable])
    settings = VENDOR / "runtime-settings.json"
    if sys.platform == "linux" and settings.is_file():
        config = json.loads(settings.read_text(encoding="utf-8"))
        if config.get(name):
            candidate = Path(config[name])
            return True, _first(candidate if candidate.is_absolute() else VENDOR / candidate)
    return False, None


def settings() -> dict:
    """Choices made for this machine, for example which browser to use (read without creating anything)."""
    file = workspace() / ".local" / "settings.json"
    try:
        return json.loads(file.read_text(encoding="utf-8")) if file.is_file() else {}
    except (OSError, ValueError):
        return {}


def save_settings(**values) -> dict:
    data = {**settings(), **values}
    (local() / "settings.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return data


def browser_mode() -> str:
    """"system": the user's own Chrome first and ours as the fallback (the default); "bundled": ours only."""
    mode = os.environ.get("AUTOSKILL_BROWSER") or settings().get("browser")
    return mode if mode in ("system", "bundled") else "system"


def system_chrome() -> str | None:
    """The Chrome this user already has installed."""
    if sys.platform == "linux":
        return _first(*[shutil.which(n) for n in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")])
    if sys.platform == "darwin":
        return _first("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    roots = [os.environ.get(name) for name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
    return _first(*[Path(root) / "Google/Chrome/Application/chrome.exe" for root in roots if root])


def chrome(bundled: bool = False) -> str | None:
    """The browser to drive. `bundled=True` asks for ours, which is the one that matches the bundled chromedriver."""
    configured, value = _configured_binary("chrome", "AUTOSKILL_CHROMIUM")
    if configured:
        return value
    folder = "chrome-linux-arm64" if runtime_target() == "linux-arm64" else "chrome-linux64"
    relative = folder + "/chrome" if sys.platform == "linux" else "chrome-win64/chrome.exe"
    ours = _first(VENDOR / "chrome" / relative)
    if bundled or browser_mode() == "bundled":
        return ours or (system_chrome() if sys.platform == "linux" else None)
    return system_chrome() or ours


def browser_info() -> dict:
    return {"mode": browser_mode(), "system": system_chrome(), "bundled": chrome(bundled=True), "using": chrome()}


def chromedriver() -> str | None:
    configured, value = _configured_binary("chromedriver", "AUTOSKILL_CHROMEDRIVER")
    if configured:
        return value
    folder = "chromedriver-linux-arm64" if runtime_target() == "linux-arm64" else "chromedriver-linux64"
    relative = folder + "/chromedriver" if sys.platform == "linux" else "chromedriver-win64/chromedriver.exe"
    return _first(VENDOR / "chromedriver" / relative, shutil.which("chromedriver") if sys.platform == "linux" else None)


def agent_browser() -> str | None:
    """The native binary, never the npm shim: the shim can install or upgrade."""
    bundled = _first(os.environ.get("AUTOSKILL_AGENT_BROWSER"), VENDOR / "agent-browser" / ("agent-browser" if sys.platform == "linux" else "agent-browser.exe"))
    if bundled:
        return bundled
    shim = shutil.which("agent-browser")
    if shim:
        folder = Path(shim).resolve().parent
        name = "agent-browser-win32-x64.exe" if os.name == "nt" else "agent-browser-" + runtime_target()
        return _first(folder / name, folder / "node_modules" / "agent-browser" / "bin" / name,
                      folder.parent / "agent-browser" / "bin" / name)
    return None


def headed() -> bool:
    """Show the browser window, maximised, so a person can watch. On by default; AUTOSKILL_HEADED=0 hides it."""
    if sys.platform == "linux" and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False  # a server without a screen has nowhere to show a window
    return os.environ.get("AUTOSKILL_HEADED") != "0"


def case_skill() -> Path:
    """The swappable skill that holds the test cases to execute; always a folder called autoskill-cases."""
    return Path(os.environ.get("AUTOSKILL_CASE_SKILL") or SKILL.parent / "autoskill-cases").resolve()


def child_env(extra: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_BROWSER_")}
    env.update(OFFLINE_ENV)
    env["PYTHONPATH"] = str(APP)  # child processes import this copy of autoskill, wherever it was unpacked
    env.update(extra or {})
    return env


def configure_http_environment() -> None:
    """Accept the bracketed IPv6 NO_PROXY entries supplied by DSH.

    httpx 0.28 expects bare IPv6 addresses here; [::1] produces an invalid URL
    pattern. Both spellings refer to the same host, so normalize without changing bypass scope.
    """
    current = os.environ.get("no_proxy") or os.environ.get("NO_PROXY") or ""
    entries = [part.strip() for part in current.split(",") if part.strip()]
    normalized = [part[1:-1] if part.startswith("[") and part.endswith("]") else part for part in entries]
    # Local fixtures and the local dashboard must still work with an offline/invalid system proxy.
    bypass = ",".join(dict.fromkeys([*normalized, "localhost", "127.0.0.1", "::1"]))
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = bypass
