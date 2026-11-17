"""Restoring a configured entry by ``restore_id``, through the common primitives.

This is the seam the whole layering rests on. ``dbabrain/common`` reads no config and knows no
``server_id``; it takes a complete request and performs it. Something has to turn "restore
CLOUD_MSSQL_TO_CLOUD2" into that request, and that something is an app - it is the half that
knows where ``restore_config.json`` lives, what a ``server_id`` means, and how to decrypt a
password. So the lookup is here and the work is there.

It replaces the shell scripts for the *restore* itself. What it deliberately does not replace is
the **transfer**: ``transfer_backup_to_target`` already moves a backup set between hosts, has been
doing it nightly for months, and re-deriving it would repeat exactly the mistakes this module was
written after - the ones where a primitive turned out to know less than the script it was meant to
generalise.

Each engine gets the sequence that was actually proven against it on 2026-08-07, not a sequence
that looks symmetrical:

* **sqlserver** - import the backup certificate if the set carries one, restore the newest full
  ``NORECOVERY``, the newest differential after it, then every log after that with ``RECOVERY``
  (and ``STOPAT`` when a moment was asked for), then verify.
* **oracle** - one ``DUPLICATE``. RMAN picks the level 0, the incrementals and the archived logs
  itself, so there is no diff or log step to run; asking for one would describe work it did not do.
* **postgresql** - combine the full and every incremental after it in one ``pg_combinebackup``,
  write the recovery configuration, then verify. The incrementals are not applied one at a time.
"""

from __future__ import annotations

from dataclasses import dataclass

from dbabrain.lib import errors
from dbabrain.lib import consistency_check
import posixpath
import re
import sys
import time

import subprocess

import json
import hashlib
import tempfile
from pathlib import Path

from typing import Any

from dbabrain.transport import common_cli
from dbabrain.lib import integrity as restore_integrity
from dbabrain.lib import instance_bundle
from dbabrain.lib import response
from dbabrain.lib.restore import landing as restore_landing
from dbabrain.lib.restore import stated as restore_stated
from dbabrain.lib.restore import windows as windows_target
from dbabrain.lib import sqlserver_certificate


class RestoreByIdError(errors.RequestError):
    """The entry cannot be restored as configured."""


def _secret(ref: str, secrets: dict[str, str], *, where: str) -> str:
    if not ref:
        return ""
    value = secrets.get(ref, "")
    if not value:
        raise RestoreByIdError(
            f"{where} maps to secret ref {ref!r}, which is not in the secret store. Add it, or "
            "pass --key/--key-base64 so the store can be read."
        )
    return str(value)


def _host_block(job: Any, *, data_dir: Any, load_secrets: Any = None) -> dict[str, Any]:
    """Where the target database runs, in the shape :mod:`dbabrain.common.hostcmd` expects.

    Derived from the entry rather than restated: ``target_server_id`` names the host and
    ``target_container`` the container on it, which is the same chain ``server_metadata`` follows.

    A target that logs in with a password gets it here - ``load_secrets()`` is called only then.
    The block used to carry a key file or nothing, so every PostgreSQL and Oracle restore onto a
    password-login machine failed at planning, "open_ssh_client needs either a password or a
    key_filename" (the lab drill, 2026-09-24); the key-login cloud hosts never showed it.

    **A Windows target** (0.39.0): the target's inventory record says ``platform: windows`` and the
    entry names no container - SQL Server running natively there, reached over Windows OpenSSH. The
    block's runtime is then ``windows`` (PowerShell, never ``sudo``), read from the record and never
    guessed from a path. What that shape cannot do is refused here, before anything is copied:
    another engine, a container, a WinRM-only host, a staging that is not a drive path
    (:func:`dbabrain.lib.restore.windows.refusal`) - and the other way round, a drive path on a target
    whose record does not say windows.
    """
    from dbabrain.backup_restore.backup import resolve_ssh_target

    target_server_id = str(getattr(job, "target_server_id", "") or "").strip()
    if not target_server_id:
        raise RestoreByIdError(f"{job.restore_id} names no target_server_id; the source is never "
                               "assumed to be the target.")
    target = resolve_ssh_target(
        target_server_id, label=job.label,
        data_dir=data_dir, require_container=False,
    )
    windows = str(getattr(target, "platform", "") or "").strip().lower() == windows_target.PLATFORM
    staging = {"target_dir": getattr(job, "target_backup_dir", ""), "visible_dir": getattr(job, "target_visible_dir", "")}
    why = (windows_target.refusal(db_type=getattr(job, "db_type", ""), access=getattr(target, "access", "ssh"),
                                  container=getattr(job, "target_container", ""), **staging)
           if windows else windows_target.linux_refusal(**staging))
    if why:
        raise RestoreByIdError(f"{job.restore_id} cannot restore onto {target_server_id}: {why} Nothing was copied.")
    if windows:
        # No sudo: there is none on Windows, and the engine is the host's own service, not a container.
        block: dict[str, Any] = {"runtime": windows_target.PLATFORM, "access": "ssh",
                                 "host": target.host, "port": target.port, "username": target.username}
    else:
        block = {
            "runtime": "docker" if job.target_container else "linux",
            "host": target.host, "port": target.port, "username": target.username,
            # Reaching docker on these hosts needs it, and guessing is how a restore fails with
            # "permission denied while trying to connect to the Docker daemon socket".
            "sudo": True,
        }
    if target.key_file:
        from dbabrain.lib.data_sources import resolve_ssh_key

        block["key_file"] = str(resolve_ssh_key(target.key_file, data_dir))
    elif target.password_ref and load_secrets is not None:
        from dbabrain.backup_restore.spec_builder import _ssh_password

        block["password"] = _ssh_password(target, load_secrets())
    if job.target_container:
        block["container"] = job.target_container
    return block


def _engine_dir(directory: str, host: dict[str, Any]) -> str:
    """The staged directory in the form the ENGINE names files: on Windows, with backslashes.

    SQL Server lists the folder (``sys.dm_os_enumerate_filesystem``) and reads every backup by this
    path, and on Windows it names its own files ``C:\\...``; the entry states the staging with forward
    slashes, the form every hop to the host takes. Elsewhere unchanged.
    """
    if host.get("runtime") == windows_target.PLATFORM:
        return windows_target.sql_server_path(directory, field="target_visible_dir")
    return directory


def _visible_dir(job: Any) -> str:
    """The staged backup directory **as the database sees it**.

    Not the same string as ``target_backup_dir`` when the target is a container: the files are
    staged on the host and reached through a mount, and the database resolves the path in its own
    namespace. Stated in the entry as ``target_visible_dir`` when the two differ - guessing it is
    how a restore reports "file not found" for a file that is plainly there.
    """
    stated = str(getattr(job, "target_visible_dir", "") or "").strip()
    if stated:
        return stated
    remote = str(getattr(job, "target_server_id", "") or "").strip() not in (
        "", str(getattr(job, "server_id", "") or "").strip())
    if remote and str(getattr(job, "target_backup_dir", "") or "").strip():
        # The copy staged the set there, on the target's own filesystem: the same directory, named.
        return str(job.target_backup_dir)
    # Never the source's `backup_dir`: that is where the SOURCE keeps its backups, and reading it
    # as the target's was a guess that only held when both happened to share a mount.
    raise RestoreByIdError(
        f"{job.restore_id}: state target_visible_dir - the backup directory as the target sees it.")


# --------------------------------------------------------------------------- #
# Per-engine plans. Each mirrors a sequence proven against the real systems.
# --------------------------------------------------------------------------- #


#: Pieces the listings of the current plan could not read as backups (1.23). Cleared per restore.
_UNREADABLE: set[str] = set()


def _list_backup_files(request: dict[str, Any]) -> dict[str, Any]:
    """What is on the share, asked through the `common` CLI.

    Planning a restore is a read of somebody else's filesystem, so it crosses the same boundary
    the steps themselves do. Wrapped rather than called inline because the planners ask three or
    four times each and every one of them wants the failure as an exception, not as an empty file
    list — "no backups found" and "the listing command did not run" lead to opposite actions.
    """
    try:
        found = common_cli.run("list-backup-files", request)
    except common_cli.CommonCliError as exc:
        raise RestoreByIdError(str(exc)) from exc
    _UNREADABLE.update(found.get("unreadable") or [])
    return found


def _sqlserver_port(job: Any, *, data_dir: Any) -> int:
    """The SQL port of the instance the restore goes INTO.

    It was 1433 whatever the target was, so on a host running two SQL Server labs (1433 and 11433)
    a restore meant for one planned - and would have restored - against the other. The target's
    own inventory record says which port it listens on; ``env.MSSQL_PORT`` overrides it for a
    target named by a host record, which carries no port.
    """
    from dbabrain.lib import data_sources

    override = str((job.env or {}).get("MSSQL_PORT") or "").strip()
    if override:
        return int(override)
    wanted = str(getattr(job, "target_server_id", "") or "").strip()
    port = _instance_sql_port(wanted, data_dir=data_dir) if wanted else None
    if port is None:
        # No 1433, and never the source's port: either is a guess about which instance to
        # overwrite (review 0.25.0, B4.5).
        raise RestoreByIdError(
            f"{job.restore_id}: the target's SQL Server port is not stated - give env.MSSQL_PORT, or "
            f"a port on the db_instances.json record of {wanted or 'the target'}.")
    return port


def _instance_sql_port(server_id: str, *, data_dir: Any) -> int | None:
    """The port a SQL Server inventory record states, or ``None``."""
    from dbabrain.lib import data_sources

    for record in data_sources.load_db_instances(data_dir):
        if str(record.get("server_id") or "").strip() != server_id:
            continue
        if str(record.get("db_type") or "").strip().lower() in ("sqlserver", "mssql") and record.get("port"):
            return int(record["port"])
        return None
    return None


def _same_machine(left: str, right: str) -> bool:
    """Do two host strings name one machine? Compared as text, then by resolved address."""
    import socket

    a, b = str(left or "").strip().lower(), str(right or "").strip().lower()
    if not a or not b:
        return False
    if a == b:
        return True
    try:
        addresses = [{info[4][0] for info in socket.getaddrinfo(name, None)} for name in (a, b)]
    except OSError:
        return False
    return bool(addresses[0] & addresses[1])


