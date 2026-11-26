from __future__ import annotations

import subprocess

import pytest

from sciget_core import catalog


class FakeRunner:
    """Records argument vectors and answers like Podman, Docker or Apptainer."""

    def __init__(self, rootless: bool = True, running: bool = True, have_image: bool = True):
        self.calls: list[list[str]] = []
        self.envs: list[dict | None] = []
        self.rootless = rootless
        self.running = running
        self.have_image = have_image

    def __call__(self, argv, *, env=None, capture=True, timeout=None):
        argv = list(argv)
        self.calls.append(argv)
        self.envs.append(env)
        out, rc = "", 0
        exe, rest = argv[0], argv[1:]
        if rest == ["--version"]:
            out = {"apptainer": "apptainer version 1.5.5", "docker": "Docker version 27.3.1"}.get(exe, "podman version 5.8.9")
        elif rest[:1] == ["version"]:
            out = "5.8.9"
        elif rest[:1] == ["info"]:
            out = "true" if self.rootless else "[name=seccomp]"
        elif rest[:2] == ["image", "inspect"]:
            rc = 0 if self.have_image else 1
        elif rest[:1] == ["run"]:
            out = "c0ffee"
        elif rest[:1] == ["inspect"]:
            out = "true" if self.running else "false"
        elif rest[:1] == ["logs"]:
            out = "line1\nline2\n"
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")


@pytest.fixture
def domain() -> catalog.Domain:
    return catalog.load()["neurodesk"]


@pytest.fixture
def fake_runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture(autouse=True)
def no_cvmfs(monkeypatch):
    # Tests must not depend on whether the build host has /cvmfs.
    monkeypatch.setattr("sciget_core.host.cvmfs_mounted", lambda repo, root=None: False)


@pytest.fixture(autouse=True)
def only_fake_engines(monkeypatch):
    # Every engine looks installed; FakeRunner answers for it.
    monkeypatch.setattr("shutil.which", lambda exe: f"/usr/bin/{exe}")
