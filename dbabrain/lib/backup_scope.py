"""Which databases a backup job takes - stated on every job, as a list (0.34.0).

The operator, 2026-10-30: the databases a SQL Server backup takes were a comma-separated string in
the job's ``env`` (``MSSQL_DATABASES``) - *not good; fix it all: a mandatory field, an array, like the
restore side*. So every backup job states, as a restore entry does (``database_mappings`` /
``restore_all_databases``):

- ``databases``: the names, as a JSON array;
- ``all_databases``: ``true`` for every user database (``databases`` then empty), ``false`` with the
  names listed.

Exactly one of the two answers. The app hands the list to the script (``MSSQL_DATABASES``, the
script's own input); naming it in ``env`` as well is refused - two places to say "which databases"
is how they come to disagree. A PostgreSQL base backup and an Oracle RMAN backup take the whole
cluster / instance, so those engines state ``all_databases: true``.

``upgrade-config`` (``stated-integrity``) moves an existing ``env.MSSQL_DATABASES`` into the field,
and states ``all_databases: true`` where the job named none - what the script did with nothing named.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dbabrain.lib import errors

#: The script input the list becomes; never stated in a job's env.
SCRIPT_ENV = "MSSQL_DATABASES"
#: Engines whose backup is the whole cluster or instance, never a named database.
WHOLE_INSTANCE_ENGINES = ("postgresql", "oracle", "mysql")


class BackupScopeError(errors.ConfigError):
    """A job's databases are missing, mistyped, stated twice or contradict its engine."""


@dataclass(frozen=True)
class BackupScope:
    databases: tuple[str, ...]
    all_databases: bool


def parse_backup_databases(job: dict[str, Any] | None, *, label: str = "backup", db_type: str = "") -> BackupScope | None:
    """A job's ``databases`` / ``all_databases``; ``None`` when it states neither (a job from before
    0.34.0 that upgrade-config has not reached). One without the other, a mistyped value, both
    answers at once, or the list also in ``env`` is refused by name."""
    raw = job or {}
    env = raw.get("env") if isinstance(raw.get("env"), dict) else {}
    has_list, has_all = "databases" in raw, "all_databases" in raw
    if not has_list and not has_all:
        return None
    if has_list != has_all:
        raise BackupScopeError(f"{label}: states {'databases' if has_list else 'all_databases'} without "
                               f"{'all_databases' if has_list else 'databases'} - both are stated on every job.")
    if SCRIPT_ENV in env:
        raise BackupScopeError(f"{label}: names its databases in env.{SCRIPT_ENV} as well as in databases - "
                               f"state them once, in databases (upgrade-config moves the env one).")
    names = raw["databases"]
    if not isinstance(names, list) or not all(isinstance(name, str) and name.strip() for name in names):
        raise BackupScopeError(f"{label}: databases must be a list of database names, e.g. [\"HR\", \"SALESDB\"].")
    if not isinstance(raw["all_databases"], bool):
        raise BackupScopeError(f"{label}: all_databases must be true or false, got {raw['all_databases']!r}.")
    cleaned = tuple(dict.fromkeys(name.strip() for name in names))
    if raw["all_databases"] and cleaned:
        raise BackupScopeError(f"{label}: all_databases is true and databases names {len(cleaned)} - one answer: "
                               "the names (all_databases false), or every user database (databases []).")
    if not raw["all_databases"] and not cleaned:
        raise BackupScopeError(f"{label}: databases is empty and all_databases is false - name the databases, "
                               "or state all_databases true.")
    if cleaned and str(db_type or "").strip().lower() in WHOLE_INSTANCE_ENGINES:
        raise BackupScopeError(f"{label}: a {db_type} backup takes the whole cluster / instance - state "
                               "all_databases true and databases [].")
    return BackupScope(databases=cleaned, all_databases=raw["all_databases"])


def require_backup_databases(scope: BackupScope | None, *, label: str = "backup") -> BackupScope:
    if scope is None:
        raise BackupScopeError(f"{label} states no databases / all_databases. Run upgrade-config (it moves "
                               f"env.{SCRIPT_ENV} into databases) or state them.")
    return scope


def script_env(scope: BackupScope) -> dict[str, str]:
    """What the script reads: the names, comma-joined, or nothing for every user database."""
    return {} if scope.all_databases else {SCRIPT_ENV: ",".join(scope.databases)}


def state_backup_databases(document: dict[str, Any]) -> list[dict[str, str]]:
    """Move every job's ``env.MSSQL_DATABASES`` into ``databases`` and state ``all_databases``; a job that
    named none takes every user database, as its script did. A job that states the fields is left
    as it is. One change per job written."""
    changes: list[dict[str, str]] = []
    section = document.get("backup_restore") if isinstance(document, dict) else None
    backups = section.get("backups") if isinstance(section, dict) else None
    for entry in backups if isinstance(backups, list) else []:
        if not isinstance(entry, dict):
            continue
        for job in entry.get("jobs") if isinstance(entry.get("jobs"), list) else []:
            if not isinstance(job, dict) or ("databases" in job and "all_databases" in job):
                continue
            env = job.get("env") if isinstance(job.get("env"), dict) else None
            text = str((env or {}).pop(SCRIPT_ENV, "") or "")
            names = [name.strip() for name in text.split(",") if name.strip()]
            job.setdefault("databases", names)
            job.setdefault("all_databases", not names)
            where = f"backup_restore.backups[{entry.get('backup_id', '?')}].jobs[{job.get('job', '?')}]"
            changes.append({"where": where, "field": "databases",
                            "value": f"{len(names)} name(s) moved from env.{SCRIPT_ENV}" if names
                            else "all_databases true - every user database, as the script took"})
    return changes
