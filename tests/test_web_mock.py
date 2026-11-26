# SPDX-License-Identifier: BSD-3-Clause
"""Checks the web UI files and the security rules of the mock API."""

from __future__ import annotations

import http.client
import importlib.util
import json
import re
import threading
import time
from html.parser import HTMLParser
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
WEB = HERE.parent / "src" / "sciget_core" / "web"
TOKEN = "test-token-123"


def _load_mock():
    spec = importlib.util.spec_from_file_location("sciget_mock_api", HERE / "mock_api.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mock_api = _load_mock()


@pytest.fixture(scope="module")
def server():
    mock_api.PULL_SECONDS = 0.3
    mock_api.START_SECONDS = 0.3
    httpd = mock_api.make_server(0, TOKEN)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def request(port, method, path, headers=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    hdrs = {"Host": f"127.0.0.1:{port}"}
    hdrs.update(headers or {})
    payload = json.dumps(body).encode() if body is not None else None
    if payload is not None:
        hdrs["Content-Type"] = "application/json"
    conn.request(method, path, body=payload, headers=hdrs)
    res = conn.getresponse()
    data = res.read()
    conn.close()
    return res, data


def auth():
    return {"Authorization": f"Bearer {TOKEN}"}


# ---------- security rules ----------

def test_api_needs_token(server):
    res, data = request(server, "GET", "/api/v1/health")
    assert res.status == 401
    assert json.loads(data)["error"] == "unauthorized"
    res, _ = request(server, "GET", "/api/v1/health", {"Authorization": "Bearer wrong"})
    assert res.status == 401
    res, data = request(server, "GET", "/api/v1/health", auth())
    assert res.status == 200
    assert json.loads(data)["ok"] is True


def test_bad_host_is_421(server):
    for host in ("evil.example", f"evil.example:{server}", "127.0.0.1:1"):
        res, _ = request(server, "GET", "/api/v1/health", {**auth(), "Host": host})
        assert res.status == 421, host
    res, _ = request(server, "GET", "/index.html", {"Host": "rebind.example"})
    assert res.status == 421


def test_localhost_host_is_allowed(server):
    res, _ = request(server, "GET", "/api/v1/health", {**auth(), "Host": f"localhost:{server}"})
    assert res.status == 200


def test_foreign_origin_is_403(server):
    res, _ = request(server, "GET", "/api/v1/domains", {**auth(), "Origin": "https://evil.example"})
    assert res.status == 403
    res, _ = request(server, "POST", "/api/v1/sessions",
                     {**auth(), "Origin": f"http://127.0.0.1:{server + 1}"}, {"domain": "neurodesk"})
    assert res.status == 403  # same host, different port: a different origin
    res, _ = request(server, "GET", "/", {"Origin": "null"})
    assert res.status == 403
    res, _ = request(server, "GET", "/api/v1/domains", {**auth(), "Origin": f"http://127.0.0.1:{server}"})
    assert res.status == 200


def test_security_headers(server):
    res, _ = request(server, "GET", "/")
    assert res.status == 200
    csp = [d.strip() for d in res.getheader("Content-Security-Policy").split(";")]
    assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert res.getheader("Cache-Control") == "no-store"
    assert res.getheader("X-Content-Type-Options") == "nosniff"
    res, _ = request(server, "GET", "/index.html")
    assert "default-src 'self'" in res.getheader("Content-Security-Policy")
    res, _ = request(server, "GET", "/api/v1/health", auth())
    assert res.getheader("Cache-Control") == "no-store"
    assert res.getheader("X-Content-Type-Options") == "nosniff"
    res, _ = request(server, "GET", "/app.js")
    assert res.getheader("Content-Type").startswith("text/javascript")


def test_static_needs_no_token_and_no_traversal(server):
    res, _ = request(server, "GET", "/app.css")
    assert res.status == 200
    res, _ = request(server, "GET", "/../../tests/mock_api.py")
    assert res.status == 404


# ---------- API behaviour the UI relies on ----------

def test_session_lifecycle(server):
    res, data = request(server, "POST", "/api/v1/sessions", auth(), {"domain": "neurodesk"})
    assert res.status == 202
    s = json.loads(data)
    assert s["state"] == "pulling"
    assert s["url"] is None
    seen = {s["state"]}
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and s["state"] != "ready":
        time.sleep(0.1)
        _, data = request(server, "GET", f"/api/v1/sessions/{s['id']}", auth())
        s = json.loads(data)
        seen.add(s["state"])
    assert s["state"] == "ready"
    assert {"pulling", "starting", "ready"} <= seen
    assert s["url"].startswith(f"http://127.0.0.1:{server}/")
    _, data = request(server, "GET", f"/api/v1/sessions/{s['id']}/logs?tail=2", auth())
    assert len(json.loads(data)["lines"]) == 2
    res, data = request(server, "DELETE", f"/api/v1/sessions/{s['id']}", auth())
    assert res.status == 200
    assert json.loads(data)["state"] == "stopped"


def test_errors_are_readable(server):
    res, data = request(server, "POST", "/api/v1/sessions", auth(),
                        {"domain": "neurodesk", "data_dir": "/no/such/folder"})
    assert res.status == 400
    body = json.loads(data)
    assert body["error"] == "bad_request" and body["message"]
    res, data = request(server, "POST", "/api/v1/sessions", auth(), {"domain": "neurodesk", "engine": "docker"})
    assert res.status == 503  # serve.py maps EngineError to 503
    assert json.loads(data)["error"] == "engine_unavailable"


def test_put_settings_favorites(server):
    res, data = request(server, "PUT", "/api/v1/settings", auth(), {"favorites": ["geodesk"]})
    assert res.status == 200
    assert json.loads(data)["favorites"] == ["geodesk"]


# ---------- static file checks ----------

class _Refs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.refs: list[str] = []
        self.inline_scripts = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "script" and "src" not in a:
            self.inline_scripts += 1
        for key in ("src", "href"):
            if a.get(key):
                self.refs.append(a[key])


def test_index_references_exist():
    p = _Refs()
    p.feed((WEB / "index.html").read_text())
    assert p.refs, "index.html references nothing"
    files = [r for r in p.refs if not r.startswith("#")]
    assert files
    for ref in files:
        assert not re.match(r"^[a-z]+:", ref), ref
        assert (WEB / ref.split("#")[0].split("?")[0]).is_file(), ref
    # Follow ES module imports too.
    for js in WEB.glob("*.js"):
        for imp in re.findall(r"""^\s*import\b[^'"]*['"]([^'"]+)['"]""", js.read_text(), re.M):
            assert (js.parent / imp).resolve().is_file(), f"{js.name} imports {imp}"


def test_no_external_urls_or_inline_code():
    files = [f for f in WEB.rglob("*") if f.is_file()]
    assert files
    for f in files:
        text = f.read_text()
        assert not re.search(r"https?://", text), f"{f.name} contains a URL"
        if f.suffix == ".html":
            assert not re.search(r"\sstyle\s*=", text, re.I), f"{f.name} has an inline style"
            assert not re.search(r"<style\b", text, re.I), f"{f.name} has a <style> block"
            assert not re.search(r"\son[a-z]+\s*=", text, re.I), f"{f.name} has an inline event handler"
            p = _Refs()
            p.feed(text)
            assert p.inline_scripts == 0, f"{f.name} has an inline <script>"
