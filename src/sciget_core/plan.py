"""Turn a domain and the user's choices into an engine-neutral launch plan.

Engines render a LaunchPlan into an argument vector (never a shell string), so
paths and values with spaces or shell metacharacters are passed through
unchanged and cannot be interpreted.

Differences from upstream Sciget App 2.2.0, on purpose:
- no --privileged by default. CVMFS is bind-mounted from the host when it is
  mounted there; otherwise the container gets /dev/fuse and CAP_SYS_ADMIN only
  (mode "container"). --privileged stays available as an explicit option.
- no fixed IP or MAC address. A MAC address can still be set per session for
  software licensed to one.
- the Jupyter token is passed in the environment, not on the command line, so
  it does not show up in `ps`.
- cgroups stay enabled under Podman.
- each session gets its own container name, port and token, so several
  sessions and several users on one host do not collide.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from sciget_core import host
from sciget_core.catalog import Domain

CVMFS_MODES = ("auto", "host", "container", "off")
_MAC = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
_SESSION_ID = re.compile(r"^[a-z0-9]{4,32}$")

# Labels let us find our own containers again without remembering anything else.
LABEL_SESSION = "io.sciget.session"
LABEL_DOMAIN = "io.sciget.domain"
LABEL_UID = "io.sciget.uid"


class PlanError(ValueError):
    """The requested combination cannot be launched."""


@dataclass(frozen=True)
class Mount:
    source: str
    target: str
    kind: str = "bind"  # "bind" or "volume"
    readonly: bool = False
    propagation: str | None = None


@dataclass
class Options:
    domain: str
    version: str | None = None
    data_dir: Path | None = None
    storage_dir: Path | None = None
    cvmfs: str = "auto"
    gpu: bool = False
    sudo: bool = True
    privileged: bool = False
    mac_address: str | None = None


@dataclass
class LaunchPlan:
    session_id: str
    name: str
    image: str
    command: list[str]
    host_port: int
    container_port: int
    cvmfs: str
    env: dict[str, str] = field(default_factory=dict)
    # Passed by name only on the command line; values travel in the environment.
    secret_env: dict[str, str] = field(default_factory=dict)
    mounts: list[Mount] = field(default_factory=list)
    devices: list[str] = field(default_factory=list)
    cap_add: list[str] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)
    shm_size: str = "1g"
    user: str | None = "root"
    privileged: bool = False
    gpu: bool = False
    mac_address: str | None = None
    rootless_keep_id: bool = False

    @property
    def token(self) -> str:
        return self.secret_env["JUPYTER_TOKEN"]


def new_session_id() -> str:
    return secrets.token_hex(4)


def _check_path(path: Path, what: str) -> str:
    text = str(path)
    # "," splits --mount fields; control characters have no business in a path.
    if "," in text or any(ord(c) < 32 for c in text):
        raise PlanError(f"The {what} path {text!r} contains a comma or control character.")
    return text


def resolve_cvmfs(requested: str, domain: Domain, engine_kind: str, mounted: bool) -> str:
    if requested not in CVMFS_MODES:
        raise PlanError(f"CVMFS mode must be one of {', '.join(CVMFS_MODES)}.")
    if domain.cvmfs_repository is None:
        return "off"
    if requested == "auto":
        if mounted:
            return "host"
        return "off" if engine_kind == "apptainer" else "container"
    if requested == "host" and not mounted:
        raise PlanError(
            f"CVMFS mode 'host' needs /cvmfs/{domain.cvmfs_repository} mounted on this "
            "computer. Install the cvmfs client and its Neurodesk config, or choose 'off'."
        )
    if requested == "container" and engine_kind == "apptainer":
        raise PlanError("Apptainer cannot mount CVMFS inside the container; use 'host' or 'off'.")
    return requested


def build(
    domain: Domain,
    opts: Options,
    engine_kind: str,
    *,
    rootless: bool = False,
    session_id: str | None = None,
    port: int | None = None,
    cvmfs_mounted: bool | None = None,
    home: Path | None = None,
) -> LaunchPlan:
    session_id = session_id or new_session_id()
    if not _SESSION_ID.match(session_id):
        raise PlanError(f"Invalid session id {session_id!r}.")
    if opts.mac_address is not None and not _MAC.match(opts.mac_address.lower()):
        raise PlanError(f"{opts.mac_address!r} is not a MAC address like 02:42:ac:11:00:02.")
    if cvmfs_mounted is None:
        cvmfs_mounted = bool(domain.cvmfs_repository) and host.cvmfs_mounted(domain.cvmfs_repository)
    cvmfs = resolve_cvmfs(opts.cvmfs, domain, engine_kind, cvmfs_mounted)

    home = home or Path.home()
    storage_dir = opts.storage_dir or home / domain.storage_mount
    storage_target = f"/{domain.storage_mount}"
    apptainer = engine_kind == "apptainer"
    host_port = port or host.free_port()
    # Under Apptainer the server shares the host network, so it listens on the
    # host port itself, on loopback only.
    server_port = host_port if apptainer else domain.container_port

    command = [
        "start.sh", "jupyter", "lab",
        "--ServerApp.password=",
        "--no-browser",
        "--expose-app-in-browser",
        f"--ServerApp.port={server_port}",
        "--LabApp.quit_button=False",
        *domain.server_args,
    ]
    if apptainer:
        command.append("--ServerApp.ip=127.0.0.1")

    env = {
        "NEURODESKTOP_VERSION": opts.version or domain.default_version,
        # Upstream's switch: "false" streams tools from CVMFS, "true" downloads
        # them. With "false" the image's before_notebook.sh first looks for
        # /cvmfs/neurodesk.ardc.edu.au mounted from outside and only falls back
        # to its own FUSE mount, so host mode needs "false" too.
        "CVMFS_DISABLE": "true" if cvmfs == "off" else "false",
        "NEURODESKTOP_CVMFS_STARTUP_MODE": "eager",
        "NEURODESK_STORAGE_MOUNT": domain.storage_mount,
        "OLLAMA_HOST": "http://host.docker.internal:11434",
    }
    if opts.sudo and not apptainer:
        env["GRANT_SUDO"] = "yes"

    keep_id = engine_kind == "podman" and rootless
    if not apptainer and not keep_id:
        # Rootful engines: start.sh switches to the caller's IDs so files on
        # bind mounts belong to them.
        env["NB_UID"] = str(host.uid())
        env["NB_GID"] = str(host.gid())

    mounts = [Mount(_check_path(storage_dir, "storage"), storage_target)]
    if not apptainer:
        mounts.append(Mount(domain.volume_mount, "/home/jovyan", kind="volume"))
    if opts.data_dir is not None:
        if not opts.data_dir.is_dir():
            raise PlanError(f"The data folder {opts.data_dir} does not exist.")
        mounts.append(Mount(_check_path(opts.data_dir.resolve(), "data"), "/data"))
    devices: list[str] = []
    cap_add: list[str] = []
    if cvmfs == "host":
        mounts.append(Mount("/cvmfs", "/cvmfs", readonly=True, propagation="rslave"))
    elif cvmfs == "container" and not opts.privileged:
        devices.append("/dev/fuse")
        cap_add.append("SYS_ADMIN")

    return LaunchPlan(
        session_id=session_id,
        name=f"sciget-{domain.name}-{host.uid()}-{session_id}",
        image=domain.image(opts.version),
        command=command,
        host_port=host_port,
        container_port=domain.container_port,
        cvmfs=cvmfs,
        env=env,
        secret_env={"JUPYTER_TOKEN": secrets.token_urlsafe(32)},
        mounts=mounts,
        devices=devices,
        cap_add=cap_add,
        labels={
            LABEL_SESSION: session_id,
            LABEL_DOMAIN: domain.name,
            LABEL_UID: str(host.uid()),
        },
        user=None if apptainer else "root",
        privileged=opts.privileged,
        gpu=opts.gpu,
        mac_address=opts.mac_address.lower() if opts.mac_address else None,
        rootless_keep_id=keep_id,
    )
