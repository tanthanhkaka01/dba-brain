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
from dataclasses import dataclass
from typing import Any

from db_ops.lib.shell import docker_cli

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
        return f"{docker_cli(host.sudo)} exec -i {shlex.quote(host.container)} sh -lc {shlex.quote(command)}"
    if host.runtime == K8S:
        target = f"-n {shlex.quote(host.namespace)} {shlex.quote(host.pod)}"
        if host.pod_container:
            target += f" -c {shlex.quote(host.pod_container)}"
        inner = f"kubectl exec -i {target} -- sh -lc {shlex.quote(command)}"
        return f"sudo {inner}" if host.sudo else inner
    return command


def access_for(host: Host) -> dict[str, Any]:
    """The login :mod:`db_ops.common.remote_exec` opens for ``host`` - the one executor.

    Stated, never looked up (this layer holds no credentials). A key file is the key; a password
    without one is a password login - ``remote_exec`` defaults to key auth, so it is said.
    """
    shell = "powershell" if host.is_windows else "bash"
    if host.is_local:
        return {"method": "local", "shell": shell}
    access: dict[str, Any] = {
        "method": host.access,
        "host": host.host,
        "port": host.port,
        "username": host.username,
        "password": host.password,
        "shell": shell,
        "platform": "windows" if host.is_windows else "linux",
    }
    if host.access == WINRM:
        access.update({"ssl": host.ssl, "auth": host.winrm_auth})
    else:
        access["auth_type"] = "key" if host.key_file else "password"
        if host.key_file:
            access["key_file"] = host.key_file
    return access


def open_session(host: Host):
    """A :mod:`remote_exec` session to the host, connected. The caller closes it.

    Separate from :func:`run` because a file transfer holds one session open across many
    operations, and opening a connection per file is what made the old per-file copy measure
    10 KB/s over two internet hops.
    """
    if host.is_local:
        raise HostCommandError("no host given: there is nothing to connect to.")
    from db_ops.common import remote_exec

    try:
        session = remote_exec.open_session(access_for(host))
        if isinstance(session, remote_exec.SshSession):
            session.client  # connect now, so a refused login is this call's error, not a later one's
        return session
    except remote_exec.RemoteExecError as exc:
        raise HostCommandError(f"could not connect to {host.username}@{host.host}: {exc}") from exc


def run(host: Host, command: str, *, timeout: int = 300, session: Any = None) -> dict[str, Any]:
    """Run ``command`` and return ``{exit_code, stdout, stderr}``. Never raises on a non-zero exit.

    A non-zero exit is an answer — ``rman`` refusing, a directory that is not there — and the
    caller decides what it means. Only being unable to run at all raises.

    ``session`` is an already-open one from :func:`open_session`, for a caller running many
    commands against one host. Without it every call connects and disconnects, which is the same
    per-file cost that made the old copy measure 10 KB/s — deleting 200 backup pieces would open
    200 SSH sessions. A borrowed session is never closed here: it belongs to whoever opened it.

    **It runs through** :mod:`remote_exec`, **like every command on a host** (0.25.0): this module
    had its own paramiko client and its own local ``subprocess`` - a second executor, with its own
    ideas about timeouts and errors.
    """
    # No wrap() over WinRM: a WinRM session already lands in PowerShell on that host, and
    # wrapping would start a second one inside it.
    full = command if host.is_winrm else wrap(host, command)
    return _through_remote_exec(host, session, lambda live: live.run(full, timeout_seconds=timeout))


def run_script(host: Host, script: str, *, env: dict[str, str] | None = None,
               timeout: int | None = None) -> dict[str, Any]:
    """Run a whole script **on the host itself**, with ``env`` exported ahead of it.

    Not :func:`run` with a longer string. A backup script is a hundred lines and its own quoting;
    passing it as an argument means every character in it has to survive two shells, and a long one
    eventually meets ``ARG_MAX``. :mod:`remote_exec` writes it to a private file on the host and
    runs that with stdin closed (0.25.0) - fed on stdin, a ``docker exec -i`` in it ate the rest of
    the script and the shell exited 0 having done nothing, which is why this layer checks a receipt
    rather than an exit code.

    On the host, and deliberately not wrapped into the container: the scripts that use this do
    their own ``docker exec`` because they need to be on the host for the directory the backup is
    written to. ``runtime`` still decides the interpreter — a Windows host gets PowerShell — but
    ``docker``/``k8s`` mean "the host that runs it", not "inside it".
    """
    return _through_remote_exec(
        host, None, lambda live: live.run_script(script, env=env, timeout_seconds=timeout))


def _through_remote_exec(host: Host, session: Any, call: Any) -> dict[str, Any]:
    """``call(session)`` on the host's :mod:`remote_exec` session, answered as this module answers.

    Could-not-run is :class:`HostCommandError`, whichever transport produced it, so a caller does
    not have to know which one it was.
    """
    from db_ops.common import remote_exec

    borrowed = session is not None
    try:
        live = session if borrowed else remote_exec.open_session(access_for(host))
        try:
            result = call(live)
        finally:
            if not borrowed:
                live.close()
    except remote_exec.RemoteExecError as exc:
        raise HostCommandError(str(exc)) from exc
    return {"exit_code": result.exit_code, "stdout": result.stdout, "stderr": result.stderr}
