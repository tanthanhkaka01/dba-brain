from __future__ import annotations
from db_ops.lib.metric_score import target_score as _target_score  # noqa: F401 - one definition
from db_ops.lib.text_format import format_utc as _format_utc  # noqa: F401 - one definition, see that module

import json
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from pathlib import Path
from typing import Any

from db_ops.common.data_sources import DEFAULT_DATA_DIR, load_config_metric_targets, resolve_config_metric_target
from db_ops.lib.policy_engine import apply_report_policy, render_policy_event, row_status, status_rank
from db_ops.lib.rows import row_text
from db_ops.db.queue_message import queue_message, store_block_from
from db_ops.lib.time_window import TimeWindow, is_time_window_open, parse_time_window_config, repeat_due, time_window_closed_reason
from db_ops.common import data_sources
from db_ops.db.metric_definitions import definition_supports_db_type
from db_ops.db.metric_store import MetricStore
from db_ops.db.metric_results import rows_by_target
from db_ops.db import DbOpsStore

# Splitting a long body is shared behaviour, not this report's own: db_ops.telegram.api applies
# the same function to every outgoing message. Imported rather than defined here, and re-exported
# under the old names, because this report still splits deliberately — each chunk is queued as its
# own row so it carries its own level, which a send-time split could not do.
from db_ops.lib.telegram_text import (
    TELEGRAM_SAFE_TEXT_LENGTH,
    split_telegram_message,
    split_text_by_lines,
)

CRITICAL_LINE_LIMIT = 30
WARNING_LINE_LIMIT = 40
VIETNAM_TZ = timezone(timedelta(hours=7))
DEFAULT_REPORTS_CONFIG_PATH = DEFAULT_DATA_DIR / "reports_config.json"
DEFAULT_METRIC_DEFINITIONS_PATH = DEFAULT_DATA_DIR / "metric_definitions.json"
BACKUP_HEALTH_CODE = "rp_backup_health_daily"
BACKUP_HEALTH_TYPE = "BACKUP_HEALTH"
DAILY_LOGGING_CODE = "rp_metric_daily_logging"
LEGACY_DAILY_LOGGING_CODE = "rp_metric_hourly_logging"
METRIC_REPORT_LEVELS = {
    DAILY_LOGGING_CODE: "logging",
    "rp_metric_hourly_warning": "warning",
    "rp_metric_hourly_critical": "critical",
}
METRIC_HISTORY_CODE = "METRIC_HISTORY"
METRIC_HISTORY_TYPE = "metric_history"
REQUIRED_SCHEDULE_FIELDS = {
    "metric_max_age_seconds",
}


def load_metric_targets(**_: Any) -> list[object]:
    return []


def load_metric_definitions(*_: Any, **__: Any) -> list[object]:
    return []


def create_metrics_reports(
    *,
    sqlite_path: str | Path,
    summary_limit: int = WARNING_LINE_LIMIT,
    target_id: str | None = None,
    target_ip: str | None = None,
) -> dict[str, Any]:
    report_store = DbOpsStore(sqlite_path)
    target_id = _resolve_report_target_id(store=report_store, target_ip=target_ip, target_id=target_id)
    rows = report_store.fetch_latest_metric_report_results(target_id=target_id, target_ip=target_ip)
    rows = _filter_rows_for_reports(rows)
    active_metric_codes = {str(row["metric_code"] or "") for row in rows if str(row["metric_code"] or "")}
    run_meta = report_store.fetch_latest_metric_run_meta()
    active_target_ids = {str(row["target_id"] or "") for row in rows if str(row["target_id"] or "")}
    target_labels = _target_config_labels_from_rows(rows)
    targets = _targets_from_rows(rows)
    definitions = _definitions_from_rows(rows)
    if not rows:
        return {
            "created": 0,
            "report_ids": [],
            "summary_limit": summary_limit,
            "target_ip": target_ip,
            "target_id": target_id,
            "skipped": [{"reason": "no report-enabled metric data"}],
        }
    unreported_rows = [row for row in rows if int(row["daily_report_created"] or 0) == 0]
    if rows and not unreported_rows:
        marked_daily_report_rows = report_store.mark_metric_daily_report_created_for_scope(
            metric_codes=active_metric_codes,
            target_ids=active_target_ids,
            collected_at_lte=max(str(row["collected_at"] or "") for row in rows),
        )
        return {
            "created": 0,
            "report_ids": [],
            "summary_limit": summary_limit,
            "target_ip": target_ip,
            "target_id": target_id,
            "marked_daily_report_rows": marked_daily_report_rows,
            "skipped": [{"reason": "latest metric rows already have daily_report_created=1"}],
        }

    report_ids: list[int] = []
    skipped: list[dict[str, Any]] = []
    for level in ("critical", "warning", "logging"):
        level_rows = _rows_for_level(rows, level)
        health_rows = _health_rows_for_level(rows, level)
        if not level_rows and not health_rows:
            continue

        text = _format_level_report(
            rows=rows,
            level=level,
            summary_limit=summary_limit,
            target_labels=target_labels,
            targets=targets,
            definitions=definitions,
            run_meta=run_meta,
            report_title=_metric_report_display_name(_report_code_for_metric_level(level)),
            report_window_seconds=_configured_metric_max_age_seconds(_report_code_for_metric_level(level)),
        )
        if not text.strip():
            continue
        report_type = f"instancely_{level}"
        report_ids.append(
            report_store.insert_report(
                report_code=f"METRICS_LATEST_{level.upper()}",
                report_name=f"Latest metrics {level} report",
                report_type=report_type,
                report_level=level,
                report_text=text,
                source_type="metrics",
                source_id=f"metrics_latest:{level}:{target_id}" if target_id else f"metrics_latest:{level}",
                metadata={
                    "level": level,
                    "report_type": report_type,
                    "summary_limit": summary_limit,
                    "target_ip": target_ip,
                    "target_id": target_id,
                    "target_count": len(active_target_ids),
                    "row_count": len(rows),
                },
            )
        )
    marked_daily_report_rows = 0
    if report_ids:
        marked_daily_report_rows = report_store.mark_metric_daily_report_created_for_scope(
            metric_codes=active_metric_codes,
            target_ids=active_target_ids,
            collected_at_lte=max(str(row["collected_at"] or "") for row in rows),
        )

    return {
        "created": len(report_ids),
        "report_ids": report_ids,
        "summary_limit": summary_limit,
        "target_ip": target_ip,
        "target_id": target_id,
        "marked_daily_report_rows": marked_daily_report_rows,
        "skipped": skipped,
    }


