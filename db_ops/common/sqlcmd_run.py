"""Run one ``sqlcmd`` batch where the SQL Server is: here, on a Linux host over SSH, or on a Windows
host through ``Invoke-Command``.

The SMB restore - the nightly SQL Server restore from a backup share onto another instance - built
this command in the ``backup_restore`` app and ran it there too: its own SSH channel for a Linux
target, a local PowerShell for a Windows one, a local ``sqlcmd`` otherwise. The operator's rule for
0.23.0 is that a restore runs through ``common.cli`` (1.38). The app still decides everything a
restore decides - which files, which statements, what an interruption means, when to retry - and
this runs what it decided, with every value in the request: the instance, the SQL login, the host
login, the timeouts. It reads nothing.

The three ways are the app's own, moved and not rewritten, because the nightly restore of a
production estate depends on them behaving exactly as they did: the same PATH for the Linux tools,
the same ``-C -b`` and timeouts, the same ``Invoke-Command`` wrapper with its credential and session
options (:mod:`db_ops.lib.powershell`, which the app already used).

**Every stdout line is echoed to stderr as it arrives.** A restore runs for an hour, and ``sqlcmd``
reports ``NN percent processed`` as it goes; the caller streams stderr to whoever is watching, and
stdout of this process is the JSON answer.

**A timeout is an answer, not an error.** ``timed_out`` true, with what had been read, because the
caller must tell "the command never started" from "it started and was cut off" - a RESTORE LOG cut
off mid-way leaves a database whose state has to be inspected, not retried blindly.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
import threading
import time
from typing import Any

VIA = ("local", "ssh", "winrm")

#: Where the Microsoft tools install on Linux. `sqlcmd` is not on a login shell's PATH by default.
_LINUX_TOOL_PATH = "export PATH=$PATH:/opt/mssql-tools/bin:/opt/mssql-tools18/bin; "


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


def _timeout_args(request: dict[str, Any]) -> list[str]:
    return ["-l", str(int(request.get("login_timeout_seconds") or 30)),
            "-t", str(int(request.get("query_timeout_seconds") or 0))]


def _answer(via: str, started: float, *, exit_code, stdout: str, stderr: str,
            timed_out: bool = False) -> dict[str, Any]:
    return {"via": via, "exit_code": exit_code, "stdout": stdout, "stderr": stderr,
            "timed_out": timed_out, "duration_ms": int((time.monotonic() - started) * 1000)}


def _run_local(argv: list[str], *, via: str, timeout: int, started: float) -> dict[str, Any]:
    """Run ``argv`` here, stdout and stderr together, each line echoed as it arrives."""
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                               encoding="utf-8", errors="replace")
    lines: list[str] = []

    def read() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            lines.append(line)
            _echo(line)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        exit_code = process.wait(timeout=timeout) if timeout > 0 else process.wait()
    except subprocess.TimeoutExpired:
        process.kill()
        reader.join(timeout=5)
        return _answer(via, started, exit_code=None, stdout="".join(lines), stderr="", timed_out=True)
    reader.join()
    return _answer(via, started, exit_code=exit_code, stdout="".join(lines), stderr="")


def _run_ssh(request: dict[str, Any], host: dict[str, Any], *, timeout: int,
             started: float) -> dict[str, Any]:
    from db_ops.common.hostcmd import open_client, parse_host

    target = parse_host({"runtime": "linux", "access": "ssh", "host": host.get("host"),
                         "port": host.get("port") or 22, "username": host.get("username"),
                         "password": host.get("password") or "", "key_file": host.get("key_file") or ""})
    remote = (_LINUX_TOOL_PATH
              + f"{shlex.quote(str(request.get('sqlcmd_path') or 'sqlcmd'))} "
              + f"-S {shlex.quote(str(request['instance']))} "
              + "-C " + " ".join(shlex.quote(arg) for arg in _auth_args(request)) + " "
              + " ".join(_timeout_args(request)) + " -b "
              + f"-Q {shlex.quote(str(request['sql']))}")
    client = open_client(target)
    try:
        channel = client.get_transport().open_session(
            timeout=int(host.get("open_timeout_seconds") or 0) or None)
        channel.exec_command(remote)
        deadline = time.monotonic() + timeout if timeout > 0 else None
        out, err = bytearray(), b""
        echoed = 0   # how much of `out` has been echoed, up to its last complete line

        def take(chunk: bytes) -> None:
            nonlocal echoed
            out.extend(chunk)
            end = out.rfind(b"\n") + 1
            if end > echoed:
                for line in bytes(out[echoed:end]).decode("utf-8", "replace").splitlines():
                    _echo(line)
                echoed = end

        timed_out = False
        while not channel.exit_status_ready():
            if channel.recv_ready():
                take(channel.recv(4096))
            if channel.recv_stderr_ready():
                err += channel.recv_stderr(4096)
            if deadline is not None and time.monotonic() >= deadline:
                timed_out = True
                channel.close()
                break
            time.sleep(0.05)
        if not timed_out:
            # A batch that finishes before the first poll leaves everything here - echoed too, or
            # a quick run shows nothing at all (found on the labs: a 0.04 s RESTORE).
            while channel.recv_ready():
                take(channel.recv(4096))
            while channel.recv_stderr_ready():
                err += channel.recv_stderr(4096)
        if echoed < len(out):
            _echo(bytes(out[echoed:]).decode("utf-8", "replace"))
        exit_code = None if timed_out else channel.recv_exit_status()
    finally:
        client.close()
    return _answer("ssh", started, exit_code=exit_code, stdout=bytes(out).decode("utf-8", "replace"),
                   stderr=err.decode("utf-8", "replace"), timed_out=timed_out)


def winrm_argv(request: dict[str, Any], host: dict[str, Any], *, timeout: int) -> list[str]:
    """The local PowerShell that runs ``sqlcmd`` on a Windows host through ``Invoke-Command``."""
    from db_ops.lib import powershell

    def array(values: list[str]) -> str:
        return ", ".join(powershell.quote_powershell(value) for value in values)

    return powershell.build_invoke_command_argv(
        host=str(host.get("host") or ""),
        username=str(host.get("username") or "") if host.get("password") else "",
        password=str(host.get("password") or ""),
        open_timeout_ms=max(int(host.get("open_timeout_seconds") or 0), 1) * 1000,
        operation_timeout_ms=timeout * 1000 if timeout > 0 else 2_147_483_647,
        arguments=[str(request.get("sqlcmd_path") or "sqlcmd"), str(request["instance"]),
                   str(request["sql"])],
        script_body=[
            "    param($SqlcmdPath, $SqlInstance, $Sql)",
            f"    $sqlAuthArgs = @({array(_auth_args(request))})",
            f"    $timeoutArgs = @({array(_timeout_args(request))})",
            "    & $SqlcmdPath -S $SqlInstance -C @sqlAuthArgs @timeoutArgs -b -Q $Sql",
            "    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }",
        ],
    )


def local_argv(request: dict[str, Any]) -> list[str]:
    return [str(request.get("sqlcmd_path") or "sqlcmd"), "-S", str(request["instance"]), "-C",
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
    timeout = int(request.get("timeout_seconds") or 0)
    started = time.monotonic()
    if via == "ssh":
        return _run_ssh(request, host, timeout=timeout, started=started)
    argv = winrm_argv(request, host, timeout=timeout) if via == "winrm" else local_argv(request)
    return _run_local(argv, via=via, timeout=timeout, started=started)
