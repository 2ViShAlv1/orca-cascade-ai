"""Dependency-free HTTP server: static dashboard, JSON API and a live
Server-Sent-Events stream of engine snapshots."""
from __future__ import annotations

import json
import mimetypes
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .api import ROOT, handle_get, handle_post
from .engine import Engine

WEB_DIR = os.path.join(ROOT, "web")


def make_handler(engine: Engine):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # keep the console quiet
            pass

        def _json(self, obj, status: int = 200) -> None:
            body = json.dumps(obj, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            url = urlparse(self.path)
            path = url.path
            if path == "/api/stream":
                return self._stream()
            if path.startswith("/api/"):
                status, obj = handle_get(engine, path, url.query)
                return self._json(obj, status)
            if path == "/":
                path = "/index.html"
            file = os.path.normpath(os.path.join(WEB_DIR, path.lstrip("/")))
            if not file.startswith(WEB_DIR) or not os.path.isfile(file):
                return self._json({"error": "not found"}, 404)
            with open(file, "rb") as fh:
                body = fh.read()
            self.send_response(200)
            ctype = mimetypes.guess_type(file)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            path = urlparse(self.path).path
            try:
                body = self._body()
            except ValueError as exc:
                return self._json({"ok": False, "error": f"invalid JSON: {exc}"}, 400)
            status, obj = handle_post(engine, path, body)
            return self._json(obj, status)

        def _stream(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            last = -1
            try:
                while True:
                    with engine.cond:
                        engine.cond.wait_for(lambda: engine.seq != last, timeout=15)
                        snap, last_seen = engine.snapshot, engine.seq
                    if last_seen == last:
                        self.wfile.write(b": keep-alive\n\n")
                    else:
                        last = last_seen
                        self.wfile.write(b"data: " + json.dumps(snap, separators=(",", ":")).encode() + b"\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                return

    return Handler


def serve(host: str = "127.0.0.1", port: int = 8000, seed: int | None = None, interval: float = 1.0) -> None:
    engine = Engine(seed=seed if seed is not None else 7, interval=interval)
    engine.start()
    # Windows SO_REUSEADDR lets a second server silently share the port; refuse instead
    ThreadingHTTPServer.allow_reuse_address = sys.platform != "win32"
    try:
        httpd = ThreadingHTTPServer((host, port), make_handler(engine))
    except OSError as exc:
        engine.running = False
        raise SystemExit(f"Port {port} is already in use ({exc}). Is ORCA already running? "
                         f"Try: python run.py --port {port + 1}")
    httpd.daemon_threads = True
    print(f"ORCA dashboard running at http://{host}:{port}  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        engine.running = False
        httpd.server_close()


if __name__ == "__main__":
    threading.current_thread().name = "orca"
    serve()
