"""Facts about the host that change how a session is launched."""

from __future__ import annotations

import os
import socket
from pathlib import Path


def free_port() -> int:
    """A TCP port on 127.0.0.1 that is free right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def cvmfs_mounted(repository: str, root: Path = Path("/cvmfs")) -> bool:
    """True if the CVMFS repository is mounted (or automounts) on the host.

    Listing the directory triggers autofs, so an automounted repository counts.
    """
    repo = root / repository
    try:
        return repo.is_dir() and any(repo.iterdir())
    except OSError:
        return False


def uid() -> int:
    return os.getuid()


def gid() -> int:
    return os.getgid()
