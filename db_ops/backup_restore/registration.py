r"""Registering one backup entry or one restore entry, as a single operation.

Asked for in the same words that produced ``instance-add``:

    *"phải có cli thêm backup restore giống cli thêm instance nhé"* — 2026-09-13

Registering a backup was a hand-edit of ``data/restore_config.json``: a nested object, an array of
jobs inside it, a ``time_window`` and a ``cleanup_retention`` per job, and one field —
``env_secrets`` — that could only be filled by writing a password into ``secrets/secret_text.json``
in the clear and running ``encrypt-secret``. That last step is the one deleting the file afterwards
does not undo, and it is why this is a command rather than a longer section in the guide.

**It lives here rather than in ``common`` because this app owns the file.** ``docs/08`` says so, and
the layering makes it the only workable home anyway: validating an entry means loading it with
:func:`load_backup_jobs` / :func:`load_restore_configs`, and ``common`` may not import an app
(``tests/test_import_boundaries.py``). ``instance-add`` is in ``common`` for the mirror-image
reason — ``db_instances.json`` is ``common``'s to read.

**What is written is validated by this app's own loader, not by a schema kept here.** Those two
functions already refuse every malformed shape and already name the missing field. A second,
parallel set of rules in this module would be a set of rules that drifts, so the candidate document
is written to a temporary file, loaded through the real loader, and committed only if it loads. A
refusal here is therefore the exact sentence the scheduled app would have failed with at 01:00 —
and ``'prod_backup_share'`` as an entire error message cannot come back.

**``cleanup_retention`` stays mandatory**, in seconds, on every backup job and every restore entry.
It was made so on 2026-09-11 because six of fourteen restore entries silently carried none and ran
on whatever that release's default happened to be. An absent field reads exactly like a considered
one, so nothing here supplies it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from db_ops.common import data_sources
from db_ops.lib import secret_text
from db_ops.lib.json_io import atomic_write_text

#: The file both halves live in — by history rather than by fit: it has carried ``backups`` as well
#: as ``restores`` since backups became schedulable.
CONFIG_FILENAME = "restore_config.json"


class RegistrationError(RuntimeError):
    """The entry cannot be registered as asked. Nothing was written."""


BACKUP_ADD_USAGE = r"""usage: python -m db_ops.backup_restore.cli backup-add <json>|@<file>|- [--key-base64 ...]

Registers ONE backup entry in data/restore_config.json: the entry, its jobs, and any secret its
scripts read as an environment variable. The secret never reaches the disk in the clear.

  backup_id      required. Its name in list-backups and in every log line
  server_id      required. The machine it runs on - transport, address and credential
                 all come from db_instances.json through this
  db_type        optional; taken from that server_id's record when absent
  backup_dir     required, unless every job carries its own
  jobs           required, non-empty. One object per level:
                   job                 database | log | archivelog | wal | ...
                   script              e.g. assets/backup/sqlserver/mssql_backup_database.sh
                   cleanup_retention   REQUIRED, in SECONDS - how long that directory
                                       keeps what this job wrote. 691200 = 8 days
                   time_window         REQUIRED {from_hour,to_hour,repeat_interval,...}.
                                       Absent is not 'unscheduled': a job with no window
                                       repeats every 300s inside an always-open one
                   notify              REQUIRED here or on the entry: {logging_on_run,
                                       alert_on_error}, each {enabled, telegram_chat,
                                       chat_id}. Absent it still notifies, but to the
                                       NEUTRAL 'logging'/'error' levels and not this
                                       entry's own chat
                   env                 optional {NAME: value} - plain environment for the
                                       script, e.g. {"BACKUP_LEVEL": "full"}. Secrets go in
                                       env_secrets / env_secret_values, never here
                   active              optional; default true
  server_metadata  optional {enabled, artifacts, note} - on a successful backup, also
                 export this instance's server-level metadata (logins with their SIDs and
                 password hashes, server roles, Agent jobs)
  env_secrets    optional {ENV_NAME: secret_ref} - refs to secrets that already exist
  env_secret_values  optional {ENV_NAME: "the actual value"} - encrypted into the store
                 here and turned into a ref. This is the field that was otherwise a
                 plaintext hand-edit
  active, note   optional
  replace        overwrite an existing backup_id instead of refusing

