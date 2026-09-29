"""Run one ``sqlcmd`` batch where the SQL Server is: here, on a Linux host over SSH, or on a Windows
host over WinRM.

The SMB restore - the nightly SQL Server restore from a backup share onto another instance - built
this command in the ``backup_restore`` app and ran it there too. The operator's rule for 0.23.0 is
that a restore runs through ``common.cli`` (1.38). The app still decides everything a restore
decides - which files, which statements, what an interruption means, when to retry - and this runs
what it decided, with every value in the request: the instance, the SQL login, the host login, the
timeouts. It reads nothing.

**It builds the command; :mod:`remote_exec` runs it** (0.25.0). Until then this module had three
executors of its own - a local ``Popen``, its own SSH channel, a local PowerShell ``Invoke-Command``
- beside the one every other command uses. The operator's rule: one way to run anything on a host.
The command line is unchanged: the same PATH for the Linux tools, the same ``-C -b`` and timeouts.

**Every stdout line is echoed to stderr as it arrives** (over WinRM, when the batch ends). A restore
runs for an hour, and ``sqlcmd`` reports ``NN percent processed`` as it goes; the caller streams
stderr to whoever is watching, and stdout of this process is the JSON answer.

**A timeout is an answer, not an error.** ``timed_out`` true, with what had been read, because the
caller must tell "the command never started" from "it started and was cut off" - a RESTORE LOG cut
off mid-way leaves a database whose state has to be inspected, not retried blindly.
"""

from __future__ import annotations

import shlex
import sys
import time
from typing import Any

VIA = ("local", "ssh", "winrm")

#: Where the Microsoft tools install on Linux. `sqlcmd` is not on a login shell's PATH by default.
_LINUX_TOOL_PATH = "export PATH=$PATH:/opt/mssql-tools/bin:/opt/mssql-tools18/bin; "

#: `sqlcmd` inside Microsoft's SQL Server image, which does not put it on PATH either. A request
#: that names a `container` and leaves `sqlcmd_path` at the bare default means this one.
CONTAINER_SQLCMD = "/opt/mssql-tools18/bin/sqlcmd"


class SqlcmdRunError(ValueError):
    """The request cannot be run as written."""


def _echo(line: str) -> None:
    print(line.rstrip("\n"), file=sys.stderr, flush=True)


def _auth_args(request: dict[str, Any]) -> list[str]:
    username = str(request.get("username") or "")
    password = str(request.get("password") or "")
    if not username and not password:
        return ["-E"]
    if not username or not password:
        raise SqlcmdRunError("a SQL login needs both username and password; give neither for -E.")
    return ["-U", username, "-P", password]


def sqlcmd_words(request: dict[str, Any]) -> list[str]:
    """The ``sqlcmd`` to start: the path itself, or ``docker exec <container> <path>``.

    A SQL Server in a container on a host with no tools of its own had no way in: this ran
    ``sqlcmd`` on the host, and a lab VM with only Docker on it has none (2026-09-27 - the restore
    of a production server into a lab VM needed a wrapper script on the VM to get past it). The container's own
    ``sqlcmd`` sees the same ``localhost,1433`` and the same bind-mounted backup path, so nothing
    else about the batch changes.
    """
    path = str(request.get("sqlcmd_path") or "").strip()
    container = str(request.get("container") or "").strip()
    if not container:
        return [path or "sqlcmd"]
    return ["docker", "exec", container, CONTAINER_SQLCMD if path in ("", "sqlcmd") else path]


def _timeout_args(request: dict[str, Any]) -> list[str]:
    return ["-l", str(int(request.get("login_timeout_seconds") or 30)),
            "-t", str(int(request.get("query_timeout_seconds") or 0))]


def _answer(via: str, started: float, *, exit_code, stdout: str, stderr: str,
            timed_out: bool = False) -> dict[str, Any]:
    return {"via": via, "exit_code": exit_code, "stdout": stdout, "stderr": stderr,
            "timed_out": timed_out, "duration_ms": int((time.monotonic() - started) * 1000)}


#: The last line of the Windows script: sqlcmd's own exit code. WinRM through pypsrp reports only
#: whether the error stream was written, and a sqlcmd that fails with -b writes its error to
#: stdout - a failed RESTORE would read as a success. Read back and removed from the output.
EXIT_MARKER = "DB_OPS_SQLCMD_EXIT="

#: "No deadline" over WinRM: its backends need a number, and the connect timeout they would fall
#: back to cuts a one-hour restore off at 30 s. The old Invoke-Command's own maximum, in seconds.
_WINRM_UNBOUNDED_SECONDS = 2_147_483


def _run_local(argv: list[str], *, via: str, timeout: int, started: float) -> dict[str, Any]:
    """Run ``argv`` here, each line echoed as it arrives; stderr follows stdout in the answer."""
    return _run({"method": "local"}, argv, via=via, timeout=timeout, started=started, merge=True)


def ssh_command(request: dict[str, Any]) -> str:
    """The command a Linux host runs - character for character what the app's SSH channel ran."""
    return (_LINUX_TOOL_PATH
            + " ".join(shlex.quote(word) for word in sqlcmd_words(request)) + " "
            + f"-S {shlex.quote(str(request['instance']))} "
            + "-C " + " ".join(shlex.quote(arg) for arg in _auth_args(request)) + " "
            + " ".join(_timeout_args(request)) + " -b "
            + f"-Q {shlex.quote(str(request['sql']))}")


