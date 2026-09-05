"""Single source of truth for **reaching a VM and running a command on it**.

Every app that needs a remote machine — the metrics cmd collectors, the SRE lab
provisioner, backup/restore's Linux targets, the control app driving the worker host —
used to carry its own paramiko connect and its own ``Invoke-Command`` wrapper. Same
three concerns re-implemented four times: *how do I authenticate*, *how do I open the
session*, *how do I run a command and read back rc/stdout/stderr*. This module owns all
three, for both transports:

* **ssh**   — paramiko, opened through :mod:`db_ops.common.ssh` (key or password);
* **winrm** — PowerShell remoting, via ``pypsrp`` when installed, else a local
  ``powershell -Command Invoke-Command`` wrapper (Windows collector hosts);
* **local** — plain ``subprocess`` on this host, so a caller can treat "no remoting"
  as just another method instead of branching.

**Input is a JSON object**, the same shape already stored in ``db_instances.json`` as
``cmd_access`` (plus the ``remote_credentials`` entry), so config travels straight into
the API with no per-app translation::

    access = {
        "method": "ssh",            # ssh | winrm | local
        "host": "192.0.2.5",
        "port": 22,                 # defaults: ssh 22, winrm 5985 (5986 with ssl)
        "username": "ubuntu",       # may instead come from the credential object
        "auth_type": "key",         # ssh: key | password
        "key_file": "worker.key",   # bare name -> data/ssh_keys/, or an absolute path
        "password_ref": "vm_pw",    # secret-store ref (or password_env / password)
        "shell": "bash",            # bash | powershell | cmd
        "timeout_seconds": 30,
    }
    with open_session(access, secrets=secrets) as session:
        result = session.run("systemctl is-active docker")
    result.exit_code, result.stdout, result.stderr

:class:`RemoteResult` is JSON-shaped on the way out too (``result.to_dict()``), and
:meth:`RemoteAccess.to_dict` redacts the password so an access object is always safe to
log. Auth rules and the ``data/ssh_keys/`` location stay in ``common.ssh`` — this module
is the session/execution layer on top of it, never a second copy of it.
"""

from __future__ import annotations

import base64
import os
import shlex
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

from db_ops.lib.coerce import as_text
from db_ops.lib.coerce import as_bool as _as_bool, as_int as _as_int
from db_ops.lib.powershell import (  # noqa: F401 - one definition, see that module
    build_invoke_command_argv,
    build_invoke_command_script,
    encode_powershell_command,
    quote_powershell,
)
from db_ops.lib.shell import POWERSHELL_NOT_FOUND_HINT, powershell_executable
from db_ops.common.ssh import (
    SshAuthError,
    SshConnectError,
    SshError,
    SshTimeoutError,
    open_ssh_client,
    resolve_ssh_key,
)

__all__ = [
    "DEFAULT_SSH_PORT",
    "DEFAULT_SESSION_TIMEOUT_SECONDS",
    "DEFAULT_WINRM_PORT",
    "DEFAULT_WINRM_SSL_PORT",
    "LocalSession",
    "RemoteAccess",
    "RemoteAuthError",
    "RemoteConnectError",
    "RemoteExecError",
    "RemoteResult",
    "RemoteSession",
    "RemoteTimeoutError",
    "SUPPORTED_METHODS",
    "SUPPORTED_SHELLS",
    "SshSession",
    "WinrmSession",
    "build_invoke_command_argv",
    "build_invoke_command_script",
    "open_session",
    "quote_powershell",
    "resolve_secret_value",
    "run_command",
    "run_script",
    "shell_prelude",
]

METHOD_LOCAL = "local"
METHOD_SSH = "ssh"
METHOD_WINRM = "winrm"
SUPPORTED_METHODS = frozenset({METHOD_LOCAL, METHOD_SSH, METHOD_WINRM})

SHELL_BASH = "bash"
SHELL_POWERSHELL = "powershell"
SHELL_CMD = "cmd"
SUPPORTED_SHELLS = frozenset({SHELL_BASH, SHELL_POWERSHELL, SHELL_CMD})

DEFAULT_SSH_PORT = 22
DEFAULT_WINRM_PORT = 5985
DEFAULT_WINRM_SSL_PORT = 5986
#: How long to wait for a remote session to open. Not the command's own deadline — see
#: `command_timeout_seconds`; the two are separate on purpose.
DEFAULT_SESSION_TIMEOUT_SECONDS = 30


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class RemoteExecError(RuntimeError):
    """A remote session or command failed.

    Carries the transport context (``method``/``host``) plus whatever output was read
    before the failure, so a caller can log or re-wrap it without a second round trip.
    """

    def __init__(
        self,
        message: str,
        *,
        method: str = "",
        host: str = "",
        command: str = "",
        exit_code: int | None = None,
        stdout: str = "",
        stderr: str = "",
        duration_seconds: float = 0.0,
    ) -> None:
        super().__init__(message)
        self.method = method
        self.host = host
        self.command = command
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.duration_seconds = duration_seconds


class RemoteAuthError(RemoteExecError):
    """Credentials were rejected by the remote host."""


class RemoteConnectError(RemoteExecError):
    """The host could not be reached (port closed, no route, service down)."""


class RemoteTimeoutError(RemoteExecError):
    """Connect or command exceeded the configured timeout."""


