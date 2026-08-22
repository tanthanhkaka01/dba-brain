"""The shape of one job-run record, and of the metadata a Telegram-triggered run files.

``metadata`` is free-form JSON at the storage layer, which is why the Telegram audit shape
below is pinned here rather than left to each caller: it is what an operator greps
``job_runs`` with when asked "who ran this and against what". Two apps built the same dict
independently until the 2026-08-06 audit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class JobRun:
    job_code: str
    level: str
    status: str
    message: str
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: int | None = None
    error_text: str | None = None
    host_name: str | None = None
    metadata: dict[str, Any] | None = None


def telegram_log_metadata(
    *,
    telegram_user_id: str,
    telegram_username: str,
    telegram_command: str,
    target_ip: str,
    target_id: str,
    start_time: datetime,
    end_time: datetime,
    status: str,
    error_summary: str = "",
) -> dict[str, Any]:
    """The ``job_runs.metadata`` block for a run a Telegram command started.

    Timestamps are converted to UTC and formatted the way every other stored timestamp is
    (``YYYY-MM-DDTHH:MM:SSZ``), because these rows are read next to ``started_at`` /
    ``finished_at``; a local-time value here would silently be seven hours off on both
    nodes and only look wrong when someone compared the two columns.
    """
    return {
        "telegram_user_id": telegram_user_id,
        "telegram_username": telegram_username,
        "telegram_command": telegram_command,
        "target_ip": target_ip,
        "target_id": target_id,
        "start_time": start_time.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end_time": end_time.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "duration_ms": int((end_time - start_time).total_seconds() * 1000),
        "status": status,
        "error_summary": error_summary,
    }
