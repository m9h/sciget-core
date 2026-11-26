"""The interface every container engine implements."""

from __future__ import annotations

import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from sciget_core.plan import LaunchPlan

# A runner executes an argument vector, never a shell string. Tests replace it.
Runner = Callable[..., subprocess.CompletedProcess]


def default_runner(
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    capture: bool = True,
    timeout: float | None = 120,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(argv),
        env=dict(env) if env is not None else None,
        capture_output=capture,
        text=True,
        timeout=timeout,
        check=False,
    )


class EngineError(RuntimeError):
    """The engine refused or failed an operation."""


@dataclass
class EngineInfo:
    kind: str
    available: bool
    rootless: bool = False
    version: str | None = None

    def to_json(self) -> dict:
        return {
            "kind": self.kind,
            "available": self.available,
            "rootless": self.rootless,
            "version": self.version,
        }


class Engine(ABC):
    kind: str

    def __init__(self, runner: Runner | None = None):
        self.run = runner or default_runner

    @abstractmethod
    def info(self) -> EngineInfo: ...

    @abstractmethod
    def argv(self, plan: LaunchPlan) -> list[str]:
        """The exact command that start() runs. Contains no secrets."""

    @abstractmethod
    def has_image(self, image: str) -> bool: ...

    @abstractmethod
    def pull(self, image: str) -> None: ...

    @abstractmethod
    def start(self, plan: LaunchPlan, log_path: Path) -> str:
        """Start the session in the background and return a handle for it."""

    @abstractmethod
    def is_running(self, handle: str) -> bool: ...

    @abstractmethod
    def stop(self, handle: str) -> None: ...

    @abstractmethod
    def logs(self, handle: str, log_path: Path, tail: int = 200) -> list[str]: ...
