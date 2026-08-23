"""Shared helpers for the db_ops control app: paths, local docker exec, and the
paramiko SSH/SFTP transport used to drive the worker host from the master (PC).

These were previously the standalone ``scripts/python/db_ops_py`` helpers; they now
live inside the control app so the master-side operations follow the same package
layout as the other db_ops apps.
"""

from __future__ import annotations

import getpass
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from db_ops.common.ssh import SshError, open_ssh_client
from db_ops.config import DEFAULT_CONFIG_PATH, load_config
from db_ops.lib.paths import TOOL_ROOT

# The project root, resolved once in db_ops/lib/paths.py rather than re-derived here from
# __file__ — that idiom answers "where is my config" with "where is my code", which is only
# true for a checkout and the container. Kept under its own name because deploy.py and
# worker_data.py import it; it is an alias, not a second value.
DB_OPS_ROOT = TOOL_ROOT
# db_ops is a standalone repo root; keep REPO_ROOT as an alias so path resolution
# never escapes the project (was DB_OPS_ROOT.parents[1] under the old repo/tools/db_ops layout).
REPO_ROOT = DB_OPS_ROOT
BUNDLE_DIR = DB_OPS_ROOT / "deploy" / "db_ops_deploy"
INIT_PY = DB_OPS_ROOT / "db_ops" / "__init__.py"
IMAGE_TAR_NAME = "db_ops_image.tar"
IMAGE_REPO = "db_ops"
DEFAULT_REMOTE_DIR = "/opt/db_ops"
DEFAULT_CONTAINER = "db_ops_daemon"

# The one directory under the deploy root that the deploy must never re-own: the lab DB
# containers keep their data and backup bind mounts here, owned by the database users inside
# them. See the note in control.deploy.copy_bundle.
CONTAINER_DATA_DIR_NAME = "containers"


# --------------------------------------------------------------------------- #
# Version (single source of truth: db_ops/__init__.py __version__)
# --------------------------------------------------------------------------- #
def read_version() -> str:
    text = INIT_PY.read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not match:
        raise SystemExit(f"Could not read __version__ from {INIT_PY}")
    return match.group(1)


# --------------------------------------------------------------------------- #
# Local docker / shell
# --------------------------------------------------------------------------- #
def run_local(cmd: list[str], *, cwd: Path | None = None) -> None:
    printable = " ".join(cmd)
    print(f"$ {printable}", flush=True)
    result = subprocess.run(cmd, cwd=str(cwd) if cwd else None)
    if result.returncode != 0:
        raise SystemExit(f"Command failed (exit {result.returncode}): {printable}")


def require_docker() -> None:
    if shutil.which("docker") is None:
        raise SystemExit("docker not found on PATH. Install/Start Docker first.")


# --------------------------------------------------------------------------- #
# Cluster (read worker host etc. from config.json master/worker arrays)
# --------------------------------------------------------------------------- #
def default_worker_host(config_path: str | Path | None = None) -> str | None:
    """First worker host from config.json, used as the default --host."""
    try:
        config = load_config(config_path or DEFAULT_CONFIG_PATH)
    except Exception:  # noqa: BLE001 - config is optional for the deploy commands.
        return None
    for node in config.worker:
        if node.host:
            return node.host
    return None


def default_worker_user(config_path: str | Path | None = None) -> str | None:
    """First worker SSH user from config.json (``worker[].user``), used as the default --user."""
    try:
        config = load_config(config_path or DEFAULT_CONFIG_PATH)
    except Exception:  # noqa: BLE001 - config is optional for the deploy commands.
        return None
    for node in config.worker:
        if node.user:
            return node.user
    return None


def default_worker_password_ref(config_path: str | Path | None = None) -> str | None:
    """First worker SSH password ref from config.json (``worker[].password_ref``). The control
    app decrypts this from the secret store with the supplied key instead of prompting."""
    try:
        config = load_config(config_path or DEFAULT_CONFIG_PATH)
    except Exception:  # noqa: BLE001 - config is optional for the deploy commands.
        return None
    for node in config.worker:
        if node.password_ref:
            return node.password_ref
    return None


# --------------------------------------------------------------------------- #
# Passwords / keys
# --------------------------------------------------------------------------- #
def resolve_password(password: str | None, *, host: str, user: str,
                     key_base64: str | None = None, key: str | None = None,
                     password_ref: str | None = None) -> str:
    """Resolve the SSH password. Order: explicit ``--password`` > the secret store
    (``password_ref`` decrypted with the supplied ``--key``/``--key-base64``) > prompt.
    This lets control commands authenticate with only ``--key-base64`` and no ``--password``."""
    if password:
        return password
    if password_ref:
        from db_ops.common import data_sources
        from db_ops.lib.secret_text import SECRET_KEY_ENV_VAR, resolve_cli_key
        try:
            secret_key = resolve_cli_key(key, key_base64)
        except ValueError:
            secret_key = None
        # Fall back to the DB_OPS_SECRET_KEY env the daemon exports to its children,
        # so a daemon-scheduled command works without --key-base64 in command_text.
        if not secret_key:
            secret_key = os.environ.get(SECRET_KEY_ENV_VAR) or None
        if secret_key:
            try:
                secrets = data_sources.load_secret_text(key=secret_key)
            except Exception as exc:  # noqa: BLE001 - wrong key etc.; fall back to prompt.
                print(f"Could not decrypt secret store ({exc}); falling back to prompt.", flush=True)
                secrets = {}
            value = secrets.get(password_ref)
            if value:
                print(f"SSH password resolved from secret '{password_ref}'.", flush=True)
                return value
            if secrets:
                print(f"Secret '{password_ref}' not found/empty; falling back to prompt.", flush=True)
    return getpass.getpass(f"SSH password for {user}@{host}: ")


