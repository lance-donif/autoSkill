"""Business projections exclude resources while separate original sources retain observed traffic."""
import base64
import copy
import json
import os
import sys
import tempfile
import threading
import unittest
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill import conversation_capture, domain, paths, projects
from autoskill.recorder import capture, har, session, traffic
from autoskill.recorder import compile as compiler


def exchange(path="/items", method="GET", kind="fetch", mime="application/json", *, sent=None, received=None, body=None, status=200):
    request = {"method": method, "url": "https://example.test" + path, "headers": sent or []}
    if body is not None:
        request["postData"] = body
    return {"_resourceType": kind, "request": request,
            "response": {"status": status, "headers": received or [], "content": {"mimeType": mime, "text": '{"ok":true}'}}}


class BusinessTrafficTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": self.tmp.name})
        environment.start()
        self.addCleanup(environment.stop)
        self.pid = projects.create("Business traffic", "https://example.test")["id"]

    def test_resource_destination_beats_filename_and_fetch_preserves_all_response_types(self):
        for kind, mime in (("document", "text/html"), ("stylesheet", "text/css"), ("script", "application/json"),
                           ("image", "image/png"), ("font", "font/woff2"), ("media", "video/mp4")):
            with self.subTest(kind=kind):
                self.assertNotEqual(traffic.category(exchange("/no-extension?cache=1", kind=kind, mime=mime)), "business")
        for path, mime in (("/download.pdf?token=synthetic", "application/pdf"), ("/fragment", "text/html"),
                           ("/report.txt", "text/plain"), ("/result.json", "application/json"), ("/dynamic.css", "text/css")):
            for kind in ("fetch", "xhr"):
                with self.subTest(path=path, kind=kind):
                    self.assertEqual(traffic.category(exchange(path, kind=kind, mime=mime)), "business")
        for path in ("/unknown.json", "/unknown.txt", "/download.pdf"):
            self.assertEqual(traffic.category(exchange(path, kind="", mime="application/octet-stream")), "business")
        self.assertEqual(traffic.category(exchange(kind="", mime="application/octet-stream", sent=[{"name": "Sec-Fetch-Dest", "value": "style"}])), "page_resource")
        source_map = exchange("/bundle.js.map?version=2", kind="other")
        source_map["response"]["content"]["text"] = '{"version":3,"sources":["app.ts"],"mappings":"AAAA"}'
        self.assertEqual(traffic.category(source_map), "page_resource")
        source_map["_resourceType"] = "fetch"
        self.assertEqual(traffic.category(source_map), "business")

    def test_preflight_differs_from_business_options_and_form_navigation_is_preserved(self):
        preflight = exchange(method="OPTIONS", sent=[{"name": "Access-Control-Request-Method", "value": "PATCH"}])
        self.assertEqual(traffic.category(preflight), "cors_preflight")
        self.assertEqual(traffic.category(exchange(method="OPTIONS")), "business")
        self.assertEqual(traffic.category(exchange(method="HEAD")), "business")
        self.assertEqual(traffic.category(exchange(method="HEAD", kind="", mime="text/html")), "business")
        self.assertEqual(traffic.category(exchange("/submit", method="POST", kind="document", mime="text/html")), "business")
        self.assertEqual(traffic.category(exchange("/login", method="POST", kind="document", mime="text/html")), "authentication")
        self.assertEqual(traffic.category(exchange("/events", kind="eventsource", mime="text/event-stream")), "business")

    def test_json_error_for_default_favicon_does_not_turn_it_into_an_api(self):
        favicon = exchange("/favicon.ico?cache=1", kind="Other", status=404)
        favicon["response"]["content"]["text"] = '{"error":"not found"}'
        self.assertEqual(traffic.category(favicon), "page_resource")
        for kind in ("fetch", "xhr"):
            self.assertEqual(traffic.category({**favicon, "_resourceType": kind}), "business")
        for path in ("/api/icons", "/reports/icon-summary.json", "/favicon.json"):
            self.assertEqual(traffic.category(exchange(path, kind="Other", status=404)), "business")
        self.assertEqual(traffic.category(exchange("/favicon.ico", kind="Other", method="POST")), "business")

    def test_redirect_hops_keep_exact_fields_and_scopes_are_explicit_on_import(self):
        first = exchange("/login", method="POST", kind="document", mime="text/html", status=302,
                         body={"mimeType": "application/x-www-form-urlencoded", "text": "username=synthetic&password=fixture"},
                         received=[{"name": "Location", "value": "/welcome"}, {"name": "Set-Cookie", "value": "sid=fixture"}])
        final = exchange("/welcome", kind="document", mime="text/html")
        resource = exchange("/assets?version=2", kind="stylesheet", mime="text/css")
        original = {"log": {"pages": [{"title": "Page"}], "entries": [first, final, resource]}}
        before = copy.deepcopy(original)
        source = self.root / "input.har"
        source.write_text(json.dumps(original), encoding="utf-8")
        summary = conversation_capture.import_source(self.pid, source)
        stored = conversation_capture.get(self.pid, summary["id"])
        archive = json.loads(domain.read_file(self.pid, stored["source"]["path"]))
        self.assertEqual(archive["log"]["entries"], [first, final])
        self.assertNotIn("pages", archive["log"])
        self.assertFalse(archive["log"]["_autoskill_capture_scope"]["complete_network_log"])
        self.assertEqual(stored["capture_scope"]["sources"]["har"]["excluded_by_reason"], {"page_resource": 1})
        self.assertEqual(json.loads(source.read_text()), before)
        self.assertEqual(original, before)

    def test_cdp_redirects_and_websocket_frames_are_scoped_to_their_actual_tab(self):
        navigation = exchange("/redirect", kind="document", mime="text/html", status=302)
        business = exchange("/items")
        unrelated = exchange("/other-tab", kind="document", mime="text/html")
        for entry, target in ((navigation, "one"), (business, "one"), (unrelated, "two")):
            entry["_cdp"] = {"session_id": target, "request_id": "same-id"}
        kept, _scope = traffic.selected([navigation, business, unrelated])
        self.assertEqual(kept, [navigation, business])
        observer = capture.Observer.__new__(capture.Observer)
        observer.requests, observer.targets, observer.entries = {}, {}, []
        for target in ("one", "two"):
            observer._websocket(target, "Network.webSocketCreated", {"requestId": "same-id", "url": "wss://example.test/echo"})
            observer._websocket(target, "Network.webSocketFrameReceived", {
                "requestId": "same-id", "timestamp": 12.5, "response": {"opcode": 1, "mask": False, "payloadData": target}})
        self.assertEqual([entry["_websocket"]["frames"][0]["response"]["payloadData"] for entry in observer.entries], ["one", "two"])

    def test_missing_har_bodies_merge_by_occurrence_without_merging_known_different_bodies(self):
        cdp = [exchange(method="POST", body={"mimeType": "application/json", "text": json.dumps({"value": value})}) for value in ("one", "two")]
        partial = copy.deepcopy(cdp)
        for entry in partial:
            del entry["request"]["postData"]
        joined = conversation_capture.compiler_source({"log": {"entries": partial}}, {"entries": cdp})["log"]["entries"]
        self.assertEqual(len(joined), 2)
        self.assertEqual([json.loads(entry["request"]["postData"]["text"])["value"] for entry in joined], ["one", "two"])
        self.assertTrue(all(len(entry["_source_links"]) == 2 for entry in joined))
        different = exchange(method="POST", body={"mimeType": "application/json", "text": '{"value":"three"}'})
        separate = conversation_capture.compiler_source({"log": {"entries": [different]}}, {"entries": cdp})["log"]["entries"]
        self.assertEqual(len(separate), 3)

    def test_compiler_has_no_thirty_request_cap_and_does_not_invent_upload_or_ws_cases(self):
        requests = [exchange(f"/items/{index}.json") for index in range(35)]
        upload = exchange("/upload", method="POST", body={"mimeType": "multipart/form-data", "params": [{"name": "file", "fileName": "input.txt"}]})
        websocket = exchange("/socket", kind="websocket", status=101)
        websocket["request"]["url"] = "wss://example.test/socket"
        requests.extend([upload, websocket])
        rec = {"start_url": "/", "steps": [{"action": "click", "selector": "#run"}], "secrets": []}
        result = compiler.build(self.pid, rec, {"log": {"entries": requests}}, None, "All requests")
        cases = [case for case in result["candidates"] if case["kind"] == "api"]
        self.assertEqual(len(cases), 35)
        self.assertEqual({case["extensions"]["traffic"] for case in cases}, {"business"})
        self.assertEqual({row["protocol"] for row in result["api_uncompiled"]}, {"http", "websocket"})
        self.assertFalse(any(case["path"] == "/upload" for case in cases))
        normalized, _ = har.entries({"log": {"entries": [exchange(method="OPTIONS"), exchange(method="HEAD")]}}, "https://example.test")
        self.assertEqual([row["method"] for row in normalized], ["OPTIONS", "HEAD"])


