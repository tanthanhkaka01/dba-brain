"""Restoring a configured entry by ``restore_id``, through the common primitives.

This is the seam the whole layering rests on. ``db_ops/common`` reads no config and knows no
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

import posixpath
import sys
import time

import subprocess

import json

from typing import Any

from db_ops.backup_restore.events import announce
from db_ops.transport import common_cli
from db_ops.lib import instance_bundle
from db_ops.lib import response


class RestoreByIdError(ValueError):
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
    """Where the target database runs, in the shape :mod:`db_ops.common.hostcmd` expects.

    Derived from the entry rather than restated: ``target_server_id`` names the host and
    ``target_container`` the container on it, which is the same chain ``server_metadata`` follows.

    A target that logs in with a password gets it here - ``load_secrets()`` is called only then.
    The block used to carry a key file or nothing, so every PostgreSQL and Oracle restore onto a
    password-login machine failed at planning, "open_ssh_client needs either a password or a
    key_filename" (the lab drill, 2026-09-24); the key-login cloud hosts never showed it.
    """
    from db_ops.backup_restore.backup import resolve_ssh_target

    target = resolve_ssh_target(
        job.target_server_id or job.server_id, label=job.label,
        data_dir=data_dir, require_container=False,
    )
    block: dict[str, Any] = {
        "runtime": "docker" if job.target_container else "linux",
        "host": target.host, "port": target.port, "username": target.username,
        # Reaching docker on these hosts needs it, and guessing is how a restore fails with
        # "permission denied while trying to connect to the Docker daemon socket".
        "sudo": True,
    }
    if target.key_file:
        from db_ops.lib.data_sources import resolve_ssh_key

        block["key_file"] = str(resolve_ssh_key(target.key_file, data_dir))
    elif target.password_ref and load_secrets is not None:
        from db_ops.backup_restore.spec_builder import _ssh_password

        block["password"] = _ssh_password(target, load_secrets())
    if job.target_container:
        block["container"] = job.target_container
    return block


def _visible_dir(job: Any) -> str:
    """The staged backup directory **as the database sees it**.

    Not the same string as ``target_backup_dir`` when the target is a container: the files are
    staged on the host and reached through a mount, and the database resolves the path in its own
    namespace. Stated in the entry as ``target_visible_dir`` when the two differ - guessing it is
    how a restore reports "file not found" for a file that is plainly there.
    """
    return str(getattr(job, "target_visible_dir", "") or job.target_backup_dir or job.backup_dir)


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
    from db_ops.lib import data_sources

    override = str((job.env or {}).get("MSSQL_PORT") or "").strip()
    if override:
        return int(override)
    wanted =str(getattr(job, "target_server_id", "") or job.server_id or "").strip()
    for record in data_sources.load_db_instances(data_dir):
        if str(record.get("server_id") or "").strip() != wanted:
            continue
        if str(record.get("db_type") or "").strip().lower() in ("sqlserver", "mssql") and record.get("port"):
            return int(record["port"])
        break
    return 1433


def _logs_through(files: list[dict[str, Any]], moment: str) -> list[dict[str, Any]]:
    """The log backups a restore to ``moment`` needs: every one finished by then, AND the first one
    finished after it - that is the one holding the moment, and STOPAT stops inside it.

    The chain used to be "finished at or before the moment", which leaves out exactly that log: the
    restore stopped at the end of the previous one, as early as fifteen minutes before what was
    asked, and reported success (the point-in-time drill, 2026-09-25). Compared in the server's
    clock, as the listing's own window is.
    """
    from db_ops.lib.restore.moment import MomentError, server_clock_text

    def clock(text: str) -> str:
        head, dot, rest = str(text or "").strip().partition(".")
        text = head + rest.lstrip("0123456789") if dot and len(head) >= 19 else str(text or "").strip()
        try:
            return server_clock_text(text[:-1] + "+00:00" if text.endswith("Z") else text)
        except MomentError:
            return text.replace("T", " ")[:19]

    bound = clock(moment)
    kept: list[dict[str, Any]] = []
    for item in files:  # oldest first, as the listing returns them
        kept.append(item)
        if item.get("finished_at") and clock(item["finished_at"]) > bound:
            break
    return kept


def _plan_sqlserver(job: Any, secrets: dict[str, str], *, point_in_time: str,
                    host: dict[str, Any], data_dir: Any, dry_run: bool = False) -> list[dict[str, Any]]:
    _UNREADABLE.clear()  # this plan's listings only - never a previous restore's
    directory = _visible_dir(job)
    target = {
        "host": host["host"], "port": _sqlserver_port(job, data_dir=data_dir),
        "username": job.env.get("MSSQL_USER", "sa"),
        "password": _secret(job.env_secrets.get("MSSQL_PASSWORD", ""), secrets,
                            where=f"{job.restore_id}.env_secrets.MSSQL_PASSWORD"),
    }
    steps: list[dict[str, Any]] = []

    cert_password = _secret(job.env_secrets.get("BACKUP_ENCRYPTION_PASSWORD", ""), secrets,
                            where=f"{job.restore_id}.env_secrets.BACKUP_ENCRYPTION_PASSWORD") \
        if job.env_secrets.get("BACKUP_ENCRYPTION_PASSWORD") else ""
    if cert_password:
        name = job.env.get("BACKUP_CERT_NAME", "db_ops_backup_cert")
        key_step = {"op": "restore-key", "request": {
            "certificate_name": name,
            "cer_path": f"{directory.rstrip('/')}/_cert/{name}.cer",
            "pvk_path": f"{directory.rstrip('/')}/_cert/{name}.pvk",
            "password": cert_password, "target": target}}
        # Imported NOW, before the listing below: RESTORE HEADERONLY cannot read an encrypted
        # backup on an instance without its certificate (Msg 33111), so a key step queued behind
        # the listing never ran on a fresh target - the listing failed first. The import drops
        # and recreates the certificate, so doing it on every run is safe. A dry run changes
        # nothing, so there it stays a planned step - and its listing may fail for this reason.
        if not dry_run:
            _execute("restore-key", key_step["request"])
            key_step["done"] = True
        steps.append(key_step)

    base = {"db_type": "sqlserver", "path": directory, "target": target}
    databases = [d for d in (job.env.get("MSSQL_DATABASES", "") or "").split(",") if d.strip()]
    if not databases:
        found = _list_backup_files({**base, "kinds": ["full"]})
        databases = sorted({f["database_name"] for f in found["files"] if f["database_name"]})
    if not databases:
        raise RestoreByIdError(f"{job.restore_id}: no databases found under {directory}.")

    for database in databases:
        scoped = {**base, "database_name": database}
        full = _list_backup_files({**scoped, "kinds": ["full"], "latest": True,
                                  **({"before": point_in_time} if point_in_time else {})})
        if not full["files"]:
            raise RestoreByIdError(f"{job.restore_id}: no full backup for {database}.")
        anchor = full["newest_finished_at"]
        diff = _list_backup_files({**scoped, "kinds": ["diff"], "latest": True, "after": anchor,
                                  **({"before": point_in_time} if point_in_time else {})})
        anchor = diff["newest_finished_at"] or anchor
        logs = _list_backup_files({**scoped, "kinds": ["log"], "after": anchor})
        if point_in_time:
            logs = {**logs, "files": _logs_through(logs["files"], point_in_time)}

        # NORECOVERY on everything but the last step: a database recovered early cannot take the
        # rest of its chain, and the only fix is to start the whole restore again.
        tail = "log" if logs["files"] else ("diff" if diff["files"] else "full")
        steps.append({"op": "restore-full", "request": {
            "db_type": "sqlserver", "database_name": database, "target": target,
            "backup_path": full["files"][0]["path"], "with_recovery": tail == "full"}})
        if diff["files"]:
            steps.append({"op": "restore-diff", "request": {
                "db_type": "sqlserver", "database_name": database, "target": target,
                "backup_path": diff["files"][0]["path"], "with_recovery": tail == "diff"}})
        if logs["files"]:
            steps.append({"op": "restore-log", "request": {
                "db_type": "sqlserver", "database_name": database, "target": target,
                "backup_paths": [f["path"] for f in logs["files"]], "with_recovery": True,
                **({"stopat": point_in_time} if point_in_time else {})}})

    steps.append({"op": "verify-restore", "request": {
        "db_type": "sqlserver", "database_names": databases, "target": target}})
    if _UNREADABLE:
        steps.append({"op": "warning", "warning": (
            f"{len(_UNREADABLE)} file(s) named like backups could not be read as backups and were "
            f"passed over - the restore used the newest chain it could read: "
            f"{', '.join(sorted(_UNREADABLE)[:5])}")})
    return steps


def _plan_oracle(job: Any, secrets: dict[str, str], *, point_in_time: str,
                 host: dict[str, Any], data_dir: Any, dry_run: bool = False) -> list[dict[str, Any]]:
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
        request["stopat"] = point_in_time
    # One DUPLICATE and nothing else: RMAN picks the level 0, the incrementals and the archived
    # logs itself, so a diff or log step here would describe work it did not do.
    return [{"op": "restore-full", "request": request},
            # The SID goes to the check as well as to the restore. Without it `sqlplus / as sysdba`
            # takes whatever ORACLE_SID the shell carries, which is right for a container holding
            # one instance and wrong for a host holding two - and a check that answers for the
            # wrong instance under the right name is worse than no check.
            {"op": "verify-restore", "request": {"db_type": "oracle", "host": host,
                                                 "oracle_sid": request["oracle_sid"]}}]


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
    chain = [full["files"][0]["path"]] + sorted(
        (f["path"] for f in incr["files"]
         if posixpath.basename(f["path"].rstrip("/")) > full_name),
        key=lambda p: posixpath.basename(p.rstrip("/")))

    data_directory = job.env.get("PGDATA", "/var/lib/postgresql/18/docker")
    staging = job.env.get("PG_STAGING", "/var/lib/postgresql/dbops_staging")
    common = {"db_type": "postgresql", "host": host, "data_dir": data_directory,
              "staging_dir": staging}
    # Every incremental after the full, together: pg_combinebackup reads them as one, and a chain
    # missing its middle produces a data directory that starts and is incomplete.
    step = ({"op": "restore-diff", "request": {**common, "backup_paths": chain}} if len(chain) > 1
            else {"op": "restore-full", "request": {**common, "backup_path": chain[0]}})
    recovery = {"wal_dir": f"{directory.rstrip('/')}/wal",
                **({"stopat": point_in_time} if point_in_time else {})}
    if host.get("runtime") == "docker":
        # Into a container the recovery configuration travels WITH the combine, written before the
        # first start. As a separate step after it, it reached a server that had already started
        # and replayed no WAL at all (the lab drill, 2026-09-24).
        step["request"].update(recovery)
        replay: list[dict[str, Any]] = []
    else:
        replay = [{"op": "restore-log", "request": {**common, **recovery}}]
    return [
        step,
        *replay,
        # PGPORT when the job states one: psql with no -p takes the default cluster, which is right
        # inside a container and wrong on a host running two.
        {"op": "verify-restore", "request": {
            "db_type": "postgresql", "host": host,
            **({"port": job.env["PGPORT"]} if job.env.get("PGPORT") else {})}},
    ]


_PLANNERS = {"sqlserver": _plan_sqlserver, "oracle": _plan_oracle,
             "postgresql": _plan_postgresql, "postgres": _plan_postgresql}


#: The step names are the `common` CLI command names. That was already true when these ran
#: in-process; since 2026-08-15 it is also how they are invoked.
_STEP_COMMANDS = frozenset({"restore-key", "restore-full", "restore-diff", "restore-log",
                            "verify-restore"})


def _execute(op: str, request: dict[str, Any]) -> dict[str, Any]:
    """Run one `common` primitive through its CLI and return the ``data`` it answered with.

    The step names *are* the CLI command names — that was already true when these ran in-process,
    and since 2026-08-15 it is also how they are invoked. See
    :mod:`db_ops.transport.common_cli` for why the transport lives app-side and why a failed
    step raises instead of coming back as data.
    """
    if op not in _STEP_COMMANDS:
        raise RestoreByIdError(f"unknown step {op!r}.")
    try:
        return common_cli.run(op, request)
    except common_cli.CommonCliError as exc:
        raise RestoreByIdError(str(exc)) from exc


def _metadata_phase(job: Any, phase: str, *, data_dir: Any, on_phase: Any) -> None:
    """One instance-metadata phase around the restore, announced either way.

    An entry without ``server_metadata`` is told so once, on the first phase - its stdout gains
    nothing (an entry that never asked must not), but a Telegram reader following the steps
    otherwise cannot tell "not configured" from "forgotten".
    """
    from db_ops.backup_restore.restore_script import replay_metadata_phase

    plan = getattr(job, "server_metadata", None)
    if plan is None or not getattr(plan, "enabled", False):
        if phase == instance_bundle.PRE_DATABASE:
            engine = str(getattr(job, "db_type", "") or "").lower()
            reason = ("not needed - logins, roles and grants are inside the physical backup"
                      if engine in ("oracle", "postgresql")
                      else "not replayed - server_metadata is off for this entry")
            announce(on_phase, "METADATA_SKIP",
                     f"Restore {job.restore_id}: instance metadata {reason}.")
        return
    replay_metadata_phase(job, phase=phase, data_dir=data_dir, on_phase=on_phase)


def _plan_summary(steps: list[dict[str, Any]]) -> str:
    """What a plan restores, in the words a Telegram reader needs: which backups, how many.

    Read off the plan itself rather than written per engine, so it cannot describe work the plan
    does not do.
    """
    parts: list[str] = []
    by_database: dict[str, list[str]] = {}
    for step in steps:
        op, req = step.get("op"), step.get("request") or {}
        paths = [str(p) for p in (req.get("backup_paths") or
                                  ([req["backup_path"]] if req.get("backup_path") else []))]
        names = [posixpath.basename(p.rstrip("/")) or p for p in paths]
        if op == "restore-key":
            parts.append("the backup certificate")
        elif op == "restore-full" and req.get("mode") == "duplicate":
            parts.append(f"RMAN DUPLICATE of {req.get('oracle_sid') or 'the instance'} from "
                         f"{req.get('backup_location') or (paths[0] if paths else '?')} (level 0, "
                         "incrementals and archived logs as RMAN picks them)")
        elif req.get("database_name"):
            # SQL Server: one chain per database.
            words = {"restore-full": f"full {names[0] if names else ''}".strip(),
                     "restore-diff": f"diff {names[0] if names else ''}".strip(),
                     "restore-log": f"{len(paths)} log(s)"}
            if op in words:
                by_database.setdefault(str(req["database_name"]), []).append(words[op])
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


# `_announce` is `db_ops.backup_restore.events.announce` since 2026-08-16 — it was four
# near-copies in this app, one of which had already drifted its parameter names.


def restore_by_id(request: dict[str, Any], *, data_dir: Any = None,
                  key: str | None = None, key_base64: str | None = None,
                  on_phase: Any = None) -> dict[str, Any]:
    """Restore one configured entry through the common primitives.

    ``on_phase(phase, message, extra)`` is called at the copy boundaries (COPY_START/COPY_DONE),
    around the restore (RESTORE_START/RESTORE_DONE) and around the check (VERIFY_START/VERIFY_DONE),
    so a caller can announce every long stretch of a drill. It is optional: this function decides
    *what happened*, the caller decides *who hears about it*.
    """
    from db_ops.backup_restore.backup import _load_secrets
    from db_ops.backup_restore.restore_script import load_script_restores

    restore_id = str(request.get("restore_id") or "").strip()
    if not restore_id:
        raise RestoreByIdError("restore_id is required.")
    point_in_time = str(request.get("point_in_time") or "").strip()
    dry_run = bool(request.get("dry_run"))
    config_path = str(request.get("config") or "") or None

    jobs = [j for j in load_script_restores(config_path) if j.restore_id == restore_id]
    if not jobs:
        # An SMB entry (no `script`) had a second route here until 0.24.0 - adapted onto these
        # primitives, beside restore-workflow's. One route per entry (rules R43): it is
        # restore-workflow's, which composes its steps through the same common commands.
        from db_ops.backup_restore.config import load_restore_configs

        if any(c.restore_id == restore_id for c in load_restore_configs(config_path)):
            raise RestoreByIdError(
                f"{restore_id} is an SMB entry (it declares no `script`): restore-workflow "
                f"--restore-id {restore_id} restores it - latest, or --point-in-time.")
        raise RestoreByIdError(f"No backup_restore entry found with restore_id={restore_id}.")
    job = jobs[0]
    engine = str(job.db_type or "").strip().lower()
    planner = _PLANNERS.get(engine)
    if planner is None:
        raise RestoreByIdError(f"{restore_id}: db_type {engine!r} has no plan.")

    secrets = _load_secrets(data_dir=data_dir, key=key, key_base64=key_base64) \
        if job.env_secrets else {}
    host = _host_block(job, data_dir=data_dir, load_secrets=lambda: secrets or _load_secrets(
        data_dir=data_dir, key=key, key_base64=key_base64))

    # Staging stays with the machinery that has been doing it nightly for months. Re-deriving it
    # here would repeat the mistake this module was written after: a primitive that turned out to
    # know less than the script it was meant to generalise.
    transferred = None
    target_host = None
    if getattr(job, "is_remote", False) and not bool(request.get("skip_transfer")) and not dry_run:
        from db_ops.backup_restore.backup import resolve_ssh_target
        from db_ops.backup_restore.restore_script import transfer_backup_to_target

        source = resolve_ssh_target(job.server_id, label=job.label, data_dir=data_dir)
        target_host = resolve_ssh_target(job.target_server_id, label=job.label,
                                         data_dir=data_dir, require_container=False)
        # The copy is the long half of a remote drill - 5 GB between two cloud regions took 41
        # minutes on 2026-08-08 - and between START and END the run said nothing at all, so "still
        # copying" and "hung" looked identical. run_script_restore has announced these two for
        # months; when the scheduled restore moved onto this function in 2.69.52 those events were
        # left behind with it, and the silence came back without anyone changing the reporting.
        announce(on_phase, "COPY_START",
                  f"Restore {restore_id}: copy started {job.server_id} -> {job.target_server_id}.")
        transferred = transfer_backup_to_target(
            job, source=source, target=target_host,
            data_dir=data_dir, key=key, key_base64=key_base64, prune=False,
            point_in_time=point_in_time,
        )
        announce(on_phase, "COPY_DONE",
                  f"Restore {restore_id}: copy finished - {transferred['copied']} piece(s), "
                  f"{transferred['bytes_copied']} bytes, {transferred['skipped']} already present"
                  + (f", {transferred['removed_absent_at_source']} removed (gone at the source)"
                     if transferred.get("removed_absent_at_source") else "")
                  + ("" if transferred.get("include") else " (the whole directory)") + ".",
                  {"copied": transferred["copied"], "skipped": transferred["skipped"],
                   "bytes_copied": transferred["bytes_copied"],
                   "removed_absent_at_source": transferred.get("removed_absent_at_source", 0)})

    steps = planner(job, secrets, point_in_time=point_in_time, host=host, data_dir=data_dir,
                    dry_run=dry_run)

    if dry_run:
        return {"restore_id": restore_id, "db_type": engine, "dry_run": True,
                "steps": [s["op"] for s in steps]}

    # The restore and its check are announced as well as the copy. After COPY_DONE a drill said
    # nothing until END - an Oracle DUPLICATE is minutes, a large one hours - and END said only
    # `status=done`: not which backups went in, not whether the databases then opened. The
    # SQL Server engine path has said VERIFY_START / VERIFY_DONE since 2026-09-15; this path, which
    # runs PostgreSQL, Oracle and container SQL Server, never did (asked on 2026-09-25, reading a
    # PostgreSQL drill in Telegram: "only copy start/done - where is the restore, the verify?").
    # Instance metadata (logins, roles, Agent jobs) before the databases and after them - the
    # order the engine path and the old script runner keep. When the move onto this function
    # (2.69.52) took the copy but left its events behind, it left this behind too: a container
    # SQL Server entry with `server_metadata` on restored its databases and never its logins.
    _metadata_phase(job, instance_bundle.PRE_DATABASE, data_dir=data_dir, on_phase=on_phase)

    plan = _plan_summary(steps)
    moment = f"to {point_in_time}" if point_in_time else "to the newest backup"
    restoring = [s for s in steps
                 if s.get("request") and s["op"] != "verify-restore" and not s.get("done")]
    if restoring:
        announce(on_phase, "RESTORE_START", f"Restore {restore_id}: restoring {plan}, {moment}.",
                 {"plan": plan})
    restore_started = time.monotonic()
    restore_done_said = not restoring

    def _restore_done() -> None:
        nonlocal restore_done_said
        if not restore_done_said:
            restore_done_said = True
            seconds = int(time.monotonic() - restore_started)
            announce(on_phase, "RESTORE_DONE",
                     f"Restore {restore_id}: {len(restoring)} restore step(s) finished in {seconds} s.",
                     {"steps": len(restoring), "restore_seconds": seconds})

    results: list[dict[str, Any]] = []
    warnings: list[str] = []
    verify: dict[str, Any] | None = None
    for step in steps:
        if step.get("warning"):
            warnings.append(step["warning"])
            continue
        if step.get("done"):
            # Run while planning (the certificate, which the listing needed); recorded, not repeated.
            results.append({"step": step["op"], "ok": True})
            continue
        if step["op"] == "verify-restore":
            _restore_done()
            announce(on_phase, "VERIFY_START",
                     f"Restore {restore_id}: checking the restored database(s) can be opened.")
        outcome = _execute(step["op"], step["request"])
        results.append({"step": step["op"], "ok": bool(outcome.get("ok", True))})
        if step["op"] == "verify-restore":
            verify = {"checked": outcome.get("checked"), "failed": outcome.get("failed")}
            announce(on_phase, "VERIFY_DONE",
                     f"Restore {restore_id}: {outcome.get('checked')} database(s) checked, "
                     f"{outcome.get('failed')} unusable.", verify)
            if not outcome.get("ok"):
                # A restore that finished and left a database nobody can query is the failure the
                # whole verify step exists to catch; it must not be reported as success.
                raise RestoreByIdError(
                    f"{restore_id}: restored, but verification failed - "
                    f"{outcome.get('failed')} of {outcome.get('checked')} database(s) unusable.")
    _restore_done()
    # Only after a restore that worked: Agent job steps name databases that have to exist.
    _metadata_phase(job, instance_bundle.POST_DATABASE, data_dir=data_dir, on_phase=on_phase)

    pruned = None
    if transferred is not None and target_host is not None:
        from db_ops.backup_restore.restore_script import prune_staged_backups

        announce(on_phase, "DELETE_START",
                 f"Restore {restore_id}: removing staged backups older than the entry's "
                 f"retention ({int(job.cleanup_retention or 0)} s) from {job.target_backup_dir}.")
        pruned = prune_staged_backups(job, target=target_host, data_dir=data_dir,
                                      key=key, key_base64=key_base64)
        announce(on_phase, "DELETE_DONE",
                 f"Restore {restore_id}: {pruned.get('pruned', 0)} staged file(s) removed"
                 + (f" ({pruned['skipped']})" if pruned.get("skipped") else "") + ".",
                 {"pruned": pruned.get("pruned", 0),
                  "retention_seconds": pruned.get("retention_seconds", 0)})
    return {"restore_id": restore_id, "db_type": engine, "steps": results,
            "transferred": transferred, "point_in_time": point_in_time or None,
            "warnings": warnings, "plan": plan, "verify": verify, "pruned": pruned}
