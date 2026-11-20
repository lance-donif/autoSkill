"""Measure cold-navigation CLS on the real loopback dashboard and the local Pages application."""
import base64
import gzip
import hashlib
import json
import os
import re
import statistics
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from functools import partial
from http.server import ThreadingHTTPServer
from importlib.metadata import version
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from test_load_dashboard import ROOT, DashboardWorkspace
from test_pages_browser import StaticFiles

from autoskill import load_testing, paths, runner

VIEWPORTS = {1440: 900, 390: 844}
TARGETS = ("dashboard", "pages")
# Markers require real content, including data-backed graphs. Reading their size does not scroll.
MARKERS = {"overview": ".kpis ~ .dash-grid .chart-svg", "cases": ".project-folders:has(nav.folder-tree) .folder-content .case-title",
           "performance": '[data-chart="percentiles"] svg[role="img"]'}
REPEATS = 5
BUDGET = 0.1
OBSERVER = Path(__file__).parent / "fixtures/dashboard-cls.js"
CATEGORIES = ("devtools.timeline,disabled-by-default-devtools.timeline,disabled-by-default-devtools.timeline.frame,"
              "blink.user_timing,loading,disabled-by-default-layout_shift.debug,disabled-by-default-devtools.screenshot")


def cls(entries):
    """Maximum session sum, with sub-second gaps and a duration under five seconds."""
    start = last = None
    total = maximum = 0.0
    for entry in entries:
        if entry["had_recent_input"]:
            continue
        when = entry["time_ms"]
        if start is None or when - start >= 5000 or when - last >= 1000:
            start, total = when, 0.0
        total += entry["value"]
        maximum = max(maximum, total)
        last = when
    return maximum


def write_json(path, data):
    staging = path.with_name(path.name + ".tmp")
    staging.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    staging.replace(path)


def write_report(output, report):
    expected = len(report["groups"]) * REPEATS
    completed = warnings = 0
    rows = []
    for group in report["groups"]:
        values = [run["cls"] for run in group["runs"]]
        completed += len(values)
        warnings += sum(value > BUDGET for value in values)
        stats = [min(values), statistics.median(values), max(values)] if values else [None] * 3
        group["min"], group["median"], group["max"] = stats
        cells = " | ".join(f"{value:.6f}" if value is not None else "—" for value in stats)
        rows.append(f"| {group['target']} | {group['scenario']} | {group['width']} × {group['height']} | {len(values)}/{REPEATS} | {cells} |")
    report.update(expected_runs=expected, completed_runs=completed, over_budget_runs=warnings,
                  complete=completed == expected and not report["errors"])
    write_json(output / "report.json", report)
    lines = ["## Chromium 布局偏移（CLS）", "", f"有效测量 {completed}/{expected} 次，其中 {warnings} 次超过 {BUDGET}。", "",
             "| 页面来源 | 场景 | 视口（CSS 像素） | 次数 | 最小值 | 中位数 | 最大值 |", "|---|---|---|---:|---:|---:|---:|", *rows,
             "", "每组首轮和首次超预算测量保存 Chromium trace 与视口截图。每次测量的 observation.json 包含事件、移动元素及坐标。"]
    if report["errors"]:
        lines.extend(["", f"有 {len(report['errors'])} 项测量或初始化错误，详情见 report.json。"])
    (output / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


class CLSWorkspace(DashboardWorkspace):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as area, patch.dict(os.environ, AUTOSKILL_WORKSPACE=area):
            if not paths.chrome():
                raise RuntimeError("AUTOSKILL_CHROMIUM must identify an installed Chromium browser")
            super().setUpClass()

    def seed_report(self):
        regular = runner.create(self.pid, ["TC-BEGIN"], headed=False)
        self.assertEqual(runner.execute(regular["id"])["status"], "Pass")
        load = load_testing.enqueue(self.pid, ["TC-SLOW"], mode="performance", concurrency=1, target_requests=4)
        self.assertEqual(runner.execute(load["id"])["status"], "Pass")
        return load["id"]

    def setUp(self):
        super().setUp()
        report_id = self.seed_report()
        detail = self.client.get("/api/load-tests/" + report_id).raise_for_status().json()
        self.assertEqual(len(detail["load_report"]["samples"]), 4)
        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(StaticFiles, directory=ROOT))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.origins = {"dashboard": self.origin + "/", "pages": f"http://127.0.0.1:{server.server_port}/preview/dashboard.html"}
        self.load_ids = {"dashboard": report_id, "pages": "load-demo-performance"}

    def url(self, target, scenario):
        fragment = {"overview": "dashboard?range=all", "cases": "cases?range=all", "performance": "load-tests/" + self.load_ids[target]}
        return self.origins[target] + "#/" + fragment[scenario]


def end_trace(cdp, page, handles, destination):
    cdp.send("Tracing.end")
    deadline = time.monotonic() + 15
    while not handles:
        if time.monotonic() >= deadline:
            raise TimeoutError("Chromium did not complete its performance trace")
        page.wait_for_timeout(20)
    handle = handles[0]
    try:
        if destination:
            with gzip.open(destination, "wb", compresslevel=3) as stream:
                while True:
                    chunk = cdp.send("IO.read", {"handle": handle})
                    stream.write(base64.b64decode(chunk["data"]) if chunk.get("base64Encoded") else chunk["data"].encode())
                    if chunk["eof"]:
                        break
    finally:
        cdp.send("IO.close", {"handle": handle})