# --------------------------------------------------------------------------- #
# Result
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RemoteResult:
    """One command's outcome, JSON-shaped via :meth:`to_dict`."""

    method: str
    host: str
    command: str
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def check(self) -> "RemoteResult":
        """Return self when the command succeeded, else raise :class:`RemoteExecError`."""
        if self.ok:
            return self
        detail = (self.stderr or self.stdout or "").strip()
        raise RemoteExecError(
            f"Remote command exited with code {self.exit_code} on {self.host or 'local'}"
            + (f": {detail[:500]}" if detail else "."),
            method=self.method,
            host=self.host,
            command=self.command,
            exit_code=self.exit_code,
            stdout=self.stdout,
            stderr=self.stderr,
            duration_seconds=self.duration_seconds,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "host": self.host,
            "command": self.command,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_seconds": self.duration_seconds,
        }

    def to_completed_process(self) -> subprocess.CompletedProcess:
        """Adapt to ``subprocess.CompletedProcess`` for callers written against it."""
        return subprocess.CompletedProcess(
            args=self.command, returncode=self.exit_code, stdout=self.stdout, stderr=self.stderr
        )


# --------------------------------------------------------------------------- #
# Access object (the JSON input)
# --------------------------------------------------------------------------- #
def resolve_secret_value(
    values: dict[str, Any],
    *,
    secrets: dict[str, str] | None = None,
    data_dir: str | Path | None = None,
    value_key: str = "password",
    env_key: str = "password_env",
    ref_key: str = "password_ref",
) -> str:
    """Resolve a secret from a JSON object: explicit value > env var > secret-store ref.

    ``<ref_key>`` is looked up in ``secrets`` first (an already-decrypted store the caller
    passed in), then in the environment (refs double as env var names across db_ops), then
    in the encrypted store on disk. Returns "" when the object names no secret at all —
    key-based SSH auth is a valid no-password case, so an empty result is not an error here.
    """
    explicit = str(values.get(value_key) or "")
    if explicit:
        return explicit
    env_name = str(values.get(env_key) or "").strip()
    if env_name:
        env_value = os.environ.get(env_name, "").strip()
        if env_value:
            return env_value
    ref = str(values.get(ref_key) or "").strip()
    if not ref:
        return ""
    if secrets and ref in secrets:
        return str(secrets[ref])
    env_value = os.environ.get(ref, "").strip()
    if env_value:
        return env_value
    # Last resort: the encrypted store on disk (needs DB_OPS_SECRET_KEY in the env).
    from db_ops.common import data_sources

    try:
        stored = data_sources.load_secret_text(data_dir)
    except Exception as exc:  # noqa: BLE001 - missing key/corrupt store; report the ref.
        raise RemoteExecError(f"Password ref '{ref}' could not be resolved: {exc}") from exc
    value = str(stored.get(ref) or "").strip()
    if not value:
        raise RemoteExecError(
            f"Password ref not found in environment or the secret store: {ref}"
        )
    return value


