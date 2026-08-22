from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from db_ops.lib.paths import TOOL_ROOT


def _describe_store(source) -> str:
    """Human-readable description of whatever store source was injected."""
    from db_ops.db.backend import StoreTarget

    try:
        return StoreTarget.coerce(source).describe()
    except Exception:  # noqa: BLE001 - a label must never fail the command.
        return str(source)


def queue_metrics_reports(
    *,
    sqlite_path: str | Path,
    telegram_groups: dict[str, str],
    summary_limit: int = 40,
    dedupe_seconds: int = 300,
    target_id: str | None = None,
    target_ip: str | None = None,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    argv = [
        sys.executable,
        "-m",
        "db_ops.reports.cli",
    ]
    if config_path:
        argv.extend(["--config", str(config_path)])
    argv += [
        "queue-metrics-reports",
        "--summary-limit",
        str(summary_limit),
        "--dedupe-seconds",
        str(dedupe_seconds),
    ]
    if target_id:
        argv.extend(["--target-id", target_id])
    if target_ip:
        argv.extend(["--target-ip", target_ip])

    completed = subprocess.run(
        argv,
        cwd=TOOL_ROOT,
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    )
    result = _parse_json_output(completed.stdout)
    result.update(
        {
            "exit_code": completed.returncode,
            "stdout": completed.stdout.strip(),
            "stderr": completed.stderr.strip(),
            # Informational only. str() of a store declaration would be a dataclass
            # repr, so report the connection string the subprocess actually used.
            "store": _describe_store(sqlite_path),
            "telegram_group_count": len(telegram_groups),
        }
    )
    if completed.returncode != 0:
        raise RuntimeError(result.get("stderr") or result.get("stdout") or f"Reports CLI failed with exit code {completed.returncode}")
    return result


def _parse_json_output(stdout: str) -> dict[str, Any]:
    text = stdout.strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
        return dict(data) if isinstance(data, dict) else {"json": data}
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(text[start : end + 1])
            return dict(data) if isinstance(data, dict) else {"json": data}
        except json.JSONDecodeError:
            return {}
    return {}
