"""The Telegram support commands: a command run in the background - started, watched, and judged complete from what it left behind.

Split out of ``telegram/command_processor.py`` on 2026-10-03 (Q11,
``audits/20261002_audit_typed_requests_and_errors.md`` section 5). ``command_processor`` re-exports every
name, so no import changes.
"""

from __future__ import annotations
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from db_ops.lib.secret_text import SECRET_KEY_ENV_VAR
from db_ops.lib.common_cli import common_invocation
from db_ops.lib.process_liveness import (  # noqa: F401 - re-exported, see above
    is_pid_alive as _is_pid_alive,
    is_windows_pid_alive as _is_windows_pid_alive,
    is_zombie as _is_zombie,
    process_start_marker,
    stop_process_tree,
)
from db_ops.lib.telegram_command_text import (  # noqa: F401 - re-exported, see above
    command_key_from_message,
    first_command_token,
    normalize_command_text,
    parse_command_message,
    split_with_verbatim_tail,
    render_command_line,
    split_command_tokens,
    strip_bot_username,
)
from db_ops.db.queue_message import queue_message, store_block_from
from db_ops.db import DbOpsStore
from db_ops.lib.paths import DEFAULT_DATA_DIR, REPO_ROOT, TOOL_ROOT  # noqa: F401 - one definition, see that module
from db_ops.telegram.detached_exit import ARGV_SEPARATOR as DETACHED_ARGV_SEPARATOR
from db_ops.telegram.command_base import SupportCommand, TelegramCommandError, _dispatch_log, _dispatch_logger, _safe_values_text, mask_sensitive_text, mask_sensitive_value
from db_ops.telegram.command_permissions import _resolve_node_role
from db_ops.telegram.command_replies import queue_command_reply
from db_ops.telegram.command_cli import _extract_error_from_output, build_cli_argv, command_env, finish_common_request, finishes, parse_json_from_output, render_template, resolve_working_dir, safe_error_summary