def assert_target_is_not_source(job: Any, host: dict[str, Any], *, data_dir: Any) -> None:
    """Refuse a restore whose target is the source instance itself.

    The SQL Server engine path has had this check since 0.2x (`validate_restore_target_is_not_source`);
    this path, which the scheduler uses, never did (review 0.25.0, B4.5 / F10.3). Same machine is
    allowed only into another container, and for SQL Server only on another port.
    """
    from dbabrain.backup_restore.backup import resolve_ssh_target

    source = resolve_ssh_target(job.server_id, label=job.label, data_dir=data_dir,
                                require_container=False)
    if not _same_machine(source.host, host.get("host", "")):
        return
    target_container = str(getattr(job, "target_container", "") or "").strip()
    source_container = str(source.container_name or "").strip()
    if not target_container or target_container == source_container:
        raise RestoreByIdError(
            f"{job.restore_id}: the target is the source instance itself ({source.host}"
            f"{', container ' + source_container if source_container else ''}). Refused: a restore "
            f"there would overwrite production with its own backup.")
    if str(job.db_type).lower() == "sqlserver":
        source_port = _instance_sql_port(job.server_id, data_dir=data_dir)
        sql_port = _sqlserver_port(job, data_dir=data_dir)
        if source_port is None or sql_port is None:
            raise RestoreByIdError(
                f"{job.restore_id}: target and source are on one machine, and the SQL Server ports "
                f"needed to tell the two instances apart are not both stated "
                f"(source {source_port}, target {sql_port}).")
        if source_port == sql_port:
            raise RestoreByIdError(
                f"{job.restore_id}: the target port {sql_port} is the source instance's port on the "
                f"same machine. Refused: the restore would overwrite production.")


def _logs_through(files: list[dict[str, Any]], moment: str) -> list[dict[str, Any]]:
    """The log backups a restore to ``moment`` needs: every one finished by then, AND the first one
    finished after it - that is the one holding the moment, and STOPAT stops inside it.

    The chain used to be "finished at or before the moment", which leaves out exactly that log: the
    restore stopped at the end of the previous one, as early as fifteen minutes before what was
    asked, and reported success (the point-in-time drill, 2026-09-25). Compared in the server's
    clock, as the listing's own window is.
    """
    bound = _server_clock(moment)
    kept: list[dict[str, Any]] = []
    for item in files:  # oldest first, as the listing returns them
        kept.append(item)
        if item.get("finished_at") and _server_clock(item["finished_at"]) > bound:
            break
    return kept


