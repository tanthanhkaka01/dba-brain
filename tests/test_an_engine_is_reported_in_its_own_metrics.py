"""An Oracle or PostgreSQL server is reported in the metrics it has, not in the ones it lacks.

The fleet page and the per-server page were written in SQL Server's vocabulary - page life
expectancy, TempDB, the Agent's jobs. On 2026-10-09 an Oracle 8i server had 26 metric codes in the
store and the fleet page loaded 9 of them; a PostgreSQL server 22, and 9. What those engines
collect under their own names - the process ceiling, the tablespaces, the wraparound horizon, the
vacuum backlog - sat in the store with a status and a sentence each, and reached neither page's
summary. A reader saw "not collected" eleven times and concluded the tool knew nothing.

So three things are held here:

- **the fleet page quotes each engine's own metrics**, one line per metric, in the collector's
  words (`build_engine_facts`) - and says *none found* where a collector ran and found nothing,
  which is not the same as not having run;
- **the per-server page has a tile for each of those questions**, shown only where the metric
  exists, so a SQL Server page is exactly what it was;
- **"not collected" is said only when something could have been collected**: an area whose
  collectors report only trouble says *none found* when they ran clean, and an area the engine has
  no metric for says *no such metric*.

Row shapes below are the ones the collectors wrote on 2026-10-09, with the names replaced.
"""

from __future__ import annotations

import pytest

from dbabrain.lib.inventory_render import HEALTH_BLOCKS
from dbabrain.reports import inventory_health as ih
from dbabrain.reports import server_health as sh
from dbabrain.reports.inventory_report import _build_detail
from dbabrain.reports.server_series import metric_catalog

AT = "2026-10-09T04:20:00Z"


@pytest.fixture(autouse=True)
def _the_catalogue_that_ships(shipped_metric_catalog):
    """Every test here reads the metric catalogue the package ships.

    Left to its default, `metric_catalog()` - and the areas built from it - reads
    `data/metric_definitions.json`, a node's own file that a public checkout does not have. This
    file passed here and failed eleven tests in `dba-brain` on 0.28.0's release day, each on an
    empty catalogue.
    """
    return shipped_metric_catalog


def _row(value, unit, message, status="OK"):
    return {"metric_value": value, "metric_unit": unit, "message": message, "status": status,
            "collected_at": AT}


ORACLE = {
    ("PROCESS_LIMIT", "processes"): _row(
        "274", "count", "resource=processes, current=274, max_seen=276, limit= 850"),
    ("PROCESS_LIMIT", "sessions"): _row(
        "271", "count", "resource=sessions, current=271, max_seen=273, limit= 940"),
    ("TABLESPACE_FREE_SPACE", "APP_TS"): _row(
        "88.98", "MB", "tablespace=APP_TS, effective_free_mb=88.98 (1.9% of max)", "CRITICAL"),
    ("TABLESPACE_FREE_SPACE", "SYSTEM"): _row(
        "32517.22", "MB", "tablespace=SYSTEM, effective_free_mb=32517.22 (99.2% of max)"),
    ("TABLESPACE_FREE_SPACE", "USERS"): _row(
        "1200", "MB", "tablespace=USERS, effective_free_mb=1200 (40% of max)"),
    ("SHARED_POOL_FREE", "shared pool:free memory"): _row(
        "9.21", "MB", "pool=shared pool, name=free memory, free_mb=9.21", "WARNING"),
    ("BUFFER_CACHE_HIT", "buffer_cache_hit_ratio"): _row(
        "96.6", "percent", "db_block_gets=1771823, consistent_gets=1071131275"),
    ("INVALID_OBJECTS", "APP/VIEW"): _row("73", "objects", "owner=APP, object_type=VIEW", "WARNING"),
    ("INVALID_OBJECTS", "APP/FUNCTION"): _row("2", "objects", "owner=APP, object_type=FUNCTION",
                                             "WARNING"),
    # A collector that ran and found nothing writes one row with no item.
    ("INDEX_UNUSABLE", None): _row(None, None, "SQL returned no rows."),
    ("INDEX_INVENTORY", "summary"): _row(
        "176", "summary", "indexes_total=176, unusable=0, unique_indexes=89, never_analyzed=6"),
    ("INDEX_INVENTORY", "APP.IX_ONE"): _row("VALID", "index", "type_desc=NORMAL, table=APP.T1"),
    ("STORAGE_DATA_FILE_SPACE", "USERS:/u01/users01.dbf"): _row(
        "1024", "MB", "tablespace=USERS, file=/u01/users01.dbf, size_mb=1024"),
    ("STORAGE_DATA_FILE_SPACE", "SYSTEM:/u01/system01.dbf"): _row(
        "512", "MB", "tablespace=SYSTEM, file=/u01/system01.dbf, size_mb=512"),
    ("DATABASE_STATUS", "ORCL"): _row(
        "READ WRITE", None,
        "database=ORCL, open_mode=READ WRITE, log_mode=NOARCHIVELOG, compatible=8.1.0"),
}

