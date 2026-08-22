"""Oracle: ask RMAN, never the file names.

An RMAN directory is flat — level 0, level 1, archivelogs and controlfile autobackups all sit side
by side under generated names — so which piece is which is a property of the catalogue, not of the
string. ``LIST BACKUP`` states it.

**Classification is per backup set, decided after the whole set has been read.** That is not
fussiness: in real ``LIST BACKUP`` output the ``Piece Name:`` line comes *before* the
``List of Archived Logs in backup set`` line that identifies the set as archivelogs. A first cut
flipped a flag when it saw that marker and so classified every archivelog piece as whatever the
previous set was — measured on the CLOUD lab as 810 "full" pieces where there is one level 0.
Reading the set, then deciding, is the only order that works.

Mapping to the three levels: a set containing archived logs is a **log**; otherwise ``Incr`` at
level 1 is a **diff**, and anything else (``Incr 0``, ``Full``) is a **full** — a full and a
level 0 are the same thing to a restore.
"""

from __future__ import annotations

import re
import shlex
from typing import Any

from db_ops.common.backupfiles import CONTROLFILE, DIFF, FULL, LOG, BackupListError, row
from db_ops.common.hostcmd import parse_host, run

_LIST = "LIST BACKUP;\nEXIT;\n"

#: RMAN prints Completion Time in the session's NLS date format, which defaults to ``06-AUG-26`` —
#: a date with no clock. That cannot order two backups taken on the same day, which is exactly what
#: ``after`` and ``latest`` have to do, so the format is set for the session rather than guessed at
#: from a coarser string.
_NLS = "NLS_DATE_FORMAT='YYYY-MM-DD HH24:MI:SS'"

#: The header of a datafile/incremental set carries the level:
#:   BS Key  Type LV Size       Device Type Elapsed Time Completion Time
#:   3555    Incr 0  1.20G      DISK        00:00:45     06-AUG-26
_DATA_SET = re.compile(
    r"^\s*(?P<key>\d+)\s+(?P<type>Incr|Full)\s+(?P<lv>\d+)\s+\S+\s+\S+\s+\S+\s+"
    r"(?P<done>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", re.IGNORECASE)
#: An archivelog set's header has no Type/LV column at all — only a size.
_ANY_SET = re.compile(r"^\s*(?P<key>\d+)\s+\S")
_ANY_DONE = re.compile(r"(?P<done>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
_ARCHIVE_MARKER = re.compile(r"List of Archived Logs", re.IGNORECASE)
#: A controlfile/spfile autobackup set. Written every 15 minutes on this estate, so
#: calling it a full swamps the answer with pieces no restore would ever start from.
_CONTROLFILE_MARKER = re.compile(r"(Control File Included|SPFILE Included)", re.IGNORECASE)
_PIECE = re.compile(r"^\s*Piece Name:\s*(?P<path>.+?)\s*$")


class _Set:
    """One backup set being read: its pieces, and what it turns out to be."""

    def __init__(self) -> None:
        self.pieces: list[str] = []
        self.kind = FULL
        self.completed = ""


def list_files(request: dict[str, Any]) -> list[dict[str, Any]]:
    host = parse_host(request.get("host"))
    directory = str(request.get("path") or "").strip()
    if not directory:
        raise BackupListError("path is required: the RMAN backup directory to report on.")

    result = run(host,
                 f"export {_NLS}; printf {shlex.quote(_LIST)} | rman target / log /dev/stdout 2>&1",
                 timeout=int(request.get("timeout_seconds") or 600))
    text = result["stdout"]
    if "Piece Name" not in text:
        raise BackupListError(f"rman refused or reported nothing: {text.strip()[-400:]}")

    prefix = directory.rstrip("/") + "/"
    sets: list[_Set] = []
    current: _Set | None = None

    for line in text.splitlines():
        data_match = _DATA_SET.match(line)
        if data_match:
            current = _Set()
            current.kind = DIFF if data_match.group("lv") == "1" else FULL
            current.completed = data_match.group("done") or ""
            sets.append(current)
            continue
        if _ANY_SET.match(line) and "Piece Name" not in line and ":" not in line.split()[0]:
            # A set header with no Type column — an archivelog or controlfile set. Its kind is
            # settled by the marker further down, so it starts as LOG only once that appears.
            current = _Set()
            done = _ANY_DONE.search(line)
            current.completed = done.group("done") if done else ""
            sets.append(current)
            continue
        if _ARCHIVE_MARKER.search(line) and current is not None:
            current.kind = LOG
            continue
        if _CONTROLFILE_MARKER.search(line) and current is not None:
            current.kind = CONTROLFILE
            continue
        piece = _PIECE.match(line)
        if piece and current is not None:
            current.pieces.append(piece.group("path"))

    rows: list[dict[str, Any]] = []
    for item in sets:
        for path in item.pieces:
            if not path.startswith(prefix):
                # A piece elsewhere on the host (an FRA copy) has no counterpart in the directory
                # being reported on, and offering it would hand a caller a path it cannot use.
                continue
            rows.append(row(path=path, kind=item.kind, database=None,
                            finished_at=item.completed or None))
    return rows
