"""One name per concept: the fields 0.22.0 renamed, and how a record is read under either name.

The shared-objects reference named one concept several ways across its entries - a record switched
off was ``active`` on eight of them and ``enabled`` on the inventory, a sort position was ``app_ord``,
``ord`` and ``menu_order`` - and a reader had to know which file it was in to know what to ask for.
``audits/TEST_VERSION_BEFORE_RELEASE_DETAIL_0_22_0.md`` section 1.4 set one standard per concept and
three stages to reach it:

* **A** - the reference declares the standard name, and the old one as a legacy spelling that
  ``check-objects`` reports as ``deprecated``;
* **B** - every reader accepts both (this module);
* **C** - ``data/`` moves to the standard names (:func:`standardize`, run by
  ``common.cli standardize-field-names``), and every writer writes the standard name.

C was planned to wait until every node ran a build with B, because a parser drops a name it does not
know without a word: ``active: false`` reaching a node that reads only ``enabled`` switches the
instance ON there. The operator took C on the master first (2026-09-23): a worker still on 0.21.0
keeps its own ``data/`` and is not deployed to until it runs 0.22.0.

The renames are listed once, here. The reference's ``legacy_fields`` are held to this table by the
suite, so the two cannot disagree about what a name means.
"""

from __future__ import annotations

import json
from typing import Any

#: object -> {legacy name: standard name}. Only renames every reader was checked for belong here;
#: the ones 0.22.0 section 1.0 considered and did not make are listed there with the reason.
RENAMES: dict[str, dict[str, str]] = {
    "db_instance": {
        "enabled": "active",
        "env": "environment",
        "ord": "sort_order",
        "database": "database_name",
        # `instance-add` asked for the database under `db_name` and wrote it there, while the
        # reference called `db_name` a legacy `service_name` and three readers used it as the
        # service LABEL. One name, two meanings: the connection never read it as a database, so a
        # PostgreSQL target registered with only `db_name` connected to its label instead.
        "db_name": "database_name",
    },
    "app_command": {"app_ord": "sort_order"},
    "telegram_support_command": {"menu_order": "sort_order"},
    # `databases` held {source_database, target_database} MAPPINGS here, and a plain list of
    # database NAMES in every restore request built from it (`verify-restore`, the restore
    # steps). The record takes the name that says what it holds.
    "restore_entry": {"databases": "database_mappings"},
    # The sentence a person reads for a SQL task. `display_name` is what app commands already call
    # the same thing, and `sql_name` read as the name OF some SQL - a script, an object - which it
    # never was.
    "sql_command": {"sql_name": "display_name"},
    # A credential's free text was `notes` where every other record says `note`.
    "database_credential": {"notes": "note"},
    "remote_credential": {"notes": "note"},
    # A console block's position was `ord`; `sort_order` everywhere else.
    "webhost_app": {"ord": "sort_order"},
    # A secret REF is `password_ref` everywhere. `password_env` meant a ref in these files and an
    # ENVIRONMENT VARIABLE's name in ssh_auth, remote_exec and sre_config - one name, two meanings.
    "restore_source": {"password_env": "password_ref", "api_link_get_cer": "certificate_api_url"},
    "restore_target": {"password_env": "password_ref", "sql_password_env": "sql_password_ref",
                       "restore_sql_password_env": "sql_password_ref",
                       "restore_sql_instance": "sql_instance", "restore_sql_username": "sql_username"},
    "docker_db_connection": {"engine": "db_type", "database": "database_name",
                             "password_env": "password_ref"},
    "store_postgresql": {"database": "database_name", "password_env": "password_ref"},
    "backup_policy_override": {"database": "database_name"},
    "restore_drill_override": {"database": "database_name"},
    # `name` is an IDENTIFIER on a metric variant and a SQL parameter; here it was the title.
    "sla_policy": {"name": "display_name", "slo_target": "objective_percent",
                   "aggregation_method": "aggregation", "operator": "comparison_operator"},
    "telegram_settings": {"groups": "level_chat_map"},
}

