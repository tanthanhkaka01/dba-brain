"""A SQL Server restore: running a step with sqlcmd - the request, the transport, the progress, the resume state, and how its output is judged.

Split out of ``backup_restore/restore_database.py`` on 2026-10-03 (Q11,
``audits/20261002_audit_typed_requests_and_errors.md`` section 5). ``restore_database``
re-exports every name, so no import changes.
"""

from __future__ import annotations
from db_ops.lib import errors
from db_ops.backup_restore.shell_quoting import _BACKUP_TIMESTAMP_RE, backup_time_from_name, _build_sqlcmd_auth_args, _escape_identifier, _escape_sql_string, _ps_array, _ps_quote  # noqa: F401 - one definition
import subprocess
import time
from db_ops.backup_restore.config import (
    BackupRestoreConfig,
)
from db_ops.backup_restore.copy_backup import resolve_password_ref
from db_ops.lib import powershell
from db_ops.lib.shell import is_powershell_executable
from db_ops.backup_restore.sanitize import sanitize_text
from db_ops.backup_restore.restore_base import _emit_restore_log, _format_metadata


RESTORE_FAILURE_MARKERS = (
    "msg 3013",
    "terminating abnormally",
    "incorrectly formed",
    "can not be read",
    "cannot be read",
    "the media family",
)

PRESTART_SQL_CONNECTION_MARKERS = (
    "login timeout expired",
    "tcp provider: error code 0x102",
    "network-related or instance-specific error",
)

AMBIGUOUS_SQL_CONNECTION_MARKERS = (
    "connection reset",
    "broken pipe",
)


class RestoreCommandTimeoutError(errors.OperationFailed):
    def __init__(self, message: str, *, command_started: bool) -> None:
        super().__init__(message)
        self.command_started = command_started


def build_sqlcmd_query_command(*, sql: str, config: BackupRestoreConfig) -> list[str]:
    command = _SqlcmdCommand(_sqlcmd_argv(sql=sql, config=config))
    command.sql = sql
    return command


def _sqlcmd_argv(*, sql: str, config: BackupRestoreConfig) -> list[str]:
    sql_auth_args = _build_sqlcmd_auth_args(config)
    timeout_args = [
        "-l",
        str(config.sql_login_timeout_seconds),
        "-t",
        str(config.sql_query_timeout_seconds),
    ]
    if config.vm_credential_target and config.is_linux:
        # Linux: returned as a sentinel — actual execution is SSH-based via run_sqlcmd_query_command.
        return ["__ssh_sqlcmd__", config.vm_credential_target, config.restore_sql_instance_on_vm, sql, *timeout_args]
    if config.vm_credential_target:
        open_timeout_ms = max(config.remote_command_timeout_seconds, 1) * 1000
        operation_timeout_ms = (
            config.restore_command_timeout_seconds * 1000
            if config.restore_command_timeout_seconds > 0
            else 2_147_483_647
        )
        password = ""
        if config.vm_username and config.vm_password_env:
            password = resolve_password_ref(config.vm_password_env)
            if not password:
                raise errors.NotConfigured(f"Password ref not found in environment or secret_text.json: {config.vm_password_env}")
        # The Invoke-Command wrapper (credential, session option, script block) is shared —
        # see db_ops.lib.powershell. Only the remote body below is restore-specific.
        return powershell.build_invoke_command_argv(
            host=config.vm_credential_target,
            username=config.vm_username if password else "",
            password=password,
            open_timeout_ms=open_timeout_ms,
            operation_timeout_ms=operation_timeout_ms,
            arguments=[config.sqlcmd_path, config.restore_sql_instance_on_vm, sql],
            script_body=[
                "    param($SqlcmdPath, $SqlInstance, $Sql)",
                f"    $sqlAuthArgs = @({_ps_array(sql_auth_args)})",
                f"    $timeoutArgs = @({_ps_array(timeout_args)})",
                "    & $SqlcmdPath -S $SqlInstance -C @sqlAuthArgs @timeoutArgs -b -Q $Sql",
                "    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }",
            ],
        )
    return [
        config.sqlcmd_path,
        "-S",
        config.restore_sql_instance_on_vm,
        "-C",
        *sql_auth_args,
        *timeout_args,
        "-b",
        "-Q",
        sql,
    ]


def _remote_exec_type(config: BackupRestoreConfig) -> str:
    if config.vm_credential_target:
        return "ssh" if config.is_linux else "powershell"
    return "local"


