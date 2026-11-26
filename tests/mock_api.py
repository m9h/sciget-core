# SPDX-License-Identifier: BSD-3-Clause
"""Stdlib-only mock of the sciget-core local API (docs/api.md, version 1).

It serves the static UI from src/sciget_core/web/ and fakes domains, engines
and sessions so the UI can be developed and tested without containers. A new
session goes pulling -> starting -> ready over a few seconds.

Run:  python3 -I tests/mock_api.py --port 8765
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

WEB_DIR = Path(__file__).resolve().parent.parent / "src" / "sciget_core" / "web"
VERSION = "0.1.0-mock"

CSP = "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
BASE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
UI_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Frame-Options": "DENY",
}

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json",
}

DOMAINS = [
    {
        "name": "neurodesk",
        "title": "Neurodesk",
        "description": "Neuroimaging desktop with hundreds of analysis tools, "
                       "loaded on demand from CVMFS.",
        "image": "docker.io/vnmd/neurodesktop",
        "default_version": "2026-09-23",
        "remote_urls": ["https://play-europe.neurodesk.org/",
                        "https://play-america.neurodesk.org/"],
        "cvmfs_repository": "neurodesk.ardc.edu.au",
    },
    {
        "name": "geodesk",
        "title": "Geodesk",
        "description": "Geoscience tools for remote sensing and GIS work.",
        "image": "docker.io/example/geodesktop",
        "default_version": "2026-08-01",
        "remote_urls": [],
        "cvmfs_repository": "geodesk.example.org",
    },
    {
        "name": "bio",
        "title": "Bioinformatics",
        "description": "Sequence analysis and genomics pipelines.",
        "image": "docker.io/example/biodesktop",
        "default_version": "2026-07-15",
        "remote_urls": ["javascript:alert(1)"],  # the UI must refuse this one
        "cvmfs_repository": "bio.example.org",
    },
]

ENGINES = [
    {"kind": "podman", "available": True, "rootless": True, "version": "5.6.2"},
    {"kind": "docker", "available": False, "rootless": False, "version": None},
    {"kind": "apptainer", "available": True, "rootless": True, "version": "1.4.3"},
]

ENGINE_KINDS = {e["kind"] for e in ENGINES}
CVMFS_SETTINGS = {"auto", "host", "container", "off"}

# Seconds spent in each phase.
PULL_SECONDS = 2.0
START_SECONDS = 2.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    def __init__(self, port_getter):
        self.lock = threading.Lock()
        self.port = port_getter
        self.settings = {
            "engine": "auto",
            "cvmfs": "auto",
            "gpu": False,
            "storage_dir": str(Path.home() / "neurodesktop-storage"),
            "favorites": ["neurodesk"],
        }
        self.sessions: dict[str, dict] = {}

    # ----- sessions -----

    def _advance(self, s: dict) -> None:
        if s["state"] in ("stopped", "error"):
            return
        age = time.monotonic() - s["_t0"]
        if age < PULL_SECONDS:
            s["state"] = "pulling"
        elif age < PULL_SECONDS + START_SECONDS:
            s["state"] = "starting"
        elif s["_fail"]:
            s["state"] = "error"
            s["error"] = "The container exited during startup. Open the logs for details."
        else:
            s["state"] = "ready"

    def public(self, s: dict) -> dict:
        self._advance(s)
        out = {k: v for k, v in s.items() if not k.startswith("_")}
        out["url"] = (f"http://127.0.0.1:{self.port()}/mock-lab/{s['id']}?token={s['_jtoken']}"
                      if s["state"] == "ready" else None)
        return out

    def create(self, body: dict) -> dict:
        settings = self.settings
        engine = body.get("engine", settings["engine"])
        if engine == "auto":
            engine = next(e["kind"] for e in ENGINES if e["available"])
        cvmfs = body.get("cvmfs", settings["cvmfs"])
        if cvmfs == "auto":
            cvmfs = "container"
        sid = secrets.token_hex(3)
        s = {
            "id": sid,
            "domain": body["domain"],
            "engine": engine,
            "version": body.get("version") or next(
                d["default_version"] for d in DOMAINS if d["name"] == body["domain"]),
            "state": "pulling",
            "created": _now_iso(),
            "port": 41000 + len(self.sessions),
            "data_dir": body.get("data_dir"),
            "cvmfs": cvmfs,
            "gpu": bool(body.get("gpu", settings["gpu"])),
            "url": None,
            "error": None,
            "_t0": time.monotonic(),
            "_jtoken": secrets.token_urlsafe(12),
            # The Bioinformatics domain always fails, so the error state can be seen.
            "_fail": body["domain"] == "bio",
        }
        self.sessions[sid] = s
        return s

    def logs(self, s: dict, tail: int) -> list[str]:
        self._advance(s)
        lines = [f"[{s['created']}] session {s['id']} created for {s['domain']}",
                 f"engine: {s['engine']}  cvmfs: {s['cvmfs']}  gpu: {s['gpu']}",
                 "pulling image ..."]
        if s["state"] != "pulling":
            lines += ["image up to date", "starting container ..."]
        if s["state"] == "ready":
            lines += ["Jupyter Server is running", f"listening on 127.0.0.1:{s['port']}"]
        if s["state"] == "error":
            lines += ["error: container exited with status 1"]
        if s["state"] == "stopped":
            lines += ["stopping container ...", "stopped"]
        return lines[-tail:] if tail > 0 else lines


class Handler(BaseHTTPRequestHandler):
    server_version = "sciget-mock"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # quiet by default; --verbose turns logging on
    def log_message(self, fmt, *args):
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    # ----- helpers -----

    def _send(self, status: int, body: bytes, ctype: str, ui: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in BASE_HEADERS.items():
            self.send_header(k, v)
        if ui:
            for k, v in UI_HEADERS.items():
                self.send_header(k, v)
        if status >= 400:
            # A rejected request may leave an unread body on the socket; don't reuse it.
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj) -> None:
        self._send(status, json.dumps(obj).encode(), "application/json")

    def _error(self, status: int, code: str, message: str) -> None:
        self._json(status, {"error": code, "message": message})

    def _allowed_hosts(self) -> set[str]:
        port = self.server.server_address[1]
        return {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _check_host_origin(self) -> bool:
        host = self.headers.get("Host", "")
        if host not in self._allowed_hosts():
            self._error(421, "bad_request", "This address is not served here. Use the address `sciget serve` printed.")
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin not in {f"http://{h}" for h in self._allowed_hosts()}:
            self._error(403, "bad_request", "Requests from other web sites are not allowed.")
            return False
        return True

    def _check_token(self) -> bool:
        auth = self.headers.get("Authorization", "")
        expected = f"Bearer {self.server.token}"
        if not secrets.compare_digest(auth.encode(), expected.encode()):
            self._error(401, "unauthorized",
                        "Missing or wrong access token. Open the address `sciget serve` printed.")
            return False
        return True

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 65536:
            raise ValueError("Request body is too large.")
        if length and self.headers.get_content_type() != "application/json":
            raise ValueError("Send JSON.")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("The request body must be a JSON object.")
        return data

    # ----- dispatch -----

    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        if not self._check_host_origin():
            return
        url = urlsplit(self.path)
        path = url.path
        if path.startswith("/api/"):
            if not self._check_token():
                return
            try:
                with self.server.store.lock:
                    self._api(method, path, parse_qs(url.query))
            except (ValueError, json.JSONDecodeError) as exc:
                self._error(400, "bad_request", str(exc))
            return
        if method != "GET":
            self._error(405, "bad_request", "Only GET is allowed here.")
            return
        if path.startswith("/mock-lab/"):
            body = ("<!doctype html><meta charset=utf-8><title>Mock JupyterLab</title>"
                    "<p>A real session would open JupyterLab here.</p>").encode()
            self._send(200, body, "text/html; charset=utf-8", ui=True)
            return
        self._static(path)

    def _static(self, path: str) -> None:
        rel = path.lstrip("/") or "index.html"
        target = (WEB_DIR / rel).resolve()
        try:
            target.relative_to(WEB_DIR.resolve())
        except ValueError:
            self._error(404, "not_found", "No such file.")
            return
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            self._error(404, "not_found", "No such file.")
            return
        ctype = CONTENT_TYPES.get(target.suffix) or mimetypes.guess_type(target.name)[0] \
            or "application/octet-stream"
        self._send(200, target.read_bytes(), ctype, ui=True)

    def _api(self, method: str, path: str, query: dict) -> None:
        store: Store = self.server.store
        parts = [p for p in path.split("/") if p]  # api, v1, ...
        if parts[:2] != ["api", "v1"]:
            self._error(404, "not_found", "Unknown API version.")
            return
        rest = parts[2:]

        if rest == ["health"] and method == "GET":
            self._json(200, {"ok": True, "version": VERSION})
        elif rest == ["domains"] and method == "GET":
            self._json(200, DOMAINS)
        elif rest == ["engines"] and method == "GET":
            self._json(200, ENGINES)
        elif rest == ["settings"] and method == "GET":
            self._json(200, store.settings)
        elif rest == ["settings"] and method == "PUT":
            self._put_settings(self._read_json())
        elif rest == ["sessions"] and method == "GET":
            self._json(200, [store.public(s) for s in store.sessions.values()])
        elif rest == ["sessions"] and method == "POST":
            self._post_session(self._read_json())
        elif len(rest) >= 2 and rest[0] == "sessions":
            s = store.sessions.get(rest[1])
            if s is None:
                self._error(404, "not_found", "That session no longer exists.")
            elif len(rest) == 2 and method == "GET":
                self._json(200, store.public(s))
            elif len(rest) == 2 and method == "DELETE":
                store._advance(s)
                s["state"] = "stopped"
                self._json(200, store.public(s))
            elif rest[2:] == ["logs"] and method == "GET":
                try:
                    tail = int(query.get("tail", ["100"])[0])
                except ValueError:
                    raise ValueError("tail must be a number.") from None
                self._json(200, {"lines": store.logs(s, tail)})
            else:
                self._error(404, "not_found", "No such endpoint.")
        else:
            self._error(404, "not_found", "No such endpoint.")

    def _put_settings(self, body: dict) -> None:
        store: Store = self.server.store
        new = dict(store.settings)
        for key, value in body.items():
            if key == "engine":
                if value != "auto" and value not in ENGINE_KINDS:
                    raise ValueError(f"Unknown engine {value!r}. Choose auto, podman, docker or apptainer.")
            elif key == "cvmfs":
                if value not in CVMFS_SETTINGS:
                    raise ValueError("CVMFS mode must be auto, host, container or off.")
            elif key == "gpu":
                if not isinstance(value, bool):
                    raise ValueError("gpu must be true or false.")
            elif key == "storage_dir":
                if not isinstance(value, str) or not value.startswith("/"):
                    raise ValueError("The storage folder must be a full path starting with /.")
            elif key == "favorites":
                if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                    raise ValueError("favorites must be a list of domain names.")
            else:
                raise ValueError(f"Unknown setting {key!r}.")
            new[key] = value
        store.settings = new
        self._json(200, new)

    def _post_session(self, body: dict) -> None:
        store: Store = self.server.store
        domain = body.get("domain")
        if not any(d["name"] == domain for d in DOMAINS):
            self._error(404, "not_found", f"There is no domain called {domain!r}.")
            return
        engine = body.get("engine", store.settings["engine"])
        if engine != "auto":
            info = next((e for e in ENGINES if e["kind"] == engine), None)
            if info is None:
                raise ValueError(f"Unknown engine {engine!r}.")
            if not info["available"]:
                self._error(503, "engine_unavailable",
                            f"{engine} is not available on this computer. Choose another engine or install it.")
                return
        cvmfs = body.get("cvmfs", store.settings["cvmfs"])
        if cvmfs not in CVMFS_SETTINGS:
            raise ValueError("CVMFS mode must be auto, host, container or off.")
        data_dir = body.get("data_dir")
        if data_dir is not None and not os.path.isdir(data_dir):
            raise ValueError(f"The data folder {data_dir} does not exist. Create it or choose another folder.")
        self._json(202, store.public(store.create(body)))


def make_server(port: int = 0, token: str | None = None, verbose: bool = False) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    httpd.token = token or secrets.token_urlsafe(24)
    httpd.verbose = verbose
    httpd.store = Store(lambda: httpd.server_address[1])
    return httpd


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Mock sciget serve for UI development.")
    parser.add_argument("--port", type=int, default=0, help="port on 127.0.0.1 (default: pick a free one)")
    parser.add_argument("--token", default=None, help="fixed API token (default: random)")
    parser.add_argument("--verbose", action="store_true", help="log every request")
    args = parser.parse_args(argv)
    httpd = make_server(args.port, args.token, args.verbose)
    port = httpd.server_address[1]
    print(f"http://127.0.0.1:{port}/#token={httpd.token}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
