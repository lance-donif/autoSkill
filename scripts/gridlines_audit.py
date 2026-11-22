"""Batch gridline audit: open every dashboard page and overlay, draw guides with probe.js and collect misses.

Maintainer tool. The default target is the Pages preview served from this checkout, which runs the
product UI against preview/fixtures.json. ``--url`` targets a running local dashboard instead.

    python scripts/gridlines_audit.py --scheme both
    python scripts/gridlines_audit.py --url http://127.0.0.1:8787/ --only cases,replay-settings
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "scripts" / "gridlines" / "probe.js"
WIDTHS = ((1440, 900), (390, 844))
READY_TIMEOUT_MS = 15000
SETTLE_MS = 600
PAGE_READY = '#view:not([aria-busy="true"]) .page-head'


@dataclass(frozen=True)
class Scenario:
    """One state to measure: a hash route, optional clicks, and the marker that says it is ready."""

    name: str
    label: str
    route: str
    ready: str = PAGE_READY
    steps: tuple[str, ...] = ()
    widths: tuple[int, ...] = (1440, 390)
    # Overlay states measure only the containers inside this element, not the page behind it.
    focus: str | None = None


# Every route in ui/js/main.js PAGES, detail routes that exist in the preview fixtures, and the overlays.
# Each step is a Playwright selector that is clicked once the previous marker is visible.
SCENARIOS = (
    Scenario("dashboard", "总览", "dashboard?range=all", "#view .kpis"),
    Scenario("cases", "测试用例", "cases?range=all", "#view .project-folders"),
    Scenario("load-tests", "压测记录", "load-tests", "#view .load-mode-tabs"),
    Scenario("project-materials", "项目资料", "project-materials"),
    Scenario("native-assets", "原生资产", "native-assets"),
    Scenario("native-runs", "原生执行记录", "native-runs?range=all"),
    Scenario("workflows", "需求与工作流", "workflows?projects=stock-room"),
    Scenario("case-plans", "测试用例表", "case-plans?projects=stock-room"),
    Scenario("conversations", "多轮对话", "conversations"),
    Scenario("diagnostics", "运行诊断", "diagnostics"),
    Scenario("runs", "执行记录", "runs?range=all"),
    Scenario("schedules", "定时任务", "schedules"),
    Scenario("data", "数据管理", "data"),
    Scenario("run-detail", "执行详情", "runs/run-demo-05?range=all", "#view .result"),
    Scenario("load-report-performance", "性能测试报告", "load-tests/load-demo-performance", "#view .load-dashboard"),
    Scenario("load-report-stress", "压力测试报告", "load-tests/load-demo-stress", "#view .load-dashboard"),
    Scenario("case-inspector-ui", "页面用例详情", "cases?range=all&projects=runoob", "#layer .drawer",
             ('#view button:text-is("读者从首页进入Python3基础语法章节")',), focus="#layer"),
    Scenario("case-inspector-api", "接口用例详情", "cases?range=all&projects=stock-room", "#layer .drawer",
             ('#view button:text-is("读取可售库存")',), focus="#layer"),
    Scenario("case-editor", "编辑用例", "cases?range=all&projects=stock-room", "#layer .drawer",
             ('#view button[aria-label="编辑用例 ST-API-001"]',), focus="#layer"),
    Scenario("confirm-delete", "删除确认", "cases?range=all&projects=stock-room", '#layer [role="alertdialog"]',
             ('#view button[aria-label="删除用例 ST-API-001"]',), focus="#layer"),
    Scenario("replay-settings", "回放设置", "dashboard?range=all", "#scope .menu.settings",
             ('#scope button:has-text("回放设置")',), focus="#scope .pop"),
    Scenario("project-menu", "项目范围菜单", "dashboard?range=all", "#scope .menu",
             ('#scope button[aria-label="筛选项目"]',), focus="#scope .pop"),
    Scenario("range-menu", "时间范围菜单", "dashboard?range=all", "#scope .menu",
             ('#scope button[aria-label="筛选时间范围"]',), focus="#scope .pop"),
    Scenario("schedule-new", "新建定时任务", "schedules", "#layer .drawer",
             ('#view .page-head button:has-text("新建定时任务")',), focus="#layer"),
    Scenario("load-launcher", "新建压测", "load-tests", "#layer .drawer",
             ('#view button:has-text("新建压测")',), focus="#layer"),
    # Starting one case queues it in the preview and opens its live execution detail.
    Scenario("run-launch", "启动后的执行详情", "cases?range=all&projects=stock-room", "#view .run-summary",
             ('#view tbody button:text-is("运行") >> nth=0',)),
    Scenario("nav-drawer", "手机导航", "dashboard?range=all", '.side[data-nav-open="true"] .side-panel',
             (".side .nav-toggle",), (390,), ".side"),
)

# Settle: fonts loaded and no finite animation still running.
SETTLED = """() => document.fonts.ready.then(() => !document.getAnimations().some((a) => {
  const timing = a.effect && a.effect.getComputedTiming();
  return a.playState === 'running' && timing && Number.isFinite(timing.endTime);
}))"""

# Classify each measured container in the order the probe visits them; `shown` mirrors probe.js.
REGIONS = """(selector) => {
  const shown = (el) => {
    if (getComputedStyle(el).visibility === 'hidden') return false;
    for (let a = el; a; a = a.parentElement) if (parseFloat(getComputedStyle(a).opacity) === 0) return false;
    return true;
  };
  return [...new Set(document.querySelectorAll(selector))].filter((c) => {
    const r = c.getBoundingClientRect();
    return r.width > 120 && r.height > 24 && shown(c);
  }).map((c) => ({
    region: c.closest('#layer, .menu, .side[data-nav-open="true"] .side-panel') ? 'overlay' : c.closest('.side, .scope') ? 'shell' : 'page',
    hint: c.tagName.toLowerCase() + [...c.classList].slice(0, 2).map((n) => '.' + n).join(''),
  }));
}"""


# The probe's container list, optionally limited to one element and its descendants.
CONTAINERS = """(focus) => {
  const all = window.__gridlines.containers.join(', ');
  return focus ? `:is(${focus}, ${focus} *):is(${all})` : all;
}"""


class QuietFiles(SimpleHTTPRequestHandler):
    """Static files without request logging."""

    def log_message(self, *_args):
        pass


@contextmanager
def preview_server():
    """Serve the checkout and yield the Pages preview URL."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietFiles, directory=str(ROOT)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/preview/dashboard.html"
    finally:
        server.shutdown()
        server.server_close()