PAGE = """<!doctype html><link rel="stylesheet" href="/theme?rev=1"><script src="/bundle?rev=2"></script>
<img src="/pixel?rev=3"><p class="font">Font</p><iframe name="result-frame"></iframe>
<form id="form" action="/form-action" method="post" target="result-frame"><input name="field" value="form-value"></form>
<button id="run">Run</button><p id="done">ready</p><script>
document.querySelector('#run').onclick = async () => {
 try {
  const headers = {'Content-Type':'application/json','Authorization':'Bearer synthetic-traffic'};
  await fetch('/items?tag=a&tag=b');
  for (const method of ['POST','PUT','PATCH','DELETE']) await fetch('/items', {method,headers,body:JSON.stringify({name:'business-value',method})});
  await fetch('/metadata',{method:'HEAD'});
  await fetch('/capabilities',{method:'OPTIONS'});
  await fetch('/download.pdf?ticket=synthetic').then(r=>r.arrayBuffer());
  await fetch('/snippet').then(r=>r.text());
  await fetch('/plain').then(r=>r.text());
  await fetch('/graphql',{method:'POST',headers,body:JSON.stringify({query:'query { items { id } }'})});
  const form = new FormData(); form.append('description','upload-value'); form.append('file',new Blob(['synthetic-file-bytes']),'fixture.txt');
  await fetch('/upload',{method:'POST',body:form});
  await fetch('CROSS_ORIGIN/cors',{method:'POST',headers,body:JSON.stringify({cors:'business'})});
  await fetch('/auth/start').then(r=>r.text());
  await fetch('/events').then(r=>r.text());
  await new Promise((resolve,reject)=>{ const events=new EventSource('/event-source'); events.onmessage=()=>{events.close();resolve()};events.onerror=reject; });
  await new Promise((resolve,reject)=>{ const socket=new WebSocket('WS_ORIGIN/echo'); let messages=0; socket.onopen=()=>socket.send('synthetic-ws-frame');
    socket.onmessage=()=>{if(++messages===2)socket.close()};socket.onclose=resolve;socket.onerror=reject; });
  await new Promise(resolve=>{document.querySelector('iframe').onload=resolve;document.querySelector('#form').submit()});
  document.querySelector('#done').textContent='complete';
 } catch(error) {document.querySelector('#done').textContent='error '+error;}
};</script>"""


class TrafficHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def respond(self, status=200, mime="application/json", data=b'{"ok":true}', extra=None):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin", "*"))
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "content-type,authorization")
        for name, value in extra or []:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def do_GET(self):
        self.server.seen.append((self.command, self.path, b""))
        path = urlsplit(self.path).path
        if path == "/":
            self.respond(mime="text/html", data=self.server.page.encode(), extra=[("Set-Cookie", "fixture=browser; Path=/")])
        elif path == "/theme":
            self.respond(mime="text/css", data=b'@font-face{font-family:Fixture;src:url(/font?rev=4)}.font{font-family:Fixture}')
        elif path == "/bundle":
            self.respond(mime="application/javascript", data=b"window.fixtureLoaded=true;")
        elif path == "/pixel":
            self.respond(mime="image/svg+xml", data=b'<svg xmlns="http://www.w3.org/2000/svg"/>')
        elif path == "/font":
            self.respond(mime="font/woff2", data=b"invalid-font-fixture")
        elif path in ("/snippet", "/auth/finish"):
            self.respond(mime="text/html", data=b"<p>business-html</p>")
        elif path == "/auth/start":
            self.respond(302, extra=[("Location", "/auth/finish"), ("Set-Cookie", "auth-fixture=observed; Path=/")])
        elif path == "/plain":
            self.respond(mime="text/plain", data=b"business-text")
        elif path == "/download.pdf":
            self.respond(mime="application/pdf", data=b"%PDF-synthetic-business-data")
        elif path in ("/events", "/event-source"):
            self.respond(mime="text/event-stream", data=b'data: {"answer":"stream-value"}\n\n')
        else:
            self.respond()

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.seen.append((self.command, self.path, body))
        self.respond(mime="text/html" if self.path == "/form-action" else "application/json")

    def do_PUT(self):
        self.do_POST()

    def do_PATCH(self):
        self.do_POST()

    def do_DELETE(self):
        self.do_POST()

    def do_HEAD(self):
        self.server.seen.append((self.command, self.path, b""))
        self.respond()

    def do_OPTIONS(self):
        self.server.seen.append((self.command, self.path, b""))
        self.respond(204, data=b"")


