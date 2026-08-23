from __future__ import annotations
from db_ops.lib.text_format import format_log_value  # noqa: F401 - one definition, see that module

import argparse
import json
import os
import shlex
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, IO

from db_ops.lib.json_io import load_json_file
from db_ops.lib.secret_text import SECRET_KEY_ENV_VAR, resolve_cli_key
from db_ops.config import DEFAULT_CONFIG_PATH, DbOpsConfig, load_config, resolve_config_path
from db_ops.lib.time_window import TimeWindow, is_time_window_open, job_due, parse_time_window_config
from db_ops.db import DbOpsStore
from db_ops.db.store import utc_now_text
from db_ops.jobs.models import JobRun
from db_ops.logging_ops import LOG_SCOPE_ENV_VAR, build_log_paths, log_event, log_function_error, setup_app_logger, validate_log_scope
from db_ops.logging_ops.handlers import archive_yesterday_if_missing, ensure_current_log_file
from db_ops.logging_ops.runtime_stdout import patch_stdout
from db_ops.lib.paths import DEFAULT_DATA_DIR, REPO_ROOT, TOOL_ROOT  # noqa: F401 - one definition, see that module


# db_ops is a standalone repo root; keep REPO_ROOT as an alias so path resolution
# never escapes the project (was TOOL_ROOT.parents[1] under the old repo/tools/db_ops layout).
MAX_OUTPUT_CHARS = 8000


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

    @property
    def repeat_interval_seconds(self) -> int | None:
        return self.time_window.repeat_interval

    @property
    def retry_interval_seconds(self) -> int:
        # Explicit None check so 0 (retry immediately) survives instead of falling back to 60.
        value = self.time_window.retry_interval
        return int(value) if value is not None else 60

    @property
    def timeout_seconds(self) -> int:
        # Explicit None check so 0 (no timeout-kill) survives instead of falling back to 300.
        value = self.time_window.timeout
        return int(value) if value is not None else 300

    @property
    def timeout_disabled(self) -> bool:
        """timeout == 0 => long-running service; the daemon never timeout-kills it."""
        return self.time_window.timeout == 0


@dataclass
class RunningAppCommand:
    app_command: AppCommand
    process: subprocess.Popen
    started_at: datetime
    log_id: int
    working_dir: Path
    logs_dir: Path
    stdout_file: IO[str]
    stderr_file: IO[str]
    #: The console request this run answers, when it was not the schedule that started it. Carried
    #: on the running record rather than looked up at reap time, because by then the only thing
    #: left tying the two together is a field inside the job_runs metadata blob.
    run_request_id: int | None = None
    requested_by: str = ""

    @property
    def request_metadata(self) -> dict[str, Any]:
        """The request fields to stamp on this run's job_runs row, at start **and** at finish.

        The finish rewrites the row's metadata wholesale, so leaving them off there silently
        erased them: a completed requested run looked exactly like a scheduled one, and "why did
        this run at 03:00" stopped being answerable the moment it succeeded.
        """
        if self.run_request_id is None:
            return {}
        return {"run_request_id": int(self.run_request_id), "requested_by": self.requested_by}


@dataclass(frozen=True)
class ForwardedKeyArgs:
    option: str = ""
    value: str = ""

    @property
    def supplied(self) -> bool:
        return bool(self.option and self.value)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run DB Ops app-command daemon forever.")
    parser.add_argument("--config", default=None, help="Path to config JSON. Defaults to config.jobs.json or config.json.")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR), help="Directory containing app_commands.json.")
    parser.add_argument("--delay-seconds", type=int, default=2, help="Delay between app command scans. Default: 2.")
    parser.add_argument("--once", action="store_true", help="Run one app command scan and exit after started commands finish.")
    parser.add_argument(
        "--key",
        default=None,
        help="Passphrase to decrypt data/encrypted_secret_text.json. Forwarded to spawned app commands as --key.",
    )
    parser.add_argument(
        "--key-base64",
        "--key_base64",
        dest="key_base64",
        default=None,
        help=(
            "Base64-encoded UTF-8 passphrase to decrypt data/encrypted_secret_text.json. "
            "Forwarded to spawned app commands as --key-base64."
        ),
    )
    return parser.parse_args(argv)


# job_runs is the busiest table in the store — the daemon appends to it on every app-command
# start and finish — and nothing pruned it, so it reached ~1M rows / 965 MB with the oldest row
# 2.5 months back. The daemon is the right owner: it is the process that writes most of those
# rows and the only one always running.
JOB_RUNS_RETENTION_DAYS = 15
#: How often to sweep. Not every tick: the loop runs every 1-10s and a `SELECT ... LIMIT` per
#: tick would be pure noise against a table this size.
JOB_RUNS_SWEEP_INTERVAL_SECONDS = 300
#: Rows per transaction, and transactions per sweep. One capped batch keeps a pass short enough
#: that scheduling never waits on housekeeping; the backlog drains over successive passes.
JOB_RUNS_SWEEP_BATCH_SIZE = 20000
JOB_RUNS_SWEEP_MAX_BATCHES = 1

_JOB_RUNS_SWEEP_STATE: dict[str, float] = {"last_swept_at": 0.0}


