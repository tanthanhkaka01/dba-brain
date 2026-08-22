"""How to reach a database, as configuration — the ``sql_access`` vocabulary.

Split out of ``common/oracle_bridge.py`` and ``common/db_connect.py`` on 2026-08-15. Opening a
connection and POSTing to the Oracle 8i bridge are operations and stayed in ``common``; deciding
what ``method: "api"`` *means*, and that ``mssql`` and ``sqlserver`` are the same engine, is a rule
about values. Three apps read that rule while parsing their own config, long before anything is
connected to, so it could not be a process — and it is the same rule for all of them, so it must
not be three copies either.
"""

from __future__ import annotations

import os
import re
from typing import Any


#: What ``sql_access.method`` may say. ``direct`` is not handled here — it means "not legacy".
SUPPORTED_SQL_ACCESS_METHODS = {"direct", "api", "subprocess"}

#: The engines a **configured target** may declare, canonically spelled. Distinct from
#: ``db_connect.SUPPORTED_DB_TYPES`` (which engines a *driver* can open) and from
#: ``metrics.definitions.SUPPORTED_DB_TYPES`` (which engines a *metric* may be written for): those
#: two are derived from what their own layer supports, and collapsing all three would mean adding
#: an engine to the metric catalog the moment a driver existed for it. This one is the config
#: vocabulary — what ``db_instances.json`` and ``sql_targets.json`` are allowed to say — and it is
#: read while validating config, before any driver is loaded.
KNOWN_DB_TYPES = ("sqlserver", "mysql", "postgresql", "oracle")


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def normalize_sql_access(raw: Any, *, label: str = "") -> dict[str, Any]:
    """Validate one target's ``sql_access`` block and fill in its method.

    Lived in ``db_ops.metrics.targets`` while metrics was the only caller; ``sql_run`` is the
    second, so it moved here rather than being parsed twice with two opinions about what a
    missing ``bridge_url`` means (``docs/13_common.md`` had already flagged the
    promotion). Absent or empty means ``direct`` — the overwhelming majority of targets say
    nothing at all.
    """
    if raw in (None, ""):
        return {"method": "direct"}
    if not isinstance(raw, dict):
        raise LegacyOracleError(f"sql_access must be an object{f' for {label}' if label else ''}.")
    resolved = dict(raw)
    method = str(raw.get("method") or "direct").strip().lower()
    if method not in SUPPORTED_SQL_ACCESS_METHODS:
        raise LegacyOracleError(
            f"sql_access.method must be one of {sorted(SUPPORTED_SQL_ACCESS_METHODS)}, "
            f"got '{method}'{f' for {label}' if label else ''}."
        )
    resolved["method"] = method
    if method == "api" and not str(raw.get("bridge_url") or "").strip():
        raise LegacyOracleError(
            f"sql_access.bridge_url is required when method is 'api'{f' for {label}' if label else ''}."
        )
    return resolved


def is_legacy(sql_access: Any) -> bool:
    """True when this target's SQL must go through the legacy tool rather than a DB connection."""
    if not isinstance(sql_access, dict):
        return False
    return str(sql_access.get("method") or "direct").strip().lower() in {"api", "subprocess"}


def normalize_db_type(db_type: str) -> str:
    """Accept the spellings that appear in config, return the canonical engine name."""
    value = str(db_type or "").strip().lower()
    aliases = {
        "mssql": "sqlserver", "sql_server": "sqlserver", "sqlsvr": "sqlserver",
        "postgres": "postgresql", "pgsql": "postgresql", "pg": "postgresql",
        "mariadb": "mysql",
        # A lab container provisioned by db_ops.sre records its *engine* ("oracle-xe" for the
        # Express Edition image), and anything that reads that field as a db_type has to reach
        # the same driver as any other Oracle. The distinction matters when creating the
        # container, never when connecting to it.
        "oracle-xe": "oracle", "oraclexe": "oracle",
    }
    return aliases.get(value, value)


class LegacyOracleError(RuntimeError):
    """A legacy-Oracle run that failed for a reason the operator can act on.

    Kept distinct from a bare ``RuntimeError`` because these are the failures with a fix
    attached — no runtime on this host, bridge down, no credential for the target — and the
    callers turn them into their own user-facing message rather than a traceback.
    """
