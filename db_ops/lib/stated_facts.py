"""Which records leave a fact to a default - the owner's no-fallback rule (rules R50).

The owner's decision (2026-10-01, review notes G): everything that says *which* thing is acted on -
host, port, platform, login, database, path - is stated, or the command does not run. Records written
before that left some of it to a default the code supplied: `auth_type` became `key`, a missing port
the engine's, the platform a guess from `os` or from the transport, a PostgreSQL database the service
label.

**Phase 1 (0.26.0) reported them**, each as a ``fallback`` notice of ``check-objects``, so a node could
be completed before anything refused. **Phase 2 (0.27.0) refuses them** (the operator, 2026-10-06,
after the worker's and the master's ``check-objects`` read 0 such records): ``check-objects`` counts
each as a violation, and the record is refused where it is used - a metric target's collection, a SQL
task's connection, a host operation, a restore entry - with a sentence naming the field. One record
refuses alone: the rest of a scan runs (rules R26).

The rules are here once, for both readers: :func:`fallbacks` walks a data directory for the report,
:func:`instance_gaps` / :func:`require_instance_facts` and :func:`restore_gaps` hold one record where
it is about to be used. ``check-objects`` reads **active** records only - an inactive one runs
nothing; a record that is used is held to the rule whatever its switch says.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from db_ops.lib import errors
from db_ops.lib.json_io import load_json_file

FALLBACK_KIND = "fallback"

#: The facts a database connection needs stated, and the facts reaching the host needs.
SQL_FACTS = ("port", "database_name", "service_name")
HOST_FACTS = ("platform", "cmd_access.auth_type")


class FactNotStated(errors.InvalidConfig):
    """A record leaves a which-thing fact to a default (rules R50): the field is named."""


#: Which review item each field closes - the sentence says it, so a reader can find the reason.
_RULE = {
    "platform": "G3.2", "cmd_access.auth_type": "G3.3", "port": "G3.9", "database_name": "G3.8",
    "service_name": "G3.8", "env.MSSQL_USER": "G2.11", "target.sqlcmd_path": "G3.9",
    "source.certificate_api_token_ref": "G3", "database_mappings": "0.27.0 1.101",
}


def _gap(file_name: str, label: str, field: str, detail: str) -> dict[str, str]:
    return {"kind": FALLBACK_KIND, "where": f"{file_name}:{label}", "field": field,
            "detail": f"{detail}; state it - a record without it is refused (rules R50, {_RULE[field]})."}


def sentence(gaps: list[dict[str, str]]) -> str:
    """One line naming every gap of one record: what to add, and why it is refused."""
    if not gaps:
        return ""
    where = gaps[0]["where"]
    return f"{where}: " + "; ".join(f"{gap['field']} is not stated - {gap['detail']}" for gap in gaps)


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


def instance_gaps(record: dict[str, Any], *, facts: tuple[str, ...] = SQL_FACTS + HOST_FACTS
                  ) -> list[dict[str, str]]:
    """The facts of ``facts`` one ``db_instances.json`` record leaves to a default."""
    label = str(record.get("server_id") or record.get("ip") or "?")
    db_type = str(record.get("db_type") or "").strip().lower()
    access = record.get("cmd_access") if isinstance(record.get("cmd_access"), dict) else {}
    reaches_host = bool(access) and access.get("enabled", True) is not False
    found: list[dict[str, str]] = []
    if "platform" in facts and reaches_host and not str(record.get("platform") or "").strip():
        guessed = record.get("os") or ("windows" if access.get("method") == "winrm" else "linux")
        found.append(_gap("db_instances.json", label, "platform",
                          f"the host's platform would be guessed ({guessed!s}) from os or the transport"))
    if "cmd_access.auth_type" in facts and reaches_host \
            and str(access.get("method") or "").lower() == "ssh" \
            and not str(access.get("auth_type") or "").strip():
        found.append(_gap("db_instances.json", label, "cmd_access.auth_type",
                          "SSH would log in as auth_type key, the default"))
    if "port" in facts and db_type and db_type != "host" and not str(record.get("port") or "").strip():
        found.append(_gap("db_instances.json", label, "port",
                          f"the {db_type} default port would be used"))
    if "database_name" in facts and db_type in {"postgresql", "mysql"} and not str(
            record.get("database_name") or record.get("database") or "").strip():
        found.append(_gap("db_instances.json", label, "database_name",
                          "the database would be taken from the service or instance label"))
    if "service_name" in facts and db_type == "oracle" and not str(record.get("service_name") or "").strip():
        found.append(_gap("db_instances.json", label, "service_name",
                          "the service would be taken from the instance or server name"))
    return found


def require_instance_facts(record: dict[str, Any], *, facts: tuple[str, ...]) -> None:
    """Raise :class:`FactNotStated` when ``record`` leaves one of ``facts`` to a default."""
    gaps = instance_gaps(record, facts=facts)
    if gaps:
        raise FactNotStated(sentence(gaps))


def _instances(root: Path) -> list[dict[str, str]]:
    payload = _load(root, "db_instances.json")
    records = payload.get("db_instances") if isinstance(payload, dict) else payload
    found: list[dict[str, str]] = []
    for record in records or []:
        if isinstance(record, dict) and _active(record):
            found.extend(instance_gaps(record))
    return found


def restore_gaps(block: dict[str, Any], record: dict[str, Any]) -> list[dict[str, str]]:
    """The facts one ``restore_config.json`` entry leaves to a default, read as the loaders read it:
    the block's own keys and blocks under the entry's."""
    label = str(record.get("restore_id") or "?")
    found: list[dict[str, str]] = []
    if record.get("script"):
        env = record.get("env") if isinstance(record.get("env"), dict) else {}
        if str(record.get("db_type") or "").lower() == "sqlserver" \
                and not str(env.get("MSSQL_USER") or "").strip():
            found.append(_gap("restore_config.json", label, "env.MSSQL_USER",
                              "the restore would log in as sa"))
        return found
    target, source = (_merged(block, record, name) for name in ("target", "source"))
    flat = {**{k: v for k, v in block.items() if not isinstance(v, (dict, list))}, **record}
    # SQL Server restores database by database: which ones is stated - by name, or every one on the
    # share. An empty list meant "whatever FULL is there", and the check after the restore had no
    # name to ask about (1.101). PostgreSQL and Oracle are script-driven and restore the instance.
    names_databases = bool(record.get("database_mappings") or record.get("databases")) or bool(str(
        flat.get("source_database_name") or flat.get("source_database")
        or source.get("source_database") or "").strip())
    # `false` or absent: nothing stated. Any other value is a statement, and the parser holds its
    # kind (a "yes" is refused there, by name).
    if not names_databases and record.get("restore_all_databases") in (None, False):
        found.append(_gap("restore_config.json", label, "database_mappings",
                          "the restore would take every FULL it finds - name the databases, or "
                          "state restore_all_databases: true for every one under its backup's name"))
    if str(target.get("sql_container") or flat.get("sql_container") or "").strip() and not str(
            target.get("sqlcmd_path") or flat.get("sqlcmd_path") or "").strip():
        found.append(_gap("restore_config.json", label, "target.sqlcmd_path",
                          "sqlcmd's path inside the container would be inferred"))
    if str(source.get("certificate_api_url") or flat.get("certificate_api_url")
           or flat.get("api_link_get_cer") or "").strip() \
            and not str(source.get("certificate_api_token_ref")
                        or flat.get("certificate_api_token_ref") or "").strip():
        found.append(_gap("restore_config.json", label, "source.certificate_api_token_ref",
                          "the certificate API's token would be read from a built-in default ref"))
    return found


def _restores(root: Path) -> list[dict[str, str]]:
    payload = _load(root, "restore_config.json")
    block = payload.get("backup_restore", payload) if isinstance(payload, dict) else {}
    found: list[dict[str, str]] = []
    for record in (block or {}).get("restores") or []:
        if isinstance(record, dict) and _active(record):
            found.extend(restore_gaps(block or {}, record))
    return found


def _merged(block: dict[str, Any], record: dict[str, Any], name: str) -> dict[str, Any]:
    outer = block.get(name) if isinstance(block.get(name), dict) else {}
    inner = record.get(name) if isinstance(record.get(name), dict) else {}
    return {**outer, **inner}


def fallbacks(data_dir: str | Path) -> list[dict[str, str]]:
    """Every active record of ``data_dir`` that leaves a fact to a default."""
    root = Path(data_dir)
    return _instances(root) + _restores(root)