def sweep_job_runs_history(
    *,
    store: DbOpsStore,
    logger: Any = None,
    retention_days: int = JOB_RUNS_RETENTION_DAYS,
    interval_seconds: int = JOB_RUNS_SWEEP_INTERVAL_SECONDS,
    now: float | None = None,
) -> int:
    """Move aged ``job_runs`` rows into ``job_runs_history``, at most once per interval.

    Returns how many rows moved (0 when the interval has not elapsed).

    Every failure is swallowed. Housekeeping must never take the scheduler down with it: a
    daemon that cannot archive is a table that grows, while a daemon that dies is every app
    command on the node not running.
    """
    moment = time.monotonic() if now is None else now
    if moment - _JOB_RUNS_SWEEP_STATE["last_swept_at"] < interval_seconds:
        return 0
    _JOB_RUNS_SWEEP_STATE["last_swept_at"] = moment
    try:
        moved = store.archive_old_job_runs(
            retention_days=retention_days,
            batch_size=JOB_RUNS_SWEEP_BATCH_SIZE,
            max_batches=JOB_RUNS_SWEEP_MAX_BATCHES,
        )
    except Exception as exc:  # noqa: BLE001 - housekeeping must not stop the scheduler.
        if logger:
            log_function_error(
                logger, function_name="app.daemon.job_runs_sweep", error_text=str(exc)
            )
        return 0
    if moved and logger:
        log_app_event(
            logger,
            "app.daemon.job_runs_sweep",
            status="done",
            moved=moved,
            retention_days=retention_days,
        )
    return moved


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    forwarded_key_args = build_forwarded_key_args(args)
    ensure_forwarded_secret_key_env(forwarded_key_args)
    # Before anything else can be left half-done: a SIGTERM arriving during startup must still
    # unwind through the shutdown path rather than killing the process where it stands.
    install_shutdown_handlers()
    logger = None
    running_commands: dict[str, RunningAppCommand] = {}
    try:
        config = load_config(resolve_config_path("jobs", args.config))
        patch_stdout(config.log_dir / "jobs_runtime.log", app_name="jobs")
        logger = setup_app_logger(config, app_name="jobs", enable_telegram_alerts=False)
        store = DbOpsStore.from_config(config)
        store.initialize()
        data_dir = Path(args.data_dir).resolve()
        delay_seconds = max(1, int(args.delay_seconds))
        log_app_event(logger, "app.daemon.start", status="running", data_dir=str(data_dir), delay_seconds=delay_seconds)
        _startup_commands = load_app_commands(data_dir / "app_commands.json", logger=logger)
        recover_stale_running_jobs(store=store, app_commands=_startup_commands, config=config, logger=logger)

        _db_lock_retries = 0
        while True:
            try:
                run_scheduler_scan(
                    config=config,
                    store=store,
                    data_dir=data_dir,
                    logger=logger,
                    running_commands=running_commands,
                    forwarded_key_args=forwarded_key_args,
                )
                # After scheduling, never before: a due app command must not wait on cleanup.
                sweep_job_runs_history(store=store, logger=logger)
                _db_lock_retries = 0
            except sqlite3.OperationalError as exc:
                if "database is locked" in str(exc).lower():
                    _db_lock_retries += 1
                    backoff = min(30, _db_lock_retries * 2)
                    if logger:
                        log_function_error(logger, function_name="app.daemon", error_text=f"database is locked (retry {_db_lock_retries}, backing off {backoff}s)")
                    print(f"WARNING: database is locked (retry {_db_lock_retries}), backing off {backoff}s", file=sys.stderr)
                    time.sleep(backoff)
                    continue
                raise
            if args.once:
                while running_commands:
                    collect_running_commands(store=store, logger=logger, running_commands=running_commands)
                    time.sleep(0.1)
                return 0
            time.sleep(delay_seconds)
    except _DaemonStopped as stop:
        close_running_on_shutdown(store=store, logger=logger,
                                  running_commands=running_commands, reason=stop.reason)
        if logger:
            log_app_event(logger, "app.daemon.stop", status="stopped", reason=stop.reason)
        return 0
    except KeyboardInterrupt:
        close_running_on_shutdown(store=store, logger=logger,
                                  running_commands=running_commands, reason="keyboard_interrupt")
        if logger:
            log_app_event(logger, "app.daemon.stop", status="stopped", reason="keyboard_interrupt")
        return 0
    except Exception as exc:  # noqa: BLE001 - command-line failure path.
        if logger:
            log_function_error(logger, function_name="app.daemon", error_text=str(exc))
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def _command_runs_on_node(command: AppCommand, node_role: str) -> bool:
    """True if this command should run on a node with the given role. ``all`` (and the
    legacy aliases ``both``/``any``/empty) runs everywhere; otherwise the command's
    node_role must equal the node role (``master`` runs master+all, ``worker`` runs
    worker+all)."""
    role = (command.node_role or "all").strip().lower()
    return role in ("all", "both", "any", "") or role == (node_role or "master").strip().lower()


def run_scheduler_scan(
    *,
    config: DbOpsConfig,
    store: DbOpsStore,
    data_dir: Path,
    logger: Any,
    running_commands: dict[str, RunningAppCommand],
    forwarded_key_args: ForwardedKeyArgs | None = None,
) -> None:
    collect_running_commands(store=store, logger=logger, running_commands=running_commands)
    app_commands = load_app_commands(data_dir / "app_commands.json", logger=logger)
    latest_runs = store.fetch_latest_job_runs_by_job_code()
    node_role = getattr(config, "node_role", "master")
    active_commands = [
        command
        for command in app_commands.values()
        if command.active and _command_runs_on_node(command, node_role)
    ]
    log_app_event(
        logger,
        "app.daemon.tick",
        status="running",
        node_role=node_role,
        active_commands=len(active_commands),
    )

    # A daemon with nothing to run looks identical to a daemon that is working: the same tick,
    # forever, with `active_commands=0`. Say which of the three reasons it is, because they have
    # three different fixes and the reader cannot tell them apart from the outside.
    #
    # The third is the one that catches people. `data/app_commands.example.json` is written for a
    # two-node estate where a `worker` runs the schedule, so a single-machine install copies it,
    # starts the daemon as `master`, and watches it do nothing — with no line saying that every
    # command was filtered out by a role it never chose.
    if not active_commands:
        inactive = [c for c in app_commands.values() if not c.active]
        wrong_role = [
            command for command in app_commands.values()
            if command.active and not _command_runs_on_node(command, node_role)
        ]
        if not app_commands:
            detail = (f"no commands are defined in {data_dir / 'app_commands.json'} - copy "
                      f"data/app_commands.example.json and enable what you want scheduled")
        elif wrong_role:
            roles = sorted({(c.node_role or "all") for c in wrong_role})
            detail = (
                f"{len(wrong_role)} command(s) are defined for node_role {roles} and this node is "
                f"'{node_role}', so none of them run here. Set DB_OPS_NODE_ROLE={roles[0]} on this "
                f"process, or change node_role to 'all' in app_commands.json"
            )
        else:
            detail = f"{len(inactive)} command(s) are defined but none has active=true"
        log_app_event(
            logger, "app.daemon.nothing_scheduled", status="warning",
            node_role=node_role, defined=len(app_commands), detail=detail,
        )

    sweep_run_requests(store, logger=logger)
    requests = open_run_requests(store, logger=logger)

    for app_command in active_commands:
        requested = requests.get(app_command.app_command_id)
        # A request is not a schedule. It deliberately overrides the allowed-hours window and the
        # repeat interval, because "run it now" is asked at the moment somebody needs the answer —
        # usually outside the window, usually right after the last run. What it does NOT override
        # is "already running": starting a second copy of an app that is mid-flight is how two
        # collectors write the same metric run.
        if app_command.app_command_id in running_commands:
            if requested is not None:
                log_app_event(
                    logger,
                    "app.daemon.command.request_waiting",
                    app_command=app_command,
                    status="skipped",
                    reason="already_running",
                )
            else:
                log_app_event(
                    logger,
                    "app.daemon.command.skip_running",
                    app_command=app_command,
                    status="skipped",
                    reason="already_running",
                    pid=running_commands[app_command.app_command_id].process.pid,
                )
            continue
        if requested is None:
            if not app_command_in_schedule_window(app_command):
                log_app_event(
                    logger,
                    "app.daemon.command.skip_window",
                    app_command=app_command,
                    status="skipped",
                    reason="outside_allowed_window",
                )
                continue
            latest = latest_runs.get(app_command.app_command_id)
            if not app_command_is_due(app_command, latest):
                _log_command_not_due(logger, app_command, latest)
                continue
            log_app_event(logger, "app.daemon.command.due", app_command=app_command, status="due")
        else:
            # Claim before starting, never after: the claim is the conditional UPDATE that makes
            # two daemons on one store unable to both act on the same request.
            requested = claim_run_request(store, app_command.app_command_id, logger=logger)
            if requested is None:
                continue
            log_app_event(
                logger,
                "app.daemon.command.requested",
                app_command=app_command,
                status="due",
                request_id=requested["request_id"],
                requested_by=requested["requested_by"],
            )
        try:
            start_app_command(
                config=config,
                store=store,
                data_dir=data_dir,
                logger=logger,
                running_commands=running_commands,
                app_command=app_command,
                forwarded_key_args=forwarded_key_args or ForwardedKeyArgs(),
                run_request=requested,
            )
        except Exception:
            if requested is not None:
                # The spawn failed before anything ran. Put the request back rather than leaving
                # it claimed, or the console shows "queued" forever with nothing happening.
                release_run_request(store, requested, logger=logger,
                                    note="Start failed; returned to the queue.")
            raise


