"""Telemetry stays in original recorder sources while only business candidates become API cases."""
import copy
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill import conversation_capture, domain, paths, projects
from autoskill.recorder import capture, session, traffic
from autoskill.recorder import compile as compiler


def exchange(path, *, kind="fetch", method="POST", mime="application/json"):
    return {"_resourceType": kind, "request": {"method": method, "url": "https://fixture.invalid" + path,
            "headers": [{"name": "Content-Type", "value": "application/json"}],
            **({"postData": {"mimeType": "application/json", "text": '{"action":"check"}'}} if method == "POST" else {})},
            "response": {"status": 200, "headers": [], "content": {"mimeType": mime, "text": '{"ok":true}'}}}


class TelemetryTrafficTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.root / "data")})
        environment.start()
        self.addCleanup(environment.stop)
        self.pid = projects.create("业务与遥测分类", "https://fixture.invalid")["id"]

    def test_exact_telemetry_and_beacons_do_not_suppress_tracking_business_or_wordpress(self):
        cloudflare = exchange("/cdn-cgi/rum?sample=1")
        analytics = exchange("/g/collect")
        analytics["request"]["url"] = "https://analytics.google.com/g/collect?v=2"
        beacon = exchange("/unclassified-beacon", kind="ping")
        business = exchange("/api/track-orders")
        wordpress = exchange("/wp-admin/admin-ajax.php", kind="xhr", mime="text/html")
        resource = exchange("/bundle", kind="script", method="GET", mime="application/javascript")
        navigation = exchange("/page", kind="document", method="GET", mime="text/html")
        entries = [cloudflare, analytics, beacon, business, wordpress, resource, navigation]
        before = copy.deepcopy(entries)
        self.assertEqual([traffic.category(entry) for entry in entries],
                         ["telemetry", "telemetry", "unclassified_beacon", "business", "business", "page_resource", "page_navigation"])
        rec = {"start_url": "/page", "steps": [{"action": "click", "selector": "#check"}], "secrets": []}
        result = compiler.build(self.pid, rec, {"log": {"entries": entries}}, None, "检查订单功能")
        cases = [case for case in result["candidates"] if case["kind"] == "api"]
        self.assertEqual({case["path"] for case in cases}, {"/api/track-orders", "/wp-admin/admin-ajax.php"})
        self.assertEqual(result["api_compile_scope"]["excluded_by_reason"],
                         {"telemetry": 2, "unclassified_beacon": 1, "page_resource": 1, "page_navigation": 1})
        self.assertEqual((result["api_compile_scope"]["business_candidates"], result["api_compile_scope"]["generated_api_cases"]), (2, 2))
        self.assertTrue(all(case["extensions"]["oracle"]["source"] == "observation" for case in cases))
        self.assertEqual(entries, before)

    def test_telemetry_only_recording_still_archives_original_har_and_cdp_bytes(self):
        entries = [exchange("/cdn-cgi/rum"), exchange("/bundle", kind="script", method="GET")]
        for number, entry in enumerate(entries):
            entry["_cdp"] = {"session_id": "tab", "request_id": str(number)}
        har = {"log": {"pages": [{"title": "原始页面"}], "entries": entries}}
        source = (json.dumps(har, ensure_ascii=False, indent=3) + "\n").encode()
        events = b'{"method":"Network.requestWillBeSent","sessionId":"tab","params":{"requestId":"0"}}\n'
        observer = capture.Observer.__new__(capture.Observer)
        observer.folder, observer.entries, observer.bodies = self.root, entries, {}
        observer.token, observer.ready = "fixture", True
        (self.root / "events.jsonl").write_bytes(events)
        evidence = observer.finish()
        self.assertEqual((self.root / "events.jsonl").read_bytes(), events)
        self.assertEqual(evidence["entries"], [])
        result = conversation_capture.capture(self.pid, {"conversation_evidence": evidence}, har, source_har=source)
        self.assertEqual(result["requests"], 0)
        prefix = f"conversations/{result['id']}/"
        self.assertEqual(domain.read_file(self.pid, prefix + "source.original.har"), source)
        self.assertEqual(domain.read_file(self.pid, prefix + "source.original.cdp.jsonl"), events)
        self.assertEqual(json.loads(domain.read_file(self.pid, prefix + "source.original.cdp.json"))["entries"], entries)
        self.assertEqual(json.loads(domain.read_file(self.pid, prefix + "source.har"))["log"]["entries"], [])


