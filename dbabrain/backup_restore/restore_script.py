"""Script-driven restore for engines the SQL Server restore engine does not cover.

``restore_database.py`` is built around SQL Server: an SMB backup share, ``.bak``/``.trn`` files
copied to a VM, and ``sqlcmd RESTORE DATABASE``. Oracle and PostgreSQL restores share none of
that - their backups already sit on the container host that will restore them, so there is no
share to mount and no ``.bak`` to copy across the network.

So they are driven the same way their backups are: a shell script asset shipped over SSH and run
against the host, with the scheduling and run recording coming from :mod:`schedule` - the same
rules backup jobs use. Nothing here touches the SQL Server path.

The script runs the same logical phases as the SQL Server workflow, so an operator reads one
sequence whatever the engine is:

    restore-key   -> make the decryption material available on the target (wallet/keystore)
    copy-backup   -> put the backup pieces where the target instance can read them
    restore-data  -> restore/duplicate the database and open it

Each phase prints ``PHASE=<name> ...`` so a failure names the step it failed in.

A SQL Server entry that opts into ``server_metadata`` gets two more, printed the same way but
run from here rather than by the script - they are TDS connections, not shell steps:
``metadata-pre-database`` before ``restore-data`` and ``metadata-post-database`` after. Neither
can fail the restore; see :func:`replay_metadata_phase`.
"""

from __future__ import annotations

import json
import posixpath
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable
from typing import Any

from dbabrain.lib import errors, stated_facts
from dbabrain.backup_restore.events import announce
from dbabrain.backup_restore import schedule
from dbabrain.backup_restore.backup import (
    TOOL_ROOT,
    BackupTarget,
    _script_path,
    execute_over_ssh,
    resolve_ssh_target,
)
from dbabrain.backup_restore.config import (
    DEFAULT_RESTORE_CONFIG_PATH,
    is_script_restore,
    parse_backup_restore_notify,
)
from dbabrain.backup_restore.server_metadata import (
    ServerMetadataPlan,
    parse_server_metadata,
    replay_phase,
)
from dbabrain.backup_restore.config import (
    DEFAULT_CLEANUP_RETENTION,
    _parse_copy_selection,
    parse_cleanup_retention,
)
from dbabrain.transport import common_cli
from dbabrain.lib import data_sources, instance_bundle, restore_space
from dbabrain.lib import integrity as restore_integrity
from dbabrain.lib import offload as offload_rules
from dbabrain.lib.shell import docker_cli
from dbabrain.lib.notify import NotifyConfig
from dbabrain.lib.restore.copy_mode import parse_copy_mode
from dbabrain.lib.time_window import TimeWindow, parse_time_window_config


@dataclass(frozen=True)
class ScriptRestore:
    restore_id: str
    db_type: str
    server_id: str
    backup_dir: str
    script: str
    time_window: TimeWindow
    target_container: str = ""
    # A target on another machine: a db_instances server_id whose cmd_access says how to reach it.
    # Empty means the target lives on the source's own host (the in-place drill).
    target_server_id: str = ""
    # Where the backup is placed on the target host before restoring. Only used for a remote
    # target, where the pieces have to be transferred rather than shared through a mount.
    target_backup_dir: str = ""
    # The backup as seen on the SOURCE HOST's own filesystem. ``backup_dir`` is the path inside
    # the database container, which SFTP cannot reach - a transfer needs the host path, and the
    # two are only the same when the backup lives on a bind mount.
    source_backup_host_dir: str = ""
    # The staged directory AS THE DATABASE SEES IT. Not the same string as target_backup_dir when
    # the target is a container: the files are staged on the host and reached through a mount, and
    # the database resolves the path in its own namespace. Empty means the two are the same.
    target_visible_dir: str = ""
    # How long staged files are kept on the target host (seconds; 0 = never delete). See
    # dbabrain.backup_restore.config.DEFAULT_CLEANUP_RETENTION.
    cleanup_retention: int = DEFAULT_CLEANUP_RETENTION
    active: bool = True
    # Opt-in: also replay the source instance's server-level metadata around this restore.
    # Absent means the restore behaves exactly as it did before this existed.
    server_metadata: ServerMetadataPlan = field(default_factory=ServerMetadataPlan)
    env: dict[str, str] = field(default_factory=dict)
    # Env vars whose value is a secret-store ref, resolved at run time: {ENV_NAME: SECRET_REF}.
    # A restore of an encrypted backup needs the passphrase, and it must not live in a
    # committed config file. Same mechanism as a backup job's env_secrets.
    env_secrets: dict[str, str] = field(default_factory=dict)
    # Per-entry notify object (dbabrain.lib.notify): logging_on_run + alert_on_error.
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    # SQL Server: a full restore over a database ONLINE on the target is refused unless stated
    # (owner decision G2.10). See config.BackupRestoreConfig.overwrite_existing.
    overwrite_existing: bool = False
    # How the backup crosses to a target on another machine: `auto` (one tar stream, file by file
    # when tar cannot be used - and the answer says which), or `tar` / `sftp` pinned, where the
    # other way is never tried (owner decision G4). See dbabrain.lib.restore.copy_mode.
    copy_mode: str = "auto"
    # The room the copy to another machine must find before it starts - the entry's `space_check`,
    # absent meaning on at x2. The tool checks it before the script runs; the script checks nothing
    # (the operator, 2026-10-02). Until then the field was read on a share-driven entry only.
    space_check: restore_space.SpaceCheck = field(default_factory=restore_space.SpaceCheck)
    # How each staged file is proven before it is used (0.34.0) - see config.BackupRestoreConfig.
    integrity: restore_integrity.RestoreIntegrity | None = None
    # `restore` or `copy_only` (0.34.0, 1.216) - see config.BackupRestoreConfig.action.
    action: str | None = None
    # `server` or `repository` (0.34.0) - see config.BackupRestoreConfig.restore_from.
    restore_from: str | None = None
    # What the copy to another machine takes (config.COPY_SELECTIONS): `chain` - the backups the
    # restore applies - or `window` - the chain and every file of `copy_recent_hours`. Read on a
    # script-driven entry since 2026-10-03; until then it was accepted and ignored here, so an
    # entry asking for every restore point of its window got the chain alone.
    copy_selection: str = "chain"
    copy_recent_hours: int = 24
    # The name of the file that marks `target_backup_dir` as this tool's staging folder; empty
    # means the copy's own default. An entry that staged before 0.29.0 states the name its folder
    # already carries (`upgrade-config` wrote it in): the default took the tool's new name, and a
    # folder retention has emptied down to its marker would have been refused under it.
    staging_marker: str = ""

    @property
    def is_remote(self) -> bool:
        return bool(self.target_server_id) and self.target_server_id != self.server_id

    @property
    def target_mode(self) -> str:
        """``docker`` when the target is a container, ``native`` when it is the host itself."""
        return "docker" if self.target_container else "native"

    @property
    def job_code(self) -> str:
        return schedule.restore_job_code(self.restore_id)

    @property
    def label(self) -> str:
        return f"{self.restore_id} ({self.db_type})"


