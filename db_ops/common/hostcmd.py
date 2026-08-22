"""Running one command where the database actually lives.

Every file-side operation in this layer needs the same three things: reach a machine, step into
whatever the database is running inside, run a command. The *stepping into* is the part worth
writing once. On this estate the same engine turns up four ways — a Windows VM, an Ubuntu VM, a
container on Ubuntu, a pod on Kubernetes — and if each caller wrapped its own ``docker exec`` they
would each pick a different quoting, a different ``sudo``, and a different way of being wrong
about a path with a space in it.

So the request **says** where it runs and this module works out the command line::

    {"runtime": "docker", "host": "203.0.113.188", "username": "ubuntu",
     "key_file": "...", "container": "ora_dg_lab-primary", "sudo": true}

``runtime`` is stated rather than guessed. A ``container`` field alone would leave "no container"
meaning both "run on the host" and "the caller forgot", and those must not look the same when the
command being run is a restore.

Pure parameters, like everything else here — nothing is looked up. No ``host`` means this machine,
which is what lets the worker use the same command against its own filesystem.
"""

from __future__ import annotations

import base64
import shlex
import subprocess
from dataclasses import dataclass
from typing import Any

#: The four shapes this estate actually runs, named in the request.
WINDOWS = "windows"      # a Windows VM: PowerShell, no container
LINUX = "linux"          # an Ubuntu/RHEL VM: the engine runs on the host itself
DOCKER = "docker"        # a container on a Linux host
K8S = "k8s"              # a pod on a Kubernetes cluster reached from a Linux host
RUNTIMES = (WINDOWS, LINUX, DOCKER, K8S)

#: How the machine is *reached*, which is a different question from what runs on it.
#:
#: They were conflated until 2026-08-07, and the conflation cost the whole Windows half of the
#: estate: ``runtime: windows`` meant "a Windows host with an OpenSSH server", and exactly one
#: Windows box here has one. The other thirteen SQL Servers are reached by **WinRM** — that is what
#: ``cmd_access.method`` says in ``db_instances.json`` and what the metrics collectors have used
#: all along. A backup command that can only speak SSH cannot back any of them up.
SSH = "ssh"
WINRM = "winrm"
ACCESSES = (SSH, WINRM)


class HostCommandError(RuntimeError):
    """The command could not be run at all — not the same as running and failing."""


@dataclass(frozen=True)
class Host:
    """Where to run. ``host`` empty means this machine."""

    runtime: str = LINUX
    host: str = ""
    port: int = 22
    username: str = ""
    password: str = ""
    key_file: str = ""
    #: runtime=docker
    container: str = ""
    #: runtime=k8s
    pod: str = ""
    namespace: str = "default"
    #: The container inside the pod, when it holds more than one.
    pod_container: str = ""
    #: Reaching docker/kubectl often needs it. Stated rather than guessed.
    sudo: bool = False
    #: How to reach it: ``ssh`` (default) or ``winrm``. Independent of ``runtime`` — a Windows host
    #: can have an OpenSSH server, and WinRM is how most of them are actually reached here.
    access: str = SSH
    #: WinRM only.
    ssl: bool = False
    winrm_auth: str = "negotiate"

    @property
    def is_local(self) -> bool:
        return not self.host

    @property
    def is_windows(self) -> bool:
        return self.runtime == WINDOWS

    @property
    def is_winrm(self) -> bool:
        return self.access == WINRM and not self.is_local


