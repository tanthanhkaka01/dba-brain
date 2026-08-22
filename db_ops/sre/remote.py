"""Run the SRE docker-db provisioner against a remote Ubuntu host over SSH.

``create-db-docker --remote-host <ip> --remote-user <user>`` runs the sre CLI on the
master and provisions the containers **directly on the remote Ubuntu machine** — no
intermediate VM, no worker hop. The provisioner is already fully abstracted over a
``runner`` (every docker/compose/health command) and an ``fs`` (instance files);
:class:`RemoteUbuntuHost` implements both faces over one paramiko SSH connection:

* ``run(argv, cwd=...)``  -> executes the command on the remote host, returning a
  ``subprocess.CompletedProcess`` so the provisioner cannot tell it is remote;
* ``exists/mkdirs/write_text/rmtree`` -> SFTP file operations for the instance dir.

The SSH password is resolved like every other db_ops secret: an explicit value wins,
otherwise a ``password_ref`` decrypted from the encrypted secret store with
``--key``/``--key-base64`` (or ``DB_OPS_SECRET_KEY``). sre owns this module — it does
not import the control app (see docs/13_common.md, "No cross-app imports").
"""

from __future__ import annotations

import shlex
import stat
import subprocess
from pathlib import Path

from db_ops.common.remote_exec import RemoteExecError, SshSession, open_session
from db_ops.common.data_sources import resolve_ssh_key, resolve_ssh_password
from db_ops.lib.ssh_errors import SshError


class RemoteHostError(RuntimeError):
    """SSH/SFTP-level failure talking to the remote Ubuntu host."""


# The SSH key location and the key/password resolvers are `data_sources` — they read
# `data/ssh_keys/` and the encrypted store, and the data folder has one reader.
# Re-exported here for callers that already import them from this module.
__all__ = ["RemoteHostError", "RemoteUbuntuHost", "ensure_docker", "format_remote_command",
           "open_ubuntu_host", "resolve_remote_ssh_password", "resolve_ssh_key"]


def _echo(stdout: str, stderr: str) -> None:
    """Mirror subprocess.run's uncaptured behavior: send both streams to the console."""
    for text in (stdout, stderr):
        if text:
            print(text, end="" if text.endswith("\n") else "\n", flush=True)


def format_remote_command(argv: list[str], cwd: str | None = None) -> str:
    """Shell-quote ``argv`` for the remote side, optionally prefixed with ``cd <cwd> &&``."""
    command = " ".join(shlex.quote(str(part)) for part in argv)
    if cwd:
        command = f"cd {shlex.quote(str(cwd))} && {command}"
    return command


def resolve_remote_ssh_password(
    *,
    password: str | None,
    password_ref: str | None,
    password_env: str | None = None,
    key: str | None = None,
    key_base64: str | None = None,
    data_dir: str | Path | None = None,
) -> str:
    """Thin wrapper over :func:`db_ops.common.data_sources.resolve_ssh_password` (kept for callers that
    import it from here). Password resolution is a read of the encrypted store, so it lives with the data folder's
    one reader and every app shares it."""
    try:
        return resolve_ssh_password(
            password=password, password_ref=password_ref, password_env=password_env,
            key=key, key_base64=key_base64, data_dir=data_dir,
        )
    except SshError as exc:
        raise RemoteHostError(str(exc)) from exc


