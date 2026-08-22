from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from db_ops.db import DbOpsStore
from db_ops.metrics.models import MetricResult
from db_ops.metrics.storage import MetricStore
from db_ops.reports import cli as reports_cli
from db_ops.reports import service
from db_ops.reports.metrics_reports import create_metric_history_report


NOW = datetime(2026, 7, 13, 6, 0, 0, tzinfo=timezone.utc)


def _sqlite_store_config(sqlite_path):
    """SQLite store declaration for a test config stand-in (mirrors DbOpsConfig.store)."""
    from pathlib import Path as _Path

    from db_ops.config import SqliteStoreConfig, StoreConfig

    return StoreConfig(sqlite=SqliteStoreConfig(path=_Path(str(sqlite_path))))


def metric_result(
    *,
    collected_at: str,
    server_id: str = "ACME-192-0-2-245",
    target_id: str = "ACME-192-0-2-245/sqlserver/SQL-SERVER",
    metric_code: str = "SYSTEM_CPU_MEMORY",
    metric_item: str = "cpu_percent",
    metric_value: str = "42",
) -> MetricResult:
    return MetricResult(
        target_id=target_id,
        server_id=server_id,
        ip="192.0.2.245",
        db_type="sqlserver",
        db_name="master",
        metric_code=metric_code,
        metric_item=metric_item,
        metric_value=metric_value,
        metric_unit="percent",
        status="OK",
        importance=1,
        message=f"{metric_item}={metric_value}",
        collected_at=collected_at,
    )


def insert_results(sqlite_path, rows):
    MetricStore(sqlite_path).insert_results(run_id=1, results=list(rows))


def app_config(sqlite_path):
    return SimpleNamespace(
        sqlite_path=sqlite_path,
        store=_sqlite_store_config(sqlite_path),
        telegram=SimpleNamespace(groups={"logging": "-100"}),
    )


def test_metric_history_cli_requires_filters_and_positive_hours():
    with pytest.raises(SystemExit):
        reports_cli.parse_args(["metric-history-report"])
    with pytest.raises(SystemExit):
        reports_cli.parse_args(
            [
                "metric-history-report",
                "--server-id",
                "server-1",
                "--metric-code",
                "SYSTEM_CPU_MEMORY",
                "--hours",
                "0",
            ]
        )

    args = reports_cli.parse_args(
        [
            "metric-history-report",
            "--server-id",
            "server-1",
            "--metric-code",
            "system_cpu_memory",
            "--hours",
            "6",
        ]
    )

    assert args.hours == 6
    assert args.summary_limit == 150
    assert args.dedupe_seconds == 0


def test_create_metric_history_report_filters_exact_scope_and_relative_window(tmp_path):
    sqlite_path = tmp_path / "runtime.sqlite"
    insert_results(
        sqlite_path,
        [
            metric_result(collected_at="2026-07-13T00:00:00Z", metric_value="10"),
            metric_result(collected_at="2026-07-13T03:00:00Z", metric_value="20"),
            metric_result(collected_at="2026-07-12T23:59:59Z", metric_value="before"),
            metric_result(collected_at="2026-07-13T06:00:01Z", metric_value="future"),
            metric_result(
                collected_at="2026-07-13T03:00:00Z",
                server_id="ACME-OTHER",
                target_id="ACME-OTHER/sqlserver/OTHER",
                metric_value="other-server",
            ),
            metric_result(
                collected_at="2026-07-13T03:00:00Z",
                metric_code="INSTANCE_STATUS",
                metric_value="other-metric",
            ),
            metric_result(
                collected_at="2026-07-13T04:00:00Z",
                target_id="ACME-192-0-2-245/sqlserver/SECOND-INSTANCE",
                metric_value="30",
            ),
        ],
    )

    result = create_metric_history_report(
        sqlite_path=sqlite_path,
        server_id="ACME-192-0-2-245",
        metric_code="system_cpu_memory",
        hours=6,
        now=NOW,
    )

    assert result["window_start"] == "2026-07-13T00:00:00Z"
    assert result["window_end"] == "2026-07-13T06:00:00Z"
    assert result["metric_code"] == "SYSTEM_CPU_MEMORY"
    assert result["row_count"] == 3
    assert result["target_ids"] == [
        "ACME-192-0-2-245/sqlserver/SECOND-INSTANCE",
        "ACME-192-0-2-245/sqlserver/SQL-SERVER",
    ]
    assert "other-server" not in result["report_text"]
    assert "other-metric" not in result["report_text"]
    assert "before" not in result["report_text"]
    assert "future" not in result["report_text"]


def test_metric_history_summary_limit_keeps_newest_rows_in_chronological_order(tmp_path):
    sqlite_path = tmp_path / "runtime.sqlite"
    insert_results(
        sqlite_path,
        [
            metric_result(collected_at="2026-07-13T01:00:00Z", metric_value="oldest"),
            metric_result(collected_at="2026-07-13T02:00:00Z", metric_value="middle"),
            metric_result(collected_at="2026-07-13T03:00:00Z", metric_value="newest"),
        ],
    )

    result = create_metric_history_report(
        sqlite_path=sqlite_path,
        server_id="ACME-192-0-2-245",
        metric_code="SYSTEM_CPU_MEMORY",
        hours=6,
        summary_limit=2,
        now=NOW,
    )

    report_text = result["report_text"]
    assert result["row_count"] == 3
    assert result["displayed_row_count"] == 2
    assert "1 older row(s) omitted" in report_text
    assert "oldest" not in report_text
    assert report_text.index("middle") < report_text.index("newest")


def test_metric_history_workflow_reads_stored_rows_and_queues_report_without_collection(tmp_path, monkeypatch):
    # The level -> chat_id map now comes from db_ops.common.cli telegram-group, not from
    # the config object, so stub the common helper to keep this test hermetic.
    monkeypatch.setattr("db_ops.reports.service.telegram_groups", lambda **_: {"logging": "-100"})
    monkeypatch.setattr("db_ops.reports.cli.telegram_groups", lambda **_: {"logging": "-100"})

    sqlite_path = tmp_path / "runtime.sqlite"
    insert_results(sqlite_path, [metric_result(collected_at="2026-07-13T05:30:00Z")])

    def fail_collect(**_kwargs):
        raise AssertionError("Metrics collection must not run for metric-history-report")

    monkeypatch.setattr(service, "collect_target_metrics", fail_collect)

    result = service.metric_history_report(
        config=app_config(sqlite_path),
        server_id="ACME-192-0-2-245",
        metric_code="SYSTEM_CPU_MEMORY",
        hours=1,
        dedupe_seconds=0,
        now=NOW,
    )

    queued = DbOpsStore(sqlite_path).fetch_pending_telegram_send_messages(limit=10)
    assert result["status"] == "success"
    assert result["exit_code"] == 0
    assert result["row_count"] == 1
    assert result["queued"] == 1
    assert len(queued) == 1
    assert queued[0]["tlgchat_id"] == "-100"
    assert queued[0]["message_text"].startswith("[Metric History Report]\n")


def test_metric_history_workflow_reports_empty_window_as_usage_error(tmp_path):
    with pytest.raises(service.ReportWorkflowError, match="No stored metric data found") as exc_info:
        service.metric_history_report(
            config=app_config(tmp_path / "runtime.sqlite"),
            server_id="ACME-192-0-2-245",
            metric_code="SYSTEM_CPU_MEMORY",
            hours=1,
            now=NOW,
        )

    assert exc_info.value.exit_code == 2