def parse_host(raw: Any, *, where: str = "host") -> Host:
    """Read a host block off a request object, refusing a runtime it cannot honour."""
    if raw in (None, {}):
        return Host()
    if not isinstance(raw, dict):
        raise HostCommandError(f"{where} must be an object.")

    runtime = str(raw.get("runtime") or LINUX).strip().lower()
    if runtime not in RUNTIMES:
        raise HostCommandError(
            f"{where}.runtime must be one of {', '.join(RUNTIMES)}; got {runtime!r}."
        )
    # `method` is accepted as a spelling of `access` because that is the word db_instances.json's
    # cmd_access already uses, so a caller can hand that block straight through.
    access = str(raw.get("access") or raw.get("method") or SSH).strip().lower()
    if access == "local":
        access = SSH        # "local" is said by leaving `host` out; see Host.is_local.
    if access not in ACCESSES:
        raise HostCommandError(
            f"{where}.access must be one of {', '.join(ACCESSES)}; got {access!r}."
        )
    ssl = bool(raw.get("ssl", False))
    default_port = (5986 if ssl else 5985) if access == WINRM else 22
    host = Host(
        runtime=runtime,
        host=str(raw.get("host") or "").strip(),
        port=int(raw.get("port") or default_port),
        username=str(raw.get("username") or "").strip(),
        password=str(raw.get("password") or ""),
        key_file=str(raw.get("key_file") or "").strip(),
        container=str(raw.get("container") or "").strip(),
        pod=str(raw.get("pod") or "").strip(),
        namespace=str(raw.get("namespace") or "default").strip(),
        pod_container=str(raw.get("pod_container") or "").strip(),
        sudo=bool(raw.get("sudo", False)),
        access=access,
        ssl=ssl,
        winrm_auth=str(raw.get("auth") or raw.get("winrm_auth") or "negotiate").strip() or "negotiate",
    )
    # Each runtime names its own thing. Missing it is refused here rather than becoming a command
    # that runs on the host and reports, quite truthfully, that the database is not there.
    if runtime == DOCKER and not host.container:
        raise HostCommandError(f"{where}.container is required when runtime is 'docker'.")
    if runtime == K8S and not host.pod:
        raise HostCommandError(f"{where}.pod is required when runtime is 'k8s'.")
    return host


def wrap(host: Host, command: str) -> str:
    """The command as it must actually be typed for that runtime.

    ``sh -lc`` inside a container or pod because the binaries these commands call (``rman``,
    ``pg_controldata``) are on a login shell's PATH and not on the bare exec environment's — a
    fact otherwise discovered once per caller, each time as "command not found" for a binary that
    is plainly installed.
    """
    if host.runtime == WINDOWS:
        # -EncodedCommand, not -Command, because there is no quoting that survives the trip. The
        # command passes through cmd.exe (locally, `shell=True`) or through whatever shell the
        # Windows OpenSSH server runs, and neither understands `shlex.quote`'s POSIX single quotes:
        # a PowerShell literal `'C:\bak\a.bkp'` arrived as `'''C:\bak\a.bkp'''` and PowerShell
        # refused the whole script with "Unexpected token". Base64 has no characters either shell
        # treats specially, so nothing in the payload — a path with a space, an apostrophe, a `&` —
        # can be reinterpreted on the way. -NoProfile keeps a user profile from changing what a
        # restore sees.
        encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
        return f"powershell -NoProfile -NonInteractive -EncodedCommand {encoded}"
    if host.runtime == DOCKER:
        inner = f"docker exec -i {shlex.quote(host.container)} sh -lc {shlex.quote(command)}"
        return f"sudo {inner}" if host.sudo else inner
    if host.runtime == K8S:
        target = f"-n {shlex.quote(host.namespace)} {shlex.quote(host.pod)}"
        if host.pod_container:
            target += f" -c {shlex.quote(host.pod_container)}"
        inner = f"kubectl exec -i {target} -- sh -lc {shlex.quote(command)}"
        return f"sudo {inner}" if host.sudo else inner
    return command


def open_client(host: Host):
    """A paramiko client to the host. The caller closes it.

    Separate from :func:`run` because a file transfer holds one session open across many
    operations, and opening a connection per file is what made the old per-file copy measure
    10 KB/s over two internet hops.
    """
    if host.is_local:
        raise HostCommandError("no host given: there is nothing to connect to.")
    from db_ops.common.ssh import open_ssh_client

    try:
        return open_ssh_client(
            host.host, host.username, port=host.port,
            password=host.password or None, key_filename=host.key_file or None,
            announce=False,
        )
    except Exception as exc:  # noqa: BLE001
        raise HostCommandError(f"could not connect to {host.username}@{host.host}: {exc}") from exc