def ssh_access(host: dict[str, Any]) -> dict[str, Any]:
    """The Linux host's login, as :mod:`remote_exec` opens it."""
    key_file = str(host.get("key_file") or "").strip()
    access: dict[str, Any] = {
        "method": "ssh", "host": str(host.get("host") or "").strip(), "port": host.get("port") or 22,
        "username": str(host.get("username") or ""), "password": str(host.get("password") or ""),
        "auth_type": "key" if key_file else "password", "platform": "linux", "shell": "bash"}
    if key_file:
        access["key_file"] = key_file
    if host.get("open_timeout_seconds"):
        access["timeout_seconds"] = int(host["open_timeout_seconds"])
    return access


def winrm_script(request: dict[str, Any]) -> str:
    """The PowerShell a Windows host runs: sqlcmd with the request's values, then its exit code."""
    from db_ops.lib.powershell import quote_powershell

    def array(values: list[str]) -> str:
        return ", ".join(quote_powershell(value) for value in values)

    return "\n".join([
        f"$SqlcmdPath = {quote_powershell(str(request.get('sqlcmd_path') or 'sqlcmd'))}",
        f"$SqlInstance = {quote_powershell(str(request['instance']))}",
        f"$Sql = {quote_powershell(str(request['sql']))}",
        f"$sqlAuthArgs = @({array(_auth_args(request))})",
        f"$timeoutArgs = @({array(_timeout_args(request))})",
        "& $SqlcmdPath -S $SqlInstance -C @sqlAuthArgs @timeoutArgs -b -Q $Sql",
        f'Write-Output "{EXIT_MARKER}$LASTEXITCODE"',
    ])


def winrm_access(host: dict[str, Any]) -> dict[str, Any]:
    """The Windows host's login. No password means the node's own identity: no username is sent
    either, as the app's ``Invoke-Command`` never sent a credential it could not complete."""
    password = str(host.get("password") or "")
    access: dict[str, Any] = {
        "method": "winrm", "host": str(host.get("host") or "").strip(), "platform": "windows",
        "username": str(host.get("username") or "") if password else "", "password": password}
    if host.get("open_timeout_seconds"):
        access["timeout_seconds"] = int(host["open_timeout_seconds"])
    return access


def _with_exit_code(stdout: str) -> tuple[int | None, str]:
    """``(exit code, stdout without the marker)`` - ``None`` when the script never reached it."""
    kept: list[str] = []
    code: int | None = None
    for line in stdout.splitlines(keepends=True):
        text = line.strip()
        if text.startswith(EXIT_MARKER):
            value = text[len(EXIT_MARKER):].strip()
            code = int(value) if value.lstrip("-").isdigit() else 0
            continue
        kept.append(line)
    return code, "".join(kept)


def _run(access: dict[str, Any], command: Any, *, via: str, timeout: int, started: float,
         merge: bool = False, script: bool = False) -> dict[str, Any]:
    """``command`` (or a ``script``) through :mod:`remote_exec`, answered as this module answers.

    Could not run at all is :class:`HostCommandError`; cut off at the deadline is ``timed_out``.
    """
    from db_ops.common import remote_exec
    from db_ops.common.hostcmd import HostCommandError

    def answer(exit_code: int | None, out: str, err: str, *, timed_out: bool = False) -> dict[str, Any]:
        if merge:
            out, err = out + err, ""
        return _answer(via, started, exit_code=exit_code, stdout=out, stderr=err, timed_out=timed_out)

    try:
        with remote_exec.open_session(access) as session:
            if script:
                result = session.run_script(command, timeout_seconds=timeout or _WINRM_UNBOUNDED_SECONDS)
            else:
                result = session.run(command, timeout_seconds=timeout or None, on_output=_echo)
    except remote_exec.RemoteCommandTimeoutError as exc:
        return answer(None, exc.stdout or "", exc.stderr or "", timed_out=True)
    except remote_exec.RemoteExecError as exc:
        raise HostCommandError(str(exc)) from exc
    if not script:
        return answer(result.exit_code, result.stdout or "", result.stderr or "")
    code, out = _with_exit_code(result.stdout or "")
    for line in out.splitlines():
        _echo(line)
    return answer(result.exit_code if code is None else code, out, result.stderr or "")


def local_argv(request: dict[str, Any]) -> list[str]:
    return [*sqlcmd_words(request), "-S", str(request["instance"]), "-C",
            *_auth_args(request), *_timeout_args(request), "-b", "-Q", str(request["sql"])]


def run_sqlcmd(request: dict[str, Any]) -> dict[str, Any]:
    """Run the batch. Returns ``via``, ``exit_code``, ``stdout``, ``stderr``, ``timed_out``,
    ``duration_ms``. Raises only when it could not run at all - no host to reach, a bad request."""
    if not str(request.get("sql") or "").strip():
        raise SqlcmdRunError("sql is required: the batch to run.")
    if not str(request.get("instance") or "").strip():
        raise SqlcmdRunError("instance is required: the -S that sqlcmd connects to, as the host sees it.")
    via = str(request.get("via") or "local").strip().lower()
    if via not in VIA:
        raise SqlcmdRunError(f"via must be one of {', '.join(VIA)}; got {via!r}.")
    host = request.get("host") or {}
    if via != "local" and not (isinstance(host, dict) and str(host.get("host") or "").strip()):
        raise SqlcmdRunError(f'via {via} needs "host": the machine sqlcmd runs on, with its login.')
    if via == "winrm" and str(request.get("container") or "").strip():
        raise SqlcmdRunError(
            "container is for a Linux host (via ssh) or this machine (via local); a Windows host "
            "runs its own sqlcmd - leave container out.")
    timeout = int(request.get("timeout_seconds") or 0)
    started = time.monotonic()
    if via == "ssh":
        return _run(ssh_access(host), ssh_command(request), via=via, timeout=timeout, started=started)
    if via == "winrm":
        return _run(winrm_access(host), winrm_script(request), via=via, timeout=timeout, started=started,
                    merge=True, script=True)
    return _run_local(local_argv(request), via=via, timeout=timeout, started=started)