def execute_cli_background_command(
    *,
    store: DbOpsStore,
    row: Any,
    command: SupportCommand,
    values: dict[str, Any],
    source_id: str,
) -> dict[str, Any]:
    config = dict(command.action_config or {})
    config_path = str(values.get("config_path") or "")
    dlog = _dispatch_logger(config_path)
    restore_id = str(values.get("restore_id") or "")
    point_in_time = str(values.get("point_in_time") or "")
    node_role = _resolve_node_role(config_path)

    _dispatch_log(
        dlog,
        f"dispatch command_received command={command.command_text} command_id={command.command_id} "
        f"source_id={source_id} node_role={node_role} restore_id={restore_id} point_in_time={point_in_time}",
    )
    _dispatch_log(dlog, f"dispatch parsed_args {_safe_values_text(values)}")

    # Key resolution: report presence only, NEVER the value. Restore needs the secret key
    # to decrypt SMB/SQL credentials, so a missing key must fail loudly here rather than
    # let the detached workflow stop silently after "started".
    key_present = bool(os.environ.get(SECRET_KEY_ENV_VAR, "").strip())
    _dispatch_log(dlog, f"dispatch key_resolution env_var={SECRET_KEY_ENV_VAR} present={key_present}")
    if bool(config.get("requires_secret_key")) and not key_present:
        message = (
            f"Cannot start {command.command_text}: the secret key is not available on this worker. "
            f"Start the worker with --key/--key-base64 (or set {SECRET_KEY_ENV_VAR}) so backup "
            "credentials can be decrypted, then retry."
        )
        _dispatch_log(
            dlog,
            f"dispatch key_missing command={command.command_text} reason=secret_key_not_available",
            level="critical",
        )
        queue_command_reply(
            store=store, row=row, command=command, message_text=message, source_id=source_id, status="failed",
        )
        return {"_queued_reply_count": 1, "status": "FAILED_NO_KEY"}

    argv = build_cli_argv(config, values)
    # A `common.cli` action is finished here (rules R09) and its request sent on the child's stdin:
    # it now carries a password, and an argument is readable by every process on the host.
    stdin_payload: bytes | None = None
    invocation = common_invocation(argv)
    if invocation is not None and finishes(invocation.command):
        try:
            finished = finish_common_request(invocation.command, invocation.request, store=store)
        except TelegramCommandError as exc:
            _dispatch_log(dlog, f"dispatch request_unfinished command={command.command_text} "
                                f"error={safe_error_summary(exc)}", level="critical")
            queue_command_reply(store=store, row=row, command=command, message_text=str(exc),
                                source_id=source_id, status="failed")
            return {"_queued_reply_count": 1, "status": "FAILED_REQUEST"}
        argv = [*argv[:invocation.request_index], "-", *argv[invocation.request_index + 1:]]
        stdin_payload = json.dumps(finished, ensure_ascii=False).encode("utf-8")
    working_dir = resolve_working_dir(str(config.get("working_dir") or "tools/db_ops"))
    timeout_seconds = int(config.get("timeout_seconds") or 1800)
    _dispatch_log(
        dlog,
        f"dispatch worker_selection node_role={node_role} working_dir={working_dir} timeout_seconds={timeout_seconds}",
    )
    _dispatch_log(dlog, f"dispatch generated_command_line argv={mask_sensitive_value(argv)}")

    # Carry the resolved secret key (and the rest of the environment) into the child
    # explicitly, so decryption never silently depends on implicit inheritance. Secret
    # parameters ride in here too, never in argv (see command_env).
    child_env = command_env(config, values)

    # Queue the "started" reply first so the user always gets it; an immediate crash then
    # appends a failure reply rather than leaving the workflow appearing to stop silently.
    start_text = str(config.get("start_text") or "Command started.")
    queue_command_reply(
        store=store,
        row=row,
        command=command,
        message_text=render_template(start_text, values),
        source_id=source_id,
        status="started",
    )
    queued_reply_count = 1

    stdout_fd, stdout_path = tempfile.mkstemp(suffix=".cli.stdout.txt")
    stderr_fd, stderr_path = tempfile.mkstemp(suffix=".cli.stderr.txt")
    exit_code_path = _exit_code_path(stdout_path)
    try:
        popen_kwargs: dict[str, Any] = {
            "cwd": working_dir,
            "stdout": os.fdopen(stdout_fd, "w", encoding="utf-8", errors="replace"),
            "stderr": os.fdopen(stderr_fd, "w", encoding="utf-8", errors="replace"),
            "text": False,
            "shell": False,
            "env": child_env,
        }
        # A detached process is not a child of the workflow that later polls it, so **neither**
        # platform can read its exit status back: `waitpid` only works for children on POSIX,
        # and on Windows the PID is released the moment the process exits, so `OpenProcess`
        # finds nothing. Without a recorded code the poller has to guess from the output, and a
        # command that says nothing machine-readable (`create-db-docker` prints a human summary)
        # is reported as FAILED even when it succeeded.
        #
        # POSIX recorded its own code from the start, through `sh -c`. Windows did not, and the
        # gap was filled by returning a hardcoded 1 — which reported every completed detached
        # task as failed. See `db_ops.telegram.detached_exit`.
        #
        # One wrapper now, for both, and in Python rather than a shell: an argument list needs
        # no quoting, and building a `cmd.exe` command line is the defect this repository met
        # twice in one day.
        launch_argv = [sys.executable, "-m", "db_ops.telegram.detached_exit",
                       exit_code_path, DETACHED_ARGV_SEPARATOR, *argv]
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        if stdin_payload is not None:
            popen_kwargs["stdin"] = subprocess.PIPE
        popen = subprocess.Popen(launch_argv, **popen_kwargs)  # noqa: S603
        # Read at once, while the process is certainly ours: the poller compares it before it
        # trusts or kills this PID (review 0.25.0, B1.6).
        started_marker = process_start_marker(popen.pid)
        popen_kwargs["stdout"].close()
        popen_kwargs["stderr"].close()
        if stdin_payload is not None and popen.stdin is not None:
            # Written and closed at once: the wrapper hands its stdin to the command, which reads
            # the whole request before it does anything, so nothing here waits on the child.
            popen.stdin.write(stdin_payload)
            popen.stdin.close()
    except Exception as exc:
        try:
            os.close(stdout_fd)
        except OSError:
            pass
        try:
            os.close(stderr_fd)
        except OSError:
            pass
        Path(stdout_path).unlink(missing_ok=True)
        Path(stderr_path).unlink(missing_ok=True)
        error_summary = safe_error_summary(exc)
        _dispatch_log(
            dlog,
            f"dispatch subprocess_start_failed command={command.command_text} error={error_summary}",
            level="critical",
        )
        failure_text = str(
            config.get("failure_text") or "Command failed.\nExit code: {exit_code}\nError: {error_summary}"
        )
        queue_command_reply(
            store=store,
            row=row,
            command=command,
            message_text=render_template(failure_text, values | {"exit_code": 1, "error_summary": error_summary}),
            source_id=source_id,
            status="failed",
        )
        return {"_queued_reply_count": queued_reply_count + 1, "status": "FAILED_START"}

    _dispatch_log(dlog, f"dispatch subprocess_started pid={popen.pid} command={command.command_text}")

    # Detect an immediate crash (bad argv, import error, early validation failure) so the
    # user gets a real error instead of the workflow appearing to stop after "started".
    grace_seconds = int(config.get("startup_grace_seconds") or 3)
    try:
        early_rc: int | None = popen.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        early_rc = None
    if early_rc is not None and early_rc != 0:
        stderr_text = _read_file_safe(stderr_path)
        stdout_text = _read_file_safe(stdout_path)
        error_detail = _extract_error_from_output(stderr_text, stdout_text)
        _dispatch_log(
            dlog,
            f"dispatch subprocess_exited_early pid={popen.pid} exit_code={early_rc} "
            f"error={_format_dispatch_value(error_detail)}",
            level="critical",
        )
        failure_text = str(
            config.get("failure_text") or "Command failed.\nExit code: {exit_code}\nError: {error_summary}"
        )
        queue_command_reply(
            store=store,
            row=row,
            command=command,
            message_text=render_template(
                failure_text, values | {"exit_code": early_rc, "error_summary": error_detail}
            ),
            source_id=source_id,
            status="failed",
        )
        _remove_file_safe(stdout_path)
        _remove_file_safe(stderr_path)
        return {"_queued_reply_count": queued_reply_count + 1, "pid": popen.pid, "exit_code": early_rc, "status": "FAILED_EARLY"}

    # Still running (normal long restore) or already finished cleanly: hand off to the
    # background-task poller, which reports the final success/failure on a later cycle.
    store.insert_telegram_background_task(
        chat_id=str(row["chat_id"]),
        message_id=int(row["message_id"]) if row["message_id"] is not None else None,
        user_id=str(row["user_id"] or ""),
        command_id=command.command_id,
        command_text=command.command_text,
        source_id=source_id,
        pid=popen.pid,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        task_data={
            "timeout_seconds": timeout_seconds,
            "pid_started": started_marker,
            "values": mask_sensitive_value(values),
            "success_text": str(config.get("success_text") or "Command completed."),
            "failure_text": str(
                config.get("failure_text")
                or "Command failed.\nExit code: {exit_code}\nError: {error_summary}"
            ),
            "timeout_text": str(
                config.get("timeout_text")
                or "Command timed out after {timeout_seconds} seconds."
            ),
            "success_output_contains": str(config.get("success_output_contains") or ""),
            "completion_probe": _render_completion_probe(config.get("completion_probe"), values),
        },
    )
    _dispatch_log(
        dlog,
        f"dispatch subprocess_tracking pid={popen.pid} command={command.command_text} "
        f"state={'finished_fast' if early_rc == 0 else 'running'}",
    )
    return {"_queued_reply_count": queued_reply_count, "pid": popen.pid}


