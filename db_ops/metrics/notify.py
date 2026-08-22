from __future__ import annotations

from db_ops.config import load_config
from db_ops.levels import ERROR, LOGGING, WARNING, normalize_level
from db_ops.metrics.health import load_instances
from db_ops.metrics.message import build_metrics_message


def send_metrics_notifications(*, config_path: str, instances_path: str, send: bool, level: str | None) -> int:
    load_config(config_path)
    instances = load_instances(instances_path)
    target_levels = [normalize_level(level)] if level else [ERROR, WARNING, LOGGING]
    if send:
        raise RuntimeError(
            "Metrics does not send Telegram directly. "
            "Run db_ops.reports.cli create-metrics-reports, db_ops.reports.cli push-report-alerts, then db_ops.telegram.cli send-queue."
        )

    for target_level in target_levels:
        message = build_metrics_message(
            instances,
            title=f"DB Ops Metrics - {target_level.upper()}",
            level=target_level,
        )
        print(message)
        print("")

    return 0