# --------------------------------------------------------------------------- #
# SSH / SFTP via paramiko
# --------------------------------------------------------------------------- #
def ssh_connect(host: str, user: str, password: str, port: int = 22):
    """Open the control app's SSH connection to the worker host.

    The connect itself (auth rules, error classification) is
    :func:`db_ops.common.ssh.open_ssh_client`; a raw paramiko client comes back because
    ``ssh_run`` below streams output live off the channel, which the request/response
    ``remote_exec`` sessions do not do."""
    print(f"Connecting to {user}@{host}:{port} ...", flush=True)
    try:
        return open_ssh_client(host, user, port=port, password=password, timeout=30, announce=False)
    except SshError as exc:
        raise SystemExit(str(exc)) from exc


def _emit(text: str, *, err: bool = False) -> None:
    """Write remote output to the console without crashing on characters the console
    encoding cannot represent. A Windows master runs a cp1252 console, but remote output
    (compose YAML, a BOM, Vietnamese text) is UTF-8; a bare print() then raises
    UnicodeEncodeError and aborts the whole worker-run. Encode to the console's own
    encoding with errors='replace' so unrepresentable characters become '?' instead."""
    stream = sys.stderr if err else sys.stdout
    encoding = getattr(stream, "encoding", None) or "utf-8"
    stream.write(text.encode(encoding, "replace").decode(encoding, "replace"))
    stream.flush()


def ssh_run(client, command: str, *, sudo_password: str | None = None,
            check: bool = True, quiet: bool = False) -> int:
    if not quiet:
        _emit(f"[remote] $ {command}\n")
    stdin, stdout, stderr = client.exec_command(command, get_pty=False)
    if sudo_password is not None:
        stdin.write(sudo_password + "\n")
        stdin.flush()
    channel = stdout.channel
    out_chunks: list[str] = []
    err_chunks: list[str] = []
    while True:
        while channel.recv_ready():
            chunk = channel.recv(4096).decode("utf-8", errors="replace")
            out_chunks.append(chunk)
            if chunk and not quiet:
                _emit(chunk)
        while channel.recv_stderr_ready():
            chunk = channel.recv_stderr(4096).decode("utf-8", errors="replace")
            err_chunks.append(chunk)
            if chunk and not quiet:
                _emit(chunk, err=True)
        if channel.exit_status_ready():
            break
        time.sleep(0.1)
    while channel.recv_ready():
        chunk = channel.recv(4096).decode("utf-8", errors="replace")
        out_chunks.append(chunk)
        if chunk and not quiet:
            _emit(chunk)
    while channel.recv_stderr_ready():
        chunk = channel.recv_stderr(4096).decode("utf-8", errors="replace")
        err_chunks.append(chunk)
        if chunk and not quiet:
            _emit(chunk, err=True)
    rc = channel.recv_exit_status()
    if check and rc != 0:
        raise SystemExit(f"Remote command failed (exit {rc}): {command}")
    return rc


def ssh_capture(client, command: str) -> tuple[int, str, str]:
    """Run a remote command and return (exit_code, stdout, stderr) without printing."""
    _stdin, stdout, stderr = client.exec_command(command, get_pty=False)
    out = stdout.read().decode("utf-8", errors="replace")
    rc = stdout.channel.recv_exit_status()
    err = stderr.read().decode("utf-8", errors="replace")
    return rc, out, err


def _sftp_mkdirs(sftp, remote_dir: str) -> None:
    current = ""
    for part in [p for p in remote_dir.split("/") if p]:
        current = f"{current}/{part}" if current else f"/{part}"
        try:
            sftp.stat(current)
        except IOError:
            sftp.mkdir(current)


def sftp_put_tree(client, local_dir: Path, remote_dir: str) -> None:
    sftp = client.open_sftp()
    try:
        _sftp_mkdirs(sftp, remote_dir)
        files = [p for p in local_dir.rglob("*") if p.is_file()]
        total = len(files)
        for index, path in enumerate(sorted(files), start=1):
            rel = path.relative_to(local_dir).as_posix()
            target = f"{remote_dir}/{rel}"
            _sftp_mkdirs(sftp, target.rsplit("/", 1)[0])
            size_mb = path.stat().st_size / (1024 * 1024)
            print(f"  [{index}/{total}] {rel} ({size_mb:.1f} MB)", flush=True)
            sftp.put(str(path), target)
    finally:
        sftp.close()


def sftp_get(client, remote_path: str, local_path: Path) -> None:
    sftp = client.open_sftp()
    try:
        sftp.get(remote_path, str(local_path))
    finally:
        sftp.close()