POSTGRESQL = {
    ("POSTGRES_XID_WRAPAROUND", "appdb"): _row(
        "13907692", "transactions", "xid_age=13907692, freeze_max_age=200000000"),
    ("POSTGRES_XID_WRAPAROUND", "olddb"): _row(
        "160000000", "transactions", "xid_age=160000000, freeze_max_age=200000000", "WARNING"),
    ("POSTGRES_VACUUM_HEALTH", "appdb.events"): _row(
        "1907227", "count", "live_tuples=14230038, dead_tuples=1907227", "CRITICAL"),
    ("POSTGRES_LONG_TRANSACTIONS", None): _row(None, None, "SQL returned no rows."),
    ("POSTGRES_WAL_ARCHIVE", "archiver"): _row(
        "0", "count", "archive_mode=on, archived_count=8093, failed_count=0"),
    ("DATABASE_STATUS", "appdb"): _row(
        "ONLINE", None, "database=appdb, connections_allowed=true, size=34 GB, database_id=5"),
    ("DATABASE_DATA_SIZE", "appdb"): _row("33.27", "GB", "bytes=35721926335"),
}

SQL_SERVER = {
    ("INSTANCE_CONNECTIONS", "server"): _row("212", "sessions", "active_sessions=212"),
    ("PAGE_LIFE_EXPECTANCY", "page_life_expectancy"): _row("5400", "seconds", ""),
    ("STORAGE_DATA_FILE_SPACE", "appdb / ROWS / D:\\data\\appdb.mdf"): _row(
        "61.5", "pct", "database=appdb, file=D:\\data\\appdb.mdf, used_pct=61.5"),
}


def _facts(code_map):
    return {fact["code"]: fact for fact in ih.build_engine_facts(code_map)}


# --------------------------------------------------------------------------- #
# The fleet page: one line per metric the engine has
# --------------------------------------------------------------------------- #
def test_an_oracle_record_says_what_oracle_collected():
    facts = _facts(ORACLE)

    assert list(facts) == ["PROCESS_LIMIT", "TABLESPACE_FREE_SPACE", "SHARED_POOL_FREE",
                           "BUFFER_CACHE_HIT", "INVALID_OBJECTS", "INDEX_UNUSABLE",
                           "INDEX_INVENTORY"]
    assert "processes 274 count — resource=processes, current=274" in facts["PROCESS_LIMIT"]["text"]
    assert "sessions 271 count" in facts["PROCESS_LIMIT"]["text"]


def test_a_sql_server_record_gains_nothing():
    """Every code is exclusive to its engine, so the page for SQL Server is what it was."""
    assert ih.build_engine_facts(SQL_SERVER) == []
    assert _build_detail({"engine_facts": ih.build_engine_facts(SQL_SERVER)}, {}) == []


def test_free_space_is_spoken_for_by_the_tablespace_with_the_least():
    """The largest number is the wrong row for a metric that counts what is left."""
    fact = _facts(ORACLE)["TABLESPACE_FREE_SPACE"]

    assert fact["status"] == "CRITICAL"
    assert fact["text"].startswith("3 tablespaces (1 CRITICAL) · APP_TS 88.98 MB")


def test_among_rows_of_equal_status_the_smallest_free_space_is_quoted():
    healthy = {key: row for key, row in ORACLE.items() if key[1] != "APP_TS"}

    assert "· USERS 1200 MB" in _facts(healthy)["TABLESPACE_FREE_SPACE"]["text"]


def test_an_age_is_spoken_for_by_the_oldest():
    fact = _facts(POSTGRESQL)["POSTGRES_XID_WRAPAROUND"]

    assert fact["status"] == "WARNING"
    assert "olddb 160000000 transactions" in fact["text"]


def test_a_ranking_names_its_largest_without_their_sentences():
    assert _facts(ORACLE)["INVALID_OBJECTS"]["text"] == (
        "2 owner and type groups (2 WARNING) · APP/VIEW 73 objects · APP/FUNCTION 2 objects")


def test_the_value_is_stated_even_when_the_sentence_does_not_repeat_it():
    """The buffer cache row's message is the three counters behind the ratio, not the ratio."""
    assert _facts(ORACLE)["BUFFER_CACHE_HIT"]["text"].startswith("buffer_cache_hit_ratio 96.6 percent")


