"""One app command of ``app_commands.json``, and the rules that say whether it runs now (1.0.0).

The daemon (``jobs.daemon``) schedules by these rules, and a node's status report
(``db.node_status``, which the master's ``control worker-status`` runs in the worker's container)
reads by them. They lived in the daemon until 1.0.0, so the report was the jobs app's CLI, and the
control app started it - one app driving another's CLI (rules R42), hidden while the call was a
shell string; 0.42.0's 1.409 made it an argv list and the guard saw it. A rule shared by two
components belongs here (the operator, 2026-11-21: *move the report into db.cli*).

Pure: the parsing of one record, and the three questions - does it run on this node, is its window
open, is it due. The daemon's own defaults (retry 60 s, timeout 300 s) are stated here once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from dbabrain.lib import node_role as node_role_rule
from dbabrain.lib import run_mode as run_mode_lib
from dbabrain.lib.time_window import (DueVerdict, TimeWindow, due_from_row, is_time_window_open,
                                      parse_time_window_config)

#: What a record that states none runs with - the daemon's defaults since it was written.
RETRY_DEFAULT_SECONDS = 60
TIMEOUT_DEFAULT_SECONDS = 300
WINDOW_DEFAULTS = {"from_day": 1, "to_day": 31, "from_hour": 0, "to_hour": 23,
                   "retry_interval": RETRY_DEFAULT_SECONDS, "timeout": TIMEOUT_DEFAULT_SECONDS}


@dataclass(frozen=True)
class AppCommand:
    app_command_id: str
    app_code: str
    app_name: str
    display_name: str
    log_scope: str
    working_dir: str
    command_text: str
    time_window: TimeWindow
    active: bool
    node_role: str = "all"  # master | worker | all — which cluster node runs this command
    #: sync (default) = the daemon waits for one run to finish before starting the next; async =
    #: it starts the command whenever it is due, in flight or not, up to max_parallel. An async
    #: command must refuse its own duplicate work — see dbabrain/lib/run_mode.py.
    run_mode: run_mode_lib.RunMode = field(default_factory=run_mode_lib.RunMode)

    @property
    def repeat_interval_seconds(self) -> int | None:
        return self.time_window.repeat_interval

    @property
    def retry_interval_seconds(self) -> int:
        # Explicit None check so 0 (retry immediately) survives instead of falling back to 60.
        value = self.time_window.retry_interval
        return int(value) if value is not None else RETRY_DEFAULT_SECONDS

    @property
    def timeout_seconds(self) -> int:
        # Explicit None check so 0 (no timeout-kill) survives instead of falling back to 300.
        value = self.time_window.timeout
        return int(value) if value is not None else TIMEOUT_DEFAULT_SECONDS

    @property
    def timeout_disabled(self) -> bool:
        """timeout == 0 => long-running service; the daemon never timeout-kills it."""
        return self.time_window.timeout == 0


def parse_app_command(item: dict[str, Any], *, log_scope: str) -> tuple[AppCommand, tuple[str, ...]]:
    """One record of ``app_commands.json`` -> the command, and the time window's deprecation warnings.

    ``log_scope`` is the caller's: the daemon validates it against the logging scopes (a component
    ``lib`` may not import); a reader that only reports passes the record's value as it stands.
    """
    app_command_id = str(item["app_command_id"])
    parsed = parse_time_window_config(item, context=f"app_commands.{app_command_id}", defaults=WINDOW_DEFAULTS)
    command = AppCommand(
        app_command_id=app_command_id,
        app_code=str(item.get("app_code") or item["app_command_id"]),
        app_name=str(item.get("app_name") or item.get("app_code") or item["app_command_id"]),
        display_name=str(item.get("display_name") or item.get("command_name") or item.get("app_name")
                         or item["app_command_id"]),
        log_scope=log_scope,
        working_dir=str(item.get("working_dir", "")),
        command_text=str(item.get("command_text", "")),
        time_window=parsed.time_window,
        active=bool(item.get("active", True)),
        node_role=(str(item.get("node_role", "all")).strip().lower() or "all"),
        run_mode=run_mode_lib.parse(item),
    )
    return command, tuple(parsed.warnings)


def runs_on_node(command: AppCommand, node_role: str) -> bool:
    """True if this command should run on a node with the given role. ``all`` (and the legacy
    aliases ``both``/``any``/empty) runs everywhere; otherwise the command's node_role must equal
    the node role (``master`` runs master+all, ``worker`` runs worker+all)."""
    return node_role_rule.runs_on(command.node_role, node_role, default="all")


def due(command: AppCommand, latest_run: Any | None, now: datetime | None = None) -> DueVerdict:
    """Whether this command runs now, with the reason when it does not.

    An ``async`` command's own run in flight is not a reason to withhold the next one - that is what
    the field means, and ``max_parallel`` is the cap. Without this the store-side due check answered
    "running within timeout" and the concurrency never happened (measured 2026-09-19). What keeps the
    *work* from doubling is the claim, not this check.
    """
    return due_from_row(
        time_window=command.time_window,
        row=latest_run,
        now=now or datetime.now(timezone.utc),
        retry_default=RETRY_DEFAULT_SECONDS,
        timeout_default=TIMEOUT_DEFAULT_SECONDS,
        running_blocks=not command.run_mode.is_async,
    )


def window_open(command: AppCommand, now_local: datetime) -> bool:
    """Whether the command's allowed hours and days include ``now_local`` (the display zone's now)."""
    return is_time_window_open(command.time_window, now_local)