def _format_dispatch_value(value: object) -> str:
    return str(value).replace("\r", " ").replace("\n", " ")


def _tail_text(text: str, *, max_chars: int = 500) -> str:
    safe = mask_sensitive_text(str(text or "")).strip()
    return safe[-max_chars:] if len(safe) > max_chars else safe


def _render_completion_probe(probe: Any, values: dict[str, Any]) -> dict[str, Any] | None:
    """Render a command's ``completion_probe`` config with the run's values (so
    ``match_metadata`` placeholders like ``{restore_id}`` become concrete) for storage
    in the background task."""
    if not isinstance(probe, dict):
        return None
    rendered = dict(probe)
    match_metadata = probe.get("match_metadata")
    if isinstance(match_metadata, dict):
        rendered["match_metadata"] = {
            str(k): render_template(str(v), values) for k, v in match_metadata.items()
        }
    return rendered


def _job_run_metadata_matches(metadata_json: Any, match_metadata: dict[str, Any]) -> bool:
    if not match_metadata:
        return True
    try:
        meta = json.loads(str(metadata_json or "{}"))
    except (ValueError, TypeError):
        return False
    if not isinstance(meta, dict):
        return False
    return all(str(meta.get(key, "")) == str(value) for key, value in match_metadata.items())


def _probe_completion(store: Any, probe: Any, *, since_created_at: str) -> tuple[str, str] | None:
    """Authoritative completion of a background command from SQLite ``job_runs``.

    Returns ``("success"|"failure", message)`` when a terminal job-run record (matching
    the probe's success/failure ``job_code`` and ``match_metadata``, created at/after the
    task start) exists, else ``None``. The newest matching record wins, so a re-run is
    reflected. This lets the poller report the real outcome even if the detached process
    lingers past the timeout (e.g. a container-side restore that finishes async)."""
    if not isinstance(probe, dict):
        return None
    success_code = str(probe.get("success_job_code") or "")
    failure_code = str(probe.get("failure_job_code") or "")
    if not (success_code or failure_code):
        return None
    match_metadata = probe.get("match_metadata") or {}
    try:
        rows = store.fetch_terminal_job_runs(
            job_codes=[success_code, failure_code], since_created_at=since_created_at
        )
    except Exception:  # noqa: BLE001 - probe is best-effort; fall back to process/marker.
        return None
    for row in rows:
        if not _job_run_metadata_matches(row["metadata_json"], match_metadata):
            continue
        code = str(row["job_code"] or "")
        message = str(row["error_text"] or row["message"] or "")
        if code == failure_code:
            return "failure", message
        if code == success_code:
            return "success", message
    return None


