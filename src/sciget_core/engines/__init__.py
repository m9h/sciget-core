"""Container engines and how one is chosen."""

from __future__ import annotations

from sciget_core.engines.apptainer import Apptainer
from sciget_core.engines.base import Engine, EngineError, EngineInfo, Runner
from sciget_core.engines.oci import Docker, Podman

# Preference order for "auto": rootless-capable Podman first, then Docker, then
# Apptainer (HPC).
KINDS = ("podman", "docker", "apptainer")
_CLASSES = {"podman": Podman, "docker": Docker, "apptainer": Apptainer}


def get(kind: str, runner: Runner | None = None) -> Engine:
    try:
        return _CLASSES[kind](runner)
    except KeyError:
        raise EngineError(f"Unknown engine {kind!r}; choose one of {', '.join(KINDS)}.") from None


def detect(runner: Runner | None = None) -> list[EngineInfo]:
    return [get(kind, runner).info() for kind in KINDS]


def choose(requested: str, runner: Runner | None = None) -> tuple[Engine, EngineInfo]:
    """The engine to use: the requested one, or the first available for "auto"."""
    kinds = KINDS if requested == "auto" else (requested,)
    for kind in kinds:
        engine = get(kind, runner)
        info = engine.info()
        if info.available:
            return engine, info
    if requested == "auto":
        raise EngineError(
            "No container engine found. Install Podman (sudo dnf install podman) "
            "or Apptainer, then try again."
        )
    raise EngineError(f"{requested} is not installed or not running.")


__all__ = ["Engine", "EngineError", "EngineInfo", "KINDS", "choose", "detect", "get"]
