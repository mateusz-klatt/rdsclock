"""Browser dashboard for ``rdsclock recon`` (standard library only).

``rdsclock recon --web`` starts ``rdsclock recon --jsonl`` as a child process
and serves a page that follows it through Server-Sent Events; the page shows
what :mod:`rdsclock.dashboard` makes of the events. The server listens on
127.0.0.1 by default. Reach a remote receiver through an SSH tunnel rather
than by exposing the page: it has no authentication.

Any web page that the operator opens can send requests to a loopback
server, so the dashboard defends itself:

* a request whose ``Host`` is not ``localhost``, an IP address or the
  configured host name is refused, which defeats DNS rebinding;
* the only action, ``POST /api/rescan``, needs an ``X-Requested-By`` header,
  which another site cannot send without a CORS preflight that this server
  never grants, a same-origin ``Origin`` when one is sent, and a client on
  this machine;
* every response carries a strict Content-Security-Policy, the page loads
  nothing from other origins, and it inserts every value as text, never as
  HTML: station names come from broadcasts.
"""

import contextlib
import html
import ipaddress
import json
import signal
import socket
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any
from urllib.parse import urlsplit

from .dashboard import COLUMNS, UNCERTAINTY_NOTE, DashboardState, ReceiverProcess
from .i18n import translate

LOG_LINES = 200
STOP_TIMEOUT_S = 5.0
TICK_S = 1.0  # the page also advances its clock between events at this pace

_STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
}
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
_RESCAN_REQUESTED = "Rescan requested; it starts after the current cycle."
_WEB_LABELS = {
    "connecting": "Connecting…",
    "rescan": "Rescan",
    "pause": "Pause view",
    "resume": "Resume view",
    "paused": "VIEW PAUSED · the receiver keeps running",
    "consensus": "Consensus",
    "stations": "Stations",
    "log": "Log",
    "waiting": "Waiting for the first completed cycle…",
    "unavailable": "TIME UNAVAILABLE",
    "empty": "No stations yet: the acquisition scan chooses them.",
    "noscript": "This dashboard needs JavaScript to follow the receiver.",
    "disconnected": "Connection lost; reconnecting…",
    "rescan_failed": "The rescan request failed:",
}


def is_loopback(address: str) -> bool:
    """Whether ``address`` names this machine (``localhost`` or a loopback IP)."""
    if address == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def host_allowed(host_header: str | None, server_host: str) -> bool:
    """Accept ``localhost``, IP addresses and the configured name; refuse the rest.

    A DNS-rebinding page reaches the server under its own domain name, which
    the browser sends in ``Host``.
    """
    hostname = urlsplit(f"//{host_header}").hostname if host_header else None
    if hostname is None:
        return False
    if hostname in ("localhost", server_host.lower()):
        return True
    with contextlib.suppress(ValueError):
        ipaddress.ip_address(hostname)
        return True
    return False


