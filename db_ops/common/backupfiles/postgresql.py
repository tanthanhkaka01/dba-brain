"""PostgreSQL: the layout is the answer, so it is read rather than interrogated.

``pg_basebackup`` writes one directory per backup, named here ``base/<stamp>_FULL`` and
``base/<stamp>_INCR``, with WAL segments under ``wal/``. There is no catalogue to ask and no header
to read — the directory names carry the level. This is the one engine where reading names is
correct rather than a shortcut, because the names are what the backup job wrote on purpose.

``database`` is null on every row and stays null: ``pg_basebackup`` is whole-cluster, so there is
no per-database backup to name. Inventing one would let a caller filter on something never true.
"""

from __future__ import annotations

import shlex
from typing import Any

from db_ops.common.backupfiles import DIFF, FULL, LOG, BackupListError, row
from db_ops.common.hostcmd import parse_host, run


def _utc(modified: str) -> str | None:
    """``stat -c %y`` in UTC, the clock a point in time is compared in.

    ``finished_at`` stays the host's own clock, cut to 19 characters, because retention compares it
    with the operator's wall clock (lib.backupfiles_retention). A moment, though, is converted to
    UTC, and compared against the host clock it was seven hours off on the +07 lab host, choosing
    an older full than it needed - and on a host behind UTC it would choose one that finished after
    the moment (2026-09-25).
    """
    from db_ops.lib.restore.moment import MomentError, server_clock_text

    text = str(modified or "").strip()
    head, dot, rest = text.partition(".")
    if dot and len(head) >= 19:
        text = head + rest.lstrip("0123456789")
    try:
        return server_clock_text(text) if text[19:].strip() else None
    except MomentError:
        return None


def list_files(request: dict[str, Any]) -> list[dict[str, Any]]:
    host = parse_host(request.get("host"))
    directory = str(request.get("path") or "").strip()
    if not directory:
        raise BackupListError("path is required: the backup root holding base/ and wal/.")
    root = shlex.quote(directory.rstrip("/"))

    # One listing rather than three: the walk is the slow part over an internet hop, and the names
    # already carry everything the caller asked for. One line per directory: name|size|mtime.
    #
    # The time is `backup_manifest`'s, not the directory's. pg_basebackup writes the manifest last,
    # so its mtime is when the backup finished, and the tar copy to a restore target keeps a file's
    # mtime - but a directory there is created by the copy and dated by it. A full and its
    # incremental staged in the same second then read as finished together, and the chain dropped
    # the incremental (the lab drill, 2026-09-25: `..._FULL` and `..._INCR` both "10:53:22").
    command = (
        f"for d in {root}/base/*_FULL {root}/base/*_INCR {root}/wal; do "
        f"[ -e \"$d\" ] || continue; "
        f"if [ -f \"$d/backup_manifest\" ]; then m=\"$d/backup_manifest\"; else m=\"$d\"; fi; "
        f"printf '%s|%s|%s\\n' \"$d\" \"$(stat -c %s \"$d\")\" \"$(stat -c %y \"$m\")\"; "
        f"done 2>/dev/null"
    )
    result = run(host, command, timeout=int(request.get("timeout_seconds") or 300))

    rows: list[dict[str, Any]] = []
    for line in result["stdout"].splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        path, size, modified = (line.split("|", 2) + ["", ""])[:3]
        name = path.rsplit("/", 1)[-1]
        if name == "wal":
            kind = LOG
        elif name.endswith("_FULL"):
            kind = FULL
        elif name.endswith("_INCR"):
            kind = DIFF
        else:
            continue
        rows.append(row(path=path, kind=kind, database=None,
                        size=int(size) if size.isdigit() else None,
                        finished_at=modified.strip() or None,
                        finished_at_utc=_utc(modified)))
    return rows
