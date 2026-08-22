"""Reading a db_ops log file from the end, a page at a time.

The console shows the running log, and the only useful order for a log you are watching is
**newest first** — the line you want is the one that was just written, not the one from three
weeks ago. Reading the file forwards to get there is what makes a log viewer unusable: the
metrics log on the worker is tens of megabytes by the end of a month, and the first hundred lines
of it are of no interest to anyone.

So this seeks backwards. :func:`read_tail` reads a chunk from the end of the file, splits it into
lines, and returns the newest ``limit`` of them plus the byte offset to continue from. The next
page passes that offset back and gets the hundred before it. Memory is bounded by the chunk, not
by the file, so a 400 MB log costs the same as a 4 KB one.

Two details are decisions rather than defaults:

* **The offset is a byte position, not a line number.** Lines are being appended while the
  operator scrolls; a line-numbered cursor counted from the start would shift under them, and one
  counted from the end would skip or repeat rows every time a new line arrived. A byte offset names
  a fixed place in the file.
* **A partial first line is discarded.** A chunk boundary lands mid-line almost every time, and
  half a log line rendered as a row is worse than one fewer row — except at the very start of the
  file, where the "partial" line is genuinely the first line.

Parsing is deliberately forgiving, and has to be, because a db_ops log has **two** line shapes in
it — ``timestamp|LEVEL|app|host|function|message`` and the same without the function, which is
what a plain ``log_event`` writes. In ``metrics.log`` they alternate line by line. On top of that
the ``*_runtime.log`` files are raw stdout, and a traceback is not pipe-delimited anything. A line
that matches nothing is returned whole as its message, because the reason someone is reading a log
is usually the line that does not fit. See :func:`parse_line`.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: How much to read per step. Large enough that 100 lines almost always arrive in one read
#: (db_ops log lines average ~120 bytes), small enough to be irrelevant to memory.
CHUNK_BYTES = 64 * 1024

#: The delimiter db_ops writes its structured logs with.
FIELD_SEPARATOR = "|"

#: The fields before the free text: time, level, app, host. Everything after them is either
#: ``function|message`` or just ``message`` — see :func:`parse_line`.
_FIXED_FIELDS = 4

#: What a function name looks like: ``metrics.collect``, ``webhost.serve``. Used to tell a real
#: function field from the first fragment of a message that happens to contain a pipe.
_FUNCTION_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,79}$")


@dataclass(frozen=True)
class LogLine:
    """One line, parsed as far as it parses."""

    text: str
    timestamp: str = ""
    level: str = ""
    app: str = ""
    host: str = ""
    function: str = ""
    message: str = ""

    @property
    def structured(self) -> bool:
        return bool(self.timestamp)

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text, "timestamp": self.timestamp, "level": self.level,
            "app": self.app, "host": self.host, "function": self.function,
            "message": self.message, "structured": self.structured,
        }


def parse_line(text: str) -> LogLine:
    """Split one log line into its fields, or keep it whole when it is not one of ours.

    **The function field is optional, and that is the whole subtlety here.** The header says
    ``DATE|LOGTYPE|APP|HOST|FUNCTION|TEXT``, but only the calls that go through
    :func:`db_ops.logging_ops.formatter.format_function_message` emit a function; a plain
    ``log_event`` writes the message straight into the fifth field. Both are everywhere in the
    same file — in ``metrics.log`` they alternate line by line — so a parser that insisted on six
    fields read half the log as unstructured text. It did, until this was fixed.

    So the fifth field is treated as a function only when it *looks* like one: an identifier path
    with no spaces. A message containing a pipe ("dsn=a|b") does not, and stays a message.

    ``maxsplit`` matters for the same reason: a message legitimately contains pipes, and splitting
    on all of them would scatter it across fields that then read as a corrupt log.
    """
    raw = text.rstrip("\r\n")
    parts = raw.split(FIELD_SEPARATOR, _FIXED_FIELDS + 1)
    if len(parts) < _FIXED_FIELDS + 1 or not _looks_like_timestamp(parts[0]):
        return LogLine(text=raw, message=raw)

    timestamp, level, app, host = parts[:_FIXED_FIELDS]
    rest = parts[_FIXED_FIELDS:]
    if len(rest) == 2 and _FUNCTION_NAME.match(rest[0].strip()):
        function, message = rest[0], rest[1]
    else:
        function, message = "", FIELD_SEPARATOR.join(rest)
    return LogLine(text=raw, timestamp=timestamp.strip(), level=level.strip().upper(),
                   app=app.strip(), host=host.strip(), function=function.strip(),
                   message=message.strip())


def _looks_like_timestamp(value: str) -> bool:
    """``YYYY-MM-DD HH:MM:SS`` at the start of the field, cheaply.

    Checked by shape rather than parsed: this runs on every line of every page, and a stdout line
    that merely happens to start with digits must not become a structured row.
    """
    text = value.strip()
    if len(text) < 19:
        return False
    return (text[4] == "-" and text[7] == "-" and text[10] == " "
            and text[13] == ":" and text[16] == ":"
            and text[:4].isdigit() and text[5:7].isdigit() and text[8:10].isdigit())


def read_tail(path: str | Path, *, limit: int = 100, before: int | None = None,
              chunk_bytes: int = CHUNK_BYTES) -> dict[str, Any]:
    """The newest ``limit`` lines ending at byte ``before``, newest first.

    ``before`` is the offset a previous call returned as ``next_before``; omit it for the end of
    the file. ``next_before`` comes back as ``None`` once the start of the file is reached, which
    is how the caller knows to stop asking.
    """
    log_path = Path(path)
    if not log_path.is_file():
        return {"lines": [], "next_before": None, "size": 0, "exhausted": True}

    size = log_path.stat().st_size
    end = size if before is None else max(0, min(int(before), size))
    if end <= 0:
        return {"lines": [], "next_before": None, "size": size, "exhausted": True}

    # Whether the region ends on a line terminator. The last line of a file often has none, and
    # the byte arithmetic below counts one newline per line; without this the next page would start
    # one byte inside a line and hand back a row missing its last character.
    with log_path.open("rb") as probe:
        probe.seek(end - 1, os.SEEK_SET)
        region_ends_with_newline = probe.read(1) == b"\n"

    collected: list[str] = []
    # The incomplete line at the front of what has been read. It is carried across steps rather
    # than re-read: an earlier version pushed the cursor back over it, which made no progress at
    # all whenever a chunk was shorter than one line, and the read spun forever.
    fragment = ""
    position = end
    with log_path.open("rb") as handle:
        while position > 0 and len(collected) < limit:
            read_size = min(chunk_bytes, position)
            position -= read_size
            handle.seek(position, os.SEEK_SET)
            chunk = handle.read(read_size).decode("utf-8", errors="replace")
            parts = (chunk + fragment).split("\n")
            # Away from the start of the file the first part is half a line whose beginning is in
            # the chunk below; at the start it is the file's first line and is complete.
            fragment = parts.pop(0) if position > 0 else ""
            # Newest first, and blank lines dropped: a trailing newline would otherwise show as an
            # empty row at the top of every page.
            collected.extend(line for line in reversed(parts) if line.strip())

    lines = collected[:limit]
    # Where the next page ends: the first byte of the oldest line handed back. Counted from the
    # bytes rather than taken from the loop's cursor, because the loop reads past ``limit`` and
    # its cursor is a chunk boundary rather than a line boundary.
    consumed = sum(len(line.encode("utf-8")) + 1 for line in lines)
    if lines and not region_ends_with_newline:
        consumed -= 1
    next_before = max(0, end - consumed)
    return {
        "lines": [parse_line(line) for line in lines],
        "next_before": next_before if next_before > 0 and len(lines) == limit else None,
        "size": size,
        "exhausted": next_before <= 0 or len(lines) < limit,
    }


def list_logs(log_dir: str | Path) -> list[dict[str, Any]]:
    """The current log files in a directory, biggest activity first.

    Dated archives (``metrics_20260819.log``) are left out: the console is for watching what is
    happening now, and listing thirty rotations of nineteen apps would bury the nineteen files
    anybody wants. They stay on disk and stay readable by name.
    """
    directory = Path(log_dir)
    if not directory.is_dir():
        return []
    found: list[dict[str, Any]] = []
    for path in directory.glob("*.log"):
        if not path.is_file() or _is_dated(path.stem):
            continue
        stat = path.stat()
        found.append({
            "name": path.name,
            "app": path.stem.replace("_runtime", ""),
            "runtime": path.stem.endswith("_runtime"),
            "size": stat.st_size,
            "modified": stat.st_mtime,
        })
    found.sort(key=lambda item: (-item["modified"], item["name"]))
    return found


def _is_dated(stem: str) -> bool:
    """Is this a rotated copy — a name ending in ``_YYYYMMDD``?"""
    tail = stem.rsplit("_", 1)[-1]
    return len(tail) == 8 and tail.isdigit()


def resolve_log(log_dir: str | Path, name: str) -> Path:
    """The path for a requested log name, or raise.

    The name is matched against what :func:`list_logs` actually found rather than joined onto the
    directory. A name arriving from a URL is untrusted, and ``../../etc/passwd`` joins just as
    happily as ``metrics.log`` does — the only safe check is that the file is one this function
    was already willing to list.
    """
    wanted = str(name or "").strip()
    for item in list_logs(log_dir):
        if item["name"] == wanted:
            return Path(log_dir) / item["name"]
    raise FileNotFoundError(f"No current log named '{wanted}' in {log_dir}.")