class WebDashboard:
    """The receiver child process, the shared dashboard state and the HTTP server."""

    def __init__(
        self,
        command: list[str],
        host: str = "127.0.0.1",
        port: int = 8080,
        clock: Callable[[], float] = time.monotonic,
        language: str = "en",
    ) -> None:
        self.host = host
        self.state = DashboardState(clock=clock, language=language)
        self.language = self.state.language
        self.labels = {key: self._t(message) for key, message in _WEB_LABELS.items()}
        self.log: deque[str] = deque(maxlen=LOG_LINES)
        self.version = 0
        self.closing = False
        self.failed = False
        self.changed = threading.Condition()
        self.pages = {
            path: resources.files(__package__).joinpath("webui", name).read_bytes()
            for path, (name, _) in _STATIC.items()
        }
        page = self.pages["/"].decode("utf-8")
        for message in (*_WEB_LABELS.values(), *COLUMNS):
            page = page.replace(f">{message}<", f">{html.escape(self._t(message))}<")
        page = page.replace('lang="en"', f'lang="{self.language}"')
        labels = html.escape(json.dumps(self.labels, ensure_ascii=False), quote=True)
        page = page.replace("<body>", f'<body data-labels="{labels}">')
        self.pages["/"] = page.encode("utf-8")
        self.receiver = ReceiverProcess(command, self._on_line, self._on_exit)
        self._stop_requested = False
        server_class = _Server6 if ":" in host else _Server
        self.server = server_class((host, port), _Handler)
        self.server.dashboard = self

    def _t(self, message: str, **values: object) -> str:
        return translate(self.language, message, **values)

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        if ipaddress.ip_address(host).is_unspecified:
            host = "127.0.0.1" if ":" not in host else "::1"
        authority = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        # Plain HTTP by design: the page is for this machine or an SSH tunnel, and
        # HTTPS would need a certificate that an operator does not have.
        return f"http://{authority}/"  # NOSONAR(S5332)

    def start(self) -> None:
        """Start the receiver; a failure is shown on the page and in the exit status."""
        try:
            self.receiver.start()
        except OSError as exc:
            with self.changed:
                self.failed = True
                self._publish(self.state.start_failed(exc))

    def _publish(self, messages: list[str]) -> None:
        # Called with ``changed`` held.
        self.log.extend(messages)
        self.version += 1
        self.changed.notify_all()

    def _on_line(self, line: str) -> None:
        with self.changed:
            self._publish(self.state.apply_line(line))

    def _on_exit(self, returncode: int, stderr_tail: list[str]) -> None:
        with self.changed:
            self.failed = self.state.exit_failed(returncode, self._stop_requested)
            self._publish(self.state.receiver_exited(returncode, stderr_tail, self._stop_requested))

    def snapshot(self, client: str) -> tuple[dict[str, Any], int]:
        """The page model for ``client`` and the state version it reflects."""
        with self.changed:
            view = self.state.view().as_dict()
            view["note"] = self._t(UNCERTAINTY_NOTE)
            view["columns"] = [self._t(name) for name in COLUMNS]
            view["language"] = self.language
            for row in view["rows"]:
                row["status_label"] = self._t(row["status"])
            if view["consensus"] is not None:
                view["consensus"]["trust_label"] = self._t(view["consensus"]["trust_level"])
            view["log"] = list(self.log)
            view["can_rescan"] = (
                self.state.can_rescan() and self.receiver.running and is_loopback(client)
            )
            return view, self.version

    def wait_for_change(self, seen: int, timeout_s: float) -> None:
        with self.changed:
            self.changed.wait_for(lambda: self.version != seen or self.closing, timeout_s)

    def request_rescan(self) -> tuple[HTTPStatus, str]:
        with self.changed:
            if not self.state.can_rescan() or not self.receiver.send_signal(signal.SIGUSR1):
                return HTTPStatus.CONFLICT, self._t("The receiver is not ready for a rescan.")
            message = self._t(_RESCAN_REQUESTED)
            self._publish([message])
            return HTTPStatus.ACCEPTED, message

    def serve_forever(self) -> None:
        self.server.serve_forever(poll_interval=0.2)

    def close(self) -> None:
        """Stop the receiver as Ctrl+C would, end the event streams and the server."""
        with self.changed:
            self.closing = True
            self._stop_requested = True
            self.changed.notify_all()
        self.receiver.stop(STOP_TIMEOUT_S)
        self.receiver.join(STOP_TIMEOUT_S)
        self.server.server_close()


class _Server(ThreadingHTTPServer):
    dashboard: WebDashboard


class _Server6(_Server):
    address_family = socket.AF_INET6