def completion_verdict(
    *,
    exit_code: int | None,
    status_str: str,
    marker_found: bool,
    timed_out: bool,
    expects_evidence: bool,
) -> bool:
    """Did a finished detached command succeed, judged from what it left behind?

    Three answers, not two: a recorded non-zero exit code settles failure, a recorded zero
    settles success, and *nothing recorded* is neither. Which way "nothing" falls depends on
    whether the command was asked to leave evidence - and both readings have already shipped
    as bugs:

    - Read as failure, a successful `/spbot_run_sql_task 24` was reported as `Exit code: 1`
      on 2026-08-26; nothing had gone wrong except that the poller could no longer open the
      finished process's handle. A command that states no completion contract still has to be
      given the benefit of the doubt.
    - Read as success, a restore killed by a daemon restart 14 minutes into a 33 GB copy was
      reported as `Restore workflow completed` on 2026-09-15. It had written neither its
      `.end` job run nor its success marker nor an exit code - the whole contract it declares
      was silent, and silence was read as a green tick on a restore that never ran.

    So: when the command declares evidence (a `completion_probe` or a `success_output_contains`
    marker) and every channel came back empty, the run is *not* reported as done. When it
    declares none, unknown stays "finished rather than accused".
    """
    if timed_out:
        return False
    if exit_code is not None:
        return exit_code == 0
    if status_str in ("SUCCESS", "OK") or marker_found:
        return True
    return not expects_evidence


def _is_our_process(pid: int, recorded: object) -> bool:
    """Is the process holding ``pid`` the one this task started? A row from before the start time
    was recorded cannot be checked and is believed, as it always was."""
    if not recorded:
        return True
    return process_start_marker(pid) == str(recorded)


