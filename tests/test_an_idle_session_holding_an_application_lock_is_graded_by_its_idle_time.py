"""An idle session holding an application lock it owns is graded by how long it has been idle.

`sp_getapplock @LockOwner = 'Session'` takes a lock with no transaction open. A query tool's session
took two such locks, went to sleep, and kept them for hours; three lock metrics read it and none
went CRITICAL, each for a reason it was built on:

- `LOCK_SLEEPING_OPEN_TRANSACTION` lists sessions with a transaction open - this one had none;
- `LOCK_TRANSACTION_HOLDERS` gave CRITICAL only to an old transaction with somebody waiting, and
  with no transaction the age is 0 however long the lock is held;
- `LOCK_BLOCKING_SESSIONS` needed a long wait - but a waiter on `sp_getapplock` gives up at its own
  `@LockTimeout` and retries, so the wait never grew.

`sys.dm_tran_locks` keeps no grant time, so the lock has no age of its own. The one measure left is
how long the session holding it has been idle: a working session releases it, an idle one keeps it.
The operator's thresholds (2026-10-15): **WARNING after 5 minutes idle, CRITICAL after 30, and
CRITICAL at once when somebody waits on it and it has been idle 60 seconds.**

It lives in `LOCK_TRANSACTION_HOLDERS`, both variants, because that metric already reads the lock
manager once, behind its size guard; a second metric scanning it every cycle is the cost the guard
exists to bound. What can be held without a server is held below - the SQL's contract, not its
results.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from dbabrain.lib.sql_text import apply_sql_settings

REPO_ROOT = Path(__file__).resolve().parents[1]
COLLECTORS = REPO_ROOT / "dbabrain" / "metrics" / "collectors"
MODERN = "sqlserver/025_sqlserver_transaction_lock_holders.sql"
LEGACY = "sqlserver/legacy_2008r2/025_sqlserver_transaction_lock_holders.sql"
VARIANTS = (MODERN, LEGACY)

THRESHOLDS = {
    "applock_idle_warning_seconds": 300,
    "applock_idle_critical_seconds": 1800,
    "applock_idle_abandoned_seconds": 60,
}

SCAN_STARTS = "IF OBJECT_ID('tempdb..#locks') IS NOT NULL"


def _text(relative: str) -> str:
    return (COLLECTORS / relative).read_text(encoding="utf-8").replace("\r\n", "\n")


def _code(sql: str) -> str:
    """The SQL without its comment lines, whitespace collapsed: the contract, not its layout."""
    lines = [line.split("--", 1)[0] for line in sql.splitlines()
             if not line.lstrip().startswith("--")]
    return re.sub(r"\s+", " ", " ".join(lines))


def _scan(relative: str) -> str:
    """The part that runs when the lock manager is small enough to enumerate."""
    text = _text(relative)
    return _code(text[text.index(SCAN_STARTS):])


def _status_rules(relative: str) -> list[str]:
    """The WHEN clauses of the status CASE, in order, each up to its THEN."""
    code = _scan(relative)
    end = code.index(" END AS status")
    case = code[code.rindex("CASE WHEN", 0, end):end]
    return [rule.strip() for rule in re.findall(r"WHEN (.*?) THEN '(?:CRITICAL|WARNING)'", case)]


# --------------------------------------------------------------------------- #
# The thresholds: the operator's, declared where configuration can reach them
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("relative", VARIANTS)
def test_both_variants_declare_the_three_thresholds_with_the_agreed_defaults(relative):
    text = _text(relative)
    for name, value in THRESHOLDS.items():
        assert re.search(rf"^DECLARE @{name} int = {value};", text, re.MULTILINE), name


@pytest.mark.parametrize("relative", VARIANTS)
def test_each_threshold_is_in_the_shape_a_configured_number_is_written_into(relative):
    """A node may state any of them in ``sql_settings``; the file must take it, once."""
    configured = apply_sql_settings(_text(relative), {name: value + 1
                                                      for name, value in THRESHOLDS.items()})
    for name, value in THRESHOLDS.items():
        assert f"DECLARE @{name} int = {value + 1};" in configured


def test_no_catalogue_states_them_before_the_code_that_declares_them_has_shipped():
    """The worker takes the master's data files at any time, and a node whose SQL does not declare
    a stated name refuses the setting - the metric would fail on every SQL Server there. The
    defaults are the operator's numbers, so nothing needs stating; when a released version declares
    all three, this test is what to delete."""
    files = [COLLECTORS.parent / "catalogue" / "metric_definitions.json",
             REPO_ROOT / "data" / "metric_definitions.json",
             REPO_ROOT / "data" / "metric_definitions.example.json"]
    for path in files:
        if not path.exists():
            continue
        document = json.loads(path.read_text(encoding="utf-8-sig"))
        metrics = next(value for value in document.values()
                       if isinstance(value, list) and value and isinstance(value[0], dict))
        for metric in metrics:
            stated = metric.get("sql_settings") or {}
            assert not set(stated) & set(THRESHOLDS), (path.name, metric.get("metric_code"))


# --------------------------------------------------------------------------- #
# What the one scan now takes, and how the idle time is measured
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("relative", VARIANTS)
def test_the_snapshot_takes_a_session_owned_application_lock_in_any_mode(relative):
    """A shared one still stops an exclusive request for the same name."""
    code = _scan(relative)
    snapshot = code[code.index("INTO #locks"):code.index("@@SPID")]

    assert ("OR (tl.resource_type = 'APPLICATION' AND tl.request_owner_type = 'SESSION')"
            in snapshot)
    assert "tl.request_owner_type," in code
    assert "LTRIM(RTRIM(tl.resource_description))" in code, "the lock's name, trimmed"


@pytest.mark.parametrize("relative", VARIANTS)
def test_the_lock_manager_is_still_read_once_and_only_after_the_size_guard(relative):
    code = _code(_text(relative))
    assert code.count("sys.dm_tran_locks") == 1
    assert code.index("@lock_scan_skip_above") < code.index("FROM sys.dm_tran_locks")


@pytest.mark.parametrize("relative", VARIANTS)
def test_idle_time_counts_only_for_a_sleeping_session_holding_one_and_never_goes_negative(relative):
    code = _scan(relative)
    measure = re.search(r"CROSS APPLY \(SELECT applock_idle_seconds = (CASE .*? END)\) ai", code)
    assert measure, "the idle time is measured once, per session"
    rule = measure.group(1)

    assert "la.session_applock_count > 0" in rule
    assert "s.status = 'sleeping'" in rule
    # last_request_end_time can read ahead of GETDATE(); the floor is the > 0 test.
    assert "DATEDIFF(SECOND, s.last_request_end_time, GETDATE()) > 0" in rule
    assert rule.endswith("ELSE 0 END")


# --------------------------------------------------------------------------- #
# The grading
# --------------------------------------------------------------------------- #
def test_the_2012_variant_grades_in_the_agreed_order():
    rules = _status_rules(MODERN)

    assert rules == [
        "h.open_tran_seconds >= @critical_tran_seconds AND h.is_blocking = 1",
        "h.applock_idle_seconds >= @applock_idle_abandoned_seconds AND h.is_blocking = 1",
        "h.applock_idle_seconds >= @applock_idle_critical_seconds",
    ]
    # Anything else that reaches a row of its own is a WARNING.
    assert re.search(r"@applock_idle_critical_seconds THEN 'CRITICAL' ELSE 'WARNING' END AS status",
                     _scan(MODERN))


def test_the_2008_r2_variant_grades_in_the_agreed_order():
    rules = _status_rules(LEGACY)

    assert rules[2:] == [
        "ai.applock_idle_seconds >= @applock_idle_abandoned_seconds AND EXISTS (SELECT 1 FROM "
        "sys.dm_exec_requests br WHERE br.blocking_session_id = s.session_id)",
        "ai.applock_idle_seconds >= @applock_idle_critical_seconds",
        "ISNULL(ta.open_tran_seconds, 0) >= @warning_tran_seconds",
        "ai.applock_idle_seconds >= @applock_idle_warning_seconds",
    ]
    assert re.search(r"@applock_idle_warning_seconds THEN 'WARNING' ELSE 'LOGGING' END AS status",
                     _scan(LEGACY))


def test_the_2012_variant_gives_an_idle_holder_a_row_of_its_own_and_takes_it_out_of_the_summary():
    """The detail rows and the summary are complements: a session is in exactly one of them."""
    code = _scan(MODERN)
    detail = re.search(r"FROM #holders h WHERE (.*?) UNION ALL", code).group(1)
    assert detail == ("h.open_tran_seconds >= @warning_tran_seconds OR h.is_blocking = 1 "
                      "OR h.applock_idle_seconds >= @applock_idle_warning_seconds")

    summary = code[code.index("'holders_summary'"):]
    assert "q.applock_idle_seconds < @applock_idle_warning_seconds" in summary
    assert "q2.applock_idle_seconds < @applock_idle_warning_seconds" in summary, \
        "the session ids listed in the summary follow the same filter"


@pytest.mark.parametrize("relative", VARIANTS)
def test_lock_seconds_is_the_larger_of_the_transaction_age_and_the_idle_time(relative):
    code = _scan(relative)
    assert re.search(r"CASE WHEN \w+\.applock_idle_seconds > (ISNULL\()?\w+\.open_tran_seconds"
                     r"(, 0\))? THEN \w+\.applock_idle_seconds ELSE (ISNULL\()?\w+\."
                     r"open_tran_seconds(, 0\))? END", code)


@pytest.mark.parametrize("relative", VARIANTS)
def test_the_message_names_the_count_the_idle_time_and_up_to_five_lock_names(relative):
    code = _scan(relative)
    assert "', session_applocks='" in code
    assert "', applock_idle_seconds='" in code
    names = code[code.index("', applock_names='"):]
    assert "SELECT TOP (5)" in names[:200]
