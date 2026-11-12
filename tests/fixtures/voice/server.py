"""Local-only deterministic SSE and WebRTC fixture; no ASR/model integration."""
import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import time
from urllib.parse import parse_qs, urlsplit


class VoiceHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(Path(__file__).parent), **kwargs)

    def log_message(self, *args):
        pass

    def do_GET(self):
        parts = urlsplit(self.path)
        if parts.path != "/events":
            return super().do_GET()
        query = parse_qs(parts.query)
        delay = min(15, max(0, int(query.get("delay_ms", [2400])[0]) / 1000))
        text = query.get("text", ["Hello test voice"])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def event(name, data):
            self.wfile.write(("event: " + name + "\ndata: " + json.dumps(data) + "\n\n").encode())
            self.wfile.flush()

        try:
            event("thinking", {"phase": "reasoning"})
            time.sleep(0.12)
            event("token", {"text": "Local reply: "})
            deadline = time.monotonic() + delay
            while time.monotonic() < deadline:
                self.wfile.write(b": thinking heartbeat\n\n")
                self.wfile.flush()
                time.sleep(min(0.2, max(0, deadline - time.monotonic())))
            event("token", {"text": text})
            event("done", {"complete": True})
        except (BrokenPipeError, ConnectionResetError):
            pass


class VoiceServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8792)
    args = parser.parse_args()
    VoiceServer(("127.0.0.1", args.port), VoiceHandler).serve_forever()