class RemoteUbuntuHost:
    """One SSH connection to an Ubuntu host, usable as the provisioner's runner AND fs.

    The connection, command execution and SFTP file operations are all
    :class:`db_ops.common.remote_exec.SshSession`; what this class adds is the *shape* the
    provisioner expects — ``subprocess.CompletedProcess`` returns, ``capture_output``
    echoing, and the detached post-start runner below."""

    def __init__(self, host: str, user: str, password: str | None = None, port: int = 22, *,
                 key_filename: str | None = None, timeout: int = 30,
                 session: SshSession | None = None):
        self.host = host
        self.user = user
        self.port = int(port)
        # An already-open session wins: `open_ubuntu_host` builds one from db_instances.json,
        # where the credential is a *reference* the store resolves — there is no password to
        # hand this constructor, and re-decrypting one here would duplicate that resolution.
        if session is not None:
            self._session = session
            return
        if not password and not key_filename:
            raise RemoteHostError("RemoteUbuntuHost needs either a password or an SSH key_filename.")
        self._session: SshSession = open_session({
            "method": "ssh",
            "host": host,
            "port": self.port,
            "username": user,
            "platform": "linux",
            "auth_type": "password" if (password and not key_filename) else "key",
            "key_file": key_filename or None,
            "password": password or "",
            "timeout_seconds": int(timeout),
        }, resolve_key=False)  # the caller already resolved the key path

    # ------------------------------------------------------------------ #
    # Connection plumbing.
    # ------------------------------------------------------------------ #
    def _connect(self):
        try:
            return self._session.client
        except RemoteExecError as exc:
            raise RemoteHostError(str(exc)) from exc

    def _run(self, command: str, *, stdin: str | None = None) -> subprocess.CompletedProcess:
        try:
            result = self._session.run(command, stdin=stdin)
        except RemoteExecError as exc:
            raise RemoteHostError(str(exc)) from exc
        return subprocess.CompletedProcess(
            args=command, returncode=result.exit_code, stdout=result.stdout, stderr=result.stderr
        )

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "RemoteUbuntuHost":
        self._connect()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def reconnect(self) -> None:
        """Drop and reopen the SSH session — used after adding the user to the docker group so
        the new membership takes effect (group changes only apply to a fresh login session)."""
        self.close()
        self._connect()

    def run_sudo(self, command_str: str, sudo_password: str | None, *,
                 capture_output: bool = True) -> subprocess.CompletedProcess:
        """Run a shell command as root via ``sudo -S`` (password on stdin, never on argv).
        ``command_str`` is a shell string executed with ``sh -c``."""
        try:
            result = self._session.run_sudo(command_str, sudo_password=sudo_password)
        except RemoteExecError as exc:
            raise RemoteHostError(str(exc)) from exc
        if not capture_output:
            _echo(result.stdout, result.stderr)
        return subprocess.CompletedProcess(
            args=result.command, returncode=result.exit_code, stdout=result.stdout, stderr=result.stderr
        )

    # ------------------------------------------------------------------ #
    # runner face — signature-compatible with subprocess.run as the
    # provisioner/healthcheck call it (argv, cwd=, capture_output=, text=, check=).
    # ------------------------------------------------------------------ #
    def run(self, argv: list[str], *, cwd: str | None = None, capture_output: bool = False,
            text: bool = True, check: bool = False, **_ignored) -> subprocess.CompletedProcess:
        completed = self._run(format_remote_command(list(argv), cwd))
        if not capture_output:
            # Mirror subprocess.run: without capture the output goes to the console.
            _echo(completed.stdout, completed.stderr)
        # stdout/stderr are always populated (they were read above) — harmless for callers
        # that did not ask for capture, and it keeps error paths informative.
        completed.args = list(argv)
        if check and completed.returncode != 0:
            raise subprocess.CalledProcessError(
                completed.returncode, argv, output=completed.stdout, stderr=completed.stderr
            )
        return completed

    # ------------------------------------------------------------------ #
    # fs face — what the provisioner needs for the instance directory.
    # ------------------------------------------------------------------ #
    def exists(self, path: str) -> bool:
        return self._session.exists(str(path))

    def mkdirs(self, path: str) -> None:
        try:
            self._session.mkdirs(str(path))
        except RemoteExecError as exc:
            raise RemoteHostError(str(exc)) from exc

    def write_text(self, path: str, content: str, *, mode: int | None = None) -> None:
        try:
            self._session.write_text(str(path), content, mode=mode)
        except RemoteExecError as exc:
            raise RemoteHostError(str(exc)) from exc

    def chmod(self, path: str, mode: int) -> None:
        self._session.sftp().chmod(str(path).replace("\\", "/"), mode)

    def _get_sftp(self):
        return self._session.sftp()

    def rmtree(self, path: str) -> None:
        # `rm -rf` over SSH: SFTP has no recursive delete, and the path is one the
        # provisioner itself created under the containers dir.
        self.run(["rm", "-rf", str(path)], capture_output=True)

    def is_dir(self, path: str) -> bool:
        try:
            return stat.S_ISDIR(self._get_sftp().stat(str(path)).st_mode)
        except FileNotFoundError:
            return False

    # ------------------------------------------------------------------ #
    # Long post-start step: run it DETACHED on the remote host and poll.
    # ------------------------------------------------------------------ #
    def run_detached(self, argv: list[str], *, cwd: str, poll_interval: int = 10,
                     timeout: int = 3600, log_name: str = "post_start.log",
                     **_ignored) -> subprocess.CompletedProcess:
        """Run ``argv`` on the remote host in a way that survives this SSH session dropping.

        A multi-minute post-start step (the Oracle Data Guard RMAN duplicate) held over one
        synchronous SSH channel is fragile: if the channel blips the remote command dies with
        SIGHUP. So the command is launched under ``setsid nohup`` writing to ``<cwd>/<log>``
        with its exit code to ``<log>.rc``, and progress is polled over SHORT, independent SSH
        connections — new log lines are streamed to stdout as they appear. Returns a
        ``CompletedProcess`` with the captured log as ``stdout`` and the real exit code."""
        log_path = f"{cwd.rstrip('/')}/{log_name}"
        rc_path = f"{log_path}.rc"
        inner = format_remote_command(list(argv), cwd)
        launch = (f"cd {cwd} && rm -f {log_name} {log_name}.rc && "
                  f"setsid nohup sh -c '{inner}; echo $? > {rc_path}' > {log_path} 2>&1 & echo $!")
        pid = self.run(["bash", "-lc", launch], capture_output=True).stdout.strip().split()[-1]

        import time as _time
        deadline = _time.monotonic() + timeout
        seen = 0
        while True:
            body = self.run(["cat", log_path], capture_output=True).stdout
            lines = body.splitlines()
            for line in lines[seen:]:
                print(line, flush=True)
            seen = len(lines)
            alive = self.run(["kill", "-0", pid], capture_output=True).returncode == 0
            if not alive:
                rc_text = self.run(["cat", rc_path], capture_output=True).stdout.strip()
                returncode = int(rc_text) if rc_text.lstrip("-").isdigit() else 1
                return subprocess.CompletedProcess(args=list(argv), returncode=returncode,
                                                   stdout=body, stderr="")
            if _time.monotonic() >= deadline:
                raise RemoteHostError(
                    f"Post-start step {' '.join(argv)} still running after {timeout}s on {self.host} "
                    f"(pid {pid}); check {log_path}."
                )
            _time.sleep(poll_interval)


