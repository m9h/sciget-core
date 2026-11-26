from __future__ import annotations

from pathlib import Path

import pytest

from sciget_core import catalog, plan


def test_builtin_neurodesk(domain):
    assert domain.registry == "docker.io/vnmd/neurodesktop"
    assert domain.cvmfs_repository == "neurodesk.ardc.edu.au"
    assert domain.image("2026-01-01") == "docker.io/vnmd/neurodesktop:2026-01-01"


def test_upstream_file_loads_unchanged(tmp_path):
    # The upstream v2.2.0 file, without our additions.
    (tmp_path / "other.yml").write_text(
        "title: other\ndefaultVersion: '1.0'\nregistry: ghcr.io/example/img\n"
        "containerName: x\nvolumeMount: other-home\ndefaultStorageMount: other-storage\n"
        "remoteUrl:\n  - https://example.org/\n"
    )
    domains = catalog.load(tmp_path)
    assert domains["other"].remote_urls == ("https://example.org/",)
    assert domains["other"].cvmfs_repository is None


@pytest.mark.parametrize("key,value", [
    ("registry", "docker.io/x; rm -rf /"),
    ("defaultStorageMount", "../etc"),
    ("defaultVersion", "1.0 --privileged"),
])
def test_unsafe_values_rejected(key, value):
    data = {"registry": "docker.io/a/b", "defaultVersion": "1", "volumeMount": "v", "defaultStorageMount": "s"}
    data[key] = value
    with pytest.raises(catalog.CatalogError):
        catalog.parse_domain("bad", data)


def test_remote_urls_must_be_https():
    data = {"registry": "docker.io/a/b", "defaultVersion": "1", "volumeMount": "v",
            "defaultStorageMount": "s", "remoteUrl": ["http://insecure.example/"]}
    with pytest.raises(catalog.CatalogError):
        catalog.parse_domain("bad", data)


def build(domain, engine="podman", **kw):
    rootless = kw.pop("rootless", True)
    mounted = kw.pop("mounted", False)
    return plan.build(domain, plan.Options(domain="neurodesk", **kw), engine,
                      rootless=rootless, port=40000, cvmfs_mounted=mounted, home=Path("/home/u"))


def test_default_is_not_privileged(domain):
    p = build(domain)
    assert not p.privileged
    assert p.cvmfs == "container"
    assert p.devices == ["/dev/fuse"] and p.cap_add == ["SYS_ADMIN"]
    assert p.env["CVMFS_DISABLE"] == "false"


def test_host_cvmfs_bind_mounted_read_only(domain):
    p = build(domain, mounted=True)
    assert p.cvmfs == "host"
    cv = [m for m in p.mounts if m.target == "/cvmfs"][0]
    assert cv.readonly and cv.propagation == "rslave"
    assert not p.devices and not p.cap_add
    # The image looks for an outside mount before trying FUSE, so streaming stays on.
    assert p.env["CVMFS_DISABLE"] == "false"


def test_host_mode_needs_a_mount(domain):
    with pytest.raises(plan.PlanError, match="cvmfs client"):
        build(domain, cvmfs="host")


def test_off_downloads(domain):
    p = build(domain, cvmfs="off")
    assert p.env["CVMFS_DISABLE"] == "true"
    assert not any(m.target == "/cvmfs" for m in p.mounts)


def test_token_not_in_command_or_env(domain):
    p = build(domain)
    assert p.token and p.token not in " ".join(p.command)
    assert p.token not in p.env.values()


def test_sessions_do_not_collide(domain):
    a, b = build(domain), build(domain)
    assert a.name != b.name and a.token != b.token
    assert a.mac_address is None


def test_rootless_podman_keeps_id(domain):
    p = build(domain)
    assert p.rootless_keep_id and "NB_UID" not in p.env
    q = build(domain, engine="docker", rootless=False)
    assert not q.rootless_keep_id and "NB_UID" in q.env


def test_apptainer(domain):
    p = build(domain, engine="apptainer")
    assert p.cvmfs == "off"
    assert "--ServerApp.ip=127.0.0.1" in p.command
    assert "--ServerApp.port=40000" in p.command
    assert p.user is None and "GRANT_SUDO" not in p.env
    with pytest.raises(plan.PlanError):
        build(domain, engine="apptainer", cvmfs="container")


def test_bad_paths_and_mac(domain, tmp_path):
    bad = tmp_path / "a,b"
    bad.mkdir()
    with pytest.raises(plan.PlanError, match="comma"):
        build(domain, data_dir=bad)
    with pytest.raises(plan.PlanError, match="does not exist"):
        build(domain, data_dir=tmp_path / "missing")
    with pytest.raises(plan.PlanError, match="MAC"):
        build(domain, mac_address="nope")
    assert build(domain, mac_address="02:42:AC:11:00:02").mac_address == "02:42:ac:11:00:02"
