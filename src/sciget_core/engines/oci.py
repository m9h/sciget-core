"""Podman and Docker, driven through their command-line clients.

The CLIs are used rather than the REST APIs because the Podman API needs the
user's podman.socket service, which is not enabled by default, and an argument
vector already avoids the quoting problems of shell strings.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from sciget_core.engines.base import Engine, EngineError, EngineInfo
from sciget_core.plan import LaunchPlan, Mount

# docker-stacks images run as jovyan, UID 1000, group users (GID 100).
_JOVYAN = "uid=1000,gid=100"


def _selinux_enabled() -> bool:
    return Path("/sys/fs/selinux/enforce").exists()


def _mount_arg(m: Mount) -> str:
    fields = [f"type={m.kind}", f"source={m.source}", f"target={m.target}"]
    if m.readonly:
        fields.append("readonly")
    if m.propagation:
        fields.append(f"bind-propagation={m.propagation}")
    return ",".join(fields)


class OciEngine(Engine):
    exe: str

    def __init__(self, runner=None, selinux: bool | None = None):
        super().__init__(runner)
        self.selinux = _selinux_enabled() if selinux is None else selinux

    def _rootless(self) -> bool:
        raise NotImplementedError

    def _version(self) -> str | None:
        raise NotImplementedError

    def _gpu_args(self) -> list[str]:
        raise NotImplementedError

    def info(self) -> EngineInfo:
        if shutil.which(self.exe) is None:
            return EngineInfo(self.kind, False)
        version = self._version()
        if version is None:
            return EngineInfo(self.kind, False)
        return EngineInfo(self.kind, True, self._rootless(), version)

    def argv(self, plan: LaunchPlan) -> list[str]:
        args = [
            self.exe, "run", "--detach", "--rm",
            "--name", plan.name,
            "--shm-size", plan.shm_size,
            "--publish", f"127.0.0.1:{plan.host_port}:{plan.container_port}",
            "--add-host", "host.docker.internal:host-gateway",
        ]
        for key, value in sorted(plan.labels.items()):
            args += ["--label", f"{key}={value}"]
        for key, value in sorted(plan.env.items()):
            args += ["--env", f"{key}={value}"]
        for key in sorted(plan.secret_env):
            # Name only: the client copies the value from its own environment.
            args += ["--env", key]
        for m in plan.mounts:
            args += ["--mount", _mount_arg(m)]
        for dev in plan.devices:
            args += ["--device", dev]
        for cap in plan.cap_add:
            args += ["--cap-add", cap]
        if plan.user:
            args += ["--user", plan.user]
        if plan.privileged:
            args.append("--privileged")
        elif self.selinux:
            # Bind mounts of the user's folders and of /cvmfs cannot be
            # relabelled, so confinement by SELinux label is turned off for
            # this container only. Namespaces, capabilities and seccomp stay.
            args += ["--security-opt", "label=disable"]
        if plan.mac_address:
            args += ["--mac-address", plan.mac_address]
        if plan.gpu:
            args += self._gpu_args()
        if plan.rootless_keep_id:
            args += ["--userns", f"keep-id:{_JOVYAN}"]
        return args + [plan.image, *plan.command]

    def has_image(self, image: str) -> bool:
        return self.run([self.exe, "image", "inspect", image]).returncode == 0

    def pull(self, image: str) -> None:
        result = self.run([self.exe, "pull", image], capture=False, timeout=None)
        if result.returncode != 0:
            raise EngineError(f"{self.exe} could not pull {image}.")

    def start(self, plan: LaunchPlan, log_path: Path) -> str:
        env = {**os.environ, **plan.secret_env}
        result = self.run(self.argv(plan), env=env)
        if result.returncode != 0:
            raise EngineError((result.stderr or "").strip() or f"{self.exe} run failed.")
        return (result.stdout or "").strip() or plan.name

    def is_running(self, handle: str) -> bool:
        result = self.run([self.exe, "inspect", "--format", "{{.State.Running}}", handle])
        return result.returncode == 0 and (result.stdout or "").strip() == "true"

    def stop(self, handle: str) -> None:
        # The container was started with --rm, so stopping it also removes it.
        result = self.run([self.exe, "stop", "--time", "10", handle], timeout=60)
        if result.returncode != 0 and self.is_running(handle):
            raise EngineError((result.stderr or "").strip() or f"{self.exe} stop failed.")

    def logs(self, handle: str, log_path: Path, tail: int = 200) -> list[str]:
        result = self.run([self.exe, "logs", "--tail", str(tail), handle])
        text = (result.stdout or "") + (result.stderr or "")
        return text.splitlines()[-tail:]


class Podman(OciEngine):
    kind = "podman"
    exe = "podman"

    def _version(self) -> str | None:
        result = self.run([self.exe, "version", "--format", "{{.Client.Version}}"])
        if result.returncode != 0:
            return None
        return (result.stdout or "").strip() or None

    def _rootless(self) -> bool:
        result = self.run([self.exe, "info", "--format", "{{.Host.Security.Rootless}}"])
        return (result.stdout or "").strip() == "true"

    def _gpu_args(self) -> list[str]:
        # CDI device names, generated by nvidia-ctk cdi generate.
        return ["--device", "nvidia.com/gpu=all"]


class Docker(OciEngine):
    kind = "docker"
    exe = "docker"

    def info(self) -> EngineInfo:
        # podman-docker installs a "docker" that is really Podman; that is not
        # a second engine.
        if shutil.which(self.exe) is not None:
            result = self.run([self.exe, "--version"])
            if (result.stdout or "").lower().startswith("podman"):
                return EngineInfo(self.kind, False)
        return super().info()

    def _version(self) -> str | None:
        # Asks the daemon, so a stopped daemon reads as unavailable.
        result = self.run([self.exe, "version", "--format", "{{.Server.Version}}"])
        if result.returncode != 0:
            return None
        return (result.stdout or "").strip() or None

    def _rootless(self) -> bool:
        result = self.run([self.exe, "info", "--format", "{{.SecurityOptions}}"])
        return "rootless" in (result.stdout or "")

    def _gpu_args(self) -> list[str]:
        return ["--gpus", "all"]