class ScriptRestores(list):
    """The script-driven entries that can run - and, apart, the inactive ones that cannot be read.

    ``unusable`` is ``{restore_id: why}``. Since 0.26.0 an entry states everything about its target
    or it is refused (owner decision G2), and the refusal was for the whole file: one retired drill
    that still named a two-level staging folder, or no ``target_server_id``, stopped every
    script-driven restore on the node - the scheduled pass, ``list-restores``, and registering a new
    entry through the bot - the "one item stops the pass" shape the same review removed everywhere
    else. An **active** entry is still refused outright: it is about to run. An **inactive** one
    runs nothing, so it is kept out of the list with its reason, shown by ``list-restores``, and
    asking for it by id answers with that reason instead of "no such entry".
    """

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self.unusable: dict[str, str] = {}


def unusable_reason(jobs: Any, restore_id: str) -> str:
    """Why ``restore_id`` was kept out of ``jobs``, or "" - ``jobs`` being any loader's answer."""
    return str(getattr(jobs, "unusable", {}).get(restore_id, ""))


def _read_entries(path: Path | None) -> list[Any] | None:
    if path is None or not path.exists():
        return None
    with path.open("r", encoding="utf-8-sig") as file:
        raw = json.load(file)
    section = raw.get("backup_restore") if isinstance(raw, dict) else None
    if not isinstance(section, dict):
        return None
    entries = section.get("restores")
    return entries if isinstance(entries, list) else None


def load_script_restores(config_path: str | Path | None = None) -> ScriptRestores:
    """The script-driven subset of ``backup_restore.restores[]``.

    Falls back to the canonical restore config for the same reason the backup loader does: the
    scheduled command runs with ``--config config.json``, which holds app settings and no restore
    entries, so without the fallback the daemon would silently restore nothing.

    The answer is a :class:`ScriptRestores`: an inactive entry that cannot be read is not in it,
    and is named in its ``unusable`` with the reason. An active one that cannot be read raises.
    """
    path = Path(config_path) if config_path else None
    entries = _read_entries(path)
    source = path
    if entries is None and (path is None or path.resolve() != DEFAULT_RESTORE_CONFIG_PATH.resolve()):
        entries = _read_entries(DEFAULT_RESTORE_CONFIG_PATH)
        source = DEFAULT_RESTORE_CONFIG_PATH
    if entries is None:
        return ScriptRestores()

    jobs = ScriptRestores()
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or not is_script_restore(entry):
            continue
        restore_id = str(entry.get("restore_id") or "").strip()
        if not restore_id:
            raise ValueError(f"backup_restore.restores[{index}] requires restore_id.")
        if restore_id in seen:
            raise ValueError(f"Duplicate restore_id: {restore_id}.")
        seen.add(restore_id)
        try:
            # The facts it must state (rules R50) - the login, for a SQL Server restore.
            unstated = stated_facts.restore_gaps({}, entry)
            if unstated:
                raise errors.ConfigError(stated_facts.sentence(unstated))
            jobs.append(_script_restore(index, entry, restore_id))
        except ValueError as exc:
            if bool(entry.get("active", True)):
                raise
            # An inactive entry runs nothing, and is how an estate retires a drill. Refusing the
            # whole file over one stopped every restore on the node - the active ones too - for an
            # entry nobody runs (see ScriptRestores). It is kept out, with its reason.
            jobs.unusable[restore_id] = str(exc)
    jobs.unusable.update(_check_staging_dirs(
        jobs, machines=_target_machines(source.parent if source else None)))
    jobs[:] = [job for job in jobs if job.restore_id not in jobs.unusable]
    return jobs


