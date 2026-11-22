"""Hand the worker's daemon its passphrase without leaving it anywhere (0.42.0, item 1.409).

The worker's container used to be started with ``DBABRAIN_SECRET_KEY`` in its environment, and
Docker keeps that in the container's configuration: ``docker inspect`` showed it to the ``docker``
group, and ``config.v2.json`` held it in clear on the host's disk (plan
``audits/20261120_plan_worker_secret_exposure.md``, F1/F2). Now the container names an unlock file
(``DBABRAIN_UNLOCK_FILE``) on a ``tmpfs`` and its daemon starts locked (``dbabrain.jobs.unlock``);
this module is the master's half:

- :func:`unlock_worker` - writes the passphrase into that file through ``docker exec -i``, from a
  script on stdin, and waits for the daemon to take it, check it and say ``unlocked``. A file the
  daemon does not take in time is removed again: a passphrase at rest is what this replaces.
- :func:`keyed_exec_script` - one command in the container with the passphrase in *its*
  environment only, handed over on stdin. ``docker exec`` no longer finds the key in the
  container's configuration, and ``worker-status`` used to put it on the host's argv
  (``--key_base64 <value>``), where ``ps`` showed it to every login.

The passphrase travels base64-encoded inside a script on stdin and is decoded by ``printf`` and
``base64`` in a pipe: on no argv, in no file but the unlock file, and never on the script's first
line, the one line the remote gate echoes.
"""

from __future__ import annotations

import base64
import json
import shlex
import time
from typing import Any, Callable

from dbabrain.lib.secret_text import UNLOCK_FILE_ENV_VAR

#: Where the worker's daemon waits for its passphrase: the host's ``/run`` is a ``tmpfs``, so a
#: reboot empties it, and the same path inside the container. ``deploy-code`` writes these into
#: the worker's compose file; the daemon reads the file's path from ``DBABRAIN_UNLOCK_FILE``.
UNLOCK_DIR = "/run/dbabrain"
UNLOCK_FILE = f"{UNLOCK_DIR}/unlock"

#: How long the master waits for the daemon to take the file. It looks every two seconds, and
#: checking the passphrase costs the store's key derivation (~0.3 s); a fresh container also has
#: to start Python first.
UNLOCK_WAIT_SECONDS = 90


def _encoded(secret_key: str) -> str:
    return base64.b64encode(secret_key.encode("utf-8")).decode("ascii")


def keyed_exec_script(container: str, secret_key: str, command: list[str], *, header: str = "") -> str:
    """A script that runs *command* in *container* with the passphrase in its environment only.

    The encoded key is the first line ``sh -c`` reads on its stdin; the command gets the rest
    (nothing). Its exit code is the script's.
    """
    quoted = " ".join(shlex.quote(token) for token in command)
    inner = ('IFS= read -r k; DBABRAIN_SECRET_KEY="$(printf %s "$k" | base64 -d)"; unset k; '
             'export DBABRAIN_SECRET_KEY; exec "$@"')
    return (f"# {header or 'dbabrain: a command in the container'}\n"
            f"printf '%s\\n' '{_encoded(secret_key)}' | "
            f"docker exec -i {shlex.quote(container)} sh -c {shlex.quote(inner)} sh {quoted}\n")


def keyed_compose_run(service: str, secret_key: str, args: str) -> str:
    """``docker compose run --rm`` of a dbabrain command, the passphrase given on stdin.

    ``-e DBABRAIN_SECRET_KEY`` would put it in that container's configuration on disk while it
    runs - the exposure the unlock file removes from the daemon's container.
    """
    inner = ('IFS= read -r k; DBABRAIN_SECRET_KEY="$(printf %s "$k" | base64 -d)"; unset k; '
             'export DBABRAIN_SECRET_KEY; exec dbabrain "$@"')
    return (f"printf '%s\\n' '{_encoded(secret_key)}' | "
            f"docker compose run --rm --no-deps -T {shlex.quote(service)} sh -c {shlex.quote(inner)} sh {args}")


def unlock_write_script(container: str, secret_key: str) -> str:
    """Write the passphrase into the container's unlock file: 0600, renamed into place whole."""
    inner = (f'f="${UNLOCK_FILE_ENV_VAR}"; [ -n "$f" ] || exit 3; umask 077; mkdir -p "$(dirname "$f")"; '
             'cat > "$f.new" && mv -f "$f.new" "$f"')
    return ("# dbabrain: unlock the worker's daemon\n"
            f"printf %s '{_encoded(secret_key)}' | base64 -d | "
            f"docker exec -i {shlex.quote(container)} sh -c {shlex.quote(inner)}\n")


