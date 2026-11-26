from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from sciget_core import engines, plan
from sciget_core.engines.apptainer import Apptainer
from sciget_core.engines.oci import Docker, Podman

from conftest import FakeRunner


def make_plan(domain, kind="podman", **kw):
    return plan.build(domain, plan.Options(domain="neurodesk", **kw), kind, rootless=kind == "podman",
                      port=40000, cvmfs_mounted=False, home=Path("/home/u"))


def test_podman_argv(domain, fake_runner):
    p = make_plan(domain, gpu=True)
    argv = Podman(fake_runner, selinux=True).argv(p)
    assert argv[:4] == ["podman", "run", "--detach", "--rm"]
    assert "--privileged" not in argv
    assert ["--publish", "127.0.0.1:40000:8888"] == argv[argv.index("--publish"):argv.index("--publish") + 2]
    assert "nvidia.com/gpu=all" in argv
    assert "label=disable" in argv
    assert "--cgroups=disabled" not in argv
    # Token travels by name only.
    assert "JUPYTER_TOKEN" in argv and p.token not in " ".join(argv)
    assert argv[-len(p.command):] == p.command


def test_privileged_is_explicit(domain, fake_runner):
    argv = Podman(fake_runner, selinux=True).argv(make_plan(domain, privileged=True))
    assert "--privileged" in argv and "label=disable" not in argv
    assert "/dev/fuse" not in argv


def test_docker_gpu_and_ids(domain, fake_runner):
    argv = Docker(fake_runner, selinux=False).argv(make_plan(domain, kind="docker", gpu=True))
    assert ["--gpus", "all"] == argv[argv.index("--gpus"):argv.index("--gpus") + 2]
    assert f"NB_UID={os.getuid()}" in argv
    assert "--userns" not in argv


def test_start_passes_token_in_environment(domain, fake_runner, tmp_path):
    p = make_plan(domain)
    handle = Podman(fake_runner, selinux=False).start(p, tmp_path / "log")
    assert handle == "c0ffee"
    assert fake_runner.envs[-1]["JUPYTER_TOKEN"] == p.token


def test_podman_docker_alias_is_not_docker():
    runner = FakeRunner()
    original = runner.__call__

    def alias(argv, **kw):
        if argv[:2] == ["docker", "--version"]:
            return subprocess.CompletedProcess(argv, 0, stdout="podman version 5.8.9", stderr="")
        return original(argv, **kw)

    assert not Docker(alias).info().available


def test_choose_auto_prefers_podman(fake_runner):
    engine, info = engines.choose("auto", fake_runner)
    assert engine.kind == "podman" and info.rootless


def test_choose_reports_missing(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda exe: None)
    with pytest.raises(engines.EngineError, match="Install Podman"):
        engines.choose("auto")


def test_apptainer_env_not_argv(domain, fake_runner):
    p = make_plan(domain, kind="apptainer")
    app = Apptainer(fake_runner)
    argv = app.argv(p)
    assert argv[:3] == ["apptainer", "exec", "--cleanenv"]
    env = app._env(p)
    assert env["APPTAINERENV_JUPYTER_TOKEN"] == p.token
    assert p.token not in " ".join(argv)


def test_apptainer_stop_only_signals_own_session(tmp_path):
    # A process that carries a different session id must not be signalled.
    proc = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(30)"],
                            env={**os.environ, "SCIGET_SESSION": "other"}, start_new_session=True)
    try:
        app = Apptainer(FakeRunner())
        handle = f"pid:{proc.pid}:mine1234"
        assert not app.is_running(handle)
        app.stop(handle)
        assert proc.poll() is None
        own = f"pid:{proc.pid}:other"
        assert app.is_running(own)
        app.stop(own)
        assert proc.wait(timeout=10) is not None
    finally:
        if proc.poll() is None:
            proc.kill()
