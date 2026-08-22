from __future__ import annotations

import argparse
import inspect
import json
import sys
from collections.abc import Callable
from typing import Any

from db_ops.lib.telegram_route import telegram_groups
from db_ops.config import DEFAULT_CONFIG_PATH, DbOpsConfig, load_config, resolve_config_path
from db_ops.logging_ops import log_function_call, log_function_error, setup_app_logger
from db_ops.logging_ops.runtime_stdout import patch_stdout
from db_ops.reports.metrics_reports import (
    create_backup_health_report,
    create_metrics_reports,
    push_report_alerts,
    queue_metrics_reports,
    run_scheduled_reports,
)
from db_ops.reports.inventory_health import build_inventory_health
from db_ops.reports.backfill import backfill_dated_reports
from db_ops.reports.index_report import create_index_reports
from db_ops.reports.inventory_summary import build_inventory_workflow
from db_ops.reports.service import ReportWorkflowError, force_hourly_report, metric_history_report


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return parsed


def non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return parsed


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build DB Ops reports from collected runtime data.")
    # Without these, a manual run outside the daemon cannot open the encrypted store: the daemon
    # forwards DB_OPS_SECRET_KEY to the children it starts, but `docker exec` does not inherit it,
    # so forcing a report by hand was impossible. Same two flags every other db_ops CLI declares.
    parser.add_argument("--key", default=None,
                        help="Secret store passphrase (plaintext). Needed for a manual run.")
    parser.add_argument("--key-base64", dest="key_base64", default=None,
                        help="Secret store passphrase, base64-encoded.")
    parser.add_argument("--config", default=None, help="Path to config JSON. Defaults to config.reports.json or config.json.")

    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser(
        "create-metrics-reports",
        help="Build latest metrics reports and save them into the reports table.",
    )
    create_parser.add_argument("--summary-limit", type=int, default=40, help="Maximum alert detail lines per metrics report.")
    create_parser.add_argument("--target-id", help="Only build metrics reports for one target_id.")
    create_parser.add_argument("--target-ip", help="Resolve target IP and only build metrics reports for that target.")
    create_parser.set_defaults(report_function=create_metrics_reports)

    backup_parser = subparsers.add_parser(
        "create-backup-health-report",
        help="Build BACKUP_HEALTH from SQLite metric history and save it into the reports table.",
    )
    backup_parser.add_argument("--days", type=int, default=7, help="History window in days. Default: 7.")
    backup_parser.add_argument("--run-hour", type=int, default=None, help="Only create during this local hour, for example 8.")
    backup_parser.add_argument("--force", action="store_true", help="Create even if due-time or once-per-day checks would skip.")
    backup_parser.set_defaults(report_function=create_backup_health_report)

    push_parser = subparsers.add_parser(
        "push-report-alerts",
        help="Push created reports into telegram_send_messages.",
    )
    push_parser.add_argument("--limit", type=int, default=50, help="Maximum reports to push.")
    push_parser.add_argument("--report-type", help="Only push one report_type, for example instancely_warning.")
    push_parser.add_argument("--report-level", choices=["logging", "warning", "critical"], help="Only push one report level.")
    push_parser.add_argument("--target-id", help="Only push created reports for one target_id.")
    push_parser.add_argument("--target-ip", help="Resolve target IP and only push created reports for that target.")
    push_parser.add_argument(
        "--dedupe-seconds",
        type=int,
        default=300,
        help="Do not push the same report alert source again within this many seconds.",
    )
    push_parser.set_defaults(report_function=push_report_alerts)

    metrics_parser = subparsers.add_parser(
        "queue-metrics-reports",
        help="Compatibility workflow: create latest metrics reports, then push them into telegram_send_messages.",
    )
    metrics_parser.add_argument("--summary-limit", type=int, default=40, help="Maximum alert detail lines per metrics report.")
    metrics_parser.add_argument("--target-id", help="Only build metrics reports for one target_id.")
    metrics_parser.add_argument("--target-ip", help="Resolve target IP and only queue metrics reports for that target.")
    metrics_parser.add_argument(
        "--dedupe-seconds",
        type=int,
        default=300,
        help="Do not queue the same metrics report level again within this many seconds.",
    )
    metrics_parser.set_defaults(report_function=queue_metrics_reports)

    scheduled_parser = subparsers.add_parser(
        "run-scheduled",
        help="Evaluate reports_config.json and queue due reports independently by report_code.",
    )
    scheduled_parser.add_argument(
        "--reports-config",
        dest="reports_config_path",
        default=None,
        help="Path to reports_config.json. Defaults to data/reports_config.json.",
    )
    scheduled_parser.add_argument("--summary-limit", type=int, default=40, help="Maximum alert detail lines per metrics report.")
    scheduled_parser.add_argument("--backup-days", type=int, default=7, help="Backup health history window in days. Default: 7.")
    scheduled_parser.add_argument("--scheduler-trigger-time", help="UTC scheduler trigger timestamp for timing logs.")
    scheduled_parser.set_defaults(report_function=run_scheduled_reports)

    force_parser = subparsers.add_parser(
        "force-hourly-report",
        help="Manual workflow: collect metrics for one target IP, create a metrics report, then queue report alerts.",
    )
    force_parser.add_argument("--server-id", dest="server_id", default=None,
                              help="Unique server_id to resolve the target (preferred; no db_type/port needed).")
    force_parser.add_argument("--target-ip", default=None,
                              help="Target IP to resolve to the internal target_id (use --server-id instead when possible).")
    force_parser.add_argument("--db-type", dest="db_type", default=None,
                              help="Disambiguate when several targets share the IP (e.g. sqlserver, postgresql).")
    force_parser.add_argument("--port", type=int, default=None,
                              help="Disambiguate by port when several targets share the IP (e.g. 5433).")
    force_parser.add_argument("--summary-limit", type=int, default=40, help="Maximum alert detail lines per metrics report.")
    force_parser.add_argument(
        "--dedupe-seconds",
        type=int,
        default=300,
        help="Do not queue the same metrics report level again within this many seconds.",
    )
    force_parser.add_argument("--include-windowed", action="store_true", help="Also collect the metrics whose time_window confines them to certain hours (DBCC CHECKDB, index fragmentation, restore validation ...). Off by default: --force means 'never mind the interval', not 'never mind the window', and those metrics are windowed precisely to keep them off a production instance in the daytime.")
    force_parser.set_defaults(report_function=force_hourly_report)

    history_parser = subparsers.add_parser(
        "metric-history-report",
        help="Build and queue a stored history report for one server_id and metric over the last N hours.",
    )
    history_parser.add_argument("--server-id", required=True, help="Exact metric_results.server_id to report.")
    history_parser.add_argument("--metric-code", required=True, help="Exact metric code (normalized to uppercase).")
    history_parser.add_argument(
        "--hours",
        required=True,
        type=positive_int,
        help="Relative window size: report rows from current UTC time minus N hours through now.",
    )
    history_parser.add_argument(
        "--summary-limit",
        type=positive_int,
        default=150,
        help="Maximum history rows included in the report. Default: 150.",
    )
    history_parser.add_argument(
        "--dedupe-seconds",
        type=non_negative_int,
        default=0,
        help="Suppress an identical manual report queued within this many seconds. Default: 0.",
    )
    history_parser.set_defaults(report_function=metric_history_report)

    inventory_parser = subparsers.add_parser(
        "build-inventory-health",
        help="Build a dated <YYYYMMDD_HHMMSS>_database-inventory.json health overlay from collected metrics.",
    )
    inventory_parser.add_argument("--days", type=int, default=2, help="Only use metrics from the last N days. Default: 2.")
    inventory_parser.add_argument("--output-dir", help="Directory for the dated file. Defaults to the runtime dir.")
    inventory_parser.add_argument("--date", help="Override the YYYYMMDD_HHMMSS stamp in the file name (for testing).")
    inventory_parser.set_defaults(report_function=build_inventory_health)

    workflow_parser = subparsers.add_parser(
        "inventory-workflow",
        help="Local workflow (reads the store directly, no SSH): build the health overlay, merge it into the canonical "
             "database-inventory.json, then render the dated summary. No SSH, no re-collection.",
    )
    workflow_parser.add_argument("--days", type=int, default=2, help="Only use metrics from the last N days. Default: 2.")
    workflow_parser.add_argument("--output-dir", help="Directory for the dated overlay + summary. Defaults to <runtime>/reports.")
    workflow_parser.add_argument("--inventory", help="Path to the canonical database-inventory.json. Defaults to data/database-inventory.json.")
    workflow_parser.add_argument("--date", help="Override the YYYYMMDD_HHMMSS stamp shared by both files (for testing).")
    workflow_parser.add_argument("--dry-run", action="store_true", help="Build the overlay but do not merge or render the summary.")
    workflow_parser.add_argument("--beauty", type=int, default=0,
                                 help="1 = also render the styled HTML + Markdown inventory report. Default 0 (plain summary only).")
    workflow_parser.set_defaults(report_function=build_inventory_workflow)

    index_parser = subparsers.add_parser(
        "index-usage-report",
        help="One index report per server_id: totals, disabled indexes, drop candidates and "
             "fragmentation, each row with a recommended action. Separate from the metric reports "
             "because a per-index listing is far too large to share a report with alerts.",
    )
    index_parser.add_argument("--days", type=int, default=3,
                              help="Only use metrics from the last N days. Default: 3.")
    index_parser.add_argument("--limit", type=int, default=25,
                              help="Detail rows listed per table. Counts stay exact. Default: 25.")
    index_parser.add_argument("--server-id", help="Only this server_id. Default: every server with data.")
    index_parser.add_argument("--output-dir",
                              help="Publish an HTML copy here so the report has a URL. Defaults to "
                                   "<runtime>/reports, which is what the webhost serves.")
    index_parser.set_defaults(report_function=create_index_reports)

    backfill_parser = subparsers.add_parser(
        "backfill-dated-reports",
        help="Write the YYYYMMDD_ copies of server-metrics and index-usage for past days, so "
             "?date= can reach days from before archiving was switched on. Reads the history "
             "already in metric_results; never touches the live file names.",
    )
    backfill_parser.add_argument("--date", dest="dates", action="append", required=True,
                                 metavar="YYYY-MM-DD",
                                 help="A day to rebuild. Repeat for several days.")
    backfill_parser.add_argument("--days", type=int, default=7,
                                 help="Window each rebuilt page covers, ending on its own date. "
                                      "Default: 7, matching the scheduled workflow.")
    backfill_parser.add_argument("--output-dir",
                                 help="Report directory. Defaults to <runtime>/reports.")
    backfill_parser.add_argument("--inventory",
                                 help="database-inventory.json to take the server list from. "
                                      "Defaults to the one in the report directory.")
    backfill_parser.set_defaults(report_function=backfill_dated_reports)

    return parser.parse_args(argv)


