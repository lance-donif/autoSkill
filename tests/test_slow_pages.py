"""A page that is merely slow must be waited for, in recording and in replay, and a real timeout must say so."""
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))
__import__("os").environ.setdefault("AUTOSKILL_HEADED", "0")  # tests never open browser windows
from autoskill import paths, projects
from autoskill.recorder import agent, session
from autoskill.replay import ui

SIGN_IN = ("<form><label>User <input name=u></label><label>Password <input type=password name=p></label>"
           "<button>Sign in</button></form>")


class SlowSite(BaseHTTPRequestHandler):
    """/?d=N: the page's script arrives after N seconds. /app: draws late, then moves to its sign-in screen."""

    def log_message(self, *args):
        pass

    def do_GET(self):
        url = urlsplit(self.path)
        delay = float((parse_qs(url.query).get("d") or ["0"])[0])
        if url.path == "/slow.js":
            time.sleep(delay)
            body, kind = b"document.getElementById('app').innerHTML='<button data-testid=\"go\">Go</button>';", "text/javascript"
        elif url.path in ("/app", "/sign-in"):
            script = ("setTimeout(function () { history.replaceState(null, '', '/sign-in');"
                      f"document.getElementById('app').innerHTML = {json.dumps(SIGN_IN)}; }}, 2500);")
            body, kind = f"<!doctype html><title>app</title><div id=app></div><script>{script}</script>".encode(), "text/html"
        else:
            body, kind = f"<!doctype html><title>slow</title><div id=app></div><script src='/slow.js?d={delay}'></script>".encode(), "text/html"
        self.send_response(200)
        self.send_header("Content-Type", kind + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            return  # the browser went away


def serve():
    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowSite)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


class FakeBrowser:
    """Stands in for agent-browser: `open` gives up after its own wait while the page keeps loading."""

    def __init__(self, urls, states, open_error="Timed out after 25000ms waiting for load state 'load'"):
        self.urls, self.states, self.open_error, self.clock, self.opened = list(urls), list(states), open_error, 0.0, 0

    def call(self, name, args, **kwargs):
        if args[0] == "open":
            self.opened += 1
            self.clock += 31
            if self.open_error:
                raise agent.AgentError(self.open_error)
            return {}
        if args[:2] == ["get", "url"]:
            return {"url": self.urls.pop(0) if len(self.urls) > 1 else self.urls[0]}
        if args[0] == "eval":
            state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            return {"result": json.dumps({"state": state, "password": False})}
        raise AssertionError(args)

    def install(self, case):
        clock = types.SimpleNamespace(monotonic=lambda: self.clock, sleep=lambda s: setattr(self, "clock", self.clock + s))
        real_call, real_time = session.agent.call, session.time
        session.agent.call, session.time = self.call, clock
        case.addCleanup(lambda: (setattr(session.agent, "call", real_call), setattr(session, "time", real_time)))
        return self


class NavigationWaitTests(unittest.TestCase):
    URL = "http://127.0.0.1:9/app"

    def test_a_load_that_outlives_the_browser_tools_own_wait_is_not_a_failure(self):
        fake = FakeBrowser([self.URL], ["loading", "interactive", "complete"]).install(self)
        page = session._open(self.URL, 90)
        self.assertEqual((page["state"], page.get("slow"), fake.opened), ("complete", None, 1))  # waited, did not navigate twice
        self.assertGreater(page["seconds"], 31)

    def test_a_page_still_loading_when_the_budget_ends_is_handed_over_and_flagged(self):
        FakeBrowser([self.URL], ["loading"]).install(self)
        page = session._open(self.URL, 60)
        self.assertTrue(page["slow"])
        self.assertLess(page["seconds"], 70)

    def test_an_unreachable_page_is_an_error_that_keeps_the_browsers_reason(self):
        FakeBrowser(["chrome-error://chromewebdata/"], [""], "net::ERR_CONNECTION_REFUSED").install(self)
        with self.assertRaises(session.RecordError) as caught:
            session._open(self.URL, 90)
        self.assertIn("ERR_CONNECTION_REFUSED", str(caught.exception))

    def test_a_browser_that_stays_on_the_blank_page_is_retried_once_then_reported(self):
        fake = FakeBrowser(["about:blank"], [""], open_error="").install(self)
        with self.assertRaises(session.RecordError):
            session._open(self.URL, 90)
        self.assertEqual(fake.opened, 2)