@dataclass
class Audit:
    """Captures, uncovered states and page errors collected over one run."""

    out: Path
    captures: list[dict] = field(default_factory=list)
    uncovered: list[dict] = field(default_factory=list)


def parse_args(argv):
    parser = argparse.ArgumentParser(description="批量画参考线，找出仪表盘各页面与浮层中偏离参考线的元素。")
    parser.add_argument("--url", help="已运行的本机仪表盘地址，例如 http://127.0.0.1:8787/；默认使用 Pages 预览")
    parser.add_argument("--chromium", help="Chromium/Chrome 可执行文件；默认读取 AUTOSKILL_CHROMIUM")
    parser.add_argument("--scheme", choices=("light", "dark", "both"), default="light", help="配色，默认 light")
    parser.add_argument("--only", help="只运行这些场景，逗号分隔")
    parser.add_argument("--out", default=".gridlines", help="输出目录，默认 .gridlines")
    parser.add_argument("--fail-on-miss", action="store_true", help="存在偏离时以 1 退出")
    args = parser.parse_args(argv)
    names = {s.name for s in SCENARIOS}
    args.selected = [s.strip() for s in args.only.split(",") if s.strip()] if args.only else sorted(names)
    unknown = sorted(set(args.selected) - names)
    if unknown:
        parser.error("未知场景：" + "、".join(unknown) + "。可选：" + "、".join(s.name for s in SCENARIOS))
    return args


def capture_name(scenario, width, scheme):
    return f"{scenario.name}-{width}" + ("-dark" if scheme == "dark" else "")


