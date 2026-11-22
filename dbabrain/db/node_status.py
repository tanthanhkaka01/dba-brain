"""A node's status: its app commands, their last runs, and how fresh its metrics are (1.0.0).

Reads the runtime store (``job_runs``, ``metric_results``) and ``app_commands.json``, and answers:
which app commands run on this node, whether each is active, its last run time / status /
duration, whether it is due now and inside its window, and (optionally) metric recency. It runs
wherever the store and the configuration are - in particular in the worker's container, where the
master's ``control worker-status`` runs it over SSH:

    python -m dbabrain.db.cli [--config config.json] node-status [--json] [--no-metrics]

It was ``jobs.status`` until 1.0.0. The control app starting it was one app driving another's CLI
(rules R42), unseen while the call was a shell string; 0.42.0's 1.409 made it an argv list, the
full suite of 2026-11-21 caught it, and the operator chose to move the report into the shared
``db`` layer. The scheduling rules it reads by moved to ``lib.app_schedule`` with it; ``jobs.status``
still answers, as this module.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dbabrain.db import DbaBrainStore
from dbabrain.db.metric_store import MetricStore
from dbabrain.lib import app_schedule
from dbabrain.lib.config import load_config, resolve_config_path
from dbabrain.lib.json_io import load_json_file
from dbabrain.lib.time_window import run_anchor
from dbabrain.lib.timezone import display_now


def _age(seconds: float | None) -> str:
    if seconds is None:
        return "never"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60}m ago"
    return f"{seconds // 86400}d{(seconds % 86400) // 3600}h ago"


def _app_commands(path: Path) -> list[app_schedule.AppCommand]:
    """The commands as written - a report reads, it does not refuse a record the daemon would."""
    data = load_json_file(path) if path.is_file() else {}
    commands = []
    for item in data.get("app_commands", []):
        command, _warnings = app_schedule.parse_app_command(item, log_scope=str(item.get("log_scope") or ""))
        commands.append(command)
    return sorted(commands, key=lambda c: c.app_command_id)


def build_status(*, config_path: str | None = None, data_dir: Path | None = None, with_metrics: bool = True,
                 key: str | None = None) -> dict[str, Any]:
    config = load_config(str(resolve_config_path("status", config_path)) if config_path else None)
    if data_dir is None:
        from dbabrain.lib.paths import DEFAULT_DATA_DIR

        data_dir = DEFAULT_DATA_DIR
    node_role = getattr(config, "node_role", "master")
    now = datetime.now(timezone.utc)
    now_local = display_now()

    store = DbaBrainStore.from_config(config, key=key)
    latest = store.fetch_latest_job_runs_by_job_code()

    apps = []
    for cmd in _app_commands(Path(data_dir) / "app_commands.json"):
        run = latest.get(cmd.app_command_id)
        last_dt = run_anchor(run)
        last_status = (str(run["status"]) if run and run["status"] is not None else None)
        apps.append({
            "app_command_id": cmd.app_command_id,
            "display_name": cmd.display_name,
            "node_role": cmd.node_role,
            "runs_on_this_node": app_schedule.runs_on_node(cmd, node_role),
            "active": cmd.active,
            "repeat_interval": cmd.repeat_interval_seconds,
            "last_status": last_status,
            "last_run_utc": last_dt.isoformat() if last_dt else None,
            "last_run_age_seconds": (now - last_dt).total_seconds() if last_dt else None,
            "last_duration_ms": (run["duration_ms"] if run and "duration_ms" in run.keys() else None),
            "in_window": app_schedule.window_open(cmd, now_local),
            "due_now": app_schedule.due(cmd, run, now).due,
            "error_text": (run["error_text"] if run and "error_text" in run.keys() and run["error_text"] else None),
        })

    # "store" names the backend and where it resolved from (data/store_config.json), so a node
    # can be asked what it is actually writing to rather than having it inferred from a path.
    out: dict[str, Any] = {"generated_at": now.isoformat(), "node_role": node_role,
                           "store": config.store.describe(),
                           "sqlite_path": str(config.sqlite_path), "apps": apps}
    if with_metrics:
        # Through the store layer, not by opening SQLite here: the store's backend decides.
        out["metrics"] = MetricStore.from_config(config, key=key).fetch_freshness()
    return out


def render(rep: dict[str, Any], *, as_json: bool) -> str:
    if as_json:
        return json.dumps(rep, ensure_ascii=False, indent=2)
    lines: list[str] = []
    store = rep.get("store") or {}
    lines.append(f"node_role={rep['node_role']}  now={rep['generated_at']}  "
                 f"store={store.get('backend', 'sqlite')}  sqlite={rep['sqlite_path']}")
    on = [a for a in rep["apps"] if a["runs_on_this_node"]]
    lines.append(f"app commands on this node: {sum(1 for a in on if a['active'])} active / {len(on)} total\n")
    hdr = f"{'APP COMMAND':<32}{'ACT':<5}{'LAST STATUS':<13}{'LAST RUN (age)':<26}{'DUE':<5}ERROR"
    lines.append(hdr)
    lines.append("-" * len(hdr))
    for a in rep["apps"]:
        if not a["runs_on_this_node"]:
            continue
        age = _age(a["last_run_age_seconds"])
        last = f"{(a['last_run_utc'] or '-')[:19]} ({age})" if a["last_run_utc"] else "never run"
        err = (a["error_text"] or "")[:60].replace("\n", " ")
        flag = "" if a["active"] else "  [INACTIVE]"
        lines.append(f"{a['app_command_id']:<32}{'yes' if a['active'] else 'no':<5}"
                     f"{str(a['last_status'] or '-'):<13}{last:<26}{'yes' if a['due_now'] else 'no':<5}{err}{flag}")
        if not a["in_window"]:
            lines.append(f"{'':<32}(outside schedule window now)")
    m = rep.get("metrics")
    if m:
        lines.append(f"\nmetric_results: newest={m['overall_last']}  rows={m['overall_rows']}")
        for t in m["per_target"]:
            lines.append(f"  {str(t['ip'] or t['server_id'] or '?'):<18} newest={t['last']}  rows={t['rows']}")
    return "\n".join(lines)
