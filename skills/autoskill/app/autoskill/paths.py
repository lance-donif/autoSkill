"""Keep program files separate from the user's complete, portable data directory."""
from __future__ import annotations

import ipaddress
import json
import os
import platform
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


def default_workspace() -> Path:
    """Resolve the stable per-user data directory without creating it."""
    home = Path(os.environ.get("USERPROFILE") or Path.home()) if sys.platform == "win32" else Path.home()
    return (home / "autoSkill-data").resolve()


def workspace() -> Path:
    """Resolve the selected data directory; initialization belongs to explicit startup."""
    selected = os.environ.get("AUTOSKILL_WORKSPACE")
    return Path(selected).expanduser().resolve() if selected else default_workspace()


def local() -> Path:
    """Saved state, credentials and execution evidence within the selected data directory."""
    from . import maintenance

    maintenance.check(workspace())
    path = workspace() / ".local"
    path.mkdir(parents=True, exist_ok=True)
    return path


def service_file() -> Path:
    """Where a running dashboard announces its port (read without creating anything)."""
    return workspace() / ".local" / "service.json"


def browser_control() -> Path:
    """Resolve this data directory's transient browser-controller state without writing."""
    return workspace() / ".local" / "browser-control"


def runtime_settings_file() -> Path:
    """Resolve platform-specific runtime preferences independently of the installed program."""
    return workspace() / ".local" / "runtime-settings" / (runtime_target() + ".json")


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

    An agent harness may only allow writes within the current session folder. The data directory and
    its browser-controller directory must both be writable before direct browser execution.
    """
    if not writable():
        return f"数据目录不可写：{workspace()}"
    control = browser_control()
    if not _can_write(control):
        return f"浏览器控制目录不可写：{control}"
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


def _bundled_chrome_path() -> Path:
    folder = "chrome-linux-arm64" if runtime_target() == "linux-arm64" else "chrome-linux64"
    relative = folder + "/chrome" if sys.platform == "linux" else "chrome-win64/chrome.exe"
    return VENDOR / "chrome" / relative


def _configured_binary(name: str, variable: str, *, explicit_only: bool = False) -> tuple[bool, str | None]:
    if os.environ.get(variable):
        return True, _first(os.environ[variable])
    settings = runtime_settings_file()
    if not settings.is_file():
        settings = VENDOR / "runtime-settings.json"
    if settings.is_file():
        config = json.loads(settings.read_text(encoding="utf-8"))
        if config.get(name):
            candidate = Path(config[name])
            candidate = candidate if candidate.is_absolute() else VENDOR / candidate
            selection = config.get("browser_selection")
            # Older setup files did not distinguish detection from a user override.
            detected = selection == "detected" or (selection is None and candidate.resolve() == _bundled_chrome_path().resolve())
            if explicit_only and detected:
                return False, None
            return True, _first(candidate)
    return False, None


def _configured_folder(setting: str, variable: str) -> Path | None:
    """A sync, error or delivery folder chosen by environment variable or the saved settings."""
    value = os.environ.get(variable) or settings().get(setting)
    return Path(value).expanduser().resolve() if isinstance(value, str) and value.strip() else None


def sync_dir() -> Path | None:
    """Where development copies native case files (JSON/ZIP) for automatic import."""
    return _configured_folder("sync_dir", "AUTOSKILL_SYNC_DIR")


def error_dir() -> Path | None:
    """Where failed replay runs are archived as JSON and Markdown for the deployment side."""
    return _configured_folder("error_dir", "AUTOSKILL_ERROR_DIR")


def delivery_dir() -> Path | None:
    """The user's project folder for the final Excel and report files only.

    `AUTOSKILL_DELIVERY_DIR` (or the saved `delivery_dir`) selects it explicitly. Otherwise the
    directory the host forwarded with the command is the user's project; direct CLI users opt in.
    """
    selected = _configured_folder("delivery_dir", "AUTOSKILL_DELIVERY_DIR")
    if selected is not None:
        return selected
    if os.environ.get("AUTOSKILL_HOST", "generic") == "generic":
        return None  # generic callers opt in explicitly; a host conversation forwards the user's project folder
    caller = os.environ.get("AUTOSKILL_CALLER_CWD")
    if not caller:
        return None
    folder = Path(caller).expanduser().resolve()
    # Never scatter deliveries into the program or the data folder itself.
    return None if folder in (SKILL.resolve(), workspace()) else folder


def settings() -> dict:
    """Choices made for this machine, for example which browser to use (read without creating anything)."""
    file = workspace() / ".local" / "settings.json"
    try:
        return json.loads(file.read_text(encoding="utf-8")) if file.is_file() else {}
    except (OSError, ValueError):
        return {}


def save_settings(**values) -> dict:
    from . import maintenance
    from .core import store
    from .locking import named_lock

    def merge(previous, changes):
        combined = dict(previous)
        for key, value in changes.items():
            combined[key] = merge(combined[key], value) if isinstance(combined.get(key), dict) and isinstance(value, dict) else value
        return combined

    with maintenance.access(workspace()), named_lock("settings", "workspace"):
        data = merge(settings(), values)
        store.write(local() / "settings.json", data)
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
    prefer_system = not bundled and browser_mode() == "system"
    configured, value = _configured_binary("chrome", "AUTOSKILL_CHROMIUM", explicit_only=prefer_system)
    if configured:
        return value
    if prefer_system:
        return system_chrome() or chrome(bundled=True)
    return _first(_bundled_chrome_path()) or (system_chrome() if sys.platform == "linux" else None)


def browser_info() -> dict:
    return {"mode": browser_mode(), "system": system_chrome(), "bundled": chrome(bundled=True), "using": chrome()}


def chromedriver() -> str | None:
    configured, value = _configured_binary("chromedriver", "AUTOSKILL_CHROMEDRIVER")
    if configured:
        return value
    folder = "chromedriver-linux-arm64" if runtime_target() == "linux-arm64" else "chromedriver-linux64"
    relative = folder + "/chromedriver" if sys.platform == "linux" else "chromedriver-win64/chromedriver.exe"
    return _first(VENDOR / "chromedriver" / relative, shutil.which("chromedriver") if sys.platform == "linux" else None)


def _native_agent_browser(file: Path) -> str | None:
    if not _first(file):
        return None
    try:
        with file.open("rb") as stream:
            header = stream.read(4)
    except OSError:
        return None
    if header[:2] == b"MZ" or header in (b"\x7fELF", b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe"):
        return str(file)
    return None


def _npm_agent_browser(shim: str) -> str | None:
    resolved = Path(shim).resolve()
    native = _native_agent_browser(resolved)
    if native:
        return native
    folder, target = resolved.parent, runtime_target()
    legacy_name = "agent-browser-win32-x64.exe" if target == "windows-x64" else "agent-browser-" + target
    native_target = {"linux-x64": "linux-x64-gnu", "linux-arm64": "linux-arm64-gnu", "windows-x64": "win32-x64-msvc"}.get(target, target)
    native_name = "agent-browser.exe" if target == "windows-x64" else "agent-browser"
    for bin_folder in (folder, folder / "node_modules/agent-browser/bin", folder.parent / "agent-browser/bin"):
        legacy = _first(bin_folder / legacy_name)
        if legacy:
            return legacy
        for modules in (bin_folder.parent / "node_modules", bin_folder.parent.parent):
            native = _native_agent_browser(modules / "@agent-browser" / ("cli-" + native_target) / "bin" / native_name)
            if native:
                return native
    return None


def agent_browser() -> str | None:
    """The native binary, never the npm shim: the shim can install or upgrade."""
    bundled = _first(os.environ.get("AUTOSKILL_AGENT_BROWSER"), VENDOR / "agent-browser" / ("agent-browser" if sys.platform == "linux" else "agent-browser.exe"))
    if bundled:
        return bundled
    shim = shutil.which("agent-browser")
    return _npm_agent_browser(shim) if shim else None


def headed() -> bool:
    """Resolve the process override or saved choice, then default to a visible window."""
    explicit = os.environ.get("AUTOSKILL_HEADED")
    if explicit in ("0", "1"):
        return explicit == "1"
    dashboard = settings().get("dashboard", {})
    if isinstance(dashboard, dict) and type(dashboard.get("headed")) is bool:
        return dashboard["headed"]
    return True


def browser_display_problem(*, headed: bool) -> str | None:
    if headed and sys.platform == "linux" and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return "显示浏览器窗口需要图形桌面。请在有桌面会话的环境中启动 autoSkill 或 DSH。"
    return None


def case_skill() -> Path:
    """Resolve default and named-session case sheets inside the selected data directory."""
    from . import task_context

    if os.environ.get("AUTOSKILL_CASE_SKILL"):
        from . import data_refs

        original = os.environ["AUTOSKILL_CASE_SKILL"]
        selected = Path(original).expanduser().resolve()
        candidates = [original, selected] if data_refs.is_absolute(original) else [selected]
        for candidate in candidates:
            try:
                return data_refs.resolve(candidate)
            except data_refs.UnmappedPathError:
                continue
        return selected
    key = task_context.session_key() if task_context.session_id() is not None else "default"
    return workspace() / ".local" / "case-plans" / key / "autoskill-cases"


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
    current = os.environ.get("no_proxy", os.environ.get("NO_PROXY", ""))
    entries = [part.strip() for part in current.split(",") if part.strip()]
    normalized = [_http_bypass_entry(part) for part in entries]
    # Local fixtures and the local dashboard must still work with an offline/invalid system proxy.
    bypass = ",".join(dict.fromkeys([*normalized, "localhost", "127.0.0.1", "::1"]))
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = bypass


def _http_bypass_entry(entry: str) -> str:
    if not entry.startswith("[") or "]" not in entry:
        return entry
    address, _, suffix = entry[1:].partition("]")
    try:
        ipaddress.IPv6Address(address)
    except ValueError:
        return entry
    if not suffix:
        return address
    if suffix.startswith(":") and suffix[1:].isdigit():
        return "all://[" + address + "]" + suffix
    return entry