The document is loaded back through load_backup_jobs before it is committed, so a refusal names
the field the scheduled app would have failed on. Verify with:

  python -m db_ops.backup_restore.cli list-backups
  python -m db_ops.backup_restore.cli backup --dry-run
"""


RESTORE_ADD_USAGE = r"""usage: python -m db_ops.backup_restore.cli restore-add <json>|@<file>|- [--key-base64 ...]

Registers ONE restore entry in data/restore_config.json: where the backups come from, the
instance they are restored onto, and which databases to carry across.

  restore_id     required
  cleanup_retention  REQUIRED, in SECONDS - how long the target keeps what this entry
                 staged for restoring. 86400 (a day) covers the next drill
  time_window    REQUIRED {from_hour, to_hour, repeat_interval, retry_interval, timeout}.
                 Absent, the entry is due on EVERY pass of APP-BACKUP-RESTORE - every
                 300 seconds - and a restore that takes longer than that simply runs
                 back to back. A daily drill is {"from_hour": 2, "to_hour": 5,
                 "repeat_interval": 72000, "retry_interval": 600, "timeout": 7200}
  source         required object: id, backup_share, credential_target, username,
                 and password_env (a secret REF) or password (encrypted here)
  target         required object: id, vm_platform, credential_target, username,
                 password_env / password, sql_instance, sql_username,
                 sql_password_env / sql_password, restore_data_dir, ...
  databases      list of {source_database, target_database}. An empty list goes on
                 restoring whatever it finds, including files nobody produces any more
  server_metadata  optional {enabled, artifacts, phases}
  notify         REQUIRED {logging_on_run, alert_on_error}, each {enabled, telegram_chat,
                 chat_id} - which chat this entry's runs and failures go to. Absent it
                 still notifies, but to the NEUTRAL 'logging'/'error' levels, which reads
                 as nothing having been sent from whichever group you are watching
  active, note   optional
  replace        overwrite an existing restore_id instead of refusing

password / sql_password inside source and target are encrypted into the store and replaced by
the matching *_env ref. Give the value or the ref, never both.

The document is loaded back through load_restore_configs before it is committed. Verify with:

  python -m db_ops.backup_restore.cli list-restores