@dataclass(frozen=True)
class RemoteAccess:
    """A normalized, validated remote-access spec — built from a JSON object.

    Construct with :meth:`from_json`; the raw dict is never used directly so every app
    gets the same defaults (ports, shell per platform, timeout) and the same validation.
    """

    method: str
    host: str = ""
    port: int = 0
    username: str = ""
    platform: str = ""
    shell: str = ""
    auth_type: str = "key"
    key_file: str | None = None
    password: str = field(default="", repr=False)
    # Opening the session. Short by design: a reachable host answers in seconds.
    timeout_seconds: int = DEFAULT_SESSION_TIMEOUT_SECONDS
    # Running a command over that session. None = unbounded, like `ssh host cmd`, because a
    # remote command may legitimately run for an hour. Never defaulted to the connect timeout.
    command_timeout_seconds: int | None = None
    ssl: bool = False
    winrm_auth: str = "negotiate"
    cert_validation: bool = False
    enabled: bool = True

    # ------------------------------------------------------------------ #
    @classmethod
    def from_json(
        cls,
        access: dict[str, Any] | "RemoteAccess" | None,
        *,
        credential: dict[str, Any] | None = None,
        secrets: dict[str, str] | None = None,
        data_dir: str | Path | None = None,
        default_host: str = "",
        default_method: str = "",
        default_timeout_seconds: int = DEFAULT_SESSION_TIMEOUT_SECONDS,
        resolve_key: bool = True,
    ) -> "RemoteAccess":
        """Normalize a ``cmd_access``-shaped JSON object into a :class:`RemoteAccess`.

        ``credential`` is an optional second JSON object (a ``remote_credentials`` entry:
        ``username`` + ``password``/``password_ref``/``password_env``); its values fill in
        anything the access object does not carry itself. ``secrets`` is a decrypted secret
        store used to resolve a ``password_ref`` without touching disk.
        """
        if isinstance(access, RemoteAccess):
            return access
        values: dict[str, Any] = dict(access or {})
        merged: dict[str, Any] = {**(credential or {}), **values}

        method = str(values.get("method") or default_method or "").strip().lower()
        if not method:
            raise RemoteExecError(
                "Remote access needs a 'method' (one of: " + ", ".join(sorted(SUPPORTED_METHODS)) + ")."
            )
        if method not in SUPPORTED_METHODS:
            raise RemoteExecError(
                f"Unsupported remote access method '{method}'. Expected one of: "
                + ", ".join(sorted(SUPPORTED_METHODS))
                + "."
            )

        host = str(values.get("host") or default_host or "").strip()
        if method != METHOD_LOCAL and not host:
            raise RemoteExecError(f"Remote access method '{method}' needs a 'host'.")

        platform = str(values.get("platform") or "").strip().lower()
        ssl = _as_bool(values.get("ssl"), default=False)
        timeout_seconds = _as_int(
            values.get("timeout_seconds"), default=int(default_timeout_seconds or DEFAULT_SESSION_TIMEOUT_SECONDS)
        )
        timeout_seconds = max(1, timeout_seconds)

        if method == METHOD_SSH:
            port = _as_int(values.get("port"), default=DEFAULT_SSH_PORT)
        elif method == METHOD_WINRM:
            port = _as_int(values.get("port"), default=DEFAULT_WINRM_SSL_PORT if ssl else DEFAULT_WINRM_PORT)
        else:
            port = 0

        shell = str(values.get("shell") or "").strip().lower()
        if shell and shell not in SUPPORTED_SHELLS:
            raise RemoteExecError(
                f"Unsupported shell '{shell}'. Expected one of: " + ", ".join(sorted(SUPPORTED_SHELLS)) + "."
            )
        if not shell:
            # WinRM is PowerShell remoting by definition; SSH/local follow the platform.
            shell = SHELL_POWERSHELL if (method == METHOD_WINRM or platform == "windows") else SHELL_BASH

        auth_type = str(values.get("auth_type") or ("password" if method == METHOD_WINRM else "key")).strip().lower()
        username = str(merged.get("username") or "").strip()

        key_file = str(values.get("key_file") or "").strip() or None
        if key_file and method == METHOD_SSH and auth_type != "password" and resolve_key:
            # A bare file name resolves inside data/ssh_keys/; an absolute path is used as-is.
            # Resolved here (not at config load) so a missing key fails only this call.
            try:
                key_file = resolve_ssh_key(key_file, data_dir)
            except SshError as exc:
                raise RemoteExecError(str(exc)) from exc

        password = ""
        if method == METHOD_WINRM or (method == METHOD_SSH and auth_type == "password"):
            password = resolve_secret_value(merged, secrets=secrets, data_dir=data_dir)
        elif method == METHOD_SSH and key_file:
            # Key auth: any password present is the key's passphrase, and it is optional —
            # never fail on a ref that does not resolve, the key alone is usually enough.
            try:
                password = resolve_secret_value(merged, secrets=secrets, data_dir=data_dir)
            except RemoteExecError:
                password = ""

        return cls(
            method=method,
            host=host,
            port=port,
            username=username,
            platform=platform,
            shell=shell,
            auth_type=auth_type,
            key_file=key_file,
            password=password,
            timeout_seconds=timeout_seconds,
            command_timeout_seconds=(
                int(values["command_timeout_seconds"])
                if str(values.get("command_timeout_seconds") or "").strip()
                else None
            ),
            ssl=ssl,
            winrm_auth=str(values.get("auth") or values.get("winrm_auth") or "negotiate").strip() or "negotiate",
            cert_validation=_as_bool(values.get("cert_validation"), default=False),
            enabled=_as_bool(values.get("enabled"), default=True),
        )

    # ------------------------------------------------------------------ #
    @property
    def is_powershell(self) -> bool:
        return self.shell == SHELL_POWERSHELL

    def describe(self) -> str:
        if self.method == METHOD_LOCAL:
            return "local"
        who = f"{self.username}@" if self.username else ""
        return f"{who}{self.host}:{self.port} ({self.method})"

    def to_dict(self) -> dict[str, Any]:
        """JSON view of the access object. **The password is redacted** so the result is
        always safe to log or embed in an event payload."""
        return {
            "method": self.method,
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "platform": self.platform,
            "shell": self.shell,
            "auth_type": self.auth_type,
            "key_file": self.key_file,
            "password": "***" if self.password else "",
            "timeout_seconds": self.timeout_seconds,
            "ssl": self.ssl,
            "winrm_auth": self.winrm_auth,
            "cert_validation": self.cert_validation,
            "enabled": self.enabled,
        }


# `_as_bool` / `_as_int` are `db_ops.lib.coerce.as_bool` / `as_int` since 2026-08-15 — the same
# two functions, moved out when `cmd_access` parsing (which `lib` owns) needed them and could not
# import `common`. The private names are kept as aliases so this module's own call sites read as
# they always did; see the import at the top of the file.


# --------------------------------------------------------------------------- #
# Script/command shaping — shared by every transport
# --------------------------------------------------------------------------- #
def shell_prelude(env: dict[str, str] | None, *, shell: str) -> str:
    """Assignments prepended to a script so the remote shell sees ``env`` as variables.

    The remote side gets no environment from us (SSH's ``AcceptEnv`` is usually closed and
    WinRM starts a fresh session), so values travel inside the script text itself.
    """
    if not env:
        return ""
    lines: list[str] = []
    for name, value in env.items():
        text = str(value)
        if shell == SHELL_POWERSHELL:
            lines.append(f"$env:{name} = '{text.replace(chr(39), chr(39) * 2)}'")
        elif shell == SHELL_CMD:
            lines.append(f"set {name}={text}")
        else:
            lines.append("export {}='{}'".format(name, text.replace("'", "'\\''")))
    return "\n".join(lines) + "\n"


def _script_to_command(script_text: str, *, shell: str) -> tuple[str, bytes | None]:
    """Turn a script body into (remote command line, stdin bytes) for the target shell."""
    if shell == SHELL_POWERSHELL:
        return (
            "powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand "
            + encode_powershell_command(script_text),
            None,
        )
    if shell == SHELL_CMD:
        # cmd.exe has no stdin-script mode worth relying on; join the lines it should run.
        joined = " && ".join(line for line in str(script_text).splitlines() if line.strip())
        return f"cmd.exe /c {joined}", None
    return "bash -s", str(script_text).encode("utf-8")