def _target_machines(data_dir: Path | None) -> dict[str, str]:
    """``server_id`` -> the machine that instance is on, from the inventory beside the restore file.

    A staging directory is a path on a *machine*, and one machine carries several instances: three
    lab restores onto the SQL Server, the PostgreSQL and the Oracle of one VM shared one
    `target_backup_dir`, each a different `target_server_id`, so the check below saw three targets
    and passed. Each copy then removed the other two's files as "gone at the source", and the
    PostgreSQL restore lost its base backup in the middle of `pg_combinebackup` 21 times in 24
    (the 0.26.0 soak, 2026-10-01).

    Empty when there is no inventory to read; an instance it does not list stands for itself, so
    the check is then the one it was - by instance.
    """
    try:
        records = data_sources.load_db_instances(data_dir)
    except Exception:  # noqa: BLE001 - an unreadable inventory must not stop the restores loading.
        return {}
    machines: dict[str, str] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        access = record.get("cmd_access") if isinstance(record.get("cmd_access"), dict) else {}
        address = str(access.get("host") or record.get("ip") or "").strip().lower()
        server_id = str(record.get("server_id") or "").strip()
        if server_id and address:
            machines[server_id] = address
    return machines


def _script_restore(index: int, entry: dict[str, Any], restore_id: str) -> ScriptRestore:
    """One entry, read strictly: everything about its target stated, or a ``ValueError`` naming
    what is missing."""
    for required in ("server_id", "backup_dir", "script"):
        if not str(entry.get(required) or "").strip():
            raise ValueError(f"{restore_id} requires {required}.")
    # Nothing about the target is derived from the source (owner decision 2026-10-01, rules
    # R47): an entry that left `target_server_id` out used to mean "the source host", and on
    # SQL Server the restore then connected to the source's own port - production restored
    # over itself with REPLACE (review 0.25.0, B4.5).
    db_type = str(entry.get("db_type") or "").strip().lower()
    if not db_type:
        raise ValueError(f"{restore_id} requires db_type.")
    target_container = str(entry.get("target_container") or "").strip()
    target_server_id = str(entry.get("target_server_id") or "").strip()
    if not target_server_id:
        raise ValueError(
            f"{restore_id} requires target_server_id: the instance the backup is restored "
            f"onto. Name it even when it is on the source's machine - the source is never "
            f"assumed to be the target."
        )
    target_backup_dir = str(entry.get("target_backup_dir") or "").strip()
    target_visible_dir = str(entry.get("target_visible_dir") or "").strip()
    source_backup_host_dir = str(entry.get("source_backup_host_dir") or "").strip()
    env = entry.get("env") or {}
    if not isinstance(env, dict):
        raise ValueError(f"{restore_id}.env must be an object.")
    if target_server_id == str(entry["server_id"]).strip():
        # Same machine as the source: the two instances differ only by container (and, for
        # SQL Server, by port), so both are stated, and so is where the target reads the set.
        if not target_container:
            raise ValueError(
                f"{restore_id} restores onto its own source machine ({target_server_id}), so it "
                f"requires target_container: the container the restore goes INTO."
            )
        if not target_visible_dir:
            raise ValueError(
                f"{restore_id} restores onto its own source machine, so it requires "
                f"target_visible_dir: the backup directory as the target container sees it."
            )
        if db_type == "sqlserver" and not str(env.get("MSSQL_PORT") or "").strip():
            raise ValueError(
                f"{restore_id} restores SQL Server onto its own source machine, so it requires "
                f"env.MSSQL_PORT: the target container's port. Without it the restore would "
                f"connect to the source instance."
            )
    if target_server_id and target_server_id != str(entry["server_id"]).strip():
        if not target_backup_dir:
            raise ValueError(
                f"{restore_id} restores onto {target_server_id}, so it requires target_backup_dir: "
                f"the backup has to be transferred there, not shared through a mount."
            )
        if not source_backup_host_dir:
            raise ValueError(
                f"{restore_id} restores onto another machine, so it requires "
                f"source_backup_host_dir: backup_dir is a path inside the database container "
                f"and the transfer reads the source host's filesystem."
            )
    env_secrets = entry.get("env_secrets") or {}
    if not isinstance(env_secrets, dict):
        raise ValueError(f"{restore_id}.env_secrets must be an object of {{ENV_NAME: secret_ref}}.")
    try:
        copy_mode = parse_copy_mode(entry.get("copy_mode"))
        space_check = restore_space.parse_space_check(entry)
        integrity = restore_integrity.parse_restore_integrity(
            entry, label=f"backup_restore.restores[{restore_id}]")
        action = restore_integrity.parse_restore_action(entry, label=f"backup_restore.restores[{restore_id}]")
        restore_from = offload_rules.parse_restore_from(entry, label=f"backup_restore.restores[{restore_id}]")
        copy_selection = _parse_copy_selection(entry.get("copy_selection"))
        # 0 is a value - a window with no limit - not "absent": `or 24` read it as a day.
        hours = entry.get("copy_recent_hours")
        copy_recent_hours = 24 if hours is None or str(hours).strip() == "" else int(hours)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{restore_id}: {exc}") from None
    if space_check.measure_restore:
        # Refused, not ignored: an entry that says its restore is measured, and is not, is a
        # statement nothing keeps. Only the share-driven SQL Server restore can be asked today.
        raise ValueError(f"{restore_id}: {restore_space.MEASURE_RESTORE_UNSUPPORTED}")
    return ScriptRestore(
        restore_id=restore_id,
        db_type=db_type,
        server_id=str(entry["server_id"]).strip(),
        target_container=target_container,
        target_server_id=target_server_id,
        target_backup_dir=target_backup_dir,
        target_visible_dir=target_visible_dir,
        source_backup_host_dir=source_backup_host_dir,
        cleanup_retention=parse_cleanup_retention(
            entry, context=f"backup_restore.restores[{index}]"
        ),
        backup_dir=str(entry["backup_dir"]).strip(),
        script=str(entry["script"]).strip(),
        time_window=parse_time_window_config(
            entry, context=f"backup_restore.restores[{index}]"
        ).time_window,
        env_secrets={str(k): str(v) for k, v in env_secrets.items()},
        notify=parse_backup_restore_notify(
            entry, context=f"backup_restore.restores[{index}] ({restore_id})"
        ),
        active=bool(entry.get("active", True)),
        overwrite_existing=entry.get("overwrite_existing") is True,
        copy_mode=copy_mode,
        space_check=space_check,
        integrity=integrity,
        action=action,
        restore_from=restore_from,
        copy_selection=copy_selection,
        copy_recent_hours=copy_recent_hours,
        staging_marker=str(entry.get("staging_marker") or "").strip(),
        env={str(k): str(v) for k, v in env.items()},
        server_metadata=parse_server_metadata(
            entry.get("server_metadata"),
            label=f"backup_restore.restores[{index}] ({restore_id})",
            for_restore=True,
            db_type=str(entry.get("db_type") or ""),
        ),
    )


