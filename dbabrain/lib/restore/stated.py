"""The fields of a backup or restore record that decide what its run does - stated on every record
(0.39.0, 1.366).

The operator, 2026-11-13 (translated): *use true, false, and these fields must be required; in backup
restore most fields must be required, because it is very important - a mistake can ruin the whole
production.* And 2026-11-14: *time_window and active must be required; backup and restore are very
important in production, so nearly every field must be required, and there is no fallback anywhere.*

A record already had to state what it acts on: a restore entry its ``action``, ``restore_from``,
``integrity``, ``overwrite_existing`` and, for SQL Server, which databases under which names; a backup
entry its ``offload_*`` fields and each job its databases. The fields here decided the rest of a run
from a value in the code that nobody could read in the record. Four kinds of record, one table each:

* a **script-driven restore entry** (Oracle, PostgreSQL, a container-to-container SQL Server drill);
* a **share-driven restore entry** (SQL Server read from an SMB share) - a field stated on the
  ``backup_restore`` block above it is stated, as the loader reads it;
* a **backup entry**, and
* each **backup job** of one.

A record that leaves one out still **loads** - one such record must not stop every other record of
its file - and is refused **inside its own run**, by name, saying what the field decides, as an
unstated ``action`` is (rules R50). ``upgrade-config`` (``stated-integrity``) writes into each record
the value it had been running on, so an upgraded node runs exactly as it did, with the decision now
on the page; ``restore-add`` and ``backup-add`` write them into a new record.

``time_window`` is the one shared object read here, and only on these records: its parsing is the
same for every app (``lib.time_window``). A backup job or restore entry states three of its fields,
because their ``null`` meant *the caller's default* - ``repeat_interval`` (300 s here),
``retry_interval`` (the repeat interval, else 60 s) and ``timeout`` (no limit, never reaped: ``0``).
The ten bounds and ``weekdays`` stay optional: a bound left out is the field's own meaning - no
restriction on that dimension - not a default somebody chose.

What stays optional, and why (docs/08_backup_restore_app.md says the same, one line each):
``note`` (free text); a backup job's ``env_secrets``, ``backup_dir``, ``server_metadata`` and
``cleanup_retention`` (each overrides the value its entry states); a backup entry's ``notify`` (only
what a job that states none would inherit - every job states its own); the logins of a share-driven
entry (which ones apply follows the platform and the certificate source it states); the legacy
single-database spellings (the databases are stated by ``database_mappings`` or
``restore_all_databases``); ``sqlcmd_path`` / ``robocopy_path`` (a program on the host's PATH -
in a container ``sqlcmd_path`` is already required); the copy log's folders (where the copy writes its
own log); ``space_check.measure_restore`` (an opt-in only the share-driven path offers);
``server_metadata`` on an engine other than SQL Server (it cannot apply there).

Values and rules only: the loaders record what a record left out, the runs refuse it.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from dbabrain.lib import consistency_check, errors
from dbabrain.lib import integrity as restore_integrity
from dbabrain.lib.earlier_defaults import EARLIER_STAGING_MARKER
from dbabrain.lib.integrity import SCRUB_FILES_PER_RUN
from dbabrain.lib.restore.kind import is_script_restore
from dbabrain.lib.restore_space import DEFAULT_SAFETY_FACTOR, UNKNOWN_CHOICES
from dbabrain.lib.time_window import LEGACY_TOP_LEVEL_FIELDS

__all__ = [
    "BACKUP_ENTRY", "BACKUP_JOB", "DEFAULT_REPEAT_SECONDS", "FIELDS", "NOTIFY_RAN_ON", "NOTIFY_RULES",
    "RESTORE_PARSER_DEFAULTS", "SCRIPT_RESTORE", "SHARE_RESTORE", "Stated", "StatedError", "kind_of",
    "ran_on", "require_stated", "section_above", "state_record", "state_restore_fields", "unstated",
]


class StatedError(errors.ConfigError):
    """A backup or restore record leaves a field that decides its run to a default."""


#: The four kinds of record, as the refusals and the upgrade's changes name them.
SCRIPT_RESTORE = "script-driven restore entry"
SHARE_RESTORE = "share-driven restore entry"
BACKUP_ENTRY = "backup entry"
BACKUP_JOB = "backup job"

#: How long after a run's start a backup job or a restore entry with no ``repeat_interval`` was due
#: again - ``backup_restore.schedule`` hands it to the shared due rule, and it is what such a record
#: ran on. Matches the sql_tasks default.
DEFAULT_REPEAT_SECONDS = 300

#: The two rules of a ``notify`` object (``lib.notify.NOTIFY_RULE_NAMES``), and what a backup job or
#: restore entry that states no rule had been running on: on, routed by the event's own severity, to
#: the level's own chat - ``backup_restore.config.BACKUP_RESTORE_NOTIFY_DEFAULTS``. Plain
#: values rather than that object: ``lib.notify`` validates a level against the node's config, and
#: ``upgrade-config`` works from its request alone (rules R09). The suite holds the two to each other.
NOTIFY_RULES = ("logging_on_run", "alert_on_error")
NOTIFY_RAN_ON = {"enabled": True, "telegram_chat": "", "chat_id": ""}

#: What a share-driven restore entry reads where its file states nothing - the parser merges this
#: table under the ``backup_restore`` block and the entry, so the entry LOADS on it. Since 0.39.0
#: (1.366) every one of these that decides the run is a field of :data:`SHARE_RESTORE_FIELDS`: the
#: entry that leaves it here is refused inside its own run, and ``upgrade-config`` writes this value.
RESTORE_PARSER_DEFAULTS = {
    "copy_file_patterns": ["*.bak", "*.trn"],
    "copy_recent_hours": 24,
    # No copy_selection here since 0.39.0 (1.366): what a restore's copy takes is stated on the entry
    # (or on the section above it). A default in this table made every entry read as having stated it.
    "cleanup_retention": None,
    "full_backup_subdir": "FULL",
    "sqlcmd_path": "sqlcmd",
    "robocopy_path": "robocopy",
    "prod_smb_credential_target": "",
    "prod_smb_username": "",
    "prod_smb_password_env": "",
    "vm_credential_target": "",
    "vm_username": "",
    "vm_password_env": "",
    "restore_sql_instance_on_vm": "localhost",
    "restore_data_dir_on_vm": r"D:\MSSQL\DATA",
    "restore_sql_username": "",
    "restore_sql_password_env": "",
    "sql_login_timeout_seconds": 60,
    "sql_query_timeout_seconds": 0,
    "remote_command_timeout_seconds": 60,
    "restore_command_timeout_seconds": 0,
    "certificate_api_url": "",
    # No built-in ref (rules R50): an entry with a certificate API names its token's secret.
    "certificate_api_token_ref": "",
    # Verified by default (review 0.25.0, B5.4): the request carries the Vault token and returns the
    # certificate's private key. An internal CA is named with certificate_api_ca_file.
    "certificate_api_verify_tls": True,
    "certificate_api_ca_file": "",
}

#: The retry a record with no ``retry_interval`` and no ``repeat_interval`` ran on: the shared due
#: rule's own (``lib.time_window.explain_due``). With a repeat interval, the retry was that interval.
_RETRY_WITHOUT_REPEAT_SECONDS = 60
#: Unknown: what a record ran on could not be read (a notify rule that is neither an object nor a
#: word). The field is then left unstated - the loader refuses the record for its own reason first.
_UNKNOWN = object()


@dataclass(frozen=True)
class Stated:
    """One field a record of one kind states."""

    #: The path as written on the record: ``checkdb``, ``time_window.timeout``, ``target.sql_instance``.
    name: str
    #: What a record that left it out had been running on: a value, or a function of the record, the
    #: block above it and its parent (``(record, above, parent) -> value``).
    ran_on: Any
    #: What the field decides - the refusal says it, so the reader knows what is at stake.
    decides: str
    #: ``(record, above) -> bool`` when the field applies to some records of its kind only; ``None``
    #: for every one.
    applies: Callable[[dict[str, Any], dict[str, Any] | None], bool] | None = None
    #: The other paths the loader reads the same value from - a legacy or flat spelling states it too.
    spellings: tuple[str, ...] = ()
    #: A stated ``null`` is an answer (``tier: null`` - a drill nobody tiered).
    null_states: bool = False
    #: A stated ``""`` is an answer (``target_container: ""`` - the engine on the host itself).
    blank_states: bool = False

    def value_for(self, record: dict[str, Any], above: dict[str, Any] | None = None,
                  parent: dict[str, Any] | None = None) -> Any:
        """What ``record`` ran on for this field - a fresh object each call."""
        value = self.ran_on(record, above, parent) if callable(self.ran_on) else self.ran_on
        return value if value is _UNKNOWN else copy.deepcopy(value)


# --------------------------------------------------------------------------- #
# Reading a record as its loader does
# --------------------------------------------------------------------------- #
def _holders(record: dict[str, Any], head: str, above: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Where a share-driven entry's ``head`` is read: ``source`` / ``target`` key by key, the entry's
    before the block's (``config._merge_source_config``); any other key from the entry when it is
    there and not null, else from the block - the entry's object replaces the block's whole."""
    outer = above if isinstance(above, dict) else None
    if head in ("source", "target"):
        return [holder for holder in (record, outer) if isinstance(holder, dict)]
    for holder in (record, outer):
        if isinstance(holder, dict) and holder.get(head) is not None:
            return [holder]
    return [record] if head in record else []


