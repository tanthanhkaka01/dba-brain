"""A daemon whose passphrase is handed over in a file, read once and removed (0.42.0, item 1.409).

The worker's daemon used to receive the passphrase as ``DBABRAIN_SECRET_KEY`` in its container's
environment, and Docker keeps a container's environment in its configuration: ``docker inspect``
printed it to the ``docker`` group, and the container's ``config.v2.json`` held it in clear on the
host's disk - through reboots, in every backup or snapshot of the VM (plan
``audits/20261120_plan_worker_secret_exposure.md``, F1 and F2).

So a node can name a file instead - ``DBABRAIN_UNLOCK_FILE``, a path in a ``tmpfs`` - and start
**locked**: it opens nothing, runs nothing and waits. The master writes the passphrase into that
file (``control unlock-worker``); the daemon reads it, checks that it opens the secret store,
**removes the file**, and from then on holds the passphrase in its own memory only. A restart of
the container or a reboot of the host loses it by design, and the daemon waits again: nothing that
opens the store is ever at rest on the worker (the operator, 2026-11-20: *after a reboot the master
unlocks*).

A state file beside it (``<unlock file>.state``) says which: ``locked`` since when and why (a wrong
passphrase is refused and said there), or ``unlocked``. It holds no secret. It is how
``unlock-worker`` and ``worker-status`` see a locked daemon - the Telegram token is in the store a
locked daemon cannot open, so it cannot say so itself.

A node that does not set ``DBABRAIN_UNLOCK_FILE`` is unchanged: the passphrase comes from
``--key``/``--key-base64`` or ``DBABRAIN_SECRET_KEY`` as before.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from dbabrain.lib import errors
from dbabrain.lib.json_io import atomic_write_text
from dbabrain.lib.secret_text import UNLOCK_FILE_ENV_VAR, load_secret_text

#: How often a locked daemon looks for the file. The unlock is a person at the master waiting for
#: an answer; two seconds is short for them and nothing for the host.
POLL_SECONDS = 2


def unlock_file_from_env() -> Path | None:
    """The file this node is unlocked through, or ``None`` when it is not unlocked that way."""
    value = os.environ.get(UNLOCK_FILE_ENV_VAR, "").strip()
    return Path(value) if value else None


def state_path(unlock_file: Path) -> Path:
    return unlock_file.with_name(unlock_file.name + ".state")


def write_state(unlock_file: Path, state: str, **fields: object) -> None:
    record = {"state": state, "pid": os.getpid(),
              # To the microsecond: the master tells a new state from the one it read before it
              # wrote the file by this field, and two writes can fall in one second.
              "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"), **fields}
    try:
        atomic_write_text(state_path(unlock_file), json.dumps(record) + "\n")
    except OSError:
        # The state is for the reader at the master; a daemon that cannot write it still unlocks.
        pass


def read_state(unlock_file: Path) -> dict:
    try:
        return json.loads(state_path(unlock_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def take(unlock_file: Path) -> str | None:
    """The passphrase in *unlock_file*, the file removed; ``None`` while there is none.

    Removed whatever it held - a wrong passphrase included: it is refused by the caller, and a
    file left in place would be a secret at rest, the one thing this exists to prevent. Only the
    line ending is stripped; a passphrase may begin or end with a space.
    """
    try:
        raw = unlock_file.read_bytes()
    except FileNotFoundError:
        return None
    finally:
        try:
            unlock_file.unlink()
        except FileNotFoundError:
            pass
    return raw.decode("utf-8").rstrip("\r\n")


def opens_the_store(key: str, data_dir: Path) -> str:
    """Empty when *key* decrypts the node's secret store (or there is none yet); else why not."""
    try:
        load_secret_text(data_dir, key=key)
    except (errors.InvalidConfig, errors.NotConfigured) as exc:
        return str(exc)
    return ""


def wait_for_unlock(unlock_file: Path, *, data_dir: Path,
                    on_event: Callable[[str, dict], None] | None = None,
                    sleep: Callable[[float], None] = time.sleep,
                    poll_seconds: float = POLL_SECONDS) -> str:
    """Block until a passphrase that opens the store arrives in *unlock_file*; return it.

    A stop signal while waiting leaves through the caller's handler; the state then says
    ``stopped``, so a reader does not take a daemon that is gone for one still waiting.
    """
    def _event(name: str, **fields: object) -> None:
        if on_event:
            on_event(name, dict(fields))

    write_state(unlock_file, "locked", reason="waiting for the passphrase")
    _event("app.daemon.locked", unlock_file=str(unlock_file))
    unlocked = False
    try:
        while True:
            key = take(unlock_file)
            if key:
                why = opens_the_store(key, data_dir)
                if not why:
                    write_state(unlock_file, "unlocked")
                    _event("app.daemon.unlocked", unlock_file=str(unlock_file))
                    unlocked = True
                    return key
                write_state(unlock_file, "locked", reason="the passphrase given does not open the store",
                            error=why)
                _event("app.daemon.unlock_refused", error=why)
            elif key == "":
                write_state(unlock_file, "locked", reason="the unlock file was empty")
            sleep(poll_seconds)
    finally:
        if not unlocked:
            write_state(unlock_file, "stopped")