#: The queue lives in its own store class; the daemon reaches it through these four helpers so the
#: scan reads as scheduling rather than as bookkeeping. Every one of them **fails open**: a queue
#: that cannot be read must never stop the scheduled work, which is the whole estate's monitoring.
def _run_request_store(store: Any) -> Any:
    from db_ops.db.run_requests import RunRequestStore

    return RunRequestStore(store.target)


def open_run_requests(store: Any, *, logger: Any = None) -> dict[str, Any]:
    """Pending "run now" requests, by app_command_id. Empty when the queue cannot be read."""
    try:
        requests = _run_request_store(store)
        return {code: row for code, row in requests.open_requests().items()
                if str(row["status"]) == "pending"}
    except Exception as exc:  # noqa: BLE001 - see the note above: the schedule must still run.
        if logger is not None:
            log_app_event(logger, "app.daemon.requests.unavailable", status="warning",
                          level="warning", error=str(exc)[:200])
        return {}


def claim_run_request(store: Any, app_command_id: str, *, logger: Any = None) -> Any | None:
    try:
        return _run_request_store(store).claim(app_command_id)
    except Exception as exc:  # noqa: BLE001
        if logger is not None:
            log_app_event(logger, "app.daemon.requests.claim_failed", status="warning",
                          level="warning", app_command_id=app_command_id, error=str(exc)[:200])
        return None


def mark_run_request_started(store: Any, request: Any, *, job_run_id: int,
                             logger: Any = None) -> None:
    try:
        _run_request_store(store).mark_started(int(request["request_id"]), job_run_id=job_run_id)
    except Exception as exc:  # noqa: BLE001 - the command is already running; this is only the note.
        if logger is not None:
            log_app_event(logger, "app.daemon.requests.mark_failed", status="warning",
                          level="warning", error=str(exc)[:200])


def finish_run_request(store: Any, request_id: int, *, run_status: str,
                       logger: Any = None) -> None:
    """Close the request whose run has just been reaped, so the console stops showing it queued."""
    try:
        _run_request_store(store).mark_done(
            int(request_id), note=f"Run finished with status {run_status}.")
    except Exception as exc:  # noqa: BLE001 - the run is already recorded; this is only the badge.
        if logger is not None:
            log_app_event(logger, "app.daemon.requests.finish_failed", status="warning",
                          level="warning", error=str(exc)[:200])


def sweep_run_requests(store: Any, *, logger: Any = None) -> None:
    """Expire what nobody picked up and close what has already finished.

    Run once per scan. Both cases exist because the daemon can be stopped at any moment: a request
    made while it was down must not fire when it returns, and one it started but never reaped must
    not stay on the dashboard forever.
    """
    try:
        requests = _run_request_store(store)
        requests.expire_stale()
        requests.close_finished()
    except Exception as exc:  # noqa: BLE001 - housekeeping must never cost the scheduled work.
        if logger is not None:
            log_app_event(logger, "app.daemon.requests.sweep_failed", status="warning",
                          level="warning", error=str(exc)[:200])


def release_run_request(store: Any, request: Any, *, note: str = "", logger: Any = None) -> None:
    try:
        _run_request_store(store).release(int(request["request_id"]), note=note)
    except Exception as exc:  # noqa: BLE001
        if logger is not None:
            log_app_event(logger, "app.daemon.requests.release_failed", status="warning",
                          level="warning", error=str(exc)[:200])