def _stop_task_tree(pid: int, recorded: object = None) -> None:
    """Stop a background task: the wrapper AND the command it runs.

    The wrapper (`detached_exit`) runs the real command as its child and waits for it. Killing the
    wrapper's PID alone left the command running while the chat was told it had timed out (review
    0.25.0, B1.6). The wrapper is started in a session (POSIX) or a process group (Windows) of its
    own, so the whole tree can be stopped and nothing outside it is touched.

    A row from before the start time was recorded has no marker, so its tree cannot be told from
    a stranger's: it gets what it always got, the wrapper's PID alone.
    """
    if recorded:
        stop_process_tree(pid, started=str(recorded))
        return
    try:
        os.kill(pid, 9 if sys.platform != "win32" else 1)
    except OSError:
        pass


def check_cli_background_tasks(*, sqlite_path: str | Path) -> dict[str, int]:
    store = DbOpsStore(sqlite_path)
    tasks = store.fetch_running_telegram_background_tasks()
    counts = {"checked": len(tasks), "completed": 0, "timed_out": 0, "queued_reply": 0}

    for task in tasks:
        task_data = json.loads(str(task["task_data"] or "{}"))
        pid = int(task["pid"])
        timeout_seconds = int(task_data.get("timeout_seconds") or 1800)
        values = dict(task_data.get("values") or {})
        created_at_str = str(task["created_at"] or "")

        # Three questions before the PID is believed (review 0.25.0, B1.6). Has the wrapper written
        # its exit code? Then it is finished, whatever holds the PID now - the file is written
        # last. Is the process holding the PID the one that was started? A PID is reused once its
        # process ends, and the poller used to wait out the timeout on a stranger and then kill it.
        # Only then: is it alive?
        finished = Path(_exit_code_path(str(task["stdout_path"] or ""))).is_file()
        ours = _is_our_process(pid, task_data.get("pid_started"))
        alive = (not finished) and ours and _is_pid_alive(pid)

        # SQLite is the authoritative completion source for jobs that log a terminal
        # record: report the real outcome even if the detached process lingers past the
        # timeout (e.g. a container-side restore that finishes async, so the workflow
        # process is still alive at the timeout although the restore already succeeded).
        probe_result = _probe_completion(
            store, task_data.get("completion_probe"), since_created_at=created_at_str
        )

        # Check for timeout even if process appears alive (only when SQLite has no verdict yet).
        timed_out = False
        if probe_result is not None:
            if alive:
                _stop_task_tree(pid, task_data.get("pid_started"))
                alive = False
        elif alive and created_at_str:
            try:
                created_dt = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
                age_seconds = (datetime.now(timezone.utc) - created_dt).total_seconds()
                if age_seconds > timeout_seconds:
                    timed_out = True
                    alive = False
                    _stop_task_tree(pid, task_data.get("pid_started"))
            except (ValueError, OSError):
                pass

        if probe_result is None and alive:
            continue

        # The recorded code, on both platforms — the child writes it as its last act, so a file
        # that exists is a finished process. `None` means "not recorded", which is unknown and
        # must never be read as failure: that reading is what reported a successful
        # `/spbot_run_sql_task 24` as `Exit code: 1` on 2026-08-26.
        exit_code = _read_exit_code_file(_exit_code_path(str(task["stdout_path"] or "")))

        stdout_text = _read_file_safe(str(task["stdout_path"] or ""))
        stderr_text = _read_file_safe(str(task["stderr_path"] or ""))
        parsed = parse_json_from_output(stdout_text) or {}
        status_str = str(parsed.get("status") or "").upper()
        success_output_contains = str(task_data.get("success_output_contains") or "")

        # Completion source, in order of authority: the SQLite terminal record (probe),
        # then the detached process's exit code / structured output / configured marker.
        vanished_without_verdict = False
        if probe_result is not None:
            success = probe_result[0] == "success"
        else:
            success_marker_found = bool(
                success_output_contains and success_output_contains in stdout_text
            )
            # `exit_code is None` means the child never recorded one — killed, or the write
            # failed. `completion_verdict` holds the rule and the two bugs it settles.
            expects_evidence = bool(
                task_data.get("completion_probe") or success_output_contains
            )
            success = completion_verdict(
                exit_code=exit_code,
                status_str=status_str,
                marker_found=success_marker_found,
                timed_out=timed_out,
                expects_evidence=expects_evidence,
            )
            vanished_without_verdict = (
                not success and not timed_out and exit_code is None and expects_evidence
            )

        if timed_out:
            message_text = render_template(
                str(task_data.get("timeout_text") or "Command timed out after {timeout_seconds} seconds."),
                values | {"timeout_seconds": timeout_seconds},
            )
            final_status = "timeout"
        elif success:
            message_text = render_template(
                str(task_data.get("success_text") or "Command completed."),
                values | parsed,
            )
            final_status = "done"
        else:
            error_detail = _extract_error_from_output(stderr_text, stdout_text)
            if probe_result is not None and probe_result[1]:
                error_detail = probe_result[1]  # authoritative error from the SQLite job_run record
            elif vanished_without_verdict:
                # The stderr of a killed process holds whatever it happened to be doing, which
                # for a restore is the config banner — reading like a run that ended tidily.
                # Say what actually happened instead.
                error_detail = (
                    "the process ended without recording an outcome: no completion record, "
                    "no exit code. It was killed or interrupted mid-run — treat the run as "
                    "incomplete and check the target before re-running."
                )
            message_text = render_template(
                str(
                    task_data.get("failure_text")
                    or "Command failed.\nExit code: {exit_code}\nError: {error_summary}"
                ),
                values
                | parsed
                | {
                    # Not 1: a code nobody recorded is unknown, and printing 1 sends its reader
                    # looking for an error the process never reported.
                    "exit_code": exit_code if exit_code is not None
                    else ("unknown" if vanished_without_verdict else 1),
                    "error_summary": error_detail,
                },
            )
            final_status = "failed"

        queue_message({
            "store": store_block_from(store),
            "chat_id": str(task["chat_id"]),
            "text": message_text,
            "reply_message_id": int(task["message_id"]) if task["message_id"] is not None else None,
            "status": final_status,
            "note": f"CLI background task completion for {task['command_text']}",
            "source_type": "telegram_background_tasks",
            "source_id": str(task["task_id"]),
            "metadata": {
                "command_id": int(task["command_id"]),
                "command_text": str(task["command_text"]),
                "status": final_status,
                "pid": pid,
                "values": mask_sensitive_value(values),
            },
        }, fallback_store=store)
        store.complete_telegram_background_task(
            task_id=int(task["task_id"]),
            status=final_status,
            result_json=json.dumps(parsed, ensure_ascii=False) if parsed else None,
        )
        dlog = _dispatch_logger("")
        _dispatch_log(
            dlog,
            f"dispatch subprocess_completed command={task['command_text']} pid={pid} "
            f"final_status={final_status} exit_code={exit_code if exit_code is not None else 'unknown'} "
            f"timed_out={timed_out} "
            f"stdout_tail={_format_dispatch_value(_tail_text(stdout_text))} "
            f"stderr_tail={_format_dispatch_value(_tail_text(stderr_text))}",
            level="logging" if final_status == "done" else "critical",
        )
        _remove_file_safe(_exit_code_path(str(task["stdout_path"] or "")))
        _remove_file_safe(str(task["stdout_path"] or ""))
        _remove_file_safe(str(task["stderr_path"] or ""))
        counts["completed"] += 1
        counts["queued_reply"] += 1
        if timed_out:
            counts["timed_out"] += 1

    return counts


def _exit_code_path(stdout_path: str) -> str:
    """Where a detached POSIX command writes its exit code (see the dispatch)."""
    return f"{stdout_path}.rc"


def _read_exit_code_file(path: str) -> int | None:
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _read_file_safe(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip() if path else ""
    except OSError:
        return ""


def _remove_file_safe(path: str) -> None:
    try:
        if path:
            Path(path).unlink(missing_ok=True)
    except OSError:
        pass