#: Records whose switch was a STRING ``status: "active"`` and is now the boolean ``active`` every
#: other record carries. Not a rename - the value changes type - so it is not in RENAMES: readers ask
#: :func:`db_ops.lib.target_flags.is_record_active`, and ``upgrade-config`` moves the files.
STATUS_TO_ACTIVE: tuple[str, ...] = ("telegram_group", "telegram_user")

#: Legacy spellings stage C moves in the files but which are NOT handed to readers under both names.
#:
#: ``sqlserver_major_version`` is only a SQL Server spelling: giving every record both names would
#: stamp a PostgreSQL instance's ``major_version`` onto it as a SQL Server version. Its readers
#: already take ``major_version`` first and ``<db_type>_major_version`` after it.
MOVED_ONLY: dict[str, dict[str, str]] = {
    "db_instance": {"sqlserver_major_version": "major_version"},
}


def standard_of(object_name: str, name: str) -> str:
    """The standard spelling of ``name`` on ``object_name`` - itself when it is not a legacy one."""
    return RENAMES.get(object_name, {}).get(name, name)


def read(record: Any, object_name: str, standard: str, default: Any = None) -> Any:
    """``record[standard]``, else the legacy spelling's value, else ``default``.

    The standard name wins when both are present: a record half-migrated by hand says what it
    means in the new spelling, and the old one is what was left behind.
    """
    if not isinstance(record, dict):
        return default
    if standard in record:
        return record[standard]
    for legacy, target in RENAMES.get(object_name, {}).items():
        if target == standard and legacy in record:
            return record[legacy]
    return default


def with_both_names(record: dict[str, Any], object_name: str) -> dict[str, Any]:
    """A copy of ``record`` carrying every renamed field under BOTH spellings.

    Applied once, where a file is read, so the many readers downstream - old code asking for
    ``enabled``, new code asking for ``active`` - all see the value whichever spelling the file
    uses. A copy, never the record itself, and never on a path that writes the file back: a record
    written with both spellings would be the half-migrated state this exists to avoid.
    """
    if not isinstance(record, dict):
        return record
    out = dict(record)
    for legacy, standard in RENAMES.get(object_name, {}).items():
        if standard in record:
            out[legacy] = record[standard]
        elif legacy in record:
            out[standard] = record[legacy]
    return out


def standardize(record: dict[str, Any], object_name: str
                ) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """``record`` under the standard names: ``(new_record, changes, conflicts)``.

    Each legacy key is renamed IN PLACE, so the key order - and therefore the diff - is kept. When
    several spellings of one concept are present with the SAME value (``db_name`` and ``database``
    both saying ``db_ops``), the first becomes the standard name and the rest are dropped. With
    DIFFERENT values the record is left exactly as it is and reported as a conflict: which one a
    person meant is not something to guess at.
    """
    table = {**RENAMES.get(object_name, {}), **MOVED_ONLY.get(object_name, {})}
    spellings: dict[str, list[str]] = {}
    for key in record:
        standard = table.get(key, key)
        if standard in table.values():
            spellings.setdefault(standard, []).append(key)
    conflicts = [
        {"standard": standard, "values": {key: record[key] for key in keys}}
        for standard, keys in spellings.items()
        if len({json_key(record[key]) for key in keys}) > 1
    ]
    if conflicts:
        return dict(record), [], conflicts

    changes: list[dict[str, Any]] = []
    out: dict[str, Any] = {}
    for key, value in record.items():
        standard = table.get(key, key)
        if standard in out:
            if key != standard:
                changes.append({"field": key, "to": standard, "action": "dropped, same value"})
            continue
        out[standard] = value
        if key != standard:
            changes.append({"field": key, "to": standard, "action": "renamed"})
    return out, changes, []


def json_key(value: Any) -> str:
    """A comparable form of any JSON value, so ``[1, 2]`` and ``{"a": 1}`` can be told apart."""
    return json.dumps(value, sort_keys=True, default=str)