def collect_running_commands(
    *,
    store: DbOpsStore,
    logger: Any,
    running_commands: dict[str, RunningAppCommand],
) -> None:
    now = datetime.now(timezone.utc)
    for app_command_id, running in list(running_commands.items()):
        elapsed_ms = int((now - running.started_at).total_seconds() * 1000)
        process = running.process
        returncode = process.poll()
        # Still running: leave it alone unless it overran its timeout. timeout_disabled
        # (timeout == 0) marks a long-running service (e.g. the web host) that must never
        # be timeout-killed — only an actual process exit is acted on.
        if returncode is None and (
            running.app_command.timeout_disabled
            or elapsed_ms <= running.app_command.timeout_seconds * 1000
        ):
            continue
        if returncode is None:
            terminate_timed_out_command(running)
            status = "timeout"
            level = "error"
            error_text = f"Command timed out after {running.app_command.timeout_seconds} seconds."
            stdout_summary = read_process_output_summary(running.stdout_file)
            stderr_summary = read_process_output_summary(running.stderr_file)
            log_app_event(
                logger,
                "app.daemon.command.timeout",
                app_command=running.app_command,
                level=level,
                status=status,
                pid=process.pid,
                elapsed_ms=elapsed_ms,
            )
            write_command_runtime_event(
                running.app_command,
                "app.daemon.command.timeout",
                logs_dir=running.logs_dir,
                level=level,
                pid=process.pid,
                working_dir=running.working_dir,
                exit_code=process.returncode,
                duration_seconds=elapsed_ms / 1000,
                stdout_summary=stdout_summary,
                stderr_summary=stderr_summary,
                status=status,
            )
        else:
            status = "done" if returncode == 0 else "error"
            level = "logging" if status == "done" else "error"
            stdout_summary = read_process_output_summary(running.stdout_file)
            stderr_summary = read_process_output_summary(running.stderr_file)
            # The exit code alone says a run failed, never why. `job_runs.error_text` held only
            # "Command exited with return code 1", so diagnosing the PostgreSQL NUL-byte failure
            # meant reading source comments and correlating deploy timestamps — the traceback the
            # child had already printed was written to a log file and never linked to the run that
            # produced it. The tail of stderr is where a Python traceback ends, so it is the part
            # worth keeping on the row itself.
            error_text = _failed_run_error_text(returncode, stderr_summary, stdout_summary)                 if status == "error" else None
            log_app_event(
                logger,
                "app.daemon.command.done",
                app_command=running.app_command,
                level=level,
                status=status,
                pid=process.pid,
                elapsed_ms=elapsed_ms,
            )
            write_command_runtime_event(
                running.app_command,
                "app.daemon.command.done",
                logs_dir=running.logs_dir,
                level=level,
                pid=process.pid,
                working_dir=running.working_dir,
                exit_code=process.returncode,
                duration_seconds=elapsed_ms / 1000,
                stdout_summary=stdout_summary,
                stderr_summary=stderr_summary,
                status=status,
            )
        store.update_job_run(
            log_id=running.log_id,
            level=level,
            status=status,
            message=f"App command {running.app_command.app_command_id} finished with status {status}.",
            finished_at=utc_now_text(),
            duration_ms=elapsed_ms,
            error_text=error_text,
            metadata=app_command_metadata(running.app_command, pid=process.pid,
                                          returncode=process.returncode,
                                          **running.request_metadata),
        )
        if running.run_request_id is not None:
            finish_run_request(store, running.run_request_id, run_status=status, logger=logger)
        close_process_output_files(running)
        running_commands.pop(app_command_id, None)


def terminate_timed_out_command(running: RunningAppCommand) -> None:
    process = running.process
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def start_app_command(
    *,
    config: DbOpsConfig,
    store: DbOpsStore,
    data_dir: Path,
    logger: Any,
    running_commands: dict[str, RunningAppCommand],
    app_command: AppCommand,
    forwarded_key_args: ForwardedKeyArgs | None = None,
    run_request: Any | None = None,
) -> None:
    working_dir = resolve_working_dir(app_command.working_dir, data_dir=data_dir)
    started = datetime.now(timezone.utc)
    stdout_file = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
    stderr_file = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
    env = os.environ.copy()
    env[LOG_SCOPE_ENV_VAR] = app_command.log_scope
    if forwarded_key_args and forwarded_key_args.supplied:
        env.setdefault(SECRET_KEY_ENV_VAR, forwarded_secret_key(forwarded_key_args))
    command_text = append_forwarded_key_args(
        app_command.command_text,
        forwarded_key_args or ForwardedKeyArgs(),
    )
    command_text = use_this_interpreter(command_text)
    try:
        process = subprocess.Popen(
            command_text,
            cwd=working_dir,
            shell=True,
            text=True,
            stdout=stdout_file,
            stderr=stderr_file,
            env=env,
        )
    except Exception:
        close_file_quietly(stdout_file)
        close_file_quietly(stderr_file)
        write_command_runtime_event(
            app_command,
            "app.daemon.command.error",
            logs_dir=config.log_dir,
            level="error",
            working_dir=working_dir,
            exit_code="start_failed",
            duration_seconds=0,
            exception=traceback.format_exc(),
            status="error",
        )
        raise
    metadata = app_command_metadata(app_command, working_dir=working_dir, pid=process.pid, sqlite_path=config.sqlite_path)
    if run_request is not None:
        # Stamped into the run itself, so "why did this run at 03:00" is answerable from job_runs
        # alone — without it a requested run is indistinguishable from a scheduled one.
        metadata["run_request_id"] = int(run_request["request_id"])
        metadata["requested_by"] = str(run_request["requested_by"] or "")
    log_id = store.insert_job_run(
        JobRun(
            job_code=app_command.app_command_id,
            level="logging",
            status="running",
            message=(f"App command {app_command.app_command_id} started"
                     + (f" on request from {run_request['requested_by'] or 'the console'}."
                        if run_request is not None else ".")),
            started_at=started.strftime("%Y-%m-%dT%H:%M:%SZ"),
            host_name=socket.gethostname(),
            metadata=metadata,
        )
    )
    running_commands[app_command.app_command_id] = RunningAppCommand(
        app_command=app_command,
        process=process,
        started_at=started,
        log_id=log_id,
        working_dir=working_dir,
        logs_dir=config.log_dir,
        stdout_file=stdout_file,
        stderr_file=stderr_file,
        run_request_id=int(run_request["request_id"]) if run_request is not None else None,
        requested_by=str(run_request["requested_by"] or "") if run_request is not None else "",
    )
    if run_request is not None:
        mark_run_request_started(store, run_request, job_run_id=log_id, logger=logger)
    write_command_runtime_event(
        app_command,
        "app.daemon.command.start",
        logs_dir=config.log_dir,
        pid=process.pid,
        working_dir=working_dir,
        exit_code="running",
        duration_seconds=0,
        status="running",
    )
    log_app_event(
        logger,
        "app.daemon.command.start",
        app_command=app_command,
        status="running",
        pid=process.pid,
    )


