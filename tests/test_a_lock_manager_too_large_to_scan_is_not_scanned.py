"""A lock manager too large to read whole is sampled, and who holds the locks is what is graded.

`LOCK_TRANSACTION_HOLDERS` reads `sys.dm_tran_locks`, and that costs by the lock. On 2026-10-07 one
session on an ERP server held 1,137,515 locks in a transaction 44 hours old; the metric's read
outlived its 60-second timeout and was the head blocker of 68 sessions. Until 1.1.0 it therefore read
nothing above a limit, named holders by the log they wrote, and was CRITICAL for the count alone - on
a server whose jobs build 2 to 20 million locks and end, and then for six days on end while five
sessions built 161 million.

What is held here, each from a measurement on the estate's servers (2026-10-09, 2026-11-23):

- **The size is read before the locks, from a counter that enumerates nothing** (`Lock Owner Blocks` -
  the locks in use, not the store's memory, which stays after they are gone: 30 GB with 14,000 locks).
- **Above the limit the first rows are sampled.** The view gives its rows as it walks the lock hash, so
  TOP ends it early, and the cost no longer depends on how many locks there are: 10,000 of 62,736 gave
  the two largest holders 59.8 % and 34.9 %, the whole 62.4 % and 35.7 %.
- **The size alone is not a finding.** A holder with a share of the sample is, by its transaction's age
  or by who waits on it; a sample that ends early is every lock there is, and the counter is behind.
- **No row per lock reaches tempdb.** Below the limit the locks are grouped as they are read, into a
  table made before; the six times this metric blocked others it was writing more than a million rows
  inside the statement that read them.
- **The limits are configuration**: `sql_settings`, a number and nothing else.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from dbabrain.lib.paths import PACKAGED_CATALOGUE
from dbabrain.lib.sql_text import SqlSettingError, apply_sql_settings
from dbabrain.metrics.definitions import load_metric_definitions

from conftest import present_copies

REPO_ROOT = Path(__file__).resolve().parents[1]
COLLECTORS = REPO_ROOT / "dbabrain" / "metrics" / "collectors"
CATALOGUE = REPO_ROOT / "dbabrain" / "metrics" / "catalogue" / "metric_definitions.json"
VARIANTS = ("sqlserver/025_sqlserver_transaction_lock_holders.sql",
            "sqlserver/legacy_2008r2/025_sqlserver_transaction_lock_holders.sql")

GUARD_STARTS = "-- 0. Is the lock manager too large to read whole?"
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
def test_both_variants_declare_lock_count_credible_locks_per_second_before_the_sample_branch(relative):
    text = _text(relative)
    decl = "DECLARE @lock_count_credible_locks_per_second bigint = 2000000;"
    assert decl in text
    assert text.index(decl) < text.index("IF ISNULL(@locks_in_use, 0) >= @lock_scan_skip_above")


@pytest.mark.parametrize("relative", VARIANTS)
def test_the_size_is_read_before_anything_enumerates_the_lock_manager(relative):
    code = _code(_text(relative))

    assert code.index("IF ISNULL(@locks_in_use, 0) >= @lock_scan_skip_above") \
        < code.index("sys.dm_tran_locks"), "the scan must not start before the guard has decided"


@pytest.mark.parametrize("relative", VARIANTS)
def test_the_sample_is_bounded_and_the_whole_read_never_follows_it(relative):
    guard = _code(_guard(relative))

    assert guard.count("sys.dm_tran_locks") == 1, "one bounded read"
    read = " ".join(guard[guard.index("SELECT TOP (@lock_sample_rows)"):].split())
    assert read.startswith("SELECT TOP (@lock_sample_rows) tl.request_session_id AS sid, tl.resource_type, "
                           "tl.request_status FROM sys.dm_tran_locks tl) x"), "TOP is on the view itself"
    assert "MAXDOP 1" in guard, "one reader walks the lock manager"
    assert guard.rstrip().endswith("RETURN;\nEND"), "without RETURN the whole read runs after the sample"


@pytest.mark.parametrize("relative", VARIANTS)
def test_a_connections_own_database_lock_is_not_a_holders(relative):
    """One per connection, and they come first: counted, 1000 rows were a third of them."""
    assert "x.resource_type <> 'DATABASE'" in _code(_guard(relative))


@pytest.mark.parametrize("relative", VARIANTS)
def test_the_sample_creates_no_temp_table(relative):
    """A server in this state is the last place to create one."""
    guard = _code(_guard(relative))

    assert "INTO #" not in guard and "CREATE TABLE" not in guard.upper()


@pytest.mark.parametrize("relative", VARIANTS)
def test_below_the_limit_the_locks_are_grouped_as_they_are_read_and_never_written_one_by_one(relative):
    code = _code(_text(relative))
    scan = " ".join(code[code.index("CREATE TABLE #locks"):].split())

    assert "INTO #locks FROM" not in scan, "SELECT ... INTO writes a row per lock inside the read"
    insert = scan[scan.index("INSERT INTO #locks"):scan.index("OPTION (HASH GROUP, MAXDOP 1);")]
    assert "COUNT_BIG(*) FROM sys.dm_tran_locks tl" in insert and "GROUP BY tl.request_session_id" in insert
    assert "SUM(tl.lock_count) AS lock_count" in scan, "what was counted by rows is summed"
    assert scan.count("COUNT(*) AS lock_count") == 0


@pytest.mark.parametrize("relative", VARIANTS)
def test_the_size_is_the_locks_in_use_not_the_memory_of_the_store(relative):
    """The store keeps its pages: 3,700 MB with 800 locks in use, two days after the incident."""
    guard = _code(_guard(relative))

    assert "counter_name = 'Lock Owner Blocks'" in guard
    assert "dm_os_memory_clerks" not in guard


# --------------------------------------------------------------------------- #
# What the sample says
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("relative", VARIANTS)
def test_the_sample_names_the_session_its_share_and_where_it_connects_from(relative):
    guard = _code(_guard(relative))

    assert "CAST('lock_scan_sampled' AS varchar(256)) AS metric_item" in guard
    for said in ("spid=", "share_pct=", "lock_count=", "blocked_sessions=", "login=", "host=", "client_ip=",
                 "program=", "database=", "open_tran_seconds=", "log_records="):
        assert said in guard, f"the rows do not say {said}"


@pytest.mark.parametrize("relative", VARIANTS)
def test_the_message_opens_with_how_many_locks_how_many_were_sampled_and_how_long_it_took(relative):
    """The operator, 2026-11-23: say how many million locks, how many were sampled, and the rest."""
    guard = " ".join(_code(_guard(relative)).split())

    for said in ("'locks in use ' + CAST(CAST(@locks_in_use / 1000000.0 AS decimal(12, 2))", "' M, read whole up to '",
                 "'; sampled '", "100.0 * @sample_rows / @locks_in_use", "@sample_ms / 1000.0", "'; lock memory '"):
        assert said.lstrip("'; ") in guard, said
    assert guard.count("+ @said") == 3, "each of the three openings says it"


@pytest.mark.parametrize("relative", VARIANTS)
def test_a_holder_is_graded_by_its_age_and_by_who_waits_never_by_the_count(relative):
    guard = " ".join(_code(_guard(relative)).split())

    assert ("CASE WHEN @count_credible = 0 OR h.share_pct < @lock_holder_min_share_pct THEN 0 "
            "WHEN h.open_tran_seconds >= @lock_holder_critical_seconds THEN 2 "
            "WHEN h.open_tran_seconds >= @critical_tran_seconds AND h.blocked_sessions > 0 THEN 2 "
            "WHEN h.open_tran_seconds >= @lock_holder_warning_seconds THEN 1 "
            "WHEN h.blocked_sessions > 0 THEN 1 ELSE 0 END AS grade") in guard
    assert ("CASE ISNULL((SELECT MAX(g0.grade) FROM Graded g0), 0) "
            "WHEN 2 THEN 'CRITICAL' WHEN 1 THEN 'WARNING' ELSE 'LOGGING' END") in guard, "the server's row is its worst holder's"


@pytest.mark.parametrize("relative", VARIANTS)
def test_a_sample_that_ended_early_is_every_lock_there_is(relative):
    """The counter keeps a past peak for minutes: what was read whole is the truth, and nothing is paged for it."""
    guard = " ".join(_code(_guard(relative)).split())

    assert "WHEN @sample_rows < @lock_sample_rows THEN 0" in guard
    assert "CASE WHEN @sample_rows < @lock_sample_rows OR @sample_locks = 0 THEN ISNULL(p.locks, 0)" in guard


@pytest.mark.parametrize("relative", VARIANTS)
def test_the_oldest_transaction_is_named_beside_the_holders(relative):
    assert "OR (h.rn_age = 1 AND h.open_tran_seconds > 0)" in _code(_guard(relative))


@pytest.mark.parametrize("relative", VARIANTS)
def test_the_sample_rows_have_the_columns_the_scan_rows_have(relative):
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


def test_the_catalogue_states_the_limit_on_the_sql_server_variants_and_the_loader_carries_it():
    """On the variants, not the metric: since 1.149 the metric has PostgreSQL and Oracle files too,
    which have no lock manager to guard, and a metric-wide setting would fail them."""
    metric = next(metric for metric in _metrics() if metric["metric_code"] == "LOCK_TRANSACTION_HOLDERS")
    loaded = next(metric for metric in load_metric_definitions(CATALOGUE)
                  if metric.metric_code == "LOCK_TRANSACTION_HOLDERS")
    expected = {"lock_scan_skip_above": 1000000, "lock_count_credible_locks_per_second": 2000000,
                "lock_sample_rows": 200000, "lock_holder_min_share_pct": 10,
                "lock_holder_warning_seconds": 1800, "lock_holder_critical_seconds": 14400}

    assert "sql_settings" not in metric
    for variant in metric["variants"]:
        if variant.get("db_type") == "sqlserver":
            assert variant["sql_settings"] == expected
        else:
            assert "sql_settings" not in variant
    assert loaded.sql_settings == {}
    assert {v.name: v.sql_settings for v in loaded.variants if v.db_type == "sqlserver"} == {
        "sqlserver_legacy_2008r2": expected, "sqlserver_modern_2012_plus": expected}


def test_every_setting_the_catalogue_states_is_one_every_variant_of_its_metric_declares():
    """The general form: a metric given a setting tomorrow is held to the same rule, and so is a
    variant added to a metric that already has one."""
    checked = 0
    for metric in _metrics():
        for variant in metric.get("variants") or []:
            # What the collector writes into this variant's file: the metric's, then its own.
            settings = {**(metric.get("sql_settings") or {}), **(variant.get("sql_settings") or {})}
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
        assert "DECLARE @lock_scan_skip_above bigint = 1000000;" in _text(relative)
        assert "DECLARE @lock_count_credible_locks_per_second bigint = 2000000;" in _text(relative)


def test_the_three_metric_definitions_files_state_the_credible_rate_and_the_limit_for_this_metric():
    files = present_copies(
        REPO_ROOT / "data" / "metric_definitions.json",
        REPO_ROOT / "data" / "metric_definitions.example.json",
        REPO_ROOT / "dbabrain" / "metrics" / "catalogue" / "metric_definitions.json",
    )
    for path in files:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
        metrics = next(v for v in document.values()
                       if isinstance(v, list) and v and isinstance(v[0], dict))
        metric = next(m for m in metrics if m["metric_code"] == "LOCK_TRANSACTION_HOLDERS")
        for variant in (v for v in metric["variants"] if v.get("db_type") == "sqlserver"):
            settings = variant.get("sql_settings") or {}
            assert settings.get("lock_count_credible_locks_per_second") == 2000000, (
                f"{path.name} {variant['name']} does not state lock_count_credible_locks_per_second: 2000000"
            )
            assert settings.get("lock_scan_skip_above") == 1000000


def test_apply_sql_settings_writes_a_configured_value_into_the_new_declare():
    sql = ("DECLARE @lock_scan_skip_above bigint = 500000;\n"
           "DECLARE @lock_count_credible_locks_per_second bigint = 2000000;\n")
    out = apply_sql_settings(sql, {"lock_count_credible_locks_per_second": 500000})
    assert "DECLARE @lock_count_credible_locks_per_second bigint = 500000;" in out
    for relative in VARIANTS:
        variant_out = apply_sql_settings(_text(relative), {"lock_count_credible_locks_per_second": 500000})
        assert "DECLARE @lock_count_credible_locks_per_second bigint = 500000;" in variant_out


def test_the_reference_describes_the_new_field():
    """A whole-record entry reports a key it does not describe as unknown (rules R16)."""
    document = json.loads((PACKAGED_CATALOGUE / "shared_config_objects.json").read_text(
        encoding="utf-8-sig"))
    entry = next(item for item in document["shared_config_objects"]
                 if item.get("object") == "metric_definition")

    assert "sql_settings" in [field["field"] for field in entry["fields"]]


def test_a_variant_setting_reaches_only_its_own_file():
    """The collector merges the metric's settings with those of the variant that runs: the
    PostgreSQL and Oracle files of this metric are prepared with nothing written into them."""
    from dbabrain.metrics.collector import _sql_settings_for

    metric = next(metric for metric in load_metric_definitions(CATALOGUE)
                  if metric.metric_code == "LOCK_TRANSACTION_HOLDERS")
    by_engine = {v.db_type: v for v in metric.variants if v.path is not None and v.db_type != "sqlserver"}
    sql_server = next(v for v in metric.variants if v.name == "sqlserver_modern_2012_plus")

    assert _sql_settings_for(metric, sql_server.path)["lock_scan_skip_above"] == 1000000
    for engine in ("postgresql", "oracle"):
        assert _sql_settings_for(metric, by_engine[engine].path) == {}
        apply_sql_settings(by_engine[engine].path.read_text(encoding="utf-8"), {})