def test_an_index_inventory_is_its_summary_row_never_its_thousand_detail_rows():
    assert _facts(ORACLE)["INDEX_INVENTORY"]["text"] == (
        "indexes_total=176, unusable=0, unique_indexes=89, never_analyzed=6")


def test_a_collector_that_ran_and_found_nothing_says_none_found():
    """Not the same fact as not having run: for these metrics it is the good answer."""
    assert _facts(ORACLE)["INDEX_UNUSABLE"]["text"] == "none found"
    assert _facts(POSTGRESQL)["POSTGRES_LONG_TRANSACTIONS"]["text"] == "none found"


def test_a_metric_with_no_row_at_all_writes_no_line():
    """Not collected here: an Oracle record gets no PostgreSQL line, and no empty one."""
    assert "POSTGRES_XID_WRAPAROUND" not in _facts(ORACLE)
    assert "PROCESS_LIMIT" not in _facts(POSTGRESQL)


def test_a_collector_that_failed_says_so():
    failed = {("POSTGRES_WAL_ARCHIVE", None): _row(
        None, None, "postgresql connect to 192.0.2.9:5432 failed: timeout", "CRITICAL")}

    fact = _facts(failed)["POSTGRES_WAL_ARCHIVE"]
    assert fact["status"] == "CRITICAL" and fact["text"].startswith("postgresql connect to")


def test_the_detail_puts_the_status_in_front_when_it_is_not_ok():
    detail = dict(_build_detail({"engine_facts": ih.build_engine_facts(ORACLE)}, {}))

    assert detail["Pool free memory"].startswith("WARNING · shared pool:free memory 9.21 MB")
    assert detail["Unusable indexes"] == "none found"


def test_every_quoted_metric_is_loaded_merged_and_exclusive_to_its_engine():
    """Three lists that have to agree, or a line is promised and never arrives - or arrives on a
    SQL Server page.
    """
    codes = [code for code, _label, _noun, _how in ih.ENGINE_FACTS]
    by_code = {entry["code"]: entry for entry in metric_catalog()}

    assert not [code for code in codes if code not in ih.HEALTH_CODES], "not loaded for the page"
    assert "engine_facts" in HEALTH_BLOCKS, "the block reaches the overlay and stops there"
    assert not [code for code in codes if "sqlserver" in by_code[code]["db_types"]]
    assert len(codes) == len(set(codes))


# --------------------------------------------------------------------------- #
# The database row
# --------------------------------------------------------------------------- #
def test_an_oracle_database_has_a_size_and_a_log_mode():
    database = ih.build_database_health(ORACLE)[0]

    assert database["data_size_gb"] == 1.5, "the sum of its data files"
    assert database["recovery_model"] == "NOARCHIVELOG"
    assert database["compatibility_level"] == "8.1.0"


def test_a_postgresql_database_has_no_compatibility_level_rather_than_zero():
    database = ih.build_database_health(POSTGRESQL)[0]

    assert database["compatibility_level"] is None
    assert database["data_size_gb"] == 33.27


def test_a_data_file_row_that_names_no_tablespace_is_not_an_oracle_file():
    """SQL Server writes STORAGE_DATA_FILE_SPACE too, in percent, and has its own size metric."""
    assert ih._datafile_size_gb(SQL_SERVER) is None


# --------------------------------------------------------------------------- #
# The per-server page: a tile where the metric exists
# --------------------------------------------------------------------------- #
def _entry(code, item, last, unit, status="OK", label=None):
    return {"code": code, "item": item, "label": label or code, "unit": unit, "status": status,
            "last": last, "numeric": True, "lastText": str(last), "message": "", "lastAt": 1}


ORACLE_SERIES = [
    _entry("INSTANCE_STATUS", "orcl", None, "", label="Instance up"),
    _entry("PROCESS_LIMIT", "processes", 274, "count"),
    _entry("TABLESPACE_FREE_SPACE", "SYSTEM", 32517.22, "MB"),
    _entry("TABLESPACE_FREE_SPACE", "APP_TS", 88.98, "MB"),
    _entry("SHARED_POOL_FREE", "shared pool:free memory", 9.21, "MB", "WARNING"),
    _entry("SHARED_POOL_FREE", "large pool:free memory", 0.59, "MB"),
]
SQL_SERVER_SERIES = [
    _entry("INSTANCE_STATUS", "ERPDB01", None, "", label="Instance up"),
    _entry("LOCK_BLOCKING_SESSIONS", "server", 0, "blocked_sessions"),
]


