from db_ops.metrics.models import MetricResult
from db_ops.metrics.storage import MetricStore


def test_fetch_latest_report_results_can_filter_active_metric_codes(tmp_path):
    store = MetricStore(tmp_path / "db_ops.sqlite")

    store.insert_results(
        run_id=1,
        results=[
            MetricResult(
                target_id="server/sqlserver/db",
                server_id="server",
                ip="127.0.0.1",
                db_type="sqlserver",
                db_name="db",
                metric_code="SQLSERVER_IO_LATENCY",
                metric_item="db",
                metric_value=None,
                metric_unit=None,
                status="WARNING",
                importance=5,
                message="SQL execution failed: old inactive metric",
                collected_at="2026-05-21T00:55:15Z",
            ),
            MetricResult(
                target_id="server/sqlserver/db",
                server_id="server",
                ip="127.0.0.1",
                db_type="sqlserver",
                db_name="db",
                metric_code="PERFORMANCE_IO_LATENCY",
                metric_item="db / ROWS / data.mdf",
                metric_value="1.00",
                metric_unit="ms",
                status="OK",
                importance=5,
                message=None,
                collected_at="2026-05-21T08:27:11Z",
            ),
        ],
    )

    rows = store.fetch_latest_report_results(metric_codes={"PERFORMANCE_IO_LATENCY"})

    assert [row["metric_code"] for row in rows] == ["PERFORMANCE_IO_LATENCY"]
    assert rows[0]["status"] == "OK"
    assert rows[0]["daily_report_created"] == 0


def test_fetch_latest_report_results_empty_metric_code_filter_returns_no_rows(tmp_path):
    store = MetricStore(tmp_path / "db_ops.sqlite")

    rows = store.fetch_latest_report_results(metric_codes=set())

    assert rows == []


def test_latest_successful_result_time_ignores_sql_execution_failed_warning(tmp_path):
    store = MetricStore(tmp_path / "db_ops.sqlite")
    store.insert_results(
        run_id=1,
        results=[
            MetricResult(
                target_id="server/sqlserver/db",
                server_id="server",
                ip="127.0.0.1",
                db_type="sqlserver",
                db_name="db",
                metric_code="PERFORMANCE_IO_LATENCY",
                metric_item="db",
                metric_value=None,
                metric_unit=None,
                status="WARNING",
                importance=5,
                message="SQL execution failed: collation conflict",
                collected_at="2026-05-21T00:55:15Z",
            )
        ],
    )

    latest = store.latest_successful_result_time(
        target_id="server/sqlserver/db",
        metric_code="PERFORMANCE_IO_LATENCY",
    )

    assert latest is None


def test_mark_daily_report_created_updates_selected_metric_rows(tmp_path):
    store = MetricStore(tmp_path / "db_ops.sqlite")
    store.insert_results(
        run_id=1,
        results=[
            MetricResult(
                target_id="server/sqlserver/db",
                server_id="server",
                ip="127.0.0.1",
                db_type="sqlserver",
                db_name="db",
                metric_code="PERFORMANCE_IO_LATENCY",
                metric_item="db",
                metric_value="1.00",
                metric_unit="ms",
                status="OK",
                importance=5,
                message=None,
                collected_at="2026-05-21T08:27:11Z",
            )
        ],
    )
    rows = store.fetch_latest_report_results(metric_codes={"PERFORMANCE_IO_LATENCY"})

    updated = store.mark_daily_report_created(result_ids=[int(rows[0]["result_id"])])
    refreshed = store.fetch_latest_report_results(metric_codes={"PERFORMANCE_IO_LATENCY"})

    assert updated == 1
    assert refreshed[0]["daily_report_created"] == 1


def test_insert_results_coerces_raw_command_metadata_to_sqlite_types(tmp_path):
    store = MetricStore(tmp_path / "db_ops.sqlite")

    class OddStream:
        def __str__(self):
            return "odd stream text"

    store.insert_results(
        run_id=1,
        results=[
            MetricResult(
                target_id="server/sqlserver/db",
                server_id="server",
                ip="127.0.0.1",
                db_type="sqlserver",
                db_name="db",
                metric_code="OS_CPU_USAGE",
                metric_item="server",
                metric_value=None,
                metric_unit=None,
                status="WARNING",
                importance=5,
                message="collection failed",
                collected_at="2026-05-21T08:27:11Z",
                raw_stdout=b"bytes out",
                raw_stderr=OddStream(),
                exit_code="1",
                execution_time="0.25",
            )
        ],
    )

    with store.connect() as conn:
        row = conn.execute(
            """
            SELECT raw_stdout, raw_stderr, exit_code, execution_time
            FROM metric_results
            WHERE metric_code = 'OS_CPU_USAGE';
            """
        ).fetchone()

    assert row["raw_stdout"] == "bytes out"
    assert row["raw_stderr"] == "odd stream text"
    assert row["exit_code"] == 1
    assert row["execution_time"] == 0.25