def _walk(holder: Any, path: str) -> tuple[bool, Any]:
    node = holder
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node


def _value(record: dict[str, Any], path: str, above: dict[str, Any] | None = None) -> tuple[bool, Any]:
    """``(found, value)`` of ``path`` as the loader reads it."""
    for holder in _holders(record, path.split(".", 1)[0], above):
        found, value = _walk(holder, path)
        if found:
            return True, value
    return False, None


def _answers(field: Stated, value: Any) -> bool:
    if value is None:
        return field.null_states
    if isinstance(value, str) and not value.strip():
        return field.blank_states
    return True


def _stated(field: Stated, record: dict[str, Any], above: dict[str, Any] | None) -> bool:
    for path in (field.name, *field.spellings):
        for holder in _holders(record, path.split(".", 1)[0], above):
            found, value = _walk(holder, path)
            if found and _answers(field, value):
                return True
    return False


def _text(record: dict[str, Any], name: str) -> str:
    return str(record.get(name) or "").strip()


# --------------------------------------------------------------------------- #
# Which records a field applies to, and what a record ran on
# --------------------------------------------------------------------------- #
def _sqlserver(record: dict[str, Any], _above: Any = None) -> bool:
    # The engine whose server-level state a backup exports and a restore replays
    # (backup_restore.server_metadata refuses the block on any other).
    return _text(record, "db_type").lower() == "sqlserver"


