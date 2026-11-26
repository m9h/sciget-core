"""Domains: which image a session runs, and how its storage is laid out.

A domain file uses the same keys as upstream Sciget App's
container_installer/*.yml, so those files load unchanged. Built-in domains ship
in sciget_core/domains/. A file with the same name in the user's config
directory (<user_config_dir>/sciget/domains/) overrides the built-in one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

import yaml

from sciget_core import paths

_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_DIRNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TAG = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
_REGISTRY = re.compile(r"^[a-z0-9.-]+(:[0-9]+)?(/[a-z0-9._-]+)+$")
_CVMFS_REPO = re.compile(r"^[a-z0-9][a-z0-9.-]+$")


class CatalogError(ValueError):
    """A domain file is missing a key or has an unsafe value."""


@dataclass(frozen=True)
class Domain:
    name: str
    title: str
    registry: str
    default_version: str
    volume_mount: str
    storage_mount: str
    description: str = ""
    remote_urls: tuple[str, ...] = ()
    cvmfs_repository: str | None = None
    server_args: tuple[str, ...] = ()
    container_port: int = 8888

    def image(self, version: str | None = None) -> str:
        tag = version or self.default_version
        if not _TAG.match(tag):
            raise CatalogError(f"{tag!r} is not a valid image tag")
        return f"{self.registry}:{tag}"

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "image": self.registry,
            "default_version": self.default_version,
            "remote_urls": list(self.remote_urls),
            "cvmfs_repository": self.cvmfs_repository,
        }


def _require(data: dict, key: str, pattern: re.Pattern | None, source: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CatalogError(f"{source}: '{key}' is missing or empty")
    value = value.strip()
    if pattern is not None and not pattern.match(value):
        raise CatalogError(f"{source}: '{key}' has an invalid value {value!r}")
    return value


def parse_domain(name: str, data: object, source: str = "<memory>") -> Domain:
    if not _NAME.match(name):
        raise CatalogError(f"{source}: domain name {name!r} is invalid")
    if not isinstance(data, dict):
        raise CatalogError(f"{source}: expected a mapping")
    remote = data.get("remoteUrl") or []
    if isinstance(remote, str):
        remote = [remote]
    if not all(isinstance(u, str) and u.startswith("https://") for u in remote):
        raise CatalogError(f"{source}: every remoteUrl must be an https:// URL")
    server_args = data.get("serverArgs") or []
    if not all(isinstance(a, str) and a.startswith("--") for a in server_args):
        raise CatalogError(f"{source}: serverArgs must be a list of --options")
    cvmfs = data.get("cvmfsRepository")
    if cvmfs is not None and not (isinstance(cvmfs, str) and _CVMFS_REPO.match(cvmfs)):
        raise CatalogError(f"{source}: cvmfsRepository has an invalid value")
    return Domain(
        name=name,
        title=str(data.get("title") or name).strip(),
        registry=_require(data, "registry", _REGISTRY, source),
        default_version=_require(data, "defaultVersion", _TAG, source),
        volume_mount=_require(data, "volumeMount", _DIRNAME, source),
        storage_mount=_require(data, "defaultStorageMount", _DIRNAME, source),
        description=str(data.get("description") or "").strip(),
        remote_urls=tuple(remote),
        cvmfs_repository=cvmfs,
        server_args=tuple(server_args),
    )


def _load_file(path: Path) -> Domain:
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return parse_domain(path.stem, data, str(path))


def load(user_dir: Path | None = None) -> dict[str, Domain]:
    """Return all domains by name: built-in ones, then the user's overrides."""
    domains: dict[str, Domain] = {}
    builtin = resources.files("sciget_core") / "domains"
    for entry in sorted(builtin.iterdir(), key=lambda e: e.name):
        if entry.name.endswith((".yml", ".yaml")):
            name = entry.name.rsplit(".", 1)[0]
            domains[name] = parse_domain(
                name, yaml.safe_load(entry.read_text(encoding="utf-8")), entry.name
            )
    user_dir = user_dir if user_dir is not None else paths.config_dir() / "domains"
    if user_dir.is_dir():
        for path in sorted(user_dir.glob("*.y*ml")):
            domains[path.stem] = _load_file(path)
    return domains