def _join_command(command: str | Sequence[str], *, cwd: str | None = None) -> str:
    """Render a command (string or argv) as one remote command line, optionally under ``cwd``."""
    if isinstance(command, (list, tuple)):
        text = " ".join(shlex.quote(str(part)) for part in command)
    else:
        text = str(command)
    if cwd:
        text = f"cd {shlex.quote(str(cwd))} && {text}"
    return text


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #
class RemoteSession:
    """A connection to one machine. Subclasses implement the transport.

    Use as a context manager so the connection is closed on every path::

        with open_session(access) as session:
            session.run("uptime").check()
    """

    def __init__(self, access: RemoteAccess) -> None:
        self.access = access

    # -- transport face ------------------------------------------------- #
    def run(
        self,
        command: str | Sequence[str],
        *,
        timeout_seconds: int | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        stdin: str | bytes | None = None,
        check: bool = False,
    ) -> RemoteResult:
        """Run one command on the machine and return its rc/stdout/stderr."""
        raise NotImplementedError

    def run_script(
        self,
        script: str | Path,
        *,
        shell: str | None = None,
        timeout_seconds: int | None = None,
        env: dict[str, str] | None = None,
        check: bool = False,
    ) -> RemoteResult:
        """Ship a whole script (text, or a local file whose text is read) and run it."""
        raise NotImplementedError

    def close(self) -> None:
        return None

    # -- plumbing ------------------------------------------------------- #
    def __enter__(self) -> "RemoteSession":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def _timeout(self, timeout_seconds: int | None) -> int | None:
        """How long this *command* may run — **not** the connect timeout.

        The two are different questions and must not share a number: opening a session to a
        reachable host takes seconds, while the command it then runs can legitimately take
        an hour (`docker compose up` pulling a 1.5 GB image, an RMAN duplicate, a restore).
        Capping commands at the connect timeout silently killed exactly the long operations
        this module exists to drive.

        So a command is **unbounded by default**, matching a plain ``ssh host cmd``. A caller
        that needs a bound passes one explicitly (metrics does, per metric), or sets
        ``command_timeout_seconds`` on the access object.
        """
        if timeout_seconds is not None:
            return max(1, int(timeout_seconds))
        if self.access.command_timeout_seconds:
            return max(1, int(self.access.command_timeout_seconds))
        return None

    def _script_text(self, script: str | Path, *, shell: str, env: dict[str, str] | None) -> str:
        if isinstance(script, Path):
            # utf-8-sig: PowerShell scripts authored on Windows carry a BOM.
            body = script.read_text(encoding="utf-8-sig")
        else:
            body = str(script)
        return shell_prelude(env, shell=shell) + body

    def _result(self, command: str, exit_code: int, stdout: str, stderr: str, started: float) -> RemoteResult:
        return RemoteResult(
            method=self.access.method,
            host=self.access.host,
            command=command,
            exit_code=int(exit_code),
            stdout=stdout,
            stderr=stderr,
            duration_seconds=round(time.monotonic() - started, 3),
        )


class LocalSession(RemoteSession):
    """"Remote" execution on this host — the no-remoting case, same API as the rest."""

    def run(
        self,
        command: str | Sequence[str],
        *,
        timeout_seconds: int | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        stdin: str | bytes | None = None,
        check: bool = False,
    ) -> RemoteResult:
        timeout = self._timeout(timeout_seconds)
        argv: list[str] | str
        if isinstance(command, (list, tuple)):
            argv = [str(part) for part in command]
            shown = _join_command(argv)
        else:
            argv = str(command)
            shown = argv
        process_env = os.environ.copy()
        process_env.update({str(k): str(v) for k, v in (env or {}).items()})
        started = time.monotonic()
        try:
            completed = subprocess.run(
                argv,
                shell=not isinstance(argv, list),
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=process_env,
                input=stdin if isinstance(stdin, str) else (stdin.decode("utf-8") if stdin else None),
            )
        except subprocess.TimeoutExpired as exc:
            raise RemoteTimeoutError(
                f"Local command timed out after {timeout} seconds.",
                method=self.access.method,
                command=shown,
                stdout=as_text(exc.stdout),
                stderr=as_text(exc.stderr),
                duration_seconds=round(time.monotonic() - started, 3),
            ) from exc
        except FileNotFoundError as exc:
            raise RemoteExecError(f"Command not found: {exc}", method=self.access.method, command=shown) from exc
        result = self._result(shown, completed.returncode, completed.stdout or "", completed.stderr or "", started)
        return result.check() if check else result

    def run_script(
        self,
        script: str | Path,
        *,
        shell: str | None = None,
        timeout_seconds: int | None = None,
        env: dict[str, str] | None = None,
        check: bool = False,
    ) -> RemoteResult:
        target_shell = (shell or self.access.shell or SHELL_BASH).lower()
        text = self._script_text(script, shell=target_shell, env=env)
        if target_shell == SHELL_POWERSHELL:
            argv = [
                powershell_executable(),
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-EncodedCommand",
                encode_powershell_command(text),
            ]
            return self.run(argv, timeout_seconds=timeout_seconds, check=check)
        if target_shell == SHELL_CMD:
            return self.run(_script_to_command(text, shell=SHELL_CMD)[0], timeout_seconds=timeout_seconds, check=check)
        return self.run(["bash", "-s"], timeout_seconds=timeout_seconds, stdin=text, check=check)


