"""Where a script-driven SQL Server restore lands: which databases, and under which names (0.39.0, 1.362).

The operator, 2026-11-13 (translated): *restore must be guarded very carefully - into an existing DB,
into a new DB, under another name*; and, the same day: *these fields must be required; in backup
restore most fields must be required - a mistake can ruin the whole production.*

Until 0.39.0 a script-driven SQL Server entry named its databases as a comma-separated string in its
``env`` (``MSSQL_DATABASES``), and with none named it restored **whatever FULL it found** under the
folder - each under its backup's own name, the only name it could land under. The share-driven entry
has stated both since 0.27.0 (1.101): ``database_mappings`` or ``restore_all_databases``. This is the
same statement for the script-driven one, read once:

- ``database_mappings``: ``[{"source_database": "APPDB", "target_database": "APPDB"}, ...]`` - each
  database restored, and the name it is restored **as**. The same name twice is the plain restore; a
  different ``target_database`` restores it under another name, beside whatever the target holds.
- ``restore_all_databases: true``: every database whose FULL is in the folder, each under its own name.

Exactly one of the two answers; an entry that states neither is refused inside its own run (rules
R50), and naming the list in ``env.MSSQL_DATABASES`` as well is refused - two places to say "which
databases" is how they come to disagree (the backup side's rule, :mod:`dbabrain.lib.backup_scope`).
A PostgreSQL base backup and an Oracle RMAN backup hold a whole cluster / instance: their databases
come as they are, so those entries state neither, and a mapping there is refused by name.

``upgrade-config`` (``stated-integrity``) moves an existing ``env.MSSQL_DATABASES`` into
``database_mappings`` - each name mapped to itself, what the entry had been doing - and states
``restore_all_databases: true`` where the entry named none.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dbabrain.lib import errors
# The script input the list used to be, never stated in an entry's env once it states the fields:
# one definition, the backup scope's.
from dbabrain.lib.backup_scope import SCRIPT_ENV

#: The engine whose restore lands database by database. Every other is the whole instance.
BY_DATABASE_ENGINES = ("sqlserver",)
#: SQL Server's limit on an identifier.
_MAX_NAME = 128

__all__ = ["BY_DATABASE_ENGINES", "Landing", "LandingError", "SCRIPT_ENV", "landing_names",
           "parse_landing", "require_landing", "said", "state_restore_landing"]


class LandingError(errors.ConfigError):
    """An entry's databases are missing, mistyped, stated twice or contradict its engine."""


@dataclass(frozen=True)
class Landing:
    """``names`` is ``(source_database, target_database)`` per database, in the entry's order; empty
    with ``all_databases`` - then every database found lands under its own name."""

    names: tuple[tuple[str, str], ...]
    all_databases: bool

    @property
    def sources(self) -> tuple[str, ...]:
        return tuple(source for source, _target in self.names)

    @property
    def renamed(self) -> tuple[tuple[str, str], ...]:
        return tuple((source, target) for source, target in self.names if source.lower() != target.lower())


def _name(value: Any, *, label: str, what: str) -> str:
    text = str(value or "").strip() if isinstance(value, str) else ""
    if not text:
        raise LandingError(f"{label}: a database mapping needs {what} - a database name.")
    if len(text) > _MAX_NAME or any(ord(character) < 32 for character in text):
        raise LandingError(f"{label}: {what} {text[:40]!r} is not a database name SQL Server takes "
                           f"(at most {_MAX_NAME} characters, no control character).")
    return text