def _check_staging_dirs(jobs: list[ScriptRestore], *,
                        machines: dict[str, str] | None = None) -> dict[str, str]:
    """Each cross-machine entry stages into a directory of its own (review 0.25.0, B4.7).

    The copy is a mirror and the prune deletes by age, so two entries sharing - or nesting - one
    `target_backup_dir` on a machine delete each other's files, and a shallow directory (`/`,
    `/data`) takes whatever else lives there. The docs asked operators for this; now it is checked.

    "On a machine", not "on a target": ``machines`` (:func:`_target_machines`) says which machine
    each target instance is on, so two entries restoring onto two instances of one host are held to
    the rule as well.

    An active entry that breaks the rule raises. An inactive one is returned as
    ``{restore_id: why}`` instead, and where an active entry and an inactive one share a directory
    it is the inactive one that yields (:class:`ScriptRestores`).
    """
    where = machines or {}

    def machine(item: ScriptRestore) -> str:
        return where.get(item.target_server_id) or item.target_server_id

    unusable: dict[str, str] = {}
    kept: list[tuple[ScriptRestore, str]] = []
    for job in jobs:
        if not job.is_remote:
            continue
        directory = posixpath.normpath(job.target_backup_dir)
        problem = ""
        if len([part for part in directory.split("/") if part]) < 3:
            problem = (
                f"{job.restore_id}: target_backup_dir {job.target_backup_dir!r} is too shallow - the "
                f"staging copy deletes what it does not recognise there. Use a directory of its own, "
                f"at least three levels deep (e.g. /opt/db_ops/restore_staging/{job.restore_id}).")
        while not problem:
            clash = next(((other, held) for other, held in kept
                          if machine(other) == machine(job)
                          and (directory == held or directory.startswith(held + "/")
                               or held.startswith(directory + "/"))), None)
            if clash is None:
                break
            other, held = clash
            place = (job.target_server_id if other.target_server_id == job.target_server_id
                     else f"{machine(job)}, the machine both {other.target_server_id} and "
                          f"{job.target_server_id} are on")
            shared = (
                f"{job.restore_id} and {other.restore_id} stage into {directory} and {held} on "
                f"{place}: one directory holds the other, so each run would delete the "
                f"other's files. Give every restore its own target_backup_dir.")
            if job.active and not other.active:
                unusable[other.restore_id] = shared
                kept.remove(clash)
                continue
            problem = shared
        if problem:
            if job.active:
                raise ValueError(problem)
            unusable[job.restore_id] = problem
            continue
        kept.append((job, directory))
    return unusable


def select_due_script_restores(
    *, jobs: list[ScriptRestore], latest_runs: dict[str, Any], now=None, local_now=None
) -> list[ScriptRestore]:
    return [
        item for item in jobs
        if item.active and schedule.is_due(
            job_code=item.job_code, time_window=item.time_window,
            latest_runs=latest_runs, now=now, local_now=local_now,
        )
    ]


def _restore_secrets(job: ScriptRestore, *, data_dir, key, key_base64) -> dict[str, str]:
    """The decrypted store, read only when the entry actually declares env_secrets - so a
    restore with nothing to decrypt still runs on a node without the passphrase."""
    if not job.env_secrets:
        return {}
    from dbabrain.backup_restore.backup import _load_secrets
    return _load_secrets(data_dir=data_dir, key=key, key_base64=key_base64)


def _script_env(job: ScriptRestore, source: BackupTarget,
                *, secrets: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        # The container the backup was taken from, and the one it is being restored into.
        # They are deliberately separate: a restore drill must never write over the source.
        # Empty when the script runs on another machine: the source container does not exist
        # there, so checking for it would fail on a perfectly good restore.
        "SOURCE_CONTAINER": "" if job.is_remote else source.container_name,
        "TARGET_CONTAINER": job.target_container,
        # On a remote target the pieces were transferred here; in place they are the same path.
        "BACKUP_DIR": job.target_backup_dir if job.is_remote else job.backup_dir,
        "RESTORE_ID": job.restore_id,
        # docker: exec into TARGET_CONTAINER. native: run on the host itself (a VM with the
        # engine installed directly), where there is no container to exec into.
        "TARGET_MODE": job.target_mode,
    }
    env.update(job.env)
    # Secrets last: the decryption passphrase reaches the script as an env var, exported ahead
    # of it on the remote side, so it never appears on a command line or in the config file.
    for name, ref in (job.env_secrets or {}).items():
        value = (secrets or {}).get(ref)
        if not value:
            raise ValueError(
                f"{job.restore_id}: env_secrets maps {name} to secret ref '{ref}', which is not "
                f"in the secret store. Add it, or pass --key/--key-base64 so the store can be read."
            )
        env[name] = str(value)
    return env