class SshSession(RemoteSession):
    """One paramiko SSH connection, opened lazily and reused for every command.

    The connection itself (key vs password, no silent agent fallback) is opened by
    :func:`db_ops.common.ssh.open_ssh_client`; this class owns the *session*: running
    commands, shipping scripts, and the SFTP file operations callers need alongside.
    """

    def __init__(self, access: RemoteAccess) -> None:
        super().__init__(access)
        self._client = None
        self._sftp = None

    # -- connection ----------------------------------------------------- #
    @property
    def client(self):
        """The underlying paramiko ``SSHClient`` (connecting on first use).

        Exposed as an escape hatch for callers that need paramiko directly — SFTP tuning,
        ``get_transport().open_session()`` for incremental reads. New code should prefer
        :meth:`run` / :meth:`run_script`.
        """
        return self._connect()

    def _connect(self):
        if self._client is not None:
            return self._client
        access = self.access
        key_filename = access.key_file if access.auth_type != "password" else None
        started = time.monotonic()
        try:
            self._client = open_ssh_client(
                access.host,
                access.username,
                port=access.port or DEFAULT_SSH_PORT,
                password=access.password or None,
                key_filename=key_filename,
                timeout=access.timeout_seconds,
                # Key auth with no key file configured means the host is set up against the
                # agent / the user's default keys; let paramiko find them.
                allow_agent_fallback=not access.password and not key_filename,
                announce=False,
            )
        except SshError as exc:
            raise _classify_ssh_error(exc, access, round(time.monotonic() - started, 3)) from exc
        return self._client

    def close(self) -> None:
        if self._sftp is not None:
            try:
                self._sftp.close()
            except Exception:  # noqa: BLE001 - closing a dead session must not mask the real error.
                pass
            self._sftp = None
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    def reconnect(self) -> None:
        """Drop and reopen the session — needed after a change that only applies to a fresh
        login (adding the user to a group, for instance)."""
        self.close()
        self._connect()

    # -- execution ------------------------------------------------------ #
    def run(
        self,
        command: str | Sequence[str],
        *,
        timeout_seconds: int | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        stdin: str | bytes | None = None,
        check: bool = False,
    ) -> RemoteResult:
        timeout = self._timeout(timeout_seconds)
        text = _join_command(command, cwd=cwd)
        if env:
            text = shell_prelude(env, shell=self.access.shell) + text
        client = self._connect()
        started = time.monotonic()
        try:
            stdin_ch, stdout_ch, stderr_ch = client.exec_command(text, timeout=timeout, get_pty=False)
            if stdin is not None:
                stdin_ch.write(stdin if isinstance(stdin, bytes) else str(stdin).encode("utf-8"))
                flush = getattr(stdin_ch, "flush", None)
                if callable(flush):
                    flush()  # paramiko buffers stdin; a sudo password must land before we wait.
            try:
                stdin_ch.channel.shutdown_write()
            except Exception:  # noqa: BLE001 - already closed by the peer; harmless.
                pass
            stdout = stdout_ch.read().decode("utf-8", errors="replace")
            stderr = stderr_ch.read().decode("utf-8", errors="replace")
            exit_code = stdout_ch.channel.recv_exit_status()
        except (socket.timeout, TimeoutError) as exc:
            raise RemoteTimeoutError(
                f"SSH command timed out after {timeout} seconds on {self.access.host}:{self.access.port}.",
                method=self.access.method,
                host=self.access.host,
                command=text,
                duration_seconds=round(time.monotonic() - started, 3),
            ) from exc
        except Exception as exc:  # noqa: BLE001 - channel/transport failures mid-command.
            raise RemoteExecError(
                f"SSH command execution error on {self.access.host}:{self.access.port}: {exc}",
                method=self.access.method,
                host=self.access.host,
                command=text,
                duration_seconds=round(time.monotonic() - started, 3),
            ) from exc
        result = self._result(text, exit_code, stdout, stderr, started)
        return result.check() if check else result

    def run_script(
        self,
        script: str | Path,
        *,
        shell: str | None = None,
        timeout_seconds: int | None = None,
        env: dict[str, str] | None = None,
        check: bool = False,
    ) -> RemoteResult:
        target_shell = (shell or self.access.shell or SHELL_BASH).lower()
        text = self._script_text(script, shell=target_shell, env=env)
        command, stdin_bytes = _script_to_command(text, shell=target_shell)
        return self.run(command, timeout_seconds=timeout_seconds, stdin=stdin_bytes, check=check)

    def run_sudo(
        self,
        command: str,
        *,
        sudo_password: str | None = None,
        timeout_seconds: int | None = None,
        check: bool = False,
    ) -> RemoteResult:
        """Run a shell command as root via ``sudo -S`` — the password goes over stdin,
        never onto the remote argv where ``ps`` would show it."""
        wrapped = f"sudo -S -p '' sh -c {shlex.quote(str(command))}"
        stdin = f"{sudo_password}\n" if sudo_password else None
        return self.run(wrapped, timeout_seconds=timeout_seconds, stdin=stdin, check=check)

    # -- files (SFTP) --------------------------------------------------- #
    def sftp(self):
        """The lazily-opened SFTP client for this session."""
        if self._sftp is None:
            self._sftp = self._connect().open_sftp()
        return self._sftp

    def exists(self, path: str) -> bool:
        try:
            self.sftp().stat(_posix(path))
            return True
        except FileNotFoundError:
            return False

    def mkdirs(self, path: str) -> None:
        """``mkdir -p`` over SFTP: create every missing component of ``path``."""
        sftp = self.sftp()
        current = ""
        for part in PurePosixPath(_posix(path)).parts:
            current = str(PurePosixPath(current) / part) if current else part
            if current == "/":
                continue
            try:
                sftp.stat(current)
            except FileNotFoundError:
                try:
                    sftp.mkdir(current)
                except OSError as exc:
                    raise RemoteExecError(
                        f"Cannot create {current} on {self.access.host} as {self.access.username}: {exc}. "
                        f"Create the base directory once with sudo and chown it to {self.access.username}.",
                        method=self.access.method,
                        host=self.access.host,
                    ) from exc

    def write_text(self, path: str, content: str, *, mode: int | None = None) -> None:
        posix = _posix(path)
        parent = posix.rsplit("/", 1)[0]
        if parent:
            self.mkdirs(parent)
        with self.sftp().open(posix, "w") as handle:
            handle.write(str(content).encode("utf-8"))
        if mode is not None:
            self.sftp().chmod(posix, mode)

    def put(self, local_path: str | Path, remote_path: str) -> None:
        posix = _posix(remote_path)
        parent = posix.rsplit("/", 1)[0]
        if parent:
            self.mkdirs(parent)
        self.sftp().put(str(local_path), posix)

    def get(self, remote_path: str, local_path: str | Path) -> None:
        self.sftp().get(_posix(remote_path), str(local_path))


