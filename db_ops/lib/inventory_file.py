"""The database inventory file an explicit ``--inventory`` names - JSON, or YAML without PyYAML.

A rule about a file's shape, so it is ``lib``'s (rules R14): the metrics target loader reads it,
and an app may import ``lib`` but not ``common``, where it sat until 0.24.0.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_database_inventory(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    if path.suffix.lower() == ".json":
        with path.open("r", encoding="utf-8-sig") as file:
            data = json.load(file) or {}
        if not isinstance(data, dict):
            return []
        return list(data.get("servers", []))

    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        return load_database_inventory_without_yaml(path)

    with path.open("r", encoding="utf-8-sig") as file:
        data = yaml.safe_load(file) or {}
    if not isinstance(data, dict):
        return []
    return list(data.get("servers", []))


def load_database_inventory_without_yaml(path: Path) -> list[dict[str, Any]]:
    servers: list[dict[str, Any]] = []
    current_server: dict[str, Any] | None = None
    current_database: dict[str, Any] | None = None
    current_list: list[Any] | None = None
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        line = raw_line.strip()
        if indent == 2 and line.startswith("- server_id:"):
            current_server = {"server_id": parse_yaml_scalar(line.split(":", 1)[1]), "databases": []}
            current_database = None
            current_list = None
            servers.append(current_server)
            continue
        if current_server is None:
            continue
        if indent == 4 and not line.startswith("- ") and ":" in line:
            key, value = line.split(":", 1)
            current_list = None
            if value.strip():
                current_server[key.strip()] = parse_yaml_scalar(value)
            continue
        if indent == 6 and line.startswith("- db_type:"):
            current_database = {"db_type": parse_yaml_scalar(line.split(":", 1)[1])}
            current_server.setdefault("databases", []).append(current_database)
            current_list = None
            continue
        if current_database is None:
            continue
        if indent == 8 and not line.startswith("- ") and ":" in line:
            key, value = line.split(":", 1)
            key = key.strip()
            current_list = None
            if value.strip():
                current_database[key] = parse_yaml_scalar(value)
            else:
                current_database[key] = []
                current_list = current_database[key]
            continue
        if indent == 10 and line.startswith("- ") and current_list is not None:
            current_list.append(parse_yaml_scalar(line[2:]))
    return servers


def parse_yaml_scalar(value: str) -> str | int:
    text = value.strip().strip("'\"")
    if text == "<not-provided>":
        return ""
    try:
        return int(text)
    except ValueError:
        return text
