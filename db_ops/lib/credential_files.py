"""The credential files, read - ``users.json``'s database and remote credential groups.

Moved out of ``common/sql_execution.py`` in 0.24.0 with the data-folder reader
(``lib/data_sources``), which needs them and may import nothing outside ``lib`` (rules R06).
``sql_execution`` re-exports them for its existing callers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from db_ops.lib.json_io import load_json_file


def load_credentials_file(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = load_json_file(path)
    return list(data.get("database_credentials", []))


def load_remote_credentials_file(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = load_json_file(path)
    if isinstance(data.get("remote_credentials"), list):
        return list(data.get("remote_credentials", []))
    legacy = data.get("remote_users")
    if not isinstance(legacy, list):
        return []
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for item in legacy:
        if not isinstance(item, dict):
            continue
        host = str(item.get("server_ip") or item.get("host") or "").strip()
        if not host:
            continue
        server_id = str(item.get("server_id") or f"{str(item.get('company_code') or 'REMOTE')}-{host.replace('.', '-')}").strip()
        key = (server_id, host)
        group = groups.setdefault(
            key,
            {
                "server_id": server_id,
                "host": host,
                "credentials": [],
            },
        )
        credential_name = str(item.get("credential_name") or item.get("name") or "").strip()
        if not credential_name:
            continue
        group["credentials"].append(
            {
                "credential_name": credential_name,
                "username": str(item.get("username") or item.get("login_name") or ""),
                "password_ref": str(item.get("password_ref") or item.get("authentication_info_ref") or ""),
                "role": str(item.get("role") or "REMOTE"),
                "note": str(item.get("note") or ""),
            }
        )
    return list(groups.values())