#: What a Windows host says when `Invoke-Command` cannot authenticate at all. The local-PowerShell
#: fallback hands these back verbatim as a CLIXML blob, which reads as a problem with the host.
_WINRM_AUTH_MARKERS = ("0x8009030e", "specified logon session does not exist",
                       "Access is denied", "cannot process the request")


def _name_the_missing_backend(result: RemoteResult, *, host: str) -> RemoteResult:
    """Say that this ran without ``pypsrp`` when the fallback could not authenticate.

    The fallback is not equivalent to the pypsrp backend, and until 2026-09-05 nothing said so.
    An install without the ``[winrm]`` extra drives `Invoke-Command` through a local PowerShell,
    and against a WORKGROUP host that fails with `0x8009030e ... A specified logon session does
    not exist` — wording that sends the reader to the host, its credentials and its TrustedHosts
    list, none of which is wrong. Measured that day on one estate: the same command, the same
    credential and the same host answered `exit 0` the moment `pypsrp` was installed, while
    the backup of that instance had been failing for two days and its OS metrics had been
    recording `WARNING: Command exited with code 1` every cycle without anyone reading them as a
    broken transport.

    Only on failure, and only for the markers above: a fallback that works is not worth a word.
    """
    if result.exit_code == 0:
        return result
    haystack = f"{result.stderr or ''}\n{result.stdout or ''}".lower()
    if not any(marker.lower() in haystack for marker in _WINRM_AUTH_MARKERS):
        return result
    # Through `packaging`, never spelled out: the distribution has two names and a hint naming the
    # wrong one sends the reader to a package that is not there. A guard test enforces this.
    from db_ops.lib.packaging import install_hint

    hint = (f"\n[db_ops] WinRM to {host} ran through the local-PowerShell fallback because "
            "`pypsrp` is not installed, and that backend could not authenticate. Install the "
            f"WinRM extra (`{install_hint('winrm')}`) and try again before treating this as a "
            "problem with the host.")
    return replace(result, stderr=(result.stderr or "") + hint)


