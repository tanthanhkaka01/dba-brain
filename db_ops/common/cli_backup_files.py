"""``list-backup-files`` and ``prune-backup-files`` — what an engine holds, and what of it is dead.

Plumbing only: :mod:`db_ops.common.backupfiles` decides, :mod:`db_ops.common.deletefiles` removes,
:mod:`db_ops.lib.response` shapes the answer, this reads argv and prints.

Two commands rather than one because listing and pruning answer to different people. A listing is
safe to run anywhere by anyone; a prune has a verdict in it, and the verdict is the thing worth
reading before anything is deleted.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

from db_ops.lib import response

_HOST_BLOCK = """\
   // Oracle / PostgreSQL - reached by running a command where the engine lives:
   "host": {"runtime": "docker",     // windows | linux | docker | k8s
            "access": "ssh",         // ssh (default) | winrm
            "host": "203.0.113.188", "username": "ubuntu",
            "key_file": "/keys/oracle-cloud.key",
            "container": "ora_dg_lab-primary",   // runtime=docker
            "pod": "...", "namespace": "...",    // runtime=k8s
            "sudo": true},

   // SQL Server - the instance is asked instead, since the files carry their own headers:
   "target": {"host": "192.0.2.249", "port": 1433,
              "username": "sa", "password": "..."}"""

LIST_USAGE = f"""\
Usage: python -m db_ops.common.cli list-backup-files '<json>'
       python -m db_ops.common.cli list-backup-files @request.json
       echo '<json>' | python -m db_ops.common.cli list-backup-files -

List the backups one engine holds, classified as full / diff / log. Reads no config: the request
says where the database runs and how to reach it.

  {{"db_type": "oracle",              // sqlserver | oracle | postgresql
   "path": "/opt/oracle/backup/dbops",   // the backup directory to report on
   "kinds": ["full"],                // WHICH LEVEL(S): full | diff | log | controlfile
                                     // one is ["full"]. Default: full+diff+log
   "database": "SALESDB_STG",            // optional; SQL Server shares one directory
                                     // between every database on the instance
   "latest": true,                   // optional; newest of each kind, per database
   "after": "2026-08-07 01:02:13",   // optional, EXCLUSIVE - "what came after the full I have"
   "before": "2026-08-07 14:00:00",  // optional, INCLUSIVE - up to and including a moment

{_HOST_BLOCK}}}

How each engine answers: SQL Server by RESTORE HEADERONLY (names are unreliable), Oracle by
asking RMAN (an RMAN directory is flat), PostgreSQL by reading the layout (the directory names
are what the backup job wrote on purpose).

data: {{"files":  [{{"path", "kind", "database", "size", "finished_at"}}, ...],
       "counts": {{"full": n, "diff": n, "log": n, "controlfile": n}},
       "newest_finished_at": "..."}}   // pass as `after` on the next call

Walking a chain is three calls, each fed by the last:
  1. {{"kinds": ["full"], "latest": true}}
  2. {{"kinds": ["diff"], "after": <newest_finished_at from 1>}}   // [] when there are none
  3. {{"kinds": ["log"],  "after": <newest_finished_at from 2, else from 1>}}
An empty `files` is a normal answer, not an error - a chain with no differentials is ordinary.
"""

PRUNE_USAGE = f"""\
Usage: python -m db_ops.common.cli prune-backup-files '<json>'|@file|-

Which backups are obsolete, and optionally remove them.

  {{"db_type": "oracle",
   "path": "/opt/oracle/backup/dbops",
   "retention_days": 14,             // DEFAULT 14
   "mode": "age",                    // DEFAULT age. age | recovery_window
   "kinds": ["full", "diff", "log"], // default: all three. controlfile is opt-in, as in list
   "database": "SALESDB_STG",            // optional; judge one database of a SQL Server directory
   "delete": false,                  // DEFAULT false - report only. true actually removes.
   "dry_run": false,                 // with delete=true: go through the motions, remove nothing

{_HOST_BLOCK}}}

  age              a file finished before (now - retention_days) is obsolete. Full, diff and log
                   alike, whatever depends on it. Predictable, and the same rule the backup
                   scripts already apply to their own directories.
  recovery_window  keep whatever is needed to restore to ANY point in the window: the anchor is
                   the newest FULL at or before the cutoff, and everything from there on stays.
                   RMAN's DELETE OBSOLETE ... RECOVERY WINDOW OF n DAYS.

The two differ only when a full is taken rarely relative to the window. Restoring to a point ten
days ago needs the FULL from before that point; under `age` it goes the moment it turns N days old
and the newer differentials that restore onto it are kept with no base. With a daily full against a
14-day window - what this estate runs - the newest full is never more than a day old and the rules
agree.

Both keep a file whose finished_at the engine could not state: "unknown age" is not "old".
`recovery_window` judges per database, because a chain belongs to one.

`delete: true` removes them one file at a time through the same path as delete-files, with the
same refusals. Without it the command only answers - and `obsolete_paths` is exactly the array
delete-files takes as `paths`, so deciding and deleting can stay two deliberate steps.

ORACLE: this removes files, not RMAN's record of them. A backup piece deleted from disk leaves
RMAN still listing it. Prefer `DELETE OBSOLETE` inside RMAN for an RMAN-managed directory; use
this to report on one, or to clean files RMAN does not own.

