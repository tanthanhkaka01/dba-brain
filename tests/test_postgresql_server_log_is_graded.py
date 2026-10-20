"""The PostgreSQL server log is graded through the docker collector with a dual-window rule.

PostgreSQL records data corruption (invalid page in block, checksum verification failed), backend
process crashes (terminated by signal), out-of-memory events, and storage exhaustion (disk full,
could not extend file) to its server log on stderr and nowhere queryable by SQL catalog views or
DMVs. DATABASE_SUSPECT_PAGES detects checksum failures only when data_checksums was enabled at
cluster initialization; otherwise corruption is invisible to SQL until a query trips over it.
Similarly, when a backend crashes with a SIGSEGV/SIGBUS, the postmaster restarts all connections,
leaving no trace in any database table.

Following 017_sqlserver_errorlog_recent_critical.sql, the collector scans the last 24 hours
(PG_LOG_WINDOW_HOURS, default 24h) for historical visibility, but restricts CRITICAL paging
status to events within the recent window (PG_LOG_CRITICAL_RECENT_MINUTES, default 120m) so that
past resolved incidents do not page repeatedly. Older critical-class lines and any warning-class
lines in the window report as WARNING. Routine FATAL events (authentication failures, clean
disconnects, server startup/shutdown) are counted but not graded.

If the container logs to a file on disk (logging_collector=on), docker logs holds only startup
lines: that is treated as an OK 'not configured' state rather than an outage.

This test file inspects the collector script text (no database or POSIX shell required).
"""

from __future__ import annotations

from pathlib import Path
import re

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "dbabrain" / "metrics" / "collectors" / "postgresql" / "023_postgresql_server_log.sh"

CRITICAL_PATTERNS = (
    "PANIC:",
    "invalid page in block",
    "could not read block",
    "checksum verification failed",
    "unexpected zero page",
    "invalid memory alloc request",
    "found xmin .* from before relfrozenxid",
    "could not access status of transaction",
    "No space left on device",
    "could not write to file",
    "could not extend file",
    "out of memory",
    "server process .* was terminated by signal",
)

ROUTINE_FATAL_PATTERNS = (
    "password authentication failed",
    "no pg_hba.conf entry",
    "the database system is starting up",
    "the database system is shutting down",
    "terminating connection due to administrator command",
    "connection to client lost",
    "canceling statement due to",
)

CONNECTION_EXHAUSTION_PATTERNS = (
    "remaining connection slots are reserved",
    "sorry, too many clients already",
)

SUMMARY_MESSAGE_KEYS = (
    "window_hours=",
    "critical_recent=",
    "critical_window=",
    "warning_class=",
    "auth_failures=",
    "routine_fatal=",
    "last_line_time=",
    "latest_class=",
    "latest_text=",
)


def _text() -> str:
    assert SCRIPT_PATH.is_file(), f"Collector script not found at {SCRIPT_PATH}"
    return SCRIPT_PATH.read_text(encoding="utf-8").replace("\r\n", "\n")


def _code() -> str:
    """The script text without comment lines, whitespace collapsed."""
    lines = [
        line.split("#", 1)[0]
        for line in _text().splitlines()
        if not line.lstrip().startswith("#")
    ]
    return re.sub(r"\s+", " ", " ".join(lines))


# --------------------------------------------------------------------------- #
# Script presence and invocation contract
# --------------------------------------------------------------------------- #
def test_collector_script_file_exists_under_collectors_postgresql():
    assert SCRIPT_PATH.is_file(), f"Expected collector at {SCRIPT_PATH}"
    assert SCRIPT_PATH.name == "023_postgresql_server_log.sh"


def test_docker_logs_command_invoked_with_since_window_and_timestamps():
    code = _code()
    assert 'logs --since' in code and '--timestamps' in code and '$container' in code, \
        "Script must invoke docker logs with --since window and --timestamps for the container"


def test_default_window_hours_is_24():
    text = _text()
    assert 'PG_LOG_WINDOW_HOURS:-24' in text, \
        "Script must declare PG_LOG_WINDOW_HOURS with default 24"


def test_default_critical_recent_minutes_is_120():
    text = _text()
    assert 'PG_LOG_CRITICAL_RECENT_MINUTES:-120' in text, \
        "Script must declare PG_LOG_CRITICAL_RECENT_MINUTES with default 120"


# --------------------------------------------------------------------------- #
# Pattern matching and classification
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("pattern", CRITICAL_PATTERNS)
def test_all_critical_class_patterns_are_present_in_script_text(pattern: str):
    text = _text()
    if ".*" in pattern:
        for part in pattern.split(".*"):
            assert part.strip() in text, f"Critical pattern part '{part.strip()}' must be present in script"
    else:
        assert pattern in text, f"Critical pattern '{pattern}' must be present in script"