class WinrmSession(RemoteSession):
    """PowerShell remoting to a Windows host.

    Two backends, picked at run time: ``pypsrp`` when it is installed (works from Linux
    too), otherwise a local ``powershell -Command Invoke-Command`` wrapper, which needs a
    PowerShell on this host. There is no persistent connection to hold, so each call opens
    its own — ``close()`` is a no-op and the session is safe to keep around.
    """

    def run(
        self,
        command: str | Sequence[str],
        *,
        timeout_seconds: int | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        stdin: str | bytes | None = None,
        check: bool = False,
    ) -> RemoteResult:
        text = _join_command(command, cwd=None)
        if cwd:
            text = f"Set-Location {quote_powershell(cwd)}; {text}"
        if self.access.shell == SHELL_CMD:
            text = f"& cmd.exe /c {quote_powershell(text)}"
        return self.run_script(text, shell=SHELL_POWERSHELL, timeout_seconds=timeout_seconds, env=env, check=check)

    def run_script(
        self,
        script: str | Path,
        *,
        shell: str | None = None,
        timeout_seconds: int | None = None,
        env: dict[str, str] | None = None,
        check: bool = False,
    ) -> RemoteResult:
        # WinRM is PowerShell remoting; a bash script cannot run over it.
        text = self._script_text(script, shell=SHELL_POWERSHELL, env=env)
        # Both backends need a real number here (pypsrp's connection_timeout, subprocess's
        # timeout), so an unbounded command falls back to the connect timeout — WinRM has no
        # way to express "wait forever" that is safe to leave on by default.
        timeout = self._timeout(timeout_seconds) or self.access.timeout_seconds
        try:
            from pypsrp.client import Client  # type: ignore[import-not-found]
        except ImportError:
            result = self._run_via_local_powershell(text, timeout)
            result = _name_the_missing_backend(result, host=self.access.host)
        else:
            result = self._run_via_pypsrp(Client, text, timeout)
        return result.check() if check else result

    # ------------------------------------------------------------------ #
    def _run_via_pypsrp(self, client_cls, script_text: str, timeout: int) -> RemoteResult:
        access = self.access
        # Basic auth over plain HTTP cannot negotiate message encryption; anything else can.
        encryption = "never" if (access.winrm_auth.lower() == "basic" and not access.ssl) else "auto"
        started = time.monotonic()
        outcome: dict[str, Any] = {}

        def _execute() -> None:
            try:
                client = client_cls(
                    access.host,
                    username=self._winrm_username(),
                    password=access.password,
                    ssl=access.ssl,
                    port=access.port,
                    auth=access.winrm_auth,
                    encryption=encryption,
                    connection_timeout=timeout,
                    cert_validation=access.cert_validation,
                )
                outcome["result"] = client.execute_ps(script_text)
            except BaseException as exc:  # noqa: BLE001 - carried across the thread, re-raised below.
                outcome["error"] = exc

        # `timeout` has to be enforced here, by wall clock. None of pypsrp's timeouts bound how
        # long a *command* may run: `connection_timeout`/`read_timeout` bound one HTTP round trip
        # and `operation_timeout` bounds one WSMan Receive, so `execute_ps` simply polls in a loop
        # for as long as the remote script keeps running. A metric declaring `timeout: 60` was
        # measured holding the collector for 490 seconds against an unresponsive host, and because
        # the pass is one queue that stalled every other server's metrics behind it — including the
        # lock metrics that should have raised CRITICAL. The subprocess fallback below never had
        # this hole (`subprocess.run(timeout=...)` bounds it), so only the pypsrp path needs the guard.
        #
        # The thread is a daemon and is abandoned rather than joined on timeout: pypsrp offers no
        # cancel, and waiting for it would reintroduce exactly the stall being fixed. It dies with
        # the process, and cannot hold up interpreter exit.
        worker = threading.Thread(target=_execute, name=f"winrm-{access.host}", daemon=True)
        worker.start()
        worker.join(timeout)
        if worker.is_alive():
            raise RemoteTimeoutError(
                f"WinRM command timed out after {timeout} seconds on {access.host}.",
                method=access.method,
                host=access.host,
                duration_seconds=round(time.monotonic() - started, 3),
            )
        error = outcome.get("error")
        if error is not None:
            raise RemoteExecError(
                f"WinRM execution failed on {access.host}:{access.port}: {error}",
                method=access.method,
                host=access.host,
                duration_seconds=round(time.monotonic() - started, 3),
            ) from error
        stdout, stderr, exit_code = outcome["result"]
        return self._result("<powershell script>", int(exit_code or 0),
                            as_text(stdout), _ps_streams_text(stderr), started)

    def _run_via_local_powershell(self, script_text: str, timeout: int) -> RemoteResult:
        """Fallback: drive ``Invoke-Command`` from a PowerShell on this host.

        The credential and the script both travel through environment variables rather than
        the command line, so neither the password nor the script body is visible in the
        process table on this host.
        """
        access = self.access
        wrapper = _INVOKE_COMMAND_WRAPPER.format(
            username=quote_powershell(self._winrm_username()),
            host=quote_powershell(access.host),
            port=access.port,
            auth=quote_powershell(_powershell_auth(access.winrm_auth)),
            use_ssl="$true" if access.ssl else "$false",
        )
        env = os.environ.copy()
        env["DB_OPS_WINRM_PASSWORD"] = access.password
        env["DB_OPS_WINRM_SCRIPT_B64"] = encode_powershell_command(script_text)
        started = time.monotonic()
        try:
            completed = subprocess.run(
                [
                    powershell_executable(),
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-EncodedCommand",
                    encode_powershell_command(wrapper),
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise RemoteTimeoutError(
                f"WinRM command timed out after {timeout} seconds on {access.host}.",
                method=access.method,
                host=access.host,
                stdout=as_text(exc.stdout),
                stderr=as_text(exc.stderr),
                duration_seconds=round(time.monotonic() - started, 3),
            ) from exc
        except FileNotFoundError as exc:
            raise RemoteExecError(
                f"WinRM needs either pypsrp or a local PowerShell. {POWERSHELL_NOT_FOUND_HINT}",
                method=access.method,
                host=access.host,
            ) from exc
        return self._result(
            "Invoke-Command", completed.returncode, completed.stdout or "", completed.stderr or "", started
        )

    def _winrm_username(self) -> str:
        """Qualify a bare username with the host, so a local account authenticates as
        ``HOST\\user`` instead of being resolved against this machine's domain."""
        username = self.access.username
        if not username or "\\" in username or "@" in username:
            return username
        return f"{self.access.host}\\{username}"


# Kept as a module constant so the remoting shape is readable in one place.
_INVOKE_COMMAND_WRAPPER = """
$ErrorActionPreference = 'Stop'
$password = ConvertTo-SecureString $env:DB_OPS_WINRM_PASSWORD -AsPlainText -Force
$credential = [System.Management.Automation.PSCredential]::new({username}, $password)
$scriptText = [System.Text.Encoding]::Unicode.GetString([System.Convert]::FromBase64String($env:DB_OPS_WINRM_SCRIPT_B64))
$scriptBlock = [ScriptBlock]::Create($scriptText)
$params = @{{
    ComputerName = {host}
    Port = {port}
    Credential = $credential
    Authentication = {auth}
}}
if ({use_ssl}) {{
    $params.UseSSL = $true
    $params.SessionOption = New-PSSessionOption -SkipCACheck -SkipCNCheck -SkipRevocationCheck
}}
Invoke-Command @params -ScriptBlock $scriptBlock
"""


# `build_invoke_command_script` / `build_invoke_command_argv` are `db_ops.lib.powershell`
# since 2026-08-15: building the script is not an execution, and `backup_restore` was
# importing this whole transport to get one. Re-exported at the top of this file.


def _powershell_auth(auth: str) -> str:
    normalized = str(auth or "").strip().lower()
    if normalized in {"basic", "credssp", "digest", "kerberos", "negotiate"}:
        return normalized.capitalize()
    return "Default"


def _classify_ssh_error(exc: SshError, access: RemoteAccess, duration: float) -> RemoteExecError:
    """Re-raise a ``common.ssh`` connect failure as the matching ``remote_exec`` error.

    ``open_ssh_client`` already made the auth-vs-unreachable call from the paramiko
    exception type; this only carries that verdict — and the transport context — across
    the module boundary.
    """
    kwargs: dict[str, Any] = {"method": access.method, "host": access.host, "duration_seconds": duration}
    if isinstance(exc, SshAuthError):
        return RemoteAuthError(f"{exc} (auth_type={access.auth_type}).", **kwargs)
    if isinstance(exc, SshTimeoutError):
        return RemoteTimeoutError(str(exc), **kwargs)
    if isinstance(exc, SshConnectError):
        return RemoteConnectError(str(exc), **kwargs)
    return RemoteExecError(str(exc), **kwargs)


def _posix(path: str | Path) -> str:
    return str(path).replace("\\", "/")


# `_text` is `db_ops.lib.coerce.as_text` since 2026-08-16 — the same five lines were here
# and in the other of `common`/`metrics`.


def _ps_streams_text(value: Any) -> str:
    """The error text out of what ``pypsrp.execute_ps`` returns as its second value.

    That value is a ``PSDataStreams`` object, not a string. Passing it through :func:`_text` gave
    ``<pypsrp.powershell.PSDataStreams object at 0x...>`` — so every failing PowerShell script run
    over WinRM reported an object address instead of the reason it failed. The metrics collectors
    have been showing that for as long as WinRM has been in use; it is the sort of message that
    reads as "the tool is broken" and hides that the host said something useful.

    Warnings are left out. They are frequent and benign here, and folding them into stderr would
    turn every script that prints one into an apparent failure at the layers above.
    """
    if value in (None, ""):
        return ""
    records = getattr(value, "error", None)
    if records is None:
        return as_text(value)
    return "\n".join(as_text(record).strip() for record in records if as_text(record).strip())


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #
def open_session(
    access: dict[str, Any] | RemoteAccess,
    *,
    credential: dict[str, Any] | None = None,
    secrets: dict[str, str] | None = None,
    data_dir: str | Path | None = None,
    **normalize_kwargs: Any,
) -> RemoteSession:
    """Open a session to the machine described by the ``access`` JSON object.

    The session is a context manager; use it when you have several commands to run so they
    share one connection. For a single command, :func:`run_command` is the one-liner.
    """
    resolved = RemoteAccess.from_json(
        access, credential=credential, secrets=secrets, data_dir=data_dir, **normalize_kwargs
    )
    if resolved.method == METHOD_SSH:
        return SshSession(resolved)
    if resolved.method == METHOD_WINRM:
        return WinrmSession(resolved)
    assert_local_host(getattr(resolved, "host", ""), method=resolved.method)
    return LocalSession(resolved)


# Hosts that mean "this machine" — anything else with method=local is a config mistake.
_LOCAL_HOSTS = {"", "localhost", "127.0.0.1", "::1", "0.0.0.0", "local", "."}


def is_local_host(host: str) -> bool:
    """True when ``host`` names the machine this process runs on."""
    value = str(host or "").strip().lower()
    return value in _LOCAL_HOSTS or value == str(socket.gethostname()).strip().lower()


def assert_local_host(host: str, *, method: str = METHOD_LOCAL) -> None:
    """Refuse ``method: local`` when ``host`` names a different machine.

    ``local`` runs the command right here — inside the worker container. Pointed at a remote
    host it does not fail: it succeeds and returns **this machine's** numbers under that host's
    name. That is worse than an outage, because the target looks monitored and healthy.

    It happened: ``ACME-192-0-2-111`` was declared Ubuntu 22.04 with ``method: local``, and its
    OS metrics reported the db_ops image (Ubuntu 24.04) with a hostname of ``dbcd646aaa1c`` —
    a container id, different on every run, because `docker compose run` makes a new container
    each time. CPU, memory, disk and uptime for that server were the collector's own for months.
    The only visible symptom was `OS_EVENTLOG_CRITICAL` complaining that journalctl was missing,
    which is true of the container and irrelevant to the host.

    Failing loudly turns silently wrong data into a config error someone can fix.
    """
    if is_local_host(host):
        return
    clean = str(host or "").strip()
    raise RemoteExecError(
        f"cmd_access.method='local' but host={clean!r} is another machine. Local execution would "
        f"report THIS host's CPU/memory/disk under {clean!r}. Use method='ssh' (Linux) or "
        f"'winrm' (Windows) with a credential, or clear host if the target really is this node.",
        method=method,
        command="",
    )


def run_command(
    access: dict[str, Any] | RemoteAccess,
    command: str | Sequence[str],
    *,
    credential: dict[str, Any] | None = None,
    secrets: dict[str, str] | None = None,
    data_dir: str | Path | None = None,
    timeout_seconds: int | None = None,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    check: bool = False,
    **normalize_kwargs: Any,
) -> RemoteResult:
    """Run one command on the machine described by ``access`` and close the connection."""
    with open_session(
        access, credential=credential, secrets=secrets, data_dir=data_dir, **normalize_kwargs
    ) as session:
        return session.run(command, timeout_seconds=timeout_seconds, env=env, cwd=cwd, check=check)


def run_script(
    access: dict[str, Any] | RemoteAccess,
    script: str | Path,
    *,
    credential: dict[str, Any] | None = None,
    secrets: dict[str, str] | None = None,
    data_dir: str | Path | None = None,
    shell: str | None = None,
    timeout_seconds: int | None = None,
    env: dict[str, str] | None = None,
    check: bool = False,
    **normalize_kwargs: Any,
) -> RemoteResult:
    """Ship a script (text or a local file) to the machine described by ``access`` and run it."""
    with open_session(
        access, credential=credential, secrets=secrets, data_dir=data_dir, **normalize_kwargs
    ) as session:
        return session.run_script(
            script, shell=shell, timeout_seconds=timeout_seconds, env=env, check=check
        )
