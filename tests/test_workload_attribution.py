"""Which database wrote the log and which statements did the work — read from metric_results only.

Asked on 2026-09-11 from one instance's page: WRITELOG was 36% of its waits and the plan cache had run
2.8 million executions in 15 minutes, and the page could not say which database or which statement.
Two collectors now record per-database log counters and per-query plan-cache totals, raw and
cumulative, and the report differences each entity against its own earlier sample.

What these tests hold it to is the honesty of that subtraction, because each way of getting it wrong
produces a table that looks plausible:

* a database or a query that restarted its counters must lose its interval, not show negative work
  or a burst that never happened;
* a query with no earlier sample is **not ranked** by its since-cached total — that would put a
  month of history beside an hour of work — unless all its plans were cached inside the window;
* nothing is called a percentile, and a missing counter is "not collected", never zero;
* the statement snippet survives the message parser intact, commas and all, and cannot overwrite the
  row's own figures.
"""

from __future__ import annotations

from pathlib import Path

from db_ops.reports import workload, workload_attribution as wa

T0 = "2026-09-11T05:00:00Z"
T1 = "2026-09-11T05:15:00Z"
T2 = "2026-09-11T05:30:00Z"
T3 = "2026-09-11T06:00:00Z"
SINCE = "2026-08-26 02:01:13"


def _log_row(stamp, database, *, since=SINCE, **counters):
    fields = ", ".join(f"{k}={v}" for k, v in counters.items())
    return {"metric_code": wa.LOG_BY_DATABASE_CODE, "metric_item": database,
            "metric_value": str(counters.get("log_bytes_flushed", 0)), "collected_at": stamp,
            "message": f"value=0, unit=bytes, counters_since={since}, {fields}, source=x, note=y"}


def _query_row(stamp, query_hash, *, first_cached="2026-08-26 02:03:27", text="SELECT 1",
               since=SINCE, db="SALESDB", obj=None, **totals):
    base = {"executions": 0, "cpu_ms": 0, "elapsed_ms": 0, "logical_reads": 0,
            "physical_reads": 0, "logical_writes": 0}
    base.update(totals)
    fields = ", ".join(f"{k}={v}" for k, v in base.items())
    extra = f", db_name={db}" + (f", object_name={obj}" if obj else "")
    return {"metric_code": wa.TOP_QUERIES_CODE, "metric_item": query_hash,
            "metric_value": str(base["cpu_ms"]), "collected_at": stamp,
            "message": (f"value=0, unit=ms, counters_since={since}, {fields}, min_elapsed_ms=1.5, "
                        f"max_elapsed_ms=900.0, plans=1, first_cached_utc={first_cached}, "
                        f"last_execution_utc=2026-09-11 05:59:00{extra}, source=dm_exec_query_stats, "
                        f"note=n, text={text}")}


def _window(result, key):
    return next(w for w in result["windows"] if w["key"] == key)


# ------------------------------------------------------------------ transaction log by database --

def test_each_database_gets_its_own_log_volume_latency_and_transaction_size():
    rows = [
        _log_row(T0, "APPDB", log_bytes_flushed=0, log_flushes=0, log_flush_waits=0,
                 write_transactions=0, log_writes=0, log_write_stall_ms=0),
        _log_row(T1, "APPDB", log_bytes_flushed=100 * 1024 * 1024, log_flushes=4000,
                 log_flush_waits=3900, write_transactions=25600, log_writes=4000,
                 log_write_stall_ms=20000),
        _log_row(T0, "tempdb", log_bytes_flushed=0, log_flushes=0, log_writes=0,
                 log_write_stall_ms=0),
        _log_row(T1, "tempdb", log_bytes_flushed=25 * 1024 * 1024, log_flushes=100, log_writes=100,
                 log_write_stall_ms=100),
    ]

    result = wa.build_log_by_database(rows)

    now = _window(result, "now")
    busiest = now["databases"][0]
    assert busiest["database"] == "APPDB", "sorted by log volume, the biggest writer first"
    assert busiest["logMb"] == 100.0 and busiest["sharePct"] == 80.0
    assert busiest["writeLatencyMs"] == 5.0, "stall over this window's writes, not over the uptime"
    assert busiest["avgTxnKb"] == 4.0, "100 MB over 25,600 write transactions is row-by-row commits"
    assert busiest["stallSharePct"] > 99


def test_a_counter_this_build_does_not_have_is_left_blank_not_zero():
    """tempdb above carries no write_transactions: its log per transaction is unknown, not 0 KB."""
    rows = [_log_row(T0, "tempdb", log_bytes_flushed=0, log_flushes=0),
            _log_row(T1, "tempdb", log_bytes_flushed=1024, log_flushes=1)]

    db = _window(wa.build_log_by_database(rows), "now")["databases"][0]

    assert db["avgTxnKb"] is None and db["writeLatencyMs"] is None