def _areas(series, freshness=None):
    return {area["key"]: area for area in sh.build_areas(series, freshness=freshness)}


def test_a_sql_server_page_has_the_thirteen_areas_it_had():
    assert list(_areas(SQL_SERVER_SERIES)) == [
        "availability", "cpu", "memory", "disk_space", "disk_latency", "storage_activity",
        "blocking", "long_queries", "backup", "jobs", "tempdb", "security", "ha"]


def test_an_oracle_page_gains_a_tile_for_each_question_oracle_answers():
    areas = _areas(ORACLE_SERIES)

    assert {"oracle_processes", "oracle_tablespaces", "oracle_sga"} <= set(areas)
    assert not [key for key in areas if key.startswith("postgres_")]
    assert areas["oracle_processes"]["value"] == "274 count"


def test_a_tablespace_tile_shows_the_one_with_the_least_free():
    assert _areas(ORACLE_SERIES)["oracle_tablespaces"]["sourceItem"] == "APP_TS"


def test_the_large_pool_at_half_a_megabyte_does_not_decide_the_shared_pool_tile():
    """It is logged, not judged, and half a megabyte free is its normal state."""
    tile = _areas(ORACLE_SERIES)["oracle_sga"]

    assert (tile["status"], tile["sourceItem"]) == ("WARNING", "shared pool:free memory")


def _freshness(*codes, not_collected=()):
    return {"metrics": [{"code": code, "state": "OK"} for code in codes],
            "notCollected": [{"code": code} for code in not_collected]}


def test_an_area_whose_metric_ran_clean_says_none_found_not_not_collected():
    """Oracle's and PostgreSQL's blocking collectors write a row only when something is blocked."""
    blocking = _areas(ORACLE_SERIES, _freshness("INSTANCE_STATUS", "LOCK_BLOCKING_SESSIONS"))["blocking"]

    assert (blocking["status"], blocking["value"]) == ("OK", "none found")


def test_without_evidence_that_it_ran_the_area_is_still_not_collected():
    """The metric fits this engine and produced nothing in the window: that is the real gap."""
    blocking = _areas(ORACLE_SERIES, _freshness(
        "INSTANCE_STATUS", not_collected=("LOCK_BLOCKING_SESSIONS",)))["blocking"]

    assert (blocking["status"], blocking["value"]) == ("UNKNOWN", "not collected")


def test_a_postgresql_tile_appears_once_its_metric_ran_even_with_nothing_to_show():
    areas = _areas(ORACLE_SERIES[:1], _freshness("INSTANCE_STATUS", "POSTGRES_LONG_TRANSACTIONS"))

    assert areas["postgres_transactions"]["value"] == "none found"
    assert "postgres_indexes" not in areas, "that metric never ran here"


def test_an_area_the_engine_has_no_metric_for_says_so():
    """TempDB on PostgreSQL: nothing fits the engine, so there is no collector to go looking for."""
    areas = _areas(ORACLE_SERIES[:1], _freshness("INSTANCE_STATUS", "POSTGRES_XID_WRAPAROUND"))

    assert areas["tempdb"]["value"] == "no such metric"
    assert areas["tempdb"]["detail"] == "this engine has no metric for this area"
    assert areas["jobs"]["value"] == "no such metric"


def test_an_area_the_engine_should_have_and_has_not_is_not_collected():
    areas = _areas(ORACLE_SERIES[:1], _freshness("INSTANCE_STATUS",
                                                 not_collected=("STORAGE_TEMP_SPACE",)))

    assert areas["tempdb"]["value"] == "not collected"


def test_an_area_fed_by_the_operating_system_is_never_called_engineless():
    """CPU exists on every server; with no host login it is simply not collected."""
    areas = _areas(ORACLE_SERIES[:1], _freshness("INSTANCE_STATUS"))

    assert areas["cpu"]["value"] == "not collected"
    assert areas["storage_activity"]["value"] == "not collected"


@pytest.mark.parametrize("spec", [spec for spec in sh.AREAS if spec.get("only_when_collected")],
                         ids=lambda spec: spec["key"])
def test_an_engine_area_is_built_from_metrics_sql_server_does_not_have(spec):
    """What keeps a SQL Server page unchanged is a property of the catalogue, so it is checked."""
    by_code = {entry["code"]: entry for entry in metric_catalog()}
    shared = [selector["code"] for selector in spec["selectors"]
              if "sqlserver" in by_code[selector["code"]]["db_types"]]

    assert shared == [], f"{spec['key']} would put a tile on every SQL Server page"
