import json
from datetime import datetime, timedelta, timezone

import pytest

from db_ops.db import DbOpsStore
from db_ops.metrics.models import MetricDefinition, MetricResult, MetricTarget
from db_ops.metrics.storage import MetricStore
from db_ops.reports.metrics_reports import (
    DEFAULT_REPORTS_CONFIG_PATH,
    TELEGRAM_SAFE_TEXT_LENGTH,
    create_backup_health_report,
    create_metric_report_for_level,
    create_metrics_reports,
    load_report_configs,
    push_report_alerts,
    run_scheduled_reports,
    split_telegram_message,
)


class FrozenDateTime(datetime):
    fixed_now = datetime(2026, 6, 4, 3, 30, 0, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return cls.fixed_now.replace(tzinfo=None)
        return cls.fixed_now.astimezone(tz)


def insert_backup_health_report(sqlite_path):
    store = DbOpsStore(sqlite_path)
    return store.insert_report(
        report_code="BACKUP_HEALTH",
        report_name="Backup health report",
        report_type="BACKUP_HEALTH",
        report_level="logging",
        report_text="[BACKUP HEALTH]\nOK",
        source_type="metric_results",
        source_id="backup_health:2026-05-23",
    )


def test_push_report_alerts_queues_backup_health_to_logging_group(tmp_path):
    sqlite_path = tmp_path / "runtime.sqlite"
    report_id = insert_backup_health_report(sqlite_path)

    result = push_report_alerts(
        sqlite_path=sqlite_path,
        telegram_groups={"logging": "-100"},
        dedupe_seconds=0,
    )

    store = DbOpsStore(sqlite_path)
    queued = store.fetch_pending_telegram_send_messages(limit=10)
    assert result["queued"] == 1
    assert result["pushed"] == 1
    assert result["sent"] == 0
    assert result["next_step"]
    assert result["pushed_report_ids"] == [report_id]
    assert len(queued) == 1
    assert queued[0]["tlgchat_id"] == "-100"
    assert queued[0]["message_text"] == "[BACKUP HEALTH]\nOK"
    assert queued[0]["source_type"] == "reports"
    assert queued[0]["source_id"] == "backup_health:2026-05-23"


def write_db_instances(path, rows):
    path.mkdir(parents=True, exist_ok=True)
    (path / "db_instances.json").write_text(json.dumps({"db_instances": rows}, ensure_ascii=False), encoding="utf-8")


def write_report_config(path, *, report_code="rp_metric_daily_logging", allowed_from_hour=10, allowed_to_hour=11, metric_max_age=600):
    path.write_text(
        json.dumps(
            {
                "reports": [
                    {
                        "report_code": report_code,
                        "display_name": "Daily Metrics Logging Summary",
                        "time_window": {
                            "from_year": None,
                            "to_year": None,
                            "from_month": None,
                            "to_month": None,
                            "from_day": 1,
                            "to_day": 31,
                            "from_hour": allowed_from_hour,
                            "to_hour": allowed_to_hour,
                            "from_minute": None,
                            "to_minute": None,
                            "repeat_interval": 86400,
                            "timeout": 1800,
                        },
                        "metric_max_age_seconds": metric_max_age,
                        "active": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def insert_logging_metric(sqlite_path, *, collected_at):
    target = MetricTarget(
        target_id="server/sqlserver/db",
        server_id="server",
        ip="127.0.0.1",
        db_type="sqlserver",
        db_name="db",
        credential_name="test",
        connection_info={"server_name": "server"},
    )
    definition = MetricDefinition(
        metric_code="INSTANCE_STATUS",
        db_type="sqlserver",
        category="instance",
        default_importance=1,
        active=True,
    )
    MetricStore(sqlite_path).insert_results(
        run_id=1,
        results=[
            MetricResult(
                target_id=target.target_id,
                server_id=target.server_id,
                ip=target.ip,
                db_type=target.db_type,
                db_name=target.db_name,
                metric_code=definition.metric_code,
                metric_item="server",
                metric_value="1",
                metric_unit=None,
                status="OK",
                importance=1,
                message="server=server",
                collected_at=collected_at,
            )
        ],
    )
    return target, definition


def test_report_config_requires_metric_max_age_seconds(tmp_path):
    reports_config_path = tmp_path / "reports_config.json"
    reports_config_path.write_text(
        """
        {
          "reports": [
            {
              "report_code": "rp_metric_hourly_warning",
              "display_name": "Metrics Warning Report",
              "time_window": {
                "from_day": 1,
                "to_day": 31,
                "from_hour": 0,
                "to_hour": 23,
                "repeat_interval": 1800,
                "timeout": 1800
              },
              "active": true
            }
          ]
        }
        """,
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="metric_max_age_seconds"):
        load_report_configs(reports_config_path)


def test_run_scheduled_reports_queues_once_then_skips_by_report_state(tmp_path, monkeypatch):
    sqlite_path = tmp_path / "runtime.sqlite"
    reports_config_path = tmp_path / "reports_config.json"
    write_report_config(reports_config_path, allowed_from_hour=0, allowed_to_hour=23)
    target, definition = insert_logging_metric(
        sqlite_path,
        collected_at=(datetime.now(timezone.utc) - timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    monkeypatch.setattr("db_ops.reports.metrics_reports.load_metric_targets", lambda **_: [target])
    monkeypatch.setattr("db_ops.reports.metrics_reports.load_metric_definitions", lambda *_, **__: [definition])

    first = run_scheduled_reports(
        sqlite_path=sqlite_path,
        telegram_groups={"logging": "-100"},
        reports_config_path=reports_config_path,
        summary_limit=150,
    )
    second = run_scheduled_reports(
        sqlite_path=sqlite_path,
        telegram_groups={"logging": "-100"},
        reports_config_path=reports_config_path,
        summary_limit=150,
    )

    queued = DbOpsStore(sqlite_path).fetch_pending_telegram_send_messages(limit=10)
    assert first["created"] == 1
    assert first["queued"] == 1
    assert second["created"] == 0
    assert second["queued"] == 0
    assert "repeat interval not elapsed" in second["reports"][0]["skipped_reason"]
    assert len(queued) == 1
    assert queued[0]["metadata_json"]


def test_daily_logging_report_runs_only_inside_allowed_window(tmp_path, monkeypatch):
    sqlite_path = tmp_path / "runtime.sqlite"
    reports_config_path = tmp_path / "reports_config.json"
    write_report_config(reports_config_path, allowed_from_hour=10, allowed_to_hour=11, metric_max_age=600)
    target, definition = insert_logging_metric(sqlite_path, collected_at="2026-06-04T03:29:00Z")
    monkeypatch.setattr("db_ops.reports.metrics_reports.datetime", FrozenDateTime)
    monkeypatch.setattr("db_ops.reports.metrics_reports.load_metric_targets", lambda **_: [target])
    monkeypatch.setattr("db_ops.reports.metrics_reports.load_metric_definitions", lambda *_, **__: [definition])

    result = run_scheduled_reports(
        sqlite_path=sqlite_path,
        telegram_groups={"logging": "-100"},
        reports_config_path=reports_config_path,
        summary_limit=150,
    )

    assert result["created"] == 1
    assert result["queued"] == 1
    assert result["reports"][0]["report_code"] == "rp_metric_daily_logging"


def test_daily_logging_report_uses_metric_max_age_as_summary_window(tmp_path, monkeypatch):
    sqlite_path = tmp_path / "runtime.sqlite"
    reports_config_path = tmp_path / "reports_config.json"
    write_report_config(reports_config_path, allowed_from_hour=10, allowed_to_hour=11, metric_max_age=86400)
    target, definition = insert_logging_metric(sqlite_path, collected_at="2026-06-04T03:29:00Z")
    MetricStore(sqlite_path).insert_results(
        run_id=2,
        results=[
            MetricResult(
                target_id=target.target_id,
                server_id=target.server_id,
                ip=target.ip,
                db_type=target.db_type,
                db_name=target.db_name,
                metric_code=definition.metric_code,
                metric_item="server",
                metric_value="1",
                metric_unit=None,
                status="OK",
                importance=1,
                message="older same-day collect",
                collected_at="2026-06-03T03:31:00Z",
            ),
            MetricResult(
                target_id=target.target_id,
                server_id=target.server_id,
                ip=target.ip,
                db_type=target.db_type,
                db_name=target.db_name,
                metric_code=definition.metric_code,
                metric_item="server",
                metric_value="1",
                metric_unit=None,
                status="WARNING",
                importance=1,
                message="warning in window",
                collected_at="2026-06-04T03:28:00Z",
            ),
        ],
    )
    monkeypatch.setattr("db_ops.reports.metrics_reports.datetime", FrozenDateTime)
    monkeypatch.setattr("db_ops.reports.metrics_reports.load_metric_targets", lambda **_: [target])
    monkeypatch.setattr("db_ops.reports.metrics_reports.load_metric_definitions", lambda *_, **__: [definition])
    monkeypatch.setattr("db_ops.reports.metrics_reports._metric_config_intervals", lambda: {"INSTANCE_STATUS": 60})

    result = run_scheduled_reports(
        sqlite_path=sqlite_path,
        telegram_groups={"logging": "-100"},
        reports_config_path=reports_config_path,
        summary_limit=150,
    )

    store = DbOpsStore(sqlite_path)
    queued = store.fetch_pending_telegram_send_messages(limit=10)
    report_text = queued[0]["message_text"]
    rows = MetricStore(sqlite_path).fetch_latest_results(limit=10)
    assert result["created"] == 1
    assert report_text.startswith("[Daily Metrics Logging Summary]\n")
    assert "[METRICS SUMMARY]" not in report_text
    assert "Duration: 86400 sec" in report_text
    assert "- INSTANCE_STATUS / 60s" in report_text
    assert "OK: 2" in report_text
    assert "WARNING: 1" in report_text
    assert [row["daily_report_created"] for row in rows] == [0, 0, 0]


def test_daily_logging_report_skips_outside_allowed_window(tmp_path, monkeypatch):
    sqlite_path = tmp_path / "runtime.sqlite"
    reports_config_path = tmp_path / "reports_config.json"
    write_report_config(reports_config_path, allowed_from_hour=10, allowed_to_hour=11)
    monkeypatch.setattr("db_ops.reports.metrics_reports.datetime", FrozenDateTime)
    FrozenDateTime.fixed_now = datetime(2026, 6, 4, 2, 30, 0, tzinfo=timezone.utc)

    result = run_scheduled_reports(
        sqlite_path=sqlite_path,
        telegram_groups={"logging": "-100"},
        reports_config_path=reports_config_path,
        summary_limit=150,
    )

    assert result["created"] == 0
    assert result["queued"] == 0
    assert result["reports"][0]["skipped_reason"] == "outside allowed hour window: 9"
    FrozenDateTime.fixed_now = datetime(2026, 6, 4, 3, 30, 0, tzinfo=timezone.utc)


def test_daily_logging_report_respects_legacy_send_state_repeat_interval(tmp_path, monkeypatch):
    sqlite_path = tmp_path / "runtime.sqlite"
    reports_config_path = tmp_path / "reports_config.json"
    write_report_config(reports_config_path, allowed_from_hour=10, allowed_to_hour=11)
    store = DbOpsStore(sqlite_path)
    store.upsert_report_send_state(
        report_code="rp_metric_hourly_logging",
        channel="logging",
        last_sent_at="2026-06-04T03:00:00Z",
        last_run_at="2026-06-04T03:00:00Z",
        last_status="queued",
        last_skipped_reason="",
    )
    monkeypatch.setattr("db_ops.reports.metrics_reports.datetime", FrozenDateTime)

    result = run_scheduled_reports(
        sqlite_path=sqlite_path,
        telegram_groups={"logging": "-100"},
        reports_config_path=reports_config_path,
        summary_limit=150,
    )

    migrated = store.fetch_report_send_state(report_code="rp_metric_daily_logging", channel="logging")
    assert result["created"] == 0
    assert result["queued"] == 0
    assert "repeat interval not elapsed" in result["reports"][0]["skipped_reason"]
    assert migrated is not None
    assert migrated["last_sent_at"] == "2026-06-04T03:00:00Z"


def test_daily_logging_report_ignores_stale_metric_rows_by_max_age(tmp_path, monkeypatch):
    sqlite_path = tmp_path / "runtime.sqlite"
    reports_config_path = tmp_path / "reports_config.json"
    write_report_config(reports_config_path, allowed_from_hour=10, allowed_to_hour=11, metric_max_age=600)
    target, definition = insert_logging_metric(sqlite_path, collected_at="2026-06-04T03:00:00Z")
    monkeypatch.setattr("db_ops.reports.metrics_reports.datetime", FrozenDateTime)
    monkeypatch.setattr("db_ops.reports.metrics_reports.load_metric_targets", lambda **_: [target])
    monkeypatch.setattr("db_ops.reports.metrics_reports.load_metric_definitions", lambda *_, **__: [definition])

    result = run_scheduled_reports(
        sqlite_path=sqlite_path,
        telegram_groups={"logging": "-100"},
        reports_config_path=reports_config_path,
        summary_limit=150,
    )

    assert result["created"] == 0
    assert result["queued"] == 0
    assert result["reports"][0]["skipped_reason"] == "no logging metric data"


def test_push_report_alerts_can_filter_report_ids(tmp_path):
    sqlite_path = tmp_path / "runtime.sqlite"
    first_report_id = insert_backup_health_report(sqlite_path)
    store = DbOpsStore(sqlite_path)
    second_report_id = store.insert_report(
        report_code="BACKUP_HEALTH",
        report_name="Backup health report",
        report_type="BACKUP_HEALTH",
        report_level="logging",
        report_text="[BACKUP HEALTH]\nSECOND",
        source_type="metric_results",
        source_id="backup_health:2026-05-24",
    )

    result = push_report_alerts(
        sqlite_path=sqlite_path,
        telegram_groups={"logging": "-100"},
        dedupe_seconds=0,
        report_ids=[second_report_id],
    )

    queued = store.fetch_pending_telegram_send_messages(limit=10)
    assert result["queued"] == 1
    assert result["pushed_report_ids"] == [second_report_id]
    assert first_report_id not in result["pushed_report_ids"]
    assert queued[0]["message_text"] == "[BACKUP HEALTH]\nSECOND"


def test_create_metrics_reports_skips_reports_disabled_target(tmp_path, monkeypatch):
    sqlite_path = tmp_path / "runtime.sqlite"
    data_dir = tmp_path / "data"
    target_id = "ACME-192-0-2-115/sqlserver/ERP"
    write_db_instances(
        data_dir,
        [
            {
                "target_id": target_id,
                "site": "ACME",
                "ip": "192.0.2.115",
                "db_type": "sqlserver",
                "service_name": "ERP",
                "enabled": True,
                "metrics": {"enabled": True},
                "reports": {"enabled": False},
            }
        ],
    )
    monkeypatch.setattr("db_ops.reports.metrics_reports.DEFAULT_DATA_DIR", data_dir)
    metric_store = MetricStore(sqlite_path)
    metric_store.insert_results(
        run_id=1,
        results=[
            MetricResult(
                target_id=target_id,
                server_id="ACME-192-0-2-115",
                ip="192.0.2.115",
                db_type="sqlserver",
                db_name="ERP",
                metric_code="INSTANCE_STATUS",
                metric_item="server",
                metric_value="1",
                metric_unit=None,
                status="OK",
                importance=1,
                message="server=ERP",
                collected_at="2026-06-02T01:00:00Z",
            )
        ],
    )

    result = create_metrics_reports(sqlite_path=sqlite_path)

    assert result["created"] == 0
    assert result["skipped"]


def test_push_report_alerts_skips_alerts_disabled_target(tmp_path, monkeypatch):
    sqlite_path = tmp_path / "runtime.sqlite"
    data_dir = tmp_path / "data"
    target_id = "ACME-192-0-2-115/sqlserver/ERP"
    write_db_instances(
        data_dir,
        [
            {
                "target_id": target_id,
                "site": "ACME",
                "ip": "192.0.2.115",
                "db_type": "sqlserver",
                "service_name": "ERP",
                "enabled": True,
                "metrics": {"enabled": True},
                "reports": {"enabled": True},
                "alerts": {"enabled": False},
            }
        ],
    )
    monkeypatch.setattr("db_ops.reports.metrics_reports.DEFAULT_DATA_DIR", data_dir)
    store = DbOpsStore(sqlite_path)
    report_id = store.insert_report(
        report_code="rp_metric_daily_logging",
        report_name="Daily Metrics Logging Summary",
        report_type="instancely_logging",
        report_level="logging",
        report_text="[METRICS SUMMARY]\nOK",
        metadata={"target_id": target_id},
    )

    result = push_report_alerts(sqlite_path=sqlite_path, telegram_groups={"logging": "-100"}, report_ids=[report_id])
    queued = store.fetch_pending_telegram_send_messages(limit=10)

    assert result["queued"] == 0
    assert result["skipped"][0]["reason"] == f"alerts disabled for target {target_id}"
    assert queued == []


def test_backup_health_report_ignores_synthetic_target_db_name_from_no_data(tmp_path, monkeypatch):
    sqlite_path = tmp_path / "runtime.sqlite"
    collected_at = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    target = MetricTarget(
        target_id="ACME-192-0-2-250/sqlserver/APPDB-PROD",
        server_id="ACME-192-0-2-250",
        ip="192.0.2.250",
        db_type="sqlserver",
        db_name="APPDB-PROD",
        credential_name="test",
        instance_name="APPDB",
        connection_info={"server_name": "APPDB-DB\\APPDB"},
    )
    metric_store = MetricStore(sqlite_path)
    metric_store.insert_results(
        run_id=1,
        results=[
            MetricResult(
                target_id=target.target_id,
                server_id=target.server_id,
                ip=target.ip,
                db_type=target.db_type,
                db_name=target.db_name,
                metric_code="BACKUP_AGE",
                metric_item="APPDB-PROD",
                metric_value=None,
                metric_unit=None,
                status="NO_DATA",
                importance=5,
                message="SQL returned no rows.",
                collected_at=collected_at,
            ),
            MetricResult(
                target_id=target.target_id,
                server_id=target.server_id,
                ip=target.ip,
                db_type=target.db_type,
                db_name=target.db_name,
                metric_code="BACKUP_AGE",
                metric_item="APPDB_Prod",
                metric_value="23",
                metric_unit="hours",
                status="OK",
                importance=5,
                message="Last full/differential backup 23 hours ago for database APPDB_Prod.",
                collected_at=collected_at,
            ),
        ],
    )
    monkeypatch.setattr("db_ops.reports.metrics_reports.load_metric_targets", lambda **_: [target])

    result = create_backup_health_report(sqlite_path=sqlite_path, force=True)

    report = DbOpsStore(sqlite_path).fetch_reports_for_push(limit=10)[0]
    assert result["created"] == 1
    assert "- APPDB_Prod:" in report["report_text"]
    assert "- APPDB-PROD:" not in report["report_text"]


def test_create_metrics_reports_marks_metric_rows_without_queueing_telegram(tmp_path, monkeypatch):
    sqlite_path = tmp_path / "runtime.sqlite"
    target = MetricTarget(
        target_id="server/sqlserver/db",
        server_id="server",
        ip="127.0.0.1",
        db_type="sqlserver",
        db_name="db",
        credential_name="test",
        connection_info={"server_name": "server"},
    )
    definition = MetricDefinition(
        metric_code="INSTANCE_STATUS",
        db_type="sqlserver",
        category="instance",
        default_importance=1,
        active=True,
    )
    metric_store = MetricStore(sqlite_path)
    metric_store.insert_results(
        run_id=1,
        results=[
            MetricResult(
                target_id=target.target_id,
                server_id=target.server_id,
                ip=target.ip,
                db_type=target.db_type,
                db_name=target.db_name,
                metric_code=definition.metric_code,
                metric_item="server",
                metric_value="1",
                metric_unit=None,
                status="OK",
                importance=1,
                message="server=server",
                collected_at="2026-05-21T08:27:11Z",
            )
        ],
    )
    monkeypatch.setattr("db_ops.reports.metrics_reports.load_metric_targets", lambda **_: [target])
    monkeypatch.setattr("db_ops.reports.metrics_reports.load_metric_definitions", lambda *_, **__: [definition])

    result = create_metrics_reports(sqlite_path=sqlite_path, summary_limit=150)

    rows = metric_store.fetch_latest_report_results(metric_codes={definition.metric_code})
    queued = DbOpsStore(sqlite_path).fetch_pending_telegram_send_messages(limit=10)
    assert result["created"] == 1
    assert result["marked_daily_report_rows"] == 1
    assert rows[0]["daily_report_created"] == 1
    assert queued == []


def test_create_metrics_reports_can_scope_to_one_target(tmp_path, monkeypatch):
    sqlite_path = tmp_path / "runtime.sqlite"
    target_a = MetricTarget(
        target_id="server-a/sqlserver/db",
        server_id="server-a",
        ip="192.0.2.115",
        db_type="sqlserver",
        db_name="db",
        credential_name="test",
        connection_info={"server_name": "server-a"},
    )
    target_b = MetricTarget(
        target_id="server-b/sqlserver/db",
        server_id="server-b",
        ip="192.0.2.116",
        db_type="sqlserver",
        db_name="db",
        credential_name="test",
        connection_info={"server_name": "server-b"},
    )
    definition = MetricDefinition(
        metric_code="INSTANCE_STATUS",
        db_type="sqlserver",
        category="instance",
        default_importance=1,
        active=True,
    )
    metric_store = MetricStore(sqlite_path)
    metric_store.insert_results(
        run_id=1,
        results=[
            MetricResult(
                target_id=target_a.target_id,
                server_id=target_a.server_id,
                ip=target_a.ip,
                db_type=target_a.db_type,
                db_name=target_a.db_name,
                metric_code=definition.metric_code,
                metric_item="server-a",
                metric_value="1",
                metric_unit=None,
                status="OK",
                importance=1,
                message="server=server-a",
                collected_at="2026-05-21T08:27:11Z",
            ),
            MetricResult(
                target_id=target_b.target_id,
                server_id=target_b.server_id,
                ip=target_b.ip,
                db_type=target_b.db_type,
                db_name=target_b.db_name,
                metric_code=definition.metric_code,
                metric_item="server-b",
                metric_value="1",
                metric_unit=None,
                status="OK",
                importance=1,
                message="server=server-b",
                collected_at="2026-05-21T08:27:11Z",
            ),
        ],
    )
    monkeypatch.setattr(
        "db_ops.reports.metrics_reports.load_metric_targets",
        lambda **kwargs: [target for target in [target_a, target_b] if not kwargs.get("target_id") or target.target_id == kwargs["target_id"]],
    )
    monkeypatch.setattr("db_ops.reports.metrics_reports.load_metric_definitions", lambda *_, **__: [definition])

    result = create_metrics_reports(sqlite_path=sqlite_path, summary_limit=150, target_id=target_a.target_id)
    rows = metric_store.fetch_latest_results(limit=10, metric_code=definition.metric_code)
    report = DbOpsStore(sqlite_path).fetch_reports_for_push(limit=10)[0]

    marked_by_target = {row["target_id"]: row["daily_report_created"] for row in rows}
    assert result["created"] == 1
    assert result["target_id"] == target_a.target_id
    assert marked_by_target[target_a.target_id] == 1
    assert marked_by_target[target_b.target_id] == 0
    assert "server-a" in report["report_text"]
    assert "server-b" not in report["report_text"]


def test_create_metrics_reports_preserves_query_store_message(tmp_path, monkeypatch):
    sqlite_path = tmp_path / "runtime.sqlite"
    target = MetricTarget(
        target_id="SALESCLUSTER/sqlserver/SALESDB",
        server_id="SALESCLUSTER",
        ip="192.0.2.115",
        db_type="sqlserver",
        db_name="SALESDB",
        credential_name="test",
        connection_info={"server_name": "SALESCLUSTER"},
    )
    definition = MetricDefinition(
        metric_code="QUERY_STORE_QUERY_ISSUES",
        db_type="sqlserver",
        category="performance",
        default_importance=4,
        active=True,
    )
    query_text = "SELECT " + ", ".join(f"col{i}" for i in range(800))
    message = (
        "db_name=SALESDB, query_id=1638838, plan_id=1205360, old_plan_id=1205359, "
        "runtime_stats_count=2, executions=2, plan_count=2, "
        "last_execution_time=2026-05-26 10:12:56, "
        "issue_type=QUERY_PLAN_REGRESSED_READS, avg_duration_sec=10.000000, "
        "max_duration_sec=1245.000000, avg_cpu_sec=2.000000, max_cpu_sec=3.000000, "
        "avg_logical_reads=1000000, max_logical_reads=2500000, best_logical_reads=1000, "
        "logical_read_ratio=2500.00, checked_window=last_6_hours, "
        "checked_from=2026-05-26 08:26:51, checked_to=2026-05-26 14:26:51, "
        f"query_text={query_text}"
    )
    metric_store = MetricStore(sqlite_path)
    metric_store.insert_results(
        run_id=1,
        results=[
            MetricResult(
                target_id=target.target_id,
                server_id=target.server_id,
                ip=target.ip,
                db_type=target.db_type,
                db_name=target.db_name,
                metric_code=definition.metric_code,
                metric_item="query_store_plan_regressed_reads",
                metric_value="2500",
                metric_unit="read_ratio",
                status="CRITICAL",
                importance=4,
                message=message,
                collected_at="2026-05-26T07:26:51Z",
            )
        ],
    )
    monkeypatch.setattr("db_ops.reports.metrics_reports.load_metric_targets", lambda **_: [target])
    monkeypatch.setattr("db_ops.reports.metrics_reports.load_metric_definitions", lambda *_, **__: [definition])

    result = create_metrics_reports(sqlite_path=sqlite_path, summary_limit=150)
    report = DbOpsStore(sqlite_path).fetch_reports_for_push(limit=10)[0]

    assert result["created"] == 1
    assert "QUERY_STORE_QUERY_ISSUES" in report["report_text"]
    assert "db_name=SALESDB" in report["report_text"]
    assert "issue_type=QUERY_PLAN_REGRESSED_READS" in report["report_text"]
    assert "max_duration_sec=1245" in report["report_text"]
    assert "query_id=1638838" in report["report_text"]
    assert "runtime_stats_count=2" in report["report_text"]
    assert "best_logical_reads=1000" in report["report_text"]
    assert "checked_from=2026-05-26 08:26:51" in report["report_text"]
    assert query_text in report["report_text"]

    push_result = push_report_alerts(
        sqlite_path=sqlite_path,
        telegram_groups={"critical": "-100"},
        dedupe_seconds=0,
    )
    queued = DbOpsStore(sqlite_path).fetch_pending_telegram_send_messages(limit=10)

    assert push_result["queued"] == 3
    assert len(queued) == 3
    assert all(len(row["message_text"]) <= TELEGRAM_SAFE_TEXT_LENGTH for row in queued)
    queued_text = "\n".join(row["message_text"] for row in queued)
    assert "max_duration_sec=1245" in queued_text
    assert "query_text=SELECT col0" in queued_text
    assert "col799" in queued_text
    assert "truncated" not in queued_text.lower()


def test_create_metrics_reports_skips_latest_rows_already_marked_daily_report(tmp_path, monkeypatch):
    sqlite_path = tmp_path / "runtime.sqlite"
    target = MetricTarget(
        target_id="server/sqlserver/db",
        server_id="server",
        ip="127.0.0.1",
        db_type="sqlserver",
        db_name="db",
        credential_name="test",
        connection_info={"server_name": "server"},
    )
    definition = MetricDefinition(
        metric_code="INSTANCE_STATUS",
        db_type="sqlserver",
        category="instance",
        default_importance=1,
        active=True,
    )
    metric_store = MetricStore(sqlite_path)
    metric_store.insert_results(
        run_id=1,
        results=[
            MetricResult(
                target_id=target.target_id,
                server_id=target.server_id,
                ip=target.ip,
                db_type=target.db_type,
                db_name=target.db_name,
                metric_code=definition.metric_code,
                metric_item="server",
                metric_value="1",
                metric_unit=None,
                status="OK",
                importance=1,
                message="server=server",
                collected_at="2026-05-21T08:27:11Z",
            )
        ],
    )
    monkeypatch.setattr("db_ops.reports.metrics_reports.load_metric_targets", lambda **_: [target])
    monkeypatch.setattr("db_ops.reports.metrics_reports.load_metric_definitions", lambda *_, **__: [definition])

    first_create = create_metrics_reports(sqlite_path=sqlite_path, summary_limit=150)
    second_create = create_metrics_reports(sqlite_path=sqlite_path, summary_limit=150)
    push_result = push_report_alerts(
        sqlite_path=sqlite_path,
        telegram_groups={"logging": "-100"},
        dedupe_seconds=0,
    )

    queued = DbOpsStore(sqlite_path).fetch_pending_telegram_send_messages(limit=10)
    assert first_create["created"] == 1
    assert second_create["created"] == 0
    assert second_create["skipped"] == [{"reason": "latest metric rows already have daily_report_created=1"}]
    assert push_result["queued"] == 1
    assert push_result["pushed_report_ids"] == first_create["report_ids"]
    assert len(queued) == 1


def test_create_metrics_reports_marks_previous_collect_rows_as_reported(tmp_path, monkeypatch):
    sqlite_path = tmp_path / "runtime.sqlite"
    target = MetricTarget(
        target_id="server/sqlserver/db",
        server_id="server",
        ip="127.0.0.1",
        db_type="sqlserver",
        db_name="db",
        credential_name="test",
        connection_info={"server_name": "server"},
    )
    definition = MetricDefinition(
        metric_code="INSTANCE_STATUS",
        db_type="sqlserver",
        category="instance",
        default_importance=1,
        active=True,
    )
    metric_store = MetricStore(sqlite_path)
    metric_store.insert_results(
        run_id=1,
        results=[
            MetricResult(
                target_id=target.target_id,
                server_id=target.server_id,
                ip=target.ip,
                db_type=target.db_type,
                db_name=target.db_name,
                metric_code=definition.metric_code,
                metric_item="server",
                metric_value="0",
                metric_unit=None,
                status="ERROR",
                importance=1,
                message="old collect",
                collected_at="2026-05-21T08:00:00Z",
            )
        ],
    )
    metric_store.insert_results(
        run_id=2,
        results=[
            MetricResult(
                target_id=target.target_id,
                server_id=target.server_id,
                ip=target.ip,
                db_type=target.db_type,
                db_name=target.db_name,
                metric_code=definition.metric_code,
                metric_item="server",
                metric_value="2",
                metric_unit=None,
                status="WARNING",
                importance=1,
                message="latest collect",
                collected_at="2026-05-21T09:00:00Z",
            )
        ],
    )
    monkeypatch.setattr("db_ops.reports.metrics_reports.load_metric_targets", lambda **_: [target])
    monkeypatch.setattr("db_ops.reports.metrics_reports.load_metric_definitions", lambda *_, **__: [definition])

    result = create_metrics_reports(sqlite_path=sqlite_path, summary_limit=150)
    rows = metric_store.fetch_latest_results(limit=10, metric_code=definition.metric_code)
    report_store = DbOpsStore(sqlite_path)
    report = report_store.fetch_reports_for_push(limit=10)[0]

    assert result["created"] == 1
    assert result["marked_daily_report_rows"] == 2
    assert [row["daily_report_created"] for row in rows] == [1, 1]
    assert "latest collect" in report["report_text"]
    assert "old collect" not in report["report_text"]


def test_push_report_alerts_skips_backup_health_when_logging_group_missing(tmp_path):
    sqlite_path = tmp_path / "runtime.sqlite"
    report_id = insert_backup_health_report(sqlite_path)

    result = push_report_alerts(
        sqlite_path=sqlite_path,
        telegram_groups={"warning": "-200"},
        dedupe_seconds=0,
    )

    store = DbOpsStore(sqlite_path)
    queued = store.fetch_pending_telegram_send_messages(limit=10)
    assert result["queued"] == 0
    assert result["pushed"] == 0
    assert result["skipped"] == [
        {
            "report_id": report_id,
            "level": "logging",
            "reason": "Telegram group not configured for level=logging.",
        }
    ]
    assert queued == []


def test_long_report_text_is_split_for_telegram_limit():
    text = "[BACKUP HEALTH]\n" + "\n".join(f"- DB{i}: FULL 1/1 DIFF 1/1 LOG 1/1 age=1h/OK" for i in range(500))

    chunks = split_telegram_message(text)

    assert len(chunks) > 1
    assert all(len(chunk) <= TELEGRAM_SAFE_TEXT_LENGTH for chunk in chunks)
    assert chunks[0].startswith("[part 1/")
    assert "DB499" in chunks[-1]


def test_push_report_alerts_queues_long_report_as_multiple_messages(tmp_path):
    sqlite_path = tmp_path / "runtime.sqlite"
    long_text = "[BACKUP HEALTH]\n" + "\n".join(f"- DB{i}: FULL 1/1 DIFF 1/1 LOG 1/1 age=1h/OK" for i in range(500))
    store = DbOpsStore(sqlite_path)
    report_id = store.insert_report(
        report_code="BACKUP_HEALTH",
        report_name="Backup health report",
        report_type="BACKUP_HEALTH",
        report_level="logging",
        report_text=long_text,
        source_type="metric_results",
        source_id="backup_health:2026-05-23",
    )

    result = push_report_alerts(
        sqlite_path=sqlite_path,
        telegram_groups={"logging": "-100"},
        dedupe_seconds=0,
    )

    queued = store.fetch_pending_telegram_send_messages(limit=20)
    assert result["pushed_report_ids"] == [report_id]
    assert result["queued"] == len(queued)
    assert len(queued) > 1
    assert all(len(row["message_text"]) <= TELEGRAM_SAFE_TEXT_LENGTH for row in queued)
    assert queued[0]["message_text"].startswith("[part 1/")
    assert "DB499" in queued[-1]["message_text"]
