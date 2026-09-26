"""Run an arbitrary command inside the worker daemon container from the master.

The command (and its args) is passed through verbatim — nothing is hard-coded — so any
``python -m db_ops.<app>.cli ...`` (or any shell command) can be triggered on the worker:

    python -m db_ops.control.cli worker-run --key-base64 "<key>" -- \
        python -m db_ops.reports.cli inventory-workflow --days 7 --beauty 1

Host/user/SSH-password come from ``config.json`` + the secret store (same ``--key`` as the
other control commands). The command runs as ``docker exec <container> <command...>`` with
each token shell-quoted, so flags like ``--days 7`` reach the in-container command intact.

``--on-host`` runs the command on the worker HOST instead - the compose file, ``docker pull`` and
the bind-mounted folders live there, not in the container. ``--sudo`` runs it with ``sudo``, fed
the same SSH password, so an upgrade needs no interactive ssh session and no stored host key.
"""

from __future__ import annotations

import shlex
import sys

from db_ops.control._support import DEFAULT_CONTAINER, ssh_connect, ssh_run


def run_worker_command(*, host: str, user: str, password: str | None, port: int = 22,
                       container: str = DEFAULT_CONTAINER, command: list[str],
                       on_host: bool = False, sudo: bool = False) -> int:
    # argparse REMAINDER may keep a leading "--" separator — drop it.
    cmd = list(command or [])
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("worker-run: no command given. Put it after the control flags, e.g.:\n"
              "  worker-run --key-base64 \"<key>\" -- python -m db_ops.reports.cli inventory-workflow --days 7 --beauty 1",
              file=sys.stderr)
        return 2

    quoted = " ".join(shlex.quote(t) for t in cmd)
    if on_host:
        # One token is a whole shell line (`cd /opt/dbabrain && docker compose ps`); several are
        # one command and its arguments, quoted as they came.
        remote = cmd[0] if len(cmd) == 1 else quoted
    else:
        remote = "docker exec " + shlex.quote(container) + " " + quoted
    client = ssh_connect(host, user, password, port)
    try:
        print(f"# worker {user}@{host}{' (host)' if on_host else ''}", flush=True)
        # `sudo` puts the line under `sudo -S` with the SSH password on stdin (run-cmd's own).
        return ssh_run(client, remote, check=False, sudo=on_host and sudo)
    finally:
        client.close()
