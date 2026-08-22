from __future__ import annotations
from db_ops.lib.metric_score import target_score as _target_score  # noqa: F401 - one definition

import argparse
import sys

from db_ops.lib.policy_engine import status_rank
from db_ops.lib.secret_text import add_key_argument, set_key_env
from db_ops.db.metric_definitions import definition_supports_db_type
from db_ops.db.metric_results import rows_by_target
from db_ops.config import DEFAULT_CONFIG_PATH, load_config, resolve_config_path
from db_ops.logging_ops import log_event, log_function_call, log_function_error, setup_app_logger
from db_ops.logging_ops.runtime_stdout import patch_stdout
from db_ops.metrics.collector import collect_metrics
from db_ops.metrics.definitions import DEFAULT_DEFINITIONS_PATH, load_metric_definitions
from db_ops.metrics.storage import MetricStore
from db_ops.metrics.targets import DEFAULT_DATA_DIR, load_metric_targets, resolve_metric_target


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DB Ops metrics collection and reporting.")
    parser.add_argument("--config", default=None, help="Path to config JSON. Defaults to config.metrics.json or config.json.")
    add_key_argument(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    collect = subparsers.add_parser("collect", help="Collect database metrics.")
    collect.add_argument("--db-type", choices=["sqlserver", "oracle", "mysql", "postgresql", "postgres"])
    collect.add_argument("--target-id")
    collect.add_argument("--target-ip")
    collect.add_argument("--metric-code")
    collect.add_argument("--dry-run", action="store_true")
    collect.add_argument("--force", action="store_true", help="Collect even when the metric interval has not elapsed.")
    collect.add_argument("--include-windowed", action="store_true", help="Also collect the metrics whose time_window confines them to certain hours (DBCC CHECKDB, index fragmentation, restore validation ...). Off by default: --force means 'never mind the interval', not 'never mind the window', and those metrics are windowed precisely to keep them off a production instance in the daytime.")
    collect.add_argument("--archive-days", type=int, default=30,
                         help="Before collecting, archive metric rows older than N days into metric_results_archive (default 30).")

    latest = subparsers.add_parser("latest", help="Show latest metric results.")
    latest.add_argument("--limit", type=int, default=50)
    latest.add_argument("--status")
    latest.add_argument("--importance-min", type=int)
    latest.add_argument("--target-id")
    latest.add_argument("--metric-code")

    report = subparsers.add_parser("report", help="Show latest metrics as a Markdown pivot table.")
    report.add_argument("--db-type", choices=["sqlserver", "oracle", "mysql", "postgresql", "postgres"])
    report.add_argument("--target-id")
    report.add_argument("--metric-code")

    summary = subparsers.add_parser("summary", aliases=["daily-summary"], help="Show a human daily metrics summary.")
    summary.add_argument("--run-id", type=int)
    summary.add_argument("--db-type", choices=["sqlserver", "oracle", "mysql", "postgresql", "postgres"])
    summary.add_argument("--target-id")
    summary.add_argument("--limit", type=int, default=20)

    health = subparsers.add_parser("health-summary", help="Show one health line per target.")
    health.add_argument("--run-id", type=int)
    health.add_argument("--db-type", choices=["sqlserver", "oracle", "mysql", "postgresql", "postgres"])
    health.add_argument("--target-id")

    summary_latest = subparsers.add_parser(
        "summary-latest",
        aliases=["latest-summary"],
        help="Show a human summary from latest result per target and metric.",
    )
    summary_latest.add_argument("--db-type", choices=["sqlserver", "oracle", "mysql", "postgresql", "postgres"])
    summary_latest.add_argument("--target-id")
    summary_latest.add_argument("--limit", type=int, default=20)

    health_latest = subparsers.add_parser(
        "health-summary-latest",
        aliases=["latest-health-summary"],
        help="Show one health line per target from latest result per target and metric.",
    )
    health_latest.add_argument("--db-type", choices=["sqlserver", "oracle", "mysql", "postgresql", "postgres"])
    health_latest.add_argument("--target-id")

    alert = subparsers.add_parser("alert-summary", help="Generate Telegram-ready alert summary.")
    alert.add_argument("--run-id", type=int)
    alert.add_argument("--importance-min", type=int, default=4)
    alert.add_argument("--include-warning", action="store_true")
    alert.add_argument("--include-ok", default="false")

    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    set_key_env(args.key, args.key_base64)
    logger = None
    try:
        config = load_config(resolve_config_path("metrics", args.config))
        patch_stdout(config.log_dir / "metrics_runtime.log", app_name="metrics")
        logger = setup_app_logger(config, app_name="metrics", enable_telegram_alerts=False, enable_console=False)
        log_function_call(logger, function_name=f"metrics.{args.command}")
        if args.command == "collect":
            target_id = _resolve_target_id_arg(target_ip=args.target_ip, target_id=args.target_id, db_type=args.db_type)
            summary = collect_metrics(
                config=config,
                db_type=args.db_type,
                target_id=target_id,
                metric_code=args.metric_code,
                dry_run=bool(args.dry_run),
                force=bool(args.force),
                include_windowed=bool(args.include_windowed),
                archive_days=int(args.archive_days),
            )
            log_event(
                logger,
                level="logging",
                message=(
                    "Metric collect finished. "
                    f"run_id={summary.run_id} targets={summary.target_count} "
                    f"metrics={summary.metric_count} executed={summary.executed_count} "
                    f"skipped_interval={summary.skipped_interval_count} disabled={summary.disabled_count} "
                    f"results={summary.result_count} ok={summary.ok_count} errors={summary.error_count} "
                    f"warnings={summary.warning_count} critical={summary.critical_count} "
                    f"no_data={summary.no_data_count} duration_seconds={summary.duration_seconds}"
                ),
            )
            _print_collect_summary(summary)
            return 0
        store = MetricStore.from_config(config)
        if args.command == "latest":
            rows = store.fetch_latest_results(
                limit=args.limit,
                status=args.status,
                importance_min=args.importance_min,
                target_id=args.target_id,
                metric_code=args.metric_code,
            )
            print(_format_latest_table(rows))
            return 0
        if args.command == "report":
            definitions = load_metric_definitions(DEFAULT_DEFINITIONS_PATH, active_only=True)
            if args.db_type:
                definitions = [definition for definition in definitions if definition_supports_db_type(definition, args.db_type)]
            targets = load_metric_targets(data_dir=DEFAULT_DATA_DIR, db_type=args.db_type, target_id=args.target_id)
            rows = store.fetch_latest_report_results(
                db_type=args.db_type,
                target_id=args.target_id,
                metric_code=args.metric_code,
                metric_codes={definition.metric_code for definition in definitions},
            )
            print(_format_report_table(rows, definitions=definitions, targets=targets, metric_code=args.metric_code))
            return 0
        if args.command in ("summary", "daily-summary"):
            run_id = args.run_id or store.latest_run_id()
            if run_id is None:
                print("No metric_runs data found.")
                return 0
            target_labels = _target_config_labels(db_type=args.db_type, target_id=args.target_id)
            rows = store.fetch_run_results(run_id=run_id, db_type=args.db_type, target_id=args.target_id)
            print(_format_daily_summary(rows, run_id=run_id, limit=args.limit, target_labels=target_labels))
            return 0
        if args.command == "health-summary":
            run_id = args.run_id or store.latest_run_id()
            if run_id is None:
                print("No metric_runs data found.")
                return 0
            target_labels = _target_config_labels(db_type=args.db_type, target_id=args.target_id)
            rows = store.fetch_run_results(run_id=run_id, db_type=args.db_type, target_id=args.target_id)
            print(_format_health_summary(rows, run_id=run_id, target_labels=target_labels))
            return 0
        if args.command in ("summary-latest", "latest-summary"):
            targets = load_metric_targets(data_dir=DEFAULT_DATA_DIR, db_type=args.db_type, target_id=args.target_id)
            definitions = load_metric_definitions(DEFAULT_DEFINITIONS_PATH, active_only=True)
            if args.db_type:
                definitions = [definition for definition in definitions if definition_supports_db_type(definition, args.db_type)]
            active_target_ids = {target.target_id for target in targets}
            target_labels = _target_config_labels(targets=targets)
            rows = store.fetch_latest_report_results(
                db_type=args.db_type,
                target_id=args.target_id,
                metric_codes={definition.metric_code for definition in definitions},
            )
            rows = [row for row in rows if str(row["target_id"] or "") in active_target_ids]
            print(_format_daily_summary(rows, run_id=None, limit=args.limit, latest=True, target_labels=target_labels))
            return 0
        if args.command in ("health-summary-latest", "latest-health-summary"):
            targets = load_metric_targets(data_dir=DEFAULT_DATA_DIR, db_type=args.db_type, target_id=args.target_id)
            definitions = load_metric_definitions(DEFAULT_DEFINITIONS_PATH, active_only=True)
            if args.db_type:
                definitions = [definition for definition in definitions if definition_supports_db_type(definition, args.db_type)]
            active_target_ids = {target.target_id for target in targets}
            target_labels = _target_config_labels(targets=targets)
            rows = store.fetch_latest_report_results(
                db_type=args.db_type,
                target_id=args.target_id,
                metric_codes={definition.metric_code for definition in definitions},
            )
            rows = [row for row in rows if str(row["target_id"] or "") in active_target_ids]
            print(_format_health_summary(rows, run_id=None, latest=True, target_labels=target_labels))
            return 0
        if args.command == "alert-summary":
            rows = store.fetch_alert_results(
                run_id=args.run_id,
                importance_min=args.importance_min,
                include_warning=bool(args.include_warning),
                include_ok=str(args.include_ok).lower() == "true",
            )
            print(_format_alert_summary(rows))
            return 0
    except Exception as exc:  # noqa: BLE001 - CLI system failure path.
        if logger:
            log_function_error(logger, function_name=f"metrics.{getattr(args, 'command', 'cli')}", error_text=str(exc))
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 1


def _resolve_target_id_arg(*, target_ip: str | None, target_id: str | None, db_type: str | None = None) -> str | None:
    if not target_ip:
        return target_id
    return resolve_metric_target(target_ip=target_ip, target_id=target_id, db_type=db_type).target_id


def _print_collect_summary(summary: object) -> None:
    for key in (
        "run_id",
        "target_count",
        "metric_count",
        "executed_count",
        "skipped_interval_count",
        "disabled_count",
        "result_count",
        "ok_count",
        "error_count",
        "warning_count",
        "critical_count",
        "no_data_count",
        "started_at",
        "finished_at",
        "duration_seconds",
    ):
        print(f"{key}: {getattr(summary, key)}")


def _format_latest_table(rows: list[object]) -> str:
    if not rows:
        return "No metric_results data found."
    lines: list[str] = []
    for row in rows:
        value = _value_message(row)
        lines.extend(
            [
                f"collected_at: {row['collected_at']}",
                f"target: {row['target_id']}",
                f"metric: {row['metric_code']}",
                f"status: {row['status']} importance={row['importance']}",
                f"daily_report_created: {row['daily_report_created']}",
                f"value: {value or '<empty>'}",
                f"message: {row['message'] or ''}",
                "",
            ]
        )
    return "\n".join(lines).strip()


def _format_alert_summary(rows: list[object]) -> str:
    if not rows:
        return "[DB METRICS ALERT]\nNo matching metric alerts."
    lines = ["[DB METRICS ALERT]"]
    for row in rows:
        lines.extend(
            [
                f"Target: {row['db_name']} / {row['ip']}",
                f"Metric: {row['metric_code']}",
                f"Status: {row['status']}",
                f"Importance: {row['importance']}",
                f"Message: {row['message'] or _value_message(row)}",
                "",
            ]
        )
    return "\n".join(lines).strip()


def _target_config_labels(
    *,
    targets: list[object] | None = None,
    db_type: str | None = None,
    target_id: str | None = None,
) -> dict[str, str]:
    if targets is None:
        targets = load_metric_targets(data_dir=DEFAULT_DATA_DIR, db_type=db_type, target_id=target_id)
    labels: dict[str, str] = {}
    for target in targets:
        server_name = str(target.connection_info.get("server_name") or "").strip()
        if server_name:
            labels[str(target.target_id)] = server_name
    return labels


def _format_daily_summary(
    rows: list[object],
    *,
    run_id: int | None,
    limit: int,
    latest: bool = False,
    target_labels: dict[str, str] | None = None,
) -> str:
    title = "[SUMMARY latest per target+metric]" if latest else f"[SUMMARY run_id={run_id}]"
    if not rows:
        return f"{title}\nNo metric results."
    critical_rows = [row for row in rows if str(row["status"] or "").upper() in ("ERROR", "CRITICAL")]
    warning_rows = [row for row in rows if str(row["status"] or "").upper() == "WARNING"]
    ok_count = sum(1 for row in rows if str(row["status"] or "").upper() in {"OK", "LOGGING"})
    labels = _target_labels(rows_by_target(rows), fallback_labels=target_labels)

    lines = [title, "", "[CRITICAL]"]
    lines.extend(_summary_bullets(critical_rows, labels=labels, limit=limit) or ["- none"])
    lines.extend(["", "[WARNING]"])
    lines.extend(_summary_bullets(warning_rows, labels=labels, limit=limit) or ["- none"])
    lines.extend(["", "[OK]", f"- {ok_count} metrics OK"])
    return "\n".join(lines)


def _summary_bullets(rows: list[object], *, labels: dict[str, str], limit: int) -> list[str]:
    bullets: list[str] = []
    seen: set[str] = set()
    ordered = sorted(
        rows,
        key=lambda row: (
            status_rank(str(row["status"] or "")),
            int(row["importance"] or 0),
            str(row["target_id"] or ""),
        ),
        reverse=True,
    )
    for row in ordered:
        text = _summary_line(row, labels=labels)
        if text in seen:
            continue
        seen.add(text)
        bullets.append(f"- {text}")
        if len(bullets) >= limit:
            remaining = len(rows) - len(bullets)
            if remaining > 0:
                bullets.append(f"- ... {remaining} more")
            break
    return bullets


def _summary_line(row: object, *, labels: dict[str, str]) -> str:
    target = labels.get(str(row["target_id"] or ""), _short_target_name(row))
    metric = _friendly_metric_name(str(row["metric_code"] or ""))
    item = str(row["metric_item"] or "").strip()
    value = str(row["metric_value"] or "").strip()
    unit = str(row["metric_unit"] or "").strip()
    message = str(row["message"] or "").strip()
    status = str(row["status"] or "").upper()

    if status == "ERROR":
        return f"{target} / {_short_error_message(message)}"
    if value:
        compact_value = f"{value}{_compact_unit(unit)}"
        if item and item.lower() not in target.lower() and item.lower() not in ("server", "sql_agent", "availability_group"):
            return f"{target} / {item} / {metric} {compact_value}"
        return f"{target} / {metric} {compact_value}"
    if message:
        return f"{target} / {metric} / {message}"
    return f"{target} / {metric}"


def _format_health_summary(
    rows: list[object],
    *,
    run_id: int | None,
    latest: bool = False,
    target_labels: dict[str, str] | None = None,
) -> str:
    title = "[HEALTH SUMMARY latest per target+metric]" if latest else f"[HEALTH SUMMARY run_id={run_id}]"
    if not rows:
        return f"{title}\nNo metric results."
    # Named `by_target`, not `rows_by_target`: the latter is the imported function, and assigning
    # to it made the whole function's `rows_by_target` a local, so the call on this very line
    # raised `UnboundLocalError` before it could run. Every `health-summary-latest` invocation and
    # its three aliases crashed; the sibling on line 307 calls the function inline and was fine,
    # which is why nothing else showed it.
    by_target = rows_by_target(rows)
    labels = _target_labels(by_target, fallback_labels=target_labels)

    lines = [title]
    for target_id in sorted(by_target, key=lambda value: labels[value].lower()):
        target_rows = by_target[target_id]
        name = labels[target_id]
        status = _target_health_status(target_rows)
        if status == "OK":
            detail = ""
        elif status == "ERROR":
            detail = _target_error_detail(target_rows)
        else:
            detail = f"score={_target_score(target_rows, status)}"
        lines.append(f"{name:<22} {status:<8} {detail}".rstrip())
    return "\n".join(lines)


def _target_labels(rows_by_target: dict[str, list[object]], fallback_labels: dict[str, str] | None = None) -> dict[str, str]:
    fallback_labels = fallback_labels or {}
    base_names = {target_id: _base_target_name(rows, fallback_labels.get(target_id, "")) for target_id, rows in rows_by_target.items()}
    labels: dict[str, str] = {}
    for target_id, rows in rows_by_target.items():
        name = base_names[target_id]
        ip = str(rows[0]["ip"] or "").strip()
        labels[target_id] = f"{name} {ip}" if ip and ip not in name else name
    return labels


def _base_target_name(rows: list[object], fallback_label: str = "") -> str:
    if not rows:
        return "unknown"
    db_type = str(rows[0]["db_type"] or "").strip().lower()
    if db_type == "sqlserver":
        server_name = _sqlserver_name_from_instance_status(rows)
        if server_name:
            return server_name
        if fallback_label:
            return fallback_label
    return _short_target_name(rows[0])


def _sqlserver_name_from_instance_status(rows: list[object]) -> str:
    for row in rows:
        if str(row["metric_code"] or "") != "INSTANCE_STATUS":
            continue
        if str(row["status"] or "").upper() == "ERROR":
            continue
        metric_item = str(row["metric_item"] or "").strip()
        if metric_item:
            return metric_item
        message = str(row["message"] or "").strip()
        marker = "server="
        if marker in message:
            return message.split(marker, 1)[1].split(",", 1)[0].strip()
    return ""


def _target_health_status(rows: list[object]) -> str:
    statuses = {str(row["status"] or "").upper() for row in rows}
    if "ERROR" in statuses:
        return "ERROR"
    if "CRITICAL" in statuses:
        return "CRITICAL"
    if "WARNING" in statuses:
        return "WARNING"
    if "NO_DATA" in statuses:
        return "NO_DATA"
    return "OK"




def _target_error_detail(rows: list[object]) -> str:
    error_rows = [row for row in rows if str(row["status"] or "").upper() == "ERROR"]
    if not error_rows:
        return "error"
    messages = [str(row["message"] or "") for row in error_rows]
    joined = " ".join(messages).lower()
    if "password ref not found" in joined:
        return "missing password ref"
    if "timed out" in joined or "timeout" in joined:
        return "timeout"
    if "connection failed" in joined or "cannot connect" in joined or "closed the connection" in joined:
        return "connection failed"
    return _short_error_message(messages[0])


def _short_target_name(row: object) -> str:
    db_name = str(row["db_name"] or "").strip()
    db_type = str(row["db_type"] or "").strip().lower()
    target_id = str(row["target_id"] or "").strip()
    if db_type == "oracle" and db_name:
        return f"{db_name}-ORACLE"
    if db_name and db_name != "<not-provided>":
        return db_name
    ip = str(row["ip"] or "").strip()
    if ip:
        return ip
    return target_id.rsplit("/", 1)[-1] if target_id else "unknown"


def _target_sort_name(row: object) -> str:
    return _short_target_name(row).lower()


def _friendly_metric_name(metric_code: str) -> str:
    names = {
        "BACKUP_AGE": "Backup age",
        "QUERY_LONG_RUNNING": "Long running query",
        "STORAGE_TEMP_SPACE": "Temp",
        "BACKUP_JOB_STATUS": "Backup job",
        "STORAGE_DISK_FREE_SPACE": "Disk free",
        "PERFORMANCE_WAIT_STATS": "Wait stats",
        "LOCK_DEADLOCK_RECENT": "Deadlocks",
        "INSTANCE_STATUS": "Connection",
        "STORAGE_TABLESPACE_USAGE": "Tablespace",
    }
    return names.get(metric_code, metric_code.replace("_", " ").title())


def _compact_unit(unit: str) -> str:
    normalized = unit.lower()
    if normalized in ("hours", "hour"):
        return "h"
    if normalized in ("seconds", "second"):
        return "s"
    if normalized == "pct":
        return "%"
    if not unit:
        return ""
    return unit


def _short_error_message(message: str) -> str:
    text = " ".join(message.replace("\r", " ").replace("\n", " ").split())
    lowered = text.lower()
    if "password ref not found" in lowered:
        return text
    if "timed out" in lowered or "timeout" in lowered:
        return "connection timeout"
    if "connection failed" in lowered or "cannot connect" in lowered or "closed the connection" in lowered:
        return "connection failed"
    return text[:120] if text else "error"


def _format_report_table(
    rows: list[object],
    *,
    definitions: list[object],
    targets: list[object],
    metric_code: str | None,
) -> str:
    if not rows:
        return "No metric_results data found."

    rows_by_metric_target: dict[tuple[str, str], list[object]] = {}
    target_ids_with_data: set[str] = set()
    row_db_types_by_metric: dict[str, str] = {}
    metric_codes_by_db_type: dict[str, set[str]] = {}
    for row in rows:
        metric = str(row["metric_code"])
        row_db_type = str(row["db_type"])
        key = (metric, str(row["target_id"]))
        rows_by_metric_target.setdefault(key, []).append(row)
        target_ids_with_data.add(str(row["target_id"]))
        row_db_types_by_metric.setdefault(metric, row_db_type)
        metric_codes_by_db_type.setdefault(row_db_type, set()).add(metric)

    definitions_by_code = {definition.metric_code: definition for definition in definitions}
    target_db_types = {target.target_id: target.db_type for target in targets}
    metric_codes = {str(row["metric_code"]) for row in rows}
    metric_order = [definition.metric_code for definition in definitions if definition.metric_code in metric_codes]
    for code in sorted(metric_codes):
        if code not in metric_order:
            metric_order.append(code)
    if metric_code:
        metric_order = [code for code in metric_order if code == metric_code]

    db_type_order: list[str] = []
    for row in rows:
        db_type = str(row["db_type"] or "")
        if db_type and db_type not in db_type_order:
            db_type_order.append(db_type)

    sections: list[str] = []
    for db_type in db_type_order:
        group_metric_order = [
            code
            for code in metric_order
            if code in metric_codes_by_db_type.get(db_type, set())
        ]
        target_order = [
            target.target_id
            for target in targets
            if target.db_type == db_type and target.target_id in target_ids_with_data
        ]
        for target_id in sorted(target_ids_with_data):
            if target_id not in target_order and target_db_types.get(target_id, db_type) == db_type:
                target_order.append(target_id)

        headers = ["ORD", "metric_name", "file", *[_target_column_name(target_id) for target_id in target_order]]
        lines = [f"### {db_type}", _markdown_row(headers), _markdown_row(["---"] * len(headers))]
        for index, code in enumerate(group_metric_order, start=1):
            definition = definitions_by_code.get(code)
            values = [
                str(index),
                code,
                _definition_file_for_db_type(definition, db_type) if definition else "",
            ]
            values.extend(
                _format_report_cell(code, rows_by_metric_target.get((code, target_id), []))
                for target_id in target_order
            )
            lines.append(_markdown_row(values))
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def _definition_file_for_db_type(definition: object, db_type: str) -> str:
    variants = [
        variant
        for variant in (getattr(definition, "variants", None) or getattr(definition, "sql_variants", []) or [])
        if getattr(variant, "db_type", "") == db_type and bool(getattr(variant, "supported", True))
    ]
    if variants:
        return str(getattr(variants[-1], "file", "") or getattr(variants[-1], "sql_file", "") or "")
    return str(getattr(definition, "file", "") or getattr(definition, "sql_file", "") or "")


def _format_report_cell(metric_code: str, rows: list[object]) -> str:
    if not rows:
        return ""
    parts = []
    for row in rows:
        value = _report_value(metric_code, row)
        collected_at = _format_report_time(str(row["collected_at"]))
        status = str(row["status"] or "")
        text = status
        if value:
            text = value if status == "OK" or value.upper() == status.upper() else f"{status}: {value}"
        text = f"{text} @{collected_at}"
        parts.append(text)
    return "<br>".join(parts)


def _report_value(metric_code: str, row: object) -> str:
    value = str(row["metric_value"] or "").strip()
    unit = str(row["metric_unit"] or "").strip()
    item = str(row["metric_item"] or "").strip()
    status = str(row["status"] or "").strip()
    message = str(row["message"] or "").strip()

    if metric_code == "INSTANCE_STATUS":
        return value or status
    if metric_code.endswith("_CONNECTIONS"):
        return f"{value} {unit}".strip() if value else status
    if metric_code == "BACKUP_AGE":
        age = f"{value} {unit}".strip() if value else "no backup"
        return f"{item}: {age}" if item else age
    if not value:
        return item or message[:80] or status
    return f"{item}: {value} {unit}".strip() if item else f"{value} {unit}".strip()


def _format_report_time(value: str) -> str:
    return value.replace("T", " ").replace("Z", "")


def _target_column_name(target_id: str) -> str:
    parts = target_id.split("/")
    if len(parts) >= 3:
        return parts[0].replace("ACME-", "") + "/" + parts[-1]
    return target_id


def _markdown_row(values: list[str]) -> str:
    return "| " + " | ".join(_escape_markdown_cell(value) for value in values) + " |"


def _escape_markdown_cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", "<br>")


def _value_message(row: object) -> str:
    value = row["metric_value"] or ""
    unit = row["metric_unit"] or ""
    return f"{row['metric_item'] or ''} {value}{unit}".strip()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
