"""App-command daemon status report.

Reads the runtime SQLite (``job_runs``) + ``app_commands.json`` and answers: which app
commands run on this node, whether each is active, its last run time/status/duration, whether
it is currently due/overdue, and (optionally) how fresh the collected metrics are. It computes
purely from local files, so it runs anywhere the runtime store + config live — in particular
inside the worker container, where the control app's ``worker-status`` invokes it over SSH.

    python -m db_ops.jobs.status [--config config.json] [--json] [--no-metrics]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from db_ops.lib import secret_text
from db_ops.config import load_config, resolve_config_path
from db_ops.db import DbOpsStore
from db_ops.db.metric_store import MetricStore
from db_ops.jobs.daemon import (
    DEFAULT_DATA_DIR,
    app_command_in_schedule_window,
    app_command_is_due,
    load_app_commands,
    row_time,
    _command_runs_on_node,
)


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


def _metric_freshness(config, *, key: str | None = None) -> dict | None:
    """Metric recency, read through the store layer rather than by opening SQLite here.

    Takes the config (not a path) so the store's declared backend decides where to read from.
    """
    return MetricStore.from_config(config, key=key).fetch_freshness()


def build_status(*, config_path: str | None = None, data_dir=None, with_metrics: bool = True,
                 key: str | None = None) -> dict:
    config = load_config(str(resolve_config_path("status", config_path)) if config_path else None)
    data_dir = data_dir or DEFAULT_DATA_DIR
    node_role = getattr(config, "node_role", "master")
    now = datetime.now(timezone.utc)

    commands = load_app_commands(data_dir / "app_commands.json")
    store = DbOpsStore.from_config(config, key=key)
    latest = store.fetch_latest_job_runs_by_job_code()

    apps = []
    for cmd in sorted(commands.values(), key=lambda c: c.app_command_id):
        run = latest.get(cmd.app_command_id)
        last_dt = row_time(run)
        last_status = (str(run["status"]) if run and run["status"] is not None else None)
        on_node = _command_runs_on_node(cmd, node_role)
        apps.append({
            "app_command_id": cmd.app_command_id,
            "display_name": cmd.display_name,
            "node_role": cmd.node_role,
            "runs_on_this_node": on_node,
            "active": cmd.active,
            "repeat_interval": cmd.repeat_interval_seconds,
            "last_status": last_status,
            "last_run_utc": last_dt.isoformat() if last_dt else None,
            "last_run_age_seconds": (now - last_dt).total_seconds() if last_dt else None,
            "last_duration_ms": (run["duration_ms"] if run and "duration_ms" in run.keys() else None),
            "in_window": app_command_in_schedule_window(cmd),
            "due_now": app_command_is_due(cmd, run, now=now),
            "error_text": (run["error_text"] if run and "error_text" in run.keys() and run["error_text"] else None),
        })

    # "store" names the backend and where it resolved from (data/store_config.json), so a node
    # can be asked what it is actually writing to rather than having it inferred from a path.
    out = {"generated_at": now.isoformat(), "node_role": node_role,
           "store": config.store.describe(),
           "sqlite_path": str(config.sqlite_path), "apps": apps}
    if with_metrics:
        out["metrics"] = _metric_freshness(config, key=key)
    return out


def _print_human(rep: dict) -> None:
    store = rep.get("store") or {}
    print(f"node_role={rep['node_role']}  now={rep['generated_at']}  "
          f"store={store.get('backend', 'sqlite')}  sqlite={rep['sqlite_path']}")
    on = [a for a in rep["apps"] if a["runs_on_this_node"]]
    print(f"app commands on this node: {sum(1 for a in on if a['active'])} active / {len(on)} total\n")
    hdr = f"{'APP COMMAND':<32}{'ACT':<5}{'LAST STATUS':<13}{'LAST RUN (age)':<26}{'DUE':<5}ERROR"
    print(hdr)
    print("-" * len(hdr))
    for a in rep["apps"]:
        if not a["runs_on_this_node"]:
            continue
        age = _age(a["last_run_age_seconds"])
        last = f"{(a['last_run_utc'] or '-')[:19]} ({age})" if a["last_run_utc"] else "never run"
        err = (a["error_text"] or "")[:60].replace("\n", " ")
        flag = "" if a["active"] else "  [INACTIVE]"
        print(f"{a['app_command_id']:<32}{'yes' if a['active'] else 'no':<5}"
              f"{str(a['last_status'] or '-'):<13}{last:<26}{'yes' if a['due_now'] else 'no':<5}{err}{flag}")
        if not a["in_window"]:
            print(f"{'':<32}(outside schedule window now)")

    m = rep.get("metrics")
    if m:
        print(f"\nmetric_results: newest={m['overall_last']}  rows={m['overall_rows']}")
        for t in m["per_target"]:
            print(f"  {str(t['ip'] or t['server_id'] or '?'):<18} newest={t['last']}  rows={t['rows']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=None)
    ap.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")
    ap.add_argument("--no-metrics", action="store_true", help="Skip the metric_results freshness section.")
    # A PostgreSQL store needs its password decrypted from the secret store, so this command needs
    # the passphrase like any other. It is not required for a SQLite store (a file needs no
    # credential), which is why it stayed keyless until the store became switchable. `docker exec`
    # does not inherit environment the daemon set at runtime, so passing it explicitly is what makes
    # an in-container status check work.
    secret_text.add_key_argument(ap)
    args = ap.parse_args(argv if argv is not None else sys.argv[1:])
    secret_text.set_key_env(args.key, args.key_base64)

    rep = build_status(config_path=args.config, with_metrics=not args.no_metrics,
                       key=secret_text.resolve_cli_key(args.key, args.key_base64))
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        _print_human(rep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
