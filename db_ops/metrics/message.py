from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from db_ops.levels import ERROR, LOGGING, WARNING
from db_ops.metrics.health import InstanceHealth


def build_metrics_message(instances: list[InstanceHealth], *, title: str, level: str | None = None) -> str:
    filtered = [item for item in instances if level is None or item.level == level]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    counts = {
        LOGGING: sum(1 for item in instances if item.level == LOGGING),
        WARNING: sum(1 for item in instances if item.level == WARNING),
        ERROR: sum(1 for item in instances if item.level == ERROR),
    }

    lines = [
        title,
        f"TimeUTC: {now}",
        f"Instances: {len(instances)} | OK: {counts[LOGGING]} | Warning: {counts[WARNING]} | Error: {counts[ERROR]}",
        "",
    ]

    if not filtered:
        lines.append("No instances for this level.")
        return "\n".join(lines)

    for item in filtered:
        lines.extend(format_instance_lines(item))
    return "\n".join(lines).strip()


def format_instance_lines(item: InstanceHealth) -> list[str]:
    endpoint = f"{item.ip}:{item.port}" if item.port is not None else item.ip
    metrics = item.metrics
    metric_parts = [
        format_metric("CPU", metrics.get("cpu_pct"), "%"),
        format_metric("MEM", metrics.get("memory_used_pct"), "%"),
        format_metric("DISK", metrics.get("disk_used_pct"), "%"),
        format_metric("Sessions", metrics.get("active_sessions"), ""),
        format_metric("Blocked", metrics.get("blocked_sessions"), ""),
        format_metric("LongQ", metrics.get("long_running_queries"), ""),
        format_metric("BackupAge", metrics.get("backup_age_hours"), "h"),
    ]
    metric_text = " | ".join(part for part in metric_parts if part)
    last_error = str(metrics.get("last_error") or "").strip()

    lines = [
        f"[{item.level.upper()}] {item.ord} {item.site} {item.env} {item.db_type}/{item.service_name}",
        f"IP: {endpoint} | OS: {item.os} | Instance: {item.instance_name or '-'}",
        f"status_OS={item.status_os} | status_DB={item.status_db} | status_connect={item.status_connect}",
    ]
    if metric_text:
        lines.append(metric_text)
    if last_error:
        lines.append(f"last_error: {last_error[:300]}")
    if item.note:
        lines.append(f"note: {item.note[:200]}")
    lines.append("")
    return lines


def format_metric(name: str, value: Any, suffix: str) -> str:
    if value is None or value == "":
        return ""
    return f"{name}={value}{suffix}"
