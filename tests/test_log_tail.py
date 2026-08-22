"""Reading a log from the end has to tile the file exactly, and never spin.

The console shows the running log newest-first, a hundred lines at a time, and asks for the next
hundred as the operator scrolls. That is only useful if the pages fit together perfectly: a gap
means a line nobody will ever see, and an overlap means the same error printed twice, which is how
an operator concludes something happened twice.

The other property is that it terminates. The first version pushed its cursor back over the
partial line at a chunk boundary, so whenever a chunk was shorter than one line the cursor did not
move and the read spun forever — a log viewer that hangs the worker's only web process.

The parse is deliberately forgiving: ``*_runtime.log`` is raw stdout, and a traceback is not
pipe-delimited anything. A line that does not fit the format is exactly the line somebody opened
the log to read, so it is kept whole rather than dropped or mangled.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from db_ops.lib import log_tail

LINES = [
    f"2026-08-21 08:{index // 60:02d}:{index % 60:02d}|LOGGING|metrics|host|metrics.collect|line {index}"
    for index in range(500)
]


@pytest.fixture()
def log_file(tmp_path: Path) -> Path:
    path = tmp_path / "metrics.log"
    path.write_text("\n".join(LINES) + "\n", encoding="utf-8")
    return path


def page_through(path: Path, *, limit: int, chunk_bytes: int = log_tail.CHUNK_BYTES,
                 max_pages: int = 5000) -> list[str]:
    """Every line, walked page by page the way the console walks it."""
    seen: list[str] = []
    before = None
    for _ in range(max_pages):
        page = log_tail.read_tail(path, limit=limit, before=before, chunk_bytes=chunk_bytes)
        seen.extend(line.text for line in page["lines"])
        before = page["next_before"]
        if before is None:
            return seen
    raise AssertionError("paging did not reach the start of the file")


# --------------------------------------------------------------------------- #
# The pages tile the file
# --------------------------------------------------------------------------- #
def test_the_newest_line_comes_first(log_file: Path) -> None:
    page = log_tail.read_tail(log_file, limit=10)
    assert page["lines"][0].message == "line 499"
    assert page["lines"][-1].message == "line 490"


@pytest.mark.parametrize("limit,chunk", [(100, log_tail.CHUNK_BYTES), (7, 64), (13, 40), (1, 16)])
def test_paging_covers_the_file_exactly_once(log_file: Path, limit: int, chunk: int) -> None:
    """No gap and no overlap, at every page size — including chunks smaller than one line."""
    assert page_through(log_file, limit=limit, chunk_bytes=chunk) == list(reversed(LINES))


def test_a_chunk_smaller_than_a_line_still_makes_progress(log_file: Path) -> None:
    """The hang. A cursor pushed back over the partial line never moved, and the read spun."""
    assert page_through(log_file, limit=5, chunk_bytes=8, max_pages=200) == list(reversed(LINES))


def test_a_file_with_no_trailing_newline_is_read_whole(tmp_path: Path) -> None:
    """The byte arithmetic counts a terminator per line; without one the last line loses a byte."""
    path = tmp_path / "no-newline.log"
    path.write_text("\n".join(LINES), encoding="utf-8")
    assert page_through(path, limit=25) == list(reversed(LINES))


def test_the_last_page_reports_that_it_is_the_last(log_file: Path) -> None:
    page = log_tail.read_tail(log_file, limit=1000)
    assert page["next_before"] is None and page["exhausted"] is True


def test_blank_lines_do_not_become_rows(tmp_path: Path) -> None:
    path = tmp_path / "gappy.log"
    path.write_text("first\n\n\nsecond\n\n", encoding="utf-8")
    page = log_tail.read_tail(path, limit=10)
    assert [line.text for line in page["lines"]] == ["second", "first"]


def test_a_missing_file_is_empty_rather_than_an_error(tmp_path: Path) -> None:
    """A log an app has not written yet is normal; the panel should say nothing, not break."""
    page = log_tail.read_tail(tmp_path / "never-written.log", limit=10)
    assert page["lines"] == [] and page["exhausted"] is True


def test_an_empty_file_is_empty(tmp_path: Path) -> None:
    path = tmp_path / "empty.log"
    path.write_text("", encoding="utf-8")
    assert log_tail.read_tail(path, limit=10)["lines"] == []


def test_reading_a_huge_file_touches_only_the_tail(tmp_path: Path) -> None:
    """Bounded by the chunk, not the file: the point of seeking rather than scanning."""
    path = tmp_path / "big.log"
    with path.open("w", encoding="utf-8") as handle:
        for index in range(200_000):
            handle.write(f"2026-08-21 08:00:00|LOGGING|metrics|host|f|line {index}\n")

    page = log_tail.read_tail(path, limit=100)
    assert page["lines"][0].message == "line 199999"
    assert len(page["lines"]) == 100
    assert page["next_before"] < path.stat().st_size


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def test_a_structured_line_splits_into_its_fields() -> None:
    line = log_tail.parse_line(
        "2026-08-21 08:17:03|LOGGING|metrics|8099c08c59fd|metrics.collect|run_id=34607 targets=21")
    assert line.timestamp == "2026-08-21 08:17:03"
    assert line.level == "LOGGING"
    assert line.app == "metrics"
    assert line.function == "metrics.collect"
    assert line.message == "run_id=34607 targets=21"
    assert line.structured is True


def test_pipes_inside_a_message_stay_in_the_message() -> None:
    """A connection string or a rendered row is full of them; splitting on all would look corrupt."""
    line = log_tail.parse_line("2026-08-21 08:00:00|ERROR|m|h|f|dsn=a|b|c")
    assert line.message == "dsn=a|b|c"


def test_a_line_without_a_function_is_still_structured() -> None:
    """Half of ``metrics.log`` has no function field, and a six-field parser read all of it as raw.

    Only the calls through ``format_function_message`` emit one; a plain ``log_event`` writes the
    message straight into the fifth field. Both shapes alternate line by line in the same file.
    """
    line = log_tail.parse_line(
        "2026-08-21 00:01:27|LOGGING|metrics|8e0ed5149589|Metric collect finished. run_id=34357")
    assert line.structured is True
    assert line.level == "LOGGING"
    assert line.function == ""
    assert line.message == "Metric collect finished. run_id=34357"


def test_a_function_line_with_an_empty_message_keeps_its_function() -> None:
    line = log_tail.parse_line("2026-08-21 00:00:49|LOGGING|metrics|8e0ed5149589|metrics.collect|")
    assert line.function == "metrics.collect" and line.message == ""


def test_a_message_with_a_pipe_is_not_mistaken_for_a_function() -> None:
    """The fifth field becomes a function only if it looks like one: an identifier, no spaces."""
    line = log_tail.parse_line("2026-08-21 08:00:00|ERROR|m|h|dsn=a|b")
    assert line.function == "" and line.message == "dsn=a|b"


def test_the_file_header_is_not_read_as_a_log_line() -> None:
    line = log_tail.parse_line(log_tail_header())
    assert line.structured is False


def log_tail_header() -> str:
    from db_ops.logging_ops.formatter import LOG_HEADER

    return LOG_HEADER


def test_a_raw_stdout_line_is_kept_whole() -> None:
    """`*_runtime.log` is stdout, and the unformatted line is usually the one being looked for."""
    for text in ("Traceback (most recent call last):", "  File \"x.py\", line 3", "1234 not a log"):
        line = log_tail.parse_line(text)
        assert line.structured is False
        assert line.message == text


def test_a_line_that_merely_starts_with_digits_is_not_mistaken_for_a_timestamp() -> None:
    line = log_tail.parse_line("2026|x|y|z|w|v")
    assert line.structured is False


# --------------------------------------------------------------------------- #
# Which files are offered
# --------------------------------------------------------------------------- #
def test_only_the_current_logs_are_listed(tmp_path: Path) -> None:
    """Thirty rotations of nineteen apps would bury the nineteen files anybody wants."""
    for name in ("metrics.log", "metrics_runtime.log", "metrics_20260819.log",
                 "telegram.log", "notes.txt"):
        (tmp_path / name).write_text("x\n", encoding="utf-8")

    names = {item["name"] for item in log_tail.list_logs(tmp_path)}
    assert names == {"metrics.log", "metrics_runtime.log", "telegram.log"}


def test_the_busiest_log_is_offered_first(tmp_path: Path) -> None:
    import os
    import time

    old, new = tmp_path / "old.log", tmp_path / "new.log"
    old.write_text("x\n", encoding="utf-8")
    new.write_text("x\n", encoding="utf-8")
    os.utime(old, (time.time() - 7200, time.time() - 7200))
    assert log_tail.list_logs(tmp_path)[0]["name"] == "new.log"


def test_a_runtime_log_is_marked_as_one(tmp_path: Path) -> None:
    (tmp_path / "metrics_runtime.log").write_text("x\n", encoding="utf-8")
    item = log_tail.list_logs(tmp_path)[0]
    assert item["runtime"] is True and item["app"] == "metrics"


def test_resolve_refuses_anything_it_would_not_list(tmp_path: Path) -> None:
    """A name off a URL is untrusted, and ``../../etc/passwd`` joins as happily as a log name."""
    (tmp_path / "metrics.log").write_text("x\n", encoding="utf-8")
    assert log_tail.resolve_log(tmp_path, "metrics.log").name == "metrics.log"
    for hostile in ("../../../etc/passwd", "/etc/passwd", "metrics.log/../../secret",
                    "metrics_20260819.log", ""):
        with pytest.raises(FileNotFoundError):
            log_tail.resolve_log(tmp_path, hostile)