@pytest.mark.parametrize("pattern", ROUTINE_FATAL_PATTERNS)
def test_routine_fatal_patterns_are_pinned_and_excluded_from_warning_grade(pattern: str):
    text = _text()
    assert pattern in text, \
        f"Routine FATAL pattern '{pattern}' must be explicitly matched in the script"


@pytest.mark.parametrize("pattern", CONNECTION_EXHAUSTION_PATTERNS)
def test_connection_exhaustion_patterns_are_present_in_script_text(pattern: str):
    text = _text()
    assert pattern in text, \
        f"Connection exhaustion pattern '{pattern}' must be explicitly matched in the script"


# --------------------------------------------------------------------------- #
# Dual-window grading rules
# --------------------------------------------------------------------------- #
def test_grading_rule_recent_critical_line_triggers_critical_status():
    code = _code()
    assert re.search(r'critical_recent.*-gt.*0.*CRITICAL', code) or \
           re.search(r'overall_status="CRITICAL"', code), \
           "A critical line within the recent window must produce status CRITICAL"


def test_grading_rule_older_critical_or_warning_line_triggers_warning_status():
    code = _code()
    assert re.search(r'critical_window.*warning_class.*WARNING', code) or \
           re.search(r'overall_status="WARNING"', code), \
           "Older critical lines or warning lines in the window must produce status WARNING"


def test_grading_rule_clean_log_triggers_ok_status():
    code = _code()
    assert 'overall_status="OK"' in code or "overall_status='OK'" in code, \
        "A log with no graded events must produce status OK"


# --------------------------------------------------------------------------- #
# Not configured & error handling
# --------------------------------------------------------------------------- #
def test_not_configured_message_when_logging_collector_is_on_or_no_log_lines():
    text = _text()
    expected = ("no graded line in the window (a container with logging_collector on logs to a file this "
                "collector does not read)")
    assert expected in text, \
        f"Script must state '{expected}' when logging_collector is active or no lines exist"


def test_docker_failure_or_missing_container_emits_warning_status():
    code = _code()
    assert 'DOCKER_CONTAINER is not set' in code, \
        "Script must handle missing DOCKER_CONTAINER"
    assert 'docker inspect failed' in code, \
        "Script must handle a container that cannot be found"
    assert re.search(r'server_log.*0.*lines.*WARNING', code), \
        "Docker command or container lookup failure must report status WARNING (monitoring gap)"


# --------------------------------------------------------------------------- #
# Helpers and row formatting contract
# --------------------------------------------------------------------------- #
def test_json_escape_and_add_row_helpers_are_present():
    text = _text()
    assert "json_escape()" in text, "Script must include json_escape helper"
    assert "add_row()" in text, "Script must include add_row helper"
    assert "emit()" in text, "Script must include emit helper"


def test_summary_row_and_logging_detail_rows_contract():
    code = _code()
    # Summary row
    assert 'add_row "server_log" "$graded_count" "lines" "$overall_status"' in code or \
           'add_row "server_log"' in code, \
           "Script must emit primary 'server_log' summary row"

    # Detail rows at status LOGGING
    assert 'server_log :: ' in code, "Detail rows must use metric_item 'server_log :: <class>'"
    assert 'LOGGING' in code, "Detail rows must report at status LOGGING"

    # Up to 5 detail rows limit
    assert re.search(r'detail_count.*-ge.*5.*break', code) or 'detail_count' in code, \
        "Detail rows must be capped at 5"

    # Message keys
    for key in SUMMARY_MESSAGE_KEYS:
        assert key in code, f"Summary message must contain key '{key}'"


# --------------------------------------------------------------------------- #
# Safety constraints
# --------------------------------------------------------------------------- #
def test_script_contains_no_destructive_commands():
    text = _text().upper()
    destructive_verbs = ("DROP ", "DELETE ", "TRUNCATE ", "ALTER ", "KILL ", "RM -RF")
    for verb in destructive_verbs:
        assert verb not in text, f"Script must be read-only and never include '{verb}'"


def test_the_log_is_streamed_through_one_grep_and_never_held_whole():
    """A day of a busy log looped over in bash would outrun the 60-second timeout (review, 1.165)."""
    code = _code()
    assert re.search(r'log_output=\$\(\$DOCKER logs --since "\$\{window_hours\}h" --timestamps "\$container" 2>&1 \| grep -E "\$patterns"\)', code)
    for needle in ("PANIC:", "FATAL:", "No space left on device", "was terminated by signal",
                   "checkpoints are occurring too frequently", "password authentication failed"):
        assert needle in code


def test_postgresqls_own_checkpoint_warning_is_a_warning():
    """checkpoint_warning (30 s by default) is PostgreSQL's own alarm for checkpoint pressure (1.145)."""
    code = _code()
    assert re.search(r'"checkpoints are occurring too frequently"\* \]\]; then matched_class="checkpoint_pressure" matched_severity="WARNING"', code)
