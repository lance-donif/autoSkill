"""Ticket Desk: a small, dependency-free demo system used to exercise autoSkill.

It has what a real back-office app has and TodoMVC lacks: a login, a session,
role checks and a JSON API behind the pages. All data is synthetic and lives in
memory; restarting the server restores the seed data.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import threading
import time
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

WEB = Path(__file__).resolve().parent / "web"
COOKIE = "td_session"
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
}
# Synthetic accounts for the demo only; they are documented in README.md.
USERS = {
    "qa_admin": {"password": "Passw0rd!demo", "id": "U-1", "name": "测试管理员", "role": "admin"},
    "qa_viewer": {"password": "Passw0rd!view", "id": "U-2", "name": "只读访客", "role": "viewer"},
}
PRIORITIES = ("low", "medium", "high")
STATUSES = ("open", "in_progress", "resolved")
SEED = (
    ("登录页在窄屏下按钮被遮挡", "high", "open"),
    ("导出报表缺少合计行", "medium", "in_progress"),
    ("帮助文档链接失效", "low", "resolved"),
)


class State:
    """All mutable demo data behind one lock."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.sessions: dict[str, str] = {}
        self.tickets: list[dict] = []
        self.next_id = 1001
        self.bug = False
        self.reset()

    def reset(self) -> None:
        with self.lock:
            self.sessions.clear()
            self.bug = False
            self.tickets = []
            self.next_id = 1001
            for title, priority, status in SEED:
                self._add(title, priority, "", "U-1", status)

    def _add(self, title: str, priority: str, description: str, user_id: str, status: str = "open") -> dict:
        ticket = {
            "id": f"T-{self.next_id}",
            "title": title,
            "priority": priority,
            "status": status,
            "description": description,
            "created_by": user_id,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        self.next_id += 1
        self.tickets.insert(0, ticket)
        return ticket

    def login(self, username: str, password: str) -> tuple[str, dict] | None:
        user = USERS.get(username)
        if not user or not secrets.compare_digest(user["password"], password):
            return None
        token = secrets.token_urlsafe(24)
        with self.lock:
            self.sessions[token] = username
        return token, public_user(username)

    def user_for(self, token: str | None) -> str | None:
        with self.lock:
            return self.sessions.get(token or "")

    def logout(self, token: str | None) -> None:
        with self.lock:
            self.sessions.pop(token or "", None)

    def expire_all(self) -> int:
        with self.lock:
            count = len(self.sessions)
            self.sessions.clear()
        return count


def public_user(username: str) -> dict:
    user = USERS[username]
    return {"id": user["id"], "username": username, "name": user["name"], "role": user["role"]}


def validate_ticket(body: object) -> tuple[dict, dict]:
    """Return (clean values, field errors)."""
    errors: dict[str, str] = {}
    if not isinstance(body, dict):
        return {}, {"body": "must be a JSON object"}
    title = body.get("title")
    title = title.strip() if isinstance(title, str) else ""
    if not title:
        errors["title"] = "required"
    elif len(title) > 80:
        errors["title"] = "too_long"
    priority = body.get("priority", "medium")
    if priority not in PRIORITIES:
        errors["priority"] = "invalid"
    description = body.get("description", "")
    if not isinstance(description, str) or len(description) > 2000:
        errors["description"] = "invalid"
    return {"title": title, "priority": priority, "description": description}, errors


class Handler(BaseHTTPRequestHandler):
    server_version = "TicketDesk/1.0"
    state: State  # set by make_server

    def log_message(self, *args) -> None:  # keep test output quiet
        pass

    # --- helpers -----------------------------------------------------
    def send_json(self, status: int, payload: object, headers: dict | None = None) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def error(self, status: int, code: str, **extra) -> None:
        self.send_json(status, {"code": code, **extra})

    def read_body(self) -> None:
        """Always consume the request body: answering with unread bytes resets the connection on Windows."""
        length = int(self.headers.get("Content-Length") or 0)
        if length > 64 * 1024:
            self.close_connection = True
            raise ValueError("body too large")
        self.body = self.rfile.read(length) if length else b""

    def read_json(self) -> object:
        return json.loads(self.body.decode("utf-8")) if self.body else {}

    def token(self) -> str | None:
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            return header[7:].strip()
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        return cookie[COOKIE].value if COOKIE in cookie else None

    def current_user(self) -> dict | None:
        username = self.state.user_for(self.token())
        return public_user(username) if username else None

    # --- routing -----------------------------------------------------
    def do_GET(self) -> None:
        self.route()

    def do_HEAD(self) -> None:
        self.route()

    def do_POST(self) -> None:
        self.route()

    def do_PATCH(self) -> None:
        self.route()

    def route(self) -> None:
        url = urlsplit(self.path)
        path = url.path
        self.body = b""
        try:
            self.read_body()
            if path.startswith("/api/"):
                self.api(path, parse_qs(url.query))
            else:
                self.static(path)
        except (ValueError, json.JSONDecodeError):
            self.error(HTTPStatus.BAD_REQUEST, "BAD_REQUEST")

    def api(self, path: str, query: dict) -> None:
        method = self.command
        state = self.state
        if path == "/api/health" and method in ("GET", "HEAD"):
            return self.send_json(200, {"status": "ok", "service": "ticket-desk"})
        if path == "/api/login" and method == "POST":
            body = self.read_json()
            body = body if isinstance(body, dict) else {}
            result = state.login(str(body.get("username", "")), str(body.get("password", "")))
            if not result:
                return self.error(401, "INVALID_CREDENTIALS", message="账号或密码不正确")
            token, user = result
            cookie = f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax"
            return self.send_json(200, {"token": token, "user": user}, {"Set-Cookie": cookie})
        if path == "/api/test/reset" and method == "POST":
            state.reset()
            return self.send_json(200, {"reset": True})
        if path == "/api/test/bug" and method == "POST":
            # Demo aid: switch on a known defect (new tickets ignore the chosen priority)
            # so that failing cases and their reports can be shown for real.
            body = self.read_json()
            state.bug = bool(isinstance(body, dict) and body.get("on"))
            return self.send_json(200, {"bug": state.bug})
        if path == "/api/test/expire-sessions" and method == "POST":
            return self.send_json(200, {"expired": state.expire_all()})

        user = self.current_user()
        if not user:
            return self.error(401, "UNAUTHENTICATED", message="请先登录")
        if path == "/api/me" and method in ("GET", "HEAD"):
            return self.send_json(200, user)
        if path == "/api/logout" and method == "POST":
            state.logout(self.token())
            expired = f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"
            return self.send_json(200, {"ok": True}, {"Set-Cookie": expired})
        return self._ticket_api(path, query, user, method, state)

    def _ticket_api(self, path: str, query: dict, user: dict, method: str, state: State) -> None:
        """Handle authenticated ticket routes with the existing lock boundaries."""
        if path == "/api/tickets" and method in ("GET", "HEAD"):
            status = (query.get("status") or [""])[0]
            keyword = (query.get("q") or [""])[0].strip().casefold()
            if status and status not in STATUSES:
                return self.error(422, "VALIDATION", fields={"status": "invalid"})
            with state.lock:
                items = [
                    dict(t)
                    for t in state.tickets
                    if (not status or t["status"] == status) and (not keyword or keyword in t["title"].casefold())
                ]
            return self.send_json(200, {"items": items, "total": len(items)})
        if path == "/api/tickets" and method == "POST":
            if user["role"] != "admin":
                return self.error(403, "FORBIDDEN", message="当前角色不能新建工单")
            values, errors = validate_ticket(self.read_json())
            if errors:
                return self.error(422, "VALIDATION", fields=errors)
            with state.lock:
                priority = "medium" if state.bug else values["priority"]
                ticket = dict(state._add(values["title"], priority, values["description"], user["id"]))
            return self.send_json(201, ticket)
        if path.startswith("/api/tickets/"):
            ticket_id = path.rsplit("/", 1)[-1]
            with state.lock:
                ticket = next((t for t in state.tickets if t["id"] == ticket_id), None)
                if not ticket:
                    return self.error(404, "NOT_FOUND")
                if method in ("GET", "HEAD"):
                    return self.send_json(200, dict(ticket))
                if method == "PATCH":
                    if user["role"] != "admin":
                        return self.error(403, "FORBIDDEN", message="当前角色不能修改工单")
                    body = self.read_json()
                    status = body.get("status") if isinstance(body, dict) else None
                    if status not in STATUSES:
                        return self.error(422, "VALIDATION", fields={"status": "invalid"})
                    ticket["status"] = status
                    return self.send_json(200, dict(ticket))
        self.error(404, "NOT_FOUND")
        return None

    def static(self, path: str) -> None:
        if self.command not in ("GET", "HEAD"):
            return self.error(405, "METHOD_NOT_ALLOWED")
        name = "index.html" if path in ("", "/") else path.lstrip("/")
        file = (WEB / name).resolve()
        if not file.is_relative_to(WEB) or not file.is_file():
            return self.error(404, "NOT_FOUND")
        data = file.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(file.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)
        return None


class Server(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second process bind a port that is already in use;
    # refuse instead, so "already running" is detected.
    allow_reuse_address = os.name != "nt"


def make_server(port: int = 8790, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    """Create a server bound to loopback. Port 0 picks a free port (used by tests)."""
    handler = type("TicketDeskHandler", (Handler,), {"state": State()})
    server = Server((host, port), handler)
    server.daemon_threads = True
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8790)
    args = parser.parse_args()
    server = make_server(args.port)
    print(f"Ticket Desk: http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