def measure(browser, url, scenario, width, folder, *, representative, have_violation):
    context = browser.new_context(viewport={"width": width, "height": VIEWPORTS[width]}, device_scale_factor=1,
                                  locale="zh-CN", timezone_id="UTC", color_scheme="light", service_workers="block")
    folder.mkdir(parents=True, exist_ok=True)
    errors = []

    def reject(route):
        errors.append("Unexpected browser request: " + route.request.url)
        route.abort()

    try:
        origin = urlsplit(url).netloc
        context.route(re.compile(r"^https?://(?!" + re.escape(origin) + r"/)"), reject)
        if "/preview/" in url:
            context.route("**/api/**", reject)
        context.add_init_script(path=OBSERVER)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        cdp = context.new_cdp_session(page)
        handles = []
        cdp.on("Tracing.tracingComplete", lambda event: handles.append(event["stream"]))
        cdp.send("Network.enable")
        cdp.send("Network.setCacheDisabled", {"cacheDisabled": True})
        cdp.send("Tracing.start", {"categories": CATEGORIES, "transferMode": "ReturnAsStream"})
        retain = True
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
            if not response or not response.ok:
                raise AssertionError(f"Failed navigation: {url}")
            observation = page.evaluate("selector => window.dashboardCLS(selector)", MARKERS[scenario])
            if errors or observation["viewport"] != {"width": width, "height": VIEWPORTS[width], "dpr": 1}:
                raise AssertionError(f"Invalid browser sample: viewport={observation['viewport']}, errors={errors}")
            value = cls(observation["entries"])
            retain = representative or (value > BUDGET and not have_violation)
            write_json(folder / "observation.json", {**observation, "cls": value})
        finally:
            end_trace(cdp, page, handles, folder / "trace.json.gz" if retain else None)
            if retain:
                page.screenshot(path=folder / "viewport.png", timeout=5000)
    finally:
        context.close()
    return value, retain


class DashboardCLSTests(CLSWorkspace):
    @classmethod
    @contextmanager
    def record_setup_errors(cls, stage):
        try:
            yield
        except Exception as error:
            cls.report["errors"].append({"stage": stage, "error": str(error)})
            write_report(cls.output, cls.report)
            raise

    @classmethod
    def setUpClass(cls):
        selected = os.environ.get("AUTOSKILL_VALIDATION_OUTPUT")
        cls.output = Path(selected) if selected else Path(tempfile.mkdtemp(prefix="autoskill-cls-"))
        cls.output.mkdir(parents=True, exist_ok=True)
        groups = [{"target": target, "scenario": scenario, "width": width, "height": height, "runs": []}
                  for target in TARGETS for scenario in MARKERS for width, height in VIEWPORTS.items()]
        cls.report = {
            "budget": BUDGET, "repeats": REPEATS, "groups": groups, "errors": [], "playwright": version("playwright"),
            "conditions": {"navigation": "cold top-level document, no input or scrolling", "device_scale_factor": 1,
                           "locale": "zh-CN", "timezone": "UTC", "color_scheme": "light", "cpu_throttling": 1,
                           "latency_ms": {"route_data_and_pages_catalog": 300, "folder_and_runtime_status": 650}, "settle_ms": 1100,
                           "dashboard_fixture": "load-dashboard: 5 cases, 1 ordinary run, 1 finished performance report with 4 samples",
                           "pages_fixture_sha256": hashlib.sha256((ROOT / "preview/fixtures.json").read_bytes()).hexdigest(),
                           "observer_sha256": hashlib.sha256(OBSERVER.read_bytes()).hexdigest()},
        }
        write_report(cls.output, cls.report)
        with cls.record_setup_errors("browser"):
            super().setUpClass()
            cls.report["chromium"] = cls.browser.version

    def setUp(self):
        with self.record_setup_errors("fixture"):
            super().setUp()

    def test_cold_navigation_layout_shifts(self):
        for group in self.report["groups"]:
            target, scenario, width = (group[key] for key in ("target", "scenario", "width"))
            url = self.url(target, scenario)
            for index in range(1, REPEATS + 1):
                path = Path(target, scenario, str(width), f"run-{index}")
                with self.subTest(target=target, scenario=scenario, width=width, repetition=index):
                    try:
                        value, retained = measure(self.browser, url, scenario, width, self.output / path, representative=index == 1,
                                                  have_violation=any(run["cls"] > BUDGET for run in group["runs"]))
                        run = {"index": index, "cls": value, "observation": (path / "observation.json").as_posix()}
                        if retained:
                            run.update(trace=(path / "trace.json.gz").as_posix(), screenshot=(path / "viewport.png").as_posix())
                        group["runs"].append(run)
                    except Exception as error:
                        self.report["errors"].append({"run": path.as_posix(), "error": str(error)})
                        raise
                    finally:
                        write_report(self.output, self.report)
        self.assertTrue(self.report["complete"], self.output / "report.json")


class CLSWindowTests(unittest.TestCase):
    def test_session_boundaries_and_recent_input(self):
        def entry(time_ms, value, recent=False):
            return {"time_ms": time_ms, "value": value, "had_recent_input": recent}

        self.assertEqual(cls([]), 0)
        self.assertAlmostEqual(cls([entry(0, 0.04), entry(800, 0.9, recent=True), entry(1000, 0.09)]), 0.09)
        burst = [entry(time_ms, 0.01) for time_ms in (0, 900, 1800, 2700, 3600, 4500)]
        self.assertAlmostEqual(cls([*burst, entry(5000, 0.02)]), 0.06)
