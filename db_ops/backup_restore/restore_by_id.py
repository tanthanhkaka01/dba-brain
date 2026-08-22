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

import sys

import subprocess

import json

from typing import Any

from db_ops.backup_restore.events import announce
from db_ops.lib import common_cli
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


def _host_block(job: Any, *, data_dir: Any) -> dict[str, Any]:
    """Where the target database runs, in the shape :mod:`db_ops.common.hostcmd` expects.

    Derived from the entry rather than restated: ``target_server_id`` names the host and
    ``target_container`` the container on it, which is the same chain ``server_metadata`` follows.
    """
    from db_ops.backup_restore.backup import resolve_ssh_target

    if isinstance(job, _EngineJob):
        # The engine entry names its target by ip and login directly; there is no server_id to
        # resolve, which is exactly why it needed adapting rather than a lookup.
        return {"runtime": "linux", "host": job._host, "port": 22,
                "username": job._username, "sudo": True}

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
        from db_ops.common.data_sources import resolve_ssh_key

        block["key_file"] = str(resolve_ssh_key(target.key_file, data_dir))
    if job.target_container:
        block["container"] = job.target_container
    return block


class _EngineJob:
    """An engine-path entry wearing the fields the planners read.

    The engine config states its target as an ip and an import directory; a script entry states a
    host and a container. Only the strings differ, so they are translated here once instead of
    every planner learning both shapes.
    """

    def __init__(self, config: Any) -> None:
        self.restore_id = config.restore_id
        self.db_type = "sqlserver"
        self.label = f"{config.restore_id} (sqlserver)"
        self.server_id = config.source_id
        self.target_server_id = config.target_id
        self.target_container = ""
        self.target_backup_dir = str(config.vm_import_unc)
        self.target_visible_dir = str(config.vm_import_unc)
        self.backup_dir = str(config.prod_backup_share)
        self.is_remote = False          # the staging is done by the engine's own copy step
        self.env = {"MSSQL_USER": config.restore_sql_username or "sa"}
        self.env_secrets = {"MSSQL_PASSWORD": config.restore_sql_password_env}
        self._host = config.vm_credential_target
        self._username = config.vm_username
        self._password_env = config.vm_password_env


def _engine_entry_as_job(restore_id: str, config_path: Any) -> Any:
    from db_ops.backup_restore.config import load_restore_configs

    found = [c for c in load_restore_configs(config_path) if c.restore_id == restore_id]
    if not found:
        raise RestoreByIdError(f"No backup_restore entry found with restore_id={restore_id}.")
    return _EngineJob(found[0])


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


def _list_backup_files(request: dict[str, Any]) -> dict[str, Any]:
    """What is on the share, asked through the `common` CLI.

    Planning a restore is a read of somebody else's filesystem, so it crosses the same boundary
    the steps themselves do. Wrapped rather than called inline because the planners ask three or
    four times each and every one of them wants the failure as an exception, not as an empty file
    list — "no backups found" and "the listing command did not run" lead to opposite actions.
    """
    try:
        return common_cli.run("list-backup-files", request)
    except common_cli.CommonCliError as exc:
        raise RestoreByIdError(str(exc)) from exc


