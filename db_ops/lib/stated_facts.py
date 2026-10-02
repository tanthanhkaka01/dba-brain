"""Which records still leave a fact to a default - phase 1 of the owner's no-fallback rule.

The owner's decision (2026-10-01, review notes G): everything that says *which* thing is acted on -
host, port, platform, login, database, path - is stated, or the command does not run. Records written
before that leave some of it to a default the code supplies: `auth_type` becomes `key`, a missing
port becomes the engine's, the platform is guessed from `os` or from the transport, a PostgreSQL
database is taken from the service label.

Refusing those records outright would stop a node the moment it upgrades. So this release reports
them - each as a ``fallback`` notice of ``check-objects``, naming the field to add and what is
assumed until then - and the next one refuses them. Only **active** records: an inactive one runs
nothing, and is how an estate retires a target.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from db_ops.lib.json_io import load_json_file

FALLBACK_KIND = "fallback"

#: The next release's word, in every notice: what an operator has to do before then.
_THEN = "state it - the next release refuses a record without it"


def _notice(file_name: str, label: str, field: str, detail: str) -> dict[str, str]:
    return {"kind": FALLBACK_KIND, "where": f"{file_name}:{label}", "field": field,
            "detail": f"{detail}; {_THEN} ({_RULE[field]})."}


#: Which review item each field closes - the notice says it, so a reader can find the reason.
_RULE = {
    "platform": "G3.2", "cmd_access.auth_type": "G3.3", "port": "G3.9", "database_name": "G3.8",
    "service_name": "G3.8", "env.MSSQL_USER": "G2.11", "target.sqlcmd_path": "G3.9",
    "source.certificate_api_token_ref": "G3",
}


def _active(record: dict[str, Any]) -> bool:
    return record.get("active", True) is not False and record.get("enabled", True) is not False


def _load(root: Path, name: str) -> Any:
    path = root / name
    if not path.is_file():
        return None
    try:
        return load_json_file(path)
    except Exception:  # noqa: BLE001 - a broken file is check-objects' own finding, not this one's
        return None


def _instances(root: Path) -> list[dict[str, str]]:
    payload = _load(root, "db_instances.json")
    records = payload.get("db_instances") if isinstance(payload, dict) else payload
    found: list[dict[str, str]] = []
    for record in records or []:
        if not isinstance(record, dict) or not _active(record):
            continue
        label = str(record.get("server_id") or record.get("ip") or "?")
        db_type = str(record.get("db_type") or "").strip().lower()
        access = record.get("cmd_access") if isinstance(record.get("cmd_access"), dict) else {}
        reaches_host = bool(access) and access.get("enabled", True) is not False
        if reaches_host and not str(record.get("platform") or "").strip():
            guessed = record.get("os") or ("windows" if access.get("method") == "winrm" else "linux")
            found.append(_notice("db_instances.json", label, "platform",
                                 f"the host's platform is guessed ({guessed!s}) from os or the transport"))
        if reaches_host and str(access.get("method") or "").lower() == "ssh" \
                and not str(access.get("auth_type") or "").strip():
            found.append(_notice("db_instances.json", label, "cmd_access.auth_type",
                                 "SSH logs in as auth_type key, the default"))
        if db_type and db_type != "host" and not str(record.get("port") or "").strip():
            found.append(_notice("db_instances.json", label, "port",
                                 f"the {db_type} default port is used"))
        if db_type in {"postgresql", "mysql"} and not str(
                record.get("database_name") or record.get("database") or "").strip():
            found.append(_notice("db_instances.json", label, "database_name",
                                 "the database is taken from the service or instance label"))
        if db_type == "oracle" and not str(record.get("service_name") or "").strip():
            found.append(_notice("db_instances.json", label, "service_name",
                                 "the service is taken from the instance or server name"))
    return found


def _restores(root: Path) -> list[dict[str, str]]:
    payload = _load(root, "restore_config.json")
    block = payload.get("backup_restore", payload) if isinstance(payload, dict) else {}
    found: list[dict[str, str]] = []
    for record in (block or {}).get("restores") or []:
        if not isinstance(record, dict) or not _active(record):
            continue
        label = str(record.get("restore_id") or "?")
        if record.get("script"):
            env = record.get("env") if isinstance(record.get("env"), dict) else {}
            if str(record.get("db_type") or "").lower() == "sqlserver" \
                    and not str(env.get("MSSQL_USER") or "").strip():
                found.append(_notice("restore_config.json", label, "env.MSSQL_USER",
                                     "the restore logs in as sa"))
            continue
        # As the loader reads an entry: the block's own keys and blocks under the entry's.
        target, source = (_merged(block, record, name) for name in ("target", "source"))
        flat = {**{k: v for k, v in block.items() if not isinstance(v, (dict, list))}, **record}
        if str(target.get("sql_container") or "").strip() and not str(
                target.get("sqlcmd_path") or flat.get("sqlcmd_path") or "").strip():
            found.append(_notice("restore_config.json", label, "target.sqlcmd_path",
                                 "sqlcmd's path inside the container is inferred"))
        if str(source.get("certificate_api_url") or flat.get("certificate_api_url") or "").strip() \
                and not str(source.get("certificate_api_token_ref")
                            or flat.get("certificate_api_token_ref") or "").strip():
            found.append(_notice("restore_config.json", label, "source.certificate_api_token_ref",
                                 "the certificate API's token is read from a built-in default ref"))
    return found


def _merged(block: dict[str, Any], record: dict[str, Any], name: str) -> dict[str, Any]:
    outer = block.get(name) if isinstance(block.get(name), dict) else {}
    inner = record.get(name) if isinstance(record.get(name), dict) else {}
    return {**outer, **inner}


def fallbacks(data_dir: str | Path) -> list[dict[str, str]]:
    """Every active record of ``data_dir`` that leaves a fact to a default, as notices."""
    root = Path(data_dir)
    return _instances(root) + _restores(root)