def run(host: Host, command: str, *, timeout: int = 300, client: Any = None) -> dict[str, Any]:
    """Run ``command`` and return ``{exit_code, stdout, stderr}``. Never raises on a non-zero exit.

    A non-zero exit is an answer — ``rman`` refusing, a directory that is not there — and the
    caller decides what it means. Only being unable to run at all raises.

    ``client`` is an already-open connection from :func:`open_client`, for a caller running many
    commands against one host. Without it every call connects and disconnects, which is the same
    per-file cost that made the old copy measure 10 KB/s — deleting 200 backup pieces would open
    200 SSH sessions. A borrowed client is never closed here: it belongs to whoever opened it.
    """
    if host.is_winrm:
        # No wrap(): a WinRM session already lands in PowerShell on that host, and wrapping would
        # start a second one inside it. remote_exec owns the protocol - it is what the metrics
        # collectors have used against these same hosts for months.
        return _via_remote_exec(host, command=command, timeout=timeout)

    full = wrap(host, command)
    if host.is_local:
        try:
            done = subprocess.run(full, shell=True, capture_output=True, text=True, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            raise HostCommandError(f"could not run locally: {exc}") from exc
        return {"exit_code": done.returncode, "stdout": done.stdout, "stderr": done.stderr}

    borrowed = client is not None
    if not borrowed:
        client = open_client(host)
    try:
        _stdin, stdout, stderr = client.exec_command(full, timeout=timeout)
        out = stdout.read().decode("utf-8", "replace")
        code = stdout.channel.recv_exit_status()
        err = stderr.read().decode("utf-8", "replace")
    finally:
        if not borrowed:
            client.close()
    return {"exit_code": code, "stdout": out, "stderr": err}


def run_script(host: Host, script: str, *, env: dict[str, str] | None = None,
               timeout: int | None = None) -> dict[str, Any]:
    """Run a whole script **on the host itself**, with ``env`` exported ahead of it.

    Not :func:`run` with a longer string. A backup script is a hundred lines and its own quoting;
    passing it as an argument means every character in it has to survive two shells, and a long one
    eventually meets ``ARG_MAX``. Fed on stdin it survives verbatim.

    On the host, and deliberately not wrapped into the container: the scripts that use this do
    their own ``docker exec`` because they need to be on the host for the directory the backup is
    written to. ``runtime`` still decides the interpreter — a Windows host gets PowerShell — but
    ``docker``/``k8s`` mean "the host that runs it", not "inside it".

    **A ``docker exec`` inside such a script must close its own stdin** (no ``-i``). The script is
    the shell's stdin, so a container that reads stdin eats the rest of the script and the shell
    then runs out of work: exit 0, no output, nothing done. That is the failure this layer checks
    a receipt for rather than trusting an exit code.
    """
    if host.is_winrm:
        return _via_remote_exec(host, script=script, env=env, timeout=timeout)

    prelude = "".join(f"export {name}={shlex.quote(str(value))}\n"
                      for name, value in sorted((env or {}).items()))
    if host.runtime == WINDOWS:
        # PowerShell reads `-Command -` from stdin; env goes in as assignments for the same reason.
        prelude = "".join(f"$env:{name} = {_ps_literal(str(value))}\n"
                          for name, value in sorted((env or {}).items()))
        interpreter = "powershell -NoProfile -NonInteractive -Command -"
    else:
        interpreter = "bash -s"
    payload = prelude + script

    if host.is_local:
        try:
            done = subprocess.run(interpreter, shell=True, input=payload,
                                  capture_output=True, text=True, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            raise HostCommandError(f"could not run locally: {exc}") from exc
        return {"exit_code": done.returncode, "stdout": done.stdout, "stderr": done.stderr}

    if host.runtime == WINDOWS:
        # PowerShell will not take a real script on stdin. `-Command -` reads it, returns 0, and
        # produces nothing: a here-string or a `function` block silently yields no output at all,
        # which for a backup script means "did nothing, reported success" — the exact failure the
        # RESULT=ok receipt exists to catch. `-EncodedCommand` is not the way out either: the
        # SQL Server script is 15 KB, which is ~41 KB of base64 against a 32767-character Windows
        # command line. So the script is written to a file on the host and run with `-File`.
        return _run_windows_script_file(host, payload, timeout=timeout)

    client = open_client(host)
    try:
        stdin, stdout, stderr = client.exec_command(interpreter, timeout=timeout or None)
        stdin.write(payload)
        stdin.flush()
        # Without this the interpreter waits for more input forever: it has no way to know the
        # script ended, and the call hangs until the timeout rather than running anything.
        stdin.channel.shutdown_write()
        out = stdout.read().decode("utf-8", "replace")
        code = stdout.channel.recv_exit_status()
        err = stderr.read().decode("utf-8", "replace")
    finally:
        client.close()
    return {"exit_code": code, "stdout": out, "stderr": err}


def _ps_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _windows_path(path: str) -> str:
    """A path SFTP reported, as PowerShell needs to see it.

    Windows OpenSSH's SFTP speaks POSIX: ``sftp.normalize()`` answers ``/C:/Users/x/y.ps1``, and
    ``-File`` on that fails with "The given path's format is not supported" — a message that reads
    like a permissions or quoting problem and is neither.
    """
    text = str(path)
    if len(text) > 2 and text[0] == "/" and text[2] == ":":
        text = text[1:]
    return text.replace("/", "\\")


def _run_windows_script_file(host: Host, payload: str, *, timeout: int | None) -> dict[str, Any]:
    """Upload the script over SFTP, run it with ``-File``, delete it.

    A file, because the two ways of passing a script on the command line both fail here: stdin is
    read and silently ignored for anything multi-line, and ``-EncodedCommand`` runs out of command
    line. A file has neither limit and is also what the script would look like if a DBA ran it by
    hand, which makes a failure reproducible.

    Written UTF-8 **with a BOM**: without one, Windows PowerShell 5.1 reads a `-File` script as the
    system ANSI codepage, and any non-ASCII character in a path or a message becomes mojibake that
    surfaces much later as a file the restore cannot find.

    The file is removed in ``finally`` — it carries the environment prelude, and that includes
    the backup encryption passphrase.
    """
    import uuid

    remote_name = f"db_ops_{uuid.uuid4().hex}.ps1"
    client = open_client(host)
    try:
        sftp = client.open_sftp()
        try:
            with sftp.file(remote_name, "wb") as handle:
                handle.write(b"\xef\xbb\xbf" + payload.encode("utf-8"))
            remote_path = _windows_path(sftp.normalize(remote_name))
        finally:
            sftp.close()

        command = (f"powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass "
                   f"-File {_ps_literal(remote_path)}")
        _stdin, stdout, stderr = client.exec_command(command, timeout=timeout or None)
        out = stdout.read().decode("utf-8", "replace")
        code = stdout.channel.recv_exit_status()
        err = stderr.read().decode("utf-8", "replace")
    finally:
        try:
            sftp = client.open_sftp()
            try:
                sftp.remove(remote_name)
            finally:
                sftp.close()
        except Exception:  # noqa: BLE001 - a leftover temp script must not mask the real result.
            pass
        client.close()
    return {"exit_code": code, "stdout": out, "stderr": err}


def _via_remote_exec(host: Host, *, command: str = "", script: str = "",
                     env: dict[str, str] | None = None,
                     timeout: int | None = None) -> dict[str, Any]:
    """WinRM, delegated rather than reimplemented.

    :mod:`db_ops.common.remote_exec` already speaks it — negotiate/basic auth, the PowerShell
    encoding, the error classification — and has run against these Windows hosts on every metrics
    cycle for months. A second WinRM client here would be a second set of quoting bugs to find, on
    the machines where being wrong means a production SQL Server.

    The values are passed, never looked up: this layer holds no credentials, so ``data_dir`` and
    ``secrets`` are deliberately not offered to ``remote_exec`` here.
    """
    from db_ops.common import remote_exec

    access = {
        "method": remote_exec.METHOD_WINRM,
        "host": host.host,
        "port": host.port,
        "username": host.username,
        "password": host.password,
        "ssl": host.ssl,
        "auth": host.winrm_auth,
        "platform": "windows",
    }
    try:
        if script:
            result = remote_exec.run_script(access, script, env=env, timeout_seconds=timeout)
        else:
            result = remote_exec.run_command(access, command, timeout_seconds=timeout)
    except remote_exec.RemoteExecError as exc:
        # Could not run at all. Same distinction the SSH path makes, so a caller does not have to
        # know which transport produced the failure.
        raise HostCommandError(str(exc)) from exc
    return {"exit_code": result.exit_code, "stdout": result.stdout, "stderr": result.stderr}