def _plan_sqlserver(job: Any, secrets: dict[str, str], *, point_in_time: str,
                    host: dict[str, Any], data_dir: Any) -> list[dict[str, Any]]:
    directory = _visible_dir(job)
    target = {
        "host": host["host"], "port": 1433, "username": job.env.get("MSSQL_USER", "sa"),
        "password": _secret(job.env_secrets.get("MSSQL_PASSWORD", ""), secrets,
                            where=f"{job.restore_id}.env_secrets.MSSQL_PASSWORD"),
    }
    steps: list[dict[str, Any]] = []

    cert_password = _secret(job.env_secrets.get("BACKUP_ENCRYPTION_PASSWORD", ""), secrets,
                            where=f"{job.restore_id}.env_secrets.BACKUP_ENCRYPTION_PASSWORD") \
        if job.env_secrets.get("BACKUP_ENCRYPTION_PASSWORD") else ""
    if cert_password:
        name = job.env.get("BACKUP_CERT_NAME", "db_ops_backup_cert")
        steps.append({"op": "restore-key", "request": {
            "certificate_name": name,
            "cer_path": f"{directory.rstrip('/')}/_cert/{name}.cer",
            "pvk_path": f"{directory.rstrip('/')}/_cert/{name}.pvk",
            "password": cert_password, "target": target}})

    base = {"db_type": "sqlserver", "path": directory, "target": target}
    databases = [d for d in (job.env.get("MSSQL_DATABASES", "") or "").split(",") if d.strip()]
    if not databases:
        found = _list_backup_files({**base, "kinds": ["full"]})
        databases = sorted({f["database"] for f in found["files"] if f["database"]})
    if not databases:
        raise RestoreByIdError(f"{job.restore_id}: no databases found under {directory}.")

    for database in databases:
        scoped = {**base, "database": database}
        full = _list_backup_files({**scoped, "kinds": ["full"], "latest": True,
                                  **({"before": point_in_time} if point_in_time else {})})
        if not full["files"]:
            raise RestoreByIdError(f"{job.restore_id}: no full backup for {database}.")
        anchor = full["newest_finished_at"]
        diff = _list_backup_files({**scoped, "kinds": ["diff"], "latest": True, "after": anchor,
                                  **({"before": point_in_time} if point_in_time else {})})
        anchor = diff["newest_finished_at"] or anchor
        logs = _list_backup_files({**scoped, "kinds": ["log"], "after": anchor,
                                  **({"before": point_in_time} if point_in_time else {})})

        # NORECOVERY on everything but the last step: a database recovered early cannot take the
        # rest of its chain, and the only fix is to start the whole restore again.
        tail = "log" if logs["files"] else ("diff" if diff["files"] else "full")
        steps.append({"op": "restore-full", "request": {
            "db_type": "sqlserver", "database": database, "target": target,
            "backup_path": full["files"][0]["path"], "with_recovery": tail == "full"}})
        if diff["files"]:
            steps.append({"op": "restore-diff", "request": {
                "db_type": "sqlserver", "database": database, "target": target,
                "backup_path": diff["files"][0]["path"], "with_recovery": tail == "diff"}})
        if logs["files"]:
            steps.append({"op": "restore-log", "request": {
                "db_type": "sqlserver", "database": database, "target": target,
                "backup_paths": [f["path"] for f in logs["files"]], "with_recovery": True,
                **({"stopat": point_in_time} if point_in_time else {})}})

    steps.append({"op": "verify-restore", "request": {
        "db_type": "sqlserver", "databases": databases, "target": target}})
    return steps


def _plan_oracle(job: Any, secrets: dict[str, str], *, point_in_time: str,
                 host: dict[str, Any], data_dir: Any) -> list[dict[str, Any]]:
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
            {"op": "verify-restore", "request": {"db_type": "oracle", "host": host}}]


