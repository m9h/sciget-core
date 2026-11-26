"""Where sciget-core keeps its configuration and state."""

from __future__ import annotations

import os
from pathlib import Path

import platformdirs

APP = "sciget"


def config_dir() -> Path:
    return Path(platformdirs.user_config_dir(APP))


def state_dir() -> Path:
    return Path(platformdirs.user_state_dir(APP))


def private_dir(path: Path) -> Path:
    """Create path (and parents) and make it readable by the owner only."""
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def write_private(path: Path, text: str) -> None:
    """Write text atomically to a file that only the owner can read."""
    private_dir(path.parent)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)