def ensure_docker(
    host: "RemoteUbuntuHost",
    *,
    sudo_password: str | None,
    containers_dir: str = "/opt/db_ops/containers",
) -> dict:
    """Make sure the remote Ubuntu host can run ``docker`` + ``docker compose`` as the SSH user,
    and that the containers dir exists and is writable — installing Docker over SSH if missing.

    Steps (all idempotent; a host that already has Docker only gets the group + dir touched):

    1. probe ``docker --version`` and ``docker compose version`` as the SSH user;
    2. if either is missing, install via the official convenience script (``get.docker.com``,
       which includes the compose v2 plugin), falling back to the distro packages
       (``docker.io`` + ``docker-compose-v2``) when ``curl`` is absent — needs root, run through
       ``sudo -S``;
    3. enable + start the docker service, add the SSH user to the ``docker`` group, and create
       the containers dir owned by that user;
    4. reconnect (so the new group membership applies) and re-probe.

    Returns a summary dict. Raises :class:`RemoteHostError` if Docker still is not usable —
    typically because the SSH user has no sudo rights (installing Docker needs root)."""
    def _usable() -> bool:
        return (host.run(["docker", "--version"], capture_output=True).returncode == 0
                and host.run(["docker", "compose", "version"], capture_output=True).returncode == 0)

    already = _usable()
    installed = False
    if not already:
        install = (
            "set -e; export DEBIAN_FRONTEND=noninteractive; "
            "if command -v curl >/dev/null 2>&1; then curl -fsSL https://get.docker.com | sh; "
            "elif command -v wget >/dev/null 2>&1; then wget -qO- https://get.docker.com | sh; "
            "else apt-get update && apt-get install -y docker.io docker-compose-v2; fi"
        )
        result = host.run_sudo(install, sudo_password, capture_output=True)
        if result.returncode != 0:
            raise RemoteHostError(
                f"Docker install failed on {host.host} (exit {result.returncode}). The SSH user "
                f"'{host.user}' likely lacks sudo rights, or the host has no internet. "
                f"Install Docker manually, then re-run. Detail: {result.stderr.strip()[:300]}"
            )
        installed = True

    # Service up, user in the docker group, containers dir owned by the user — all via root.
    host.run_sudo(
        "systemctl enable --now docker 2>/dev/null || service docker start || true; "
        f"getent group docker >/dev/null || groupadd docker; usermod -aG docker {host.user}; "
        f"mkdir -p {containers_dir}; chown -R {host.user}: {containers_dir}",
        sudo_password, capture_output=True,
    )
    # New login session so the docker group membership takes effect for plain `docker` calls.
    host.reconnect()

    if not _usable():
        # Group may need a fully fresh session on some images; fall back to a sudo probe so we
        # can report the real state rather than a misleading permission error.
        sudo_probe = host.run_sudo("docker compose version", sudo_password, capture_output=True)
        raise RemoteHostError(
            f"Docker is installed on {host.host} but not usable as '{host.user}' without sudo "
            f"(group membership may need a fresh session). sudo probe rc={sudo_probe.returncode}. "
            f"Log out/in on the host or add the user to the docker group, then re-run."
        )
    return {"host": host.host, "already_present": already, "installed": installed,
            "containers_dir": containers_dir}