def test_a_database_whose_counters_reset_loses_its_interval_and_nobody_elses():
    """A restore or AUTO_CLOSE resets one database's counters without an engine restart."""
    rows = [
        _log_row(T0, "RESTORED", log_bytes_flushed=9_000_000, log_flushes=900),
        _log_row(T1, "RESTORED", log_bytes_flushed=1_000, log_flushes=1),
        _log_row(T0, "STEADY", log_bytes_flushed=1_000, log_flushes=1),
        _log_row(T1, "STEADY", log_bytes_flushed=2_000_000, log_flushes=10),
    ]

    names = [db["database"] for db in _window(wa.build_log_by_database(rows), "now")["databases"]]

    assert names == ["STEADY"]


def test_one_collection_is_not_an_interval_and_says_so():
    result = wa.build_log_by_database([_log_row(T1, "APPDB", log_bytes_flushed=5)])

    assert result["available"] is False and result["samples"] == 1


# ------------------------------------------------------------------------------ top queries -----

def test_a_query_is_ranked_by_what_it_did_in_the_window_not_since_it_was_cached():
    rows = [
        # Enormous history, little work in the window.
        _query_row(T0, "0xOLD", executions=1_000_000, cpu_ms=9_000_000),
        _query_row(T2, "0xOLD", executions=1_000_010, cpu_ms=9_000_100),
        # Small history, the real work of the window.
        _query_row(T0, "0xHOT", executions=10, cpu_ms=100),
        _query_row(T2, "0xHOT", executions=5_010, cpu_ms=600_100),
    ]

    queries = _window(wa.build_top_queries(rows), "now")["queries"]

    assert [q["hash"] for q in queries] == ["0xHOT", "0xOLD"]
    hot = queries[0]
    assert hot["cpuMs"] == 600_000 and hot["executions"] == 5_000
    assert hot["avgCpuMs"] == 120.0 and hot["ranks"]["cpu_ms"] == 1
    assert hot["basis"] == "delta"


def test_a_query_cached_inside_the_window_is_ranked_by_its_whole_total():
    """All of its plans are newer than the earlier sample, so every unit of its total was spent
    inside the window — the total is the interval, exactly."""
    rows = [
        _query_row(T0, "0xANY", executions=1, cpu_ms=1),
        _query_row(T2, "0xANY", executions=2, cpu_ms=2),
        _query_row(T2, "0xNEW", executions=40, cpu_ms=8_000, first_cached="2026-09-11 05:20:00"),
    ]

    new = next(q for q in _window(wa.build_top_queries(rows), "now")["queries"] if q["hash"] == "0xNEW")

    assert new["cpuMs"] == 8_000 and new["basis"] == "cached_in_window"


def test_a_query_with_no_earlier_sample_is_counted_but_never_ranked_by_its_history():
    """It existed at the earlier sample and was not in that ranking, so what it did in the window
    is unknown. Its since-cached total is a month; ranking by it would be the averaging mistake
    the whole Workload section exists to avoid."""
    rows = [
        _query_row(T0, "0xANY", executions=1, cpu_ms=1),
        _query_row(T2, "0xANY", executions=2, cpu_ms=2),
        _query_row(T2, "0xUNSEEN", executions=9_999_999, cpu_ms=99_999_999),
    ]

    window = _window(wa.build_top_queries(rows), "now")

    assert "0xUNSEEN" not in [q["hash"] for q in window["queries"]]
    assert window["unranked"] == 1


def test_a_query_that_lost_a_plan_to_eviction_is_not_shown_as_negative_work():
    rows = [
        _query_row(T0, "0xEVICTED", executions=500, cpu_ms=5_000),
        _query_row(T2, "0xEVICTED", executions=20, cpu_ms=300),
    ]

    window = _window(wa.build_top_queries(rows), "now")

    assert window["queries"] == [] and window["unranked"] == 1


def test_a_query_that_did_nothing_in_the_window_is_not_listed():
    rows = [_query_row(T0, "0xIDLE", executions=7, cpu_ms=70),
            _query_row(T2, "0xIDLE", executions=7, cpu_ms=70)]

    assert _window(wa.build_top_queries(rows), "now")["queries"] == []


def test_no_window_is_taken_across_an_engine_restart():
    rows = [_query_row(T0, "0xQ", executions=1, cpu_ms=10),
            _query_row(T2, "0xQ", executions=5, cpu_ms=50, since="2026-09-11 05:10:00")]

    result = wa.build_top_queries(rows)

    assert result["available"] is False


