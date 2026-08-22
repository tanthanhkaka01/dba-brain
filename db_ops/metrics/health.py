from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from db_ops.levels import ERROR, LOGGING, WARNING
from db_ops.lib.paths import DEFAULT_DATA_DIR


DEFAULT_INSTANCES_PATH = DEFAULT_DATA_DIR / "db_instances.example.json"


@dataclass(frozen=True)
class InstanceHealth:
    ord: str
    site: str
    env: str
    ip: str
    port: int | None
    db_type: str
    service_name: str
    database_names: list[str]
    instance_name: str
    os: str
    owner: str
    status_os: str
    status_db: str
    status_connect: str
    metrics: dict[str, Any]
    note: str

    @property
    def level(self) -> str:
        statuses = {self.status_os, self.status_db, self.status_connect}
        if ERROR in statuses:
            return ERROR
        if WARNING in statuses or "unknown" in statuses:
            return WARNING
        return LOGGING


def load_instances(path: str | Path) -> list[InstanceHealth]:
    with Path(path).open("r", encoding="utf-8-sig") as file:
        data = json.load(file)

    instances: list[InstanceHealth] = []
    for item in data.get("db_instances", []):
        if not bool(item.get("enabled", True)):
            continue
        instances.append(
            InstanceHealth(
                ord=str(item.get("ord", "")),
                site=str(item.get("site", "")),
                env=str(item.get("env", "")),
                ip=str(item.get("ip", "")),
                port=int(item["port"]) if item.get("port") not in (None, "") else None,
                db_type=str(item.get("db_type", "")),
                service_name=str(item.get("service_name") or item.get("db_name") or ""),
                database_names=[str(name) for name in item.get("database_names", []) or []],
                instance_name=str(item.get("instance_name", "")),
                os=str(item.get("os", "")),
                owner=str(item.get("owner", "")),
                status_os=normalize_status(item.get("status_os", "unknown")),
                status_db=normalize_status(item.get("status_db", "unknown")),
                status_connect=normalize_status(item.get("status_connect", "unknown")),
                metrics=item.get("metrics") or {},
                note=str(item.get("note", "")),
            )
        )
    return instances


def normalize_status(value: Any) -> str:
    status = str(value or "unknown").strip().lower()
    if status in ("ok", "healthy", "up", "online", "connected"):
        return "ok"
    if status in ("warn", "warning"):
        return WARNING
    if status in ("err", "error", "critical", "down", "offline", "failed"):
        return ERROR
    return "unknown"
