from __future__ import annotations

import ipaddress
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from time import perf_counter
from typing import Any

from db_ops.lib.telegram_route import telegram_groups
from db_ops.common.data_sources import DEFAULT_DATA_DIR, resolve_config_metric_target
from db_ops.config import DbOpsConfig
from db_ops.db import DbOpsStore
from db_ops.lib.paths import TOOL_ROOT


class ReportWorkflowError(RuntimeError):
    def __init__(self, message: str, *, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def force_hourly_report(
    *,
    config: DbOpsConfig,
    target_ip: str = "",
    server_id: str | None = None,
    summary_limit: int = 40,
    dedupe_seconds: int = 300,
    db_type: str | None = None,
    port: int | None = None,
    logger: Any | None = None,
    command_name: str = "force-hourly-report",
    config_path: str | Path | None = None,
    include_windowed: bool = False,
) -> dict[str, Any]:
    # server_id is the unique per-instance key and needs no db_type/port disambiguation; an IP can
    # be shared by several instances. Accept either; require one.
    normalized_server_id = str(server_id or "").strip()
    normalized_ip = validate_target_ip(target_ip) if str(target_ip or "").strip() else ""
    if not normalized_ip and not normalized_server_id:
        raise ReportWorkflowError("force-hourly-report requires --server-id or --target-ip.", exit_code=2)
    resolve_label = normalized_ip or normalized_server_id
    workflow_started = perf_counter()
    target_id = ""
    result: dict[str, Any] = {
        "command_name": command_name,
        "target_ip": normalized_ip,
        "server_id": normalized_server_id,
        "target_id": "",
        "summary_limit": summary_limit,
        "dedupe_seconds": dedupe_seconds,
        "include_windowed": bool(include_windowed),
        "exit_code": 1,
        "status": "running",
        "steps": [],
    }

    try:
        target = _run_step(
            logger=logger,
            command_name=command_name,
            step_name="resolve target",
            target_ip=resolve_label,
            target_id="",
            action=lambda: resolve_report_target(
                sqlite_path=config.store,
                target_ip=normalized_ip,
                server_id=normalized_server_id,
                db_type=db_type,
                port=port,
                data_dir=_data_dir_from_config_path(config_path),
            ),
        )
        target_id = target.target_id
        result["target_id"] = target_id
        # The server_id path resolved the IP for us; fill it in for logging/reply.
        if not normalized_ip:
            normalized_ip = str(getattr(target, "ip", "") or "")
            result["target_ip"] = normalized_ip
        if not normalized_server_id:
            normalized_server_id = str(getattr(target, "server_id", "") or "")
            result["server_id"] = normalized_server_id
        metrics_enabled = bool(getattr(target, "metrics_enabled", True))

        if metrics_enabled:
            collect_summary = _run_step(
                logger=logger,
                command_name=command_name,
                step_name="collect metrics",
                target_ip=normalized_ip,
                target_id=target_id,
                action=lambda: collect_target_metrics(
                    config_path=config_path, target_id=target_id, include_windowed=include_windowed,
                ),
            )
        else:
            collect_summary = _run_step(
                logger=logger,
                command_name=command_name,
                step_name="use stored metrics",
                target_ip=normalized_ip,
                target_id=target_id,
                action=lambda: stored_metric_summary(sqlite_path=config.store, target_id=target_id),
            )
        result["collect"] = _collect_summary_dict(collect_summary)

        created = _run_step(
            logger=logger,
            command_name=command_name,
            step_name="create hourly metrics report",
            target_ip=normalized_ip,
            target_id=target_id,
            action=lambda: create_hourly_metrics_report(
                sqlite_path=config.store,
                summary_limit=summary_limit,
                target_id=target_id,
            ),
        )
        result["created"] = created

        pushed = _run_step(
            logger=logger,
            command_name=command_name,
            step_name="push report alert",
            target_ip=normalized_ip,
            target_id=target_id,
            action=lambda: push_hourly_report_alerts(
                sqlite_path=config.store,
                telegram_groups=telegram_groups(),
                dedupe_seconds=dedupe_seconds,
                report_ids=[int(report_id) for report_id in created.get("report_ids", [])],
            ),
        )
        result["pushed"] = pushed
        result["status"] = "success"
        result["exit_code"] = 0
        return result
    except Exception as exc:
        result["status"] = "failed"
        result["error_message"] = safe_error_summary(exc)
        result["exit_code"] = getattr(exc, "exit_code", 1)
        _log_workflow_event(
            logger=logger,
            command_name=command_name,
            step_name="workflow",
            target_ip=normalized_ip,
            target_id=target_id,
            duration_ms=int((perf_counter() - workflow_started) * 1000),
            status="failed",
            error_message=result["error_message"],
        )
        if isinstance(exc, ReportWorkflowError):
            raise
        raise ReportWorkflowError(result["error_message"], exit_code=int(result["exit_code"])) from exc
    finally:
        if result["status"] == "success":
            _log_workflow_event(
                logger=logger,
                command_name=command_name,
                step_name="workflow",
                target_ip=normalized_ip,
                target_id=target_id,
                duration_ms=int((perf_counter() - workflow_started) * 1000),
                status="success",
                error_message="",
            )


def metric_history_report(
    *,
    config: DbOpsConfig,
    server_id: str,
    metric_code: str,
    hours: int,
    summary_limit: int = 150,
    dedupe_seconds: int = 0,
    logger: Any | None = None,
    command_name: str = "metric-history-report",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Create and queue a report from stored metric history only."""

    normalized_server_id = str(server_id or "").strip()
    normalized_metric_code = str(metric_code or "").strip().upper()
    if not normalized_server_id:
        raise ReportWorkflowError("server_id is required.", exit_code=2)
    if not normalized_metric_code:
        raise ReportWorkflowError("metric_code is required.", exit_code=2)
    if int(hours) < 1:
        raise ReportWorkflowError("hours must be >= 1.", exit_code=2)
    if int(summary_limit) < 1:
        raise ReportWorkflowError("summary_limit must be >= 1.", exit_code=2)

    workflow_started = perf_counter()
    result: dict[str, Any] = {
        "command_name": command_name,
        "server_id": normalized_server_id,
        "metric_code": normalized_metric_code,
        "hours": int(hours),
        "summary_limit": int(summary_limit),
        "dedupe_seconds": int(dedupe_seconds),
        "target_id": "",
        "exit_code": 1,
        "status": "running",
        "steps": [],
    }
    try:
        created = _run_step(
            logger=logger,
            command_name=command_name,
            step_name="create stored metric history report",
            target_ip="",
            target_id=normalized_server_id,
            action=lambda: create_stored_metric_history_report(
                sqlite_path=config.store,
                server_id=normalized_server_id,
                metric_code=normalized_metric_code,
                hours=int(hours),
                summary_limit=int(summary_limit),
                now=now,
            ),
        )
        result.update(
            {
                key: created[key]
                for key in (
                    "window_start",
                    "window_end",
                    "row_count",
                    "displayed_row_count",
                    "target_id",
                    "target_ids",
                    "report_ids",
                )
                if key in created
            }
        )
        result["created"] = created

        pushed = _run_step(
            logger=logger,
            command_name=command_name,
            step_name="queue stored metric history report",
            target_ip="",
            target_id=str(created.get("target_id") or normalized_server_id),
            action=lambda: push_metric_history_report(
                sqlite_path=config.store,
                telegram_groups=telegram_groups(),
                dedupe_seconds=int(dedupe_seconds),
                report_ids=[int(report_id) for report_id in created.get("report_ids", [])],
            ),
        )
        result["pushed"] = pushed
        result["queued"] = int(pushed.get("queued", 0))
        result["status"] = "success"
        result["exit_code"] = 0
        return result
    except Exception as exc:
        error_message = safe_error_summary(exc)
        _log_workflow_event(
            logger=logger,
            command_name=command_name,
            step_name="workflow",
            target_ip="",
            target_id=normalized_server_id,
            duration_ms=int((perf_counter() - workflow_started) * 1000),
            status="failed",
            error_message=error_message,
        )
        if isinstance(exc, ReportWorkflowError):
            raise
        exit_code = int(getattr(exc, "exit_code", 1))
        if isinstance(exc, ValueError):
            exit_code = 2
        raise ReportWorkflowError(error_message, exit_code=exit_code) from exc
    finally:
        if result["status"] == "success":
            _log_workflow_event(
                logger=logger,
                command_name=command_name,
                step_name="workflow",
                target_ip="",
                target_id=str(result.get("target_id") or normalized_server_id),
                duration_ms=int((perf_counter() - workflow_started) * 1000),
                status="success",
                error_message="",
            )


def validate_target_ip(target_ip: str) -> str:
    value = str(target_ip or "").strip()
    if not value:
        raise ReportWorkflowError("target_ip is required.", exit_code=2)
    try:
        return str(ipaddress.ip_address(value))
    except ValueError as exc:
        raise ReportWorkflowError(f"Invalid target_ip: {value}.", exit_code=2) from exc


def safe_error_summary(error: object) -> str:
    text = " ".join(str(error).replace("\r", " ").replace("\n", " ").split())
    if not text:
        return "workflow failed"
    blocked_markers = ("password", "token", "secret", "connection string", "pwd=")
    lowered = text.lower()
    if any(marker in lowered for marker in blocked_markers):
        return "workflow failed; sensitive error detail hidden"
    return text[:300]


def resolve_report_target(
    *,
    sqlite_path: str | Path,
    target_ip: str = "",
    server_id: str | None = None,
    db_type: str | None = None,
    port: int | None = None,
    data_dir: str | Path = DEFAULT_DATA_DIR,
) -> Any:
    normalized_server_id = str(server_id or "").strip()
    config_target = resolve_config_metric_target(
        target_ip=target_ip or None,
        server_id=normalized_server_id or None,
        db_type=db_type,
        port=port,
        data_dir=data_dir,
    )
    if config_target is not None:
        if not bool(getattr(config_target, "reports_enabled", True)):
            raise ReportWorkflowError(
                f"report disabled for target {normalized_server_id or target_ip}", exit_code=2
            )
        return config_target

    # server_id targets live in the config inventory; the stored-data fallback below only works by
    # IP, so a server_id that matched no config target is a hard error (not an IP to fall back on).
    if normalized_server_id and not str(target_ip or "").strip():
        raise ReportWorkflowError(
            f"No configured metric target for server_id={normalized_server_id}.", exit_code=2
        )

    store = DbOpsStore(sqlite_path)
    rows = store.fetch_latest_metric_report_results(target_ip=target_ip)
    target_ids = sorted({str(row["target_id"] or "") for row in rows if str(row["target_id"] or "")})
    # Narrow the stored-data fallback by db_type when given (target_id contains /<db_type>/).
    normalized_db_type = str(db_type or "").strip().lower()
    if normalized_db_type and normalized_db_type not in ("-", "any", "auto"):
        narrowed = [tid for tid in target_ids if f"/{normalized_db_type}/" in tid]
        if narrowed:
            target_ids = narrowed
    if not target_ids:
        raise ReportWorkflowError(f"No stored metric data found for target_ip={target_ip}. Run Metrics Engine first.", exit_code=2)
    if len(target_ids) > 1:
        raise ReportWorkflowError(f"Ambiguous stored metric data for target_ip={target_ip}; matched target_ids: {', '.join(target_ids)}.", exit_code=2)
    return SimpleNamespace(target_id=target_ids[0], ip=target_ip)


def stored_metric_summary(*, sqlite_path: str | Path, target_id: str) -> Any:
    store = DbOpsStore(sqlite_path)
    rows = store.fetch_latest_metric_report_results(target_id=target_id)
    if not rows:
        raise ReportWorkflowError(
            f"metrics collection is disabled for target {target_id} and no stored metric rows are available",
            exit_code=2,
        )
    statuses = [str(row["status"] or "").upper() for row in rows]
    return SimpleNamespace(
        run_id=None,
        target_count=1,
        metric_count=len({str(row["metric_code"] or "") for row in rows}),
        executed_count=0,
        result_count=len(rows),
        error_count=sum(1 for status in statuses if status == "ERROR"),
        warning_count=sum(1 for status in statuses if status in {"WARNING", "NO_DATA"}),
        critical_count=sum(1 for status in statuses if status == "CRITICAL"),
        duration_seconds=0,
    )


def _data_dir_from_config_path(config_path: str | Path | None) -> Path:
    if config_path is None:
        return DEFAULT_DATA_DIR
    return Path(config_path).resolve().parent / "data"


def collect_target_metrics(
    *, config_path: str | Path | None, target_id: str, include_windowed: bool = False
) -> Any:
    argv = [
        sys.executable,
        "-m",
        "db_ops.metrics.cli",
        "--config",
        str(config_path or "config.json"),
        "collect",
        "--target-id",
        target_id,
        "--force",
    ]
    # Opt-in only: see collector._metric_window_open. Without this the on-demand report ran
    # DBCC CHECKDB and an index-fragmentation scan at whatever hour someone typed it.
    if include_windowed:
        argv.append("--include-windowed")
    completed = subprocess.run(
        argv,
        cwd=TOOL_ROOT,
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    )
    if completed.returncode != 0:
        error_text = completed.stderr.strip() or completed.stdout.strip() or f"Metrics CLI failed with exit code {completed.returncode}"
        raise ReportWorkflowError(safe_error_summary(error_text), exit_code=completed.returncode)
    return _collect_summary_from_cli_output(completed.stdout)


def create_hourly_metrics_report(*, sqlite_path: str, summary_limit: int, target_id: str) -> dict[str, Any]:
    from db_ops.reports.metrics_reports import create_metrics_reports

    return create_metrics_reports(sqlite_path=sqlite_path, summary_limit=summary_limit, target_id=target_id)


def create_stored_metric_history_report(
    *,
    sqlite_path: str | Path,
    server_id: str,
    metric_code: str,
    hours: int,
    summary_limit: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    from db_ops.reports.metrics_reports import create_metric_history_report

    return create_metric_history_report(
        sqlite_path=sqlite_path,
        server_id=server_id,
        metric_code=metric_code,
        hours=hours,
        summary_limit=summary_limit,
        now=now,
    )


def push_hourly_report_alerts(
    *,
    sqlite_path: str,
    telegram_groups: dict[str, str],
    dedupe_seconds: int,
    report_ids: list[int],
) -> dict[str, Any]:
    from db_ops.reports.metrics_reports import push_report_alerts

    return push_report_alerts(
        sqlite_path=sqlite_path,
        telegram_groups=telegram_groups,
        dedupe_seconds=dedupe_seconds,
        report_ids=report_ids,
    )


def push_metric_history_report(
    *,
    sqlite_path: str | Path,
    telegram_groups: dict[str, str],
    dedupe_seconds: int,
    report_ids: list[int],
) -> dict[str, Any]:
    from db_ops.reports.metrics_reports import push_report_alerts

    return push_report_alerts(
        sqlite_path=sqlite_path,
        telegram_groups=telegram_groups,
        dedupe_seconds=dedupe_seconds,
        report_ids=report_ids,
    )


def _run_step(
    *,
    logger: Any | None,
    command_name: str,
    step_name: str,
    target_ip: str,
    target_id: str,
    action: Any,
) -> Any:
    started = perf_counter()
    _log_workflow_event(
        logger=logger,
        command_name=command_name,
        step_name=step_name,
        target_ip=target_ip,
        target_id=target_id,
        duration_ms=0,
        status="started",
        error_message="",
    )
    try:
        value = action()
    except Exception as exc:
        _log_workflow_event(
            logger=logger,
            command_name=command_name,
            step_name=step_name,
            target_ip=target_ip,
            target_id=target_id,
            duration_ms=int((perf_counter() - started) * 1000),
            status="failed",
            error_message=safe_error_summary(exc),
        )
        raise
    _log_workflow_event(
        logger=logger,
        command_name=command_name,
        step_name=step_name,
        target_ip=target_ip,
        target_id=getattr(value, "target_id", target_id),
        duration_ms=int((perf_counter() - started) * 1000),
        status="success",
        error_message="",
    )
    return value


def _log_workflow_event(
    *,
    logger: Any | None,
    command_name: str,
    step_name: str,
    target_ip: str,
    target_id: str,
    duration_ms: int,
    status: str,
    error_message: str,
) -> None:
    if logger is None:
        return
    from db_ops.logging_ops import log_event

    log_event(
        logger,
        level="error" if status == "failed" else "logging",
        message=(
            f"command_name={command_name} "
            f"step_name={step_name} "
            f"target_ip={target_ip} "
            f"target_id={target_id} "
            f"duration_ms={duration_ms} "
            f"status={status} "
            f"error_message={error_message}"
        ),
    )


def _collect_summary_dict(summary: Any) -> dict[str, Any]:
    return {
        "run_id": getattr(summary, "run_id", None),
        "target_count": getattr(summary, "target_count", 0),
        "metric_count": getattr(summary, "metric_count", 0),
        "executed_count": getattr(summary, "executed_count", 0),
        "result_count": getattr(summary, "result_count", 0),
        "error_count": getattr(summary, "error_count", 0),
        "warning_count": getattr(summary, "warning_count", 0),
        "critical_count": getattr(summary, "critical_count", 0),
        "duration_seconds": getattr(summary, "duration_seconds", 0),
    }


def _collect_summary_from_cli_output(stdout: str) -> Any:
    values: dict[str, Any] = {}
    for line in stdout.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if value.replace(".", "", 1).isdigit():
            values[key] = float(value) if "." in value else int(value)
        else:
            values[key] = value
    if not values:
        try:
            parsed = json.loads(stdout)
            if isinstance(parsed, dict):
                values = parsed
        except json.JSONDecodeError:
            values = {"stdout": stdout.strip()}
    return SimpleNamespace(**values)


__all__ = [
    "ReportWorkflowError",
    "force_hourly_report",
    "metric_history_report",
    "safe_error_summary",
    "validate_target_ip",
]
