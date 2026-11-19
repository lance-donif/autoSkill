"""Check the reference palette on the live dashboard and its self-contained HTML export."""
import json
import mimetypes
import os
import unittest
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "skills/autoskill/app/ui"
ORIGIN = "http://dashboard.test"
MENUS = ("总览", "测试用例", "执行记录", "定时任务", "数据管理")
# Approved light and dark colours from e0b26b0, independent of the styles under test.
PALETTES = {
    "light": {
        "plane": "#f4f4f1", "surface": "#fcfcfb", "ink": "#0b0b0b", "secondary": "#52514e",
        "side": "#17191c", "accent": "#2a78d6", "active": "#1c5cab", "selected": "#e6f0fc",
        "pass": ("#006300", "#e4f5e4"), "fail": ("#a82424", "#fbe6e6"), "idle": ("#52514e", "#eeede8"),
    },
    "dark": {
        "plane": "#0d0d0d", "surface": "#1a1a19", "ink": "#f0efec", "secondary": "#c3c2b7",
        "side": "#111112", "accent": "#3987e5", "active": "#86b6ef", "selected": "#16263a",
        "pass": ("#4cc24c", "#13281a"), "fail": ("#ee7a7a", "#331a1a"), "idle": ("#c3c2b7", "#262625"),
    },
}
SERIES = {"未执行": "#898781", "通过": "#0ca30c", "失败": "#d03b3b"}


def rgb(hex_colour):
    return "rgb({}, {}, {})".format(*bytes.fromhex(hex_colour[1:]))


def luminance(hex_colour):
    channels = [x / 255 for x in bytes.fromhex(hex_colour[1:])]
    linear = [x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in channels]
    return sum(x * weight for x, weight in zip(linear, (0.2126, 0.7152, 0.0722), strict=True))


@contextmanager
def failure_screenshot(page, name):
    try:
        yield
    except Exception:
        output = os.environ.get("AUTOSKILL_VALIDATION_OUTPUT")
        if output:
            directory = Path(output) / "palette"
            directory.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=directory / f"{name}.png", timeout=5000)
        raise


class DashboardPaletteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        playwright = sync_playwright().start()
        cls.addClassCleanup(playwright.stop)
        cls.browser = playwright.chromium.launch(executable_path=os.environ.get("AUTOSKILL_CHROMIUM"))
        cls.addClassCleanup(cls.browser.close)
        cls.fixtures = json.loads((ROOT / "preview/fixtures.json").read_text(encoding="utf-8"))
        cls.report = next(run["id"] for run in cls.fixtures["/runs"]["items"]
                          if run["family"] == "replay" and run["status"] == "Fail")

    def serve(self, route):
        url = urlsplit(route.request.url)
        if url.hostname != "dashboard.test" or route.request.method != "GET":
            self.fail(f"Unexpected dashboard request: {route.request.method} {url.geturl()}")
        if url.path.startswith("/api/"):
            key = url.path.removeprefix("/api")
            entry = self.fixtures.get(key + "?" + url.query, self.fixtures.get(key))
            if entry is None:
                self.fail(f"Missing dashboard fixture: {url.path}")
            route.fulfill(json=entry)
        else:
            file = UI / (url.path.lstrip("/") or "index.html")
            if file.is_file():
                route.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file)[0] or "text/plain")
            else:
                route.fulfill(status=404)

    def check_text(self, element, colour, background):
        expect(element).to_have_css("color", rgb(colour))
        expect(element).to_have_css("background-color", rgb(background))
        low, high = sorted((luminance(colour), luminance(background)))
        self.assertGreaterEqual((high + 0.05) / (low + 0.05), 4.5, (colour, background))

    def check_menus(self, page, palette, width):
        nav = page.get_by_role("navigation", name="主导航")
        for label in MENUS:
            if width == 390:
                toggle = page.get_by_role("button", name="功能导航")
                expect(toggle).to_have_css("color", rgb("#e9e8e4"))
                toggle.click()
            link = nav.get_by_role("link", name=label, exact=True)
            link.click()
            expect(page.locator("#view h1")).to_have_text(label)
            active_link = page.locator(".nav a[aria-current=page]")
            expect(active_link).to_have_attribute("aria-label", label)
            expect(active_link).to_have_css("color", rgb("#ffffff"))
            expect(page.locator(".side")).to_have_css("background-color", rgb(palette["side"]))
            self.check_text(page.locator("body"), palette["ink"], palette["plane"])
            expect(page.locator("#view .card").first).to_have_css("background-color", rgb(palette["surface"]))
            if label == "总览":
                expect(page.locator(".brand-mark")).to_have_css("background-color", rgb(palette["accent"]))
                for name, colour in SERIES.items():
                    swatch = page.locator(".legend-item").filter(has_text=name).locator(".swatch")
                    expect(swatch).to_have_css("background-color", rgb(colour))
                bars = page.locator(".chart-svg .col path").evaluate_all(
                    "(bars) => [...new Set(bars.map(bar => getComputedStyle(bar).fill))]")
                self.assertCountEqual(bars, [rgb(colour) for colour in SERIES.values()])
            elif label == "测试用例":
                self.check_text(page.locator(".folder-item.selected").first, palette["active"], palette["selected"])
            elif label == "执行记录":
                for kind in ("pass", "fail", "idle"):
                    self.check_text(page.locator(f"#view .status.{kind}").first, *palette[kind])
            else:
                expect(page.locator("#view .btn.primary").first).to_have_css("background-color", rgb(palette["accent"]))

    def check_offline(self, page, theme, palette, viewport):
        page.goto(f"{ORIGIN}/#/runs/{self.report}?range=all")
        download_button = page.get_by_role("button", name="下载 HTML 报告")
        with page.expect_download() as receipt:
            download_button.click()
        with self.browser.new_context(offline=True, viewport=viewport) as offline:
            document = offline.new_page()
            with failure_screenshot(document, f"offline-{viewport['width']}-{theme}"):
                document.set_content(receipt.value.path().read_text(encoding="utf-8"))
                expect(document.locator("html")).to_have_attribute("data-theme", theme)
                self.check_text(document.locator("body"), palette["ink"], palette["plane"])
                expect(document.locator(".run-summary")).to_have_css("background-color", rgb(palette["surface"]))
                self.check_text(document.locator(".run-summary .status.fail"), *palette["fail"])
                expect(document.locator(".report-toolbar .scope-text")).to_have_css("color", rgb(palette["secondary"]))

    def test_live_pages_and_downloaded_report(self):
        for width in (390, 1360):
            viewport = {"width": width, "height": 900}
            for theme, palette in PALETTES.items():
                with self.subTest(width=width, theme=theme), self.browser.new_context(
                    viewport=viewport, color_scheme=theme, reduced_motion="reduce",
                ) as context:
                    context.route("**/*", self.serve)
                    page = context.new_page()
                    latest_bucket = self.fixtures["/dashboard"]["summary"]["trend"]["points"][-1]["bucket"]
                    page.clock.set_fixed_time(latest_bucket + "T23:59:00Z")
                    page.goto(f"{ORIGIN}/#/dashboard?range=all")
                    expect(page.locator("html")).to_have_attribute("data-theme", theme)
                    with failure_screenshot(page, f"live-{width}-{theme}"):
                        self.check_menus(page, palette, width)
                        self.check_offline(page, theme, palette, viewport)