def _remote(record: dict[str, Any], _above: Any = None) -> bool:
    target = _text(record, "target_server_id")
    return bool(target) and target != _text(record, "server_id")


def _stages(record: dict[str, Any], _above: Any = None) -> bool:
    return bool(_text(record, "target_backup_dir"))


def _copies_a_window(record: dict[str, Any], _above: Any = None) -> bool:
    return _text(record, "copy_selection").lower() == "window"


def _checks_space(record: dict[str, Any], above: Any = None) -> bool:
    found, enabled = _value(record, "space_check.enabled", above)
    return not (found and enabled is False)


def _states_integrity(record: dict[str, Any], above: Any = None) -> bool:
    # A missing block is refused by the copy gate as a whole (lib.integrity); its fields are asked for
    # once there is a block - written without copy_max_retry, a block would not even load.
    return isinstance(record.get("integrity"), dict)


def _certificate_api(record: dict[str, Any], above: Any = None) -> bool:
    # Every spelling the share-driven parser reads the API's address from.
    for path in ("source.certificate_api_url", "source.api_link_get_cer", "certificate_api_url", "api_link_get_cer"):
        found, value = _value(record, path, above)
        if found and isinstance(value, str) and value.strip():
            return True
    return False


def _repeat_written(record: dict[str, Any], above: Any) -> Any:
    found, value = _value(record, "time_window.repeat_interval", above)
    if found and value is not None:
        return value
    # The window's field wins over a legacy top-level one; with none in the window, the legacy one.
    return next((record[legacy] for legacy in _legacy("repeat_interval") if record.get(legacy) is not None), None)


