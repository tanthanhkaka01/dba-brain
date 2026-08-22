"""What the metric store must survive writing, not just what it stores.

Every collected row from every target goes into ``metric_results`` as one ``executemany``. That
makes the write a single point of failure for the whole run: a value one server produced that the
store backend refuses does not cost that row, it costs every row behind it. The store is the
place to be forgiving about content, because the alternative is losing a monitoring cycle.
"""

from db_ops.db.metric_store import MetricResult, MetricStore


def test_a_nul_byte_in_a_message_does_not_kill_the_whole_run(tmp_path):
    """SQL Server nvarchar holds 0x0000 and sys.dm_exec_sql_text hands it back, so a metric that
    quotes the running statement can carry one. PostgreSQL rejects it outright ("invalid byte
    sequence for encoding UTF8: 0x00") and, since the rows go in as one executemany, that killed
    the entire collection — every metric for every remaining target — over one stray byte.
    """
    store = MetricStore(tmp_path / "m.sqlite")
    store.initialize()
    run_id = store.start_run(started_at="2026-08-03T00:00:00Z", status="running")

    dirty = MetricResult(
        target_id="t1", server_id="S1", ip="10.0.0.1", db_type="sqlserver", db_name="db",
        metric_code="LOCK_TRANSACTION_HOLDERS", metric_item="SPID=1\x00", metric_value="9\x00",
        metric_unit="lock(s)", status="WARNING", importance=3,
        message="last_or_running_sql=SELECT 1\x00 FROM t", collected_at="2026-08-03T00:00:00Z",
    )
    assert store.insert_results(run_id=run_id, results=[dirty]) == 1

    with store.connect() as conn:
        row = list(conn.execute(
            "SELECT metric_item, metric_value, message FROM metric_results"))[0]
    # The finding survives; only the byte that no reader wanted is gone.
    assert "\x00" not in row["metric_item"] and row["metric_item"] == "SPID=1"
    assert "\x00" not in row["metric_value"] and row["metric_value"] == "9"
    assert "\x00" not in row["message"]
    assert row["message"] == "last_or_running_sql=SELECT 1 FROM t"
