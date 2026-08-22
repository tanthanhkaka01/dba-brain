import re
from datetime import datetime, timedelta, timezone

from db_ops.db import DbOpsStore
from db_ops.metrics.models import MetricResult, MetricTarget
from db_ops.metrics.storage import MetricStore
from db_ops.reports.metrics_reports import (
    TELEGRAM_SAFE_TEXT_LENGTH,
    create_metric_report_for_level,
    push_report_alerts,
    split_telegram_message,
)


def _target():
    return MetricTarget(
        target_id="server/sqlserver/db",
        server_id="server",
        ip="127.0.0.1",
        db_type="sqlserver",
        db_name="db",
        credential_name="test",
        connection_info={"server_name": "server"},
    )


def _warning_result(target, *, metric_code, item, message, collected_at, status="WARNING"):
    return MetricResult(
        target_id=target.target_id,
        server_id=target.server_id,
        ip=target.ip,
        db_type=target.db_type,
        db_name=target.db_name,
        metric_code=metric_code,
        metric_item=item,
        metric_value=None,
        metric_unit=None,
        status=status,
        importance=3,
        message=message,
        collected_at=collected_at,
    )


def _result(
    target,
    *,
    metric_code,
    item="",
    value=None,
    unit=None,
    message="",
    collected_at,
    status="WARNING",
    collector_type=None,
    category=None,
    error_type=None,
    signature=None,
):
    return MetricResult(
        target_id=target.target_id,
        server_id=target.server_id,
        ip=target.ip,
        db_type=target.db_type,
        db_name=target.db_name,
        metric_code=metric_code,
        metric_item=item,
        metric_value=value,
        metric_unit=unit,
        status=status,
        importance=4,
        message=message,
        collected_at=collected_at,
        collector_type=collector_type,
        category=category,
        error_type=error_type,
        normalized_error_signature=signature,
    )


def _event_counts(report_text, severity="Warning"):
    raw = int(re.search(rf"Raw {severity} Rows: (\d+)", report_text).group(1))
    events = int(re.search(rf"{severity} Events: (\d+)", report_text).group(1))
    return raw, events


def _section_headers(report_text):
    return re.findall(r"^\[.+\]$", report_text, flags=re.MULTILINE)


def _count_dash_lines(text):
    return sum(1 for line in text.splitlines() if line.startswith("- "))


def test_warning_events_count_is_preserved_across_telegram_message_split(tmp_path, monkeypatch):
    sqlite_path = tmp_path / "runtime.sqlite"
    target = _target()
    fresh = (datetime.now(timezone.utc) - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    metric_store = MetricStore(sqlite_path)
    metric_store.insert_results(
        run_id=1,
        results=[
            _warning_result(
                target,
                metric_code="STORAGE_DISK_FREE_SPACE",
                item=f"disk_{i}",
                message=f"low free space on volume {i} with a fairly long diagnostic message to pad length",
                collected_at=fresh,
            )
            for i in range(60)
        ],
    )
    monkeypatch.setattr("db_ops.reports.metrics_reports.load_metric_targets", lambda **_: [target])

    create_result = create_metric_report_for_level(
        sqlite_path=sqlite_path,
        report_code="rp_metric_hourly_warning",
        level="warning",
        freshness_seconds=600,
        unreported_only=False,
    )
    report = DbOpsStore(sqlite_path).fetch_reports_for_push(limit=10)[0]
    report_text = report["report_text"]
    raw, events = _event_counts(report_text)

    assert create_result["created"] == 1
    assert raw == 60
    assert events == 60
    assert events == _count_dash_lines(report_text)
    assert "[server 127.0.0.1 / Disk free]" not in report_text
    assert "- server 127.0.0.1 / STORAGE_DISK_FREE_SPACE / disk_0: low free space on volume 0" in report_text
    assert len(report_text) > TELEGRAM_SAFE_TEXT_LENGTH

    chunks = split_telegram_message(report_text)
    assert len(chunks) > 1
    assert chunks[0].startswith("[part 1/")
    assert chunks[1].startswith("[part 2/")
    assert sum(_count_dash_lines(chunk) for chunk in chunks) == events


def test_push_report_alerts_preserves_warning_event_count_across_queued_parts(tmp_path, monkeypatch):
    sqlite_path = tmp_path / "runtime.sqlite"
    target = _target()
    fresh = (datetime.now(timezone.utc) - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    metric_store = MetricStore(sqlite_path)
    metric_store.insert_results(
        run_id=1,
        results=[
            _warning_result(
                target,
                metric_code="STORAGE_DISK_FREE_SPACE",
                item=f"disk_{i}",
                message=f"low free space on volume {i} with a fairly long diagnostic message to pad length",
                collected_at=fresh,
            )
            for i in range(60)
        ],
    )
    monkeypatch.setattr("db_ops.reports.metrics_reports.load_metric_targets", lambda **_: [target])

    created = create_metric_report_for_level(
        sqlite_path=sqlite_path,
        report_code="rp_metric_hourly_warning",
        level="warning",
        freshness_seconds=600,
        unreported_only=False,
    )
    report = DbOpsStore(sqlite_path).fetch_reports_for_push(limit=10)[0]
    _, events = _event_counts(report["report_text"])

    push_result = push_report_alerts(
        sqlite_path=sqlite_path,
        telegram_groups={"warning": "-100"},
        report_ids=created["report_ids"],
        dedupe_seconds=0,
    )
    queued = DbOpsStore(sqlite_path).fetch_pending_telegram_send_messages(limit=20)

    assert push_result["queued"] == len(queued)
    assert len(queued) > 1
    assert queued[0]["message_text"].startswith("[part 1/")
    assert sum(_count_dash_lines(row["message_text"]) for row in queued) == events