def _ssh_login(target: BackupTarget, *, data_dir, key, key_base64) -> dict[str, Any]:
    """The login a ``common.cli`` copy command opens its session with: resolved here, handed there.

    ``common`` reads no configuration, so the secret ref and the key name stop at this line.
    """
    from dbabrain.lib.data_sources import resolve_ssh_key, resolve_ssh_password

    login: dict[str, Any] = {"host": target.host, "port": target.port, "username": target.username}
    key_filename = resolve_ssh_key(target.key_file, data_dir) if target.key_file else None
    if key_filename:
        login["key_file"] = str(key_filename)
    else:
        login["password"] = resolve_ssh_password(
            password=None, password_ref=target.password_ref,
            key=key, key_base64=key_base64, data_dir=data_dir,
        )
    return login


def transfer_backup_to_target(
    job: ScriptRestore,
    *,
    source: BackupTarget,
    target: BackupTarget,
    data_dir=None,
    key=None,
    key_base64=None,
    log=None,
    prune: bool = True,
    point_in_time: str = "",
    catalog: Any = None,
) -> dict[str, Any]:
    """Copy the backup from the source host to the target host before the restore runs.

    This is the copy-backup phase doing real work. In the in-place drill the same directory is
    visible to both ends through a mount and nothing moves; once the target is another machine
    the pieces genuinely have to travel, which is the whole difference between a drill and a
    recovery onto new hardware.

    Two ``common.cli`` commands since 0.23.0, each a step of its own: ``backup-chain`` says which
    parts of the source directory the restore needs (PostgreSQL's newest chain, RMAN's own answer
    for Oracle, everything for SQL Server and for a point in time), ``copy-backup-dir`` moves them
    as one tar stream and mirrors the source. The app resolves the two logins; ``common`` opens the
    sessions. Their progress streams on stderr as it happens (``log`` is kept for callers and no
    longer used).

    ``prune=False`` leaves the retention cleanup to the caller: ``restore_by_id`` runs it after the
    restored databases are verified (its DELETE step), as the engine path does.

    Every file goes through the checksum gate (0.34.0, 1.213b, :func:`_copy_gated_between_hosts`);
    a copy that is not proven raises, so no restore runs on it. ``catalog`` records the run (its id
    is answered as ``restore_run_id``, for the caller to close with the restore's outcome).
    """
    del log
    proofs: dict[str, dict[str, Any]] | None = None
    source_dir = job.source_backup_host_dir
    if getattr(job, "restore_from", None) == "repository":
        source, source_dir, proofs = _repository_source(job, catalog=catalog, data_dir=data_dir)
    source_login = _ssh_login(source, data_dir=data_dir, key=key, key_base64=key_base64)
    target_login = _ssh_login(target, data_dir=data_dir, key=key, key_base64=key_base64)
    window = job.copy_selection == "window"
    if window and job.copy_recent_hours <= 0:
        # A window with no limit is the whole directory; there is no chain to narrow it to.
        include: list[str] = []
    else:
        include = list(common_cli.run("backup-chain", {
            "db_type": job.db_type, "source": source_login,
            "source_dir": source_dir, "backup_dir": job.backup_dir,
            "container": source.container_name, "point_in_time": point_in_time,
        }, stream_stderr=True).get("include") or [])
    request = {
        "source": source_login, "source_dir": source_dir,
        "target": target_login, "target_dir": job.target_backup_dir,
        "include": include, "make_readable": True, "open_for_engine": True,
        "copy_mode": job.copy_mode,
        "space_check": restore_space.as_request(job.space_check),
        # The chain AND every file of the window: never fewer backups than `chain` copies.
        **({"window_hours": job.copy_recent_hours} if window and job.copy_recent_hours > 0 else {}),
        **_staging_marker(job),
    }
    out = _copy_gated_between_hosts(job, request, source=source, target=target, catalog=catalog, proofs=proofs)
    if proofs is not None:
        out["restore_from"] = "repository"
        out["repository_server_id"] = source.server_id
        out["repository_dir"] = source_dir
    out["include"] = include
    out["copy_selection"] = job.copy_selection
    pruned = (prune_staged_backups(job, target=target, data_dir=data_dir, key=key,
                                   key_base64=key_base64)
              if prune else {"pruned": 0, "retention_seconds": int(job.cleanup_retention or 0),
                             "skipped": "left to the caller"})
    out["pruned"] = pruned.get("pruned", 0)
    out["retention_seconds"] = pruned.get("retention_seconds", 0)
    return out


class CopyNotProvenError(errors.Refused):
    """A host-to-host copy the checksum gate could not prove; the restore is not run on it."""


#: The engines a restore can read from the repository: their chain is read from the folder's own
#: layout, which the repository keeps. Oracle's pieces are RMAN's to catalog (a later item).
REPOSITORY_ENGINES = ("postgresql", "sqlserver")


