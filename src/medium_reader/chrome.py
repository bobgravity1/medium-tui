"""Local, temporary bridge to the user's installed Chrome extension.

Only rendered Medium story cards cross this bridge. Cookies and passwords stay
in Chrome. A fresh bearer token and loopback port are created for each CLI run.
"""

from __future__ import annotations

import html
import json
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .core import Article, ReaderError
from .home import parse_home

MAX_SNAPSHOT = 2 * 1024 * 1024


class SignInRequired(ReaderError):
    """Chrome answered, but Medium has not verified a signed-in account."""


class ChromeFeed:
    def __init__(self, *, extension_dir: Path | None = None, timeout: float = 30):
        self.extension_dir = extension_dir or Path(__file__).parent / "chrome_extension"
        self.timeout = timeout
        self.token = secrets.token_urlsafe(32)
        self.condition = threading.Condition()
        self.snapshot: dict | None = None
        self.command: dict | None = None
        self.reply_id = ""
        self.reply: dict | None = None
        self.paired = False
        self.opened = False
        self.closed = False
        self.origin: str | None = None
        self.loaded = False
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    @property
    def connected(self) -> bool:
        with self.condition:
            return bool(
                self.paired
                and self.snapshot
                and self.snapshot.get("signedIn") is True
                and time.monotonic() - self.snapshot["received"] < 10
            )

    def _start(self) -> None:
        if self.closed:
            raise ReaderError("Chrome connection was closed. Use /login to reconnect.")
        if self.server is not None:
            return
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:
                pass  # Request headers and pairing tokens must never reach logs.

            def respond(self, status: int, data: dict | str) -> None:
                body = data.encode() if isinstance(data, str) else json.dumps(data).encode()
                self.send_response(status)
                self.send_header(
                    "Content-Type",
                    "text/html; charset=utf-8" if isinstance(data, str) else "application/json",
                )
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header(
                    "Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'"
                )
                self.end_headers()
                self.wfile.write(body)

            def allowed(self) -> bool:
                expected_host = f"127.0.0.1:{bridge.server.server_port}"
                if self.headers.get("Host") != expected_host:
                    return False
                token = self.headers.get("Authorization", "")
                if not secrets.compare_digest(token, "Bearer " + bridge.token):
                    return False
                origin = self.headers.get("Origin")
                if origin:
                    import re

                    if not re.fullmatch(r"chrome-extension://[a-p]{32}", origin):
                        return False
                    if bridge.origin is not None and origin != bridge.origin:
                        return False
                    bridge.origin = origin
                return True

            def do_GET(self) -> None:
                if self.path == "/connect" and self.headers.get("Host") == (
                    f"127.0.0.1:{bridge.server.server_port}"
                ):
                    directory = html.escape(str(bridge.extension_dir))
                    self.respond(
                        200,
                        "<!doctype html><html><meta charset='utf-8'>"
                        "<title>Connect Medium Reader</title><style>"
                        "body{background:#1e1e2e;color:#cdd6f4;font:18px system-ui;"
                        "max-width:720px;margin:10vh auto;padding:24px;line-height:1.6}"
                        "button{font:inherit;padding:12px;color:#1e1e2e;background:#b4befe;"
                        "border:0;border-radius:8px}code{overflow-wrap:anywhere}"
                        "</style><h1>Connect Medium Reader</h1>"
                        "<p>Chrome keeps your Medium login. The reader receives story cards "
                        "from the connected tab, never your password or cookies.</p>"
                        "<p id='connection'>Install the Medium Reader extension first:</p>"
                        "<ol><li>Open <code>chrome://extensions</code>.</li>"
                        "<li>Enable Developer mode, choose Load unpacked, and select "
                        f"<code>{directory}</code>.</li>"
                        "<li>Reload this page, then click Connect.</li></ol>"
                        "<div id='medium-reader-connect'></div></html>",
                    )
                    return
                if not self.allowed():
                    self.respond(403, {"error": "Connection not authorized"})
                    return
                if self.path != "/command":
                    self.respond(404, {"error": "Unknown endpoint"})
                    return
                with bridge.condition:
                    command = bridge.command or {}
                self.respond(200, command)

            def do_POST(self) -> None:
                if not self.allowed():
                    self.respond(403, {"error": "Connection not authorized"})
                    return
                self.connection.settimeout(5)
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= MAX_SNAPSHOT:
                        self.respond(413, {"error": "Invalid message size"})
                        return
                    data = json.loads(self.rfile.read(length))
                    if not isinstance(data, dict):
                        raise ValueError("Expected an object")
                except (ValueError, OSError):
                    self.respond(400, {"error": "Invalid message"})
                    return
                with bridge.condition:
                    if self.path == "/pair":
                        bridge.paired = True
                    elif self.path == "/snapshot" and bridge.paired:
                        try:
                            if not isinstance(data.get("url"), str):
                                raise ValueError("Expected a URL")
                            url = urlsplit(data["url"])
                        except ValueError:
                            self.respond(400, {"error": "Invalid page URL"})
                            return
                        if url.scheme != "https" or url.hostname != "medium.com":
                            self.respond(400, {"error": "Expected a Medium page"})
                            return
                        if not isinstance(data.get("html", ""), str):
                            self.respond(400, {"error": "Expected rendered cards"})
                            return
                        data["received"] = time.monotonic()
                        bridge.snapshot = data
                        if bridge.command and data.get("id") == bridge.command["id"]:
                            bridge.reply_id = data["id"]
                            bridge.reply = dict(data)
                            bridge.command = None
                    else:
                        self.respond(404, {"error": "Unknown endpoint"})
                        return
                    bridge.condition.notify_all()
                self.respond(200, {"ok": True})

        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        except OSError as exc:
            raise ReaderError("Could not start the local Chrome connection.") from exc
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def begin_login(self) -> None:
        self._start()
        self.opened = True
        self.loaded = False
        with self.condition:
            self.command = {"id": secrets.token_hex(12), "action": "login"}
        address = f"http://127.0.0.1:{self.server.server_port}/connect#{self.token}"
        if not webbrowser.open(address, new=2):
            raise ReaderError("Could not open your default browser. Check its system setting.")

    def _request(self, action: str) -> dict:
        self._start()
        if not self.paired:
            raise ReaderError("Chrome is not connected. Use /login and connect the extension.")
        request_id = secrets.token_hex(12)
        deadline = time.monotonic() + self.timeout
        with self.condition:
            self.command = {"id": request_id, "action": action}
            self.condition.notify_all()
            while self.reply_id != request_id and not self.closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if self.command and self.command["id"] == request_id:
                        self.command = None
                    raise ReaderError("Chrome did not respond. Keep the connected Medium tab open.")
                self.condition.wait(remaining)
            if self.closed:
                raise ReaderError("Chrome was disconnected.")
            result = dict(self.reply or {})
        if result.get("error"):
            raise ReaderError(str(result["error"]))
        if result.get("signedIn") is not True:
            raise SignInRequired("Sign in to Medium in Chrome, then return to the reader.")
        return result

    def load_more(self) -> list[Article]:
        result = self._request("more" if self.loaded else "home")
        self.loaded = True
        articles = parse_home(result.get("html", ""))
        if not articles:
            raise ReaderError("Chrome is signed in, but no homepage stories were found.")
        return articles

    def load_favorites(self) -> list[Article]:
        result = self._request("favorites")
        articles = parse_home(result.get("html", ""))
        if not articles and not result.get("empty"):
            raise ReaderError("Could not find saved stories in Chrome's Medium Reading list.")
        self.loaded = False
        return articles

    def confirm_login(self) -> list[Article]:
        if not self.connected:
            raise SignInRequired("Connect the extension and finish signing in to Medium in Chrome.")
        self.loaded = False
        return self.load_more()

    def close(self) -> None:
        with self.condition:
            self.closed = True
            self.condition.notify_all()
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        if self.thread:
            self.thread.join(timeout=2)