def open_state(page, base, scenario, scheme):
    """Load the scenario's route, apply its steps and wait until it has settled."""
    page.goto(base + "#/" + scenario.route, wait_until="domcontentloaded")
    for step in scenario.steps:
        page.locator(step).first.click(timeout=READY_TIMEOUT_MS)
    page.locator(scenario.ready).first.wait_for(state="visible", timeout=READY_TIMEOUT_MS)
    # A saved dashboard theme wins over the emulated colour scheme, so pin the theme being measured.
    page.evaluate("(scheme) => { document.documentElement.dataset.theme = scheme; }", scheme)
    page.wait_for_function(SETTLED, timeout=READY_TIMEOUT_MS)
    page.wait_for_timeout(SETTLE_MS)


def measure(audit, context, base, scenario, width, scheme):
    name = capture_name(scenario, width, scheme)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        open_state(page, base, scenario, scheme)
        page.add_script_tag(path=str(PROBE))
        selector = page.evaluate(CONTAINERS, scenario.focus)
        containers = page.evaluate("(containers) => window.__gridlines({ containers })", selector)
        regions = page.evaluate(REGIONS, selector)
        page.screenshot(path=str(audit.out / f"{name}.png"))
    except Exception as error:  # noqa: BLE001  # any Playwright failure marks the state as not covered
        audit.uncovered.append({"capture": name, "scenario": scenario.name, "label": scenario.label,
                                "width": width, "scheme": scheme, "error": str(error).splitlines()[0]})
        return
    finally:
        page.close()
    for container, region in zip(containers, regions):
        container.update(region)
    capture = {"capture": name, "scenario": scenario.name, "label": scenario.label, "width": width,
               "scheme": scheme, "route": scenario.route, "page_errors": errors, "containers": containers}
    (audit.out / f"{name}.json").write_text(json.dumps(capture, ensure_ascii=False, indent=1), encoding="utf-8")
    audit.captures.append(capture)


def run(args, base, chromium):
    from playwright.sync_api import sync_playwright

    audit = Audit(Path(args.out).resolve())
    audit.out.mkdir(parents=True, exist_ok=True)
    schemes = ("light", "dark") if args.scheme == "both" else (args.scheme,)
    scenarios = [s for s in SCENARIOS if s.name in args.selected]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium, headless=True)
        try:
            for scheme in schemes:
                for width, height in WIDTHS:
                    # The preview pins its scripts with a CSP hash, so the injected probe needs CSP bypass.
                    context = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=2,
                                                  color_scheme=scheme, bypass_csp=True)
                    for scenario in scenarios:
                        if width in scenario.widths:
                            measure(audit, context, base, scenario, width, scheme)
                    context.close()
        finally:
            browser.close()
    return audit


def group_misses(captures):
    """Group identical misses across captures: region → container → edge → (px, line, item) → captures."""
    groups = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: defaultdict(list))))
    for capture in captures:
        for container in capture["containers"]:
            title = f"{container['container']} ({container.get('hint', '?')})"
            for miss in container["outliers"]:
                key = (miss["px"], miss["line"], miss["item"])
                names = groups[container.get("region", "page")][title][miss["edge"]][key]
                if capture["capture"] not in names:
                    names.append(capture["capture"])
    return groups


def miss_table(captures):
    rows = []
    for capture in captures:
        count = sum(len(c["outliers"]) for c in capture["containers"])
        shell = sum(len(c["outliers"]) for c in capture["containers"] if c.get("region") == "shell")
        rows.append({"capture": capture["capture"], "scenario": capture["scenario"], "width": capture["width"],
                     "scheme": capture["scheme"], "misses": count, "shell_misses": shell})
    return rows


def grouped_json(groups):
    return {region: {title: {edge: [{"px": px, "line": line, "item": item, "captures": names}
                                    for (px, line, item), names in sorted(entries.items(), key=lambda kv: -len(kv[1]))]
                             for edge, entries in edges.items()}
                     for title, edges in containers.items()}
            for region, containers in groups.items()}