def _retry_ran_on(record: dict[str, Any], above: Any, _parent: Any) -> int:
    """The due rule's ``retry_default`` for backup_restore is the window's own repeat interval
    (``schedule.is_due``), and with none it is the rule's 60 s. A manual record (-1) never retries,
    so 60 is written there: a stated number nothing reads."""
    try:
        repeat = int(_repeat_written(record, above))
    except (TypeError, ValueError):
        return _RETRY_WITHOUT_REPEAT_SECONDS
    return repeat if repeat >= 0 else _RETRY_WITHOUT_REPEAT_SECONDS


def _rule_as_read(holder: Any, rule: str, base: dict[str, Any]) -> dict[str, Any] | None:
    """One rule as ``lib.notify.parse_notify_rule`` reads it, over ``base`` (what an absent one is):
    the nested block's rule, else the legacy top-level key; ``true`` / ``false`` the switch alone; an
    object field by field. ``None`` for a shape the parser refuses."""
    if not isinstance(holder, dict):
        return dict(base)
    block = holder.get("notify") if isinstance(holder.get("notify"), dict) else {}
    raw = block.get(rule, holder.get(rule))
    if raw is None:
        return dict(base)
    if isinstance(raw, bool):
        return {**base, "enabled": raw}
    if not isinstance(raw, dict):
        return None
    return {"enabled": bool(raw.get("enabled", base["enabled"])),
            "telegram_chat": str((raw["telegram_chat"] if "telegram_chat" in raw else base["telegram_chat"]) or "")
            .strip().lower(),
            "chat_id": str(raw.get("chat_id") or "").strip()}


def _notify_ran_on(rule: str, part: str) -> Callable[[dict[str, Any], Any, Any], Any]:
    def ran(record: dict[str, Any], above: Any, parent: Any) -> Any:
        holders = _holders(record, "notify", above)
        view: dict[str, Any] = {"notify": holders[0]["notify"]} if holders else {}
        view.update({name: record[name] for name in NOTIFY_RULES if name in record})
        # A job's rule is its entry's where it states none (backup_restore.backup.load_backup_jobs).
        base = _rule_as_read(parent, rule, NOTIFY_RAN_ON) if isinstance(parent, dict) else dict(NOTIFY_RAN_ON)
        resolved = _rule_as_read(view, rule, base) if base is not None else None
        return _UNKNOWN if resolved is None else resolved[part]

    return ran


def _integrity_ran_on(name: str) -> Any:
    return getattr(restore_integrity.RestoreIntegrity(), name)


def _legacy(name: str) -> tuple[str, ...]:
    """The top-level spellings ``lib.time_window`` still reads for one window field."""
    return tuple(legacy for legacy, field_name in LEGACY_TOP_LEVEL_FIELDS.items() if field_name == name)


def _space_check_ran_on(name: str) -> Any:
    return {"enabled": True, "factor": DEFAULT_SAFETY_FACTOR, "on_unknown": UNKNOWN_CHOICES[0]}[name]


_ACTIVE = "whether the scheduler runs it at all"
_REPEAT = "how long after a run's start the next one is due"
_RETRY = "how long after a failed run's start it is tried again"
# One sentence for every record that states it - one name means one thing (the guard holds it): a
# backup job's script is stopped at it as well.
_TIMEOUT = ("how long a run may take before it is closed as abandoned - a backup's script is stopped then "
            "(0: no limit)")