def create_metric_history_report(
    *,
    sqlite_path: str | Path,
    server_id: str,
    metric_code: str,
    hours: int,
    summary_limit: int = 150,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Create one stored-metric history report for a relative UTC window.

    This workflow is intentionally read-only with respect to metric collection: it
    reads existing ``metric_results`` rows and never invokes the Metrics Engine.
    """

    normalized_server_id = str(server_id or "").strip()
    normalized_metric_code = str(metric_code or "").strip().upper()
    hour_count = int(hours)
    line_limit = int(summary_limit)
    if not normalized_server_id:
        raise ValueError("server_id is required.")
    if not normalized_metric_code:
        raise ValueError("metric_code is required.")
    if hour_count < 1:
        raise ValueError("hours must be >= 1.")
    if line_limit < 1:
        raise ValueError("summary_limit must be >= 1.")

    window_end = now or datetime.now(timezone.utc)
    if window_end.tzinfo is None:
        window_end = window_end.replace(tzinfo=timezone.utc)
    else:
        window_end = window_end.astimezone(timezone.utc)
    window_start = window_end - timedelta(hours=hour_count)
    window_start_text = _format_utc(window_start)
    window_end_text = _format_utc(window_end)

    store = DbOpsStore(sqlite_path)
    rows = store.fetch_metric_report_results(
        server_id=normalized_server_id,
        metric_code=normalized_metric_code,
        collected_at_gte=window_start_text,
        collected_at_lte=window_end_text,
    )
    rows = _filter_rows_for_reports(rows)
    if not rows:
        raise ValueError(
            "No stored metric data found for "
            f"server_id={normalized_server_id}, metric_code={normalized_metric_code}, "
            f"window={window_start_text}..{window_end_text}."
        )

    displayed_rows = rows[-line_limit:]
    target_ids = sorted({str(row["target_id"] or "") for row in rows if str(row["target_id"] or "")})
    target_id = target_ids[0] if len(target_ids) == 1 else ""
    report_text = _format_metric_history_report(
        rows=displayed_rows,
        total_row_count=len(rows),
        server_id=normalized_server_id,
        metric_code=normalized_metric_code,
        hours=hour_count,
        window_start=window_start,
        window_end=window_end,
        target_ids=target_ids,
        status_counts=_status_counts(rows),
    )
    report_id = store.insert_report(
        report_code=METRIC_HISTORY_CODE,
        report_name=f"Metric history: {normalized_metric_code}",
        report_type=METRIC_HISTORY_TYPE,
        report_level="logging",
        report_text=report_text,
        source_type="metric_results",
        source_id=f"metric_history:{normalized_server_id}:{normalized_metric_code}:{hour_count}",
        metadata={
            "server_id": normalized_server_id,
            "metric_code": normalized_metric_code,
            "hours": hour_count,
            "window_start": window_start_text,
            "window_end": window_end_text,
            "row_count": len(rows),
            "displayed_row_count": len(displayed_rows),
            "target_id": target_id,
            "target_ids": target_ids,
        },
    )
    return {
        "created": 1,
        "report_ids": [report_id],
        "server_id": normalized_server_id,
        "metric_code": normalized_metric_code,
        "hours": hour_count,
        "window_start": window_start_text,
        "window_end": window_end_text,
        "row_count": len(rows),
        "displayed_row_count": len(displayed_rows),
        "target_id": target_id,
        "target_ids": target_ids,
        "report_text": report_text,
    }


def create_backup_health_report(
    *,
    sqlite_path: str | Path,
    days: int = 7,
    run_hour: int | None = None,
    force: bool = False,
    display_name: str | None = None,
) -> dict[str, Any]:
    store = DbOpsStore(sqlite_path)
    local_now = datetime.now(VIETNAM_TZ)
    run_hour_window = TimeWindow(from_hour=int(run_hour), to_hour=int(run_hour)) if run_hour is not None else None
    if run_hour_window is not None and not force and not is_time_window_open(run_hour_window, local_now):
        return {
            "created": 0,
            "report_ids": [],
            "skipped": [
                {
                    "reason": f"not due: local hour is {local_now.hour}, expected {int(run_hour)}",
                    "run_hour": int(run_hour),
                }
            ],
        }
    if not force and store.report_exists_on_local_date(
            report_code=BACKUP_HEALTH_CODE, local_date=local_now.date().isoformat()):
        return {
            "created": 0,
            "report_ids": [],
            "skipped": [{"reason": f"{BACKUP_HEALTH_CODE} already created for {local_now.date().isoformat()}"}],
        }

    day_count = max(1, int(days))
    cutoff = (local_now - timedelta(days=day_count)).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    rows = _fetch_backup_metric_rows(store, cutoff=cutoff)
    targets = [target for target in _targets_from_rows(rows) if str(target.db_type).lower() == "sqlserver"]
    report_title = display_name or _metric_report_display_name(BACKUP_HEALTH_CODE)
    text = _format_backup_health_report(rows=rows, targets=targets, days=day_count, generated_at=local_now, report_title=report_title)
    report_id = store.insert_report(
        report_code=BACKUP_HEALTH_CODE,
        report_name=report_title,
        report_type=BACKUP_HEALTH_TYPE,
        report_level="logging",
        report_text=text,
        source_type="metric_results",
        source_id=f"backup_health:{local_now.date().isoformat()}",
        metadata={
            "days": day_count,
            "run_hour": run_hour,
            "row_count": len(rows),
            "target_count": len(targets),
            "generated_local_date": local_now.date().isoformat(),
        },
    )
    return {
        "created": 1,
        "report_ids": [report_id],
        "days": day_count,
        "row_count": len(rows),
        "target_count": len(targets),
        "skipped": [],
    }


def run_scheduled_reports(
    *,
    sqlite_path: str | Path,
    telegram_groups: dict[str, str],
    reports_config_path: str | Path = DEFAULT_REPORTS_CONFIG_PATH,
    summary_limit: int = WARNING_LINE_LIMIT,
    backup_days: int = 7,
    logger: Any | None = None,
    scheduler_trigger_time: str | None = None,
) -> dict[str, Any]:
    store = DbOpsStore(sqlite_path)
    report_configs = load_report_configs(reports_config_path, logger=logger)
    evaluated_at = datetime.now(timezone.utc)
    trigger_time = scheduler_trigger_time or _format_utc(evaluated_at)
    results = []

    for report_config in report_configs:
        report_code = str(report_config["report_code"])
        result = _run_one_scheduled_report(
            store=store,
            sqlite_path=sqlite_path,
            telegram_groups=telegram_groups,
            report_config=report_config,
            summary_limit=summary_limit,
            backup_days=backup_days,
            evaluated_at=evaluated_at,
            scheduler_trigger_time=trigger_time,
            logger=logger,
        )
        results.append(result)

    return {
        "evaluated": len(results),
        "created": sum(int(item.get("created", 0)) for item in results),
        "queued": sum(int(item.get("queued", 0)) for item in results),
        "sent": 0,
        "reports": results,
        "reports_config_path": str(reports_config_path),
    }


def load_report_configs(path: str | Path = DEFAULT_REPORTS_CONFIG_PATH, *, logger: Any | None = None) -> list[dict[str, Any]]:
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8-sig") as file:
        raw = json.load(file)
    reports = raw.get("reports")
    if not isinstance(reports, list):
        raise RuntimeError(f"reports_config must contain a reports list: {config_path}")
    seen_codes: set[str] = set()
    validated = []
    for item in reports:
        if not isinstance(item, dict):
            raise RuntimeError("Each report config item must be a JSON object.")
        report_code = str(item.get("report_code") or "").strip()
        if not report_code:
            raise RuntimeError("Each report config item must contain report_code.")
        if report_code in seen_codes:
            raise RuntimeError(f"Duplicate report_code in reports_config: {report_code}")
        missing = sorted(REQUIRED_SCHEDULE_FIELDS - set(item))
        if missing:
            raise RuntimeError(f"Report {report_code} missing schedule fields: {', '.join(missing)}")
        parsed_time_window = parse_time_window_config(
            item,
            context=f"reports.{report_code}",
            defaults={
                "from_day": 1,
                "to_day": 31,
                "from_hour": 0,
                "to_hour": 23,
                "repeat_interval": 300,
                "timeout": 300,
            },
        )
        _log_report_time_window_warnings(logger, parsed_time_window.warnings)
        item = dict(item)
        item["time_window"] = {
            "from_year": parsed_time_window.time_window.from_year,
            "to_year": parsed_time_window.time_window.to_year,
            "from_month": parsed_time_window.time_window.from_month,
            "to_month": parsed_time_window.time_window.to_month,
            "from_day": parsed_time_window.time_window.from_day,
            "to_day": parsed_time_window.time_window.to_day,
            "from_hour": parsed_time_window.time_window.from_hour,
            "to_hour": parsed_time_window.time_window.to_hour,
            "from_minute": parsed_time_window.time_window.from_minute,
            "to_minute": parsed_time_window.time_window.to_minute,
            "repeat_interval": parsed_time_window.time_window.repeat_interval,
            "timeout": parsed_time_window.time_window.timeout,
        }
        seen_codes.add(report_code)
        validated.append(item)
    return validated


def create_metric_report_for_level(
    *,
    sqlite_path: str | Path,
    report_code: str,
    level: str,
    summary_limit: int = WARNING_LINE_LIMIT,
    target_id: str | None = None,
    target_ip: str | None = None,
    freshness_seconds: int | None = None,
    unreported_only: bool = False,
    display_name: str | None = None,
) -> dict[str, Any]:
    report_store = DbOpsStore(sqlite_path)
    target_id = _resolve_report_target_id(store=report_store, target_ip=target_ip, target_id=target_id)
    if freshness_seconds is None:
        freshness_seconds = _configured_metric_max_age_seconds(report_code)
    report_title = display_name or _metric_report_display_name(report_code)
    collected_at_gte = _metric_freshness_cutoff(seconds=freshness_seconds)
    rows = report_store.fetch_metric_report_results(
        target_id=target_id,
        target_ip=target_ip,
        collected_at_gte=collected_at_gte,
        unreported_only=unreported_only and level != "logging",
    )
    rows = _filter_rows_for_reports(rows)
    run_meta = report_store.fetch_latest_metric_run_meta()
    active_target_ids = {str(row["target_id"] or "") for row in rows if str(row["target_id"] or "")}
    target_labels = _target_config_labels_from_rows(rows)
    targets = _targets_from_rows(rows)
    definitions = _definitions_from_rows(rows)

    level_rows = _rows_for_level(rows, level)
    health_rows = _health_rows_for_level(rows, level)
    if not level_rows and not health_rows:
        return {
            "created": 0,
            "report_ids": [],
            "summary_limit": summary_limit,
            "target_ip": target_ip,
            "target_id": target_id,
            "skipped": [{"reason": f"no {level} metric data"}],
        }
    text = _format_level_report(
        rows=rows,
        level=level,
        summary_limit=summary_limit,
        target_labels=target_labels,
        targets=targets,
        definitions=definitions,
        run_meta=run_meta,
        report_title=report_title,
        report_window_seconds=freshness_seconds,
    )
    if not text.strip():
        return {
            "created": 0,
            "report_ids": [],
            "summary_limit": summary_limit,
            "target_ip": target_ip,
            "target_id": target_id,
            "skipped": [{"reason": f"empty {level} report text"}],
        }

    report_type = f"instancely_{level}"
    generated_at = _format_utc(datetime.now(timezone.utc))
    report_id = report_store.insert_report(
        report_code=report_code,
        report_name=report_title,
        report_type=report_type,
        report_level=level,
        report_text=text,
        source_type="metrics",
        source_id=f"{report_code}:{generated_at}:{target_id}" if target_id else f"{report_code}:{generated_at}",
        metadata={
            "level": level,
            "report_type": report_type,
            "summary_limit": summary_limit,
            "target_ip": target_ip,
            "target_id": target_id,
            "target_count": len(active_target_ids),
            "row_count": len(rows),
            "report_generated_at": generated_at,
            "metric_run": run_meta or {},
        },
    )
    marked_daily_report_rows = 0
    if level != "logging":
        # Mark only the rows THIS level actually reported, by result_id.
        #
        # It used to mark the scope `metric_codes x target_ids` of everything fetched, up to the
        # newest collected_at in the fetch — but `rows` is the unfiltered fetch of every status,
        # while only `level_rows` is rendered. So a warning report consumed the critical rows
        # collected before it, and they were never reported by anything.
        #
        # 2026-08-17 on 192.0.2.115: SPID 831 went CRITICAL as a head blocker at 16:15:57
        # (LOCK_BLOCKING_SESSIONS, 43 sessions blocked) and 16:16:10
        # (LOCK_SLEEPING_OPEN_TRANSACTION). The warning report ran at 16:17:26 and marked both.
        # The critical report 44 seconds later announced "Raw Critical Rows: 5" with neither of
        # them in it — the only critical rows that survived were the three collected *after* the
        # warning run's cutoff. The alert that did go out said "3 lock(s), blocked_sessions=1",
        # while the instance had 43 sessions blocked and climbing to 73.
        #
        # `health_rows` is deliberately not marked: it carries every row of an unhealthy target,
        # including its OK ones, which belong to no level's report.
        reported_ids = [int(row["result_id"]) for row in level_rows
                        if str(row["result_id"] or "").strip().lstrip("-").isdigit()]
        if reported_ids:
            metric_store = MetricStore(report_store.target)
            metric_store.initialize()
            marked_daily_report_rows = metric_store.mark_daily_report_created(
                result_ids=reported_ids)
        # The backlog sweep the scope marking was there for, kept — but bounded at the freshness
        # cutoff instead of at the newest row fetched. A row older than the window can never be
        # reported by any level again, so clearing it stops `unreported_only` dragging ancient
        # rows back in; a row *inside* the window still belongs to whichever level's report has
        # not run yet, which is the half that was eating the critical rows.
        swept_metric_codes = {str(row["metric_code"] or "") for row in rows
                              if str(row["metric_code"] or "")}
        if swept_metric_codes and active_target_ids and collected_at_gte:
            marked_daily_report_rows += report_store.mark_metric_daily_report_created_for_scope(
                metric_codes=swept_metric_codes,
                target_ids=active_target_ids,
                collected_at_lte=collected_at_gte,
            )
    return {
        "created": 1,
        "report_ids": [report_id],
        "summary_limit": summary_limit,
        "target_ip": target_ip,
        "target_id": target_id,
        "marked_daily_report_rows": marked_daily_report_rows,
        "skipped": [],
    }


def push_report_alerts(
    *,
    sqlite_path: str | Path,
    telegram_groups: dict[str, str],
    limit: int = 50,
    report_type: str | None = None,
    report_level: str | None = None,
    dedupe_seconds: int = 300,
    report_ids: list[int] | None = None,
    target_id: str | None = None,
    target_ip: str | None = None,
    report_configs: list[dict[str, Any]] | None = None,
    logger: Any | None = None,
) -> dict[str, Any]:
    store = DbOpsStore(sqlite_path)
    target_id = _resolve_report_target_id(store=store, target_ip=target_ip, target_id=target_id)
    reports = store.fetch_reports_for_push(
        limit=limit,
        report_type=report_type,
        report_level=report_level,
        report_ids=report_ids,
        target_id=target_id,
    )
    pushed_ids: list[int] = []
    queued_ids: list[int] = []
    skipped: list[dict[str, Any]] = []

    for report in reports:
        level = str(report["report_level"] or "").lower()
        chat_id = _chat_id_for_level(telegram_groups, level)
        report_id = int(report["report_id"])
        report_code = str(report["report_code"] or "")
        alert_target_id = _report_target_id(report)
        if alert_target_id and not _target_alerts_enabled(alert_target_id):
            reason = f"alerts disabled for target {alert_target_id}"
            store.mark_report_skipped(report_id=report_id, reason=reason)
            skipped.append({"report_id": report_id, "level": level, "reason": reason})
            continue
        send_started_at = datetime.now(timezone.utc)
        if not chat_id:
            reason = f"Telegram group not configured for level={level}."
            store.mark_report_skipped(report_id=report_id, reason=reason)
            skipped.append({"report_id": report_id, "level": level, "reason": reason})
            continue

        source_id = f"report:{report_id}"
        dedupe_source_id = str(report["source_id"] or source_id)
        if dedupe_seconds > 0 and store.recent_alert_exists(
                source_id=dedupe_source_id, within_seconds=dedupe_seconds):
            reason = f"deduped within {dedupe_seconds} seconds"
            store.mark_report_skipped(report_id=report_id, reason=reason)
            skipped.append({"report_id": report_id, "level": level, "reason": reason})
            continue

        message_chunks = split_telegram_message(str(report["report_text"] or ""))
        first_send_message_id = 0
        for chunk_index, message_text in enumerate(message_chunks, start=1):
            send_message_id = queue_message({
                "store": store_block_from(store),
                "chat_id": chat_id,
                "text": message_text,
                # Every chunk carries the report's level, including the `[part 2/2]`
                # continuations. Those start mid-body with no header to read, so under the
                # header heuristic they went out untagged — the tail of a critical report
                # looked like ordinary chatter.
                "level": level,
                "note": f"report_{report['report_type']}",
                "source_type": "reports",
                "source_id": f"{dedupe_source_id}:part:{chunk_index}" if len(message_chunks) > 1 else dedupe_source_id,
                "metadata": {
                    "report_id": report_id,
                    "report_code": report["report_code"],
                    "report_name": report["report_name"],
                    "report_type": report["report_type"],
                    "report_level": level,
                    "dedupe_seconds": dedupe_seconds,
                    "part_index": chunk_index,
                    "part_count": len(message_chunks),
                    "dedupe_source_id": dedupe_source_id,
                },
            }, fallback_store=store)
            if first_send_message_id == 0:
                first_send_message_id = send_message_id
            queued_ids.append(send_message_id)
        store.mark_report_pushed(report_id=report_id, telegram_send_message_id=first_send_message_id)
        send_finished_at = datetime.now(timezone.utc)
        _log_report_timing(
            logger,
            report_code=report_code,
            report_generated_at=str(report["created_at"] or ""),
            report_schedule_evaluated_at=_format_utc(send_started_at),
            report_send_allowed=True,
            report_last_sent_at="",
            report_send_started_at=_format_utc(send_started_at),
            report_send_finished_at=_format_utc(send_finished_at),
            report_age_seconds=_report_age_seconds(report, now=send_started_at),
            report_send_delay_seconds=max(0.0, (send_started_at - _parse_utc(str(report["created_at"] or ""))).total_seconds())
            if _parse_utc(str(report["created_at"] or ""))
            else None,
            scheduler_trigger_time="",
            actual_send_time=_format_utc(send_finished_at),
            skipped_reason="",
        )
        pushed_ids.append(report_id)

    return {
        "queued": len(queued_ids),
        # Backward-compatible name. Reports are queued into telegram_send_messages;
        # actual Telegram API delivery is owned by db_ops.telegram send-queue/run-workflow.
        "pushed": len(pushed_ids),
        "pushed_report_ids": pushed_ids,
        "queued_ids": queued_ids,
        "sent": 0,
        "failed": 0,
        "limit": limit,
        "dedupe_seconds": dedupe_seconds,
        "report_ids_filter": report_ids,
        "target_ip": target_ip,
        "target_id": target_id,
        "next_step": "" if not queued_ids else "Run: python -m db_ops.telegram.cli --config config.json send-queue",
        "skipped": skipped,
    }


def queue_metrics_reports(
    *,
    sqlite_path: str | Path,
    telegram_groups: dict[str, str],
    summary_limit: int = WARNING_LINE_LIMIT,
    dedupe_seconds: int = 300,
    target_id: str | None = None,
    target_ip: str | None = None,
) -> dict[str, Any]:
    store = DbOpsStore(sqlite_path)
    target_id = _resolve_report_target_id(store=store, target_ip=target_ip, target_id=target_id)
    created = create_metrics_reports(sqlite_path=sqlite_path, summary_limit=summary_limit, target_id=target_id)
    pushed = push_report_alerts(
        sqlite_path=sqlite_path,
        telegram_groups=telegram_groups,
        limit=50,
        dedupe_seconds=dedupe_seconds,
        report_ids=[int(report_id) for report_id in created.get("report_ids", [])],
    )
    return {
        "created": created["created"],
        "report_ids": created["report_ids"],
        "queued": pushed["queued"],
        "queued_ids": pushed["queued_ids"],
        "summary_limit": summary_limit,
        "dedupe_seconds": dedupe_seconds,
        "target_ip": target_ip,
        "target_id": target_id,
        "skipped": created["skipped"] + pushed["skipped"],
    }


def _run_one_scheduled_report(
    *,
    store: DbOpsStore,
    sqlite_path: str | Path,
    telegram_groups: dict[str, str],
    report_config: dict[str, Any],
    summary_limit: int,
    backup_days: int,
    evaluated_at: datetime,
    scheduler_trigger_time: str,
    logger: Any | None,
) -> dict[str, Any]:
    report_code = str(report_config["report_code"])
    report_schedule_evaluated_at = _format_utc(evaluated_at)
    channel = _channel_for_report(report_code)
    _migrate_legacy_report_send_state(store=store, report_code=report_code, channel=channel)
    state = store.fetch_report_send_state(report_code=report_code, channel=channel)
    last_sent_at = str(state["last_sent_at"] or "") if state else ""
    skipped_reason = _schedule_skip_reason(report_config=report_config, evaluated_at=evaluated_at, last_sent_at=last_sent_at)
    if skipped_reason:
        store.upsert_report_send_state(
            report_code=report_code,
            channel=channel,
            last_run_at=report_schedule_evaluated_at,
            last_status="skipped",
            last_skipped_reason=skipped_reason,
        )
        _log_report_timing(
            logger,
            report_code=report_code,
            report_generated_at="",
            report_schedule_evaluated_at=report_schedule_evaluated_at,
            report_send_allowed=False,
            report_last_sent_at=last_sent_at,
            report_send_started_at="",
            report_send_finished_at="",
            report_age_seconds=None,
            report_send_delay_seconds=None,
            scheduler_trigger_time=scheduler_trigger_time,
            actual_send_time="",
            skipped_reason=skipped_reason,
        )
        return {"report_code": report_code, "created": 0, "queued": 0, "skipped_reason": skipped_reason}

    created = _create_scheduled_report(
        sqlite_path=sqlite_path,
        report_code=report_code,
        report_config=report_config,
        summary_limit=summary_limit,
        backup_days=backup_days,
    )
    if not created.get("report_ids"):
        reason = "; ".join(str(item.get("reason", "")) for item in created.get("skipped", []) if item.get("reason")) or "no report created"
        store.upsert_report_send_state(
            report_code=report_code,
            channel=channel,
            last_run_at=report_schedule_evaluated_at,
            last_status="skipped",
            last_skipped_reason=reason,
        )
        _log_report_timing(
            logger,
            report_code=report_code,
            report_generated_at="",
            report_schedule_evaluated_at=report_schedule_evaluated_at,
            report_send_allowed=False,
            report_last_sent_at=last_sent_at,
            report_send_started_at="",
            report_send_finished_at="",
            report_age_seconds=None,
            report_send_delay_seconds=None,
            scheduler_trigger_time=scheduler_trigger_time,
            actual_send_time="",
            skipped_reason=reason,
        )
        return {"report_code": report_code, "created": 0, "queued": 0, "skipped_reason": reason}

    send_started_at = datetime.now(timezone.utc)
    pushed = push_report_alerts(
        sqlite_path=sqlite_path,
        telegram_groups=telegram_groups,
        report_ids=[int(report_id) for report_id in created["report_ids"]],
        dedupe_seconds=0,
        report_configs=[report_config],
        logger=logger,
    )
    send_finished_at = datetime.now(timezone.utc)
    if pushed.get("queued", 0):
        store.upsert_report_send_state(
            report_code=report_code,
            channel=channel,
            last_run_at=report_schedule_evaluated_at,
            last_status="queued",
            last_skipped_reason="",
            last_sent_at=_format_utc(send_finished_at),
        )
        skipped_reason = ""
    else:
        skipped_reason = "; ".join(str(item.get("reason", "")) for item in pushed.get("skipped", []) if item.get("reason")) or "push skipped"
        store.upsert_report_send_state(
            report_code=report_code,
            channel=channel,
            last_run_at=report_schedule_evaluated_at,
            last_status="skipped",
            last_skipped_reason=skipped_reason,
        )

    _log_report_timing(
        logger,
        report_code=report_code,
        report_generated_at=report_schedule_evaluated_at,
        report_schedule_evaluated_at=report_schedule_evaluated_at,
        report_send_allowed=bool(pushed.get("queued", 0)),
        report_last_sent_at=last_sent_at,
        report_send_started_at=_format_utc(send_started_at),
        report_send_finished_at=_format_utc(send_finished_at),
        report_age_seconds=max(0.0, (send_started_at - evaluated_at).total_seconds()),
        report_send_delay_seconds=max(0.0, (send_started_at - evaluated_at).total_seconds()),
        scheduler_trigger_time=scheduler_trigger_time,
        actual_send_time=_format_utc(send_finished_at) if pushed.get("queued", 0) else "",
        skipped_reason=skipped_reason,
    )
    return {
        "report_code": report_code,
        "created": int(created.get("created", 0)),
        "report_ids": created.get("report_ids", []),
        "queued": int(pushed.get("queued", 0)),
        "queued_ids": pushed.get("queued_ids", []),
        "skipped_reason": skipped_reason,
    }


def _create_scheduled_report(
    *,
    sqlite_path: str | Path,
    report_code: str,
    report_config: dict[str, Any],
    summary_limit: int,
    backup_days: int,
) -> dict[str, Any]:
    if report_code in METRIC_REPORT_LEVELS:
        return create_metric_report_for_level(
            sqlite_path=sqlite_path,
            report_code=report_code,
            level=METRIC_REPORT_LEVELS[report_code],
            summary_limit=summary_limit,
            freshness_seconds=_metric_max_age_seconds(report_config),
            unreported_only=True,
            display_name=str(report_config.get("display_name") or "").strip() or None,
        )
    if report_code == BACKUP_HEALTH_CODE:
        return create_backup_health_report(
            sqlite_path=sqlite_path,
            days=backup_days,
            force=True,
            display_name=str(report_config.get("display_name") or "").strip() or None,
        )
    return {"created": 0, "report_ids": [], "skipped": [{"reason": f"unknown report_code {report_code}"}]}


def _schedule_skip_reason(*, report_config: dict[str, Any], evaluated_at: datetime, last_sent_at: str) -> str:
    if not bool(report_config.get("active", True)):
        return "inactive"
    local_now = evaluated_at.astimezone(VIETNAM_TZ)
    time_window = _report_time_window(report_config)
    closed_reason = time_window_closed_reason(time_window, local_now)
    if closed_reason:
        return closed_reason
    # Shared run-once convention: repeat_interval=0 => send once (never repeat after sent).
    last_sent = _parse_utc(last_sent_at)
    if last_sent is not None and not repeat_due(last_sent, time_window.repeat_interval, evaluated_at, default=300):
        if time_window.repeat_interval == 0:
            return "run-once: already sent"
        elapsed = int((evaluated_at - last_sent).total_seconds())
        repeat_interval = int(time_window.repeat_interval or 300)
        return f"repeat interval not elapsed: {elapsed}s/{repeat_interval}s"
    return ""


def _report_time_window(report_config: dict[str, Any]) -> TimeWindow:
    return parse_time_window_config(report_config, context="reports_config").time_window


def _log_report_time_window_warnings(logger: Any | None, warnings: tuple[str, ...]) -> None:
    if logger is None:
        return
    from db_ops.logging_ops import log_event

    for message in warnings:
        log_event(logger, level="warning", message=f"reports.config.deprecated_time_window|scope=reports|message={message}")


def _channel_for_report(report_code: str) -> str:
    if report_code.endswith("_critical"):
        return "critical"
    if report_code.endswith("_warning"):
        return "warning"
    return "logging"


def _migrate_legacy_report_send_state(*, store: DbOpsStore, report_code: str, channel: str) -> None:
    if report_code != DAILY_LOGGING_CODE:
        return
    if store.fetch_report_send_state(report_code=DAILY_LOGGING_CODE, channel=channel):
        return
    legacy_state = store.fetch_report_send_state(report_code=LEGACY_DAILY_LOGGING_CODE, channel=channel)
    if not legacy_state:
        return
    store.upsert_report_send_state(
        report_code=DAILY_LOGGING_CODE,
        channel=channel,
        last_sent_at=str(legacy_state["last_sent_at"] or "") or None,
        last_run_at=str(legacy_state["last_run_at"] or "") or None,
        last_status=str(legacy_state["last_status"] or ""),
        last_skipped_reason=str(legacy_state["last_skipped_reason"] or ""),
    )


def _metric_max_age_seconds_from_configs(report_configs: list[dict[str, Any]], *, report_code: str) -> int | None:
    for item in report_configs:
        if str(item.get("report_code") or "") == report_code and item.get("metric_max_age_seconds") is not None:
            return int(item["metric_max_age_seconds"])
    return None


def _report_display_name_from_configs(report_configs: list[dict[str, Any]], *, report_code: str) -> str:
    for item in report_configs:
        if str(item.get("report_code") or "") == report_code:
            return str(item.get("display_name") or report_code).strip() or report_code
    return report_code


def _metric_max_age_seconds(report_config: dict[str, Any]) -> int | None:
    return int(report_config["metric_max_age_seconds"])


def _configured_metric_max_age_seconds(report_code: str) -> int | None:
    try:
        return _metric_max_age_seconds_from_configs(load_report_configs(), report_code=report_code)
    except (FileNotFoundError, RuntimeError, ValueError):
        return None


def _metric_report_display_name(report_code: str) -> str:
    try:
        return _report_display_name_from_configs(load_report_configs(), report_code=report_code)
    except (FileNotFoundError, RuntimeError, ValueError):
        return report_code


def _report_code_for_metric_level(level: str) -> str:
    for report_code, report_level in METRIC_REPORT_LEVELS.items():
        if report_level == level:
            return report_code
    return f"METRICS_LATEST_{level.upper()}"


def _metric_freshness_cutoff(*, seconds: int | None) -> str | None:
    if seconds is None or seconds <= 0:
        return None
    return _format_utc(datetime.now(timezone.utc) - timedelta(seconds=int(seconds)))


def _report_age_seconds(report: object, *, now: datetime) -> float | None:
    created_at = _parse_utc(str(report["created_at"] or ""))
    if not created_at:
        return None
    return max(0.0, (now - created_at).total_seconds())




def _log_report_timing(
    logger: Any | None,
    *,
    report_code: str,
    report_generated_at: str,
    report_schedule_evaluated_at: str,
    report_send_allowed: bool,
    report_last_sent_at: str,
    report_send_started_at: str,
    report_send_finished_at: str,
    report_age_seconds: float | None,
    report_send_delay_seconds: float | None,
    scheduler_trigger_time: str,
    actual_send_time: str,
    skipped_reason: str,
) -> None:
    if logger is None:
        return
    from db_ops.logging_ops import log_event

    log_event(
        logger,
        level="logging",
        message=(
            "report_timing "
            f"report_code={report_code} "
            f"report_generated_at={report_generated_at} "
            f"report_schedule_evaluated_at={report_schedule_evaluated_at} "
            f"report_send_allowed={report_send_allowed} "
            f"report_last_sent_at={report_last_sent_at} "
            f"report_send_started_at={report_send_started_at} "
            f"report_send_finished_at={report_send_finished_at} "
            f"report_age_seconds={'' if report_age_seconds is None else round(report_age_seconds, 3)} "
            f"report_send_delay_seconds={'' if report_send_delay_seconds is None else round(report_send_delay_seconds, 3)} "
            f"scheduler_trigger_time={scheduler_trigger_time} "
            f"actual_send_time={actual_send_time} "
            f"skipped_reason={skipped_reason}"
        ),
    )


def _resolve_report_target_id(*, store: DbOpsStore, target_ip: str | None, target_id: str | None) -> str | None:
    if not target_ip:
        return target_id
    config_target = resolve_config_metric_target(
        target_ip=target_ip,
        target_id=target_id,
        data_dir=DEFAULT_DATA_DIR,
    )
    if config_target is not None:
        if not bool(getattr(config_target, "reports_enabled", True)):
            raise ValueError(f"report disabled for target {target_ip}")
        return str(config_target.target_id)
    rows = store.fetch_latest_metric_report_results(target_id=target_id, target_ip=target_ip)
    target_ids = sorted({str(row["target_id"] or "") for row in rows if str(row["target_id"] or "")})
    if not target_ids:
        raise ValueError(f"No stored metric data found for target_ip={target_ip}. Run Metrics Engine first.")
    if len(target_ids) > 1:
        raise ValueError(f"Ambiguous stored metric data for target_ip={target_ip}; matched target_ids: {', '.join(target_ids)}.")
    return target_ids[0]


def _collect_only_metric_codes() -> set[str]:
    """Metric codes that are collected and stored but kept OUT of the scheduled metric reports.

    Some metrics are an **inventory**, not a signal. MAINTENANCE_INDEX_USAGE emits one row per
    index - ~29k on a single large database - all of them status OK by design. Those rows are
    wanted in the store, because the inventory report renders them and an operator queries them;
    putting them through the hourly warning/critical report would bury every real alert under
    thousands of lines that are not alerts at all.

    Declared per metric in ``data/metric_definitions.json`` as
    ``report_policy.collect_only: true`` - config, not a hard-coded list here, so adding the next
    inventory metric needs no code change.
    """
    codes: set[str] = set()
    try:
        data = {"metrics": data_sources.load_metric_definition_records(
            DEFAULT_METRIC_DEFINITIONS_PATH)}  # one reader; see common.data_sources
    except (OSError, json.JSONDecodeError):
        # A broken definitions file must not silently empty every report.
        return codes
    for item in data.get("metrics") or []:
        if not isinstance(item, dict):
            continue
        policy = item.get("report_policy")
        if isinstance(policy, dict) and bool(policy.get("collect_only")):
            codes.add(str(item.get("metric_code") or "").upper())
    codes.discard("")
    return codes


def _chart_summary_only_metric_codes() -> set[str]:
    """Metrics whose per-item DETAIL must never reach a chart series - only their aggregate rows.

    Separate from ``collect_only`` on purpose, because the two answer different questions:

    * ``collect_only``       - "this is maintenance work, not an alert" -> keep it out of the
                               warning/critical reports. True for fragmentation AND index usage.
    * ``chart_summary_only`` - "there are tens of thousands of these" -> keep the detail out of
                               anything that loads a per-server series. True only for index usage.

    Conflating them dropped MAINTENANCE_INDEX_FRAGMENTATION from the per-server report, where its
    per-index rows are wanted and are few enough to chart.

    Declared as ``report_policy.chart_summary_only`` in ``data/metric_definitions.json``.
    """
    codes: set[str] = set()
    try:
        data = {"metrics": data_sources.load_metric_definition_records(
            DEFAULT_METRIC_DEFINITIONS_PATH)}  # one reader; see common.data_sources
    except (OSError, json.JSONDecodeError):
        return codes
    for item in data.get("metrics") or []:
        if not isinstance(item, dict):
            continue
        policy = item.get("report_policy")
        if isinstance(policy, dict) and bool(policy.get("chart_summary_only")):
            codes.add(str(item.get("metric_code") or "").upper())
    codes.discard("")
    return codes


def _filter_rows_for_reports(rows: list[object]) -> list[object]:
    collect_only = _collect_only_metric_codes()
    if collect_only:
        # Rows are mapping-like (sqlite3.Row / dict), the same access the target filter below
        # uses. A row without the column is kept: dropping data because of a shape surprise would
        # hide metrics rather than filter them.
        # An inventory metric contributes no alerts, so both its detail AND its aggregate stay out
        # of the warning/critical reports. The aggregate is for the inventory and per-server
        # reports, which apply this same rule in reverse - see server_report._drop_collect_only.
        rows = [row for row in rows if row_text(row, "metric_code").upper() not in collect_only]
    targets = load_config_metric_targets(data_dir=DEFAULT_DATA_DIR)
    if not targets:
        return rows
    reports_enabled_by_id = {str(target.target_id): bool(getattr(target, "reports_enabled", True)) for target in targets}
    return [row for row in rows if reports_enabled_by_id.get(str(row["target_id"] or ""), True)]


def _target_alerts_enabled(target_id: str) -> bool:
    targets = load_config_metric_targets(data_dir=DEFAULT_DATA_DIR)
    if not targets:
        return True
    matches = [target for target in targets if str(target.target_id) == str(target_id)]
    if not matches:
        return True
    return bool(getattr(matches[0], "alerts_enabled", True))


def _report_target_id(report: object) -> str:
    try:
        metadata = json.loads(str(report["metadata_json"] or "{}"))
    except json.JSONDecodeError:
        return ""
    return str(metadata.get("target_id") or "")


def _fetch_backup_metric_rows(store: DbOpsStore, *, cutoff: datetime) -> list[object]:
    """The backup-health evidence rows, from the store that owns ``metric_results``.

    A MetricStore built on the same target rather than a raw query through ``store``: both wrap the
    one physical store, but the table belongs to MetricStore and the SQL belongs with the table
    (2026-08-11 audit). Cheap — the target is already resolved and each call opens its own
    connection either way.
    """
    metric_codes = _backup_health_metric_codes()
    if not metric_codes:
        return []
    metric_store = MetricStore(store.target)
    metric_store.initialize()
    return list(metric_store.fetch_results_since(
        metric_codes=metric_codes,
        since=cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
        db_type="sqlserver",
    ))


def _backup_health_metric_codes() -> list[str]:
    return sorted(_backup_health_roles())


def _backup_health_roles() -> dict[str, str]:
    policies = _load_report_policy_context()["metric_policies"]
    roles: dict[str, str] = {}
    for metric_code, policy in policies.items():
        backup_policy = policy.get("backup_health")
        if not isinstance(backup_policy, dict):
            continue
        role = str(backup_policy.get("role") or "").strip()
        if role:
            roles[metric_code] = role
    return roles


def _format_metric_history_report(
    *,
    rows: list[object],
    total_row_count: int,
    server_id: str,
    metric_code: str,
    hours: int,
    window_start: datetime,
    window_end: datetime,
    target_ids: list[str],
    status_counts: dict[str, int],
) -> str:
    status_text = ", ".join(f"{status}={count}" for status, count in status_counts.items() if count) or "unknown"
    local_start = window_start.astimezone(VIETNAM_TZ).strftime("%Y-%m-%d %H:%M:%S")
    local_end = window_end.astimezone(VIETNAM_TZ).strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "[Metric History Report]",
        f"Server ID: {server_id}",
        f"Metric: {metric_code}",
        f"Window: {local_start} to {local_end} UTC+07:00 (last {hours} hour(s))",
        f"Rows: {total_row_count}; displayed: {len(rows)}; targets: {len(target_ids)}",
        f"Status: {status_text}",
    ]
    if target_ids:
        lines.append(f"Target IDs: {', '.join(target_ids)}")
    omitted_count = max(0, total_row_count - len(rows))
    if omitted_count:
        lines.append(f"... {omitted_count} older row(s) omitted by summary limit")
    lines.extend(["", "Time (UTC+07) | Target ID | Item | Value | Status | Message"])
    for row in rows:
        raw_unit = str(row["metric_unit"] or "").strip()
        value = _value_with_unit(
            _metric_history_cell(row["metric_value"], limit=80),
            _compact_unit(_metric_history_cell(raw_unit, limit=30)) if raw_unit else "",
        )
        lines.append(
            " | ".join(
                (
                    _local_time_text(str(row["collected_at"] or "")),
                    _metric_history_cell(row["target_id"], limit=100),
                    _metric_history_cell(row["metric_item"], limit=100),
                    value,
                    _metric_history_cell(row["status"], limit=20).upper(),
                    _metric_history_cell(row["message"], limit=180),
                )
            )
        )
    return "\n".join(lines)


def _metric_history_cell(value: object, *, limit: int) -> str:
    text = " ".join(str(value or "").replace("|", "/").replace("\r", " ").replace("\n", " ").split())
    if not text:
        return "-"
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 3)].rstrip() + "..."


def _format_backup_health_report(
    *,
    rows: list[object],
    targets: list[object],
    days: int,
    generated_at: datetime,
    report_title: str,
) -> str:
    target_lookup = {str(target.target_id): target for target in targets}
    instance_stats = _backup_instance_stats(rows=rows, target_lookup=target_lookup)
    db_stats = [db for instance in instance_stats for db in instance["databases"]]

    full_ok = sum(_backup_type_ok_count(item, "FULL") for item in db_stats)
    full_obs = sum(_backup_type_observed_count(item, "FULL") for item in db_stats)
    diff_ok = sum(_backup_type_ok_count(item, "DIFF") for item in db_stats)
    diff_obs = sum(_backup_type_observed_count(item, "DIFF") for item in db_stats)
    log_ok = sum(_backup_type_ok_count(item, "LOG") for item in db_stats)
    log_obs = sum(_backup_type_observed_count(item, "LOG") for item in db_stats)
    stale_count = sum(1 for item in db_stats if item["backup_age_status"] in {"WARNING", "CRITICAL", "ERROR"})
    job_issue_count = sum(1 for item in instance_stats if item["job_status"] in {"WARNING", "CRITICAL", "ERROR"})

    lines = [
        f"[{report_title}]",
        f"Generated: {generated_at.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Window: last {days} days",
        "Source: SQLite metric_results only; grouped by SQL Server database",
        "",
        "[SUMMARY]",
        f"SQL Server instances: {len(instance_stats)}",
        f"SQL Server databases: {len(db_stats)}",
        f"FULL events: {full_ok}/{full_obs} OK",
        f"DIFF events: {diff_ok}/{diff_obs} OK",
        f"LOG events: {log_ok}/{log_obs} OK",
        f"Backup age issues: {stale_count}",
        f"Backup job issues: {job_issue_count}",
        "",
    ]

    for instance in sorted(instance_stats, key=lambda item: str(item["server_label"]).lower()):
        items = instance["databases"]
        server_full_ok = sum(_backup_type_ok_count(item, "FULL") for item in items)
        server_full_obs = sum(_backup_type_observed_count(item, "FULL") for item in items)
        server_diff_ok = sum(_backup_type_ok_count(item, "DIFF") for item in items)
        server_diff_obs = sum(_backup_type_observed_count(item, "DIFF") for item in items)
        server_log_ok = sum(_backup_type_ok_count(item, "LOG") for item in items)
        server_log_obs = sum(_backup_type_observed_count(item, "LOG") for item in items)
        lines.append(
            f"[{instance['server_label']}] DB={len(items)} "
            f"FULL={server_full_ok}/{server_full_obs} DIFF={server_diff_ok}/{server_diff_obs} "
            f"LOG={server_log_ok}/{server_log_obs} job={instance['job_status'] or 'unknown'}"
        )
        for item in sorted(items, key=lambda value: str(value["database_name"]).lower()):
            lines.append(_backup_db_line(item))
        lines.append("")

    return "\n".join(lines).strip()


def _backup_instance_stats(*, rows: list[object], target_lookup: dict[str, object]) -> list[dict[str, Any]]:
    instances: dict[str, dict[str, Any]] = {}
    roles = _backup_health_roles()
    for row in rows:
        target_id = str(row["target_id"] or "")
        if not target_id:
            continue
        instance = instances.setdefault(target_id, _empty_backup_instance(target_id, target_lookup.get(target_id), row))
        role = roles.get(str(row["metric_code"] or "").upper(), "")
        status = str(row["status"] or "").upper()
        if role == "job_status":
            instance["job_status"] = _worse_status(str(instance["job_status"]), status)
            if not instance["job_message"] and str(row["message"] or "").strip():
                instance["job_message"] = _short_message(str(row["message"] or ""))
            continue

        if _synthetic_backup_metric_row(row):
            continue

        database_name = _backup_database_name(row)
        if not database_name:
            continue
        db_stats = instance["database_map"].setdefault(database_name, _empty_backup_database(target_id, database_name))
        if role == "last_result":
            backup_type = _backup_type(row)
            if backup_type in db_stats["types"]:
                event_key = _backup_event_key(row, backup_type=backup_type)
                db_stats["types"][backup_type]["observed_events"].add(event_key)
                if status in {"OK", "LOGGING"}:
                    db_stats["types"][backup_type]["ok_events"].add(event_key)
                finish = _message_field(str(row["message"] or ""), "backup_finish_date")
                if finish and finish != "NULL":
                    current = str(db_stats["types"][backup_type]["last_finish"])
                    db_stats["types"][backup_type]["last_finish"] = max(current, finish) if current else finish
        elif role == "age":
            db_stats["backup_age_status"] = _worse_status(str(db_stats["backup_age_status"]), status)
            value = str(row["metric_value"] or "").strip()
            if value and (not db_stats["backup_age_hours"] or _safe_float(value) > _safe_float(str(db_stats["backup_age_hours"]))):
                db_stats["backup_age_hours"] = value

    for target_id, target in target_lookup.items():
        instances.setdefault(target_id, _empty_backup_instance(target_id, target, None))

    result = []
    for instance in instances.values():
        instance["databases"] = list(instance["database_map"].values())
        if instance["databases"]:
            result.append(instance)
    return result


def _empty_backup_instance(target_id: str, target: object | None, row: object | None) -> dict[str, Any]:
    ip = str(getattr(target, "ip", "") or (row["ip"] if row else "") or "")
    instance = str(getattr(target, "instance_name", "") or "")
    server = str(getattr(target, "server_id", "") or (row["server_id"] if row else "") or "")
    server_label = " ".join(part for part in [server, ip, instance] if part).strip() or target_id or "unknown"
    return {
        "target_id": target_id,
        "server_label": server_label,
        "database_map": {},
        "databases": [],
        "job_status": "",
        "job_message": "",
    }


def _empty_backup_database(target_id: str, database_name: str) -> dict[str, Any]:
    type_stats = {
        "FULL": {"observed_events": set(), "ok_events": set(), "last_finish": ""},
        "DIFF": {"observed_events": set(), "ok_events": set(), "last_finish": ""},
        "LOG": {"observed_events": set(), "ok_events": set(), "last_finish": ""},
    }
    return {
        "target_id": target_id,
        "database_name": database_name,
        "types": type_stats,
        "backup_age_status": "",
        "backup_age_hours": "",
    }


def _backup_db_line(item: dict[str, Any]) -> str:
    age = f"age={item['backup_age_hours']}h/{item['backup_age_status']}" if item["backup_age_hours"] else "age=unknown"
    return (
        f"- {item['database_name']}: "
        f"FULL {_backup_type_ok_count(item, 'FULL')}/{_backup_type_observed_count(item, 'FULL')}; "
        f"DIFF {_backup_type_ok_count(item, 'DIFF')}/{_backup_type_observed_count(item, 'DIFF')}; "
        f"LOG {_backup_type_ok_count(item, 'LOG')}/{_backup_type_observed_count(item, 'LOG')}; "
        f"{age}"
    )


def _backup_type(row: object) -> str:
    message_type = _message_field(str(row["message"] or ""), "backup_type")
    if message_type:
        return message_type.upper()
    item = str(row["metric_item"] or "").upper()
    if "FULL" in item:
        return "FULL"
    if "DIFF" in item:
        return "DIFF"
    if "LOG" in item:
        return "LOG"
    return "UNKNOWN"


def _backup_database_name(row: object) -> str:
    message_database = _message_field(str(row["message"] or ""), "database")
    if message_database:
        return message_database
    item = str(row["metric_item"] or "").strip()
    if " / " in item:
        return item.split(" / ", 1)[0].strip()
    return item


def _synthetic_backup_metric_row(row: object) -> bool:
    role = _backup_health_roles().get(str(row["metric_code"] or "").upper(), "")
    if role not in {"last_result", "age"}:
        return False
    message = _short_message(str(row["message"] or "")).lower()
    if not message:
        return False
    return (
        "sql returned no rows" in message
        or "sql execution failed" in message
        or message in {"connection failed", "connection timeout"}
    )


def _backup_type_observed_count(item: dict[str, Any], backup_type: str) -> int:
    return len(item["types"][backup_type]["observed_events"])


def _backup_type_ok_count(item: dict[str, Any], backup_type: str) -> int:
    return len(item["types"][backup_type]["ok_events"])


def _backup_event_key(row: object, *, backup_type: str) -> str:
    finish = _message_field(str(row["message"] or ""), "backup_finish_date")
    if finish and finish != "NULL":
        return f"{backup_type}:{finish}"
    inferred = _inferred_backup_finish_hour(row)
    if inferred:
        return f"{backup_type}:inferred:{inferred}"
    return f"{backup_type}:result:{row['result_id']}"


def _inferred_backup_finish_hour(row: object) -> str:
    value = str(row["metric_value"] or "").strip()
    if not value:
        return ""
    try:
        age_hours = float(value)
        collected_at = datetime.fromisoformat(str(row["collected_at"]).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return ""
    finish_at = collected_at - timedelta(hours=age_hours)
    return finish_at.strftime("%Y-%m-%d %H")


def _message_field(message: str, field_name: str) -> str:
    match = re.search(rf"(?:^|,\s*){re.escape(field_name)}=([^,]+)", message)
    return match.group(1).strip() if match else ""


def _worse_status(left: str, right: str) -> str:
    order = {"": 0, "OK": 1, "LOGGING": 1, "NO_DATA": 2, "WARNING": 3, "ERROR": 3, "CRITICAL": 4}
    return right if order.get(right.upper(), 0) >= order.get(left.upper(), 0) else left


def _safe_float(value: str) -> float:
    try:
        return float(value)
    except ValueError:
        return -1.0


def _format_level_report(
    *,
    rows: list[object],
    level: str,
    summary_limit: int,
    target_labels: dict[str, str],
    targets: list[object],
    definitions: list[object],
    run_meta: dict[str, Any] | None,
    report_title: str,
    report_window_seconds: int | None,
) -> str:
    policy_context = _load_report_policy_context()
    policy_result = apply_report_policy(
        rows,
        metric_policies=policy_context["metric_policies"],
        instance_policies=policy_context["instance_policies"],
    )
    raw_level_count = len(_rows_for_level(policy_result.effective_rows, level))
    rows = policy_result.rows
    summary_rows = _rows_for_level(rows, level)
    labels = _target_labels(rows_by_target(rows), fallback_labels=target_labels)
    counts = _status_counts(rows)
    run_time = _run_time_text(run_meta) or _latest_run_time(rows)

    if level == "logging":
        return _format_logging_report(
            report_title=report_title,
            rows=rows,
            run_time=run_time,
            duration_text=_duration_text(run_meta, report_window_seconds=report_window_seconds),
            counts=counts,
            targets=targets,
            definitions=definitions,
            report_window_seconds=report_window_seconds,
        )
    if level == "critical":
        return _format_metric_event_report(
            report_title=report_title,
            rows=summary_rows,
            labels=labels,
            run_time=run_time,
            duration_text=_duration_text(run_meta, report_window_seconds=report_window_seconds),
            severity_label="CRITICAL",
            section_title="[CRITICAL EVENTS]",
            raw_row_count=raw_level_count,
        )

    return _format_metric_event_report(
        report_title=report_title,
        rows=summary_rows,
        labels=labels,
        run_time=run_time,
        duration_text=_duration_text(run_meta, report_window_seconds=report_window_seconds),
        severity_label="WARNING",
        section_title="[WARNING EVENTS]",
        raw_row_count=raw_level_count,
    )


def _format_logging_report(
    *,
    report_title: str,
    rows: list[object],
    run_time: str,
    duration_text: str,
    counts: dict[str, int],
    targets: list[object],
    definitions: list[object],
    report_window_seconds: int | None,
) -> str:
    lines = [
        f"[{report_title}]",
        f"Run: {run_time}",
        f"Duration: {duration_text}",
        "",
        "[TRACKING TARGETS]",
    ]
    lines.extend(_tracking_targets_lines(targets))
    lines.extend(["", "[TRACKING METRICS]"])
    lines.extend(_tracking_metrics_lines(definitions, targets=targets, rows=rows, report_window_seconds=report_window_seconds))
    lines.extend(
        [
            "",
            "[RESULT SUMMARY]",
            f"OK: {counts['OK']}",
            f"WARNING: {counts['WARNING'] + counts['NO_DATA'] + counts['ERROR']}",
            f"CRITICAL: {counts['CRITICAL']}",
        ]
    )
    return "\n".join(lines).strip()


def _format_metric_event_report(
    *,
    report_title: str,
    rows: list[object],
    labels: dict[str, str],
    run_time: str,
    duration_text: str,
    severity_label: str,
    section_title: str,
    raw_row_count: int | None = None,
) -> str:
    rendered_events = render_metric_events(
        rows,
        severity_label=severity_label,
        section_title=section_title,
        labels=labels,
    )
    lines = [
        f"[{report_title}]",
        f"Run: {run_time}",
        f"Duration: {duration_text}",
        f"Raw {severity_label.title()} Rows: {raw_row_count if raw_row_count is not None else len(rows)}",
        f"{severity_label.title()} Events: {len(rendered_events)}",
        "",
        section_title,
    ]
    lines.extend(rendered_events)
    return "\n".join(lines).strip()


def render_metric_events(
    events: list[object],
    *,
    severity_label: str,
    section_title: str,
    labels: dict[str, str],
) -> list[str]:
    del severity_label, section_title
    return [f"- {_alert_line(row, labels=labels)}" for row in events]


def _policy_event_groups(events: list[object], *, labels: dict[str, str]) -> list[tuple[str, list[str]]]:
    groups: list[tuple[str, list[str]]] = []
    for event in sorted(events, key=lambda item: (str(getattr(item, "title_key", "")), str(getattr(item, "event_code", "")))):
        rows = list(getattr(event, "rows", []) or [])
        first = rows[0] if rows else {}
        target = labels.get(str(first.get("target_id") or ""), _fallback_target(first)) if isinstance(first, dict) else str(getattr(event, "title_key", ""))
        event_code = str(getattr(event, "event_code", "EVENT"))
        title = f"[{target} / {event_code}]"
        groups.append((title, [f"- {render_policy_event(event)}"]))
    return groups


def _load_report_policy_context() -> dict[str, dict[str, dict[str, Any]]]:
    metric_policies: dict[str, dict[str, Any]] = {}
    for item in data_sources.load_metric_definition_records(DEFAULT_METRIC_DEFINITIONS_PATH):
        metric_code = str(item.get("metric_code") or "").upper()
        if not metric_code:
            continue
        metric_policies[metric_code] = _normalize_report_policy(item)

    instance_policies: dict[str, dict[str, Any]] = {}
    for target in load_config_metric_targets(data_dir=DEFAULT_DATA_DIR):
        raw_config = getattr(target, "raw_config", {}) or {}
        instance_policies[str(target.target_id)] = {
            "env": str(raw_config.get("env") or "").strip().lower(),
            "report_policy": raw_config.get("report_policy") if isinstance(raw_config.get("report_policy"), dict) else {},
        }
    return {"metric_policies": metric_policies, "instance_policies": instance_policies}


def _normalize_report_policy(item: dict[str, Any]) -> dict[str, Any]:
    policy = item.get("report_policy") if isinstance(item.get("report_policy"), dict) else {}
    normalized = dict(policy)
    normalized["collector_type"] = str(item.get("collector_type") or "").strip()
    normalized["category"] = str(item.get("category") or "").strip()
    if "technical_error" not in normalized and isinstance(policy.get("collapse"), dict):
        collapse = policy.get("collapse") or {}
        if bool(collapse.get("enabled", False)):
            normalized["technical_error"] = {
                "collapse_enabled": True,
                "event_code_template": collapse.get("event_code") or "MONITORING_{collector_type}_UNAVAILABLE",
                "key_fields": _legacy_policy_fields(collapse.get("key_fields") or []),
                "replace_raw_rows": True,
                "max_sample_metrics": collapse.get("max_sample_metrics", 3),
                "max_sample_items": collapse.get("max_sample_items", 5),
            }
    if "severity_policy" not in normalized:
        severity = policy.get("severity") if isinstance(policy.get("severity"), dict) else {}
        thresholds = policy.get("thresholds") if isinstance(policy.get("thresholds"), dict) else {}
        env_actions = {"prod": "KEEP", "dev": "KEEP", "lab": "KEEP", "sandbox": "KEEP"} if normalized.get("technical_error") else {
            "prod": "KEEP",
            "dev": _legacy_severity_action(severity.get("dev")),
            "lab": _legacy_severity_action(severity.get("lab")),
            "sandbox": _legacy_severity_action(severity.get("sandbox")),
        }
        normalized["severity_policy"] = {
            "default": "KEEP",
            "by_env": env_actions,
            "thresholds": _legacy_thresholds(thresholds),
        }
    if "condition_grouping" not in normalized and bool((policy.get("collapse") or {}).get("enabled", False)):
        collapse = policy.get("collapse") or {}
        normalized["condition_grouping"] = {
            "enabled": not bool(normalized.get("technical_error", {}).get("collapse_enabled", False)),
            "key_fields": _legacy_policy_fields(collapse.get("key_fields") or []),
            "aggregate_fields": {
                "count": "count",
                "max_value": {"op": "max", "field": "metric_value"},
                "sample_items": {"op": "sample", "field": "metric_item", "limit": collapse.get("max_sample_items", 5)},
            },
            "replace_raw_rows": True,
        }
    return normalized


def _legacy_policy_fields(fields: list[object]) -> list[str]:
    aliases = {"instance": "instance_key"}
    return [aliases.get(str(field), str(field)) for field in fields]


def _legacy_severity_action(value: object) -> str:
    severity = str(value or "").upper()
    if severity == "WARNING":
        return "DOWNGRADE_TO_WARNING"
    if severity in {"LOGGING", "OK"}:
        return "DOWNGRADE_TO_LOGGING"
    return "KEEP"


def _legacy_thresholds(thresholds: dict[str, Any]) -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = []
    warning_idle = thresholds.get("warning_idle_minutes")
    critical_idle = thresholds.get("critical_idle_minutes")
    unless = [
        {"field": "is_blocking", "op": "=", "value": True},
        {"field": "has_x_lock", "op": "=", "value": True},
    ]
    if warning_idle is not None:
        rules.append({"field": "idle_minutes", "op": "<", "value": warning_idle, "severity": "SUPPRESS", "unless": unless})
        rules.append({"field": "idle_minutes", "op": ">=", "value": warning_idle, "severity": "WARNING"})
    if critical_idle is not None:
        rules.append({"field": "idle_minutes", "op": ">=", "value": critical_idle, "severity": "CRITICAL"})
    if thresholds.get("critical_if_blocking"):
        rules.append({"field": "is_blocking", "op": "=", "value": True, "severity": "CRITICAL"})
    if thresholds.get("critical_if_x_lock"):
        rules.append({"field": "has_x_lock", "op": "=", "value": True, "severity": "CRITICAL"})
    return rules


def _rows_for_level(rows: list[object], level: str) -> list[object]:
    statuses_by_level = {
        "critical": {"CRITICAL"},
        "warning": {"WARNING", "NO_DATA", "ERROR"},
        "logging": {"OK", "LOGGING"},
    }
    statuses = statuses_by_level[level]
    return [row for row in rows if _report_status(row) in statuses]


def _health_rows_for_level(rows: list[object], level: str) -> list[object]:
    target_ids: set[str] = set()
    for target_id, target_rows in rows_by_target(rows).items():
        status = _target_health_status_for_report(target_rows).upper()
        if level == "critical" and status == "CRITICAL":
            target_ids.add(target_id)
        elif level == "warning" and status in {"WARNING", "NO_DATA"}:
            target_ids.add(target_id)
        elif level == "logging" and status == "OK":
            target_ids.add(target_id)
    return [row for row in rows if str(row["target_id"] or "") in target_ids]


def _target_health_status_for_report(rows: list[object]) -> str:
    statuses = {_report_status(row) for row in rows}
    if "CRITICAL" in statuses:
        return "CRITICAL"
    if "WARNING" in statuses or "ERROR" in statuses:
        return "WARNING"
    if "NO_DATA" in statuses:
        return "NO_DATA"
    return "OK"


def _recent_metrics_report_exists(store: DbOpsStore, *, source_id: str, dedupe_seconds: int) -> bool:
    return store.recent_alert_exists(source_id=source_id, within_seconds=dedupe_seconds)




def _chat_id_for_level(telegram_groups: dict[str, str], level: str) -> str:
    # Routing belongs to the shared layer; this app applies no rules of its own.
    from db_ops.lib.telegram_route import chat_id_for_level

    return chat_id_for_level(telegram_groups, level)


def _targets_from_rows(rows: list[object]) -> list[object]:
    targets: list[object] = []
    seen: set[str] = set()
    for row in rows:
        target_id = str(row["target_id"] or "")
        if not target_id or target_id in seen:
            continue
        seen.add(target_id)
        targets.append(
            SimpleNamespace(
                target_id=target_id,
                db_type=str(row["db_type"] or ""),
                ip=str(row["ip"] or ""),
                db_name=str(row["db_name"] or ""),
                instance_name="",
                connection_info={"server_name": str(row["server_id"] or "")},
            )
        )
    return targets


def _definitions_from_rows(rows: list[object]) -> list[object]:
    definitions: list[object] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        key = (str(row["db_type"] or ""), str(row["metric_code"] or ""))
        if not key[1] or key in seen:
            continue
        seen.add(key)
        definitions.append(
            SimpleNamespace(
                metric_code=key[1],
                interval_seconds=0,
                db_type=key[0],
                variants=[],
            )
        )
    return definitions


def _target_config_labels_from_rows(rows: list[object]) -> dict[str, str]:
    labels: dict[str, str] = {}
    for row in rows:
        target_id = str(row["target_id"] or "")
        server_id = str(row["server_id"] or "").strip()
        if target_id and server_id:
            labels[target_id] = server_id
    return labels


def _target_labels(rows_by_target: dict[str, list[object]], fallback_labels: dict[str, str] | None = None) -> dict[str, str]:
    fallback_labels = fallback_labels or {}
    labels: dict[str, str] = {}
    for target_id, rows in rows_by_target.items():
        first = rows[0]
        name = str(fallback_labels.get(target_id) or first["db_name"] or target_id).strip()
        ip = str(first["ip"] or "").strip()
        labels[target_id] = f"{name} {ip}" if ip and ip not in name else name
    return labels


def _target_health_status(rows: list[object]) -> str:
    statuses = {str(row["status"] or "").upper() for row in rows}
    if "CRITICAL" in statuses:
        return "CRITICAL"
    if "WARNING" in statuses or "ERROR" in statuses:
        return "WARNING"
    if "NO_DATA" in statuses:
        return "NO_DATA"
    return "OK"




def _run_time_text(run_meta: dict[str, Any] | None) -> str:
    if not run_meta:
        return ""
    return _local_time_text(str(run_meta.get("finished_at") or run_meta.get("started_at") or ""))


def _duration_text(run_meta: dict[str, Any] | None, *, report_window_seconds: int | None = None) -> str:
    if report_window_seconds is not None and report_window_seconds > 0:
        return f"{int(report_window_seconds)} sec"
    if not run_meta:
        return "unknown"
    started = _parse_utc(str(run_meta.get("started_at") or ""))
    finished = _parse_utc(str(run_meta.get("finished_at") or ""))
    if not started or not finished:
        return "unknown"
    return f"{max(0.0, (finished - started).total_seconds()):.1f} sec"


def _local_time_text(value: str) -> str:
    parsed = _parse_utc(value)
    if not parsed:
        return value.replace("T", " ").replace("Z", "")[:19] if value else "unknown"
    return parsed.astimezone(VIETNAM_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _parse_utc(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _tracking_targets_lines(targets: list[object], *, per_type_limit: int = 12) -> list[str]:
    lines: list[str] = []
    for db_type in _ordered_db_types(targets):
        type_targets = [target for target in targets if str(target.db_type).lower() == db_type]
        lines.append(f"{_db_type_label(db_type)}: {len(type_targets)}")
        for target in type_targets[:per_type_limit]:
            lines.append(f"- {_target_config_line(target)}")
        if len(type_targets) > per_type_limit:
            lines.append(f"... {len(type_targets) - per_type_limit} more")
        lines.append("")
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def _tracking_metrics_lines(
    definitions: list[object],
    *,
    targets: list[object],
    rows: list[object] | None = None,
    report_window_seconds: int | None = None,
) -> list[str]:
    lines: list[str] = []
    config_intervals = _metric_config_intervals()
    observed_intervals = _observed_metric_intervals(rows or [], report_window_seconds=report_window_seconds)
    active_db_types = set(_ordered_db_types(targets))
    for db_type in _ordered_db_types(targets):
        if db_type not in active_db_types:
            continue
        type_definitions = [definition for definition in definitions if definition_supports_db_type(definition, db_type)]
        lines.append(f"{_db_type_label(db_type)}:")
        for definition in type_definitions:
            metric_code = str(definition.metric_code).upper()
            interval_seconds = int(getattr(definition, "interval_seconds", 0) or 0)
            if interval_seconds <= 0:
                interval_seconds = config_intervals.get(metric_code, 0)
            if interval_seconds <= 0:
                interval_seconds = observed_intervals.get((db_type, metric_code), 0)
            if interval_seconds <= 0 and report_window_seconds is not None and report_window_seconds > 0:
                interval_seconds = int(report_window_seconds)
            lines.append(f"- {definition.metric_code} / {interval_seconds}s")
        lines.append("")
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def _metric_config_intervals() -> dict[str, int]:
    """metric_code -> configured time_window.repeat_interval from data/metric_definitions.json."""
    intervals: dict[str, int] = {}
    definitions_path = DEFAULT_METRIC_DEFINITIONS_PATH
    if not definitions_path.exists():
        return intervals
    try:
        data = json.loads(definitions_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return intervals
    for item in data.get("metrics") or []:
        if not isinstance(item, dict):
            continue
        metric_code = str(item.get("metric_code") or "").upper()
        if not metric_code:
            continue
        try:
            parsed = parse_time_window_config(item, context=f"metric_definitions.{metric_code}")
        except RuntimeError:
            continue
        interval = parsed.time_window.repeat_interval
        if interval is not None and interval > 0:
            intervals[metric_code] = int(interval)
    return intervals


def _observed_metric_intervals(
    rows: list[object],
    *,
    report_window_seconds: int | None,
) -> dict[tuple[str, str], int]:
    """Fallback when a metric has no configured interval: average seconds between
    collections observed in the report window, per (db_type, metric_code) — window
    seconds x tracked targets / collection count (a collection = one distinct
    (target, collected_at), so multi-item metrics do not overcount)."""
    if not rows or not report_window_seconds or report_window_seconds <= 0:
        return {}
    collections: dict[tuple[str, str], set[tuple[str, str]]] = {}
    metric_targets: dict[tuple[str, str], set[str]] = {}
    for row in rows:
        key = (str(row["db_type"] or "").lower(), str(row["metric_code"] or "").upper())
        if not key[1]:
            continue
        target_id = str(row["target_id"] or "")
        collections.setdefault(key, set()).add((target_id, str(row["collected_at"] or "")))
        metric_targets.setdefault(key, set()).add(target_id)
    intervals: dict[tuple[str, str], int] = {}
    for key, pulls in collections.items():
        if not pulls:
            continue
        target_count = max(1, len(metric_targets[key]))
        intervals[key] = max(1, int(round(report_window_seconds * target_count / len(pulls))))
    return intervals


def _problem_target_lines(rows: list[object], *, labels: dict[str, str], limit: int) -> list[str]:
    lines = []
    seen = set()
    ordered = sorted(
        rows,
        key=lambda row: (
            -status_rank(str(row["status"] or "")),
            str(row["target_id"] or ""),
            str(row["metric_code"] or ""),
            str(row["metric_item"] or ""),
        ),
    )
    for row in ordered:
        text = _problem_line(row, labels=labels)
        if text in seen:
            continue
        seen.add(text)
        lines.append(f"- {text}")
        if len(lines) >= limit:
            remaining = len(rows) - len(lines)
            if remaining > 0:
                lines.append(f"... {remaining} more")
            break
    return lines


def _problem_line(row: object, *, labels: dict[str, str]) -> str:
    target = labels.get(str(row["target_id"] or ""), _fallback_target(row))
    metric_code = str(row["metric_code"] or "").upper()
    status = str(row["status"] or "").upper()
    if status == "NO_DATA":
        status = "WARNING"
    detail = _problem_detail(row)
    return f"{target} / {metric_code} / {status} / {detail}"


def _problem_detail(row: object) -> str:
    item = str(row["metric_item"] or "").strip()
    value = str(row["metric_value"] or "").strip()
    unit = _compact_unit(str(row["metric_unit"] or ""))
    message = _short_message(str(row["message"] or ""))
    metric_code = str(row["metric_code"] or "").upper()
    if "BACKUP" in metric_code and value:
        return f"{item}: backup age {_value_with_unit(value, unit)}" if item else f"backup age {_value_with_unit(value, unit)}"
    if "DISK" in metric_code and value:
        return f"{item}: {_value_with_unit(value, unit)}" if item else _value_with_unit(value, unit)
    if value:
        return f"{item}: {_value_with_unit(value, unit)}" if item else _value_with_unit(value, unit)
    return f"{item}: {message}" if item and message else message or item or "see metric detail"


def _ordered_db_types(items: list[object]) -> list[str]:
    seen = {str(item.db_type).lower() for item in items}
    ordered = [db_type for db_type in ("sqlserver", "oracle", "mysql", "postgresql", "postgres") if db_type in seen]
    ordered.extend(sorted(seen - set(ordered)))
    return ordered


def _db_type_label(db_type: str) -> str:
    if not str(db_type).strip():
        return "OS_ONLY"
    return {"sqlserver": "SQLServer", "oracle": "Oracle", "mysql": "MySQL", "postgresql": "PostgreSQL", "postgres": "PostgreSQL"}.get(db_type, db_type)


def _target_config_line(target: object) -> str:
    name = str(target.connection_info.get("server_name") or target.instance_name or target.db_name or target.target_id).strip()
    ip = str(target.ip or "").strip()
    return f"{name} {ip}".strip()


def _status_counts(rows: list[object]) -> dict[str, int]:
    counts = {"OK": 0, "WARNING": 0, "ERROR": 0, "CRITICAL": 0, "NO_DATA": 0}
    for row in rows:
        status = str(row["status"] or "").upper()
        if status == "LOGGING":
            status = "OK"
        if status in counts:
            counts[status] += 1
    return counts


def _health_counts(rows: list[object]) -> dict[str, int]:
    counts = {"OK": 0, "WARNING": 0, "ERROR": 0, "CRITICAL": 0, "NO_DATA": 0}
    for target_rows in rows_by_target(rows).values():
        status = _target_health_status(target_rows).upper()
        if status in counts:
            counts[status] += 1
    return counts


def _latest_run_time(rows: list[object]) -> str:
    values = [str(row["collected_at"] or "") for row in rows if str(row["collected_at"] or "")]
    if not values:
        return "unknown"
    return max(values).replace("T", " ").replace("Z", "")[:16]


def _alert_line(row: object, *, labels: dict[str, str]) -> str:
    target = labels.get(str(row["target_id"] or ""), _fallback_target(row))
    metric_code = str(row["metric_code"] or "").upper()
    item = str(row["metric_item"] or "").strip()
    value = str(row["metric_value"] or "").strip()
    unit = _compact_unit(str(row["metric_unit"] or ""))
    message = _short_message(str(row["message"] or ""))
    value_text = _value_with_unit(value, unit) if value else ""
    detail_parts = [part for part in (value_text, message) if part]
    detail = "; ".join(detail_parts) if detail_parts else metric_code

    return f"{target} / {metric_code} / {item}: {detail}" if item else f"{target} / {metric_code}: {detail}"


def _report_status(row: object) -> str:
    return row_status(row)


def _format_health_lines(
    rows: list[object],
    *,
    labels: dict[str, str],
    statuses: set[str],
    limit: int,
) -> list[str]:
    lines = []
    for target_id, target_rows in sorted(rows_by_target(rows).items(), key=lambda item: labels.get(item[0], item[0]).lower()):
        status = _target_health_status(target_rows).upper()
        if status not in statuses:
            continue
        label = labels.get(target_id, target_id)
        score = _health_score(target_rows) if status in {"ERROR", "CRITICAL", "WARNING", "NO_DATA"} else _target_score(target_rows, status)
        lines.append(f"- {label} score={score}")
        if len(lines) >= limit:
            break
    return lines


def _critical_total_line_count(rows: list[object], health_rows: list[object]) -> int:
    return len({_alert_line(row, labels={}) for row in rows}) + len(rows_by_target(health_rows))


def _health_score(rows: list[object]) -> int:
    return sum(
        int(row["importance"] or 0)
        for row in rows
        if str(row["status"] or "").upper() in {"ERROR", "CRITICAL", "WARNING", "NO_DATA"}
    )


def _fallback_target(row: object) -> str:
    name = str(row["db_name"] or row["server_id"] or row["target_id"] or "unknown").strip()
    ip = str(row["ip"] or "").strip()
    return f"{name} {ip}" if ip and ip not in name else name


def _compact_unit(unit: str) -> str:
    normalized = unit.lower()
    if normalized in ("hours", "hour"):
        return "h"
    if normalized in ("gb", "gbyte", "gbytes"):
        return "GB"
    if normalized in ("pct", "percent"):
        return "%"
    return unit


def _value_with_unit(value: str, unit: str) -> str:
    if not unit:
        return value
    if unit in {"h", "GB", "%"}:
        return f"{value}{unit}"
    return f"{value} {unit}"


def _short_message(message: str) -> str:
    return " ".join(message.replace("\r", " ").replace("\n", " ").split())