@unittest.skipUnless(paths.agent_browser() and paths.chrome(), "requires the local recording browser")
class BusinessTrafficBrowserTests(unittest.TestCase):
    def test_real_favicon_json_404_remains_archived_without_becoming_a_candidate(self):
        class FaviconSite(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_GET(self):
                path = urlsplit(self.path).path
                status, mime = (404 if path == "/favicon.ico" else 200), "application/json"
                body = b'{"error":"not found"}' if status == 404 else b'{"ok":true}'
                if path == "/":
                    mime = "text/html"
                    body = b'''<!doctype html><link rel="icon" href="/favicon.ico"><button id="check">Check</button><p id="result">ready</p>
                    <script>document.querySelector('#check').onclick=async()=>{await fetch('/api/icons');await fetch('/reports/icon-summary.json');
                    document.querySelector('#result').textContent='done';};</script>'''
                self.send_response(status)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
                "AUTOSKILL_WORKSPACE": folder, "AUTOSKILL_BRIDGE": "off", "AUTOSKILL_HEADED": "0", "AUTOSKILL_SESSION": "favicon-fixture"}):
            site = ThreadingHTTPServer(("127.0.0.1", 0), FaviconSite)
            thread = threading.Thread(target=site.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
            thread.start()
            try:
                pid = projects.create("Favicon capture", f"http://127.0.0.1:{site.server_port}")["id"]
                session.start(pid, login=False)
                session.browser(["click", "#check"])
                self.assertTrue(session.check("text", "#result", "done", timeout_seconds=5)["passed"])
                with patch("autoskill.server.jobs.ensure_worker"):
                    result = session.stop("读取真实图标业务接口")
                prefix = f"conversations/{result['conversation_captures']['id']}/"
                har_source = json.loads(domain.read_file(pid, prefix + "source.original.har"))
                cdp_source = json.loads(domain.read_file(pid, prefix + "source.original.cdp.json"))
                for rows in (har_source["log"]["entries"], cdp_source["entries"]):
                    icons = [entry for entry in rows if urlsplit(entry["request"]["url"]).path == "/favicon.ico"]
                    self.assertTrue(icons)
                    self.assertTrue(all(entry["response"]["status"] == 404 and traffic.category(entry) == "page_resource" for entry in icons))
                self.assertEqual({case["path"] for case in result["candidates"] if case["kind"] == "api"},
                                 {"/api/icons", "/reports/icon-summary.json"})
            finally:
                session.cancel()
                site.shutdown()
                site.server_close()
                thread.join(timeout=3)

    def test_real_browser_filters_archives_and_compiles_crud_without_reissuing_business_requests(self):
        from websockets.sync.server import serve

        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
                "AUTOSKILL_WORKSPACE": folder, "AUTOSKILL_HEADED": "0", "AUTOSKILL_SESSION": "business-filter-fixture"}):
            def echo(socket):
                socket.send(socket.recv())
                socket.send(b"\x00\xffbinary-frame")

            socket_server = serve(echo, "127.0.0.1", 0)
            socket_thread = threading.Thread(target=socket_server.serve_forever, daemon=True)
            socket_thread.start()
            site, cross = (ThreadingHTTPServer(("127.0.0.1", 0), TrafficHandler) for _ in range(2))
            site.seen, cross.seen = [], []
            base, other = f"http://127.0.0.1:{site.server_port}", f"http://127.0.0.1:{cross.server_port}"
            site.page = PAGE.replace("CROSS_ORIGIN", other).replace("WS_ORIGIN", f"ws://127.0.0.1:{socket_server.socket.getsockname()[1]}")
            threads = [threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True) for server in (site, cross)]
            for thread in threads:
                thread.start()
            try:
                pid = projects.create("Business protocols", base)["id"]
                session.start(pid, login=False)
                session.browser(["click", "#run"])
                self.assertTrue(session.check("text", "#done", "complete", timeout_seconds=15)["passed"])
                with patch("autoskill.server.jobs.ensure_worker"):
                    result = session.stop("CRUD and protocol capture")
                saved = conversation_capture.get(pid, result["conversation_captures"]["id"])
                calls = saved["requests"]
                urls = [urlsplit(call["request"]["url"]).path for call in calls]
                wanted = {"/items", "/metadata", "/capabilities", "/download.pdf", "/snippet", "/plain", "/graphql", "/upload",
                          "/cors", "/events", "/event-source", "/echo", "/form-action", "/auth/start", "/auth/finish"}
                self.assertTrue(wanted <= set(urls), wanted - set(urls))
                self.assertFalse(set(urls) & {"/", "/theme", "/bundle", "/pixel", "/font"})
                self.assertEqual(sum(call["request"]["method"] == "OPTIONS" for call in calls), 1)
                self.assertEqual({call["request"]["method"] for call in calls if urlsplit(call["request"]["url"]).path == "/items"}, {"GET", "POST", "PUT", "PATCH", "DELETE"})
                for call in calls:
                    if urlsplit(call["request"]["url"]).path == "/items" and call["request"]["method"] != "GET":
                        self.assertEqual(call["body_json"], {"name": "business-value", "method": call["request"]["method"]})
                        self.assertEqual(traffic.headers(call["request"]["headers"])["authorization"], "Bearer synthetic-traffic")
                self.assertEqual(sum(path == "/upload" for path in urls), 1)
                upload = next(call for call in calls if urlsplit(call["request"]["url"]).path == "/upload")
                self.assertIn("upload-value", json.dumps(upload["request"]))
                self.assertIsNot(upload["request_body_complete"], True)
                websocket = next(call for call in calls if call["protocol"] == "websocket")
                self.assertEqual(websocket["transport_status"], 101)
                self.assertTrue({"sent", "received"} <= {frame["direction"] for frame in websocket["websocket"]["frames"]})
                self.assertTrue(all("timestamp" in frame for frame in websocket["websocket"]["frames"]))
                binary = next(frame["response"]["payloadData"] for frame in websocket["websocket"]["frames"] if frame["response"]["opcode"] == 2)
                self.assertEqual(base64.b64decode(binary), b"\x00\xffbinary-frame")
                self.assertEqual(sum(call["protocol"] == "sse" for call in calls), 2)
                self.assertTrue(all("stream-value" in call["response"]["content"].get("text", "") for call in calls if call["protocol"] == "sse"))
                for ref in saved["sources"]:
                    data = domain.read_file(pid, ref["path"])
                    self.assertEqual(domain.content_hash(data), ref["sha256"])
                    if "/source.original." in ref["path"]:
                        continue
                    self.assertNotIn(b"/theme?rev=1", data)
                    self.assertNotIn(b"/bundle?rev=2", data)
                    self.assertNotIn(b"/pixel?rev=3", data)
                self.assertFalse(saved["capture_scope"]["complete_network_log"])
                self.assertGreater(saved["capture_scope"]["sources"]["cdp"]["excluded_entries"], 0)
                cases = [case for case in result["candidates"] if case["kind"] == "api"]
                self.assertTrue({"/download.pdf", "/snippet", "/capabilities", "/form-action"} <= {case["path"] for case in cases})
                self.assertFalse({"/", "/theme", "/bundle", "/pixel", "/font", "/upload", "/echo"} & {case["path"] for case in cases})
                self.assertEqual({row["protocol"] for row in result["api_uncompiled"]}, {"http", "websocket"})
                self.assertEqual(Counter(method for method, path, _body in site.seen if urlsplit(path).path == "/items"), Counter({"GET": 1, "POST": 1, "PUT": 1, "PATCH": 1, "DELETE": 1}))
                self.assertEqual(Counter(method for method, _path, _body in cross.seen), Counter({"OPTIONS": 1, "POST": 1}))
                self.assertFalse(list(Path(folder).rglob("recording.har")))
                self.assertFalse(list(session.recording_dir().glob("network-*")))
            finally:
                session.cancel()
                for server in (site, cross):
                    server.shutdown()
                    server.server_close()
                for thread in threads:
                    thread.join(timeout=3)
                socket_server.shutdown()
                socket_thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
