"""A process milliseconds old is not ranked as the host's top CPU consumer.

`ps` reports `%cpu` as CPU time over the process's whole life. A process that has just started has
almost no life to divide by, so it reads 50 - 100 %: on the idle `.251` lab on 2026-10-15 the top of
`OS_PROCESS_TOP_CPU` was `ps` itself at 100 % (WARN), then the `docker exec` and `runc` the other
collectors had just started. The collector was measuring its own measuring. Processes younger than
`OS_TOP_MIN_AGE_SECONDS` (10) are left out; a load that lasts is still there on the next run. A `ps`
without `etimes` (procps before 3.3) lists every process, as before, rather than failing.
"""

from __future__ import annotations

from pathlib import Path

SCRIPT = (Path(__file__).resolve().parents[1] / "dbabrain" / "metrics" / "collectors"
          / "os" / "linux" / "013_os_process_top_cpu.sh")


def _text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_processes_younger_than_the_minimum_age_are_left_out():
    text = _text()
    assert "min_age_seconds=${OS_TOP_MIN_AGE_SECONDS:-10}" in text
    assert "ps -eo pid=,etimes=,comm=,%cpu=,%mem=,rss= --sort=-%cpu" in text
    assert """awk -v min="$min_age_seconds" '$2 >= min { $2 = ""; print }'""" in text


def test_the_age_is_read_before_the_command_name_that_may_hold_spaces():
    text = _text()
    fields = text[text.index("ps -eo pid=,etimes="):].split()[2]
    assert fields.index("etimes=") < fields.index("comm=")


def test_a_ps_without_etimes_still_lists_every_process():
    text = _text()
    assert "if ps -o etimes= -p $$ >/dev/null 2>&1; then" in text
    assert "    ps -eo pid=,comm=,%cpu=,%mem=,rss= --sort=-%cpu 2>/dev/null\n" in text


def test_the_script_is_read_by_bash_on_linux_with_unix_line_endings():
    assert b"\r\n" not in SCRIPT.read_bytes()