def _assert_sql_command_target(cmd: list[str], config: BackupRestoreConfig) -> str:
    exec_type = _remote_exec_type(config)
    first = str(cmd[0]).lower() if cmd else ""
    is_ssh = first == "__ssh_sqlcmd__"
    is_powershell = is_powershell_executable(cmd[0]) if cmd else False

    if config.is_linux and not is_ssh:
        raise errors.Refused(
            f"Target context mismatch: restore_id={config.restore_id} target_host={config.vm_credential_target} "
            f"target_os_type=linux cannot execute remote_exec_type={'powershell' if is_powershell else 'local'}."
        )
    if not config.is_linux and config.vm_credential_target and not is_powershell:
        raise errors.Refused(
            f"Target context mismatch: restore_id={config.restore_id} target_host={config.vm_credential_target} "
            f"target_os_type=windows cannot execute remote_exec_type={'ssh' if is_ssh else 'local'}."
        )
    if is_ssh:
        if len(cmd) < 4 or cmd[1] != config.vm_credential_target or cmd[2] != config.restore_sql_instance_on_vm:
            raise errors.Refused(
                f"Target context mismatch: restore_id={config.restore_id} SSH command target does not match "
                f"target_host={config.vm_credential_target} sql_instance={config.restore_sql_instance_on_vm}."
            )
    if is_powershell:
        expected = f"Invoke-Command -ComputerName {_ps_quote(config.vm_credential_target)}"
        script = cmd[-1] if cmd else ""
        if expected not in script:
            raise errors.Refused(
                f"Target context mismatch: restore_id={config.restore_id} PowerShell command does not match "
                f"target_host={config.vm_credential_target}."
            )
    return exec_type


def _sql_of(cmd: list[str]) -> str:
    """The batch inside a command :func:`build_sqlcmd_query_command` made, in any of its shapes."""
    if cmd and cmd[0] == "__ssh_sqlcmd__":
        return str(cmd[3])
    if "-Q" in cmd:
        return str(cmd[cmd.index("-Q") + 1])
    # The Invoke-Command argv carries the batch inside its script; build_sqlcmd_query_command
    # records it on the list it returns.
    sql = getattr(cmd, "sql", None)
    if sql is None:
        raise errors.InvalidRequest("no SQL batch found in the sqlcmd command.")
    return str(sql)


class _SqlcmdCommand(list):
    """The argv :func:`build_sqlcmd_query_command` returns, which also remembers its batch.

    A list, so everything that compared or asserted its shape still does; the batch rides along
    because the PowerShell shape buries it inside a script, and ``common.cli run-sqlcmd`` is handed
    the batch and its context as values, not a command line to reverse-engineer. A restore step
    rides along instead as ``restore_step`` - ``(level, request)`` - and goes to
    ``common.cli restore-<level>``, which writes the RESTORE itself."""

    sql: str = ""
    restore_step: tuple[str, dict[str, object]] | None = None


def _restore_step_command(restore: tuple[str, dict[str, object]], *,
                          config: BackupRestoreConfig) -> list[str]:
    """The command shape for a restore step: where it runs (ssh / PowerShell / local), with the step
    in place of a batch - the shape is still what :func:`_assert_sql_command_target` checks and what
    the log names, and nothing reads a statement out of it."""
    level, fields = restore
    command = build_sqlcmd_query_command(
        sql=f"-- restore-{level} {fields.get('backup_path')} (written by common.cli)", config=config)
    command.restore_step = (level, dict(fields))
    return command


def _sqlcmd_request(cmd: list[str], config: BackupRestoreConfig, *, via: str) -> dict[str, object]:
    """The ``run-sqlcmd`` request for one batch, every value resolved here - ``common`` reads none."""
    auth = _build_sqlcmd_auth_args(config)
    username, password = (auth[1], auth[3]) if auth[:1] == ["-U"] else ("", "")
    request: dict[str, object] = {
        "sql": _sql_of(cmd),
        "instance": config.restore_sql_instance_on_vm,
        "sqlcmd_path": config.sqlcmd_path,
        "username": username,
        "password": password,
        "login_timeout_seconds": config.sql_login_timeout_seconds,
        "query_timeout_seconds": config.sql_query_timeout_seconds,
        "timeout_seconds": config.restore_command_timeout_seconds,
        "via": via,
    }
    if config.sql_container and via != "winrm":
        # The container's own sqlcmd: the target host may have none (a lab VM with only Docker).
        request["container"] = config.sql_container
    if via != "local":
        host_password = ""
        if config.vm_password_env and (via == "ssh" or config.vm_username):
            host_password = resolve_password_ref(config.vm_password_env)
            if not host_password:
                raise errors.NotConfigured(
                    f"Password ref not found in environment or secret_text.json: {config.vm_password_env}")
        request["host"] = {
            "host": config.vm_credential_target,
            "username": config.vm_username,
            "password": host_password,
            "open_timeout_seconds": config.remote_command_timeout_seconds,
        }
    return request


