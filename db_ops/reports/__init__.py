from __future__ import annotations

from typing import Any


__all__ = [
    "create_backup_health_report",
    "create_metrics_reports",
    "push_report_alerts",
    "queue_metrics_reports",
    "run_scheduled_reports",
]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from db_ops.reports import metrics_reports

        return getattr(metrics_reports, name)
    raise AttributeError(name)
