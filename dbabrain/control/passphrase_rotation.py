"""Change the secret store's passphrase on the master and the worker, in one run (0.42.0, item 1.411).

The operator, 2026-11-21 (translated): *change the worker's key; the secrets need not change*; *do not
update the important data and lose its trace - set the old one inactive, then make the new one active*;
*for the secret store, keep no old copy*. Everything the passphrase opens moves together, or nothing
moves:

1. **Plan** (always, and all a run without ``--apply`` does): the master's store and the worker's store
   open with the old passphrase; every active envelope in the runtime store (age keys, backup
   certificates) opens with it. A thing that does not open stops the run before anything is touched.
2. **Stop the worker's container** - a daemon holding the old passphrase must not run a job while its
   stores change under it (a backup in flight gets ``docker compose stop``'s 120 s, as a deploy does).
3. **The runtime store**: both tables re-sealed in one transaction, old rows retired
   (:mod:`dbabrain.db.passphrase_rotation`).
4. **The master's secret store** sealed with the new passphrase, read back.
5. **The worker's secret store** fetched again (the container is stopped, so it is final), sealed with the
   new passphrase here, written there whole, fetched once more and opened with the new one.
6. **Start the container** - it starts locked - and hand it the new passphrase (``unlock-worker``); the
   daemon accepts it only if it opens the worker's store.
7. The runtime store planned again with the new passphrase: every active envelope opens.

A step that fails says which step and what state everything is in; nothing is retried behind the
operator's back. The plaintext source on the master (``secrets/secret_text.json``) holds no passphrase
and is not touched.
"""

from __future__ import annotations

import json
import shlex
import tempfile
from pathlib import Path
from typing import Any, Callable

from dbabrain.control import worker_unlock
from dbabrain.control._support import DEFAULT_CONTAINER, DEFAULT_REMOTE_DIR, ssh_connect, ssh_run
from dbabrain.lib import secret_text
from dbabrain.lib.config import DEFAULT_CONFIG_PATH, load_config
from dbabrain.lib.paths import DEFAULT_DATA_DIR

REASON = "passphrase rotated (0.42.0, 1.411)"


class RotationRefused(SystemExit):
    """A refusal: nothing was changed, or the run stopped at the step it names."""


def _store_target(config_path: str | Path, key: str):
    from dbabrain.db.backend import StoreTarget

    return StoreTarget.from_config(load_config(config_path), key=key)


def rotate_passphrase(*, host: str, user: str, password: str, port: int = 22, old_key: str, new_key: str,
                      remote_dir: str = DEFAULT_REMOTE_DIR, container: str = DEFAULT_CONTAINER,
                      apply: bool = False, config_path: str | Path = DEFAULT_CONFIG_PATH,
                      data_dir: str | Path = DEFAULT_DATA_DIR, say: Callable[[str], None] = print) -> int:
    from dbabrain.db import passphrase_rotation as store_rotation

    if not old_key or not new_key:
        raise RotationRefused("both the old passphrase (--key / --key-base64) and the new one "
                              "(--new-key-base64) are required")
    if old_key == new_key:
        raise RotationRefused("the new passphrase is the old one")
    master_store = Path(data_dir) / secret_text.ENCRYPTED_SECRET_TEXT_FILENAME
    worker_store = f"{remote_dir.rstrip('/')}/data/{secret_text.ENCRYPTED_SECRET_TEXT_FILENAME}"

    # 1. Plan - read only.
    master_count = len(secret_text.load_secret_text(data_dir, key=old_key))
    target = _store_target(config_path, old_key)
    envelopes = store_rotation.plan(target, old_passphrase=old_key)
    client = ssh_connect(host, user, password, port)
    try:
        worker_text = _fetch(client, worker_store)
        worker_count = len(secret_text.decrypt_secret_text(json.loads(worker_text), old_key))
        say(f"master store  {master_store}: {master_count} secrets open with the old passphrase")
        say(f"worker store  {host}:{worker_store}: {worker_count} secrets open with the old passphrase")
        for table, facts in envelopes.items():
            say(f"runtime store {table}: {facts['active']} active (re-sealed as new rows, the old retired), "
                f"{facts['retired']} retired already, {len(facts['not_opening'])} not opening")
        refused = {t: f["not_opening"] for t, f in envelopes.items() if f["not_opening"]}
        if refused:
            raise RotationRefused(f"rows that do not open with the old passphrase: {refused} - nothing was changed")
        if not apply:
            say("PLAN ONLY - nothing changed. --apply would: stop the worker's container; re-seal the runtime "
                "store's envelopes (one transaction); seal the master's and the worker's secret stores with "
                "the new passphrase; start the container and unlock it with the new passphrase.")
            return 0

        step = "2. stop the worker's container"
        try:
            say(f"\n{step}")
            ssh_run(client, f"cd {shlex.quote(remote_dir)} && docker compose stop -t 120")
            step = "3. re-seal the runtime store's envelopes"
            say(step)
            done = store_rotation.reseal_all(target, old_passphrase=old_key, new_passphrase=new_key, reason=REASON)
            for table, rows in done.items():
                say(f"  {table}: " + (", ".join(f"{r['old_id']} -> {r['new_id']}" for r in rows) or "none"))
            step = "4. seal the master's secret store with the new passphrase"
            say(step)
            say(f"  {secret_text.rekey_secret_text_file(master_store, old_key=old_key, new_key=new_key)} secrets")
            step = "5. seal the worker's secret store with the new passphrase"
            say(step)
            sealed, count = secret_text.rekey_secret_text(_fetch(client, worker_store), old_key=old_key,
                                                         new_key=new_key)
            client.put_bytes(sealed.encode("utf-8"), worker_store)
            secret_text.decrypt_secret_text(json.loads(_fetch(client, worker_store)), new_key)
            say(f"  {count} secrets, read back with the new passphrase")
            step = "6. start the container and unlock it with the new passphrase"
            say(step)
            ssh_run(client, f"cd {shlex.quote(remote_dir)} && docker compose up -d")
            if worker_unlock.unlock_worker(client, container=container, secret_key=new_key, force=True, say=say) != 0:
                raise RotationRefused("the worker did not accept the new passphrase")
        except BaseException as exc:
            say(f"\nSTOPPED at step {step}: {exc}")
            say("State: steps before it are done; this one and those after are not. The runtime store's rows "
                "change in one transaction (all or none). The worker's container may be stopped - nothing "
                "was retried. Read the state before running anything again.")
            raise
    finally:
        client.close()

    # 7. Every active envelope opens with the new passphrase.
    after = store_rotation.plan(_store_target(config_path, new_key), old_passphrase=new_key)
    for table, facts in after.items():
        say(f"runtime store {table}: {facts['active']} active, {facts['retired']} retired, "
            f"{len(facts['not_opening'])} not opening with the NEW passphrase")
    return 0 if not any(f["not_opening"] for f in after.values()) else 1


def _fetch(client: Any, remote: str) -> str:
    """The remote file's text, through a private temporary file removed at once (it is the encrypted
    store - still, no copy of it is left behind)."""
    folder = tempfile.mkdtemp(prefix="dbabrain_rotation_")
    local = Path(folder) / "store.json"
    try:
        client.get(remote, local)
        return local.read_text(encoding="utf-8-sig")
    finally:
        try:
            local.unlink()
        except OSError:
            pass
        Path(folder).rmdir()