def test_each_ranking_is_its_own_top_list():
    rows = []
    for stamp, scale in ((T0, 0), (T2, 1)):
        rows.append(_query_row(stamp, "0xCPU", executions=1 + scale, cpu_ms=1 + 9_000 * scale,
                               logical_reads=1 + 10 * scale))
        rows.append(_query_row(stamp, "0xREADS", executions=1 + scale, cpu_ms=1 + 10 * scale,
                               logical_reads=1 + 90_000_000 * scale))

    by_hash = {q["hash"]: q for q in _window(wa.build_top_queries(rows), "now")["queries"]}

    assert by_hash["0xCPU"]["ranks"]["cpu_ms"] == 1 and by_hash["0xREADS"]["ranks"]["cpu_ms"] == 2
    assert by_hash["0xREADS"]["ranks"]["logical_reads"] == 1


def test_the_statement_snippet_survives_the_parser_and_cannot_overwrite_the_figures():
    """The collector puts the snippet last and spaces out every '=' in it. A statement reading
    `WHERE cpu_ms = 5, executions = 1` must stay text, and the row's own cpu_ms must stay the row's."""
    text = "UPDATE dbo.t SET a = 1, b = 2 WHERE cpu_ms = 5, executions = 1"
    rows = [_query_row(T0, "0xT", executions=1, cpu_ms=1, text=text),
            _query_row(T2, "0xT", executions=101, cpu_ms=10_001, text=text, obj="usp_Save")]

    query = _window(wa.build_top_queries(rows), "now")["queries"][0]

    assert query["text"] == text
    assert query["cpuMs"] == 10_000 and query["executions"] == 100
    assert query["object"] == "usp_Save"


def test_the_spread_is_the_since_cached_extremes_and_nothing_is_labelled_a_percentile():
    rows = [_query_row(T0, "0xQ", executions=1, cpu_ms=1),
            _query_row(T2, "0xQ", executions=11, cpu_ms=101)]

    query = _window(wa.build_top_queries(rows), "now")["queries"][0]

    assert (query["minElapsedMs"], query["maxElapsedMs"]) == (1.5, 900.0)
    assert not [key for key in query if "p95" in key.lower() or "percentile" in key.lower()]


def test_the_share_of_instance_cpu_is_a_ratio_of_rates():
    """Query samples are every 30 minutes and counter samples every 15, so their spans differ;
    rates over nearly the same window compare where totals over different spans would not."""
    rows = [_query_row(T0, "0xQ", executions=1, cpu_ms=0),
            _query_row(T2, "0xQ", executions=2, cpu_ms=900_000)]       # 500 ms/s over 30 min

    query = _window(wa.build_top_queries(rows, instance_cpu_ms_per_sec={"now": 2_000.0}),
                    "now")["queries"][0]

    assert query["cpuSharePct"] == 25.0


def test_the_hour_window_reaches_for_the_widest_honest_pair():
    rows = [_query_row(stamp, "0xQ", executions=n, cpu_ms=n * 10)
            for stamp, n in ((T0, 1), (T2, 100), (T3, 400))]

    result = wa.build_top_queries(rows)

    assert _window(result, "now")["queries"][0]["executions"] == 300
    assert _window(result, "hour")["queries"][0]["executions"] == 399
    assert _window(result, "hour")["seconds"] == 3600


# ------------------------------------------------------------------------------- wiring ---------

def test_the_fleet_overlay_does_not_load_a_row_per_statement():
    """WORKLOAD_CODES is fetched for every server by the fleet overlay, which renders none of it."""
    assert not set(wa.ATTRIBUTION_CODES) & set(workload.WORKLOAD_CODES)


def test_the_server_page_payload_carries_both_blocks():
    from db_ops.reports import server_report

    rows = [_log_row(T0, "APPDB", log_bytes_flushed=0, log_flushes=0),
            _log_row(T1, "APPDB", log_bytes_flushed=1024, log_flushes=1),
            _query_row(T0, "0xQ", executions=1, cpu_ms=1),
            _query_row(T2, "0xQ", executions=2, cpu_ms=2)]

    payload = server_report.build_payload([], [], workload_rows=rows)

    assert payload["attribution"]["log"]["available"] is True
    assert payload["attribution"]["queries"]["available"] is True


def test_the_collector_keeps_the_snippet_last_and_neutralised():
    """The two properties the report's parsing depends on, pinned in the SQL itself."""
    sql = (Path(wa.__file__).parents[1] / "metrics" / "collectors" / "sqlserver"
           / "077_sqlserver_top_queries.sql").read_text(encoding="utf-8")
    message = sql[sql.index("AS metric_unit"):sql.index("AS message")]

    assert message.rindex("', text=") > message.rindex("', note="), "text must be the last field"
    assert "'=', ' = '" in sql, "every '=' in the statement must be spaced out"