def app_command_is_due(app_command: AppCommand, latest_run: Any | None, now: datetime | None = None) -> bool:
    """Thin wrapper over the shared :func:`job_due` so app_commands, metrics and reports
    all follow the same run-once / repeat / retry / stale-running convention."""
    if latest_run is None:
        return True
    last_run = row_time(latest_run)
    if last_run is None:
        return True
    try:
        status = str(latest_run["status"] or "").strip().lower()
    except (IndexError, KeyError, TypeError):
        status = ""
    return job_due(
        last_run=last_run,
        last_status=status,
        repeat_interval=app_command.repeat_interval_seconds,
        retry_interval=app_command.retry_interval_seconds,
        now=now or datetime.now(timezone.utc),
        timeout=app_command.timeout_seconds,
        timeout_disabled=app_command.timeout_disabled,
    )


def app_command_in_schedule_window(app_command: AppCommand, now_local: datetime | None = None) -> bool:
    current = now_local or datetime.now().astimezone()
    return is_time_window_open(app_command.time_window, current)


#: A stale RUNNING row older than this is history, not news. The daemon has restarted many times
#: since; nothing about it needs doing now, so reconciling it is bookkeeping and belongs in the log.
STALE_RECOVERY_ALERT_MAX_AGE_SECONDS = 24 * 3600


def _alert_stale_recovery(*, store: Any, fresh_crashes: list[str], backlog: int) -> None:
    """Alert about workflows that actually died recently; count the rest.

    The alert answers "did something crash", and only a recent row answers it. This used to push
    one Telegram message per reconciled row, which was survivable only while recovery could see a
    single row per job code — it silently skipped every older one. Fixing that made the sweep find
    the real backlog and alert 171 times in a burst, including rows from two weeks earlier, each
    formatted as a fresh incident.

    Two different things were being conflated, so they are separated here: a workflow that died in
    the last day is an incident worth a message; a pile of rows nobody ever closed is a number.
    An operator's phone is not a log file, and an alert channel that floods is one nobody reads
    the next day.
    """
    if not fresh_crashes:
        # Backlog only: nothing happened, the books were just balanced. The log has the detail.
        return
    shown = fresh_crashes[:10]
    lines = [
        "Stale running workflows recovered on startup.",
        f"recent_crashes={len(fresh_crashes)}",
        *shown,
    ]
    if len(fresh_crashes) > len(shown):
        lines.append(f"...and {len(fresh_crashes) - len(shown)} more")
    if backlog:
        lines.append(f"older_rows_reconciled_silently={backlog}")
    lines.append("action=marked_timeout_retry_will_resume")
    _push_daemon_telegram(
        store=store,
        level="error",
        message="\n".join(lines),
        metadata={"stale_recovery": True, "recent_crashes": len(fresh_crashes),
                  "backlog_rows": backlog},
    )