def _repository_source(job: Any, *, catalog: Any, data_dir: Any) -> tuple[BackupTarget, str, dict[str, Any]]:
    """``restore_from: repository`` (0.34.0): the repository host as the copy's source, the folder there
    that stands for the entry's ``source_backup_host_dir``, and every repository file's offload proof."""
    from dbabrain.backup_restore.backup import resolve_ssh_target
    from dbabrain.backup_restore.offload import repository_source

    engine = str(job.db_type or "").lower()
    if engine not in REPOSITORY_ENGINES:
        raise CopyNotProvenError(f"{job.label}: restore_from repository reads {', '.join(REPOSITORY_ENGINES)} "
                                 f"backups; {engine} is not built (RMAN catalogs its own pieces).")
    if catalog is None:
        raise CopyNotProvenError(f"{job.label}: restore_from repository reads the backup catalog - where each "
                                 "copy is and what it was proven as - and no catalog is open.")
    server_id, folder = repository_source(
        catalog.repository_locations(server_id=job.server_id, under=job.source_backup_host_dir),
        job.source_backup_host_dir)
    repository = resolve_ssh_target(server_id, label=f"{job.label} repository", data_dir=data_dir,
                                    require_container=False)
    proofs = catalog.repository_proofs(server_id=job.server_id, repository_server_id=server_id)
    return repository, folder, proofs


def _ssh_host_block(login: dict[str, Any]) -> dict[str, Any]:
    """``file-checksum``'s ``host`` for a machine reached over SSH - the login ``copy-backup-dir`` uses."""
    return {"runtime": "linux", "access": "ssh", **login}


def _script_file_type(db_type: str, relative: str) -> str:
    """A staged file's ``file_type`` from the layout the PostgreSQL and Oracle jobs write: ``base/`` and
    ``wal/`` for PostgreSQL, RMAN's piece names for Oracle (a best reading - the catalog's own type is
    used whenever the backup recorded the file); SQL Server's own layout."""
    path = relative.replace("\\", "/")
    name = posixpath.basename(path).upper()
    engine = str(db_type or "").lower()
    if engine == "postgresql":
        if path.startswith("wal/") or "/wal/" in path:
            return "WAL"
        if name == "BACKUP_MANIFEST":
            return "MANIFEST"
        return "INCR_BASE" if "_INCR" in path.upper() else "FULL_BASE"
    if engine == "oracle":
        if name.startswith("C-"):
            return "CONTROLFILE"      # RMAN's autobackup: c-<dbid>-<yyyymmdd>-<nn>
        for marker, kind in (("CONTROLFILE", "CONTROLFILE"), ("SPFILE", "SPFILE"), ("ARCH", "ARCHIVELOG"),
                             ("L1", "RMAN_L1"), ("INCR", "RMAN_L1")):
            if marker in name:
                return kind
        return "RMAN_L0"
    return restore_integrity.sqlserver_file_type(path)


