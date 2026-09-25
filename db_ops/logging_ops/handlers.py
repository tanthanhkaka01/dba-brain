from __future__ import annotations

import logging
import socket

from db_ops.levels import CRITICAL, ERROR, LOGGING, WARNING
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


class DailyArchiveFileHandler(logging.FileHandler):
    def __init__(self, filename: Path, *, encoding: str = "utf-8") -> None:
        self.path = Path(filename)
        self.current_date = display_today()

        self.path.parent.mkdir(parents=True, exist_ok=True)

        # Check when the app starts.
        archive_yesterday_if_missing(self.path)
        ensure_current_log_file(self.path)

        super().__init__(self.path, encoding=encoding, delay=True)

    def emit(self, record: logging.LogRecord) -> None:
        today = display_today()

        if today != self.current_date:
            self.current_date = today

            if self.stream:
                self.stream.close()
                self.stream = None

            archive_yesterday_if_missing(self.path, today=today)
            ensure_current_log_file(self.path)

        super().emit(record)


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