data: {{"mode", "retention_days", "cutoff",
       "obsolete": [{{"path", "kind", "database", "size", "finished_at", "verdict", "reason"}}, ...],
       "keep":     [ ...same shape... ],
       "obsolete_paths": [path, ...], "counts", "reclaimable_bytes",
       "deleted": {{...}}}}   // only when delete=true; the delete-files answer verbatim
"""

_COMMANDS = {"list-backup-files": LIST_USAGE, "prune-backup-files": PRUNE_USAGE}


def run(operation: str, argv: list[str], *, read_request: Any) -> int:
    usage = _COMMANDS[operation]
    if not argv or argv[0] in {"-h", "--help"}:
        print(usage, file=sys.stderr)
        return 0 if argv else 2
    if len(argv) > 1:
        return response.emit(response.fail(
            operation, f"{operation} takes one JSON payload; got {len(argv)} arguments."))

    request, code = read_request(argv[0], usage)
    if request is None:
        return code

    if operation == "prune-backup-files":
        return _prune(request)
    return _list(request)


def _list(request: dict) -> int:
    from db_ops.common.backupfiles import list_backup_files

    started = time.monotonic()
    try:
        result = list_backup_files(request)
    except Exception as exc:  # noqa: BLE001 - reported as JSON, like every command here.
        return response.emit(response.fail("list-backup-files", str(exc)))

    counts = result["counts"]
    return response.emit(response.ok(
        "list-backup-files",
        message=(f"{len(result['files'])} backup file(s): "
                 f"{counts['full']} full, {counts['diff']} diff, {counts['log']} log."),
        data=result,
        metrics={"duration_ms": int((time.monotonic() - started) * 1000),
                 "files": len(result["files"]), **counts},
    ))


def _prune(request: dict) -> int:
    from db_ops.common.backupfiles import list_backup_files
    from db_ops.lib.backupfiles_retention import DEFAULT_RETENTION_DAYS, plan_retention

    operation = "prune-backup-files"
    started = time.monotonic()
    try:
        # Listed through the same code path the listing command uses, so a caller cannot be shown
        # one set of files by `list` and have a different set judged by `prune`.
        listed = list_backup_files(request)
        plan = plan_retention(
            listed["files"],
            retention_days=request.get("retention_days", DEFAULT_RETENTION_DAYS),
            mode=request.get("mode") or "age",
        )
    except Exception as exc:  # noqa: BLE001
        return response.emit(response.fail(operation, str(exc)))

    counts = plan["counts"]
    metrics = {"duration_ms": int((time.monotonic() - started) * 1000),
               "reclaimable_bytes": plan["reclaimable_bytes"], **counts}

    if not request.get("delete"):
        return response.emit(response.ok(
            operation,
            message=(f"{counts['obsolete']} of {counts['total']} backup file(s) obsolete under a "
                     f"{plan['retention_days']}-day {plan['mode']} rule "
                     f"({_bytes(plan)}). Nothing deleted: pass delete=true."),
            data=plan, metrics=metrics))

    if not plan["obsolete_paths"]:
        return response.emit(response.ok(
            operation,
            message=f"Nothing obsolete under a {plan['retention_days']}-day {plan['mode']} rule.",
            data={**plan, "deleted": None}, metrics=metrics))

    from db_ops.common.deletefiles import delete_files

    try:
        deleted = delete_files({
            "paths": plan["obsolete_paths"],
            "host": request.get("host"),
            # The directory being pruned is the only place this may remove anything from. The
            # paths came from the listing of that directory, so the fence costs nothing and closes
            # the gap between listing and deleting if the two ever disagree.
            "must_be_under": request.get("path"),
            "dry_run": bool(request.get("dry_run")),
        })
    except Exception as exc:  # noqa: BLE001
        return response.emit(response.fail(operation, str(exc), data=plan, metrics=metrics))

    data = {**plan, "deleted": deleted}
    metrics = {**metrics, "deleted": deleted["counts"]["deleted"],
               "bytes_freed": deleted["bytes_freed"]}
    if deleted["counts"]["failed"]:
        return response.emit(response.fail(
            operation,
            f"{deleted['counts']['failed']} obsolete file(s) could not be deleted: "
            f"{'; '.join(deleted['failed'][:3])}",
            data=data, metrics=metrics))
    verb = "would be deleted" if request.get("dry_run") else "deleted"
    return response.emit(response.ok(
        operation,
        message=(f"{counts['obsolete']} obsolete file(s) {verb} "
                 f"({deleted['bytes_freed']} bytes freed), {counts['keep']} kept."),
        data=data, metrics=metrics))


def _bytes(plan: dict) -> str:
    """The reclaimable size, or an honest note that the engine did not report one.

    Oracle's listing comes from RMAN, which carries no file size, so the sum is 0 for a directory
    of gigabyte pieces. Printing "0 bytes" there is a number an operator might size a disk against.
    """
    if not plan["obsolete"]:
        return "0 bytes"
    if not plan["sizes_known"]:
        return "size not reported by this engine's listing"
    return f"{plan['reclaimable_bytes']} bytes"


