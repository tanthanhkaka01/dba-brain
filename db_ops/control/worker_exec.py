"""Run an arbitrary command inside the worker daemon container from the master.

The command (and its args) is passed through verbatim — nothing is hard-coded — so any
``python -m db_ops.<app>.cli ...`` (or any shell command) can be triggered on the worker:

    python -m db_ops.control.cli worker-run --key-base64 "<key>" -- \
        python -m db_ops.reports.cli inventory-workflow --days 7 --beauty 1

Host/user/SSH-password come from ``config.json`` + the secret store (same ``--key`` as the
other control commands). The command runs as ``docker exec <container> <command...>`` with
each token shell-quoted, so flags like ``--days 7`` reach the in-container command intact.
"""

from __future__ import annotations

import shlex
import sys

from db_ops.control._support import DEFAULT_CONTAINER, ssh_connect, ssh_run


def run_worker_command(*, host: str, user: str, password: str | None, port: int = 22,
                       container: str = DEFAULT_CONTAINER, command: list[str]) -> int:
    # argparse REMAINDER may keep a leading "--" separator — drop it.
    cmd = list(command or [])
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("worker-run: no command given. Put it after the control flags, e.g.:\n"
              "  worker-run --key-base64 \"<key>\" -- python -m db_ops.reports.cli inventory-workflow --days 7 --beauty 1",
              file=sys.stderr)
        return 2

    remote = "docker exec " + shlex.quote(container) + " " + " ".join(shlex.quote(t) for t in cmd)
    client = ssh_connect(host, user, password, port)
    try:
        print(f"# worker {user}@{host}", flush=True)
        return ssh_run(client, remote, check=False)
    finally:
        client.close()
