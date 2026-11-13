"""Two-origin browser fixture with mixed frame/shadow scopes and root decoys."""
from __future__ import annotations

import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

CONTEXT = [
    {"kind": "shadow", "locators": [{"by": "css", "value": "#top-shell"}]},
    {"kind": "frame", "locators": [{"by": "css", "value": "#remote-frame"}]},
    {"kind": "shadow", "locators": [{"by": "css", "value": "#middle-shell"}]},
    {"kind": "frame", "locators": [{"by": "css", "value": "#inner-frame"}]},
    {"kind": "shadow", "locators": [{"by": "css", "value": "#leaf-shell"}]},
]


class LocatorSite:
    """Serve two actual HTTP origins without launching a browser or touching user data."""

    def __enter__(self):
        site = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_GET(self):
                parsed = urlsplit(self.path)
                if parsed.path == "/hit":
                    query = parse_qs(parsed.query)
                    item = {name: query.get(name, [""])[0] for name in ("where", "value")}
                    with site.lock:
                        site._hits.append(item)
                    body, content_type = json.dumps({"saved": True}), "application/json"
                else:
                    body, content_type = site.document(parsed.path), "text/html; charset=utf-8"
                raw = body.encode()
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(raw)

        self.lock, self._hits = threading.Lock(), []
        self.servers = [ThreadingHTTPServer(("127.0.0.1", 0), Handler) for _ in range(2)]
        self.base_url, self.other_url = [f"http://127.0.0.1:{server.server_port}" for server in self.servers]
        self.threads = []
        for server in self.servers:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
            thread.start()
            self.threads.append(thread)
        return self

    def __exit__(self, *_args):
        for server in self.servers:
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join(timeout=2)

    @property
    def context(self):
        return copy.deepcopy(CONTEXT)

    @property
    def hits(self):
        with self.lock:
            return copy.deepcopy(self._hits)

    def config(self, timeout_seconds=2):
        return {"base_url": self.base_url, "allowed_origins": [self.base_url, self.other_url],
                "timeout_seconds": timeout_seconds, "page_load_seconds": 10}

    @staticmethod
    def form():
        return """<section id="content"><label for="name">项目名称</label>
<input id="name" data-testid="name" placeholder="输入项目名称" value="原名称">
<button id="save" data-testid="save">保存</button><p id="result" data-testid="result">尚未保存</p>
<p id="present" data-testid="present">一直可见</p>
<button id="quoted">包含 &quot;双引号&quot; 和 '单引号'</button>
<button class="duplicate">同名</button><button class="duplicate">同名</button></section>"""

    def wire(self, scope, where):
        return """(function(root) {
  root.querySelector('#save').onclick = async function() {
    const value = root.querySelector('#name').value;
    const response = await fetch(BASE + '/hit?where=' + WHERE + '&value=' + encodeURIComponent(value));
    if ((await response.json()).saved) root.querySelector('#result').textContent = '保存成功：' + value;
  };
})(SCOPE);""".replace("BASE", json.dumps(self.base_url)).replace("WHERE", json.dumps(where)).replace("SCOPE", scope)

    @staticmethod
    def mount(host, content):
        return ("document.getElementById(" + json.dumps(host) + ").attachShadow({mode:'open'}).innerHTML="
                + json.dumps(content, ensure_ascii=False) + ";")

    def document(self, path):
        if path == "/outer":
            content = self.form() + '<div id="middle-shell"></div>'
            inner = '<section id="frame-anchor"><iframe id="inner-frame" title="内层" src="' + self.base_url + '/inner"></iframe></section>'
            script = self.wire("document", "outer-decoy") + self.mount("middle-shell", inner)
        elif path == "/inner":
            content = self.form() + '<div id="leaf-shell"></div>'
            script = (self.wire("document", "inner-decoy") + self.mount("leaf-shell", self.form())
                      + self.wire("document.getElementById('leaf-shell').shadowRoot", "leaf"))
        elif path == "/decoy":
            content, script = self.form(), self.wire("document", "sibling-decoy")
        elif path == "/plain":
            content, script = self.form(), self.wire("document", "plain")
        else:
            content = (self.form() + '<div id="top-shell"></div><div id="closed-shell"></div>'
                       + '<iframe id="sibling-frame" title="同名干扰" src="' + self.other_url + '/decoy"></iframe>')
            frame = '<section id="frame-anchor"><iframe id="remote-frame" title="跨域" src="' + self.other_url + '/outer"></iframe></section>'
            script = (self.wire("document", "root-decoy") + self.mount("top-shell", frame)
                      + "document.getElementById('closed-shell').attachShadow({mode:'closed'}).innerHTML='<button>不可访问</button>';")
        return ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>定位上下文验收</title><body>'
                + content + '<script>' + script + '</script></body></html>')

    def automation(self, value="新的项目", *, mixed=True, force_xpath=False):
        """Return fresh IR; server-side hits independently identify the scope actually used."""
        def target(name, tag):
            primary = {"by": "testid", "value": "missing-" + name if force_xpath else name}
            xpath = {"by": "xpath", "value": ".//" + tag + "[@id='" + name + "']", "xpath_anchor": "#content"}
            return [primary, xpath] if force_xpath else [primary, {"by": "css", "value": "#" + name}, xpath]

        def scoped(step):
            return {**step, "context": self.context} if mixed else step

        return {"start_url": "/mixed" if mixed else "/plain", "actions": [
            scoped({"action": "fill", "locators": target("name", "input"), "value": value}),
            scoped({"action": "click", "locators": target("save", "button")}),
        ], "assertions": [
            scoped({"assertion": "text", "locators": target("result", "p"), "expected": "保存成功：" + value}),
        ]}