class ReplayLoadBudgetMixin:
    @classmethod
    def setUpClass(cls):
        cls.site, cls.base = serve()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.config = {"base_url": cls.base, "allowed_origins": [cls.base], "timeout_seconds": 3}

    @classmethod
    def tearDownClass(cls):
        cls.site.shutdown()
        cls.site.server_close()
        cls.tmp.cleanup()

    def replay(self, config):
        locators = [{"by": "testid", "value": "go"}]
        automation = {"start_url": "/?d=6", "actions": [{"action": "click", "locators": locators}],
                      "assertions": [{"assertion": "text", "locators": locators, "expected": "Go"}]}
        case = {"id": "TC-SLOW", "title": "slow", "kind": "ui", "extensions": {"automation": automation}}
        driver = self.make_driver(config)
        try:
            driver.reset()
            return ui.run_case(driver, case, config, Path(self.tmp.name), "slow.png")
        finally:
            driver.close()

    def test_a_page_slower_than_the_step_timeout_still_replays(self):
        result = self.replay(self.config)  # 6 s to load, 3 s per step, 90 s for a page
        self.assertEqual(result["status"], "Pass", result["reason"])

    def test_a_page_slower_than_its_load_budget_is_reported_as_a_page_timeout(self):
        result = self.replay({**self.config, "page_load_seconds": 2})
        self.assertEqual((result["status"], result["category"]), ("Blocked", ui.TIMEOUT))
        self.assertIn("--page-load", result["reason"])


class PlaywrightLoadBudgetTests(ReplayLoadBudgetMixin, unittest.TestCase):
    @staticmethod
    def make_driver(config):
        from autoskill.replay.playwright_driver import PlaywrightDriver

        return PlaywrightDriver(config)


@unittest.skipUnless(paths.chrome() and paths.chromedriver(), "set AUTOSKILL_CHROMIUM and AUTOSKILL_CHROMEDRIVER")
class SeleniumLoadBudgetTests(ReplayLoadBudgetMixin, unittest.TestCase):
    @staticmethod
    def make_driver(config):
        from autoskill.replay.selenium_driver import SeleniumDriver

        return SeleniumDriver(config)


@unittest.skipUnless(paths.agent_browser() and paths.chrome(), "set AUTOSKILL_AGENT_BROWSER and AUTOSKILL_CHROMIUM")
class RecordingStartTests(unittest.TestCase):
    """The real recorder against pages that load late, draw late or bounce to a sign-in screen."""

    @classmethod
    def setUpClass(cls):
        cls.site, cls.base = serve()

    @classmethod
    def tearDownClass(cls):
        cls.site.shutdown()
        cls.site.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.previous = os.environ.get("AUTOSKILL_WORKSPACE")
        os.environ["AUTOSKILL_WORKSPACE"] = self.tmp.name
        projects.create("慢页面", self.base + "/?d=4", "", "slow")

    def tearDown(self):
        try:
            if session.current(required=False):
                session.cancel()
            agent.call(session.session_name(), ["close"], check=False, timeout=30)
        finally:
            if self.previous is None:
                os.environ.pop("AUTOSKILL_WORKSPACE", None)
            else:
                os.environ["AUTOSKILL_WORKSPACE"] = self.previous
            self.tmp.cleanup()

    def test_recording_waits_for_a_late_script_and_returns_usable_elements(self):
        started = session.start("slow")
        self.assertIn('button "Go"', started["snapshot"])
        self.assertNotIn("needs_login", started)
        self.assertNotIn("redirected", started)

    def test_a_page_that_bounces_to_its_sign_in_screen_says_so(self):
        started = session.start("slow", url="/app")
        self.assertTrue(started["url"].endswith("/sign-in"))
        self.assertIn("/sign-in", started["redirected"])
        self.assertIn("--as-login", started["needs_login"])
        self.assertIn("Password", started["snapshot"])  # the late-drawn form, not an empty first snapshot

    def test_a_recording_whose_browser_is_gone_does_not_block_the_next_one(self):
        session.start("slow", url="/?d=0")
        with self.assertRaises(session.RecordError):
            session.start("slow", url="/?d=0")  # a live recording is never thrown away
        agent.call(session.session_name(), ["close"], timeout=30)
        started = session.start("slow", url="/?d=0")
        self.assertIn("已丢弃", started["notes"][0])

    def test_recording_can_navigate_reload_and_compile_another_origin(self):
        other, origin = serve()
        try:
            session.start("slow", url="/?d=0")
            opened = session.browser(["open", origin + "/?d=0"])
            self.assertIn('button "Go"', opened["snapshot"])
            reloaded = session.browser(["reload"])
            self.assertIn('button "Go"', reloaded["snapshot"])
            session.check("url_contains", None, origin)
            result = session.stop("跨站导航与刷新")
            self.assertEqual(result["ui_case"]["assertions"], 1)
            case = next(c for c in projects.cases("slow") if c["kind"] == "ui")
            for engine in ("playwright", "selenium"):
                driver = (PlaywrightLoadBudgetTests if engine == "playwright" else SeleniumLoadBudgetTests).make_driver(projects.config("slow"))
                try:
                    driver.reset()
                    outcome = ui.run_case(driver, case, projects.config("slow"), Path(self.tmp.name), engine + ".png")
                    self.assertEqual(outcome["status"], "Pass", outcome["reason"])
                finally:
                    driver.close()
        finally:
            other.shutdown()
            other.server_close()


if __name__ == "__main__":
    unittest.main()
