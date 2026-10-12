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

from .core import Article, ReaderError, clean_line, safe_url
from .home import parse_home

MAX_SNAPSHOT = 2 * 1024 * 1024
HEARTBEAT_GRACE = 90  # Chrome can throttle timers in a background tab.


class ConnectionRequired(ReaderError):
    """There is no paired Chrome tab for this reader process."""


class SignInRequired(ReaderError):
    """Chrome answered, but Medium has not verified a signed-in account."""


class ChromeFeed:
    def __init__(
        self, *, extension_dir: Path | None = None, timeout: float = 30, login_timeout: float = 180
    ):
        self.extension_dir = extension_dir or Path(__file__).parent / "chrome_extension"
        self.timeout = timeout
        self.login_timeout = login_timeout
        self.token = secrets.token_urlsafe(32)
        self.condition = threading.Condition()
        self.request_lock = threading.Lock()
        self.snapshot: dict | None = None
        self.command: dict | None = None
        self.reply_id = ""
        self.reply: dict | None = None
        self.paired = False
        self.opened = False
        self.closed = False
        self.origin: str | None = None
        self.session: str | None = None
        self.loaded = False
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    @property
    def connected(self) -> bool:
        with self.condition:
            return bool(
                not self.closed
                and self.paired
                and self.snapshot
                and self.snapshot.get("signedIn") is True
                and not self.snapshot.get("error")
                and time.monotonic() - self.snapshot["received"] < HEARTBEAT_GRACE
            )

    @property
    def state(self) -> str:
        with self.condition:
            if self.closed:
                return "disconnected"
            if not self.paired:
                return "waiting-for-extension" if self.opened else "disconnected"
            if self.snapshot and time.monotonic() - self.snapshot["received"] >= HEARTBEAT_GRACE:
                return "disconnected"
            if self.snapshot and self.snapshot.get("error"):
                return "error"
            if self.snapshot and self.snapshot.get("signedIn") is not True:
                return "sign-in-required"
            return "connected" if self.connected else "waiting-for-browser"

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

            def allowed(self, *, pairing: bool = False) -> bool:
                if bridge.closed:
                    return False
                expected_host = f"127.0.0.1:{self.server.server_port}"
                if self.headers.get("Host") != expected_host:
                    return False
                token = self.headers.get("Authorization", "")
                if not token.isascii() or not secrets.compare_digest(
                    token, "Bearer " + bridge.token
                ):
                    return False
                origin = self.headers.get("Origin")
                if origin:
                    import re

                    if not re.fullmatch(r"chrome-extension://[a-p]{32}", origin):
                        return False
                    if not pairing and bridge.origin is not None and origin != bridge.origin:
                        return False
                if not pairing:
                    session = self.headers.get("X-Medium-Reader-Session", "")
                    if (
                        not bridge.session
                        or not session.isascii()
                        or not secrets.compare_digest(session, bridge.session)
                    ):
                        return False
                return True

            def do_GET(self) -> None:
                if self.path == "/connect" and self.headers.get("Host") == (
                    f"127.0.0.1:{self.server.server_port}"
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
                        "<p>This project includes a custom connection extension; "
                        "it is not an official Medium extension.</p>"
                        "<p id='connection'>One-time setup: install the connection extension "
                        "in your normal Chrome window.</p>"
                        "<ol><li>In Chrome, type <code>chrome://extensions</code> "
                        "in the address bar.</li>"
                        "<li>Turn on <strong>Developer mode</strong> at the top right.</li>"
                        "<li>Click <strong>Load unpacked</strong>.</li>"
                        "<li>On macOS, press <strong>⌘⇧G (Command–Shift–G)</strong> "
                        "in the folder picker. Copy and paste this full path, then press "
                        "Return. On Windows or Linux, navigate to this folder: "
                        f"<br><code>{directory}</code></li>"
                        "<li>Click <strong>Select</strong> or <strong>Open</strong>.</li>"
                        "<li>Return to this page in Chrome, reload it, then click "
                        "<strong>Connect and open Medium</strong>.</li></ol>"
                        "<p>After connecting, sign in to Medium if prompted and return "
                        "to the terminal. Your feed appears automatically. Keep this "
                        "Medium tab open while reading.</p>"
                        "<p>If this page opened in Safari, Firefox, or another browser, "
                        "copy its full address from the address bar and paste it into "
                        "Chrome, where you installed the extension. Keep the terminal "
                        "reader running while you connect.</p>"
                        "<div id='medium-reader-connect'></div></html>",
                    )
                    return
                with bridge.condition:
                    if not self.allowed():
                        self.respond(403, {"error": "Connection not authorized"})
                        return
                    if self.path != "/command":
                        self.respond(404, {"error": "Unknown endpoint"})
                        return
                    command = bridge.command or {}
                self.respond(200, command)

            def do_POST(self) -> None:
                if not self.allowed(pairing=self.path == "/pair"):
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
                except (ValueError, OSError, RecursionError):
                    self.respond(400, {"error": "Invalid message"})
                    return
                with bridge.condition:
                    # The pairing may change while this request body is arriving.
                    if not self.allowed(pairing=self.path == "/pair"):
                        self.respond(403, {"error": "Connection not authorized"})
                        return
                    if self.path == "/pair":
                        bridge.origin = self.headers.get("Origin")
                        bridge.session = secrets.token_urlsafe(32)
                        bridge.paired = True
                        bridge.snapshot = None
                        bridge.loaded = False
                    elif self.path == "/snapshot" and bridge.paired:
                        try:
                            if not isinstance(data.get("url"), str):
                                raise ValueError("Expected a URL")
                            url = urlsplit(safe_url(data["url"], medium_only=True))
                        except (ValueError, ReaderError):
                            self.respond(400, {"error": "Invalid page URL"})
                            return
                        if url.scheme != "https" or url.hostname != "medium.com":
                            self.respond(400, {"error": "Expected a Medium page"})
                            return
                        if not isinstance(data.get("html", ""), str):
                            self.respond(400, {"error": "Expected rendered cards"})
                            return
                        if (
                            type(data.get("signedIn")) is not bool
                            or not isinstance(data.get("id", ""), str)
                            or not isinstance(data.get("error", ""), str)
                            or type(data.get("empty", False)) is not bool
                        ):
                            self.respond(400, {"error": "Invalid snapshot fields"})
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
                self.respond(
                    200,
                    {"ok": True, "session": bridge.session}
                    if self.path == "/pair"
                    else {"ok": True},
                )

        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        except OSError as exc:
            raise ReaderError("Could not start the local Chrome connection.") from exc
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def begin_login(self) -> None:
        self._start()
        self.loaded = False
        with self.condition:
            # A new /login request opens a fresh consent page. Revoke the old tab
            # now so its heartbeat cannot auto-complete the new login gate.
            self.paired = False
            self.session = None
            self.snapshot = None
            self.command = {"id": secrets.token_hex(12), "action": "login"}
        address = f"http://127.0.0.1:{self.server.server_port}/connect#{self.token}"
        try:
            opened = webbrowser.open(address, new=2)
        except (OSError, webbrowser.Error):
            opened = False
        if not opened:
            with self.condition:
                self.command = None
            raise ReaderError("Could not open your default browser. Check its system setting.")
        self.opened = True

    def wait_for_login(self) -> None:
        """One-shot CLI commands can authorize in Chrome without contaminating JSON stdout."""
        deadline = time.monotonic() + self.login_timeout
        with self.condition:
            while not self.connected:
                if self.closed:
                    raise ConnectionRequired("Chrome was disconnected.")
                if self.snapshot and self.snapshot.get("error"):
                    raise ReaderError(clean_line(self.snapshot["error"]))
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ConnectionRequired(
                        "Chrome connection timed out. Install the extension, run login, "
                        "and click Connect in the browser."
                    )
                self.condition.wait(remaining)

    def _request(self, action: str) -> dict:
        # Serialize calls so one request cannot overwrite another's command/reply.
        with self.request_lock:
            return self._request_serial(action)

    def _request_serial(self, action: str) -> dict:
        self._start()
        if not self.paired:
            raise ConnectionRequired(
                "Chrome is not connected. Use /login and connect the extension."
            )
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
            raise ReaderError(clean_line(result["error"]))
        if result.get("signedIn") is not True:
            raise SignInRequired("Sign in to Medium in Chrome, then return to the reader.")
        return result

    def load_more(self) -> list[Article]:
        result = self._request("more" if self.loaded else "home")
        articles = parse_home(result.get("html", ""))
        if not articles and not result.get("empty"):
            raise ReaderError("Chrome is signed in, but no homepage stories were found.")
        self.loaded = True
        return articles

    def load_favorites(self) -> list[Article]:
        self.loaded = False  # Navigation leaves the homepage even when saved-list loading fails.
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
        # A login snapshot already contains verified homepage cards. Reuse it
        # rather than asking Chrome for the same page immediately a second time.
        with self.condition:
            result = dict(self.snapshot or {})
        if urlsplit(result.get("url", "")).path == "/":
            articles = parse_home(result.get("html", ""))
            if articles or result.get("empty"):
                self.loaded = True
                return articles
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
