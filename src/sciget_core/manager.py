"""Sessions: start, track, stop. The CLI and the HTTP service both use this."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from sciget_core import catalog, engines, paths, plan
from sciget_core.engines import Engine, EngineError

STATES = ("pulling", "starting", "ready", "stopped", "error")
SETTINGS_DEFAULTS = {
    "engine": "auto",
    "cvmfs": "auto",
    "gpu": False,
    "storage_dir": None,
    "favorites": [],
}


class SessionError(RuntimeError):
    """A request about a session cannot be met."""


@dataclass
class Session:
    id: str
    domain: str
    engine: str
    version: str
    port: int
    token: str
    cvmfs: str
    gpu: bool
    data_dir: str | None
    created: str
    state: str = "pulling"
    handle: str | None = None
    error: str | None = None
    argv: list[str] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/lab?token={self.token}"

    def to_json(self, include_url: bool = True) -> dict:
        data = {
            "id": self.id,
            "domain": self.domain,
            "engine": self.engine,
            "version": self.version,
            "state": self.state,
            "created": self.created,
            "port": self.port,
            "data_dir": self.data_dir,
            "cvmfs": self.cvmfs,
            "gpu": self.gpu,
            "error": self.error,
        }
        if include_url:
            data["url"] = self.url
        return data


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def wait_ready(port: int, token: str, timeout: float, alive: Callable[[], bool]) -> None:
    """Return once Jupyter answers on port, or raise SessionError."""
    deadline = time.monotonic() + timeout
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/status",
        headers={"Authorization": f"token {token}"},
    )
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
            pass
        if not alive():
            raise SessionError("The session stopped before Jupyter was ready. See its logs.")
        time.sleep(1)
    raise SessionError(f"Jupyter did not answer within {int(timeout)} seconds.")


class Manager:
    def __init__(
        self,
        state_dir: Path | None = None,
        config_dir: Path | None = None,
        runner: engines.Runner | None = None,
        domains: dict[str, catalog.Domain] | None = None,
        ready_timeout: float = 900,
    ):
        self.state_dir = state_dir or paths.state_dir()
        self.config_dir = config_dir or paths.config_dir()
        self.runner = runner
        self._domains = domains
        self.ready_timeout = ready_timeout
        self._lock = threading.Lock()

    # Domains, engines, settings

    def domains(self) -> dict[str, catalog.Domain]:
        if self._domains is None:
            self._domains = catalog.load(self.config_dir / "domains")
        return self._domains

    def domain(self, name: str) -> catalog.Domain:
        try:
            return self.domains()[name]
        except KeyError:
            raise SessionError(f"There is no domain called {name!r}.") from None

    def engines(self) -> list[engines.EngineInfo]:
        return engines.detect(self.runner)

    def _settings_path(self) -> Path:
        return self.config_dir / "settings.json"

    def settings(self) -> dict:
        data = dict(SETTINGS_DEFAULTS)
        try:
            data.update(json.loads(self._settings_path().read_text()))
        except (OSError, ValueError):
            pass
        return {k: data[k] for k in SETTINGS_DEFAULTS}

    def update_settings(self, changes: dict) -> dict:
        unknown = set(changes) - set(SETTINGS_DEFAULTS)
        if unknown:
            raise SessionError(f"Unknown settings: {', '.join(sorted(unknown))}.")
        if "engine" in changes and changes["engine"] not in ("auto", *engines.KINDS):
            raise SessionError("engine must be auto, podman, docker or apptainer.")
        if "cvmfs" in changes and changes["cvmfs"] not in plan.CVMFS_MODES:
            raise SessionError("cvmfs must be auto, host, container or off.")
        if "gpu" in changes and not isinstance(changes["gpu"], bool):
            raise SessionError("gpu must be true or false.")
        if "storage_dir" in changes and changes["storage_dir"] is not None:
            if not isinstance(changes["storage_dir"], str) or not Path(changes["storage_dir"]).is_absolute():
                raise SessionError("storage_dir must be an absolute path.")
        if "favorites" in changes:
            fav = changes["favorites"]
            if not (isinstance(fav, list) and all(isinstance(f, str) for f in fav)):
                raise SessionError("favorites must be a list of domain names.")
        with self._lock:
            data = self.settings()
            data.update(changes)
            paths.write_private(self._settings_path(), json.dumps(data, indent=2))
        return data

    # Session records

    def _session_path(self, session_id: str) -> Path:
        return self.state_dir / "sessions" / f"{session_id}.json"

    def log_path(self, session_id: str) -> Path:
        return self.state_dir / "sessions" / f"{session_id}.log"

    def _save(self, session: Session) -> None:
        with self._lock:
            paths.write_private(self._session_path(session.id), json.dumps(asdict(session), indent=2))

    def _load(self, session_id: str) -> Session:
        if not plan._SESSION_ID.match(session_id):
            raise SessionError(f"There is no session {session_id!r}.")
        try:
            return Session(**json.loads(self._session_path(session_id).read_text()))
        except (OSError, ValueError, TypeError):
            raise SessionError(f"There is no session {session_id!r}.") from None

    def _engine(self, session: Session) -> Engine:
        return engines.get(session.engine, self.runner)

    def _refresh(self, session: Session) -> Session:
        if session.state in ("starting", "ready") and session.handle:
            if not self._engine(session).is_running(session.handle):
                session.state = "stopped"
                self._save(session)
        return session

    def get(self, session_id: str) -> Session:
        return self._refresh(self._load(session_id))

    def list(self) -> list[Session]:
        folder = self.state_dir / "sessions"
        if not folder.is_dir():
            return []
        sessions = []
        for path in sorted(folder.glob("*.json")):
            try:
                sessions.append(self._refresh(self._load(path.stem)))
            except SessionError:
                continue
        return sorted(sessions, key=lambda s: s.created, reverse=True)

    # Lifecycle

    def prepare(self, request: dict) -> tuple[Engine, plan.LaunchPlan, Session]:
        """Validate a start request and plan it, without starting anything."""
        settings = self.settings()
        domain = self.domain(request.get("domain") or "neurodesk")
        engine, info = engines.choose(request.get("engine") or settings["engine"], self.runner)
        data_dir = request.get("data_dir")
        storage_dir = settings["storage_dir"]
        opts = plan.Options(
            domain=domain.name,
            version=request.get("version") or None,
            data_dir=Path(data_dir) if data_dir else None,
            storage_dir=Path(storage_dir) if storage_dir else None,
            cvmfs=request.get("cvmfs") or settings["cvmfs"],
            gpu=bool(request.get("gpu", settings["gpu"])),
            sudo=bool(request.get("sudo", True)),
            privileged=bool(request.get("privileged", False)),
            mac_address=request.get("mac_address") or None,
        )
        launch = plan.build(domain, opts, engine.kind, rootless=info.rootless)
        session = Session(
            id=launch.session_id,
            domain=domain.name,
            engine=engine.kind,
            version=opts.version or domain.default_version,
            port=launch.host_port,
            token=launch.token,
            cvmfs=launch.cvmfs,
            gpu=opts.gpu,
            data_dir=str(opts.data_dir.resolve()) if opts.data_dir else None,
            created=_now(),
            argv=engine.argv(launch),
        )
        return engine, launch, session

    def start(self, request: dict, background: bool = False) -> Session:
        engine, launch, session = self.prepare(request)
        storage = launch.mounts[0]
        Path(storage.source).mkdir(parents=True, exist_ok=True)
        session.state = "starting" if engine.has_image(launch.image) else "pulling"
        self._save(session)
        if background:
            threading.Thread(target=self._run, args=(engine, launch, session), daemon=True).start()
        else:
            self._run(engine, launch, session)
        return session

    def _run(self, engine: Engine, launch: plan.LaunchPlan, session: Session) -> None:
        try:
            if session.state == "pulling":
                engine.pull(launch.image)
                session.state = "starting"
                self._save(session)
            session.handle = engine.start(launch, self.log_path(session.id))
            self._save(session)
            wait_ready(
                session.port,
                session.token,
                self.ready_timeout,
                lambda: engine.is_running(session.handle),
            )
            session.state = "ready"
        except (EngineError, SessionError, plan.PlanError) as exc:
            session.state = "error"
            session.error = str(exc)
        self._save(session)

    def stop(self, session_id: str) -> Session:
        session = self._load(session_id)
        if session.handle and session.state not in ("stopped",):
            self._engine(session).stop(session.handle)
        session.state = "stopped"
        self._save(session)
        return session

    def logs(self, session_id: str, tail: int = 200) -> list[str]:
        session = self._load(session_id)
        if not session.handle:
            return []
        return self._engine(session).logs(session.handle, self.log_path(session_id), tail)

    def forget(self, session_id: str) -> None:
        """Delete the record of a stopped session."""
        session = self._load(session_id)
        if session.state not in ("stopped", "error"):
            raise SessionError("Stop the session before removing it.")
        for path in (self._session_path(session_id), self.log_path(session_id)):
            path.unlink(missing_ok=True)
