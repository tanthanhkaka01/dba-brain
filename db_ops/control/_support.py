"""Shared helpers for the db_ops control app: paths, local docker exec, and the
SSH session used to drive the worker host from the master (PC).

The session is a :class:`db_ops.lib.remote_host.RemoteHost` - every command and every file goes
through ``common.cli`` (``run-cmd``, ``push-file``, ``pull-file``). Until 0.24.0 it was a raw
paramiko client out of ``common.ssh``, which made ``control`` an app importing ``common`` (rules
R03). The helpers below kept their names, so the deploy reads as it did.

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
from pathlib import Path

from db_ops.lib.config import DEFAULT_CONFIG_PATH, load_config
from db_ops.lib.paths import TOOL_ROOT
from db_ops.lib.remote_host import RemoteError, RemoteHost
from db_ops.lib.secret_text import redact_key_arguments

# The project root, resolved once in db_ops/lib/paths.py rather than re-derived here from
# __file__ — that idiom answers "where is my config" with "where is my code", which is only
# true for a checkout and the container. Kept under its own name because deploy.py and
# worker_data.py import it; it is an alias, not a second value.
DB_OPS_ROOT = TOOL_ROOT
# db_ops is a standalone repo root; keep REPO_ROOT as an alias so path resolution
# never escapes the project (was DB_OPS_ROOT.parents[1] under the old repo/tools/db_ops layout).
REPO_ROOT = DB_OPS_ROOT
BUNDLE_DIR = DB_OPS_ROOT / "deploy" / "db_ops_deploy"
INIT_PY = DB_OPS_ROOT / "db_ops" / "lib" / "version.py"
IMAGE_TAR_NAME = "db_ops_image.tar"
IMAGE_REPO = "db_ops"
# Where the worker's tool root and its container are, as of 2026-09-16.
#
# **They moved together, and a stale default here does not fail — it lies.** The worker used to be
# `/opt/db_ops` running `db_ops_daemon`, an image built on the master and `docker load`ed from a
# tar. It now runs the published artifact, `ghcr.io/…/dbabrain:<version>`, from `/opt/dbabrain`
# under `docker compose`, so upgrading is a tag and not a build.
#
# Measured the minute after the switch, with these constants still pointing at the old pair:
# `worker-status` answered `container: db_ops_daemon | Exited (0)` — the worker reported **down**
# while it was collecting metrics normally under the new name. `worker-pull-data-config` would
# have carried config back out of the retired folder, which is worse: that stale copy then feeds
# a `--merge` deploy and overwrites the master with it.
#
# The old container is kept stopped as the rollback. Rolling back is `docker start db_ops_daemon`
# on the worker plus reverting this commit — both names still work through `--container` and
# `--remote-dir` on every command that takes them.
DEFAULT_REMOTE_DIR = "/opt/dbabrain"
DEFAULT_CONTAINER = "dbabrain"

#: The retired pair, kept named rather than deleted: `--container`/`--remote-dir` still reach the
#: stopped node, and a reader meeting `/opt/db_ops` on the worker needs to know what it is.
PREVIOUS_REMOTE_DIR = "/opt/db_ops"
PREVIOUS_CONTAINER = "db_ops_daemon"

# The one directory under the deploy root that the deploy must never re-own: the lab DB
# containers keep their data and backup bind mounts here, owned by the database users inside
# them. See the note in control.deploy.copy_bundle.
CONTAINER_DATA_DIR_NAME = "containers"


# --------------------------------------------------------------------------- #
# Version (single source of truth: db_ops/lib/version.py __version__)
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
        from db_ops.lib import data_sources
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
# SSH / file transfer, through common.cli
# --------------------------------------------------------------------------- #
def ssh_connect(host: str, user: str, password: str, port: int = 22) -> RemoteHost:
    """The worker host, reached through ``common.cli``. Nothing is opened here: each command and
    each transfer is its own session, so a login that fails says so on the first call."""
    from db_ops.transport import common_cli

    print(f"Connecting to {user}@{host}:{port} ...", flush=True)
    return RemoteHost(host=host, username=user, password=password or "", port=port,
                      call=common_cli.run_allowing_failure)


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


def ssh_run(client: RemoteHost, command: str, *, sudo: bool = False,
            check: bool = True, quiet: bool = False) -> int:
    """Run one shell line on the worker and show what it printed; the exit code is returned.

    ``sudo`` runs it as root through ``sudo -S``, the login's own password on stdin - never on the
    remote argv. The output arrives when the command ends rather than as it is written: the price
    of going through ``run-cmd``, visible on a long ``docker load`` and nowhere else.
    """
    # Shown, never run, with its passphrases hidden: an operator types `--key-base64 <key>` or
    # `-e DB_OPS_SECRET_KEY="$(echo <key> | base64 -d)"` into a remote line, and this echo and the
    # failure message below printed it back in clear (0.27.0 item 1.92).
    shown = redact_key_arguments(command)
    if not quiet:
        _emit(f"[remote] $ {'sudo ' if sudo else ''}{shown}\n")
    try:
        result = client.run(command, sudo=sudo)
    except RemoteError as exc:
        raise SystemExit(redact_key_arguments(str(exc))) from exc
    if not quiet:
        if result.stdout:
            _emit(result.stdout if result.stdout.endswith("\n") else result.stdout + "\n")
        if result.stderr.strip():
            _emit(result.stderr if result.stderr.endswith("\n") else result.stderr + "\n", err=True)
    if check and result.exit_code != 0:
        raise SystemExit(f"Remote command failed (exit {result.exit_code}): {shown}")
    return result.exit_code


def ssh_capture(client: RemoteHost, command: str) -> tuple[int, str, str]:
    """Run a remote command and return (exit_code, stdout, stderr) without printing."""
    try:
        result = client.run(command)
    except RemoteError as exc:
        raise SystemExit(str(exc)) from exc
    return result.exit_code, result.stdout, result.stderr


def sftp_put_tree(client: RemoteHost, local_dir: Path, remote_dir: str) -> None:
    """Every file under ``local_dir`` to the same place under ``remote_dir``, said as it lands."""
    files = [p for p in Path(local_dir).rglob("*") if p.is_file()]
    total = len(files)
    done = [0]

    def _report(local: Path, _remote: str) -> None:
        done[0] += 1
        rel = local.relative_to(local_dir).as_posix()
        print(f"  [{done[0]}/{total}] {rel} ({local.stat().st_size / (1024 * 1024):.1f} MB)", flush=True)

    try:
        client.put_tree(Path(local_dir), remote_dir, on_file=_report)
    except RemoteError as exc:
        raise SystemExit(str(exc)) from exc


def sftp_get(client: RemoteHost, remote_path: str, local_path: Path) -> None:
    try:
        client.get(remote_path, local_path)
    except RemoteError as exc:
        raise SystemExit(str(exc)) from exc


def sftp_put_files(client: RemoteHost, pairs, *, on_file=None) -> None:
    """Upload named files, creating the directories above them.

    The counterpart of :func:`sftp_put_tree` for a push that names its files instead of a
    directory. The small ones travel as one tar, not a transfer each: a whole ``assets/`` push is a
    few hundred small files. Every transfer is hash-checked at both ends, so a truncated one raises
    here rather than leaving the worker holding half a config file that parses as valid JSON right
    up to where it stops.
    """
    try:
        client.put_files(pairs, on_file=on_file)
    except RemoteError as exc:
        raise SystemExit(str(exc)) from exc