def _plan_postgresql(job: Any, secrets: dict[str, str], *, point_in_time: str,
                     host: dict[str, Any], data_dir: Any) -> list[dict[str, Any]]:
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
    incr = _list_backup_files({**base, "kinds": ["diff"], "after": full["newest_finished_at"],
                              **({"before": point_in_time} if point_in_time else {})})
    chain = [full["files"][0]["path"]] + [f["path"] for f in incr["files"]]

    data_directory = job.env.get("PGDATA", "/var/lib/postgresql/18/docker")
    staging = job.env.get("PG_STAGING", "/var/lib/postgresql/dbops_staging")
    common = {"db_type": "postgresql", "host": host, "data_dir": data_directory,
              "staging_dir": staging}
    # Every incremental after the full, together: pg_combinebackup reads them as one, and a chain
    # missing its middle produces a data directory that starts and is incomplete.
    step = ({"op": "restore-diff", "request": {**common, "backup_paths": chain}} if len(chain) > 1
            else {"op": "restore-full", "request": {**common, "backup_path": chain[0]}})
    return [
        step,
        {"op": "restore-log", "request": {
            **common, "wal_dir": f"{directory.rstrip('/')}/wal",
            **({"stopat": point_in_time} if point_in_time else {})}},
        {"op": "verify-restore", "request": {"db_type": "postgresql", "host": host}},
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
    :mod:`db_ops.lib.common_cli` for why the transport lives app-side and why a failed
    step raises instead of coming back as data.
    """
    if op not in _STEP_COMMANDS:
        raise RestoreByIdError(f"unknown step {op!r}.")
    try:
        return common_cli.run(op, request)
    except common_cli.CommonCliError as exc:
        raise RestoreByIdError(str(exc)) from exc


# `_announce` is `db_ops.backup_restore.events.announce` since 2026-08-16 — it was four
# near-copies in this app, one of which had already drifted its parameter names.


def restore_by_id(request: dict[str, Any], *, data_dir: Any = None,
                  key: str | None = None, key_base64: str | None = None,
                  on_phase: Any = None) -> dict[str, Any]:
    """Restore one configured entry through the common primitives.

    ``on_phase(phase, message, extra)`` is called at the copy boundaries so a caller can announce
    the long half of a remote drill. It is optional: this function decides *what happened*, the
    caller decides *who hears about it*.
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
        # An engine-path entry (SMB share + sqlcmd) describes the same restore in a different
        # shape. Adapted rather than given its own planner: the work is identical once the paths
        # and the login are known, and two planners for one engine would drift.
        job = _engine_entry_as_job(restore_id, config_path)
    else:
        job = jobs[0]
    engine = str(job.db_type or "").strip().lower()
    planner = _PLANNERS.get(engine)
    if planner is None:
        raise RestoreByIdError(f"{restore_id}: db_type {engine!r} has no plan.")

    secrets = _load_secrets(data_dir=data_dir, key=key, key_base64=key_base64) \
        if job.env_secrets else {}
    host = _host_block(job, data_dir=data_dir)

    # Staging stays with the machinery that has been doing it nightly for months. Re-deriving it
    # here would repeat the mistake this module was written after: a primitive that turned out to
    # know less than the script it was meant to generalise.
    transferred = None
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
            data_dir=data_dir, key=key, key_base64=key_base64,
        )
        announce(on_phase, "COPY_DONE",
                  f"Restore {restore_id}: copy finished - {transferred['copied']} piece(s), "
                  f"{transferred['bytes_copied']} bytes, {transferred['skipped']} already present.",
                  {"copied": transferred["copied"], "skipped": transferred["skipped"],
                   "bytes_copied": transferred["bytes_copied"],
                   "pruned": transferred.get("pruned", 0)})

    steps = planner(job, secrets, point_in_time=point_in_time, host=host, data_dir=data_dir)

    if dry_run:
        return {"restore_id": restore_id, "db_type": engine, "dry_run": True,
                "steps": [s["op"] for s in steps]}

    results: list[dict[str, Any]] = []
    for step in steps:
        outcome = _execute(step["op"], step["request"])
        results.append({"step": step["op"], "ok": bool(outcome.get("ok", True))})
        if step["op"] == "verify-restore" and not outcome.get("ok"):
            # A restore that finished and left a database nobody can query is the failure the
            # whole verify step exists to catch; it must not be reported as success.
            raise RestoreByIdError(
                f"{restore_id}: restored, but verification failed - "
                f"{outcome.get('failed')} of {outcome.get('checked')} database(s) unusable.")
    return {"restore_id": restore_id, "db_type": engine, "steps": results,
            "transferred": transferred, "point_in_time": point_in_time or None}