def open_ubuntu_host(target: str, *, data_dir=None, connect_timeout_seconds: int | None = None) -> "RemoteUbuntuHost":
    """A :class:`RemoteUbuntuHost` for a machine ``db_instances.json`` already describes.

    The ``--remote-host/--remote-user/--remote-password-ref`` triple exists for a host that is
    not in the inventory yet — which is the normal case when *creating* an instance on a fresh
    VM. Operating on a host that already runs db_ops containers is the opposite case: the
    ``cmd_access`` block and its credential are there, and asking an operator to retype them is
    how two spellings of the same host end up in two runbooks.

    Resolution is the same three steps ``host_ops.resolve_host`` takes, out of the same files —
    ``data_sources`` for the record and the credentials, ``lib.cmd_access`` for what the block
    means — rather than a call into ``host_ops``, because an app does not import ``common``.
    Nothing here is a second interpretation of the block: the rules live in ``lib``, and both
    callers ask them.
    """
    from db_ops.common.data_sources import load_remote_credentials, resolve_target_instance
    from db_ops.lib.cmd_access import resolve_cmd_access, resolve_cmd_credential, resolve_platform

    text = str(target or "").strip()
    if not text:
        raise RemoteHostError("a target is required (a server_id or an ip from db_instances.json).")
    try:
        instance = resolve_target_instance(text, data_dir=data_dir)
        platform = resolve_platform(instance)
        block = resolve_cmd_access(instance, platform=platform, host=str(instance.get("ip") or ""))
        credential = resolve_cmd_credential(block, load_remote_credentials(data_dir))
    except Exception as exc:  # noqa: BLE001 - every failure here is "cannot reach that host"
        raise RemoteHostError(f"{text}: {exc}") from exc

    if not block or not block.get("enabled", True):
        raise RemoteHostError(
            f"{text} has no usable cmd_access block in db_instances.json, so there is no way to "
            "reach the host. Add one (method ssh plus a credential_name)."
        )
    if platform == "windows" or str(block.get("method") or "") != "ssh":
        method = block.get("method") or "nothing"
        raise RemoteHostError(
            f"{text} is reached by {method} on {platform or 'an unknown platform'}; this needs an "
            "SSH-reachable Linux host, because docker and compose run there."
        )
    if connect_timeout_seconds:
        block = {**block, "timeout_seconds": int(connect_timeout_seconds)}
    try:
        session = open_session(block, credential=credential, data_dir=data_dir)
    except RemoteExecError as exc:
        raise RemoteHostError(f"{text}: {exc}") from exc
    return RemoteUbuntuHost(
        str(block.get("host") or text),
        str(block.get("username") or (credential or {}).get("username") or ""),
        port=int(block.get("port") or 22),
        session=session,
    )
