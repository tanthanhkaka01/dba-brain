"""Register a provisioned lab database as a connection entry under ``data/``.

Provisioned lab containers are a new, SRE-owned concept, so they get their own
additive registry file — ``data/docker_db_connections.json`` — rather than being
forced into the metrics/inventory-critical ``db_instances.json`` schema. The file
follows the existing project convention (a top-level key wrapping an array of
snake_case objects, 2-space indent) so it reads like the rest of ``data/``.

The write is an idempotent upsert keyed by ``id`` (``<NAME>`` upper-cased), so
re-running ``create-db-docker`` for the same name refreshes the entry in place.
"""

from __future__ import annotations
from db_ops.common.data_sources import REGISTRY_FILENAME  # noqa: F401 - one definition

import json
from pathlib import Path

from db_ops.sre.docker_db.models import DockerDbSpec

REGISTRY_ROOT_KEY = "docker_db_connections"
CREATED_BY = "db_ops.sre.create-db-docker"


def default_registry_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / REGISTRY_FILENAME


def connection_id(name: str) -> str:
    return name.upper()


def build_connection_entry(
    spec: DockerDbSpec,
    *,
    host: str,
    compose_path: str,
    worker_host: str = "",
) -> dict:
    meta = spec.meta
    docker: dict = {
        "instance_name": spec.name,
        "mode": spec.mode,
        "version": spec.version,
        "compose_path": compose_path,
    }
    if spec.is_ha:
        docker["replicas"] = spec.replicas
    entry: dict = {
        "id": connection_id(spec.name),
        "engine": spec.engine,
        "host": host,
        "port": spec.host_port,
        "database": meta.database,
        "username": meta.username,
        "password_env": spec.password_env,
        "docker": docker,
        "created_by": CREATED_BY,
    }
    if worker_host:
        entry["worker_host"] = worker_host
    return entry


def load_registry(path: str | Path) -> dict:
    path = Path(path)
    if not path.exists():
        return {REGISTRY_ROOT_KEY: []}
    with path.open("r", encoding="utf-8-sig") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or not isinstance(data.get(REGISTRY_ROOT_KEY), list):
        raise ValueError(
            f"{path} is not a valid docker-db registry (expected a '{REGISTRY_ROOT_KEY}' array)."
        )
    return data


def save_registry(path: str | Path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def register_connection(registry_path: str | Path, entry: dict) -> str:
    """Upsert ``entry`` into the registry. Returns ``"added"`` or ``"updated"``."""
    data = load_registry(registry_path)
    entries = data[REGISTRY_ROOT_KEY]
    entry_id = entry["id"]
    for index, existing in enumerate(entries):
        if isinstance(existing, dict) and existing.get("id") == entry_id:
            entries[index] = entry
            save_registry(registry_path, data)
            return "updated"
    entries.append(entry)
    save_registry(registry_path, data)
    return "added"


def relocate_connection(registry_path: str | Path, entry_id: str, *, host: str,
                        worker_host: str, compose_path: str) -> str:
    """Point an existing connection entry at the machine the instance now runs on.

    A move changes exactly three facts — where the database answers, which host holds the
    containers, and where its compose file is — and leaves everything else (engine, port,
    credentials ref, version) alone. Rebuilding the entry from a spec instead would quietly
    reset fields the operator has edited since it was provisioned.

    Returns ``"updated"``, or ``"not_registered"`` when the instance predates the registry —
    which is not an error: the move succeeded, there was simply nothing here to correct.
    """
    data = load_registry(registry_path)
    for entry in data[REGISTRY_ROOT_KEY]:
        if not isinstance(entry, dict) or entry.get("id") != entry_id:
            continue
        entry["host"] = host
        entry["worker_host"] = worker_host
        docker = entry.get("docker")
        if isinstance(docker, dict):
            docker["compose_path"] = compose_path
        save_registry(registry_path, data)
        return "updated"
    return "not_registered"
