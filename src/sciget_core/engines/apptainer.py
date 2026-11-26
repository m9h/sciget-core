"""Apptainer, for HPC systems and hosts without Podman or Docker.

Apptainer has no detached mode that runs an arbitrary command, so the session
is a background `apptainer exec` in its own process group, with its output in
a log file. Stopping signals only that process group, and only after checking
that the PID still belongs to this session, never by process name.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
from pathlib import Path

from sciget_core.engines.base import Engine, EngineError, EngineInfo
from sciget_core.plan import LaunchPlan

_SESSION_ENV = "SCIGET_SESSION"


def _pid(handle: str) -> int:
    kind, _, rest = handle.partition(":")
    if kind != "pid" or not rest.isdigit():
        raise EngineError(f"{handle!r} is not an Apptainer session handle.")
    return int(rest)


def _session_of(pid: int) -> str | None:
    """The sciget session a live process belongs to, or None."""
    try:
        environ = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return None
    for item in environ.split(b"\0"):
        if item.startswith(_SESSION_ENV.encode() + b"="):
            return item.split(b"=", 1)[1].decode(errors="replace")
    return None


class Apptainer(Engine):
    kind = "apptainer"
    exe = "apptainer"

    def info(self) -> EngineInfo:
        if shutil.which(self.exe) is None:
            return EngineInfo(self.kind, False)
        result = self.run([self.exe, "--version"])
        if result.returncode != 0:
            return EngineInfo(self.kind, False)
        version = (result.stdout or "").strip().removeprefix("apptainer version ").strip()
        # Apptainer runs as the calling user unless installed setuid.
        return EngineInfo(self.kind, True, True, version or None)

    def argv(self, plan: LaunchPlan) -> list[str]:
        args = [self.exe, "exec", "--cleanenv"]
        for m in plan.mounts:
            if m.kind != "bind":
                continue
            args += ["--bind", f"{m.source}:{m.target}" + (":ro" if m.readonly else "")]
        if plan.gpu:
            args.append("--nv")
        return args + [f"docker://{plan.image}", *plan.command]

    def _env(self, plan: LaunchPlan) -> dict[str, str]:
        # --cleanenv drops the caller's environment; APPTAINERENV_* is how
        # variables are passed in without putting them on the command line.
        env = dict(os.environ)
        for key, value in {**plan.env, **plan.secret_env}.items():
            env[f"APPTAINERENV_{key}"] = value
        env[_SESSION_ENV] = plan.session_id
        return env

    def has_image(self, image: str) -> bool:
        # Apptainer converts and caches the image on first use; it has no cheap
        # "is it cached" query, so report not present and let pull() do it.
        return False

    def pull(self, image: str) -> None:
        result = self.run([self.exe, "exec", f"docker://{image}", "true"], capture=False, timeout=None)
        if result.returncode != 0:
            raise EngineError(f"Apptainer could not fetch {image}.")

    def start(self, plan: LaunchPlan, log_path: Path) -> str:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            proc = subprocess.Popen(
                self.argv(plan),
                env=self._env(plan),
                stdin=subprocess.DEVNULL,
                stdout=fd,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            os.close(fd)
        return f"pid:{proc.pid}:{plan.session_id}"

    def _owned_pid(self, handle: str) -> int | None:
        parts = handle.split(":")
        if len(parts) != 3:
            raise EngineError(f"{handle!r} is not an Apptainer session handle.")
        pid = _pid(f"{parts[0]}:{parts[1]}")
        return pid if _session_of(pid) == parts[2] else None

    def is_running(self, handle: str) -> bool:
        return self._owned_pid(handle) is not None

    def stop(self, handle: str) -> None:
        pid = self._owned_pid(handle)
        if pid is None:
            return
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    def logs(self, handle: str, log_path: Path, tail: int = 200) -> list[str]:
        try:
            return log_path.read_text(errors="replace").splitlines()[-tail:]
        except OSError:
            return []
