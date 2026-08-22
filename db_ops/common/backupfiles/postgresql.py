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


def list_files(request: dict[str, Any]) -> list[dict[str, Any]]:
    host = parse_host(request.get("host"))
    directory = str(request.get("path") or "").strip()
    if not directory:
        raise BackupListError("path is required: the backup root holding base/ and wal/.")
    root = shlex.quote(directory.rstrip("/"))

    # One listing rather than three: the walk is the slow part over an internet hop, and the names
    # already carry everything the caller asked for. `-c` prints name|size|mtime in one pass.
    command = (
        f"for d in {root}/base/*_FULL {root}/base/*_INCR {root}/wal; do "
        f"[ -e \"$d\" ] && stat -c '%n|%s|%y' \"$d\"; done 2>/dev/null"
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
                        finished_at=modified.strip() or None))
    return rows
