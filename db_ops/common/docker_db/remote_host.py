"""One SSH connection to an Ubuntu host, shaped as the provisioner's runner and filesystem.

``create-db-docker`` with a ``remote`` login provisions the containers **directly on that Ubuntu
machine** - no intermediate VM, no worker hop. The provisioner is abstracted over a ``runner``
(every docker/compose/health command) and an ``fs`` (instance files);
:class:`RemoteUbuntuHost` implements both faces over one paramiko SSH connection:

* ``run(argv, cwd=...)``  -> executes the command on the remote host, returning a
  ``subprocess.CompletedProcess`` so the provisioner cannot tell it is remote;
* ``exists/mkdirs/write_text/rmtree`` -> SFTP file operations for the instance dir.

**The login arrives resolved** (0.23.0): a host, a port, a username and either the password or
an absolute key path. This used to be ``sre/remote.py`` and could also open a host by its
``db_instances.json`` record and decrypt a ``password_ref``; that half stayed in ``sre``, which
resolves the login and hands it to ``common.cli`` in the request. Nothing here reads a file of
the tool's own. Output meant for a person goes to **stderr**, because stdout is the JSON answer.
"""

from __future__ import annotations

import shlex
import stat
import subprocess
import sys

from db_ops.common.remote_exec import RemoteExecError, SshSession, open_session


class RemoteHostError(RuntimeError):
    """SSH/SFTP-level failure talking to the remote Ubuntu host."""


def _echo(stdout: str, stderr: str) -> None:
    """Mirror subprocess.run's uncaptured behavior - to stderr, which is where a person reads."""
    for text in (stdout, stderr):
        if text:
            print(text, end="" if text.endswith("\n") else "\n", file=sys.stderr, flush=True)


def format_remote_command(argv: list[str], cwd: str | None = None) -> str:
    """Shell-quote ``argv`` for the remote side, optionally prefixed with ``cd <cwd> &&``."""
    command = " ".join(shlex.quote(str(part)) for part in argv)
    if cwd:
        command = f"cd {shlex.quote(str(cwd))} && {command}"
    return command


class RemoteUbuntuHost:
    """One SSH connection to an Ubuntu host, usable as the provisioner's runner AND fs.

    The connection, command execution and SFTP file operations are all
    :class:`db_ops.common.remote_exec.SshSession`; what this class adds is the *shape* the
    provisioner expects — ``subprocess.CompletedProcess`` returns, ``capture_output``
    echoing, and the detached post-start runner below."""

    def __init__(self, host: str, user: str, password: str | None = None, port: int = 22, *,
                 key_filename: str | None = None, timeout: int = 30):
        self.host = host
        self.user = user
        self.port = int(port)
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
        }, resolve_key=False)  # the caller resolved the key path; nothing is looked up here

    @classmethod
    def from_login(cls, login: dict, *, timeout: int = 30) -> "RemoteUbuntuHost":
        """A host from the ``ssh_login`` object a request carries: host, port, username and a
        resolved password or an absolute key file path."""
        if not isinstance(login, dict) or not str(login.get("host") or "").strip():
            raise RemoteHostError("an SSH login needs at least a host.")
        if not str(login.get("username") or "").strip():
            raise RemoteHostError(f"the SSH login for {login.get('host')} names no username.")
        return cls(str(login["host"]).strip(), str(login["username"]).strip(),
                   str(login.get("password") or "") or None, port=int(login.get("port") or 22),
                   key_filename=str(login.get("key_file") or "") or None, timeout=timeout)

    @property
    def session(self) -> SshSession:
        """The open SSH session - what :mod:`db_ops.common.ssh_relay` streams between."""
        return self._session

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
        connections — new log lines are streamed to stderr as they appear. Returns a
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
                print(line, file=sys.stderr, flush=True)
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
    backup_mount: str = "",
) -> dict:
    """Make sure the remote Ubuntu host can run ``docker`` + ``docker compose`` as the SSH user,
    and that every directory a lab writes exists and is the user's — installing Docker over SSH
    if missing.

    ``backup_mount`` is the lab's backup bind mount (``/opt/db_ops/backup`` by default). It is
    prepared here with the containers dir because the provisioner creates it later over SFTP as
    the plain SSH user, which cannot write in a root-owned ``/opt/db_ops``: on 2026-09-24
    ``/spbot_create_db_docker ... install_docker=yes`` failed with *Cannot create /opt/db_ops/backup
    ... Permission denied* from a user with full sudo rights, because only the containers dir had
    ever been prepared with them.

    Steps (all idempotent; a host that already has Docker only gets the group + dir touched):

    1. probe ``docker --version`` and ``docker compose version`` as the SSH user;
    2. if either is missing, install via the official convenience script (``get.docker.com``,
       which includes the compose v2 plugin), falling back to the distro packages
       (``docker.io`` + ``docker-compose-v2``) when ``curl`` is absent — needs root, run through
       ``sudo -S``;
    3. enable + start the docker service, add the SSH user to the ``docker`` group, and create
       the containers dir and the backup mount owned by that user;
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

    # Service up, user in the docker group, every directory a lab writes owned by the user - all
    # via root. The result is checked: it used to be ignored, so a sudo that failed here surfaced
    # only later, as an SFTP "Permission denied" on a directory nobody could explain.
    folders = [folder for folder in (containers_dir, backup_mount) if folder]
    quoted = " ".join(shlex.quote(folder) for folder in folders)
    owner = f"{shlex.quote(host.user)}:"
    # The backup mount is handed over at its TOP only, never recursively. The database engines
    # write their backups beneath it as their own users (SQL Server as uid 10001), and on
    # 2026-09-24 a second lab built on the same host ran `chown -R` over it: every existing backup
    # folder became the SSH user's, and the next LOG and FULL backups failed "Access is denied".
    # The containers directory keeps the recursive chown it always had - it holds the files this
    # tool writes over SFTP.
    chowns = ([f"chown -R {owner} {shlex.quote(containers_dir)}"] if containers_dir else []) + \
             ([f"chown {owner} {shlex.quote(backup_mount)}"] if backup_mount else [])
    prepared = host.run_sudo(
        "systemctl enable --now docker 2>/dev/null || service docker start || true; "
        f"getent group docker >/dev/null || groupadd docker; usermod -aG docker {host.user}; "
        f"mkdir -p {quoted} && " + " && ".join(chowns),
        sudo_password, capture_output=True,
    )
    if prepared.returncode != 0:
        raise RemoteHostError(
            f"Could not prepare {', '.join(folders)} on {host.host} as root for '{host.user}' "
            f"(exit {prepared.returncode}). Detail: {str(prepared.stderr or '').strip()[:300]}"
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
            "containers_dir": containers_dir, "backup_mount": backup_mount}