#: What a backup job that stated no timeout ran under: the request builder handed its script 3600 s
#: (`timeout or 3600`) until the operator removed that fallback (2026-11-14: *remove it*), so 3600 is
#: written - the bound it actually ran under - and a stated 0 now means no limit, as it says.
BACKUP_SCRIPT_TIMEOUT_RAN_ON = 3600
_NOTIFY_ENABLED = "whether the {rule} message is sent"
_NOTIFY_CHAT = "which Telegram level the {rule} message goes to (\"\": the event's own severity)"


def _schedule_and_notify(*, timeout_ran_on: int = 0) -> tuple[Stated, ...]:
    """The fields a backup job and a restore entry share: when it runs, and who is told."""
    rows = [
        Stated("active", True, _ACTIVE),
        Stated("time_window.repeat_interval", DEFAULT_REPEAT_SECONDS, _REPEAT, spellings=_legacy("repeat_interval")),
        Stated("time_window.retry_interval", _retry_ran_on, _RETRY),
        Stated("time_window.timeout", timeout_ran_on, _TIMEOUT, spellings=_legacy("timeout")),
    ]
    for rule in NOTIFY_RULES:
        rows.append(Stated(f"notify.{rule}.enabled", _notify_ran_on(rule, "enabled"),
                           _NOTIFY_ENABLED.format(rule=rule)))
        rows.append(Stated(f"notify.{rule}.telegram_chat", _notify_ran_on(rule, "telegram_chat"),
                           _NOTIFY_CHAT.format(rule=rule), blank_states=True))
    return tuple(rows)


#: What both kinds of restore entry state.
_RESTORE_COMMON: tuple[Stated, ...] = (
    Stated("allow_production_target", False,
           "whether it may restore onto an instance the inventory calls production"),
    Stated("checkdb", True, "whether the consistency check runs on what was restored"),
    Stated("copy_selection", "chain",
           "what its copy takes - the chain the restore applies, or every file of a window"),
    Stated("space_check.enabled", _space_check_ran_on("enabled"),
           "whether the copy must fit the target's free space first"),
    Stated("space_check.factor", _space_check_ran_on("factor"),
           "how much room beyond the copy's own bytes the target must have", applies=_checks_space),
    Stated("space_check.on_unknown", _space_check_ran_on("on_unknown"),
           "what happens when the free space cannot be read - refuse or proceed", applies=_checks_space),
    *_schedule_and_notify(),
    Stated("checkdb_timeout_seconds", consistency_check.DEFAULT_TIMEOUT_SECONDS,
           "how long the consistency check may run on the target"),
    Stated("checkdb_jobs", consistency_check.DEFAULT_JOBS,
           "on how many connections the consistency check runs"),
    Stated("tier", None, "how often the drill must succeed before the reports call it overdue (null: no tier)",
           null_states=True),
    Stated("integrity.algorithm", _integrity_ran_on("algorithm"),
           "the hash each staged file is proven by", applies=_states_integrity),
    Stated("integrity.copy_retry_interval_seconds", _integrity_ran_on("copy_retry_interval_seconds"),
           "how long a copy whose hash differs waits before it is copied again", applies=_states_integrity),
    Stated("integrity.verify_source_before_copy", _integrity_ran_on("verify_source_before_copy"),
           "whether the source is hashed on its own server before the copy", applies=_states_integrity),
    Stated("integrity.uncatalogued", _integrity_ran_on("uncatalogued"),
           "what is done with a file no backup catalogued", applies=_states_integrity),
    Stated("integrity.engine_verify_before_restore", _integrity_ran_on("engine_verify_before_restore"),
           "whether the engine reads the backup through before it is restored", applies=_states_integrity),
)

_SERVER_METADATA = "whether the instance's server-level state (logins, jobs, ...) goes with it"
_HOURS = "how many hours back the copy reaches - a window's every file, a share's staged FULL"
_ENV = "what its script is told beyond what the tool hands it"
_ENV_SECRETS = "which secrets reach its script, by ref"