def _copy_gated_between_hosts(job: ScriptRestore, request: dict[str, Any], *, source: BackupTarget,
                              target: BackupTarget, catalog: Any = None,
                              proofs: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """``copy-backup-dir`` held to the checksum gate (0.34.0, 1.213b) - the same rule as every other copy.

    1. ``plan_only``: the files the request selects, read from the source.
    2. :func:`copy_gate.copy_verified_batch` over them: the sources hashed on the source host, the
       targets on the target, in one ``file-checksum`` call each per round.
    3. Each round's copies are ONE ``copy-backup-dir`` - the request as asked, plus ``replace`` naming
       the files still to copy, so a same-size copy with other bytes is copied over. The copy is lazy:
       the gate asks for files one by one, and they travel together when it hashes the round.

    A run whose files are all on the target already still makes one copy call, for what the command
    does besides copying: the mirror (removing what the source no longer has), the marker, opening
    the folder for the engine. Anything not proven raises :class:`CopyNotProvenError`.
    """
    from dbabrain.backup_restore import copy_gate

    integrity = restore_integrity.require_restore_integrity(
        {"integrity": job.integrity.as_record()} if job.integrity else {}, label=f"restore_id={job.label}")
    source_dir = str(request.get("source_dir") or job.source_backup_host_dir).rstrip("/")
    target_dir = job.target_backup_dir.rstrip("/")
    if proofs is not None:
        # From the repository, only what an offload proved: anything else there - a copy cut short,
        # a stray file - is refused, never restored (the entry's own uncatalogued choice is for its
        # server, where every file was written by a backup).
        from dataclasses import replace as _replace

        integrity = _replace(integrity, uncatalogued="refuse")
    plan = common_cli.run("copy-backup-dir", {**request, "plan_only": True}, stream_stderr=True)
    selected = [(str(item["path"]), int(item.get("size_bytes") or 0)) for item in plan.get("files") or []]

    stored: dict[str, dict[str, Any]] = dict(proofs or {})
    if proofs is None and catalog is not None and selected:
        names = sorted({posixpath.basename(rel) for rel, _size in selected})
        for name, row in catalog.stored_files(server_id=job.server_id, file_names=names).items():
            # A name is held to its stored checksum only at the path the backup recorded it: a
            # rebuilt PostgreSQL cluster names its WAL from ...0001 again, and every base backup
            # has its own backup_manifest.
            # host_path, or the file's own path when it lies in no container's mount (the catalog's
            # column is file_path; "path" was a FILE line's word, and read None here - 1.213b).
            stored[restore_integrity.hashed_path(row.get("file_type") or "",
                                                 str(row.get("host_path") or row.get("file_path") or ""))] = row
    items = []
    for rel, _size in selected:
        source_path = f"{source_dir}/{rel}"
        row = stored.get(source_path) or {}
        items.append(copy_gate.FileToCopy(
            source_server_id=source.server_id if proofs is not None else job.server_id,
            target_server_id=job.target_server_id,
            # The type the backup recorded wins; the layout is read only for a file no backup recorded.
            file_name=posixpath.basename(rel), file_type=row.get("file_type") or _script_file_type(job.db_type, rel),
            source_path=source_path, target_path=f"{target_dir}/{rel}", engine=str(job.db_type or "").lower(),
            checksum_after_backup=row.get("checksum_after_backup"), size_after_backup=row.get("size_bytes"),
            file_id=row.get("file_id"), source_host_path=source_path, target_host_path=f"{target_dir}/{rel}",
            backup_taken_at=row.get("backup_ended_at")))
    relative_of = {item.target_path: rel for item, (rel, _size) in zip(items, selected)}

    restore_run_id = None
    if catalog is not None:
        from dbabrain.lib.version import __version__
        import socket

        restore_run_id = catalog.start_restore_run(
            action=restore_integrity.ACTION_COPY_ONLY if getattr(job, "action", None) == restore_integrity.ACTION_COPY_ONLY
            else restore_integrity.ACTION_RESTORE, restore_id=job.restore_id, source_server_id=job.server_id,
            target_server_id=job.target_server_id, target_folder=target_dir,
            copy_max_retry=integrity.copy_max_retry, phase="copying",
            node_host=socket.gethostname(), app_version=__version__)

    copies: list[dict[str, Any]] = []
    pending: list[str] = []

    def flush(replace: list[str]) -> None:
        answer = dict(common_cli.run("copy-backup-dir", {**request, "replace": replace}, stream_stderr=True))
        copies.append(answer)

    def hasher(host: dict[str, Any], *, copies_first: bool = False):
        def batch(paths: list[str]) -> dict[str, tuple[str, int | None]]:
            if pending and copies_first:
                # The round's copies travel now, together, before the round is hashed.
                round_files, pending[:] = list(pending), []
                try:
                    flush(round_files)
                except Exception as exc:  # noqa: BLE001 - a failed round is an attempt spent, per file
                    copies.append({"error": f"{type(exc).__name__}: {exc}"[:2000]})
            if not paths:
                return {}
            answer = common_cli.run("file-checksum", {"host": host, "paths": list(paths)})
            return {f["path"]: (f["sha256"], f.get("size_bytes")) for f in answer.get("files") or []}
        return batch

    source_host = _ssh_host_block(request["source"])
    target_host = _ssh_host_block(request["target"])

    def copy(item: Any) -> int:
        pending.append(relative_of[item.target_path])
        return 0

    try:
        outcomes = copy_gate.copy_verified_batch(
            items, integrity=integrity, hash_sources=hasher(source_host), hash_targets=hasher(target_host, copies_first=True),
            copy=copy, catalog=catalog, restore_run_id=restore_run_id, copy_engine="copy-backup-dir")
        if not copies:
            # Nothing needed copying: the mirror, the marker and the engine's read access are still
            # this run's, and a failure here is the run's own error. (A round that failed is already
            # an attempt spent, and its file says so.)
            flush([])
    except Exception as exc:
        if catalog is not None and restore_run_id is not None:
            catalog.set_restore_run(restore_run_id, phase="error", error_text=f"{type(exc).__name__}: {exc}"[:2000])
        raise
    summary = copy_gate.summarize(outcomes)
    if catalog is not None and restore_run_id is not None:
        catalog.set_restore_run(
            restore_run_id, phase="verified" if summary["proven"] else "refused",
            chain_files=summary["files"], files_matched=summary["verdicts"].get("match", 0),
            files_recopied=summary["recopied"], refused_reason="; ".join(summary["unproven"])[:2000])
    if not summary["proven"]:
        if catalog is not None and restore_run_id is not None:
            from dbabrain.db.store import utc_now_text

            # Ended here: no restore follows a refused copy, so nobody else closes it.
            catalog.set_restore_run(restore_run_id, ended_at=utc_now_text())
        raise CopyNotProvenError(
            f"{job.label}: the copy {job.server_id} -> {job.target_server_id} is not proven, so it is not "
            f"restored - {'; '.join(summary['unproven'][:5])}")

    done = [answer for answer in copies if "error" not in answer]
    out = dict(done[0]) if done else {}
    out["copied"] = sum(int(answer.get("copied") or 0) for answer in done)
    out["bytes_copied"] = sum(int(answer.get("bytes_copied") or 0) for answer in done)
    out["skipped"] = int(done[0].get("skipped") or 0) if done else 0
    out.pop("files", None)
    out["integrity"] = {**summary, "restore_run_id": restore_run_id}
    out["restore_run_id"] = restore_run_id
    return out


def _staging_marker(job: Any) -> dict[str, str]:
    """The marker an entry states, as the request field; nothing when it states none, which leaves
    the copy its own default. One place, so the copy and the prune cannot be handed two names."""
    marker = str(job.staging_marker or "").strip()
    return {"staging_marker": marker} if marker else {}


def prune_staged_backups(
    job: ScriptRestore,
    *,
    target: BackupTarget,
    data_dir=None,
    key=None,
    key_base64=None,
    log=None,
) -> dict[str, Any]:
    """The DELETE step of a remote restore: ``common.cli prune-staged-backups`` on its staging.

    After the verify, not inside the copy: a drill that failed keeps what it staged for the next
    attempt - the rule the engine path has followed since 2026-09-11 ("the retention cleanup must
    not prune the backups when the drill that proves them restorable has just failed").
    """
    del log
    return common_cli.run("prune-staged-backups", {
        "target": _ssh_login(target, data_dir=data_dir, key=key, key_base64=key_base64),
        "target_dir": job.target_backup_dir,
        "cleanup_retention": int(job.cleanup_retention or 0),
        # The same marker the copy looked for: a prune that asks for another refuses the folder.
        **_staging_marker(job),
    }, stream_stderr=True)


# `_announce` is `dbabrain.backup_restore.events.announce` since 2026-08-16 — it was four
# near-copies in this app, one of which had already drifted its parameter names.


def replay_metadata_phase(
    job: ScriptRestore,
    *,
    phase: str,
    data_dir: str | Path | None = None,
    on_phase: Callable[..., None] | None = None,
) -> tuple[str, dict[str, Any] | None]:
    """Adapter onto the shared :func:`server_metadata.replay_phase`.

    The decision is shared with the engine path and lives there; this only says how a
    ``ScriptRestore`` answers its questions - a container-to-container restore names a container,
    where the engine path names an ip.
    """
    return replay_phase(
        job.server_metadata,
        phase=phase,
        label=job.label,
        source_server_id=job.server_id,
        target_server_id=job.target_server_id,
        target_container=job.target_container,
        data_dir=data_dir,
        announce=on_phase,
    )


def run_script_restore(
    job: ScriptRestore,
    *,
    data_dir: str | Path | None = None,
    key: str | None = None,
    key_base64: str | None = None,
    on_phase: Callable[..., None] | None = None,
) -> tuple[str, int | None, str, str]:
    """Run the restore. Returns (status, exit_code, out, err).

    In place, the script runs on the source's own host and execs into the target container.
    Onto another machine, the backup is transferred first and the script runs on the *target*
    host - so the engine commands, the data directory and the restored instance are all local
    to where the database will actually live.

    ``on_phase(phase, message, extra)`` is called at the copy boundaries when there is a copy to
    report, so a caller can announce them. It is optional and never affects the restore: a
    caller that does not pass it (or an emit that fails) must not turn a good restore into a
    failed one, which is why the copy runs outside the notification, not through it.
    """
    source = resolve_ssh_target(job.server_id, label=job.label, data_dir=data_dir)

    if job.is_remote:
        target = resolve_ssh_target(
            job.target_server_id, label=job.label, data_dir=data_dir, require_container=False,
        )
        if target.host == source.host and job.target_container == source.container_name:
            raise ValueError(
                f"{job.label}: the target is the source container ({source.container_name}). "
                f"A restore must not overwrite the database it is restoring from."
            )
        # Only a remote drill copies anything. An in-place one shares the directory through a
        # mount, so announcing a copy there would report work that never happened.
        announce(on_phase, "COPY_START",
                  f"Restore {job.label}: copy started {job.server_id} -> {job.target_server_id}.")
        transfer = transfer_backup_to_target(
            job, source=source, target=target,
            data_dir=data_dir, key=key, key_base64=key_base64,
        )
        announce(on_phase, "COPY_DONE",
                  f"Restore {job.label}: copy finished - {transfer['copied']} piece(s), "
                  f"{transfer['bytes_copied']} bytes, {transfer['skipped']} already present.",
                  {"copied": transfer["copied"], "skipped": transfer["skipped"],
                   "bytes_copied": transfer["bytes_copied"],
                   "pruned": transfer.get("pruned", 0)})
        prefix = (
            f"PHASE=copy-backup transferred {job.server_id} -> {job.target_server_id} "
            f"copied={transfer['copied']} skipped={transfer['skipped']} "
            f"bytes={transfer['bytes_copied']} pruned={transfer.get('pruned', 0)} "
            f"retention={transfer.get('retention_seconds', 0)}s\n"
        )
        run_on = target
    else:
        if not job.target_container:
            raise ValueError(
                f"{job.label}: an in-place restore needs target_container."
            )
        if job.target_container == source.container_name:
            raise ValueError(
                f"{job.label}: target_container is the source container ({source.container_name}). "
                f"A restore drill must not overwrite the database it is validating."
            )
        prefix = ""
        run_on = source

    # Before the databases are restored, or every restored database's users are orphaned: their
    # SIDs point at logins that do not exist on the target yet.
    pre_line, _pre = replay_metadata_phase(
        job, phase=instance_bundle.PRE_DATABASE, data_dir=data_dir, on_phase=on_phase,
    )

    script_text = _script_path(job.script, label=job.label).read_text(encoding="utf-8")
    out, err, exit_code = execute_over_ssh(
        script_text=script_text, env=_script_env(job, source, secrets=_restore_secrets(
            job, data_dir=data_dir, key=key, key_base64=key_base64)), target=run_on,
        timeout=job.time_window.timeout, data_dir=data_dir, key=key, key_base64=key_base64,
    )
    out = prefix + pre_line + out
    completed = "RESULT=ok" in out
    status = "done" if exit_code == 0 and completed else "error"

    # Only after a restore that worked. Agent job steps name databases that have to exist, so
    # replaying them onto a failed restore would create jobs pointing at databases that are not
    # there - and would do it while the operator is reading a failure message about something
    # else entirely.
    if status == "done":
        post_line, _post = replay_metadata_phase(
            job, phase=instance_bundle.POST_DATABASE, data_dir=data_dir, on_phase=on_phase,
        )
        out += post_line
    return status, exit_code, out, err
