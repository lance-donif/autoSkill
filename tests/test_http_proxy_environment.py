"""DSH proxy bypass literals work for actual synchronous and AI HTTP clients."""
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill import paths
from autoskill.engines.ai_eval import AiEvaluationAdapter
from autoskill.replay import api


class ProxyTarget(BaseHTTPRequestHandler):
    def respond(self):
        self.server.requests.append(self.path)
        self.server.credentials.append(self.headers.get("Proxy-Authorization"))
        data = b'{"answer":"ready"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.respond()

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.respond()

    def log_message(self, *_args):
        pass


class HttpProxyEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ProxyTarget)
        self.server.requests, self.server.credentials = [], []
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        proxy = f"http://fixture-user:fixture-pass@127.0.0.1:{self.server.server_port}"
        self.environment = {"NO_PROXY": "localhost,127.0.0.1,::1,[::1]", "no_proxy": "localhost,127.0.0.1,::1,[::1]",
                            **dict.fromkeys(("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"), proxy)}

    def test_sync_http_and_ai_conversations_accept_the_actual_dsh_bypass_environment(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {**self.environment, "AUTOSKILL_WORKSPACE": folder}):
            with api.new_client({"timeout_seconds": 5}) as client:
                self.assertEqual(client.get(self.origin + "/sync").json(), {"answer": "ready"})
            adapter = AiEvaluationAdapter()
            for number, url in enumerate((self.origin + "/conversation", "http://not-resolvable.invalid/conversation")):
                spec = {"endpoint": {"url": url, "body": {"question": "{{question}}"}},
                        "samples": [{"id": "ready", "turns": [{"question": "service readiness", "expected": "ready"}]}]}
                source = Path(folder) / "spec.json"
                source.write_text(json.dumps(spec))
                # Restore the exact DSH spelling before each independent AI client.
                os.environ.update(self.environment)
                prepared = adapter.prepare({"id": "proxy-check", "argv": ["spec.json"], "cwd": "."}, Path(folder),
                                           Path(folder) / ("evidence-" + str(number)), {})
                result = adapter.execute(prepared)
                self.assertEqual((result["status"], result["business_status"]), ("Pass", "Pass"), result)
            self.assertIn("/sync", self.server.requests)
            self.assertIn("/conversation", self.server.requests)
            self.assertIn("http://not-resolvable.invalid/conversation", self.server.requests)
            self.assertTrue(any(value and value.startswith("Basic ") for value in self.server.credentials))
            self.assertEqual(os.environ["HTTP_PROXY"], self.environment["HTTP_PROXY"])

    def test_ipv6_normalization_keeps_business_domain_port_and_scheme_scope(self):
        bypass = ".business.invalid,api.exact.invalid:8443,[2001:db8::2]:8443,[::1],http://scheme.invalid:8080,[literal-business-name]"
        with patch.dict(os.environ, {**self.environment, "NO_PROXY": "ignored-upper.invalid", "no_proxy": bypass}):
            paths.configure_http_environment()
            self.assertIn("[literal-business-name]", os.environ["no_proxy"])
            # A non-address bracketed name remains an invalid user entry, rather than being rewritten.
            os.environ["no_proxy"] = os.environ["NO_PROXY"] = os.environ["no_proxy"].replace(",[literal-business-name]", "")
            with httpx.Client() as client:
                for url, direct in (("http://api.business.invalid", True), ("http://business.invalid", False),
                                    ("https://api.exact.invalid:8443", True), ("https://api.exact.invalid:8444", False),
                                    ("http://[2001:db8::2]:8443", True), ("http://[2001:db8::2]:8444", False),
                                    ("http://[::1]:9321", True), ("http://scheme.invalid:8080", True),
                                    ("https://scheme.invalid:8080", False)):
                    with self.subTest(url=url):
                        self.assertEqual(client._transport_for_url(httpx.URL(url)) is client._transport, direct)

    def test_explicit_empty_lowercase_bypass_does_not_inherit_uppercase_business_domains(self):
        with patch.dict(os.environ, {**self.environment, "NO_PROXY": "business.invalid", "no_proxy": ""}):
            paths.configure_http_environment()
            self.assertNotIn("business.invalid", os.environ["no_proxy"])
            self.assertEqual(os.environ["NO_PROXY"], os.environ["no_proxy"])


if __name__ == "__main__":
    unittest.main()