REGION_TITLES = {"shell": "外壳（侧边栏与范围栏，跨页面相同的偏离只列一次）", "page": "页面容器", "overlay": "浮层（菜单、抽屉、对话框）"}


def captures_text(names, total):
    if len(names) == total:
        return "全部截图"
    return "、".join(names) if len(names) <= 6 else "、".join(names[:6]) + f" 等 {len(names)} 张"


def summary_lines(audit, rows, groups):
    total_misses = sum(r["misses"] for r in rows)
    lines = ["# 参考线批量检查", "",
             f"- 截图：{len(audit.captures)} 张；未覆盖：{len(audit.uncovered)} 个状态",
             f"- 偏离记录：{total_misses} 条（去重后 {sum(len(e) for c in groups.values() for t in c.values() for e in t.values())} 组）",
             "- 每张截图与同名 JSON 在本目录；PNG 已画参考线（红：起始线与标签线，蓝：尾线，琥珀：尾列中线）。", "",
             "## 每个状态的偏离数", "", "| 截图 | 偏离 | 其中外壳 |", "| --- | ---: | ---: |"]
    lines += [f"| {r['capture']} | {r['misses']} | {r['shell_misses']} |" for r in rows]
    for region in ("shell", "page", "overlay"):
        if region not in groups:
            continue
        lines += ["", f"## {REGION_TITLES[region]}"]
        for title, edges in sorted(groups[region].items()):
            lines += ["", f"### {title}"]
            for edge, entries in sorted(edges.items()):
                lines.append(f"- **{edge}**")
                for (px, line, item), names in sorted(entries.items(), key=lambda kv: -len(kv[1])):
                    lines.append(f"  - {px}px，参考线 {line}px：`{item}` — {captures_text(names, len(audit.captures))}")
    lines += ["", "## 未覆盖", ""]
    lines += [f"- {u['capture']}（{u['label']}）：{u['error']}" for u in audit.uncovered] or ["- 无"]
    errors = [(c["capture"], e) for c in audit.captures for e in c["page_errors"]]
    if errors:
        lines += ["", "## 页面脚本错误", ""] + [f"- {name}：{error}" for name, error in errors]
    return lines


def write_report(audit, args, target):
    rows = miss_table(audit.captures)
    groups = group_misses(audit.captures)
    report = {"target": target, "scheme": args.scheme, "scenarios": args.selected, "captures": rows,
              "misses": grouped_json(groups), "uncovered": audit.uncovered,
              "page_errors": [{"capture": c["capture"], "errors": c["page_errors"]} for c in audit.captures if c["page_errors"]]}
    (audit.out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    (audit.out / "summary.md").write_text("\n".join(summary_lines(audit, rows, groups)) + "\n", encoding="utf-8")
    return rows


def main(argv=None):
    args = parse_args(argv)
    chromium = args.chromium or os.environ.get("AUTOSKILL_CHROMIUM")
    if not chromium or not Path(chromium).is_file():
        print("找不到 Chromium：请用 --chromium 或 AUTOSKILL_CHROMIUM 指定浏览器可执行文件。", file=sys.stderr)
        return 2
    try:
        import playwright.sync_api  # noqa: F401  # fail early with a readable message
    except ImportError:
        print("缺少 Playwright：请先安装 requirements 中的依赖。", file=sys.stderr)
        return 2
    if args.url:
        target = args.url.rstrip("/") + "/"
        audit = run(args, target, chromium)
    else:
        with preview_server() as target:
            audit = run(args, target, chromium)
    rows = write_report(audit, args, target)
    total = sum(r["misses"] for r in rows)
    shell = sum(r["shell_misses"] for r in rows)
    print(f"参考线检查完成：{len(rows)} 张截图，偏离 {total} 条（外壳 {shell} 条），未覆盖 {len(audit.uncovered)} 个状态。")
    for item in audit.uncovered:
        print(f"  未覆盖 {item['capture']}：{item['error']}")
    print(f"报告：{audit.out / 'summary.md'}")
    return 1 if args.fail_on_miss and total else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
