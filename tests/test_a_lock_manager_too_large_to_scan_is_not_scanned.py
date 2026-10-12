"""A lock manager too large to enumerate is not enumerated - and saying so is the finding.

`LOCK_TRANSACTION_HOLDERS` starts by reading `sys.dm_tran_locks` whole, and enumerating the lock
manager takes latches on the lock manager. On 2026-10-07 one session on an ERP server held
1,137,515 locks in a transaction 44 hours old. The metric's scan outlived its 60-second timeout and
became the head of 68 waiting sessions: the monitoring query was the outage's head blocker. Nothing
bounded the scan by the size of what it scanned.

Three decisions are held here, each made from a measurement on the estate's servers (2026-10-09):

- **The size is read before the scan, from a counter that enumerates nothing, and above the limit
  the scan is not run.** The skip is one CRITICAL row, and it names the sessions most likely to hold
  the locks - with their login, host and client address - from the transaction DMVs, which do not
  touch the lock manager.
- **The counter is the locks in use (`Lock Owner Blocks`), not the lock manager's memory.** That
  store keeps its pages after the locks are gone: the same server read 3,700 MB with 800 locks in
  use two days after the incident, so a limit in megabytes would have stayed CRITICAL for good.
- **The limit is configuration.** A collector's thresholds were literals in its SQL - the one place
  a threshold could not be configured. A metric may now state `sql_settings`, and only a number is
  ever written into the file, into a `DECLARE` the file itself carries with its default.

The SQL itself was run on SQL Server 2008 R2, 2019 and 2022 with the limit forced to 1; what can be
held without a server is held below.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from db_ops.lib.paths import PACKAGED_CATALOGUE
from db_ops.lib.sql_text import SqlSettingError, apply_sql_settings
from db_ops.metrics.definitions import load_metric_definitions

REPO_ROOT = Path(__file__).resolve().parents[1]
COLLECTORS = REPO_ROOT / "db_ops" / "metrics" / "collectors"
CATALOGUE = REPO_ROOT / "db_ops" / "metrics" / "catalogue" / "metric_definitions.json"
VARIANTS = ("sqlserver/025_sqlserver_transaction_lock_holders.sql",
            "sqlserver/legacy_2008r2/025_sqlserver_transaction_lock_holders.sql")

GUARD_STARTS = "-- 0. Is the lock manager too large to enumerate at all?"
GUARD_ENDS = "    RETURN;\nEND\n"


def _text(relative: str) -> str:
    return (COLLECTORS / relative).read_text(encoding="utf-8").replace("\r\n", "\n")


def _guard(relative: str) -> str:
    text = _text(relative)
    return text[text.index(GUARD_STARTS):text.index(GUARD_ENDS) + len(GUARD_ENDS)]


def _code(sql: str) -> str:
    """The SQL without its comment lines: a comment may name what the code must not touch."""
    return "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))


def _metrics() -> list[dict]:
    document = json.loads(CATALOGUE.read_text(encoding="utf-8-sig"))
    return next(value for value in document.values()
                if isinstance(value, list) and value and isinstance(value[0], dict))


# --------------------------------------------------------------------------- #
# The guard comes first, and does not do what it guards against
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("relative", VARIANTS)
def test_the_size_is_read_before_anything_enumerates_the_lock_manager(relative):
    code = _code(_text(relative))

    assert code.index("IF ISNULL(@locks_in_use, 0) >= @lock_scan_skip_above") \
        < code.index("sys.dm_tran_locks"), "the scan must not start before the guard has decided"


@pytest.mark.parametrize("relative", VARIANTS)
def test_the_skip_leaves_before_the_scan_and_never_reads_the_locks_itself(relative):
    guard = _code(_guard(relative))

    assert "sys.dm_tran_locks" not in guard, "the skip branch would be the scan it replaces"
    assert guard.rstrip().endswith("RETURN;\nEND"), "without RETURN the scan runs after the skip"


@pytest.mark.parametrize("relative", VARIANTS)
def test_the_skip_creates_no_temp_table(relative):
    """A server in this state is the last place to create one."""
    guard = _code(_guard(relative))

    assert "INTO #" not in guard and "CREATE TABLE" not in guard.upper()


@pytest.mark.parametrize("relative", VARIANTS)
def test_the_size_is_the_locks_in_use_not_the_memory_of_the_store(relative):
    """The store keeps its pages: 3,700 MB with 800 locks in use, two days after the incident."""
    guard = _code(_guard(relative))

    assert "counter_name = 'Lock Owner Blocks'" in guard
    assert "dm_os_memory_clerks" not in guard


# --------------------------------------------------------------------------- #
# The skip is a finding, and it names who
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("relative", VARIANTS)
def test_the_skip_is_critical_and_names_the_session_and_where_it_connects_from(relative):
    guard = _code(_guard(relative))

    assert "CAST('lock_scan_skipped' AS varchar(256)) AS metric_item" in guard
    assert "CAST('CRITICAL' AS varchar(16)) AS status" in guard
    for said in ("spid=", "login=", "host=", "client_ip=", "program=", "database=",
                 "open_tran_seconds=", "log_records="):
        assert said in guard, f"the skip rows do not say {said}"


@pytest.mark.parametrize("relative", VARIANTS)
def test_the_largest_writer_and_the_oldest_transaction_are_both_critical(relative):
    """A holder that only read - millions of shared locks, no log - has no log to rank by."""
    guard = _code(_guard(relative))

    assert "CASE WHEN h.rn = 1 OR h.rn_age = 1 THEN 'CRITICAL' ELSE 'WARNING' END" in guard
    assert "WHERE h.rn <= 5 OR h.rn_age = 1" in guard, "the oldest must be a row even when sixth"


@pytest.mark.parametrize("relative", VARIANTS)
def test_the_skip_rows_have_the_columns_the_scan_rows_have(relative):
    """One result set either way: a reader of this metric must not need to know which ran."""
    guard = _guard(relative)

    assert ("SELECT out_rows.metric_item, out_rows.metric_value, out_rows.metric_unit, "
            "out_rows.status,\n           out_rows.lock_seconds, out_rows.message") in guard


def test_the_2008_r2_variant_carries_the_same_guard_word_for_word():
    """Written without CONCAT, IIF or any column 2012 added, so one text serves both files - and
    a fix made to one cannot be forgotten in the other."""
    assert _guard(VARIANTS[0]) == _guard(VARIANTS[1])
    for newer in ("CONCAT(", "IIF(", "TRY_CONVERT", "FORMAT(", "open_transaction_count",
                  "STRING_AGG"):
        assert newer not in _code(_guard(VARIANTS[1])), f"{newer} is 2012 or later"


# --------------------------------------------------------------------------- #
# The limit is configuration
# --------------------------------------------------------------------------- #
SQL = ("-- header\n"
       "DECLARE @warning_tran_seconds  int = 300;    -- worth a look\n"
       "DECLARE @lock_scan_skip_above bigint = 500000;\n"
       "SELECT 1;\n")


def test_a_configured_number_replaces_the_default_the_file_declares():
    out = apply_sql_settings(SQL, {"lock_scan_skip_above": 750000})

    assert "DECLARE @lock_scan_skip_above bigint = 750000;" in out
    assert "DECLARE @warning_tran_seconds  int = 300;    -- worth a look" in out


def test_with_nothing_configured_the_file_runs_as_it_is():
    assert apply_sql_settings(SQL, {}) == SQL and apply_sql_settings(SQL, None) == SQL


def test_a_whole_number_is_written_as_one():
    assert "bigint = 750000;" in apply_sql_settings(SQL, {"lock_scan_skip_above": 750000.0})


@pytest.mark.parametrize("settings", [
    {"lock_scan_skip_above": "1; DROP TABLE x"},
    {"lock_scan_skip_above": "750000"},
    {"lock_scan_skip_above": True},
    {"lock_scan_skip_above": None},
    {"lock scan": 1},
    {"lock_scan_skip_above; --": 1},
], ids=["sql in a string", "a number as text", "a boolean", "nothing", "a space in the name",
        "sql in the name"])
def test_only_a_number_under_a_plain_name_is_ever_written_into_sql(settings):
    """This writes into SQL text, so there is nothing to quote: what needs quoting is refused."""
    with pytest.raises(SqlSettingError):
        apply_sql_settings(SQL, settings)


def test_a_setting_the_file_does_not_declare_is_an_error_not_a_value_quietly_unused():
    """An operator who raised a limit no SQL reads would believe it was raised."""
    with pytest.raises(SqlSettingError, match="must declare it exactly once"):
        apply_sql_settings(SQL, {"lock_scan_skip_over": 1})


def test_the_catalogue_states_the_limit_and_the_loader_carries_it():
    stated = next(metric for metric in _metrics()
                  if metric["metric_code"] == "LOCK_TRANSACTION_HOLDERS")["sql_settings"]
    loaded = next(metric for metric in load_metric_definitions(CATALOGUE)
                  if metric.metric_code == "LOCK_TRANSACTION_HOLDERS")

    assert stated == {"lock_scan_skip_above": 500000}
    assert loaded.sql_settings == stated


def test_every_setting_the_catalogue_states_is_one_every_variant_of_its_metric_declares():
    """The general form: a metric given a setting tomorrow is held to the same rule, and so is a
    variant added to a metric that already has one."""
    checked = 0
    for metric in _metrics():
        settings = metric.get("sql_settings") or {}
        for variant in metric.get("variants") or []:
            if not settings or variant.get("supported", True) is False or not variant.get("file"):
                continue
            configured = apply_sql_settings(_text(variant["file"]), settings)
            for name, value in settings.items():
                assert re.search(rf"DECLARE @{name} \w+ = {value};", configured), (
                    f"{variant['file']} did not take sql_settings.{name}")
            checked += 1

    assert checked >= 2, "the two variants of LOCK_TRANSACTION_HOLDERS, at least"


def test_the_default_in_each_file_is_the_number_the_catalogue_ships():
    """Run by hand, the file behaves as a node with the shipped catalogue does."""
    for relative in VARIANTS:
        assert "DECLARE @lock_scan_skip_above bigint = 500000;" in _text(relative)


def test_the_reference_describes_the_new_field():
    """A whole-record entry reports a key it does not describe as unknown (rules R16)."""
    document = json.loads((PACKAGED_CATALOGUE / "shared_config_objects.json").read_text(
        encoding="utf-8-sig"))
    entry = next(item for item in document["shared_config_objects"]
                 if item.get("object") == "metric_definition")

    assert "sql_settings" in [field["field"] for field in entry["fields"]]