def _local_request_from_argv(cmd: list[str], *, timeout_seconds: int) -> dict[str, object]:
    """A local ``sqlcmd`` argv, read back into a request - for a caller with no restore config."""
    def after(flag: str, default: str = "") -> str:
        return str(cmd[cmd.index(flag) + 1]) if flag in cmd else default

    return {"sql": _sql_of(cmd), "instance": after("-S"), "sqlcmd_path": str(cmd[0]),
            "username": after("-U"), "password": after("-P"),
            "login_timeout_seconds": int(after("-l", "30")), "query_timeout_seconds": int(after("-t", "0")),
            "timeout_seconds": timeout_seconds, "via": "local"}


def _sqlcmd_in_common(request: dict[str, object], *, cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Run one batch through ``common.cli run-sqlcmd`` (1.38) and hand it back as the process it was.

    The restore's statements used to run here - an SSH channel of the app's own, a local
    PowerShell, a local ``sqlcmd``. They run in ``common`` now, which reads no configuration: this
    app resolved every value above. What an answer MEANS - a failure hidden in exit code 0, a
    connection lost mid-RESTORE LOG - is still decided by the caller of this function, unchanged.
    stderr streams: ``sqlcmd``'s *percent processed* reaches this process's log as it happens.
    """
    from db_ops.transport import common_cli

    step = getattr(cmd, "restore_step", None)
    if step is not None:
        level, fields = step
        # The same sqlcmd, in the same place, with the same timeouts - the batch is written there.
        sqlcmd = {key: value for key, value in request.items() if key != "sql"}
        ok, data, error = common_cli.run_allowing_failure(
            f"restore-{level}", {**fields, "sqlcmd": sqlcmd}, stream_stderr=True)
    else:
        ok, data, error = common_cli.run_allowing_failure("run-sqlcmd", request, stream_stderr=True)
    if not ok:
        raise errors.OperationFailed(f"sqlcmd could not be run ({request.get('via')}): {error}")
    if data.get("timed_out"):
        raise RestoreCommandTimeoutError(
            f"Restore command timed out after {request.get('timeout_seconds')} seconds.",
            command_started=True,
        )
    exit_code = data.get("exit_code")
    completed = subprocess.CompletedProcess(
        cmd, 1 if exit_code is None else int(exit_code),
        str(data.get("stdout") or ""), str(data.get("stderr") or ""))
    # What actually ran, for the step's record - a restore step's text is written in common.
    completed.statements = list(data.get("statements") or [])
    return completed


def _run_sqlcmd_via_ssh(cmd: list[str], config: BackupRestoreConfig) -> subprocess.CompletedProcess[str]:
    """``sqlcmd`` on a Linux target over SSH. ``cmd`` is the sentinel list from build_sqlcmd_query_command."""
    _assert_sql_command_target(cmd, config)
    return _sqlcmd_in_common(_sqlcmd_request(cmd, config, via="ssh"), cmd=cmd)


def run_sqlcmd_query_command(
    cmd: list[str],
    *,
    config: BackupRestoreConfig | None = None,
    logger: object | None = None,
    progress_step: str | None = None,
    progress_database: str | None = None,
    restore_id: str = "",
    allow_transient_retry: bool = True,
    command_file: str = "",
) -> subprocess.CompletedProcess[str]:
    if config is not None:
        remote_exec_type = _assert_sql_command_target(cmd, config)
        _emit_restore_log(
            logger,
            "restore-db remote-command dispatch "
            + _format_metadata(
                restore_id=config.restore_id or None,
                target_id=config.target_id,
                target_host=config.vm_credential_target or "local",
                target_os_type=config.vm_platform,
                remote_exec_type=remote_exec_type,
                sql_instance=config.restore_sql_instance_on_vm,
                command_phase=progress_step or "sql",
                sql_login_timeout_seconds=config.sql_login_timeout_seconds,
                sql_query_timeout_seconds=config.sql_query_timeout_seconds,
                remote_command_timeout_seconds=config.remote_command_timeout_seconds,
                restore_command_timeout_seconds=config.restore_command_timeout_seconds,
            ),
        )
    attempts = 3 if allow_transient_retry else 1
    for attempt in range(1, attempts + 1):
        result = _execute_sqlcmd_once(
            cmd,
            config=config,
            logger=logger,
            progress_step=progress_step,
            progress_database=progress_database,
            restore_id=restore_id,
        )
        if result.returncode == 0 and not restore_output_has_failure(result.stdout, result.stderr):
            return result
        combined_output = f"{result.stdout}\n{result.stderr}"
        if attempt < attempts and _is_transient_sql_connection_failure(combined_output):
            _emit_restore_log(
                logger,
                "restore-db remote-command retry "
                + _format_metadata(
                    restore_id=restore_id or (config.restore_id if config else None),
                    target_id=config.target_id if config else None,
                    target_host=config.vm_credential_target if config else None,
                    command_phase=progress_step or "sql",
                    reason="transient_connection_before_restore",
                    resume_decision="retry_safe_not_started",
                    last_confirmed_log="unknown",
                    next_log=command_file or "unknown",
                    attempt=attempt + 1,
                ),
            )
            time.sleep(min(attempt, 2))
            continue
        if progress_step == "restore-log" and _is_ambiguous_sql_connection_failure(combined_output):
            raise RestoreCommandTimeoutError(
                "Connection was lost after RESTORE LOG command dispatch; execution status is ambiguous.",
                command_started=True,
            )
        break
    details = [f"sqlcmd command failed with exit code {result.returncode}."]
    stdout = sanitize_text(result.stdout.strip())
    stderr = sanitize_text(result.stderr.strip())
    if stdout:
        details.append(f"stdout:\n{stdout}")
    if stderr:
        details.append(f"stderr:\n{stderr}")
    if not stdout and not stderr:
        details.append("No stdout/stderr was returned by PowerShell/sqlcmd.")
    raise errors.OperationFailed("\n".join(details))


def _execute_sqlcmd_once(
    cmd: list[str],
    *,
    config: BackupRestoreConfig | None,
    logger: object | None,
    progress_step: str | None,
    progress_database: str | None,
    restore_id: str,
) -> subprocess.CompletedProcess[str]:
    timeout_seconds = config.restore_command_timeout_seconds if config else 0
    if cmd and cmd[0] == "__ssh_sqlcmd__":
        if config is None:
            raise errors.InvalidRequest("config is required for SSH sqlcmd execution.")
        result = _run_sqlcmd_via_ssh(cmd, config)
        # The Linux path logged no progress at all until 0.23.0; it has the same answer to read.
        _log_restore_progress(result, logger=logger, progress_step=progress_step or "sql",
                              progress_database=progress_database or "unknown", restore_id=restore_id)
        return result
    return _run_sqlcmd_query_command_streaming(
        cmd,
        logger=logger,
        progress_step=progress_step or "sql",
        progress_database=progress_database or "unknown",
        restore_id=restore_id,
        timeout_seconds=timeout_seconds,
        config=config,
    )


def _run_sqlcmd_query_command_streaming(
    cmd: list[str],
    *,
    logger: object | None,
    progress_step: str,
    progress_database: str,
    restore_id: str = "",
    timeout_seconds: int = 0,
    config: BackupRestoreConfig | None = None,
) -> subprocess.CompletedProcess[str]:
    """A Windows target through ``Invoke-Command``, or a local ``sqlcmd`` - through ``common.cli``.

    The progress events are read off the answer; the live *percent processed* lines reach this
    process's stderr while the restore runs (see :func:`_sqlcmd_in_common`).
    """
    if config is not None:
        via = "winrm" if config.vm_credential_target and not config.is_linux else "local"
        request = _sqlcmd_request(cmd, config, via=via)
    else:
        request = _local_request_from_argv(cmd, timeout_seconds=timeout_seconds)
    result = _sqlcmd_in_common(request, cmd=cmd)
    _log_restore_progress(result, logger=logger, progress_step=progress_step,
                          progress_database=progress_database, restore_id=restore_id)
    return result


def _log_restore_progress(result: subprocess.CompletedProcess[str], *, logger: object | None,
                          progress_step: str, progress_database: str, restore_id: str = "") -> None:
    """One ``progress`` event per *NN percent processed* line sqlcmd printed."""
    if not logger:
        return
    for line in (result.stdout or "").splitlines():
        progress = _parse_restore_progress_percent(line)
        if progress is not None:
            _emit_restore_log(
                logger,
                f"restore-db {progress_step} progress "
                + _format_metadata(restore_id=restore_id or None, database=progress_database, percent=progress),
            )


def _is_transient_sql_connection_failure(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in PRESTART_SQL_CONNECTION_MARKERS)


def _is_ambiguous_sql_connection_failure(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in AMBIGUOUS_SQL_CONNECTION_MARKERS)


def _parse_restore_progress_percent(line: str) -> int | None:
    lowered = line.lower()
    if "percent" not in lowered:
        return None
    parts = lowered.replace(".", " ").split()
    for index, part in enumerate(parts[:-1]):
        if part.isdigit() and parts[index + 1].startswith("percent"):
            return int(part)
    return None


def restore_output_has_failure(stdout: str | None, stderr: str | None) -> bool:
    text = f"{stdout or ''}\n{stderr or ''}".lower()
    return any(marker in text for marker in RESTORE_FAILURE_MARKERS)


def _is_sqlserver_msg_4305(text: str) -> bool:
    lowered = text.lower()
    return "msg 4305" in lowered and "too recent to apply" in lowered