class _Handler(BaseHTTPRequestHandler):
    server: _Server
    server_version = "rdsclock"
    sys_version = ""

    @property
    def dashboard(self) -> WebDashboard:
        return self.server.dashboard

    def log_message(self, format: str, *args: object) -> None:
        pass  # the page shows what matters; request logs would bury it

    def do_GET(self) -> None:
        if not self._host_allowed():
            return
        path = urlsplit(self.path).path
        if path in _STATIC:
            self._send(HTTPStatus.OK, self.dashboard.pages[path], _STATIC[path][1])
        elif path == "/api/state":
            self._send_json(HTTPStatus.OK, self.dashboard.snapshot(self.client_address[0])[0])
        elif path == "/events":
            self._stream()
        else:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found."})

    def do_POST(self) -> None:
        if not self._host_allowed():
            return
        if urlsplit(self.path).path != "/api/rescan":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
            return
        refusal = self._action_refusal()
        if refusal is not None:
            self._send_json(HTTPStatus.FORBIDDEN, {"error": refusal})
            return
        status, message = self.dashboard.request_rescan()
        key = "message" if status == HTTPStatus.ACCEPTED else "error"
        self._send_json(status, {key: message})

    def _host_allowed(self) -> bool:
        if host_allowed(self.headers.get("Host"), self.dashboard.host):
            return True
        self._send_json(HTTPStatus.MISDIRECTED_REQUEST, {"error": "Unknown host name."})
        return False

    def _action_refusal(self) -> str | None:
        if self.headers.get("X-Requested-By") != "rdsclock":
            return "The X-Requested-By header is missing."
        origin = self.headers.get("Origin")
        if origin is not None and urlsplit(origin).netloc != self.headers.get("Host"):
            return "Requests from other origins are not accepted."
        if not is_loopback(self.client_address[0]):
            return "Rescans are accepted only from this machine."
        return None

    def _stream(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self._security_headers()
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            while not self.dashboard.closing:
                view, version = self.dashboard.snapshot(self.client_address[0])
                self.wfile.write(b"data: " + json.dumps(view).encode() + b"\n\n")
                self.wfile.flush()
                self.dashboard.wait_for_change(version, TICK_S)

    def _security_headers(self) -> None:
        for name, value in _SECURITY_HEADERS.items():
            self.send_header(name, value)

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        if "error" in payload:
            payload = {**payload, "error": self.dashboard._t(payload["error"])}
        self._send(status, json.dumps(payload).encode(), "application/json")


def _stop_serving(signum: int, frame: object) -> None:
    raise KeyboardInterrupt  # SIGTERM stops the server and the receiver as Ctrl+C does


def run_web(command: list[str], host: str, port: int, language: str = "en") -> int:
    """Serve the dashboard until Ctrl+C or SIGTERM; return the exit status.

    SIGUSR1 requests a rescan, as it does for live recon without a dashboard.
    """
    try:
        dashboard = WebDashboard(command, host, port, language=language)
    except (OSError, OverflowError) as exc:  # OverflowError: port outside 0-65535
        print(
            translate(
                language,
                "error: cannot listen on {host}:{port}: {error}",
                host=host,
                port=port,
                error=exc,
            ),
            file=sys.stderr,
        )
        return 2
    print(
        translate(
            language, "Dashboard at {url} (Ctrl+C stops it and the receiver)", url=dashboard.url
        ),
        file=sys.stderr,
    )
    if not is_loopback(host):
        print(
            translate(
                language,
                "warning: the dashboard has no authentication; anyone who can reach this "
                "address can watch it. Rescans are accepted only from this machine.",
            ),
            file=sys.stderr,
        )

    def rescan_soon(signum: int, frame: object) -> None:
        # The handler interrupts the main thread, which may hold the dashboard
        # lock; a short-lived thread takes it instead.
        threading.Thread(target=dashboard.request_rescan, daemon=True).start()

    previous = {
        signal.SIGUSR1: signal.signal(signal.SIGUSR1, rescan_soon),
        signal.SIGTERM: signal.signal(signal.SIGTERM, _stop_serving),
    }
    try:
        dashboard.start()
        dashboard.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        dashboard.close()
    return 1 if dashboard.failed else 0