class _DaemonStopped(BaseException):
    """The daemon was asked to stop. Not an error, and deliberately not an ``Exception``.

    ``BaseException`` so the broad ``except Exception`` handlers scattered through the loop cannot
    swallow a shutdown and keep running - the same reason ``KeyboardInterrupt`` is one.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def install_shutdown_handlers() -> None:
    """Turn SIGTERM into something the loop can unwind from.

    ``docker stop`` sends SIGTERM and Python's default action is to die on the spot, so every
    deploy killed the daemon mid-run and left its app-command rows at ``running``. The next
    startup then found them, could not tell a deliberate stop from a crash, and alerted:
    "Stale running workflows recovered on startup. recent_crashes=2" - at error level, to the
    alert chat, on every single deploy. Eight of those went out on 2026-08-08 alone, none of
    them true.

    Handling the signal is what makes the *remaining* alerts mean something: after this, an open
    row at startup really is a crash or a SIGKILL, because a clean stop closes its own rows.
    """
    import signal

    def _stop(signum, _frame):
        raise _DaemonStopped(f"signal_{signum}")

    for name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, _stop)
            except (ValueError, OSError):
                # Not the main thread, or a platform without it: the daemon still runs, it just
                # goes back to being reported as a crash on the next start.
                pass


def close_running_on_shutdown(
    *,
    store: DbOpsStore,
    logger: Any,
    running_commands: dict[str, RunningAppCommand],
    reason: str,
) -> int:
    """Close the rows this daemon still owns, so the next startup has nothing to cry wolf about.

    Closed as ``timeout`` on purpose: :func:`job_due` resumes a run after ``retry_interval`` only
    for a status in :data:`ERROR_STATUSES`, so any friendlier word would read as "finished, never
    repeat" and the command would never come back. What changes is the *message*, which now says
    the daemon stopped rather than implying the command hung.

    Every failure is swallowed. A daemon that cannot tidy up must still exit.
    """
    closed = 0
    for app_command_id, running in list(running_commands.items()):
        try:
            store.update_job_run(
                log_id=running.log_id,
                level="logging",
                status="timeout",
                message=(f"App command {app_command_id} was interrupted: the daemon stopped "
                         f"({reason}). It resumes on the next scan."),
                finished_at=utc_now_text(),
                error_text=None,
                metadata=app_command_metadata(
                    running.app_command, shutdown=True, reason=reason,
                ),
            )
            closed += 1
        except Exception:  # noqa: BLE001 - tidying up must not stop the daemon exiting.
            continue
    if closed and logger:
        log_app_event(logger, "app.daemon.stop.closed_running", level="logging",
                      closed=closed, reason=reason)
    return closed


def recover_stale_running_jobs(
    *,
    store: DbOpsStore,
    app_commands: dict[str, AppCommand],
    config: Any,
    logger: Any,
) -> None:
    """
    On daemon startup, detect job_runs rows with status='running' that have elapsed longer
    than the command's configured timeout. Mark them 'timeout' to unblock retry logic.

    This handles: daemon crash, host reboot, SIGKILL — any scenario where the subprocess
    kept a 'running' row in SQLite but the daemon lost in-memory tracking of the process.

    **Every** open row is reconciled, not just the newest one per job code. Reading only the
    latest row meant a stale run that had already been overtaken by a newer one could never be
    closed again: it is no longer the latest for its job_code, but it is still open. They
    accumulated — 177 job_runs and 104 metric_runs were still RUNNING on the worker, the oldest
    from 2026-05-18 — which makes every "is anything running now" question unanswerable.

    A command with ``timeout_seconds <= 0`` is a **long-running service**, not a job: APP-WEBHOST
    serves HTTP until the daemon stops. Its row is still closed here — this runs at startup, when
    the daemon owns no children yet, so every open row belongs to a life that has ended — but it
    is closed **quietly**, because a service ending when its daemon ended is not a crash. The old
    code compared ``elapsed < 0``, which is never true for a service, so the web host was reported
    stale, logged as an error and alerted on Telegram at every single startup; simply skipping it
    instead leaked one unclosed row per restart.

    It is closed as ``timeout`` specifically: :func:`job_due` restarts a run-once entry whose last
    status is an error status after ``retry_interval``, which is 0 for the web host. Any other
    status would read as "finished successfully, never repeat" and the service would never come
    back up.
    """
    now = datetime.now(timezone.utc)
    recovered = 0
    fresh_crashes: list[str] = []
    backlog = 0
    for row in store.fetch_running_job_runs():
        try:
            app_command_id = str(row["job_code"] or "").strip()
            status = str(row["status"] or "").strip().lower()
        except Exception:
            continue
        if status != "running":
            continue
        app_command = app_commands.get(app_command_id)
        # A row whose command is gone from config cannot be timed out against anything, but it is
        # still open forever. Close it on the daemon's own sweep interval instead.
        if app_command is None:
            continue

        last_run = row_time(row)
        if last_run is None:
            continue
        elapsed_seconds = int((now - last_run).total_seconds())
        # A service has no timeout to be "within": its row is open because the daemon that owned
        # it is gone, whatever the elapsed time. Saying so explicitly rather than leaning on
        # `elapsed < 0` being false, which is true only by accident of the sentinel value.
        if not app_command.timeout_disabled and elapsed_seconds < app_command.timeout_seconds:
            log_app_event(
                logger,
                "app.daemon.startup.running_within_timeout",
                app_command=app_command,
                level="logging",
                started_at=last_run.isoformat(),
                elapsed_seconds=elapsed_seconds,
                timeout_seconds=app_command.timeout_seconds,
            )
            continue
        elapsed_minutes = elapsed_seconds // 60
        timeout_minutes = app_command.timeout_seconds // 60
        store.update_job_run(
            log_id=int(row["log_id"]),
            level="error",
            status="timeout",
            message=(
                f"App command {app_command_id} recovered from stale RUNNING state on daemon startup "
                f"after {elapsed_seconds}s (timeout={app_command.timeout_seconds}s)."
            ),
            finished_at=utc_now_text(),
            error_text=(
                f"Stale running job detected on startup. "
                f"elapsed={elapsed_seconds}s timeout={app_command.timeout_seconds}s. "
                f"Marked timeout to unblock retry."
            ),
            metadata=app_command_metadata(
                app_command,
                stale_recovery=True,
                elapsed_seconds=elapsed_seconds,
                timeout_seconds=app_command.timeout_seconds,
            ),
        )
        log_app_event(
            logger,
            "app.daemon.startup.stale_running_recovered",
            app_command=app_command,
            level="error",
            started_at=last_run.isoformat(),
            elapsed_seconds=elapsed_seconds,
            elapsed_minutes=elapsed_minutes,
            timeout_seconds=app_command.timeout_seconds,
            action="marked_timeout_retry_will_resume",
        )
        recovered += 1
        # A service's row is expected to be open at every startup: that is what "runs until the
        # daemon stops" means. Closing it is bookkeeping; alerting on it is crying wolf.
        if app_command.timeout_disabled:
            backlog += 1
        elif elapsed_seconds <= STALE_RECOVERY_ALERT_MAX_AGE_SECONDS:
            fresh_crashes.append(
                f"{app_command_id} started {last_run.isoformat()} ({elapsed_minutes} min ago)")
        else:
            backlog += 1
    if recovered:
        log_app_event(
            logger, "app.daemon.startup.stale_recovery_complete", level="logging",
            recovered=recovered, fresh=len(fresh_crashes), backlog=backlog,
        )
        _alert_stale_recovery(store=store, fresh_crashes=fresh_crashes, backlog=backlog)


def _log_command_not_due(logger: Any, app_command: AppCommand, latest: Any | None) -> None:
    """Log diagnostics explaining why an app command is not scheduled this tick."""
    if logger is None:
        return
    now = datetime.now(timezone.utc)
    last_run = row_time(latest)
    try:
        status = str(latest["status"] or "").strip().lower() if latest is not None else "never_run"
    except Exception:
        status = "unknown"
    next_retry_at = None
    if last_run is not None:
        if status == "running":
            next_retry_at = last_run + timedelta(seconds=app_command.timeout_seconds)
        elif status in {"error", "timeout", "fail", "failed", "failure"}:
            next_retry_at = last_run + timedelta(seconds=app_command.retry_interval_seconds)
    log_app_event(
        logger,
        "app.daemon.command.not_due",
        app_command=app_command,
        level="logging",
        workflow_state=status,
        last_run=last_run.isoformat() if last_run else None,
        next_retry_at=next_retry_at.isoformat() if next_retry_at else None,
        retry_interval_seconds=app_command.retry_interval_seconds,
        timeout_seconds=app_command.timeout_seconds,
    )


def _push_daemon_telegram(
    *,
    store: Any,
    level: str,
    message: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Queue a daemon notification, if the shared router says this level may alert.

    No ``config``: the daemon holds no Telegram settings of its own — taking one here is what
    let it drift (it gated on ``alert_levels`` while every other app asked the router).
    """
    try:
        # Routing is the Telegram app's answer, not this one's, and `alert` already means
        # "Telegram is on AND this level may alert AND it has a chat" - act on that flag rather
        # than re-deriving it from config.telegram, which is how the daemon used to drift from
        # every other app.
        from db_ops.lib.notify_route import chat_from_route
        from db_ops.lib.telegram_route import telegram_route

        chat_id = chat_from_route(telegram_route(level))
        if not chat_id:
            return
        text = f"{level.upper()}|{socket.gethostname()}|{message}"
        if len(text) > 3900:
            text = text[:3897] + "..."
        from db_ops.db.queue_message import queue_message, store_block_from

        # The store states its own connection: this helper deliberately takes no config, so the
        # live store is the only thing that can say where the row goes.
        queue_message({
            "store": store_block_from(store),
            "chat_id": chat_id,
            "text": text,
            "level": level,
            "note": "Daemon stale recovery notification",
            "source_type": "daemon_events",
            "source_id": "stale_recovery",
            "metadata": metadata or {},
        }, fallback_store=store)
    except Exception as exc:
        print(f"daemon telegram push failed: {exc}", file=sys.stderr)


