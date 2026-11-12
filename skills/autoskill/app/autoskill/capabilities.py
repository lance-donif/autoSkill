"""Static, machine-readable browser support; discovery never starts a browser.

The catalog describes the autoSkill adapter, not a successful environment probe.
Keep native execution distinct from the smaller portable recording contract.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy

AGENT_BROWSER_VERSION = "0.42.0"
SCHEMA_VERSION = 1


def _entry(command: str, group: str, syntax: str, mode: str = "portable", *,
           aliases: tuple[str, ...] = (), dependencies: tuple[str, ...] = (),
           prerequisites: tuple[str, ...] = (), limitations: str = "", browser: bool = True,
           executable: bool | None = None, surface: str = "browser") -> dict:
    portable = mode in ("portable", "configuration")
    native = mode == "native_only"
    return {
        "id": command.replace(" ", "."), "command": command, "group": group,
        "syntax": "autoskill " + (surface + " " if surface else "") + syntax, "aliases": list(aliases),
        "executable": mode != "unavailable" if executable is None else executable, "recordable": mode == "portable" or native,
        "recording": mode,
        "replayable": {"playwright": portable, "selenium": portable},
        "exportable": {"playwright": portable, "selenium": portable},
        "dependencies": ["agent-browser==" + AGENT_BROWSER_VERSION, *(["Chrome"] if browser else []), *dependencies],
        "prerequisites": list(prerequisites or (("active_recording",) if portable else ())),
        "verified": False,
        "limitations": limitations or (
            "Native mutation is recorded as native_step; portable replay/export is blocked."
            if native else "Runtime behavior has not been verified by this catalog."
        ),
    }


# Only the syntax listed here is covered by the portable contract. Upstream
# options, launch settings and native mutations do not acquire portability just
# because the agent-browser executable accepts them.
_CAPABILITIES = [
    _entry("open", "navigation", "open <url-or-project-path>", aliases=("goto", "navigate")),
    _entry("reload", "navigation", "reload"),
    _entry("back", "navigation", "back"),
    _entry("forward", "navigation", "forward"),
    _entry("pushstate", "navigation", "pushstate <url>", "native_only"),
    _entry("read", "navigation", "read [url] [options]", "observation",
           limitations="Native page/network read, not a recorded assertion or portable step."),
    _entry("close", "session", "close [options]", "administration", aliases=("quit", "exit"), browser=False,
           prerequisites=("no_active_recording",),
           limitations="During recording use record stop or record cancel so the recording is finalized explicitly."),
    _entry("connect", "session", "connect <cdp-port-or-url>", "administration",
           prerequisites=("no_active_recording", "reachable_CDP_browser")),
    _entry("session", "session", "session <id|list|info> [options]", "observation", browser=False),
    _entry("profiles", "session", "profiles [options]", "observation", browser=False),
    _entry("profile", "session", "profile [operation] [args]", "native_only"),
    _entry("snapshot", "snapshots", "snapshot [--all]", "observation",
           prerequisites=("active_recording",),
           limitations="Refreshes recording refs; --all includes noninteractive content. Other native snapshot options are not the portable adapter syntax."),
    _entry("describe", "locators", "describe <target>", "observation",
           prerequisites=("active_recording",)),
    _entry("click", "interactions", "click <target>"),
    _entry("dblclick", "interactions", "dblclick <target>"),
    _entry("hover", "interactions", "hover <target>"),
    _entry("focus", "interactions", "focus <target>"),
    _entry("fill", "interactions", "fill <target> <text-or-secret:NAME>"),
    _entry("type", "interactions", "type <target> <text-or-secret:NAME>"),
    _entry("select", "interactions", "select <target> <value>"),
    _entry("check", "interactions", "check <target>"),
    _entry("uncheck", "interactions", "uncheck <target>"),
    _entry("press", "keyboard", "press <key>", aliases=("key",)),
    _entry("keydown", "keyboard", "keydown <key>"),
    _entry("keyup", "keyboard", "keyup <key>"),
    _entry("keyboard", "keyboard", "keyboard <type|inserttext|press|down|up> <value>"),
    _entry("scroll", "interactions", "scroll <up|down|left|right> [pixels] [--selector <target>]"),
    _entry("scrollintoview", "interactions", "scrollintoview <target>", aliases=("scrollinto",)),
    _entry("drag", "interactions", "drag <source-target> <destination-target>"),
    _entry("upload", "interactions", "upload <target> <file> [file ...]",
           prerequisites=("active_recording", "readable_source_files"),
           limitations="Files are copied into project assets; cases store project-relative paths. Share/export the staged assets with the case."),
    _entry("download", "interactions", "download <target> <relative-filename>",
           prerequisites=("active_recording", "element_triggers_download"),
           limitations="Output belongs to private run artifacts, not a machine-absolute path in the case."),
    _entry("captcha", "interactions", "captcha <image-target> <input-target>",
           dependencies=("bundled_alphanumeric_CAPTCHA_model",),
           limitations="Alphanumeric image CAPTCHA only; standalone exports require human entry. Slider, selection, arithmetic, SMS and QR challenges are unsupported."),
    _entry("get", "information", "get <text|html|value|attr|url|title|count|box|styles|cdp-url> [target] [attribute]", "observation"),
    _entry("is", "state_checks", "is <visible|enabled|checked> <target>", "observation"),
    _entry("find", "locators", "find <locator-kind> <value> <action> [options]", "native_only",
           limitations="Native semantic find may perform an action; it is not converted into portable locator candidates."),
    _entry("wait", "waits", "wait <milliseconds|target> [--state <visible|hidden|attached|detached>] [--timeout <ms>] [--stable-for <ms>]"),
    _entry("wait condition", "waits", "wait <--text|--url|--load|--fn> <value>", "native_only",
           aliases=("wait --text", "wait -t", "wait --url", "wait -u", "wait --load", "wait -l", "wait --fn", "wait -f")),
    _entry("mouse", "mouse", "mouse move <x> <y> | mouse <down|up> <button> | mouse wheel <dy> [dx]",
           limitations="Viewport coordinates depend on page geometry; use the same viewport and layout. Human-path and timing options are native-only."),
    _entry("tab list", "tabs", "tab [list]", "observation",
           prerequisites=("active_recording",)),
    _entry("tab new", "tabs", "tab new [url-or-project-path]"),
    _entry("tab switch", "tabs", "tab <tN>"),
    _entry("tab close", "tabs", "tab close [tN]",
           limitations="Recording-local stable tab ids; closing the final recording tab requires record stop/cancel."),
    _entry("window", "tabs", "window new", "native_only"),
    _entry("frame", "frames", "frame <CSS-selector|@ref|main>",
           limitations="Nested selection is relative to the selected frame. Use frame main to reset; tab/page ownership stays separate."),
    _entry("dialog accept", "dialogs", "dialog accept [text]",
           dependencies=("Python_Playwright_CDP_listener",),
           prerequisites=("active_recording", "prepare_before_triggering_dialog"),
           limitations="Arms the next-dialog policy before the triggering action. Recovering an already-open dialog executes as native_step, not a portable preparation."),
    _entry("dialog dismiss", "dialogs", "dialog dismiss",
           dependencies=("Python_Playwright_CDP_listener",),
           prerequisites=("active_recording", "prepare_before_triggering_dialog")),
    _entry("dialog status", "dialogs", "dialog status", "observation"),
    _entry("eval", "javascript", "eval <script> | eval -b <base64> | eval --script-file <path>",
           limitations="JavaScript expression/function, not arbitrary statement source. Opaque scripts are kept in the local value store; cases use ENV references. Supply those values when sharing/exporting."),
    _entry("clipboard read", "clipboard", "clipboard read", "observation"),
    _entry("clipboard write", "clipboard", "clipboard write <text>",
           limitations="Browser clipboard context and permissions are required; desktop/system clipboard parity is not promised."),
    _entry("screenshot", "captures", "screenshot [relative-path] [options]", "observation"),
    _entry("pdf", "captures", "pdf [relative-path]", "observation"),
    _entry("record", "video", "record <start|restart> [relative-path] [options] | record stop", "observation",
           dependencies=("ffmpeg_on_PATH_for_start_or_restart",),
           limitations="Native video artifact, separate from autoskill record start/stop business recording; not exported as a case step."),
    _entry("trace", "debugging", "trace start | trace stop [relative-path]", "observation"),
    _entry("profiler", "debugging", "profiler start | profiler stop [relative-path]", "observation"),
    _entry("console", "debugging", "console [--clear]", "observation"),
    _entry("errors", "debugging", "errors [--clear]", "observation"),
    _entry("highlight", "debugging", "highlight <target>", "observation"),
    _entry("inspect", "debugging", "inspect", "observation",
           prerequisites=("visible_browser_desktop",)),
    _entry("set viewport", "settings", "set viewport <width> <height> [scale]"),
    _entry("set device", "settings", "set device <device-name>", "native_only"),
    _entry("set geo", "settings", "set geo <latitude> <longitude>", aliases=("set geolocation",)),
    _entry("set offline", "settings", "set offline <on|off>"),
    _entry("set headers", "settings", "set headers <json-string-values-object>"),
    _entry("set credentials", "settings", "set credentials <username> <password>", "native_only", aliases=("set auth",)),
    _entry("set media", "settings", "set media <dark|light|no-preference> [reduced-motion|no-preference]"),
    _entry("cookies get", "cookies", "cookies [get]", "observation"),
    _entry("cookies set", "cookies", "cookies set <name> <value> [--http-only] [--secure] [--same-site Strict|Lax|None] [--url URL|--domain DOMAIN] [--path PATH] [--expires SECONDS]",
           limitations="Only the listed cookie attributes are portable; --curl/import and unknown flags are native-only."),
    _entry("cookies import", "cookies", "cookies set --curl <file> [options]", "native_only", aliases=("cookies set --curl",)),
    _entry("cookies clear", "cookies", "cookies clear"),
    _entry("storage read", "storage", "storage <local|session> [key]", "observation"),
    _entry("storage set", "storage", "storage <local|session> set <key> <value>",
           aliases=("storage local set", "storage session set")),
    _entry("storage clear", "storage", "storage <local|session> clear",
           aliases=("storage local clear", "storage session clear")),
    _entry("network route", "network", "network route <url-pattern> [options]", "native_only"),
    _entry("network unroute", "network", "network unroute [url-pattern]", "native_only"),
    _entry("network requests", "network", "network requests [--filter <pattern>]", "observation"),
    _entry("network request", "network", "network request <request-id>", "observation"),
    _entry("network har", "network", "network har start [options] | network har stop [relative-path]", "observation",
           prerequisites=("no_active_recording",),
           limitations="Business recording owns its HAR. Native HAR start/stop cannot take it over; stop/cancel first."),
    _entry("flow read", "flows", "flow <steps|list> [options]", "observation", aliases=("flow steps", "flow list")),
    _entry("flow", "flows", "flow <save|run> [options]", "native_only",
           prerequisites=("native_flow_capture_for_save",),
           limitations="Upstream experimental saved flows use their own capture and HTTP/page/replay behavior; not autoSkill portable cases."),
    _entry("auth read", "authentication", "auth <list|show> [options]", "observation", aliases=("auth list", "auth show")),
    _entry("auth", "authentication", "auth <save|login|delete> [options]", "native_only",
           limitations="Upstream auth profiles are separate from autoSkill secret/login/session management. Native mutations block portable case replay."),
    _entry("plugin read", "plugins", "plugin <list|show> [options]", "observation", aliases=("plugin list", "plugin show")),
    _entry("plugin", "plugins", "plugin <add|run> [options]", "native_only",
           prerequisites=("plugin_specific_runtime_and_permissions",),
           limitations="External plugin behavior is not translated into portable steps; adding a plugin can download dependencies."),
    _entry("state save", "state", "state save <relative-path>", "observation",
           limitations="Auth state is private evidence, not a shareable case or a portable step."),
    _entry("state load", "state", "state load <path>", "native_only"),
    _entry("stream status", "streaming", "stream status", "observation"),
    _entry("stream", "streaming", "stream <enable|disable> [options]", "native_only",
           limitations="Live stream input can change the page outside recorded commands; not a portable case interface."),
    _entry("webmcp list", "webmcp", "webmcp list [tool] [--frame <id>]", "observation"),
    _entry("webmcp result", "webmcp", "webmcp result <invocation-id>", "observation"),
    _entry("webmcp invoke", "webmcp", "webmcp invoke <tool> [options]", "native_only"),
    _entry("webmcp cancel", "webmcp", "webmcp cancel <invocation-id>", "native_only"),
    _entry("dashboard", "dashboard", "dashboard <start|stop> [options]", "administration", browser=False,
           limitations="Upstream dashboard runs in an isolated admin session; use autoskill dashboard for autoSkill projects/results."),
    _entry("install", "installation", "install [options]", "administration", browser=False,
           prerequisites=("explicit_runtime_maintenance_request", "network_and_system_package_permissions"),
           limitations="Upstream maintenance command can download/install software. Normal autoSkill work uses the bundled runtime and never installs missing dependencies."),
    _entry("upgrade", "installation", "upgrade [options]", "administration", aliases=("update",), browser=False,
           prerequisites=("no_active_recording", "explicit_runtime_maintenance_request", "network"),
           limitations="Runtime replacement must not disturb an active recording. autoSkill does not invoke this to repair missing dependencies."),
    _entry("doctor", "diagnostics", "doctor [--offline --quick] [options]", "administration", browser=False,
           limitations="Upstream diagnostics run isolated from the recording. --fix requires no active recording and may reinstall or purge state; autoskill doctor checks bundled dependencies."),
    _entry("mcp", "mcp", "mcp [options]", "administration", browser=False, executable=False,
           limitations="Returns executed=false and a stdio command descriptor for an external MCP client; does not start a server or execute a browser action."),
    _entry("mcp call", "mcp", "mcp call <tool> <json> [options]", "administration",
           limitations="Finite tool call runs in an isolated session during recording; not a portable action on the recorded business page."),
    _entry("react", "react", "react <tree|inspect|renders|suspense> [options]", "observation",
           prerequisites=("React_page", "react_devtools_enabled_at_browser_launch"),
           limitations="Enable the React hook before recording launch; changing launch flags during a recording is rejected instead of relaunching silently."),
    _entry("vitals", "react", "vitals [options]", "observation"),
    _entry("vitals url", "react", "vitals <url> [options]", "native_only",
           limitations="URL variant can navigate the page; native collection is not a portable assertion."),
    _entry("a11y", "accessibility", "a11y [--tags <tags>] [--selector <CSS>]", "observation",
           prerequisites=("Chrome_CDP",),
           limitations="Native axe audit. Findings need review and are not business assertions."),
    _entry("a11y url", "accessibility", "a11y <url> [options]", "native_only",
           limitations="URL navigation is native-only; it is not translated into a portable navigation/assertion."),
    _entry("removeinitscript", "init_scripts", "removeinitscript <identifier>", "native_only"),
    _entry("init script", "init_scripts", "--init-script <path> <command> [args]", "administration",
           prerequisites=("no_active_recording",),
           limitations="Launch configuration affects the native session and may relaunch it; not embedded in portable cases."),
    _entry("global options", "global_options", "[global-options] <command> [args]", "administration",
           prerequisites=("no_active_recording_for_launch_or_identity_changes",),
           limitations="Session, namespace, profile, config, CDP, provider, engine, proxy and launch changes cannot replace a recording session. autoSkill portable replay uses full Chrome and its configured engine."),
    _entry("configuration", "configuration", "--config <file> <command> [args]", "administration", browser=False,
           prerequisites=("no_active_recording",),
           limitations="Native config-file discovery is distinct from autoSkill project settings."),
    _entry("device list", "touch", "device list", "observation"),
    _entry("device", "touch", "device <operation> [args]", "native_only"),
    _entry("tap", "touch", "tap <target> [options]", "native_only"),
    _entry("swipe", "touch", "swipe <direction> [options]", "native_only"),
    _entry("diff", "comparison", "diff <snapshot|screenshot> [args]", "observation"),
    _entry("diff url", "comparison", "diff url <url> [options]", "native_only"),
    _entry("batch", "batch", "batch <command-string> [command-string ...]", "native_only",
           limitations="Native batch bypasses per-command portable recording; issue autoskill browser commands separately for portable cases."),
    _entry("confirm", "policy", "confirm [args]", "native_only"),
    _entry("deny", "policy", "deny [args]", "native_only"),
    _entry("chat", "diagnostics", "chat [options]", "native_only",
           prerequisites=("single_prompt_argument", "configured_model_provider"),
           limitations="One-shot prompt only; interactive stdin chat is rejected. Model-driven page changes are native-only."),
    _entry("skills", "diagnostics", "skills [args]", "administration", browser=False,
           limitations="Native skill administration runs isolated from active business recording."),
    _entry("microphone", "media", "project update <project> --audio-input <PCM16.wav|off>", "configuration", surface="",
           dependencies=("nonempty_16bit_PCM_WAV_project_asset",),
           prerequisites=("explicit_test_audio_configuration",),
           limitations="Project launch configuration, not a business step. Uses staged synthetic/file audio as microphone input on both engines/exports. WebRTC connectivity depends on host ICE/network policy; external ASR quality is not established by file capture."),
]


def help_query(argv: list[str]) -> str | None:
    """Recognize wrapper help before session lookup; literal text after -- is data."""
    if not argv:
        return ""
    if argv[0] in ("help", "capabilities"):
        return " ".join(argv[1:])
    before_literal = argv[:argv.index("--")] if "--" in argv else argv
    if any(word in ("-h", "--help") for word in before_literal):
        return " ".join(word for word in before_literal if word not in ("-h", "--help"))
    return None


def catalog(query: str = "", group: str | None = None) -> dict:
    """Return declared support only; do not inspect sessions, files or executables."""
    rows = deepcopy(_CAPABILITIES)
    groups = sorted({row["group"] for row in rows})
    if group and group not in groups:
        raise ValueError(f"Unknown capability group {group!r}; choose: {', '.join(groups)}")
    selected = [row for row in rows if not group or row["group"] == group]
    query = query.strip().removeprefix("autoskill ").removeprefix("browser ").strip()
    if query:
        matches, prefixes = [], []
        for row in selected:
            names = [row["command"], row["id"], *row["aliases"]]
            if query == row["group"] or any(name == query or name.startswith(query + " ") for name in names):
                matches.append(row)
            scores = [len(name) for name in names if query.startswith(name + " ")]
            if scores:
                prefixes.append((max(scores), row))
        # A specific option variant (cookies set --curl, wait --text) must
        # supersede the shorter portable base command when supplied in a query.
        if not matches and prefixes:
            longest = max(score for score, _ in prefixes)
            matches = [row for score, row in prefixes if score == longest]
        selected = matches
        if not selected:
            raise ValueError(f"Unknown browser capability {query!r}; use autoskill capabilities to list commands and groups")
    modes = Counter(row["recording"] for row in selected)
    return {
        "schema_version": SCHEMA_VERSION,
        "source": {"agent_browser_version": AGENT_BROWSER_VERSION, "adapter": "autoSkill"},
        "verified": False,
        "verification": "Static implementation catalog; this discovery call does not probe environment availability or browser behavior.",
        "evidence_report": "reports/能力补齐与内部闭环验证.md",
        "evidence_scope": "Source-checkout internal fixture evidence on Debian 13 x64 only; consult the report for actual executed paths and limits. It does not verify the current host, user site, external ASR or DSH integration.",
        "definitions": {
            "executable": "An adapter/native execution path exists, subject to the listed dependencies and prerequisites.",
            "recordable": "Business recording persists a portable action or an explicit nonportable native_step; observations are not case steps.",
            "replayable": "Declared autoSkill action support on each engine; native-only mutations block replay rather than being skipped.",
            "exportable": "Declared standalone Python script support on each engine; native_step blocks export.",
            "verified": "False is not a test result. Discovery itself does not run commands or verify runtime behavior.",
        },
        "groups": groups,
        "counts": {"total": len(selected), "executable": sum(row["executable"] for row in selected),
                   "recordable": sum(row["recordable"] for row in selected),
                   "portable": modes["portable"], "native_only": modes["native_only"],
                   "observations": modes["observation"], "administration": modes["administration"],
                   "configuration": modes["configuration"], "unavailable": modes["unavailable"],
                   "verified": sum(row["verified"] for row in selected)},
        "limitations": [
            "Portable support covers the documented adapter syntax, not every upstream flag or arbitrary native plugin.",
            "Command queries return documentation, not a validation of every argument in an invocation; use the listed syntax and limitations.",
            "Recording owns browser/session identity, tabs/frames and HAR; lifecycle changes report prerequisites instead of silently disturbing it.",
            "Native observations create evidence, not assertions. Use autoskill check for business expectations.",
            "No site allowlist or proxy fence is imposed by autoSkill. DSH is an optional host adapter.",
        ],
        "capabilities": selected,
    }
