"""Replay a measured loopback scenario under the official Chrome DevTools MCP server."""
import argparse
import json
import os
import re
import subprocess
import uuid
from contextlib import ExitStack
from pathlib import Path

from test_dashboard_cls import MARKERS, OBSERVER, VIEWPORTS, CLSWorkspace, cls, write_json

from autoskill import paths


def diagnostic(cli, workspace, group, output, *, embedded):
    width, scenario, target = group["width"], group["scenario"], group["target"]
    url = workspace.url(target, scenario)
    if embedded:
        url = url.replace("/#/", "/?embed=1#/")
    directory = output / ("devtools-embed" if embedded else "devtools")
    directory.mkdir(exist_ok=True)
    command = [str(cli), "--sessionId", uuid.uuid4().hex]
    environment = {**os.environ, "TZ": "UTC", "CHROME_DEVTOOLS_MCP_NO_UPDATE_CHECKS": "1"}

    def call(*arguments):
        result = subprocess.run([*command, *arguments], capture_output=True, text=True, timeout=60, env=environment, check=False)
        if result.returncode:
            raise RuntimeError(f"{arguments[0]} failed ({result.returncode}): {result.stderr or result.stdout}")
        return result.stdout

    def data(*arguments):
        return json.loads(call(*arguments, "--output-format", "json"))

    # The CLI daemon gets a unique session and an ephemeral browser profile for this navigation.
    call("start", "--headless", "--isolated", "--executablePath", paths.chrome(), "--chrome-arg=--no-sandbox",
         "--chrome-arg=--accept-lang=zh-CN", "--chrome-arg=--lang=zh-CN", "--proxyServer", "http://127.0.0.1:9",
         "--no-performance-crux", "--no-usage-statistics", "--filesystemRoot", str(directory))
    try:
        page = str(next(item["id"] for item in data("list_pages")["pages"] if item["selected"]))
        call("emulate", page, "--viewport", f"{width}x{VIEWPORTS[width]}x1", "--colorScheme", "light")
        call("performance_start_trace", page, "--reload=false", "--autoStop=false")
        call("navigate_page", page, "--type", "url", "--url", url, "--initScript", OBSERVER.read_text(encoding="utf-8"))
        observation_file = directory / "observation.json"
        call("evaluate_script", "() => window.dashboardCLS(" + json.dumps(MARKERS[scenario]) + ")",
             "--pageId", page, "--filePath", str(observation_file))
        observation = json.loads(observation_file.read_text(encoding="utf-8"))
        if observation["viewport"] != {"width": width, "height": VIEWPORTS[width], "dpr": 1}:
            raise RuntimeError(f"Unexpected MCP viewport: {observation['viewport']}")
        trace = data("performance_stop_trace", page, "--filePath", str(directory / "trace.json.gz"))
        write_json(directory / "performance_stop_trace.json", trace)
        # Each recorded navigation has its own insight set. Choose the one for this exact URL.
        sets = re.findall(r"^## insight set id: (\S+)\s+URL: ([^\n]+)", trace["traceSummary"], re.MULTILINE)
        insight_set = next(identity for identity, route in sets if route == url)
        insight = data("performance_analyze_insight", page, insight_set, "CLSCulprits")
        description = insight["traceInsight"]
        (directory / "CLSCulprits.md").write_text(description + "\n", encoding="utf-8")
        call("take_screenshot", page, "--filePath", str(directory / "viewport.png"))
        write_json(observation_file, {**observation, "cls": cls(observation["entries"]), "group": f"{target}/{scenario}/{width}",
                                      "embedded": embedded, "insight_set_id": insight_set, "cli_version": call("--version").strip()})
    finally:
        call("stop")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", required=True, type=Path, help="Pinned chrome-devtools executable from chrome-devtools-mcp")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--match", help="Replay a specific measured group, for example pages/cases/390")
    mode.add_argument("--embedded", action="store_true", help="Diagnose the separate desktop embedded dashboard entry")
    args = parser.parse_args()
    output = Path(os.environ["AUTOSKILL_VALIDATION_OUTPUT"]).resolve()
    groups = json.loads((output / "report.json").read_text(encoding="utf-8"))["groups"]
    measured = {f"{g['target']}/{g['scenario']}/{g['width']}": g for g in groups if g["runs"]}
    if args.embedded:
        group = measured["dashboard/overview/1440"]
    elif args.match:
        group = measured[args.match]
    else:
        group = max(measured.values(), key=lambda item: item["max"])
    with ExitStack() as cleanup:
        workspace = CLSWorkspace()
        cleanup.callback(workspace.doClassCleanups)
        workspace.setUpClass()
        cleanup.callback(workspace.doCleanups)
        workspace.setUp()
        diagnostic(args.cli.resolve(), workspace, group, output, embedded=args.embedded)
        workspace.tearDown()


if __name__ == "__main__":
    main()