def call_report_function(
    *,
    report_function: Callable[..., dict[str, Any]],
    args: argparse.Namespace,
    config: DbOpsConfig,
    config_path: str,
) -> dict[str, Any]:
    available_values = vars(args) | {
        "config": config,
        "config_path": config_path,
        "command_name": args.command,
        # The store declaration, not a path: helpers pass this straight into a store
        # class, so it must follow data/store_config.json rather than pinning SQLite.
        "sqlite_path": config.store,
        "telegram_groups": telegram_groups(),
    }
    function_params = inspect.signature(report_function).parameters
    function_args = {
        name: available_values[name]
        for name in function_params
        if name in available_values and available_values[name] is not None
    }
    return report_function(**function_args)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    logger = None
    try:
        # Before load_config: the store connection is opened while the config is resolved, and it
        # needs the passphrase already in the environment.
        from db_ops.lib.secret_text import set_key_env
        set_key_env(getattr(args, "key", None), getattr(args, "key_base64", None))

        config_path = str(resolve_config_path("reports", args.config))
        config = load_config(config_path)
        patch_stdout(config.log_dir / "reports_runtime.log", app_name="reports")
        logger = setup_app_logger(config, app_name="reports", enable_telegram_alerts=False, enable_console=False)
        log_function_call(logger, function_name=f"reports.{args.command}")
        args.logger = logger
        result = call_report_function(report_function=args.report_function, args=args, config=config, config_path=config_path)
    except ReportWorkflowError as exc:
        if logger:
            log_function_error(logger, function_name=f"reports.{args.command}", error_text=str(exc))
        print(f"ERROR: {exc}", file=sys.stderr)
        return int(exc.exit_code)
    except Exception as exc:  # noqa: BLE001 - command-line failure path.
        if logger:
            log_function_error(logger, function_name=f"reports.{args.command}", error_text=str(exc))
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