PAGE = """<!doctype html><link rel="stylesheet" href="/style"><script src="/bundle"></script><img src="/image">
<button id="check">检查订单</button><p id="result">等待操作</p><script>
document.querySelector('#check').onclick=async()=>{
 navigator.sendBeacon('/cdn-cgi/rum?local=1',new Blob([JSON.stringify({eventType:1,startTime:42})],{type:'application/json'}));
 navigator.sendBeacon('/unclassified-beacon',new Blob([JSON.stringify({metric:'fixture'})],{type:'application/json'}));
 if(location.pathname!='/telemetry-only') {
   const order=await fetch('/api/track-orders',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({order:'fixture'})}).then(r=>r.json());
   await fetch('/wp-admin/admin-ajax.php',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded','X-Requested-With':'XMLHttpRequest'},body:'action=pinglun_zan&postid=fixture'});
   document.querySelector('#result').textContent=order.ok?'订单已查到':'业务失败';
 }else document.querySelector('#result').textContent='遥测已发送';
};</script>"""


class TelemetrySite(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def respond(self, mime, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path in ("/", "/telemetry-only"):
            self.respond("text/html; charset=utf-8", PAGE.encode())
        elif path == "/style":
            self.respond("text/css", b"body{margin:24px}")
        elif path == "/bundle":
            self.respond("application/javascript", b"window.pageLoaded=true;")
        else:
            self.respond("image/svg+xml", b'<svg xmlns="http://www.w3.org/2000/svg"/>')

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.server.requests.append((urlsplit(self.path).path, body))
        self.respond("application/json", b'{"ok":true}')


@unittest.skipUnless(paths.agent_browser() and paths.chrome(), "requires installed Chromium and recorder")
class TelemetryBrowserTests(unittest.TestCase):
    def test_real_page_beacons_and_resources_remain_sources_but_never_become_business_cases(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                "AUTOSKILL_WORKSPACE": directory, "AUTOSKILL_HEADED": "0", "AUTOSKILL_BRIDGE": "off", "AUTOSKILL_SESSION": "telemetry-browser"}):
            site = ThreadingHTTPServer(("127.0.0.1", 0), TelemetrySite)
            site.requests = []
            thread = threading.Thread(target=site.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
            thread.start()
            self.addCleanup(site.server_close)
            self.addCleanup(site.shutdown)
            original_capture = conversation_capture.capture
            original_bytes = []

            def retain_source(*args, **kwargs):
                original_bytes.append(kwargs.get("source_har"))
                return original_capture(*args, **kwargs)

            try:
                for name, path, expected in (("business", "/", "订单已查到"), ("telemetry", "/telemetry-only", "遥测已发送")):
                    with self.subTest(recording=name):
                        pid = projects.create("本机来源分类", f"http://127.0.0.1:{site.server_port}" + path, project_id=name)["id"]
                        session.start(pid, login=False)
                        session.browser(["click", "#check"])
                        self.assertTrue(session.check("text", "#result", expected, timeout_seconds=5)["passed"])
                        with patch.object(conversation_capture, "capture", side_effect=retain_source), patch("autoskill.server.jobs.ensure_worker"):
                            result = session.stop("核对订单查询结果" if name == "business" else "页面遥测观察")
                        prefix = f"conversations/{result['conversation_captures']['id']}/"
                        self.assertIsInstance(original_bytes[-1], bytes)
                        source = domain.read_file(pid, prefix + "source.original.har")
                        self.assertEqual(source, original_bytes[-1])
                        raw_paths = {urlsplit(row["request"]["url"]).path for row in json.loads(source)["log"]["entries"]}
                        self.assertTrue({"/cdn-cgi/rum", "/unclassified-beacon", "/style", "/bundle"} <= raw_paths, raw_paths)
                        raw_cdp = json.loads(domain.read_file(pid, prefix + "source.original.cdp.json"))
                        cdp_paths = {urlsplit(row["request"]["url"]).path for row in raw_cdp["entries"]}
                        self.assertTrue({"/cdn-cgi/rum", "/unclassified-beacon", "/style", "/bundle"} <= cdp_paths, cdp_paths)
                        business_paths = {case["path"] for case in result["candidates"] if case["kind"] == "api"}
                        self.assertEqual(business_paths, {"/api/track-orders", "/wp-admin/admin-ajax.php"} if name == "business" else set())
                        self.assertEqual(len(result["api_cases"]), 2 if name == "business" else 0)
                        self.assertGreater(result["api_compile_scope"]["excluded_by_reason"]["telemetry"], 0)
                        self.assertGreater(result["api_compile_scope"]["excluded_by_reason"]["unclassified_beacon"], 0)
                self.assertEqual([path for path, _body in site.requests].count("/api/track-orders"), 1)
                self.assertEqual([path for path, _body in site.requests].count("/wp-admin/admin-ajax.php"), 1)
                self.assertEqual([path for path, _body in site.requests].count("/cdn-cgi/rum"), 2)
            finally:
                session.cancel()