#: A script-driven restore entry (Oracle, PostgreSQL, a container-to-container SQL Server drill).
SCRIPT_RESTORE_FIELDS: tuple[Stated, ...] = _RESTORE_COMMON + (
    Stated("server_metadata.enabled", False, _SERVER_METADATA, applies=_sqlserver),
    Stated("env", {}, _ENV),
    Stated("env_secrets", {}, _ENV_SECRETS),
    Stated("target_container", "", "the container the restore goes into (\"\": the engine on the target host itself)",
           blank_states=True),
    Stated("target_visible_dir", lambda record, _above, _parent: _text(record, "target_backup_dir"),
           "the staged folder as the target's database sees it", applies=_remote),
    Stated("copy_mode", "auto", "how the files cross to the target - auto, cat or sftp", applies=_remote),
    Stated("staging_marker", EARLIER_STAGING_MARKER,
           "the file that marks the target's staging folder as this tool's", applies=_stages),
    Stated("copy_recent_hours", RESTORE_PARSER_DEFAULTS["copy_recent_hours"], _HOURS, applies=_copies_a_window),
)

#: A share-driven restore entry (SQL Server read from an SMB share). Each may be stated on the
#: ``backup_restore`` block above it instead, as the loader reads it.
SHARE_RESTORE_FIELDS: tuple[Stated, ...] = _RESTORE_COMMON + (
    Stated("server_metadata.enabled", False, _SERVER_METADATA),
    Stated("copy_file_patterns", RESTORE_PARSER_DEFAULTS["copy_file_patterns"], "which files of the share are copied"),
    Stated("copy_recent_hours", RESTORE_PARSER_DEFAULTS["copy_recent_hours"], _HOURS),
    Stated("full_backup_subdir", RESTORE_PARSER_DEFAULTS["full_backup_subdir"],
           "the folder the FULL backups are found in"),
    Stated("execution_mode", "sync", "whether the steps run one after another or apart (sync / unsync)"),
    Stated("target.vm_platform", "windows", "the target machine's platform - windows or linux",
           spellings=("vm_platform",)),
    Stated("target.sql_instance", RESTORE_PARSER_DEFAULTS["restore_sql_instance_on_vm"],
           "the SQL Server instance the backup is restored onto",
           spellings=("target.restore_sql_instance", "restore_sql_instance_on_vm")),
    Stated("target.restore_data_dir", RESTORE_PARSER_DEFAULTS["restore_data_dir_on_vm"],
           "where the restored data files go", spellings=("restore_data_dir_on_vm",)),
    Stated("target.sql_container", "", "the container sqlcmd runs in (\"\": the target host's own sqlcmd)",
           spellings=("sql_container",), blank_states=True),
    Stated("target.sql_login_timeout_seconds", RESTORE_PARSER_DEFAULTS["sql_login_timeout_seconds"],
           "how long sqlcmd waits to log in", spellings=("sql_login_timeout_seconds",)),
    Stated("target.sql_query_timeout_seconds", RESTORE_PARSER_DEFAULTS["sql_query_timeout_seconds"],
           "how long one statement may run (0: no limit)", spellings=("sql_query_timeout_seconds",)),
    Stated("target.remote_command_timeout_seconds", RESTORE_PARSER_DEFAULTS["remote_command_timeout_seconds"],
           "how long a command on the target may run", spellings=("remote_command_timeout_seconds",)),
    Stated("target.restore_command_timeout_seconds", RESTORE_PARSER_DEFAULTS["restore_command_timeout_seconds"],
           "how long the RESTORE may run (0: no limit)", spellings=("restore_command_timeout_seconds",)),
    Stated("source.certificate_api_verify_tls", RESTORE_PARSER_DEFAULTS["certificate_api_verify_tls"],
           "whether the certificate API's TLS certificate is verified", applies=_certificate_api,
           spellings=("certificate_api_verify_tls",)),
)