def _server_clock(text: str) -> str:
    """A moment or a listing's time in the server's clock, comparable as text."""
    from dbabrain.lib.restore.moment import MomentError, server_clock_text

    head, dot, rest = str(text or "").strip().partition(".")
    text = head + rest.lstrip("0123456789") if dot and len(head) >= 19 else str(text or "").strip()
    try:
        return server_clock_text(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except MomentError:
        return text.replace("T", " ")[:19]


#: A backup file's own UTC stamp, as dbabrain's scripts name it: ``..._YYYYMMDD_HHMMSSZ.<ext>``.
_UTC_STAMP_IN_NAME = re.compile(r"_(\d{8})_(\d{6})Z\.")


def source_offset_minutes(files: list[dict[str, Any]]) -> int | None:
    """The source server's UTC offset, in minutes, read off its own backup files (0.39.0, 1.363).

    A SQL Server backup's header times (``BackupFinishDate``) and its log records are in the SOURCE
    instance's clock, and STOPAT is read in that clock. The restore used to hand them the moment in
    UTC - the target's clock - so from a Windows server on +07 every point in time landed seven hours
    early: found 2026-11-13 by the random point-in-time test (the restore to *now* stopped at the log of
    seven hours before and said done; a moment soon after the first full found no full before it).

    dbabrain's scripts name each file by its start in UTC (``_YYYYMMDD_HHMMSSZ``), so a file's header
    end minus its name is its duration plus the offset; a log backup lasts seconds, so the smallest
    such difference, to the nearest quarter hour, is the offset. ``None`` when no file carries a UTC
    stamp - a backup another tool wrote - and the moment is then read in the target's clock, as before.
    """
    from datetime import datetime

    diffs = []
    for item in files:
        found = _UTC_STAMP_IN_NAME.search(str(item.get("path") or "").replace("\\", "/").rsplit("/", 1)[-1])
        finished = str(item.get("finished_at") or "")[:19].replace("T", " ")
        if not found or len(finished) < 19:
            continue
        try:
            stamp = datetime.strptime(found.group(1) + found.group(2), "%Y%m%d%H%M%S")
            local = datetime.strptime(finished, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        diffs.append((local - stamp).total_seconds() / 60)
    if not diffs:
        return None
    return int(round(min(diffs) / 15) * 15)


def in_source_clock(moment: str, offset_minutes: int | None) -> str:
    """``moment`` as ``YYYY-MM-DD HH:MM:SS`` in the source's clock; unchanged when its offset is unknown."""
    if not moment or offset_minutes is None:
        return moment
    from dbabrain.lib.restore.moment import server_clock_text

    return server_clock_text(moment, server_utc_offset_hours=offset_minutes / 60)


def past_the_newest(files: list[dict[str, Any]], moment: str) -> str:
    """The newest file's end when ``moment`` lies at or after it, else ``""`` (0.39.0, 1.363).

    The operator, 2026-11-13: *restore must be able to restore to a point in time; the newest is a point
    in time of now.* A moment after the newest log is that log's end - nothing after it is in a backup -
    and asked for as a STOPAT it broke the restore: SQL Server leaves the database RESTORING when the
    last log ends before its STOPAT (Msg 4326), PostgreSQL 13+ stops with "recovery ended before
    configured recovery target was reached". So the chain is replayed to its end, and the answer says
    where that is.
    """
    ends = [str(item.get("finished_at")) for item in files if item.get("finished_at")]
    if not moment or not ends:
        return ""
    newest = max(ends, key=_server_clock)
    return newest if _server_clock(newest) <= _server_clock(moment) else ""


def _sqlserver_target(job: Any, secrets: dict[str, str], *, host: dict[str, Any], data_dir: Any) -> dict[str, Any]:
    """The SQL Server login a restore runs as. Stated (rules R50, G2.11): it ran as `sa` when the entry
    said nothing until 0.27.0, with a warning in the answer. The loader refuses such an entry; this is
    the same rule for a job built any other way."""
    username = str(job.env.get("MSSQL_USER") or "").strip()
    if not username:
        raise RestoreByIdError(
            f"{job.restore_id}: env.MSSQL_USER is not stated - the restore would log in as sa; "
            "state it - a record without it is refused (rules R50, G2.11).")
    return {"host": host["host"], "port": _sqlserver_port(job, data_dir=data_dir), "username": username,
            "password": _secret(job.env_secrets.get("MSSQL_PASSWORD", ""), secrets,
                                where=f"{job.restore_id}.env_secrets.MSSQL_PASSWORD")}


def _plan_sqlserver(job: Any, secrets: dict[str, str], *, point_in_time: str,
                    host: dict[str, Any], data_dir: Any, dry_run: bool = False,
                    resume: dict[str, dict[str, Any]] | None = None,
                    with_certificate: bool = True) -> list[dict[str, Any]]:
    """``with_certificate`` false: the certificate is the workflow's own step, run before this
    (0.40.0, 1.375) - a dry run still plans it."""
    _UNREADABLE.clear()  # this plan's listings only - never a previous restore's
    # The login first: a plan never runs as `sa` on its own, whatever else it lacks.
    target = _sqlserver_target(job, secrets, host=host, data_dir=data_dir)
    stated_dir = _visible_dir(job)
    directory = _engine_dir(stated_dir, host)
    steps: list[dict[str, Any]] = []

    cert_password = _secret(job.env_secrets.get("BACKUP_ENCRYPTION_PASSWORD", ""), secrets,
                            where=f"{job.restore_id}.env_secrets.BACKUP_ENCRYPTION_PASSWORD") \
        if job.env_secrets.get("BACKUP_ENCRYPTION_PASSWORD") and with_certificate else ""
    if cert_password:
        name = job.env.get("BACKUP_CERT_NAME", sqlserver_certificate.DEFAULT_NAME)
        # Imported NOW, before the listing below: RESTORE HEADERONLY cannot read an encrypted
        # backup on an instance without its certificate (Msg 33111), so a key step queued behind
        # the listing never ran on a fresh target - the listing failed first. The import finds
        # the certificate by thumbprint and never drops one, so doing it on every run is safe - and
        # it no longer replaces a target's own certificate of the same name. A dry run changes
        # nothing, so there it stays a planned step - and its listing may fail for this reason.
        steps.append(_key_from_vault(job, name=name, password=cert_password, target=target, host=host,
                                     directory=stated_dir, dry_run=dry_run))

    base = {"db_type": "sqlserver", "path": directory, "target": target}
    # Which databases, and the name each lands under - the entry's own statement (0.39.0, 1.362). It
    # used to be env.MSSQL_DATABASES, and with none named, whatever FULL the folder held.
    try:
        landing = restore_landing.require_landing(getattr(job, "landing", None),
                                                  label=f"restore_id={job.restore_id}")
    except restore_landing.LandingError as exc:
        raise RestoreByIdError(str(exc)) from exc
    found_names: list[str] = []
    if landing.all_databases:
        found = _list_backup_files({**base, "kinds": ["full"]})
        found_names = sorted({f["database_name"] for f in found["files"] if f["database_name"]})
    names = restore_landing.landing_names(landing, found_names)
    if not names:
        raise RestoreByIdError(f"{job.restore_id}: no databases found under {directory}.")

    asked = point_in_time
    for database, lands_as in names:
        # Listed by the name the BACKUP carries; restored as the name the entry states.
        scoped = {**base, "database_name": database}
        named = database if database.lower() == lands_as.lower() else f"{database} (as {lands_as})"
        resumed = (resume or {}).get(lands_as.lower())
        if resumed and resumed.get("complete"):
            # This entry's unfinished run already restored it to its end and opened it (1.372): a
            # recovered database takes no more of a chain, so it is kept as it is, said, and held to
            # where THAT run was to stop - while the databases the run never finished go on.
            steps.append({"op": "continued", "continued": {
                "database_name": lands_as, "source_database": database, "complete": True,
                "from_run": resumed.get("restore_run_id"), "after_file": resumed.get("file_name"),
                "after": resumed.get("after")}})
            steps.append({"op": "expect", "expect": {
                "database_name": lands_as, "source_database": database,
                "moment": str(resumed.get("stopat") or ""), "stopat": bool(resumed.get("stopat")),
                "chain_ends": str(resumed.get("after") or ""), "files": 0}})
            continue
        if asked:
            # The moment in the SOURCE's clock (1.363): the clock the headers, the log records and
            # STOPAT are in. Read per database - one entry, one server, but each from its own files.
            point_in_time = in_source_clock(asked, source_offset_minutes(
                _list_backup_files({**scoped, "kinds": ["log"]})["files"]))
        full = _list_backup_files({**scoped, "kinds": ["full"], "latest": True,
                                  **({"before": point_in_time} if point_in_time else {})})
        if not full["files"]:
            raise RestoreByIdError(f"{job.restore_id}: no full backup for {database}"
                                   + (f" at or before {point_in_time}" if point_in_time else "") + ".")
        anchor = full["newest_finished_at"]
        diff = _list_backup_files({**scoped, "kinds": ["diff"], "latest": True, "after": anchor,
                                  **({"before": point_in_time} if point_in_time else {})})
        anchor = diff["newest_finished_at"] or anchor
        logs = _list_backup_files({**scoped, "kinds": ["log"], "after": anchor})
        if point_in_time:
            logs = {**logs, "files": _logs_through(logs["files"], point_in_time)}
        # The chain is held to its LSNs before anything is restored (1.250, gap G10): a missing log
        # found at RESTORE LOG leaves the target half-restored, and found here it is named instead.
        broken = restore_integrity.sqlserver_chain_break(
            full["files"][0], diff["files"][0] if diff["files"] else None, logs["files"])
        if broken:
            raise RestoreByIdError(f"{job.restore_id}: {named}: the backup chain is broken - {broken}. "
                                   "Nothing was restored.")

        # A database this entry's own failed run left RESTORING goes on from the file after the last
        # one the TARGET says it applied (1.372) - when that file is in the chain to restore now. A
        # chain that has moved on (a newer full or differential) cannot take it up: said, not guessed.
        chain = [full["files"][0]] + ([diff["files"][0]] if diff["files"] else []) + list(logs["files"])
        skip = 0
        if resumed:
            at = next((index for index, item in enumerate(chain)
                       if _clock(item.get("finished_at")) == str(resumed.get("after") or "")), None)
            if at is None or at >= len(chain) - 1:
                raise RestoreByIdError(
                    f"{job.restore_id}: {named}: the target holds it RESTORING - run {resumed.get('restore_run_id')} "
                    f"of this entry stopped after {resumed.get('file_name')} - and "
                    + ("that file is not in the chain to restore now (a newer full or differential has been "
                       "taken since)" if at is None else "nothing of its chain is left to apply")
                    + ". It cannot be continued: drop the unfinished database on the target, or state "
                      "overwrite_existing: true to restore over it. Nothing was restored.")
            skip = at + 1
            steps.append({"op": "continued", "continued": {
                "database_name": lands_as, "source_database": database, "complete": False,
                "from_run": resumed.get("restore_run_id"), "after_file": resumed.get("file_name"),
                "after": resumed.get("after"), "skipped": skip, "left": len(chain) - skip}})
        log_files = list(logs["files"])[max(0, skip - 1 - (1 if diff["files"] else 0)):]

        # NORECOVERY on everything but the last step: a database recovered early cannot take the
        # rest of its chain, and the only fix is to start the whole restore again.
        tail = "log" if logs["files"] else ("diff" if diff["files"] else "full")
        steps.append({"op": "restore-full", "source_database": database, "chain": [full["files"][0]], "request": {
            "db_type": "sqlserver", "database_name": lands_as, "target": target,
            "backup_path": full["files"][0]["path"], "with_recovery": tail == "full",
            # Onto another server, always (0.39.0, 1.354): the backup names its source's paths -
            # a Windows server's D:\ on a Linux target - so every file goes to the target's folders.
            "relocate": {},
            # The entry's word: a name the target already holds - in any state - is refused without it
            # (G2.10; any state since 0.39.0, 1.362). Never while a lockdown is on (1.220): the step
            # then refuses it.
            "overwrite_existing": bool(job.overwrite_existing) and not _locked()}})
        if skip:
            # Applied by the unfinished run this one goes on from: the full is not restored again.
            steps.pop()
        if diff["files"] and skip <= 1:
            steps.append({"op": "restore-diff", "source_database": database, "chain": [diff["files"][0]],
                          "request": {
                              "db_type": "sqlserver", "database_name": lands_as, "target": target,
                              "backup_path": diff["files"][0]["path"], "with_recovery": tail == "diff"}})
        reached = ""
        if logs["files"]:
            reached = past_the_newest(logs["files"], point_in_time)
            # One step per log file (0.39.0, 1.370; the operator, 2026-11-14: *one message for each
            # log file*). One command for all of them said one message for 46 logs, and a failure on
            # the seventeenth was recorded on all 46 - the step's error is each of its files'. One
            # file a step: its own message as it goes in, its own chain row applied or failed. The
            # statements are the ones one command sent: NORECOVERY on each, the recovery and the
            # STOPAT on the last.
            for number, item in enumerate(log_files, start=1):
                last_log = number == len(log_files)
                steps.append({"op": "restore-log", "source_database": database, "chain": [item],
                              "log_number": number, "log_total": len(log_files),
                              "request": {
                                  "db_type": "sqlserver", "database_name": lands_as, "target": target,
                                  "backup_path": item["path"], "with_recovery": last_log,
                                  **({"stopat": point_in_time} if last_log and point_in_time and not reached
                                     else {})}})
            if reached:
                steps.append({"op": "warning", "warning": (
                    f"{named}: the moment {point_in_time} is at or after its newest log backup (finished "
                    f"{reached}) - restored to that log's end; what came after it is in no backup yet")})
        # What this database was asked to stop at, for the check after it (1.362): the moment as the
        # engine was handed it - the SOURCE's clock - whether a STOPAT carries it, and the end of the
        # last backup of its chain. Read back against what the target itself recorded.
        last = (logs["files"] or diff["files"] or full["files"])[-1]
        steps.append({"op": "expect", "expect": {
            "database_name": lands_as, "source_database": database,
            "moment": point_in_time or "", "stopat": bool(point_in_time and logs["files"] and not reached),
            "chain_ends": str(last.get("finished_at") or ""),
            "files": 1 + (1 if diff["files"] else 0) + len(logs["files"])}})

    # The consistency check too (1.251) unless the entry says `checkdb: false`.
    steps.append({"op": "verify-restore", "request": {
        "db_type": "sqlserver", "database_names": [lands_as for _database, lands_as in names], "target": target,
        "consistency": getattr(job, "checkdb", True), **consistency_check.verify_fields(job)}})
    if _UNREADABLE:
        steps.append({"op": "warning", "warning": (
            f"{len(_UNREADABLE)} file(s) named like backups could not be read as backups and were "
            f"passed over - the restore used the newest chain it could read: "
            f"{', '.join(sorted(_UNREADABLE)[:5])}")})
    return steps


#: The folder the vault's pair is placed in on the target for the import, and removed from right
#: after - one file per command, each held to the pair's own facts.
KEY_STAGE = "__dbabrain_cert"


def _vault_pair(server_id: str, name: str):
    """``(cer, pvk, row)``, the newest proven pair dbabrain's vault holds, or ``None``."""
    from dbabrain.backup_restore.certificate_take import vault_pair
    from dbabrain.lib.config import load_config
    from dbabrain.lib.secret_text import resolve_key

    passphrase = resolve_key()
    if not passphrase:
        return None
    return vault_pair(load_config(), server_id=server_id, certificate_name=name, passphrase=passphrase)


def _key_from_vault(job: Any, *, name: str, password: str, target: dict[str, Any], host: dict[str, Any],
                    directory: str, dry_run: bool) -> dict[str, Any]:
    """The source's certificate, from dbabrain's vault, imported on the target (0.37.0).

    Never from beside the backups: the operator, 2026-11-10 - *the certificate and key are kept in
    dbabrain and deleted on the server_id; a copy carries the backups, never _cert*. The pair is
    placed on the target only for the import (``push-file``, proven on arrival), and removed right
    after by ``delete-verified-file`` - one file per command, held to the pair's own bytes and
    sha256 - then the empty folder. A pair that cannot be removed fails the restore by name.
    """
    stage = f"{directory.rstrip('/')}/{KEY_STAGE}"
    # The engine reads the pair by its own form of the path (a Windows SQL Server: backslashes); the
    # file commands place and remove it by the stated one.
    request = {"certificate_name": name, "cer_path": _engine_dir(f"{stage}/{name}.cer", host),
               "pvk_path": _engine_dir(f"{stage}/{name}.pvk", host), "password": password, "target": target}
    step: dict[str, Any] = {"op": "restore-key", "request": request, "from": "dbabrain's certificate vault"}
    if dry_run:
        return step
    source_id = str(getattr(job, "server_id", "") or "").strip()
    pair = _vault_pair(source_id, name)
    if pair is None:
        raise RestoreByIdError(
            f"{job.restore_id}: dbabrain's certificate vault holds no proven {name} for {source_id} - "
            "take it first (backup_restore take-certificate --backup-id ID --apply). A certificate is "
            "never read from beside the backups.")
    cer, pvk, row = pair
    placed: list[tuple[str, str, int, str]] = []
    failed = False
    try:
        with tempfile.TemporaryDirectory(prefix="dbabrain_key_") as local:
            for suffix, data in (("cer", cer), ("pvk", pvk)):
                file_name = f"{name}.{suffix}"
                local_path = Path(local) / file_name
                local_path.write_bytes(data)
                remote = f"{stage}/{file_name}"
                _execute("push-file", {"local_path": str(local_path), "remote_path": remote, "host": host})
                placed.append((remote, file_name, len(data), hashlib.sha256(data).hexdigest()))
        _execute("restore-key", request)
    except BaseException:
        failed = True
        raise
    finally:
        left = _remove_staged_key(host, stage=stage, placed=placed)
        if left and not failed:
            raise RestoreByIdError(f"{job.restore_id}: the certificate imported, but its pair could not be "
                                   f"removed from the target: {'; '.join(left)}")
    step["done"] = True
    step["vault_row"] = row["certificate_id"]
    return step


def _remove_staged_key(host: dict[str, Any], *, stage: str, placed: list[tuple[str, str, int, str]]) -> list[str]:
    """What could not be removed - each file by its own command and facts, then the folder."""
    left = []
    root = stage.rsplit("/", 1)[0]
    for remote, file_name, size, sha in placed:
        try:
            _execute("delete-verified-file", {"host": host, "path": remote, "file_name": file_name,
                                              "size_bytes": size, "sha256": sha, "must_be_under": stage})
        except RestoreByIdError as exc:
            left.append(f"{remote}: {exc}")
    if placed and not left:
        try:
            _execute("delete-empty-folder", {"host": host, "path": stage, "must_be_under": root})
        except RestoreByIdError as exc:
            left.append(f"{stage}: {exc}")
    return left


def _locked() -> bool:
    from dbabrain.db.lockdown_store import lockdown_on

    return lockdown_on()


def _plan_oracle(job: Any, secrets: dict[str, str], *, point_in_time: str,
                 host: dict[str, Any], data_dir: Any, dry_run: bool = False,
                 clock_offset_minutes: int | None = None) -> list[dict[str, Any]]:
    directory = _visible_dir(job)
    request: dict[str, Any] = {
        "db_type": "oracle", "mode": "duplicate", "host": host,
        "backup_path": directory, "backup_location": directory,
        "oracle_sid": job.env.get("ORACLE_SID", "FREE"),
    }
    encryption = job.env_secrets.get("BACKUP_ENCRYPTION_PASSWORD", "")
    if encryption:
        request["encryption_password"] = _secret(
            encryption, secrets, where=f"{job.restore_id}.env_secrets.BACKUP_ENCRYPTION_PASSWORD")
    if point_in_time:
        # In the source's clock (1.385): the time its archived logs carry, which UNTIL TIME is read
        # against - the moment arrives in UTC with its offset stated (prepare).
        from dbabrain.lib.restore.moment import server_clock_text

        request["stopat"] = (server_clock_text(point_in_time, server_utc_offset_hours=clock_offset_minutes / 60)
                             if clock_offset_minutes is not None else point_in_time)
    # The entry's word (0.39.0, 1.362): a DUPLICATE rebuilds the whole target database, so without
    # it the step stops when that database holds user data. Never while a lockdown is on (1.220).
    request["overwrite_existing"] = bool(getattr(job, "overwrite_existing", False)) and not _locked()
    # One DUPLICATE and nothing else: RMAN picks the level 0, the incrementals and the archived
    # logs itself, so a diff or log step here would describe work it did not do. The chain record
    # names the folder for the same reason: which pieces RMAN read is RMAN's to say.
    return [{"op": "restore-full", "request": request, "chain": [{"path": directory.rstrip("/") + "/"}]},
            # The SID goes to the check as well as to the restore. Without it `sqlplus / as sysdba`
            # takes whatever ORACLE_SID the shell carries, which is right for a container holding
            # one instance and wrong for a host holding two - and a check that answers for the
            # wrong instance under the right name is worse than no check.
            {"op": "verify-restore", "request": {"db_type": "oracle", "host": host,
                                                 "oracle_sid": request["oracle_sid"],
                                                 "consistency": getattr(job, "checkdb", True),
                                                 **consistency_check.verify_fields(job)}}]


def _plan_postgresql(job: Any, secrets: dict[str, str], *, point_in_time: str,
                     host: dict[str, Any], data_dir: Any, dry_run: bool = False) -> list[dict[str, Any]]:
    directory = _visible_dir(job)
    # Listed on the HOST: the layout is read from directory names, and the host is where the
    # staging lives. The combine itself runs inside the container.
    listing_host = {**host, "runtime": "linux"}
    listing_host.pop("container", None)
    base = {"db_type": "postgresql", "path": directory, "host": listing_host}

    full = _list_backup_files({**base, "kinds": ["full"], "latest": True,
                              **({"before": point_in_time} if point_in_time else {})})
    if not full["files"]:
        raise RestoreByIdError(f"{job.restore_id}: no base backup found under {directory}.")
    # The incrementals by NAME, as pg_combinebackup's chain and `backup-chain` read it: every
    # `_INCR` whose stamp sorts after the full's. By finish time ("after" the full) they tied when
    # a full and its incremental were staged in the same second, and the chain lost the
    # incremental (2026-09-25).
    incr = _list_backup_files({**base, "kinds": ["diff"],
                              **({"before": point_in_time} if point_in_time else {})})
    full_name = posixpath.basename(full["files"][0]["path"].rstrip("/"))
    members = [full["files"][0]] + sorted(
        (f for f in incr["files"] if posixpath.basename(f["path"].rstrip("/")) > full_name),
        key=lambda f: posixpath.basename(f["path"].rstrip("/")))
    chain = [f["path"] for f in members]

    # The WAL from the newest member's start, held to its names before anything is restored (1.250,
    # gap G10): recovery to latest stops at the first missing segment and opens on an older state,
    # and the drill would have reported done.
    wal = _list_backup_files({**base, "kinds": ["log"], "wal_segments": True})
    segments = [name for item in wal["files"] for name in item.get("wal_segments") or []]
    broken = restore_integrity.postgresql_wal_break(
        str(members[-1].get("start_wal") or ""), segments, to_latest=not point_in_time)
    if broken:
        raise RestoreByIdError(f"{job.restore_id}: the WAL chain is broken - {broken}. Nothing was restored.")

    data_directory = job.env.get("PGDATA", "/var/lib/postgresql/18/docker")
    staging = job.env.get("PG_STAGING", "/var/lib/postgresql/dbabrain_staging")
    common = {"db_type": "postgresql", "host": host, "data_dir": data_directory,
              "staging_dir": staging}
    # Every incremental after the full, together: pg_combinebackup reads them as one, and a chain
    # missing its middle produces a data directory that starts and is incomplete.
    step = ({"op": "restore-diff", "request": {**common, "backup_paths": chain}} if len(chain) > 1
            else {"op": "restore-full", "request": {**common, "backup_path": chain[0]}})
    # The entry's word (0.39.0, 1.362): the base backup becomes the target's whole data directory, so
    # without it the step stops when that directory holds a cluster with user data. Never while a
    # lockdown is on (1.220).
    step["request"]["overwrite_existing"] = bool(getattr(job, "overwrite_existing", False)) and not _locked()
    # What this restore applies, for the record: each base backup of the chain, then the WAL folder.
    step["chain"] = [*members, {"path": f"{directory.rstrip('/')}/wal/", "step": "restore-log",
                                "finished_at": wal.get("newest_finished_at")}]
    # A moment at or after the newest archived WAL is the end of the WAL (1.363): asked for as a target,
    # PostgreSQL 13+ stops the recovery with "recovery ended before configured recovery target".
    reached = past_the_newest(wal["files"], point_in_time)
    recovery = {"wal_dir": f"{directory.rstrip('/')}/wal",
                **({"stopat": point_in_time} if point_in_time and not reached else {})}
    if host.get("runtime") == "docker":
        # Into a container the recovery configuration travels WITH the combine, written before the
        # first start. As a separate step after it, it reached a server that had already started
        # and replayed no WAL at all (the lab drill, 2026-09-24).
        step["request"].update(recovery)
        replay: list[dict[str, Any]] = []
    else:
        replay = [{"op": "restore-log", "request": {**common, **recovery}}]
    said = ([{"op": "warning", "warning": (
        f"the moment {point_in_time} is at or after the newest WAL archived (finished {reached}) - recovered to "
        "the end of the WAL; what came after it is in no backup yet")}] if reached else [])
    return [
        step,
        *replay,
        *said,
        # PGPORT when the job states one: psql with no -p takes the default cluster, which is right
        # inside a container and wrong on a host running two.
        {"op": "verify-restore", "request": {
            "db_type": "postgresql", "host": host, "consistency": getattr(job, "checkdb", True),
            **consistency_check.verify_fields(job),
            **({"port": job.env["PGPORT"]} if job.env.get("PGPORT") else {})}},
    ]


#: What a restore onto another machine runs after its copy, said by a dry run that has nothing staged
#: to plan the steps from yet (0.39.0, 1.356). The step names are the ``common`` commands.
DRY_RUN_STEPS = {
    "sqlserver": ["restore-key - the backup certificate from the vault, when the backup is encrypted",
                  "restore-full per database - every file moved to the target's folders (relocate), NORECOVERY",
                  "restore-diff - the newest differential after that full, when there is one",
                  "restore-log - each log after it in order; STOPAT on the last for a point in time; RECOVERY",
                  "verify-restore - DBCC CHECKDB on each restored database",
                  "delete - staged files past cleanup_retention, each by its recorded facts"],
    "postgresql": ["restore-full - the base backup laid down as the data directory (pg_combinebackup "
                   "with its incrementals into one)",
                   "restore-log - WAL replayed; recovery_target_time for a point in time",
                   "verify-restore - the server started and pg_amcheck run",
                   "delete - staged files past cleanup_retention, each by its recorded facts"],
    # A lab drill, 2026-11-16: an Oracle dry run answered `steps: []`, as if it would run nothing.
    "oracle": ["restore-full - one RMAN DUPLICATE from the staged folder: the level 0, the incrementals and "
               "the archived logs as RMAN picks them; UNTIL TIME for a point in time",
               "verify-restore - a query on the opened database, RMAN VALIDATE CHECK LOGICAL and "
               "v$database_block_corruption",
               "delete - staged files past cleanup_retention, each by its recorded facts"],
}
DRY_RUN_STEPS["postgres"] = DRY_RUN_STEPS["postgresql"]


_PLANNERS = {"sqlserver": _plan_sqlserver, "oracle": _plan_oracle,
             "postgresql": _plan_postgresql, "postgres": _plan_postgresql}


#: The step names are the `common` CLI command names. That was already true when these ran
#: in-process; since 2026-08-15 it is also how they are invoked.
_STEP_COMMANDS = frozenset({"restore-key", "restore-full", "restore-diff", "restore-log",
                            "verify-restore",
                            # 0.37.0: the vault's certificate placed for the import, then removed.
                            "push-file", "delete-verified-file", "delete-empty-folder"})


def _execute(op: str, request: dict[str, Any]) -> dict[str, Any]:
    """Run one `common` primitive through its CLI and return the ``data`` it answered with.

    The step names *are* the CLI command names — that was already true when these ran in-process,
    and since 2026-08-15 it is also how they are invoked. See
    :mod:`dbabrain.transport.common_cli` for why the transport lives app-side and why a failed
    step raises instead of coming back as data.
    """
    if op not in _STEP_COMMANDS:
        raise RestoreByIdError(f"unknown step {op!r}.")
    try:
        return common_cli.run(op, request)
    except common_cli.CommonCliError as exc:
        raise RestoreByIdError(str(exc)) from exc


def _plan_summary(steps: list[dict[str, Any]]) -> str:
    """What a plan restores, in the words a Telegram reader needs: which backups, how many.

    Read off the plan itself rather than written per engine, so it cannot describe work the plan
    does not do.
    """
    parts: list[str] = []
    by_database: dict[str, list[str]] = {}
    logs_of: dict[str, int] = {}
    for step in steps:
        op, req = step.get("op"), step.get("request") or {}
        paths = [str(p) for p in (req.get("backup_paths") or
                                  ([req["backup_path"]] if req.get("backup_path") else []))]
        names = [_file_name(p) for p in paths]
        if op == "restore-key":
            parts.append("the backup certificate")
        elif op == "restore-full" and req.get("mode") == "duplicate":
            parts.append(f"RMAN DUPLICATE of {req.get('oracle_sid') or 'the instance'} from "
                         f"{req.get('backup_location') or (paths[0] if paths else '?')} (level 0, "
                         "incrementals and archived logs as RMAN picks them)")
        elif req.get("database_name"):
            # SQL Server: one chain per database.
            words = {"restore-full": f"full {names[0] if names else ''}".strip(),
                     "restore-diff": f"diff {names[0] if names else ''}".strip()}
            if op in words or op == "restore-log":
                source = str(step.get("source_database") or req["database_name"])
                label = source if source.lower() == str(req["database_name"]).lower() \
                    else f"{source} as {req['database_name']}"
                said = by_database.setdefault(label, [])
                if op != "restore-log":
                    said.append(words[op])
                else:
                    # A log step holds one file since 1.370: a database's logs are counted together,
                    # and a single one is named - the plan says "46 log(s)", a step says which log.
                    count = logs_of.get(label, 0)
                    logs_of[label] = count + len(paths)
                    if count:
                        said.pop()
                    said.append(f"log {names[0]}" if logs_of[label] == 1 and names
                                else f"{logs_of[label]} log(s)")
        elif op in ("restore-full", "restore-diff"):
            # A summary that raised would fail the restore it describes; a step without a named
            # path is described, not indexed.
            chain = ("the planned backup" if not names else f"base backup {names[0]}"
                     + (f" + {len(names) - 1} incremental(s)" if len(names) > 1 else ""))
            parts.append(chain + (", then WAL replay" if req.get("wal_dir") else ""))
        elif op == "restore-log":
            parts.append("WAL replay")
    parts.extend(f"{database}: {' + '.join(words)}" for database, words in by_database.items())
    return "; ".join(parts)




#: The steps whose files are a restore's chain - the ``common.cli`` command names, as the catalog holds them.
_CHAIN_STEPS = ("restore-full", "restore-diff", "restore-log")


def step_said(step: dict[str, Any]) -> str:
    """One restore step in the words of the chat (0.39.0, 1.370): the database, the backup(s) it
    applies and when they were taken, a STOPAT it carries, and whether it leaves the database open.

    The operator, 2026-11-13: *restore is like backup - it must message every step of its flow;
    restore has restore full -> diff -> log as well, a message for each*. Read off the step itself
    (:func:`_plan_summary`), so it cannot describe work the step does not do; and never the reason a
    restore fails - a step that cannot be described is named by its command.
    """
    try:
        request = step.get("request") or {}
        said = _plan_summary([step]) or str(step.get("op") or "a step")
        if step.get("log_total"):
            # Where this log stands in its database's logs (1.370): a reader of the chat counts them down.
            said += f" ({step['log_number']} of {step['log_total']} logs)"
        ends =[_clock(item.get("finished_at")) for item in step.get("chain") or [] if item.get("finished_at")]
        if len(ends) == 1:
            said += f" (backup finished {ends[0]})"
        elif ends:
            said += f" (backups finished {ends[0]} .. {ends[-1]})"
        if request.get("stopat"):
            said += f", stopped at {request['stopat']}"
        if request.get("database_name"):
            said += (" - recovered, the database is open" if request.get("with_recovery")
                     else " - left restoring for the next step")
        return said
    except Exception:  # noqa: BLE001 - a message is not the restore
        return str(step.get("op") or "a step")


def continued_said(continued: list[dict[str, Any]]) -> str:
    """One line per database a run took up from an unfinished one (1.372), for the chat and the END message."""
    parts = []
    for item in continued:
        name = item["database_name"] if str(item.get("source_database") or "").lower() == str(item["database_name"]).lower() \
            else f"{item.get('source_database')} as {item['database_name']}"
        if item.get("complete"):
            parts.append(f"{name}: kept as run {item.get('from_run')} restored it (to {item.get('after')})")
        else:
            parts.append(f"{name}: continued after {item.get('after_file')} - run {item.get('from_run')} stopped "
                         f"there; {item.get('skipped')} file(s) already applied, {item.get('left')} to go")
    return "; ".join(parts)


def continuation(recorded: list[dict[str, Any]], *, state: Any, restored_to: Any) -> dict[str, Any] | None:
    """Whether a database the target already holds is this entry's own unfinished restore - and after
    which file a run goes on (0.39.0, 1.372). ``None`` when it is not: the name is then held like any
    other database that exists (refused, or replaced when the entry says so).

    The operator, 2026-11-13: *like backup - can a failed restore go on?* Until now it could not: a
    restore that failed after its full left the database RESTORING, and the next run - which since
    1.362 replaces nothing that exists - was refused by that very database. Two records are held to
    each other, and it goes on only where they agree:

    - ``recorded``: the chain rows of this entry's runs onto this target since its last finished
      restore, for this landing name (``BackupCatalogStore.unfinished_chain``) - what makes the
      database *this entry's*; a RESTORING database nobody recorded is somebody's standby.
    - ``restored_to``: what the TARGET says it applied last (``verify-restore``, from msdb) - the
      truth about where it stopped: a SQL Server log is a step of its own since 1.370, so its row says
      which log failed - but a row is what dbabrain asked and heard back, the target is what it holds.

    RESTORING, the target's last backup among the recorded files: continued after that file. ONLINE
    with its last recorded file applied and that file the target's last: this entry's unfinished run
    restored it to its end - kept (``complete``), so the run's other databases can go on.
    """
    if not recorded or not isinstance(restored_to, dict):
        return None
    last = _clock(restored_to.get("last_backup_finished"))
    if not last:
        return None
    at = next((row for row in reversed(recorded) if _clock(row.get("backup_ended_at")) == last), None)
    if at is None:
        return None
    found = {"restore_run_id": at.get("restore_run_id"), "file_name": at.get("file_name"), "after": last}
    status = str(state or "").upper()
    if status == "RESTORING":
        return {**found, "complete": False}
    final = recorded[-1]
    if (status == "ONLINE" and str(final.get("result") or "") == "applied"
            and _clock(final.get("backup_ended_at")) == last):
        return {**found, "complete": True, "stopat": str(final.get("stopat") or "")}
    return None


def _file_name(path: str) -> str:
    """A step's file name, whichever separator its path uses - the last part, or the path itself.

    A SQL Server on Windows reads its backup as ``C:\\...\\APP_LOG_....trn`` (0.39.0): split on ``/``
    alone, the "name" was the whole path, so its chain rows were tied to no catalogued backup file and
    its messages named the path (found by the first restore onto the lab host's native instance, 2026-11-14)."""
    text = str(path or "").rstrip("/\\")
    return re.split(r"[\\/]", text)[-1] or str(path or "")


def _chain_rows(job: Any, steps: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[int, list[int]]]:
    """The chain a plan applies, one row per file in the order they go in, and - per step index - the
    positions of its files. Read off the plan's own steps, so it cannot name a file the plan does not use."""
    rows: list[dict[str, Any]] = []
    positions: dict[int, list[int]] = {}
    for index, step in enumerate(steps):
        files = step.get("chain") or []
        if step.get("op") not in _CHAIN_STEPS or not files:
            continue
        request = step.get("request") or {}
        for number, item in enumerate(files, start=1):
            last = number == len(files)
            path = str(item.get("path") or "")
            rows.append({
                "source_server_id": str(getattr(job, "server_id", "") or ""),
                "target_server_id": str(getattr(job, "target_server_id", "") or getattr(job, "server_id", "") or ""),
                "database_name": str(step.get("source_database") or ""),
                "restored_as": str(request.get("database_name") or ""),
                # A file may say its own step: PostgreSQL's WAL folder travels with the combine.
                "step": str(item.get("step") or step["op"]),
                "file_name": _file_name(path), "file_path": path,
                "backup_ended_at": str(item.get("finished_at") or "") or None,
                "with_recovery": 1 if last and request.get("with_recovery") else 0,
                "stopat": str(request["stopat"]) if last and request.get("stopat") else None})
            positions.setdefault(index, []).append(len(rows))
    return rows, positions


def _record_chain(catalog: Any, run_id: int | None, job: Any,
                  steps: list[dict[str, Any]]) -> tuple[dict[int, list[int]], str]:
    """Write the plan's chain into the catalog, each file tied to its backup file where the catalog
    knows it. ``({step index: positions}, warning)`` - the warning is said when the chain could not be
    written: the record is not the restore (:func:`copy_gate.close_restore_runs`), but a restore whose
    files nobody can show afterwards says so instead of ending as if they were kept."""
    rows, positions = _chain_rows(job, steps)
    if not rows:
        return {}, ""
    if catalog is None or run_id is None:
        return {}, ("the files this restore applied are not recorded - no catalog was open for it" if catalog is None
                    else "the files this restore applied are not recorded - the run has no catalog row")
    try:
        # A PostgreSQL base backup is applied as its directory; recorded file by file (1.386), it is tied
        # to its backup_manifest - the file that stands for the whole backup.
        names = {row["file_name"] for row in rows}
        known = catalog.backup_file_facts(server_id=str(getattr(job, "server_id", "") or ""),
                                          file_names=sorted(names | {f"{name}/backup_manifest" for name in names
                                                                     if restore_integrity.is_base_directory("FULL_BASE", name)}))
        for row in rows:
            fact = known.get(row["file_name"]) or known.get(f"{row['file_name']}/backup_manifest")
            if fact:
                row["file_id"] = fact["file_id"]
                row["file_type"] = str(fact.get("file_type") or "")
        catalog.plan_chain(int(run_id), rows)
    except Exception as exc:  # noqa: BLE001 - the record is not the restore; it is said, never silent.
        return {}, f"the files this restore applied could not be recorded - {type(exc).__name__}: {exc}"[:400]
    return positions, ""


def _mark_chain(catalog: Any, run_id: int | None, positions: list[int] | None, *, result: str,
                error_text: str = "") -> None:
    if catalog is None or run_id is None or not positions:
        return
    try:
        catalog.set_chain_step(int(run_id), positions, result=result, error_text=error_text)
    except Exception:  # noqa: BLE001 - the record is not the restore
        return


def _clock(text: Any) -> str:
    """A moment as ``YYYY-MM-DD HH:MM:SS`` for comparing two of them; ``""`` for none."""
    raw = str(text or "").strip()
    return _server_clock(raw) if raw else ""


def where_it_stopped(expects: list[dict[str, Any]], rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """What the target recorded of each restored database, held to what the plan asked (0.39.0, 1.362).

    ``(landed, off_target)``. A restore that opens and checks clean can still be in the wrong place:
    1.363's stopped seven hours early and said done. So after the check, the target's own record is
    read (``verify-restore`` answers it per database as ``restored_to``) and compared:

    - a moment carried by a STOPAT: the target's ``stop_at`` is that moment;
    - anything else - the newest backup, a moment at or past the newest log, a chain with no log:
      the last backup the target applied ended when the last file of the plan's chain ended.

    Both sides are in the SOURCE's clock - the backup's headers and STOPAT are. ``off_target`` names
    each database whose record differs; one the target gave no record for is ``not read`` and a
    difference of nothing (an engine or a login that cannot answer is not a wrong restore).
    """
    by_name = {str(row.get("database_name") or "").lower(): row for row in rows}
    landed: list[dict[str, Any]] = []
    off_target: list[str] = []
    for expect in expects:
        name = str(expect.get("database_name") or "")
        row = by_name.get(name.lower()) or {}
        got = row.get("restored_to") if isinstance(row.get("restored_to"), dict) else {}
        item = {"database_name": name, "source_database": expect.get("source_database") or name,
                "state": row.get("state"), "asked": expect.get("moment") or "the newest backup",
                "by_stopat": bool(expect.get("stopat")), "chain_ends": expect.get("chain_ends") or "",
                "stop_at": got.get("stop_at"), "last_backup_finished": got.get("last_backup_finished"),
                "last_backup_type": got.get("last_backup_type"), "restored_at": got.get("restored_at")}
        if not got or (not got.get("stop_at") and not got.get("last_backup_finished")):
            item["verdict"] = "not read"
        elif expect.get("stopat"):
            if _clock(got.get("stop_at")) == _clock(expect.get("moment")):
                item["verdict"] = "stopped at the moment asked"
            else:
                item["verdict"] = "NOT at the moment asked"
                off_target.append(f"{name}: asked to stop at {_clock(expect.get('moment'))}, the target recorded "
                                  f"{_clock(got.get('stop_at')) or 'no stop point'}")
        elif not expect.get("chain_ends"):
            item["verdict"] = "not read"
        elif _clock(got.get("last_backup_finished")) == _clock(expect.get("chain_ends")):
            item["verdict"] = "restored to the end of its chain"
        else:
            item["verdict"] = "NOT at the end of its chain"
            off_target.append(f"{name}: its chain ends {_clock(expect.get('chain_ends'))}, the last backup the "
                              f"target applied ended {_clock(got.get('last_backup_finished')) or 'at no known time'}")
        landed.append(item)
    return landed, off_target


def landed_said(landed: list[dict[str, Any]]) -> str:
    """One line per database for the END message: where it stopped, in the source's clock."""
    parts = []
    for item in landed:
        name = item["database_name"] if str(item["source_database"]).lower() == str(item["database_name"]).lower() \
            else f"{item['source_database']} as {item['database_name']}"
        where = (f"stopped at {_clock(item.get('stop_at'))}" if item.get("by_stopat") and item.get("stop_at")
                 else f"last backup applied ended {_clock(item.get('last_backup_finished'))}"
                 if item.get("last_backup_finished") else "stop point not read")
        parts.append(f"{name}: {where} ({item.get('verdict')})")
    return "; ".join(parts)


def _where_it_lands(job: Any, secrets: dict[str, str], *, host: dict[str, Any], data_dir: Any,
                    files: list[dict[str, Any]], catalog: Any = None) -> list[dict[str, Any]]:
    """For a dry run: each database of the restore, the name it lands under, and what the target holds
    under that name now - asked read only (``verify-restore`` with no consistency check).

    SQL Server lands database by database. PostgreSQL and Oracle lay down a whole instance: one row
    saying so, and that the step itself reads what the target holds when it runs.
    """
    engine = str(getattr(job, "db_type", "") or "").lower()
    overwrite = bool(getattr(job, "overwrite_existing", False))
    if engine not in restore_landing.BY_DATABASE_ENGINES:
        return [{"lands": "the whole instance of the target",
                 "will": ("REPLACES whatever the target instance holds (overwrite_existing is true)" if overwrite else
                          "refused by its restore step when the target instance holds user data "
                          "(overwrite_existing is false) - read when it runs, not by this dry run")}]
    landing = getattr(job, "landing", None)
    if landing is None:
        return []
    found = sorted({str(item.get("path") or "").replace("\\", "/").split("/")[0] for item in files
                    if "/" in str(item.get("path") or "").replace("\\", "/")})
    names = restore_landing.landing_names(landing, found)
    if not names:
        return []
    try:
        target = {"host": host["host"], "port": _sqlserver_port(job, data_dir=data_dir),
                  "username": str(job.env.get("MSSQL_USER") or "").strip(),
                  "password": _secret(job.env_secrets.get("MSSQL_PASSWORD", ""), secrets,
                                      where=f"{job.restore_id}.env_secrets.MSSQL_PASSWORD")}
        # A failed verdict is the usual answer here, not an error: verify-restore says "not usable" for
        # a database that is not there - exactly the name a restore under a new name asks about (found
        # on the first dry run of one, 2026-11-13: the plan read "the target could not be asked").
        _usable, answer, error = common_cli.run_allowing_failure(
            "verify-restore", {"db_type": "sqlserver", "consistency": False, "target": target,
                               "database_names": [lands_as for _source, lands_as in names]})
        rows = answer.get("databases") if isinstance(answer, dict) else None
        if not rows:
            raise errors.OperationFailed(error or "the target gave no answer")
        states = {str(row.get("database_name") or "").lower(): str(row.get("state") or "") for row in rows}
        records = {str(row.get("database_name") or "").lower(): row.get("restored_to") for row in rows}
    except Exception as exc:  # noqa: BLE001 - a dry run says what it could not read; it does not stop.
        return [{"source_database": source, "lands_as": lands_as, "on_target": "not read",
                 "will": f"not known - the target could not be asked: {type(exc).__name__}: {exc}"[:300]}
                for source, lands_as in names]
    # This entry's own unfinished restore (1.372): read only where the entry may not replace - with
    # overwrite_existing true a run restores over what is there, as it always has.
    unfinished: list[dict[str, Any]] = []
    if catalog is not None and not overwrite and any(state and state != "ABSENT" for state in states.values()):
        try:
            unfinished = catalog.unfinished_chain(restore_id=str(job.restore_id),
                                                  target_server_id=str(getattr(job, "target_server_id", "") or
                                                                       getattr(job, "server_id", "") or ""))
        except Exception:  # noqa: BLE001 - without the record nothing is continued; the name is held as before
            unfinished = []
    lands = []
    for source, lands_as in names:
        state = states.get(lands_as.lower()) or "ABSENT"
        item: dict[str, Any] = {"source_database": source, "lands_as": lands_as, "on_target": state}
        goes_on = None if state == "ABSENT" or overwrite else continuation(
            [row for row in unfinished if str(row.get("restored_as") or "").lower() == lands_as.lower()],
            state=state, restored_to=records.get(lands_as.lower()))
        if state == "ABSENT":
            item["will"] = "created - the target holds no database of that name"
        elif overwrite:
            item["will"] = f"REPLACES the database there now ({state}) - overwrite_existing is true"
        elif goes_on:
            item["continues"] = goes_on
            item["will"] = (f"KEPT - run {goes_on['restore_run_id']} of this entry restored it to its end "
                            f"({goes_on['after']}) before it failed elsewhere" if goes_on.get("complete") else
                            f"CONTINUED after {goes_on['file_name']} - run {goes_on['restore_run_id']} of this "
                            f"entry stopped there and left it {state}")
        else:
            item["will"] = "REFUSED"
            item["refused"] = (f"{lands_as} exists on the target ({state}) and overwrite_existing is false - "
                               "the restore stops before it restores anything"
                               + (" (it is not an unfinished restore of this entry that can be continued: no "
                                  "failed run of it recorded the backup the target applied last)"
                                  if state.upper() == "RESTORING" else ""))
        lands.append(item)
    return lands


def _refuse_a_whole_instance_replace_under_a_lockdown(job: Any, *, restore_id: str) -> None:
    """A PostgreSQL or Oracle entry that may replace its target, under a lockdown: refused before the
    copy, by the lockdown's name (0.39.0).

    Its restore lays down the whole instance, and whether the target holds data of its own is read by
    the restore step itself, after the copy - which a lockdown runs without the entry's true. The
    overwrite test on the lab host (2026-11-14, under the lockdown of 2026-11-13) copied its chain, and the
    step then answered *overwrite_existing is not true* for an entry that states true. A lockdown is
    the exception; this entry's intent is to replace, so it waits for the lockdown, said as such."""
    if (str(getattr(job, "db_type", "") or "").lower() in restore_landing.BY_DATABASE_ENGINES
            or not bool(getattr(job, "overwrite_existing", False)) or not _locked()):
        return
    raise RestoreByIdError(
        f"{restore_id}: overwrite_existing is true, so this restore replaces whatever its target instance holds "
        "- and a lockdown is on: nothing is replaced until it is turned off (/spbot_lockdown, or backup_restore "
        "lockdown). Nothing was copied or restored.")


def _refuse_a_name_already_held(job: Any, secrets: dict[str, str], *, host: dict[str, Any], data_dir: Any,
                                catalog: Any = None) -> dict[str, dict[str, Any]]:
    """Stop a SQL Server restore whose landing name the target holds, before its copy starts - and
    answer the names this run goes on from instead (1.372): ``{landing name, lower: continuation}``,
    a database this entry's own unfinished restore left RESTORING, or restored to its end.

    Nothing when the entry may replace (``overwrite_existing: true``), names no database
    (``restore_all_databases``), or is another engine - a whole-instance restore is asked by its own
    step. A target that could not be asked is not a refusal here: the step's guard still decides.
    """
    landing = getattr(job, "landing", None)
    overwrite = bool(getattr(job, "overwrite_existing", False))
    # A lockdown holds every replace (1.220): the RESTORE is then sent without REPLACE and refused by
    # the server - after the whole copy, and in words that told the entry to state the true it states
    # (the overwrite test on the lab host, 2026-11-14, under the lockdown of 2026-11-13). Asked here, first.
    locked = overwrite and _locked()
    if (str(getattr(job, "db_type", "") or "").lower() not in restore_landing.BY_DATABASE_ENGINES
            or landing is None or landing.all_databases or (overwrite and not locked)):
        return {}
    lands = _where_it_lands(job, secrets, host=host, data_dir=data_dir, files=[], catalog=catalog)
    held = [item["refused"] for item in lands if item.get("refused")]
    if held and locked:
        names = ", ".join(str(item["lands_as"]) for item in lands if item.get("refused"))
        raise RestoreByIdError(
            f"{job.restore_id}: {names} exist(s) on the target and the entry states overwrite_existing true, "
            "but a lockdown is on - nothing is replaced until it is turned off (/spbot_lockdown, or "
            "backup_restore lockdown). Nothing was copied or restored.")
    if held:
        raise RestoreByIdError(
            f"{job.restore_id}: " + "; ".join(held) + ". Nothing was copied or restored. State "
            "overwrite_existing: true on the entry when replacing it is the intent, or map the database to "
            "another name (database_mappings).")
    return {str(item["lands_as"]).lower(): item["continues"] for item in lands if item.get("continues")}


def restore_by_id(request: dict[str, Any], *, data_dir: Any = None,
                  key: str | None = None, key_base64: str | None = None,
                  on_phase: Any = None, catalog: Any = None,
                  job_run_id: int | None = None) -> dict[str, Any]:
    """Restore one configured entry through the common primitives - and say nothing.

    The steps are this module's (:func:`prepare`, :func:`copy`, :func:`metadata`, :func:`certificate`,
    :func:`plan_chain`, :func:`apply`, :func:`cleanup`); the order they run in, and every message
    about them, are the workflow's (0.40.0, 1.375 - the operator, 2026-11-16: *the workflow calls
    each step*): ``workflow.run_restore_by_id``. This is that same run with no one told - what a
    caller that only wants the answer runs. ``on_phase`` is accepted and not called: nothing under the
    workflow says a step any more.

    ``catalog`` records the copy's checksum gate (0.34.0, 1.213b); its run row is closed with the
    restore's outcome. Every restore has a row (1.214), moved through ``restoring`` and
    ``verifying_db``, with ``job_run_id`` (the scheduler's row) and the databases the check opened.
    """
    from dbabrain.backup_restore.workflow import run_restore_by_id

    return run_restore_by_id(request, data_dir=data_dir, key=key, key_base64=key_base64, catalog=catalog,
                             job_run_id=job_run_id, say=None)


def _start_unstaged_run(catalog: Any, job: Any, *, point_in_time: str,
                        job_run_id: int | None) -> int | None:
    """The run row of a restore that copies nothing - in place, the target reading the source's own
    folder (1.214). No files: nothing travelled, so there is nothing for the gate to prove; the row
    still says what ran, where, when, and whether the databases opened."""
    import socket

    from dbabrain.lib import integrity as restore_integrity
    from dbabrain.lib.version import __version__

    integrity = getattr(job, "integrity", None)
    try:
        return catalog.start_restore_run(
            action=restore_integrity.ACTION_RESTORE, restore_id=job.restore_id, job_run_id=job_run_id,
            source_server_id=job.server_id, target_server_id=job.target_server_id or job.server_id,
            target_folder=str(getattr(job, "target_backup_dir", "") or ""),
            point_in_time=point_in_time or "LATEST",
            # Nothing is copied here, so no copy is retried; the entry's own number is recorded as stated.
            copy_max_retry=integrity.copy_max_retry if integrity is not None else 0,
            phase="restoring", tier=getattr(job, "tier", None),
            node_host=socket.gethostname(), app_version=__version__)
    except Exception:  # noqa: BLE001 - the record is not the restore (copy_gate.close_restore_runs)
        return None


# --------------------------------------------------------------------------- #
# The steps - each answers, none says (0.40.0, 1.375)
# --------------------------------------------------------------------------- #
@dataclass
class Prepared:
    """One restore, resolved and held to its rules - what every step after it reads."""

    restore_id: str
    job: Any
    engine: str
    planner: Any
    secrets: dict[str, str]
    host: dict[str, Any]
    point_in_time: str
    copy_only: bool
    dry_run: bool
    skip_transfer: bool
    data_dir: Any
    key: str | None
    key_base64: str | None
    #: The source database's clock, minutes east of UTC, for an Oracle / PostgreSQL point in time
    #: (1.385) - ``None`` when there is none to read or it was not needed.
    source_clock_offset_minutes: int | None = None

    @property
    def remote(self) -> bool:
        return bool(getattr(self.job, "is_remote", False)) and not self.skip_transfer

    @property
    def from_repository(self) -> bool:
        return getattr(self.job, "restore_from", None) == "repository"


def prepare(request: dict[str, Any], *, data_dir: Any, key: str | None, key_base64: str | None,
            catalog: Any = None) -> Prepared:
    """The entry the request names, held to every rule that refuses it before anything is touched."""
    from dbabrain.backup_restore.backup import _load_secrets
    from dbabrain.backup_restore.restore_script import load_script_restores, unusable_reason

    restore_id = str(request.get("restore_id") or "").strip()
    if not restore_id:
        raise RestoreByIdError("restore_id is required.")
    point_in_time = str(request.get("point_in_time") or "").strip()
    dry_run = bool(request.get("dry_run"))
    config_path = str(request.get("config") or "") or None

    loaded = load_script_restores(config_path)
    why_not = unusable_reason(loaded, restore_id)
    if why_not:
        # An inactive entry the loader could not read: its own reason, not "no such entry".
        raise RestoreByIdError(why_not)
    jobs = [j for j in loaded if j.restore_id == restore_id]
    if not jobs:
        # An SMB entry (no `script`) had a second route here until 0.24.0 - adapted onto these
        # primitives, beside restore-workflow's. One route per entry (rules R43): it is
        # restore-workflow's, which composes its steps through the same common commands.
        from dbabrain.backup_restore.config import load_restore_configs

        smb_entries = load_restore_configs(config_path)
        smb_why_not = unusable_reason(smb_entries, restore_id)
        if smb_why_not:
            raise RestoreByIdError(smb_why_not)
        if any(c.restore_id == restore_id for c in smb_entries):
            raise RestoreByIdError(
                f"{restore_id} is an SMB entry (it declares no `script`): restore-workflow "
                f"--restore-id {restore_id} restores it - latest, or --point-in-time.")
        raise RestoreByIdError(f"No backup_restore entry found with restore_id={restore_id}.")
    job = jobs[0]
    copy_only = bool(request.get("copy_only"))
    # The refusals restore-workflow makes before it runs an entry, made here too (0.39.0, 1.361): this
    # second door ran a repository entry the workflow refuses - in place, an engine the repository does
    # not hold - and an entry stating no action.
    from dbabrain.backup_restore.workflow import _entry_action

    if getattr(job, "overwrite_existing", False) is None:
        # Loaded, and stating none (0.39.0, 1.362): the workflow's refusal, at this door too.
        from dbabrain.lib.integrity import require_overwrite_stated

        try:
            require_overwrite_stated(None, label=f"restore_id={restore_id}")
        except Exception as exc:  # noqa: BLE001 - said as this door's own error
            raise RestoreByIdError(str(exc)) from exc
    if str(getattr(job, "db_type", "") or "").lower() in restore_landing.BY_DATABASE_ENGINES:
        # Which databases, under which names (1.362) - refused here, before a file is copied for it.
        try:
            restore_landing.require_landing(getattr(job, "landing", None), label=f"restore_id={restore_id}")
        except restore_landing.LandingError as exc:
            raise RestoreByIdError(str(exc)) from exc
    try:
        # The fields that decide the run, stated (1.366) - refused here, before a file is copied.
        restore_stated.require_stated(getattr(job, "unstated", ()), label=f"restore_id={restore_id}")
    except restore_stated.StatedError as exc:
        raise RestoreByIdError(str(exc)) from exc
    if getattr(job, "restore_from", None) == "repository":
        # The loader already refuses an entry that states no restore_from or action (R50); what this door
        # skipped were the repository's own rules.
        _action, refused = _entry_action(job, copy_only=copy_only)
        if refused is not None:
            raise RestoreByIdError(str(refused))
    if copy_only:
        import dataclasses

        from dbabrain.lib.integrity import ACTION_COPY_ONLY

        if not getattr(job, "is_remote", False):
            # In place, the target reads the source's own folder through a mount: nothing travels,
            # so there is nothing to copy and nothing to prove.
            raise RestoreByIdError(f"{restore_id}: copy only needs a target_server_id other than the "
                                   "source - this entry restores in place, and nothing is copied.")
        job = dataclasses.replace(job, action=ACTION_COPY_ONLY)
    engine = str(job.db_type or "").strip().lower()
    planner = _PLANNERS.get(engine)
    if planner is None:
        raise RestoreByIdError(f"{restore_id}: db_type {engine!r} has no plan.")

    secrets = _load_secrets(data_dir=data_dir, key=key, key_base64=key_base64) \
        if job.env_secrets else {}
    host = _host_block(job, data_dir=data_dir, load_secrets=lambda: secrets or _load_secrets(
        data_dir=data_dir, key=key, key_base64=key_base64))
    assert_target_is_not_source(job, host, data_dir=data_dir)
    from dbabrain.backup_restore.config import assert_target_is_allowed

    assert_target_is_allowed(restore_id=job.restore_id, target_server_id=getattr(job, "target_server_id", ""),
                             allowed=bool(getattr(job, "allow_production_target", False)), data_dir=data_dir)
    clock = None
    if point_in_time and engine in ("oracle", "postgresql", "postgres"):
        point_in_time, clock = _moment_in_utc(job, point_in_time, engine=engine, catalog=catalog, data_dir=data_dir,
                                              key=key, key_base64=key_base64)
    return Prepared(restore_id=restore_id, job=job, engine=engine, planner=planner, secrets=secrets, host=host,
                    point_in_time=point_in_time, copy_only=copy_only, dry_run=dry_run,
                    skip_transfer=bool(request.get("skip_transfer")), data_dir=data_dir, key=key,
                    key_base64=key_base64, source_clock_offset_minutes=clock)


def _moment_in_utc(job: Any, point_in_time: str, *, engine: str, catalog: Any, data_dir: Any, key: str | None,
                   key_base64: str | None) -> tuple[str, int | None]:
    """An Oracle / PostgreSQL point in time in UTC with its offset stated, and the source's clock (1.385).

    The source database is asked its offset; an Oracle backup's recorded one stands in when it does not
    answer (a restore from the repository of a source that is gone). A moment without an offset is in
    that clock; Oracle's UNTIL TIME is read in it whatever the moment states - so either one the clock
    is needed for and could not be had refuses the restore, before a file is copied.
    """
    from dbabrain.backup_restore import source_clock
    from dbabrain.backup_restore.backup import _load_secrets
    from dbabrain.lib.restore.moment import MomentError, utc_moment_text

    try:
        secrets = _load_secrets(data_dir=data_dir, key=key, key_base64=key_base64)
    except Exception:  # noqa: BLE001 - the login is then resolved by the fill itself
        secrets = None
    clock, why = source_clock.ask(job.server_id, db_type=engine, data_dir=data_dir, secrets=secrets)
    if clock is None and engine == "oracle":
        clock = source_clock.recorded(catalog, job.server_id)
        if clock is not None:
            why = ""
    try:
        moment = utc_moment_text(point_in_time, source_offset_minutes=clock)
    except MomentError as exc:
        raise RestoreByIdError(f"{job.restore_id}: {exc}" + (f" ({why})" if why else "")) from exc
    if clock is None and engine == "oracle":
        raise RestoreByIdError(
            f"{job.restore_id}: Oracle's UNTIL TIME is read in the source's clock, and it could not be had - "
            f"{why or 'not recorded'}; no backup of {job.server_id} recorded it either (record_chain).")
    return moment, clock


def dry_run(prep: Prepared, *, catalog: Any) -> dict[str, Any]:
    """What the restore would do, read only - nothing copied, made, restored or deleted."""
    job, restore_id, engine = prep.job, prep.restore_id, prep.engine
    if prep.remote:
        # 0.39.0 (1.356): onto another machine a dry run planned from what was already staged - on a
        # target nothing was restored to yet, an empty folder - and failed there. It now reads the
        # chain where the copy would read it and asks the target, read only, whether it fits.
        from dbabrain.backup_restore.backup import resolve_ssh_target
        from dbabrain.backup_restore.restore_script import plan_transfer

        source = resolve_ssh_target(job.server_id, label=job.label, data_dir=prep.data_dir)
        target_host = resolve_ssh_target(job.target_server_id, label=job.label,
                                         data_dir=prep.data_dir, require_container=False)
        transfer_plan = plan_transfer(job, source=source, target=target_host, data_dir=prep.data_dir, key=prep.key,
                                      key_base64=prep.key_base64, point_in_time=prep.point_in_time, catalog=catalog)
        # Where it lands (0.39.0, 1.362): asked of the target, read only - a name already held there is
        # said now, as the restore's own refusal or as what it will replace.
        lands = [] if prep.copy_only else _where_it_lands(job, prep.secrets, host=prep.host, data_dir=prep.data_dir,
                                                          files=transfer_plan.get("files") or [], catalog=catalog)
        transfer_plan["would_refuse"] = list(transfer_plan.get("would_refuse") or []) + [
            item["refused"] for item in lands if item.get("refused")]
        return {"restore_id": restore_id, "db_type": engine, "dry_run": True, "copy_only": prep.copy_only,
                "point_in_time": prep.point_in_time or "LATEST", "transfer_plan": transfer_plan, "lands": lands,
                "steps": (["copy only - each file proven on the target; nothing restored"] if prep.copy_only
                          else [step for step in DRY_RUN_STEPS.get(engine, [])
                                # The vault's key only for a backup encrypted with a certificate.
                                if not step.startswith("restore-key")
                                or "BACKUP_ENCRYPTION_PASSWORD" in (job.env_secrets or {})]),
                "would_refuse": transfer_plan.get("would_refuse") or [],
                "changed": "nothing - no file copied, no folder made, nothing restored or deleted"}
    if prep.copy_only:
        return {"restore_id": restore_id, "db_type": engine, "copy_only": True, "dry_run": True,
                "transfer": None, "steps": [], "warnings": []}
    steps = prep.planner(job, prep.secrets, point_in_time=prep.point_in_time, host=prep.host, data_dir=prep.data_dir,
                         dry_run=True)
    return {"restore_id": restore_id, "db_type": engine, "dry_run": True,
            "steps": [s["op"] for s in steps if s["op"] not in ("expect", "continued")]}


def refuse_before_copy(prep: Prepared, *, catalog: Any) -> dict[str, dict[str, Any]]:
    """A name the target already holds, asked BEFORE anything is copied (0.39.0, 1.362), and a whole
    instance replace under a lockdown. Answers the databases a run goes on from, where this entry's
    own unfinished restore left them (1.372)."""
    resume = _refuse_a_name_already_held(prep.job, prep.secrets, host=prep.host, data_dir=prep.data_dir,
                                         catalog=catalog)
    _refuse_a_whole_instance_replace_under_a_lockdown(prep.job, restore_id=prep.restore_id)
    return resume


def copy(prep: Prepared, *, catalog: Any) -> tuple[dict[str, Any], Any]:
    """The chain copied onto the target and proven there (decrypted on its way from an encrypted
    repository) - ``(transferred, target_host)``. Only for a restore onto another machine."""
    from dbabrain.backup_restore.backup import resolve_ssh_target
    from dbabrain.backup_restore.restore_script import transfer_backup_to_target

    job = prep.job
    source = resolve_ssh_target(job.server_id, label=job.label, data_dir=prep.data_dir)
    target_host = resolve_ssh_target(job.target_server_id, label=job.label, data_dir=prep.data_dir,
                                     require_container=False)
    transferred = transfer_backup_to_target(job, source=source, target=target_host, data_dir=prep.data_dir,
                                            key=prep.key, key_base64=prep.key_base64, prune=False,
                                            point_in_time=prep.point_in_time, catalog=catalog)
    return transferred, target_host


def open_run(prep: Prepared, *, transferred: dict[str, Any] | None, catalog: Any, job_run_id: int | None,
             runs: list[tuple[int, bool]]) -> int | None:
    """The restore's catalog run row - the copy's, or one of its own for a restore that copies nothing."""
    from dbabrain.backup_restore.copy_gate import record_run

    run_id = (transferred or {}).get("restore_run_id")
    if run_id is not None:
        # Proven, or the transfer would have raised: closed done / error with the restore.
        runs.append((int(run_id), True))
    record_run(catalog, run_id, job_run_id=job_run_id, point_in_time=prep.point_in_time or "LATEST")
    if run_id is None and catalog is not None and not prep.copy_only:
        run_id = _start_unstaged_run(catalog, prep.job, point_in_time=prep.point_in_time, job_run_id=job_run_id)
        if run_id is not None:
            runs.append((run_id, True))
    return run_id


def metadata(prep: Prepared, phase: str) -> dict[str, Any]:
    """One instance-metadata phase around the restore - its report, never raising: the databases are
    the deliverable. ``{"status": "OFF", "reason": ...}`` when the entry restores none (an engine whose
    logins are inside the physical backup, or the entry's field off)."""
    from dbabrain.backup_restore.restore_script import replay_metadata_phase

    plan = getattr(prep.job, "server_metadata", None)
    if plan is None or not getattr(plan, "enabled", False):
        reason = ("logins, roles and grants are inside the physical backup"
                  if prep.engine in ("oracle", "postgresql", "postgres") else "server_metadata is off for this entry")
        return {"ok": True, "status": "OFF", "reason": reason}
    _line, report = replay_metadata_phase(prep.job, phase=phase, data_dir=prep.data_dir, on_phase=None)
    if report is None:
        return {"ok": True, "status": "OFF", "reason": f"the entry's server_metadata has no {phase} phase"}
    return report


def metadata_warning(report: dict[str, Any], phase: str) -> str | None:
    """A phase asked for and not replayed is a warning on a restore that worked (1.88) - never a failure."""
    if report.get("status") == "OFF" or report.get("ok", True):
        return None
    return (f"instance metadata ({phase}) was asked for and not replayed - "
            f"{report.get('error') or report.get('status') or 'see the restore events'}")


def certificate(prep: Prepared) -> dict[str, Any]:
    """The backup's certificate, from dbabrain's vault, imported on the target - before the chain is
    planned: RESTORE HEADERONLY cannot read an encrypted backup on an instance without it (Msg 33111).
    Answers ``{"status": "imported" | "none" | "not_applicable", ...}``."""
    job = prep.job
    if prep.engine != "sqlserver":
        return {"status": "not_applicable", "engine": prep.engine}
    if not job.env_secrets.get("BACKUP_ENCRYPTION_PASSWORD"):
        return {"status": "none"}
    password = _secret(job.env_secrets["BACKUP_ENCRYPTION_PASSWORD"], prep.secrets,
                       where=f"{job.restore_id}.env_secrets.BACKUP_ENCRYPTION_PASSWORD")
    name = job.env.get("BACKUP_CERT_NAME", sqlserver_certificate.DEFAULT_NAME)
    step = _key_from_vault(job, name=name, password=password, target=_sqlserver_target(job, prep.secrets,
                           host=prep.host, data_dir=prep.data_dir), host=prep.host,
                           directory=_visible_dir(job), dry_run=False)
    return {"status": "imported", "name": name, "vault_row": step.get("vault_row"), "step": step}


def plan_chain(prep: Prepared, *, resume: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """The chain to apply - each full, diff and log a step, then the check - read where it is staged.
    The certificate is :func:`certificate`'s, already imported."""
    kwargs: dict[str, Any] = {"resume": resume} if resume else {}
    if prep.engine == "sqlserver":
        kwargs["with_certificate"] = False
    if prep.engine == "oracle":
        kwargs["clock_offset_minutes"] = prep.source_clock_offset_minutes
    return prep.planner(prep.job, prep.secrets, point_in_time=prep.point_in_time, host=prep.host,
                        data_dir=prep.data_dir, dry_run=False, **kwargs)


def apply(step: dict[str, Any], *, catalog: Any, run_id: int | None, positions: list[int] | None,
          number: int | None = None, total: int = 0, restore_id: str = "") -> dict[str, Any]:
    """One step of the plan run, and the files it carries marked applied or failed in the restore's chain."""
    try:
        outcome = _execute(step["op"], step["request"])
    except Exception as exc:
        # One command carries a step's files, so the step's error is each of theirs.
        _mark_chain(catalog, run_id, positions, result="failed", error_text=f"{type(exc).__name__}: {exc}")
        if number and isinstance(exc, RestoreByIdError):
            # Which step of the chain, and which backup(s): what the restore's error says.
            raise RestoreByIdError(f"{restore_id}: step {number} of {total} failed - "
                                   f"{step_said(step)}: {exc}") from exc
        raise
    _mark_chain(catalog, run_id, positions, result="applied")
    return outcome


def step_facts(step: dict[str, Any]) -> dict[str, Any]:
    """What a chain step applies, as facts - its database, the name it lands as, its files and when they
    were taken, a STOPAT, whether it opens the database. The workflow words it."""
    request = step.get("request") or {}
    paths = [str(p) for p in (request.get("backup_paths") or
                              ([request["backup_path"]] if request.get("backup_path") else []))]
    landing = str(request.get("database_name") or "")
    return {"op": step.get("op"), "database": str(step.get("source_database") or landing),
            "landing": landing, "files": [_file_name(p) for p in paths],
            "finished": [_clock(item.get("finished_at")) for item in step.get("chain") or [] if item.get("finished_at")],
            "stopat": str(request.get("stopat") or ""), "recovered": bool(request.get("with_recovery")),
            "mode": str(request.get("mode") or ""), "wal": bool(request.get("wal_dir")),
            "summary": _plan_summary([step]), "log_number": step.get("log_number"), "log_total": step.get("log_total")}


def cleanup(prep: Prepared, *, transferred: dict[str, Any], target_host: Any, run_id: int | None,
            catalog: Any) -> dict[str, Any]:
    """Every copy this entry staged, deleted now the databases are restored and checked (1.371), and the
    staged files past retention pruned. Never turns a verified restore into a failed one: what could not
    be done comes back as ``warnings``."""
    from dbabrain.backup_restore.restore_script import (
        delete_decrypted_staging,
        prune_staged_backups,
        staged_copies_of_entry,
    )

    job = prep.job
    warnings: list[str] = []
    decrypted = list(transferred.get("files") or []) if transferred.get("encrypted") else []
    staged: list[dict[str, Any]] = []
    try:
        staged = staged_copies_of_entry(job, catalog, exclude_paths={str(one.get("path")) for one in decrypted})
    except Exception as exc:  # noqa: BLE001 - the cleanup never turns a verified restore into a failed one
        warnings.append(f"staging: the copies this entry staged could not be read from the catalog - "
                        f"{type(exc).__name__}: {exc}"[:400])
    to_delete = decrypted + staged
    plain = None
    if to_delete:
        try:
            plain = delete_decrypted_staging(job, target=target_host, files=to_delete, restore_run_id=run_id,
                                             data_dir=prep.data_dir, key=prep.key, key_base64=prep.key_base64,
                                             catalog=catalog)
        except Exception as exc:  # noqa: BLE001 - the cleanup never turns a verified restore into a failed one
            plain = {"plain_files": len(to_delete), "deleted": 0, "already_gone": 0, "bytes_deleted": 0,
                     "error": f"the staged copies could not be deleted and are still in the clear on "
                              f"{job.target_server_id} - {type(exc).__name__}: {exc}"[:400]}
        if plain.get("error"):
            warnings.append(f"staging: {plain['error']}")
        plain["earlier_runs"] = sum(1 for one in staged if one.get("restore_run_id") not in (None, run_id))
    pruned = prune_staged_backups(job, target=target_host, data_dir=prep.data_dir, key=prep.key,
                                  key_base64=prep.key_base64, catalog=catalog)
    # Said in the run's own words (0.38.0): a staged file the catalog does not know is kept and
    # named, and one that should have gone and did not is a warning on a restore that worked.
    kept = pruned.get("kept_unknown") or []
    if kept:
        names = ", ".join(str(one.get("path") or "").rsplit("/", 1)[-1] for one in kept[:3])
        warnings.append(
            f"{len(kept)} staged file(s) past retention are not in the catalog and were kept "
            f"({names}{', ...' if len(kept) > 3 else ''}) - clear one with "
            "common.cli delete-file and \"force\": true")
    if pruned.get("error"):
        warnings.append(f"staging cleanup: {pruned['error']}")
    return {"plain": plain, "pruned": pruned, "kept": len(kept), "warnings": warnings}
