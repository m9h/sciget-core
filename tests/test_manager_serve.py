from __future__ import annotations

import http.client
import json
import threading

import pytest

from sciget_core import manager as manager_mod
from sciget_core.manager import Manager, SessionError
from sciget_core.serve import make_server

from conftest import FakeRunner


@pytest.fixture
def mgr(tmp_path, monkeypatch, fake_runner):
    monkeypatch.setattr(manager_mod, "wait_ready", lambda *a, **k: None)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return Manager(state_dir=tmp_path / "state", config_dir=tmp_path / "conf", runner=fake_runner)


def test_start_list_stop(mgr, fake_runner):
    s = mgr.start({"domain": "neurodesk"})
    assert s.state == "ready" and s.handle == "c0ffee"
    assert [x.id for x in mgr.list()] == [s.id]
    record = (mgr.state_dir / "sessions" / f"{s.id}.json")
    assert record.stat().st_mode & 0o077 == 0
    mgr.stop(s.id)
    assert fake_runner.calls[-1][:2] == ["podman", "stop"]
    assert mgr.get(s.id).state == "stopped"
    mgr.forget(s.id)
    assert mgr.list() == []


def test_pull_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(manager_mod, "wait_ready", lambda *a, **k: None)
    runner = FakeRunner(have_image=False)
    m = Manager(state_dir=tmp_path / "s", config_dir=tmp_path / "c", runner=runner)
    m.start({})
    assert any(c[:2] == ["podman", "pull"] for c in runner.calls)


def test_dead_session_reads_stopped(mgr, fake_runner):
    s = mgr.start({})
    fake_runner.running = False
    assert mgr.get(s.id).state == "stopped"


def test_settings_validation(mgr):
    assert mgr.settings()["engine"] == "auto"
    mgr.update_settings({"favorites": ["neurodesk"], "cvmfs": "off"})
    assert mgr.settings()["cvmfs"] == "off"
    for bad in ({"engine": "lxc"}, {"cvmfs": "maybe"}, {"storage_dir": "relative"}, {"nope": 1}):
        with pytest.raises(SessionError):
            mgr.update_settings(bad)


def test_unknown_session_id(mgr):
    with pytest.raises(SessionError):
        mgr.get("../../etc")


@pytest.fixture
def api(mgr):
    server = make_server(mgr, 0, token="t0ken")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]

    def call(method, path, body=None, token="t0ken", host=None, origin=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        headers = {"Host": host or f"127.0.0.1:{port}"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if origin:
            headers["Origin"] = origin
        data = None
        if body is not None:
            data = json.dumps(body)
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = raw
        return resp.status, dict(resp.getheaders()), payload

    call.port = port
    yield call
    server.shutdown()
    server.server_close()


def test_api_auth_host_origin(api):
    assert api("GET", "/api/v1/health")[0] == 200
    assert api("GET", "/api/v1/health", token=None)[0] == 401
    assert api("GET", "/api/v1/health", token="wrong")[0] == 401
    assert api("GET", "/api/v1/health", host="evil.example:80")[0] == 421
    assert api("GET", "/api/v1/health", origin="https://evil.example")[0] == 403
    assert api("GET", "/api/v1/health", origin=f"http://localhost:{api.port}")[0] == 200


def test_api_sessions_flow(api):
    status, _, domains = api("GET", "/api/v1/domains")
    assert status == 200 and domains[0]["name"] == "neurodesk"
    status, _, s = api("POST", "/api/v1/sessions", {"domain": "neurodesk"})
    assert status == 202 and s["state"] in ("pulling", "starting", "ready")
    status, _, got = api("GET", f"/api/v1/sessions/{s['id']}")
    assert status == 200 and got["url"].startswith("http://127.0.0.1:")
    assert api("POST", "/api/v1/sessions", {"privileged": True})[0] == 400
    assert api("GET", "/api/v1/sessions/zzzzzz")[0] == 404
    assert api("DELETE", f"/api/v1/sessions/{s['id']}")[2]["state"] == "stopped"


def test_api_errors_are_json(api):
    status, headers, body = api("GET", "/api/v1/nope")
    assert status == 404 and body["error"] == "not_found"
    assert headers["Cache-Control"] == "no-store"
    status, _, body = api("PUT", "/api/v1/settings", {"engine": "lxc"})
    assert status == 400 and "engine" in body["message"]


def test_static_traversal_blocked(api):
    assert api("GET", "/../pyproject.toml", token=None)[0] == 404
    assert api("GET", "/.hidden", token=None)[0] == 404