#: A backup entry - one machine's backup set. Its jobs carry the schedules.
BACKUP_ENTRY_FIELDS: tuple[Stated, ...] = (
    Stated("active", True, _ACTIVE),
    Stated("env_secrets", {}, _ENV_SECRETS),
    Stated("scrub_files_per_run", SCRUB_FILES_PER_RUN, "how many older files each run re-hashes where they lie"),
    Stated("server_metadata.enabled", False, _SERVER_METADATA, applies=_sqlserver),
)

#: Each job of a backup entry: when it runs, what its script is told, and who is told.
BACKUP_JOB_FIELDS: tuple[Stated, ...] = (
    *_schedule_and_notify(timeout_ran_on=BACKUP_SCRIPT_TIMEOUT_RAN_ON),
    Stated("env", {}, _ENV),
)

FIELDS: dict[str, tuple[Stated, ...]] = {
    SCRIPT_RESTORE: SCRIPT_RESTORE_FIELDS, SHARE_RESTORE: SHARE_RESTORE_FIELDS,
    BACKUP_ENTRY: BACKUP_ENTRY_FIELDS, BACKUP_JOB: BACKUP_JOB_FIELDS,
}

#: What each field decides, by name - the refusal's words. One name means one thing in every table.
_DECIDES: dict[str, str] = {}
for _table in FIELDS.values():
    for _field in _table:
        _DECIDES.setdefault(_field.name, _field.decides)


# --------------------------------------------------------------------------- #
# The rule
# --------------------------------------------------------------------------- #
def kind_of(record: Any) -> str:
    """A restore entry's kind - the one rule of ``lib.restore.kind``."""
    return SCRIPT_RESTORE if is_script_restore(record if isinstance(record, dict) else {}) else SHARE_RESTORE


def section_above(section: Any) -> dict[str, Any]:
    """The ``backup_restore`` block as a share-driven entry inherits it: its own keys, not its lists."""
    if not isinstance(section, dict):
        return {}
    return {key: value for key, value in section.items() if key not in ("backups", "restores", "sources")}


def _applies(field: Stated, record: dict[str, Any], above: dict[str, Any] | None) -> bool:
    return field.applies is None or field.applies(record, above)


def unstated(record: Any, *, kind: str | None = None, above: dict[str, Any] | None = None) -> tuple[str, ...]:
    """The fields of ``kind`` (a restore entry's own, when not given) that ``record`` - a mapping, as
    written - does not state. ``above`` is the block a share-driven entry inherits from."""
    if not isinstance(record, dict):
        return ()
    kind = kind or kind_of(record)
    held = above if kind == SHARE_RESTORE else None
    return tuple(field.name for field in FIELDS[kind]
                 if _applies(field, record, held) and not _stated(field, record, held))


def ran_on(name: str, record: dict[str, Any] | None = None, *, kind: str | None = None,
           above: dict[str, Any] | None = None, parent: dict[str, Any] | None = None) -> Any:
    """What a record that did not state ``name`` had been running on (a fresh object each call)."""
    record = record if isinstance(record, dict) else {}
    kind = kind or kind_of(record)
    field = next(field for field in FIELDS[kind] if field.name == name)
    return field.value_for(record, above if kind == SHARE_RESTORE else None, parent)


def require_stated(names: Any, *, label: str = "restore") -> None:
    """Refuse a record that left any of ``names`` unstated, saying what each decides."""
    missing = [name for name in (names or ()) if name in _DECIDES]
    if not missing:
        return
    why = "; ".join(f"{name} - {_DECIDES[name]}" for name in missing)
    raise StatedError(
        f"{label} states no {', '.join(missing)}, so a default nobody can read in the entry would decide: {why}. "
        "State " + ("it" if len(missing) == 1 else "them") + ", or run upgrade-config - it writes the value the "
        "entry has been running on.")


def _put(record: dict[str, Any], path: str, value: Any, above: dict[str, Any] | None) -> None:
    head, *rest = path.split(".")
    if rest and head not in ("source", "target") and record.get(head) is None \
            and isinstance(above, dict) and isinstance(above.get(head), dict):
        # The block's object is what the entry ran on; written alone, the entry's half-object would
        # replace all of it (the entry's object replaces the block's whole).
        record[head] = copy.deepcopy(above[head])
    node = record
    for part in [head, *rest][:-1]:
        if not isinstance(node.get(part), dict):
            node[part] = {}
        node = node[part]
    node[path.split(".")[-1]] = value