def _in_container(container: str, inner: str) -> str:
    return f"docker exec {shlex.quote(container)} sh -c {shlex.quote(inner)}"


def read_state(client: Any, container: str) -> dict:
    """The daemon's lock state, as it wrote it; ``{"configured": False}`` for a container that is
    not unlocked through a file, ``{"error": ...}`` when the container cannot be asked."""
    inner = (f'f="${UNLOCK_FILE_ENV_VAR}"; [ -n "$f" ] || exit 3; '
             'printf "%s\\n" "$f"; [ -e "$f" ] && echo pending || echo none; cat "$f.state" 2>/dev/null || true')
    result = client.run(_in_container(container, inner))
    if result.exit_code == 3:
        return {"configured": False}
    if not result.ok:
        return {"configured": None, "error": (result.stderr or result.stdout).strip()[:300]}
    lines = result.stdout.splitlines()
    state: dict = {}
    if len(lines) > 2:
        try:
            state = json.loads(lines[2])
        except ValueError:
            state = {}
    return {"configured": True, "unlock_file": lines[0] if lines else "",
            "file_waiting": len(lines) > 1 and lines[1] == "pending", **state}


def describe(state: dict) -> str:
    """One line for ``worker-status`` and ``unlock-worker``."""
    if state.get("configured") is False:
        return f"not unlocked through a file (no {UNLOCK_FILE_ENV_VAR}): the passphrase is given another way"
    if state.get("configured") is None:
        return f"cannot be read: {state.get('error', '')}"
    name = state.get("state") or "unknown"
    line = f"{name} (since {state.get('at', '?')})"
    if state.get("reason") and name != "unlocked":
        line += f" - {state['reason']}"
    if state.get("error"):
        line += f": {state['error']}"
    return line


def unlock_worker(client: Any, *, container: str, secret_key: str, force: bool = False,
                  wait_seconds: int = UNLOCK_WAIT_SECONDS,
                  sleep: Callable[[float], None] = time.sleep,
                  say: Callable[[str], None] = print) -> int:
    """Hand the daemon in *container* its passphrase; 0 once it says ``unlocked``.

    A daemon that already says ``unlocked`` is left alone unless *force* (a file it will not read
    would sit there): a container just recreated may still show its predecessor's state, which is
    what *force* is for, and what ``deploy-code`` passes.
    """
    if not secret_key:
        say("ERROR: no passphrase to hand over (--key / --key-base64).")
        return 2
    before = read_state(client, container)
    if before.get("configured") is not True:
        say(f"lock: {describe(before)}")
        return 2 if before.get("configured") is False else 1
    if before.get("state") == "unlocked" and not force:
        say(f"lock: {describe(before)} - nothing written.")
        return 0
    result = client.run_script(unlock_write_script(container, secret_key))
    if not result.ok:
        say(f"ERROR: writing the unlock file failed (exit {result.exit_code}): "
            f"{(result.stderr or result.stdout).strip()[:300]}")
        return 1
    deadline = time.monotonic() + max(1, int(wait_seconds))
    seen = before
    while time.monotonic() < deadline:
        sleep(2)
        seen = read_state(client, container)
        moved = {k: seen.get(k) for k in ("state", "at", "pid", "error")} != \
                {k: before.get(k) for k in ("state", "at", "pid", "error")}
        if seen.get("file_waiting") or not moved:
            continue
        if seen.get("state") == "unlocked":
            say(f"lock: {describe(seen)}")
            return 0
        if seen.get("state") == "locked" and seen.get("error"):
            say(f"ERROR: the daemon refused the passphrase - {describe(seen)}")
            return 1
    # Not taken: take it back. A passphrase left in the file is a secret at rest.
    client.run(_in_container(container, f'rm -f "${UNLOCK_FILE_ENV_VAR}" "${UNLOCK_FILE_ENV_VAR}.new"'))
    say(f"ERROR: the daemon did not take the passphrase within {wait_seconds} s; the file was removed. "
        f"lock: {describe(seen)}")
    return 1
