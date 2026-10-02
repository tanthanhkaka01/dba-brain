r"""A log file holds one line per record, and rotates every night on Windows too.

Two gaps of the log handlers (review 0.25.0, F2.2 and F2.3):

* **The nightly rotation never happened on a Windows master.** Windows cannot rename a file another
  process holds open, and the daemon held ``errors.log`` / ``jobs.log`` open for its whole life. The
  rename failed every night - silently, because a failure there is normally a race with another
  writer - and the file grew without bound. On Windows a record now opens, appends and closes, and
  the archive check runs before each one, so a rename that lost a race is retried on the next line.
* **A newline inside a message split the record.** Remote stderr, a driver error or a traceback
  became extra lines with no ``DATE|LOGTYPE|APP|HOST`` prefix, which the log tail and every parser
  then filed under the record before. The file handlers write every line break as ``\n``.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

import pytest

from db_ops.logging_ops.handlers import DailyArchiveFileHandler, HostNameFilter


def _handler(path: Path, *, close_after_each_record: bool) -> DailyArchiveFileHandler:
    handler = DailyArchiveFileHandler(path)
    handler.close_after_each_record = close_after_each_record
    handler.setFormatter(logging.Formatter("%(logtype)s|%(message)s"))
    handler.addFilter(HostNameFilter())
    return handler


def _record(message: str, *, exc_info=None) -> logging.LogRecord:
    return logging.LogRecord("db_ops", logging.ERROR, __file__, 1, message, None, exc_info)


def test_a_multi_line_message_is_one_line_in_the_file(tmp_path):
    path = tmp_path / "errors.log"
    handler = _handler(path, close_after_each_record=True)

    handler.handle(_record("sqlcmd failed\nMsg 3013, Level 16\r\nRESTORE is terminating"))

    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[-1] == r"ERROR|sqlcmd failed\nMsg 3013, Level 16\nRESTORE is terminating"


def test_a_traceback_stays_on_its_record_s_line(tmp_path):
    path = tmp_path / "errors.log"
    handler = _handler(path, close_after_each_record=True)
    try:
        raise ValueError("boom")
    except ValueError:
        handler.handle(_record("collector failed", exc_info=sys.exc_info()))

    last = path.read_text(encoding="utf-8").splitlines()[-1]
    assert last.startswith("ERROR|collector failed\\nTraceback") and last.endswith("ValueError: boom")


def test_no_handle_is_held_between_records_where_a_held_one_blocks_the_rename(tmp_path):
    path = tmp_path / "jobs.log"
    handler = _handler(path, close_after_each_record=True)

    handler.handle(_record("one"))

    assert handler.stream is None


def test_yesterday_s_file_is_archived_on_the_next_record_not_the_next_day(tmp_path):
    path = tmp_path / "jobs.log"
    handler = _handler(path, close_after_each_record=True)
    handler.handle(_record("written yesterday"))
    yesterday = time.time() - 2 * 86400
    os.utime(path, (yesterday, yesterday))

    handler.handle(_record("written today"))

    archives = sorted(p.name for p in tmp_path.glob("jobs_*.log"))
    assert len(archives) == 1
    assert "written today" in path.read_text(encoding="utf-8")
    assert "written today" not in (tmp_path / archives[0]).read_text(encoding="utf-8")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows refuses to rename a file held open")
def test_on_windows_another_process_can_rotate_a_file_this_one_writes(tmp_path):
    path = tmp_path / "errors.log"
    handler = DailyArchiveFileHandler(path)
    handler.setFormatter(logging.Formatter("%(message)s"))

    handler.handle(_record("the daemon's line"))
    path.rename(tmp_path / "errors_20260930.log")

    assert (tmp_path / "errors_20260930.log").exists()