def row_time(row: Any | None) -> datetime | None:
    if row is None:
        return None
    value = row["started_at"] or row["created_at"] or row["finished_at"]
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def resolve_working_dir(value: str, *, data_dir: Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path

    # "tools/db_ops" is the logical alias for the tool root, independent of where the
    # project physically lives (a flat dev checkout, or /app/tools/db_ops in the image).
    if value.replace("\\", "/") == "tools/db_ops":
        return TOOL_ROOT

    for candidate in (REPO_ROOT / path, TOOL_ROOT / path, data_dir / path):
        resolved = candidate.resolve()
        if resolved.exists():
            return resolved
    raise FileNotFoundError(f"working_dir not found: {value}")


def build_forwarded_key_args(args: argparse.Namespace) -> ForwardedKeyArgs:
    if getattr(args, "key", None) and getattr(args, "key_base64", None):
        raise RuntimeError("Use only one of --key or --key-base64.")
    if getattr(args, "key_base64", None):
        return ForwardedKeyArgs("--key-base64", str(args.key_base64))
    if getattr(args, "key", None):
        return ForwardedKeyArgs("--key", str(args.key))
    return ForwardedKeyArgs()


def forwarded_secret_key(forwarded_key_args: ForwardedKeyArgs) -> str:
    if not forwarded_key_args.supplied:
        return ""
    if forwarded_key_args.option in {"--key-base64", "--key_base64"}:
        return resolve_cli_key(None, forwarded_key_args.value)
    if forwarded_key_args.option == "--key":
        return resolve_cli_key(forwarded_key_args.value, None)
    return ""


def ensure_forwarded_secret_key_env(forwarded_key_args: ForwardedKeyArgs) -> None:
    if not forwarded_key_args.supplied:
        return
    secret_key = forwarded_secret_key(forwarded_key_args)
    if secret_key:
        os.environ.setdefault(SECRET_KEY_ENV_VAR, secret_key)


#: Words a command may start with that mean "the Python I am running under".
_INTERPRETER_WORDS = ("python", "python3", "python.exe", "python3.exe")


def use_this_interpreter(command_text: str) -> str:
    """Rewrite a leading bare ``python`` to the interpreter this daemon is running under.

    Every scheduled command in `data/app_commands.example.json` begins ``python -m db_ops...``,
    which resolves through `PATH` — and `PATH` is not where the toolkit is installed. After
    `pip install dbabrain` into a virtualenv (the documented way), `db-ops daemon` starts from the
    venv while its children get whatever `python` means on that machine: a system Python without
    the package, or another project's venv entirely.

    The symptom is the worst kind. Every command a reader ran by hand works; the moment the daemon
    runs the same commands they all fail with ``ModuleNotFoundError: No module named 'db_ops'``,
    once a minute, in a child process whose output nobody is watching. Measured on 2026-08-23 in a
    clean `pip install`: three scheduled commands, three failures, and a manual run of each that
    succeeded.

    `sys.executable` is the answer to the question the config was really asking. A command naming
    a *specific* interpreter — an absolute path, or a different runtime — is left exactly as
    written, because that is someone being deliberate.
    """
    stripped = command_text.lstrip()
    if not stripped:
        return command_text
    try:
        first = shlex.split(stripped, posix=True)[0]
    except ValueError:  # unbalanced quotes; not ours to repair
        return command_text
    if first.lower() not in _INTERPRETER_WORDS:
        return command_text
    leading = command_text[: len(command_text) - len(stripped)]
    # These run through `shell=True`, so the quoting has to match the shell that will parse it.
    # `shlex.quote` emits single quotes, which cmd.exe does not treat as quoting at all — it would
    # hand the child a path with literal apostrophes around it. Only bites when the interpreter
    # path contains a space, which is exactly where `C:\Program Files\...` lives.
    quoted = f'"{sys.executable}"' if os.name == "nt" else shlex.quote(sys.executable)
    return leading + quoted + stripped[len(first):]


def append_forwarded_key_args(command_text: str, forwarded_key_args: ForwardedKeyArgs) -> str:
    if not forwarded_key_args.supplied:
        return command_text
    # Only forward to CLIs that actually declare --key/--key-base64. Injecting the key into
    # a CLI that lacks the option (reports, sla) makes argparse read the key VALUE
    # as the positional subcommand and abort with exit code 2.
    if not command_accepts_forwarded_key(command_text):
        return command_text
    tokens = shlex.split(command_text, posix=True)
    if any(token in {"--key", "--key-base64", "--key_base64"} for token in tokens):
        return command_text
    insert_at = forwarded_key_insert_index(tokens)
    updated = [
        *tokens[:insert_at],
        forwarded_key_args.option,
        forwarded_key_args.value,
        *tokens[insert_at:],
    ]
    return " ".join(shell_quote_arg(token) for token in updated)


def command_accepts_forwarded_key(command_text: str) -> bool:
    """True only when the target CLI declares the shared --key/--key-base64 options.

    Every key-aware db_ops CLI registers them through ``secret_text.add_key_argument``;
    the CLIs that don't (reports, sla) must never receive the key, otherwise argparse
    consumes the value as the positional subcommand and exits with code 2.

    This is read from the target's **source**, never from a list, which is why `webhost`
    started receiving the key by itself the moment its console needed the store: declaring the
    option is the whole registration. A hand-kept list would have needed editing in the same
    pass, and the failure for forgetting is a login page that cannot reach its own accounts.
    """
    source = forwarded_key_target_source(command_text)
    return source is not None and "add_key_argument" in source


def forwarded_key_target_source(command_text: str) -> str | None:
    """Return the source text of the ``python -m db_ops.<...>`` module a command runs,
    or None when the command is not a recognizable ``-m db_ops`` invocation. The module
    file is read from disk only — it is never imported, so this has no side effects."""
    try:
        tokens = shlex.split(command_text, posix=True)
    except ValueError:
        return None
    if len(tokens) < 3 or tokens[1] != "-m":
        return None
    module = tokens[2]
    if not module.startswith("db_ops."):
        return None
    module_path = TOOL_ROOT.joinpath(*module.split("."))
    for candidate in (module_path.with_suffix(".py"), module_path / "__main__.py"):
        if candidate.is_file():
            try:
                return candidate.read_text(encoding="utf-8")
            except OSError:
                return None
    return None


def forwarded_key_insert_index(tokens: list[str]) -> int:
    if len(tokens) < 3 or tokens[1] != "-m":
        return len(tokens)
    index = 3
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            return index
        if not token.startswith("-"):
            return index
        index += 1
        if "=" not in token and index < len(tokens) and not tokens[index].startswith("-"):
            index += 1
    return index


def shell_quote_arg(value: str) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline([value])
    return shlex.quote(value)


def load_app_commands(path: Path, *, logger: Any = None) -> dict[str, AppCommand]:
    data = load_json_file(path)
    commands: list[AppCommand] = []
    for item in data.get("app_commands", []):
        app_command_id = str(item["app_command_id"])
        parsed_time_window = parse_time_window_config(
            item,
            context=f"app_commands.{app_command_id}",
            defaults={
                "from_day": 1,
                "to_day": 31,
                "from_hour": 0,
                "to_hour": 23,
                "retry_interval": 60,
                "timeout": 300,
            },
        )
        log_deprecated_time_window_warnings(logger, parsed_time_window.warnings)
        commands.append(
            AppCommand(
                app_command_id=app_command_id,
                app_code=str(item.get("app_code") or item["app_command_id"]),
                app_name=str(item.get("app_name") or item.get("app_code") or item["app_command_id"]),
                display_name=str(item.get("display_name") or item.get("command_name") or item.get("app_name") or item["app_command_id"]),
                log_scope=validate_app_command_log_scope(item),
                working_dir=str(item.get("working_dir", "")),
                command_text=str(item.get("command_text", "")),
                time_window=parsed_time_window.time_window,
                active=bool(item.get("active", True)),
                node_role=(str(item.get("node_role", "all")).strip().lower() or "all"),
            )
        )
    return {command.app_command_id: command for command in commands}


def log_deprecated_time_window_warnings(logger: Any, warnings: tuple[str, ...]) -> None:
    if logger is None:
        return
    for message in warnings:
        log_event(logger, level="warning", message=f"app.daemon.config.deprecated_time_window|scope=jobs|message={format_log_value(message)}")


def validate_app_command_log_scope(item: dict[str, Any]) -> str:
    try:
        return validate_log_scope(str(item.get("log_scope") or ""))
    except RuntimeError as exc:
        app_command_id = str(item.get("app_command_id", "<missing>"))
        raise RuntimeError(f"app_commands.json app_command_id={app_command_id} missing required log_scope.") from exc


def app_command_metadata(app_command: AppCommand, **extra: Any) -> dict[str, Any]:
    metadata = {
        "scope": app_command.log_scope,
        "app_command_id": app_command.app_command_id,
        "app_code": app_command.app_code,
        "app_name": app_command.app_name,
        "display_name": app_command.display_name,
        "command_text": app_command.command_text,
        "retry_interval_seconds": app_command.retry_interval_seconds,
        "timeout_seconds": app_command.timeout_seconds,
    }
    metadata.update({key: str(value) if isinstance(value, Path) else value for key, value in extra.items()})
    return metadata


def write_command_runtime_event(
    app_command: AppCommand,
    event_name: str,
    *,
    logs_dir: Path,
    level: str = "logging",
    **fields: Any,
) -> None:
    _main_log_path, runtime_log_path = build_log_paths(logs_dir, app_command.log_scope)
    archive_yesterday_if_missing(runtime_log_path)
    ensure_current_log_file(runtime_log_path)
    event_fields = {
        "scope": app_command.log_scope,
        "app_command_id": app_command.app_command_id,
        "app_name": app_command.app_name,
        "display_name": app_command.display_name,
        "command_text": app_command.command_text,
        "timeout_seconds": app_command.timeout_seconds,
        **fields,
    }
    timestamp = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
    hostname = socket.gethostname()
    parts = [event_name]
    for key, value in event_fields.items():
        if value is None:
            continue
        parts.append(f"{key}={format_log_value(value)}")
    with runtime_log_path.open("a", encoding="utf-8") as file:
        file.write(f"{timestamp}|{level.upper()}|{app_command.log_scope}|{hostname}|{'|'.join(parts)}\n")


#: How much of a failed child's output to keep on its job_runs row. Enough for a Python
#: traceback's final frames and its exception line; not so much that the column becomes a log.
FAILED_RUN_ERROR_TEXT_CHARS = 2000


def _failed_run_error_text(returncode: int | None, stderr_summary: str, stdout_summary: str) -> str:
    """Exit code plus the tail of what the child actually said before dying.

    The tail, not the head: a traceback ends with the exception, and a command that logged 300
    lines before failing has its cause in the last few. Falls back to stdout because several
    db_ops CLIs print their failure there.
    """
    detail = (stderr_summary or "").strip() or (stdout_summary or "").strip()
    text = f"Command exited with return code {returncode}."
    if detail:
        tail = detail[-FAILED_RUN_ERROR_TEXT_CHARS:]
        if len(detail) > FAILED_RUN_ERROR_TEXT_CHARS:
            tail = f"...[{len(detail) - FAILED_RUN_ERROR_TEXT_CHARS} earlier chars omitted]...{tail}"
        text = f"{text}\n{tail}"
    return text


def read_process_output_summary(output_file: IO[str]) -> str:
    try:
        output_file.flush()
        output_file.seek(0)
        return trim_output(output_file.read())
    except Exception as exc:  # noqa: BLE001 - summary logging must not crash collection.
        return f"<failed to read output summary: {exc}>"


def close_process_output_files(running: RunningAppCommand) -> None:
    close_file_quietly(running.stdout_file)
    close_file_quietly(running.stderr_file)


def close_file_quietly(file_obj: IO[str]) -> None:
    try:
        file_obj.close()
    except Exception:
        return


def log_app_event(logger: Any, event_name: str, *, level: str = "logging", app_command: AppCommand | None = None, **fields: Any) -> None:
    if logger is None:
        return
    parts = [event_name]
    if app_command is not None:
        fields = {
            "scope": app_command.log_scope,
            "app_command_id": app_command.app_command_id,
            "app_name": app_command.app_name,
            "display_name": app_command.display_name,
            **fields,
        }
    for key, value in fields.items():
        if value is None:
            continue
        parts.append(f"{key}={format_log_value(value)}")
    log_event(logger, level=level, message="|".join(parts))




def trim_output(value: str | None) -> str:
    text = value or ""
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[-MAX_OUTPUT_CHARS:]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
