"""The bundled demo system must behave like a real authenticated app."""
import importlib.util
import json
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1] / "skills/autoskill/examples/ticket-desk/server.py"
spec = importlib.util.spec_from_file_location("ticket_desk", SERVER)
ticket_desk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ticket_desk)


class TicketDeskTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ticket_desk.make_server(0)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.call("POST", "/api/test/reset")

    def call(self, method, path, body=None, token=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if data:
            request.add_header("Content-Type", "application/json")
        if token:
            request.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.loads(error.read())

    def login(self, username="qa_admin", password="Passw0rd!demo"):
        status, body = self.call("POST", "/api/login", {"username": username, "password": password})
        self.assertEqual(status, 200)
        return body["token"]

    def test_business_api_requires_login(self):
        self.assertEqual(self.call("GET", "/api/health")[0], 200)
        self.assertEqual(self.call("GET", "/api/tickets"), (401, {"code": "UNAUTHENTICATED", "message": "请先登录"}))

    def test_wrong_password_is_rejected(self):
        status, body = self.call("POST", "/api/login", {"username": "qa_admin", "password": "wrong"})
        self.assertEqual((status, body["code"]), (401, "INVALID_CREDENTIALS"))

    def test_create_then_resolve_then_filter(self):
        token = self.login()
        self.assertEqual(self.call("GET", "/api/tickets", token=token)[1]["total"], 3)
        status, ticket = self.call("POST", "/api/tickets", {"title": "新工单", "priority": "high"}, token)
        self.assertEqual((status, ticket["status"], ticket["priority"]), (201, "open", "high"))
        status, changed = self.call("PATCH", "/api/tickets/" + ticket["id"], {"status": "resolved"}, token)
        self.assertEqual((status, changed["status"]), (200, "resolved"))
        listed = self.call("GET", "/api/tickets?status=resolved", token=token)[1]
        self.assertIn(ticket["id"], [item["id"] for item in listed["items"]])

    def test_validation_and_role_checks(self):
        token = self.login()
        self.assertEqual(self.call("POST", "/api/tickets", {"title": "  "}, token)[1]["fields"], {"title": "required"})
        self.assertEqual(self.call("POST", "/api/tickets", {"title": "x" * 81}, token)[1]["fields"], {"title": "too_long"})
        viewer = self.login("qa_viewer", "Passw0rd!view")
        self.assertEqual(self.call("POST", "/api/tickets", {"title": "x"}, viewer)[0], 403)

    def test_expired_session_is_rejected(self):
        token = self.login()
        self.call("POST", "/api/test/expire-sessions")
        self.assertEqual(self.call("GET", "/api/me", token=token)[0], 401)

    def test_static_files_cannot_escape_web_root(self):
        self.assertEqual(self.call("GET", "/../server.py")[0], 404)


if __name__ == "__main__":
    unittest.main()