"""


def _config_path(data_dir: str | Path | None) -> Path:
    root = Path(data_dir) if data_dir else data_sources.DEFAULT_DATA_DIR
    return root / CONFIG_FILENAME


def _read_document(path: Path) -> dict[str, Any]:
    """The whole file, with both arrays present. An absent file is an empty one, not an error."""
    if not path.exists():
        return {"backup_restore": {"backups": [], "restores": []}}
    try:
        data = json.loads(path.read_bytes().decode("utf-8-sig"))
    except ValueError as exc:
        raise RegistrationError(f"{path.name} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RegistrationError(f"{path.name} is not a JSON object.")
    section = data.setdefault("backup_restore", {})
    if not isinstance(section, dict):
        raise RegistrationError(f"{path.name}: backup_restore must be an object.")
    section.setdefault("backups", [])
    section.setdefault("restores", [])
    return data


def _validate(path: Path, document: dict[str, Any], loader) -> None:
    """Refuse a document this app's own loader would not accept, before anything is written.

    The candidate goes to a sibling file, the real loader is pointed at *that*, and the sibling is
    always removed. It runs **before** any secret is stored: a document the loader rejects must
    leave nothing behind at all, and storing the secret first would leave one in the encrypted
    store under a ref no config ever mentions - invisible, and impossible to tell from a live one.
    """
    scratch = path.with_name(path.name + ".candidate")
    atomic_write_text(scratch, json.dumps(document, ensure_ascii=False, indent=2) + "\n")
    try:
        loader(scratch)
    except KeyError as exc:
        # Defence in depth rather than the main path. `parse_restore_config` used to read six
        # fields by subscript, so a missing one arrived as a KeyError whose entire text was the key
        # in quotes; it checks them up front now and names every one, in the spelling an operator
        # writes (`config.REQUIRED_RESTORE_FIELDS`). This stays because a *new* unguarded subscript
        # anywhere under the loaders would otherwise reach the caller as a bare key again - and
        # because that is precisely how `'prod_backup_share'` cost a day in September 2026.
        raise RegistrationError(
            f"missing required field: {exc.args[0] if exc.args else exc}. The entry is incomplete "
            "for this restore shape - see restore-add --help and "
            "data/restore_config.example.json for a worked entry. Nothing was written.") from exc
    except Exception as exc:  # noqa: BLE001 - the loader's own sentence is the useful one.
        raise RegistrationError(
            "the entry was refused by the loader the scheduled app uses, so nothing was written: "
            f"{exc}") from exc
    finally:
        scratch.unlink(missing_ok=True)


def _write_document(path: Path, document: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(document, ensure_ascii=False, indent=2) + "\n")


def _ref_for(prefix: str, entry_id: str, name: str) -> str:
    """``BACKUP_ACME_APPDB_FULL_BACKUP_ENCRYPTION_PASSWORD`` — derived, so it cannot disagree."""
    def slug(text: str) -> str:
        out = "".join(char if char.isalnum() else "_" for char in str(text)).upper()
        while "__" in out:
            out = out.replace("__", "_")
        return out.strip("_")
    return f"{prefix}_{slug(entry_id)}_{slug(name)}"


def _store(root: Path, ref: str, value: str, key: str | None, *, overwrite: bool) -> None:
    if not key:
        raise RegistrationError(
            "a secret value was given but no passphrase is available to encrypt it. Export "
            "DB_OPS_SECRET_KEY, or pass --key-base64. Nothing was written.")
    try:
        secret_text.set_secret_text(root, ref, value, key=key, overwrite=overwrite)
    except RuntimeError as exc:
        raise RegistrationError(str(exc)) from exc


def _instance_field(root: Path, server_id: str, field: str) -> Any:
    """One field off the inventory record, so db_type need not be repeated by hand."""
    path = data_sources.db_instances_path(root)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_bytes().decode("utf-8-sig"))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    for item in data.get("db_instances") or []:
        if isinstance(item, dict) and str(item.get("server_id")) == server_id:
            return item.get(field)
    return None


def add_backup(request: dict[str, Any] | None = None, *,
               data_dir: str | Path | None = None,
               key: str | None = None) -> dict[str, Any]:
    """Register one ``backup_restore.backups[]`` entry and the secrets its scripts read."""
    from db_ops.backup_restore.backup import load_backup_jobs

    payload = dict(request or {})
    root = Path(data_dir) if data_dir else data_sources.DEFAULT_DATA_DIR
    path = _config_path(data_dir)
    replace = bool(payload.get("replace"))

    backup_id = str(payload.get("backup_id") or "").strip()
    server_id = str(payload.get("server_id") or "").strip()
    missing = [name for name, value in (("backup_id", backup_id), ("server_id", server_id))
               if not value]
    if missing:
        raise RegistrationError(
            f"missing required field(s): {', '.join(missing)}. A backup entry needs at least a "
            "backup_id (its name in list-backups) and a server_id (the machine it runs on).")

    jobs = payload.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise RegistrationError(
            f"{backup_id} needs a non-empty jobs array - one object per level, each with job, "
            "script and cleanup_retention (in seconds). Nothing schedules an entry with no jobs.")
    # Required for the same reason it is required on a restore entry, and it is the SAME rule:
    # backup jobs and restore entries both go through `schedule.is_due`, where a job carrying no
    # window gets an always-open window and `DEFAULT_REPEAT_SECONDS` - 300. "No time_window" does
    # not mean unscheduled, it means every five minutes, which for a full backup is as wrong as
    # it was for the restore that ran back to back for half an hour at a time on 2026-09-14.
    unnotified = [str(job.get("job") or f"#{index}")
                  for index, job in enumerate(jobs)
                  if isinstance(job, dict) and not (job.get("notify") or {})]
    if unnotified and not (payload.get("notify") or {}):
        raise RegistrationError(
            f"{backup_id}: job(s) {', '.join(unnotified)} need a notify object, or give one on the "
            "entry for all of them. Without it they still notify, but to the neutral 'logging' and "
            "'error' levels rather than this entry's own chat, which reads as nothing having been "
            'sent. Use {"logging_on_run": {"enabled": true, "telegram_chat": "backup"}, '
            '"alert_on_error": {"enabled": true, "telegram_chat": "backup"}}.')

    windowless = [str(job.get("job") or f"#{index}")
                  for index, job in enumerate(jobs)
                  if isinstance(job, dict) and not (job.get("time_window") or {})]
    if windowless:
        raise RegistrationError(
            f"{backup_id}: job(s) {', '.join(windowless)} need a time_window. Without one a job "
            "repeats every 300 seconds inside an always-open window - not 'unscheduled'. A nightly "
            'full is {"from_hour": 1, "to_hour": 4, "repeat_interval": 72000, '
            '"retry_interval": 1800, "timeout": 7200}; a log backup every 15 minutes is '
            '{"repeat_interval": 900, "retry_interval": 300, "timeout": 1800}.')

    document = _read_document(path)
    section = document["backup_restore"]
    existing = [item for item in section["backups"]
                if isinstance(item, dict) and str(item.get("backup_id")) == backup_id]
    if existing and not replace:
        raise RegistrationError(
            f"{backup_id} is already in {path.name}. Pass replace to overwrite it - a silent "
            "overwrite of a backup somebody else registered is not something to guess at.")

    db_type = str(payload.get("db_type") or "").strip().lower()
    if not db_type:
        db_type = str(_instance_field(root, server_id, "db_type") or "").strip().lower()

    env_secrets = dict(payload.get("env_secrets") or {})
    values = payload.get("env_secret_values") or {}
    if not isinstance(values, dict):
        raise RegistrationError("env_secret_values must be an object of {ENV_NAME: value}.")
    both = sorted(set(env_secrets) & set(values))
    if both:
        raise RegistrationError(
            f"{', '.join(both)} appears in both env_secrets (a ref) and env_secret_values (a "
            "value). Two answers to 'which secret' is how the wrong one gets used.")

    entry: dict[str, Any] = {name: value for name, value in payload.items()
                             if name not in {"replace", "env_secret_values"}}
    entry["backup_id"] = backup_id
    entry["server_id"] = server_id
    if db_type:
        entry["db_type"] = db_type
    entry.setdefault("active", True)

    # The refs are derived first and the values stored last, with validation in between: a ref is
    # only a string, so the document can be checked complete before a single secret exists.
    stored = {str(name): _ref_for("BACKUP", backup_id, name) for name in values}
    env_secrets.update(stored)
    if env_secrets:
        entry["env_secrets"] = env_secrets

    section["backups"] = [item for item in section["backups"]
                          if not (isinstance(item, dict)
                                  and str(item.get("backup_id")) == backup_id)]
    section["backups"].append(entry)
    _validate(path, document, load_backup_jobs)

    written: list[str] = []
    for name, value in values.items():
        _store(root, stored[str(name)], str(value), key, overwrite=replace)
    if stored:
        written.append(data_sources.secret_text_path(root).name)
    _write_document(path, document)
    written.append(path.name)

    loaded = [job for job in load_backup_jobs(path)
              if str(getattr(job, "backup_id", "")) == backup_id]
    return {
        "backup_id": backup_id,
        "server_id": server_id,
        "db_type": db_type,
        "jobs": len(jobs),
        "jobs_loaded": len(loaded),
        "secrets_stored": sorted(stored.values()),
        "replaced": bool(existing),
        "files_written": written,
        "next": [
            "db-ops backup_restore list-backups",
            "db-ops backup_restore backup --dry-run",
        ],
    }


def add_restore(request: dict[str, Any] | None = None, *,
                data_dir: str | Path | None = None,
                key: str | None = None) -> dict[str, Any]:
    """Register one ``backup_restore.restores[]`` entry and the secrets it logs in with."""
    from db_ops.backup_restore.config import (
        SCRIPT_RESTORE_DB_TYPES, is_script_restore, load_restore_configs)

    payload = dict(request or {})
    root = Path(data_dir) if data_dir else data_sources.DEFAULT_DATA_DIR
    path = _config_path(data_dir)
    replace = bool(payload.get("replace"))

    restore_id = str(payload.get("restore_id") or "").strip()
    if not restore_id:
        raise RegistrationError(
            "missing required field(s): restore_id. A restore entry needs a name before anything "
            "else can refer to it.")
    # Two shapes live in `restores[]`, and this command knew only one. `config.is_script_restore`
    # is the product's own answer to which is which - declaring a `script` makes an entry
    # script-driven, and so does an Oracle/PostgreSQL/MySQL `db_type` - and the SQL Server parser
    # already skips those rather than failing on the SMB/`.bak`/sqlcmd fields they do not carry.
    # Requiring `source` and `target` of every entry therefore refused, by construction, every
    # entry the engine path is written to step over.
    #
    # Measured 2026-09-17 rebuilding a node one command at a time: **9 of 14 production restores
    # were refused**, all with "needs a source object" - the Oracle/PostgreSQL drills and the
    # cross-site transfers. Each one runs today and each one had to go back to being a hand-edit
    # of restore_config.json, which is the hand-edit `restore-add` was added in 0.17.0 to replace.
    if is_script_restore(payload):
        # What a script-driven entry reads from. The script itself may come from `script` or be
        # implied by `db_type`, so `backup_dir` is the one field that is always its own.
        if not str(payload.get("backup_dir") or "").strip():
            raise RegistrationError(
                f"{restore_id} is script-driven (it declares a script, or a db_type of "
                f"{'/'.join(sorted(SCRIPT_RESTORE_DB_TYPES))}), so it needs backup_dir - the "
                "directory the script reads the backup from. A script-driven entry takes no "
                "source/target object; those belong to the SQL Server engine path.")
    else:
        for name in ("source", "target"):
            block = payload.get(name)
            if not isinstance(block, dict) or not block:
                raise RegistrationError(
                    f"{restore_id} needs a {name} object - see restore-add --help and "
                    "data/restore_config.example.json for the fields it takes. (An entry that "
                    "declares a script, or an oracle/postgresql/mysql db_type, is script-driven "
                    "and needs backup_dir instead.)")
    # Checked here rather than left to the loader, because the loader accepts the two legacy
    # spellings and this command must not let a new entry be written in them.
    if "cleanup_retention" not in payload:
        raise RegistrationError(
            f"{restore_id} needs cleanup_retention, in SECONDS - how long the target keeps what "
            "this entry staged for restoring. It is mandatory because an absent value reads "
            "exactly like a considered one; 86400 (a day) covers the next drill.")
    # Required for the same reason, and for one measured on 2026-09-14: an entry registered
    # without a window restored a 183 GB database back to back for half an hour at a time,
    # because "no window" means due on every pass of APP-BACKUP-RESTORE and that runs every 300
    # seconds. The target lost about 5 GB of free disk per cycle and nothing in the logs called
    # it unusual - each run reported success.
    # Required for a third reason, measured 2026-09-15: an entry with no notify object still
    # notifies - the loader defaults it on, deliberately - but to the NEUTRAL levels, `logging` and
    # `error`. So a restore registered here posted "Restore workflow started" into the Logs group
    # while the operator watched the Restore group and reported that nothing was sent at all. Every
    # hand-written entry on this estate routes to its own chat; only the ones this command made did
    # not, because the field was not offered.
    if not (payload.get("notify") or {}):
        raise RegistrationError(
            f"{restore_id} needs a notify object. Without one it still notifies, but to the "
            "neutral 'logging' and 'error' levels rather than this entry's own chat - which reads "
            "as nothing having been sent, from whichever group you are watching. Use "
            '{"logging_on_run": {"enabled": true, "telegram_chat": "restore"}, '
            '"alert_on_error": {"enabled": true, "telegram_chat": "restore"}}, '
            "or state the levels you do want.")

    if not (payload.get("time_window") or {}):
        raise RegistrationError(
            f"{restore_id} needs a time_window. Without one it is due on EVERY pass of "
            "APP-BACKUP-RESTORE - every 300 seconds - so a restore taking longer than that runs "
            'back to back for as long as the daemon is up. A daily drill is {"from_hour": 2, '
            '"to_hour": 5, "repeat_interval": 72000, "retry_interval": 600, "timeout": 7200}.')

    document = _read_document(path)
    section = document["backup_restore"]
    existing = [item for item in section["restores"]
                if isinstance(item, dict) and str(item.get("restore_id")) == restore_id]
    if existing and not replace:
        raise RegistrationError(
            f"{restore_id} is already in {path.name}. Pass replace to overwrite it - a silent "
            "overwrite of a restore somebody else registered is not something to guess at.")

    entry: dict[str, Any] = {name: value for name, value in payload.items() if name != "replace"}
    entry["restore_id"] = restore_id
    entry.setdefault("active", True)

    #: The password fields each block may carry inline, and the ref field each becomes.
    inline = (("source", "password", "password_env"),
              ("target", "password", "password_env"),
              ("target", "sql_password", "sql_password_env"))
    stored: list[tuple[str, str]] = []
    for block_name, value_field, ref_field in inline:
        block = dict(entry.get(block_name) or {})
        if value_field not in block:
            continue
        if block.get(ref_field):
            raise RegistrationError(
                f"{block_name}.{value_field} and {block_name}.{ref_field} were both given. Pass "
                "the value to have it encrypted here, or the ref to point at a secret that "
                "already exists - not both.")
        ref = _ref_for("RESTORE", restore_id, f"{block_name}_{value_field}")
        # Popped from the block now: the value must never reach the document, not even the
        # candidate one that only exists for as long as the loader takes to read it.
        stored.append((ref, str(block.pop(value_field))))
        block[ref_field] = ref
        entry[block_name] = block

    section["restores"] = [item for item in section["restores"]
                           if not (isinstance(item, dict)
                                   and str(item.get("restore_id")) == restore_id)]
    section["restores"].append(entry)
    _validate(path, document, load_restore_configs)

    written: list[str] = []
    for ref, value in stored:
        _store(root, ref, value, key, overwrite=replace)
    if stored:
        written.append(data_sources.secret_text_path(root).name)
    _write_document(path, document)
    written.append(path.name)

    loaded = [item for item in load_restore_configs(path)
              if str(getattr(item, "restore_id", "")) == restore_id]
    return {
        "restore_id": restore_id,
        "databases": len(payload.get("databases") or []),
        "secrets_stored": [ref for ref, _value in stored],
        "loaded": bool(loaded),
        "replaced": bool(existing),
        "files_written": written,
        "next": [
            "db-ops backup_restore list-restores",
            f"db-ops backup_restore restore-latest --restore-id {restore_id} --dry-run",
        ],
    }