def state_record(record: dict[str, Any], *, kind: str | None = None, above: dict[str, Any] | None = None,
                 parent: dict[str, Any] | None = None) -> list[tuple[str, Any]]:
    """Write into ``record`` each field of its kind it does not state, at the value it had been
    running on. What it states is left as it is. Every value is read before the first is written - a
    retry interval is the repeat interval the record had, not the one written beside it. Returns
    ``[(field, value)]`` in the table's order."""
    if not isinstance(record, dict):
        return []
    kind = kind or kind_of(record)
    held = above if kind == SHARE_RESTORE else None
    writes: list[tuple[str, Any]] = []
    for name in unstated(record, kind=kind, above=held):
        field = next(field for field in FIELDS[kind] if field.name == name)
        value = field.value_for(record, held, parent)
        if value is not _UNKNOWN:
            writes.append((name, value))
    # A notify rule written where the record had none, or only a word, is written whole: the chat it
    # was routed to is part of what it ran on (a job's comes from its entry).
    for rule in NOTIFY_RULES:
        if any(name.startswith(f"notify.{rule}.") for name, _value_ in writes):
            found, current = _value(record, f"notify.{rule}", held)
            if not (found and isinstance(current, dict)):
                chat = _notify_ran_on(rule, "chat_id")(record, held, parent)
                if chat is not _UNKNOWN:
                    writes.append((f"notify.{rule}.chat_id", chat))
    for name, value in writes:
        _put(record, name, value, held)
    return writes


def _where(kind: str, record: dict[str, Any], index: int, parent: dict[str, Any] | None = None) -> str:
    if kind == BACKUP_ENTRY:
        return f"backup_restore.backups[{record.get('backup_id') or index}]"
    if kind == BACKUP_JOB:
        owner = (parent or {}).get("backup_id") or "?"
        return f"backup_restore.backups[{owner}].jobs[{record.get('job') or index}]"
    return f"backup_restore.restores[{record.get('restore_id') or index}]"


def state_restore_fields(document: dict[str, Any]) -> list[dict[str, str]]:
    """Write into every record of a ``restore_config.json`` - each restore entry, each backup entry
    and each of its jobs - every field of its kind it does not state, at the value it had been running
    on (:func:`state_record`). One change per field written."""
    changes: list[dict[str, str]] = []
    section = document.get("backup_restore") if isinstance(document, dict) else None
    if not isinstance(section, dict):
        return changes
    above = section_above(section)

    def note(kind: str, record: dict[str, Any], index: int, writes: list[tuple[str, Any]],
             parent: dict[str, Any] | None = None) -> None:
        where = _where(kind, record, index, parent)
        for name, value in writes:
            changes.append({"where": where, "field": name,
                            "value": f"{json_text(value)}, stated - what the {kind} had been running on"})

    restores = section.get("restores")
    for index, entry in enumerate(restores if isinstance(restores, list) else []):
        if isinstance(entry, dict):
            kind = kind_of(entry)
            note(kind, entry, index, state_record(entry, kind=kind, above=above))
    backups = section.get("backups")
    for index, entry in enumerate(backups if isinstance(backups, list) else []):
        if not isinstance(entry, dict):
            continue
        # A job's notify rule is its entry's where it states none: read through the entry as written.
        for job_index, job in enumerate(entry.get("jobs") if isinstance(entry.get("jobs"), list) else []):
            if isinstance(job, dict):
                note(BACKUP_JOB, job, job_index, state_record(job, kind=BACKUP_JOB, parent=entry), parent=entry)
        note(BACKUP_ENTRY, entry, index, state_record(entry, kind=BACKUP_ENTRY))
    return changes


def json_text(value: Any) -> str:
    """A value as it reads in the file: ``false``, ``"chain"``, ``{...}``."""
    return json.dumps(value, ensure_ascii=False)