def parse_landing(entry: dict[str, Any] | None, *, label: str = "restore", db_type: str = "") -> Landing | None:
    """An entry's ``database_mappings`` / ``restore_all_databases``; ``None`` when it states neither.

    Both answers at once, a mistyped value, two databases landing under one name, the list also in
    ``env``, or a mapping on an engine that restores a whole instance is refused by name.
    """
    raw = entry or {}
    env = raw.get("env") if isinstance(raw.get("env"), dict) else {}
    engine = str(db_type or raw.get("db_type") or "").strip().lower()
    mappings = raw.get("database_mappings")
    everything = raw.get("restore_all_databases")
    if engine and engine not in BY_DATABASE_ENGINES:
        if mappings or everything is True:
            raise LandingError(
                f"{label}: a {engine} restore lays down the whole cluster / instance its backup holds - its "
                "databases come under the names they had; database_mappings and restore_all_databases are "
                "a SQL Server entry's.")
        return None
    if mappings is None and everything is None:
        return None
    if everything is not None and not isinstance(everything, bool):
        raise LandingError(f"{label}: restore_all_databases must be true or false, got {everything!r}.")
    if mappings is not None and not isinstance(mappings, list):
        raise LandingError(f"{label}: database_mappings must be a list of "
                           '{"source_database": ..., "target_database": ...}.')
    if SCRIPT_ENV in env:
        raise LandingError(f"{label}: names its databases in env.{SCRIPT_ENV} as well as in database_mappings / "
                           "restore_all_databases - state them once (upgrade-config moves the env one).")
    names: list[tuple[str, str]] = []
    for item in mappings or []:
        if not isinstance(item, dict):
            raise LandingError(f"{label}: a database mapping is an object "
                               '{"source_database": ..., "target_database": ...}; got ' f"{item!r}.")
        names.append((_name(item.get("source_database"), label=label, what="source_database"),
                      _name(item.get("target_database"), label=label, what="target_database")))
    if everything is True and names:
        raise LandingError(f"{label}: restore_all_databases is true and database_mappings names {len(names)} - "
                           "one answer: the names, or every database in the folder under its own name.")
    if not names and everything is not True:
        raise LandingError(f"{label}: database_mappings is empty and restore_all_databases is not true - name "
                           "the databases, or state restore_all_databases true.")
    for position, kind in ((0, "source_database"), (1, "target_database")):
        seen: dict[str, str] = {}
        for pair in names:
            key = pair[position].lower()
            if key in seen:
                raise LandingError(
                    f"{label}: {kind} {pair[position]!r} is named twice - "
                    + ("a database is restored once per run." if position == 0 else
                       "two databases cannot land under one name: the second would replace the first."))
            seen[key] = pair[position]
    return Landing(names=tuple(names), all_databases=everything is True)


def require_landing(landing: Landing | None, *, label: str = "restore") -> Landing:
    """The landing an entry stated, or a refusal naming the entry (R50)."""
    if landing is None:
        raise LandingError(
            f"{label} states no database_mappings and no restore_all_databases, so nothing says which "
            "databases it restores or under which names. State database_mappings (each source_database "
            "with its target_database), or restore_all_databases: true. upgrade-config moves an "
            f"env.{SCRIPT_ENV} into database_mappings.")
    return landing


def landing_names(landing: Landing, found: list[str]) -> list[tuple[str, str]]:
    """``(source, target)`` for this run: the entry's names, or - ``all_databases`` - every database
    ``found`` under its own name."""
    if landing.all_databases:
        return [(name, name) for name in found]
    return list(landing.names)


def said(names: list[tuple[str, str]]) -> str:
    """``APPDB, HR as HR_COPY`` - the databases of a run and the names they land under."""
    return ", ".join(source if source.lower() == target.lower() else f"{source} as {target}"
                     for source, target in names)


def state_restore_landing(document: dict[str, Any]) -> list[dict[str, str]]:
    """Move every script-driven SQL Server restore entry's ``env.MSSQL_DATABASES`` into
    ``database_mappings`` - each name mapped to itself - and state ``restore_all_databases: true``
    where it named none: what the entry had been doing. An entry that states either field is left as
    it is. One change per entry written."""
    changes: list[dict[str, str]] = []
    section = document.get("backup_restore") if isinstance(document, dict) else None
    restores = section.get("restores") if isinstance(section, dict) else None
    for index, entry in enumerate(restores if isinstance(restores, list) else []):
        if not isinstance(entry, dict) or not str(entry.get("script") or "").strip():
            continue
        if str(entry.get("db_type") or "").strip().lower() not in BY_DATABASE_ENGINES:
            continue
        if entry.get("database_mappings") is not None or entry.get("restore_all_databases") is not None:
            continue
        env = entry.get("env") if isinstance(entry.get("env"), dict) else None
        text = str((env or {}).pop(SCRIPT_ENV, "") or "")
        names = list(dict.fromkeys(name.strip() for name in text.split(",") if name.strip()))
        where = f"backup_restore.restores[{entry.get('restore_id') or index}]"
        if names:
            entry["database_mappings"] = [{"source_database": name, "target_database": name} for name in names]
            changes.append({"where": where, "field": "database_mappings",
                            "value": f"{len(names)} name(s) moved from env.{SCRIPT_ENV}, each under its own name"})
        else:
            entry["restore_all_databases"] = True
            changes.append({"where": where, "field": "restore_all_databases",
                            "value": "true - every database in the folder under its own name, as the entry restored"})
    return changes
