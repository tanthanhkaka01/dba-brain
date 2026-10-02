from __future__ import annotations

import logging
import socket
import sys

from db_ops.lib.levels import CRITICAL, ERROR, LOGGING, WARNING
from db_ops.logging_ops.formatter import LOG_HEADER
from db_ops.lib.timezone import display_today, to_display
from datetime import datetime, timezone
from pathlib import Path


class HostNameFilter(logging.Filter):
    hostname = socket.gethostname()

    def filter(self, record: logging.LogRecord) -> bool:
        record.hostname = self.hostname
        if not hasattr(record, "logtype"):
            record.logtype = record_to_logical_level(record).upper()
        return True


#: What a newline inside one record becomes in a log file - ASCII, so a cp1252 console that shows
#: the same text cannot choke on it, and readable back as the escape it looks like.
NEWLINE_IN_A_RECORD = "\\n"


def one_line(text: str) -> str:
    r"""``text`` as a single log line: every line break inside it written as ``\n``.

    A record is one line, ``DATE|LOGTYPE|APP|HOST|FUNCTION|TEXT``. A message carrying a newline - a
    remote command's stderr, a driver error, a traceback appended by the formatter - became extra
    lines with none of those fields, which `lib/log_tail` and every parser then filed under whatever
    record came before (review 0.25.0, F2.3). Only some callers flattened their own values.
    """
    return text.replace("\r\n", NEWLINE_IN_A_RECORD).replace("\n", NEWLINE_IN_A_RECORD).replace(
        "\r", NEWLINE_IN_A_RECORD)


class DailyArchiveFileHandler(logging.FileHandler):
    #: Windows cannot rename a file that another process holds open, and the daemon held its logs
    #: open for its whole life: the nightly rename failed - silently, by design, as a race - every
    #: night, and `errors.log` grew without bound on a Windows master (review 0.25.0, F2.2). There a
    #: record opens, appends and closes, as `TeeStdout` does, so no handle outlives one write, and
    #: the archive check runs before each record: a rename that lost a race to another writer is
    #: retried on the next line, not the next day. POSIX renames an open file, and keeps the stream.
    close_after_each_record = sys.platform == "win32"

    def __init__(self, filename: Path, *, encoding: str = "utf-8") -> None:
        self.path = Path(filename)
        self.current_date = display_today()

        self.path.parent.mkdir(parents=True, exist_ok=True)

        # Check when the app starts.
        archive_yesterday_if_missing(self.path)
        ensure_current_log_file(self.path)

        super().__init__(self.path, encoding=encoding, delay=True)

    def format(self, record: logging.LogRecord) -> str:
        # The file is what parsers read, so the file holds one line per record. The console keeps
        # its line breaks: a person reads a traceback there.
        return one_line(super().format(record))

    def emit(self, record: logging.LogRecord) -> None:
        today = display_today()
        new_day = today != self.current_date

        if new_day:
            self.current_date = today

            if self.stream:
                self.stream.close()
                self.stream = None

        if new_day or self.close_after_each_record:
            archive_yesterday_if_missing(self.path, today=today)
            ensure_current_log_file(self.path)

        try:
            super().emit(record)
        finally:
            if self.close_after_each_record and self.stream:
                self.stream.close()
                self.stream = None


def archive_yesterday_if_missing(path: Path, *, today=None) -> Path | None:
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return None

    reference_date = today or display_today()
    # The day the file was LAST WRITTEN, on the operator's clock - not "yesterday". Named after
    # yesterday whatever it held, the first process of a new root filed that root's first lines
    # (written today) under yesterday's date, and a node back from three days down filed its
    # last day under the wrong one (found 2026-09-23). A file last written today is today's log
    # and is left alone.
    written_on = to_display(datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)).date()
    if written_on >= reference_date:
        return None
    archive_path = path.with_name(
        f"{path.stem}_{written_on.strftime('%Y%m%d')}{path.suffix}"
    )

    if archive_path.exists():
        return None

    try:
        path.rename(archive_path)
    except OSError:
        # Concurrent-writer race at the daily rollover: another db_ops process may
        # have archived (or be mid-archive on) this file between the exists() check
        # and here. Tolerate it — the archive still ends up present — instead of
        # crashing the app that happened to log first past midnight.
        return archive_path if archive_path.exists() else None
    return archive_path


def ensure_current_log_file(path: Path) -> None:
    path = Path(path)
    if path.exists() and path.stat().st_size > 0:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        file.write(f"{LOG_HEADER}\n")


def record_to_logical_level(record: logging.LogRecord) -> str:
    if hasattr(record, "logtype"):
        return str(record.logtype).lower()
    if record.levelno >= logging.CRITICAL:
        return CRITICAL
    if record.levelno >= logging.ERROR:
        return ERROR
    if record.levelno >= logging.WARNING:
        return WARNING
    return LOGGING
