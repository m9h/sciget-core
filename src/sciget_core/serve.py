"""`sciget serve`: the local HTTP API (docs/api.md) and the web UI.

Loopback only. Every /api/ call needs the bearer token printed at startup;
Host and Origin are checked so another web page cannot drive the service
through the user's browser.
"""

from __future__ import annotations

import json
import mimetypes
import re
import secrets
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, urlsplit

from sciget_core import __version__, paths
from sciget_core.engines import EngineError
from sciget_core.manager import Manager, SessionError
from sciget_core.plan import PlanError

_SESSION_PATH = re.compile(r"^/api/v1/sessions/([a-z0-9]{4,32})(/logs)?$")
_UI_CSP = "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
_MAX_BODY = 64 * 1024


class ApiError(Exception):
    def __init__(self, status: HTTPStatus, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _web_root():
    return resources.files("sciget_core") / "web"


class Handler(BaseHTTPRequestHandler):
    server_version = "sciget"
    sys_version = ""
    manager: Manager
    token: str
    port: int

    # Logging: one line per request on stderr, without query strings.
    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write(f"{self.command} {urlsplit(self.path).path} {args[1] if len(args) > 1 else ''}\n")

    # Responses

    def _common_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")

    def _json(self, status: HTTPStatus, body) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self._common_headers()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, err: ApiError) -> None:
        self._json(err.status, {"error": err.code, "message": err.message})

    # Checks

    def _check_host_and_origin(self) -> None:
        allowed = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        if self.headers.get("Host") not in allowed:
            raise ApiError(HTTPStatus.MISDIRECTED_REQUEST, "bad_request", "Unexpected Host header.")
        origin = self.headers.get("Origin")
        if origin is not None and origin not in {f"http://{h}" for h in allowed}:
            raise ApiError(HTTPStatus.FORBIDDEN, "bad_request", "Requests from other sites are not allowed.")

    def _check_token(self) -> None:
        auth = self.headers.get("Authorization", "")
        scheme, _, value = auth.partition(" ")
        if scheme != "Bearer" or not secrets.compare_digest(value.strip(), self.token):
            raise ApiError(
                HTTPStatus.UNAUTHORIZED,
                "unauthorized",
                "Missing or wrong token. Reopen the address that `sciget serve` printed.",
            )

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > _MAX_BODY:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "bad_request", "Request body too large.")
        if self.headers.get_content_type() != "application/json" and length:
            raise ApiError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "bad_request", "Send JSON.")
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad_request", "The body is not valid JSON.") from None
        if not isinstance(data, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad_request", "The body must be a JSON object.")
        return data

    # Dispatch

    def _handle(self, method: str) -> None:
        try:
            self._check_host_and_origin()
            url = urlsplit(self.path)
            if not url.path.startswith("/api/"):
                if method != "GET":
                    raise ApiError(HTTPStatus.METHOD_NOT_ALLOWED, "bad_request", "Only GET is allowed here.")
                return self._static(url.path)
            self._check_token()
            self._api(method, url.path, parse_qs(url.query))
        except ApiError as err:
            self._error(err)
        except (SessionError, PlanError) as exc:
            self._error(ApiError(HTTPStatus.BAD_REQUEST, "bad_request", str(exc)))
        except EngineError as exc:
            self._error(ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "engine_unavailable", str(exc)))
        except Exception as exc:  # noqa: BLE001 - last resort, reported to the client
            sys.stderr.write(f"internal error: {exc!r}\n")
            self._error(ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, "internal", "Internal error; see the sciget serve output."))

    def _api(self, method: str, path: str, query: dict) -> None:
        m = self.manager
        if path == "/api/v1/health" and method == "GET":
            return self._json(HTTPStatus.OK, {"ok": True, "version": __version__})
        if path == "/api/v1/domains" and method == "GET":
            return self._json(HTTPStatus.OK, [d.to_json() for d in m.domains().values()])
        if path == "/api/v1/engines" and method == "GET":
            return self._json(HTTPStatus.OK, [e.to_json() for e in m.engines()])
        if path == "/api/v1/settings":
            if method == "GET":
                return self._json(HTTPStatus.OK, m.settings())
            if method == "PUT":
                return self._json(HTTPStatus.OK, m.update_settings(self._body()))
        if path == "/api/v1/sessions":
            if method == "GET":
                return self._json(HTTPStatus.OK, [s.to_json() for s in m.list()])
            if method == "POST":
                allowed = {"domain", "engine", "version", "data_dir", "cvmfs", "gpu"}
                body = self._body()
                unknown = set(body) - allowed
                if unknown:
                    raise ApiError(HTTPStatus.BAD_REQUEST, "bad_request", f"Unknown fields: {', '.join(sorted(unknown))}.")
                return self._json(HTTPStatus.ACCEPTED, m.start(body, background=True).to_json())
        match = _SESSION_PATH.match(path)
        if match:
            session_id, logs = match.groups()
            if logs and method == "GET":
                try:
                    tail = max(1, min(5000, int(query.get("tail", ["200"])[0])))
                except ValueError:
                    raise ApiError(HTTPStatus.BAD_REQUEST, "bad_request", "tail must be a number.") from None
                return self._json(HTTPStatus.OK, {"lines": m.logs(session_id, tail)})
            if not logs and method == "GET":
                return self._json(HTTPStatus.OK, self._session_or_404(session_id).to_json())
            if not logs and method == "DELETE":
                self._session_or_404(session_id)
                return self._json(HTTPStatus.OK, m.stop(session_id).to_json())
        raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "No such endpoint.")

    def _session_or_404(self, session_id: str):
        try:
            return self.manager.get(session_id)
        except SessionError as exc:
            raise ApiError(HTTPStatus.NOT_FOUND, "not_found", str(exc)) from None

    def _static(self, path: str) -> None:
        root = _web_root()
        rel = path.lstrip("/") or "index.html"
        parts = rel.split("/")
        if any(p in ("", ".", "..") or p.startswith(".") for p in parts):
            raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "Not found.")
        node = root
        for p in parts:
            node = node / p
        if not node.is_file():
            raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "Not found.")
        data = node.read_bytes()
        ctype = mimetypes.guess_type(parts[-1])[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
            ctype += "; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self._common_headers()
        self.send_header("Content-Security-Policy", _UI_CSP)
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_DELETE(self) -> None:
        self._handle("DELETE")


def make_server(manager: Manager, port: int = 0, token: str | None = None) -> ThreadingHTTPServer:
    token = token or secrets.token_urlsafe(32)
    handler = type("BoundHandler", (Handler,), {"manager": manager, "token": token, "port": 0})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    handler.port = server.server_address[1]
    server.daemon_threads = True
    server.token = token  # type: ignore[attr-defined]
    return server


def ui_url(server: ThreadingHTTPServer) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}/#token={server.token}"  # type: ignore[attr-defined]


def serve(manager: Manager, port: int = 0, open_browser: bool = False) -> None:
    server = make_server(manager, port)
    url = ui_url(server)
    paths.write_private(
        manager.state_dir / "serve.json",
        json.dumps({"url": f"http://127.0.0.1:{server.server_address[1]}/", "token": server.token}),  # type: ignore[attr-defined]
    )
    print(f"Sciget is running. Open this address in your browser:\n  {url}", flush=True)
    if open_browser:
        import webbrowser

        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        (manager.state_dir / "serve.json").unlink(missing_ok=True)
