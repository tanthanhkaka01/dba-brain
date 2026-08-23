"""Master-side helpers for the docker-db SRE feature.

Two operations, both reusing the existing control transport (paramiko SSH/SFTP,
same host/user/key resolution as every other control command):

* :func:`pull_data_config` — copy updated ``data/`` config files from the worker
  back to the master. The worker's ``data/`` is bind-mounted on the host at
  ``<remote-dir>/data``, so this is a plain SFTP fetch (no docker cp needed).
* :func:`create_db_docker_on_worker` — run ``sre.cli create-db-docker`` inside
  the worker container via ``worker-run``, then optionally pull the config back.
"""

from __future__ import annotations
from db_ops.common.data_sources import REGISTRY_FILENAME  # noqa: F401 - one definition

import copy
import json
import os
import stat as stat_mod
import tempfile
from pathlib import Path

from db_ops.control._support import (
    DB_OPS_ROOT,
    DEFAULT_CONTAINER,
    DEFAULT_REMOTE_DIR,
    ssh_connect,
)
from db_ops.control.worker_exec import run_worker_command
from db_ops.lib.paths import OPERATOR_ASSET_KINDS


# The worker's data/ is bind-mounted on the host under <remote-dir>/data.
DEFAULT_WORKER_DATA_PATH = f"{DEFAULT_REMOTE_DIR}/data"
DEFAULT_MASTER_DATA_PATH = str(DB_OPS_ROOT / "data")
# Local plaintext source used to regenerate the encrypted store. It is gitignored
# and is never copied to the worker. Lives inside the tool (db_ops/secrets/) so the
# tool is self-contained.
DEFAULT_PLAINTEXT_SECRET_PATH = str(DB_OPS_ROOT / "secrets" / "secret_text.json")
# The worker's sql/ is bind-mounted on the host under <remote-dir>/sql.
DEFAULT_WORKER_SQL_PATH = f"{DEFAULT_REMOTE_DIR}/assets"
DEFAULT_MASTER_SQL_PATH = str(DB_OPS_ROOT / "assets")
# Never pull the encrypted secret store back by default — master is its source of truth.
SECRET_FILES = frozenset({"encrypted_secret_text.json", "secret_text.json"})
SECRET_STORE_FILENAME = "encrypted_secret_text.json"
# Config files the worker may mutate at runtime (Telegram add-sql, etc.). These are
# pulled back with overwrite by the config-writeback preset so the master stays in sync.
WORKER_OWNED_CONFIG_FILES = ("sql_commands.json", "sql_targets.json", "app_commands.json")

# Config the worker *adds records to* at runtime, and the field(s) that identify one record.
# `deploy` unions these worker-side records into the master's copy before it builds the bundle,
# because the bundle overwrites the worker's data/ wholesale: without the merge, everything the
# bot registered since the last deploy is destroyed by the next one. That is not hypothetical —
# on 2026-07-31 a deploy silently deleted a SQL task an operator had just added through
# /spbot_add_sql, and the only reason it could be rebuilt was that its run history happened to
# record the resolved target.
#
# `telegram_groups`/`telegram_users` grow the same way: the bot writes every new chat and user it
# sees in getUpdates, with permissions defaulted off.
MERGED_ON_DEPLOY: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("sql_commands.json", "sql_commands", ("sql_id",)),
    ("sql_targets.json", "sql_targets", ("sql_id", "target_no")),
    ("telegram_groups.json", "telegram_groups", ("group_id",)),
    ("telegram_users.json", "telegram_users", ("user_id",)),
    # The SRE app's create-db-docker registers a lab database here, on the worker, by `id`.
    ("docker_db_connections.json", "docker_db_connections", ("id",)),
    # Nothing writes app_commands.json at runtime today; unioning it costs nothing and means a
    # future runtime writer does not silently lose entries before anyone notices.
    ("app_commands.json", "app_commands", ("app_command_id",)),
)

# Config the worker *edits in place* rather than appends to, and the exact leaf paths it owns.
# `/spbot_metric_toggle` is the only runtime writer of `db_instances.json`, and it touches only
# these four; everything else in a record (ip, port, credentials, cmd_access, service_name...) is
# master-owned inventory. A union by server_id alone would keep the master's record whole and
# throw the operator's toggle away — the file would look merged while the metric they switched
# off overnight came back on.
#
# The paths are leaves on purpose. `metrics` also holds `collector_env` and `severity_map`, which
# the toggle never writes; taking the worker's whole `metrics` object would revert those.
FIELD_MERGED_ON_DEPLOY: tuple[tuple[str, str, tuple[str, ...], tuple[tuple[str, ...], ...]], ...] = (
    (
        "db_instances.json", "db_instances", ("server_id",),
        (
            ("metrics", "enabled"),
            ("metrics", "disabled_collector_types"),
            ("metrics", "metric_overrides"),
            ("report_policy", "disabled_metric_codes"),
        ),
    ),
)


class SecretMergeConflict(RuntimeError):
    """The same secret ref holds different values in stores being merged."""


class WorkerConfigUnreadable(RuntimeError):
    """A worker config file exists but cannot be read, so the merge cannot honour it."""


def _remote_exists(sftp, remote: str) -> bool:
    """Is ``remote`` there at all? ``stat`` needs only directory search, not file read."""
    try:
        sftp.stat(remote)
        return True
    except IOError:
        return False


def _record_key(record: dict, fields: tuple[str, ...]) -> tuple:
    return tuple(str(record.get(field)) for field in fields)


def merge_record_lists(
    *, master: list[dict], worker: list[dict], key_fields: tuple[str, ...]
) -> tuple[list[dict], list[tuple]]:
    """Master's records, plus every worker record whose key the master does not have.

    **The master wins on a shared key, and that direction is deliberate.** A record present on
    both sides has usually been *edited* on the master (a schedule retuned, a group's
    `allow_command` raised from the 0 the bot writes for every chat it discovers); taking the
    worker's copy would revert that edit invisibly. A record only the worker has can only be an
    addition, so it is kept.

    What this does not do is merge *field by field* within a shared record. Two people editing
    the same target between deploys is a conflict, and quietly interleaving their fields would
    produce a row neither of them wrote.

    Returns the merged list and the keys that were added, so the caller can report them.
    """
    master_keys = {_record_key(item, key_fields) for item in master if isinstance(item, dict)}
    merged = list(master)
    added: list[tuple] = []
    for item in worker:
        if not isinstance(item, dict):
            continue
        key = _record_key(item, key_fields)
        if key in master_keys:
            continue
        merged.append(item)
        master_keys.add(key)
        added.append(key)
    return merged, added


_MISSING = object()


def _get_path(record: dict, path: tuple[str, ...]):
    """The value at ``path``, or ``_MISSING`` — absent and ``None`` are not the same thing here."""
    node = record
    for part in path:
        if not isinstance(node, dict) or part not in node:
            return _MISSING
        node = node[part]
    return node


def _set_path(record: dict, path: tuple[str, ...], value) -> None:
    node = record
    for part in path[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[path[-1]] = value


def merge_records_by_field(
    *,
    master: list[dict],
    worker: list[dict],
    key_fields: tuple[str, ...],
    worker_owned_paths: tuple[tuple[str, ...], ...],
) -> tuple[list[dict], list[tuple], list[str]]:
    """Master's records with the worker's value applied at ``worker_owned_paths`` only.

    For records the master does not have, the worker's is taken whole (it can only be an
    addition). For shared records the master's copy is the base — its inventory fields are
    authoritative — and each worker-owned path is overlaid from the worker, because those paths
    hold decisions an operator made *on the worker* through the bot.

    A path the worker does not have is left as the master has it, so re-deploying a master that
    knows about a toggle the worker has since removed does not resurrect it as ``null``.

    Returns ``(merged, added_keys, changed_descriptions)``.
    """
    worker_by_key = {
        _record_key(item, key_fields): item for item in worker if isinstance(item, dict)
    }
    master_keys = {_record_key(item, key_fields) for item in master if isinstance(item, dict)}

    merged: list[dict] = []
    changed: list[str] = []
    for item in master:
        if not isinstance(item, dict):
            merged.append(item)
            continue
        key = _record_key(item, key_fields)
        counterpart = worker_by_key.get(key)
        if counterpart is None:
            merged.append(item)
            continue
        record = copy.deepcopy(item)
        for path in worker_owned_paths:
            worker_value = _get_path(counterpart, path)
            if worker_value is _MISSING:
                continue
            if _get_path(record, path) == worker_value:
                continue
            _set_path(record, path, copy.deepcopy(worker_value))
            changed.append(f"{'/'.join(key)}:{'.'.join(path)}")
        merged.append(record)

    added: list[tuple] = []
    for key, item in worker_by_key.items():
        if key in master_keys:
            continue
        merged.append(item)
        added.append(key)
    return merged, added, changed


def _detect_json_indent(text: str, default: int = 4) -> int:
    """The indent width the file already uses.

    These configs are hand-read and version-controlled, and they do not agree: `db_instances.json`
    is 1 space, the Telegram files 2, the SQL ones 4. Re-indenting one on a merge would turn a
    two-line change into a whole-file diff and bury what actually changed.
    """
    for line in text.splitlines()[1:]:
        stripped = line.lstrip(" ")
        if stripped and stripped != line:
            return len(line) - len(stripped)
    return default


def _write_json_atomic(path: Path, data: dict, *, indent: int = 4) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    os.close(fd)
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(data, ensure_ascii=False, indent=indent) + "\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def merge_worker_secrets(
    *,
    host: str,
    user: str,
    password: str | None,
    port: int = 22,
    key: str | None = None,
    from_worker_path: str = DEFAULT_WORKER_DATA_PATH,
    to_master_path: str | None = None,
    plaintext_secret_path: str | Path | None = None,
    dry_run: bool = False,
) -> str:
    """Union the worker's secret store into the master's, before a deploy ships the master's.

    `/spbot_create_db_docker` registers a new database password **on the worker**. A deploy then
    re-encrypts the master's plaintext source over the top and the ref is gone — the same loss
    that destroyed a bot-created SQL task on 2026-07-31, one layer down and harder to notice,
    because what breaks is a connection that used to work.

    Both master stores are written (encrypted **and** the plaintext source), or the very next
    ``_refresh_encrypted_secret_store`` would drop the merged refs again.

    A ref that exists on both sides with **different values is a conflict**, not a merge:
    :class:`SecretMergeConflict` is raised and nothing is written. Guessing which side is current
    is how a production credential gets replaced by a lab password.
    """
    from db_ops.lib.secret_text import resolve_key

    # Both stores are encrypted, so with no key there is nothing to compare and no way to write.
    # Skip loudly rather than abort: a deploy that ran fine before this step existed must keep
    # running, and the operator is told which refs are therefore unprotected.
    if not resolve_key(key):
        print("NOTE: no secret key given; skipping the worker secret merge. A ref created on "
              "the worker (e.g. by /spbot_create_db_docker) will be lost by this deploy. "
              "Re-run with --key-base64 to merge it.", flush=True)
        return "SKIPPED"

    to_master = Path(to_master_path or DEFAULT_MASTER_DATA_PATH)
    plaintext = Path(plaintext_secret_path or DEFAULT_PLAINTEXT_SECRET_PATH)
    local = to_master / SECRET_STORE_FILENAME
    remote = f"{from_worker_path.rstrip('/')}/{SECRET_STORE_FILENAME}"

    client = ssh_connect(host, user, password, port)
    try:
        sftp = client.open_sftp()
        try:
            print(f"# merge worker secrets {user}@{host}:{remote} -> {local}"
                  f"{' (dry-run)' if dry_run else ''}", flush=True)
            try:
                sftp.stat(remote)
            except IOError:
                print("  MISSING  encrypted_secret_text.json (not on worker)", flush=True)
                return "MISSING"
            if not local.exists():
                print("  MISSING  encrypted_secret_text.json (not on master)", flush=True)
                return "MISSING"
            action = _merge_secret_store(
                sftp, remote=remote, local=local, key=key, dry_run=dry_run,
                plaintext=plaintext if plaintext.exists() else None,
            )
            print(f"  {action:<8} encrypted_secret_text.json", flush=True)
            return action
        finally:
            sftp.close()
    finally:
        client.close()


def merge_worker_config(
    *,
    host: str,
    user: str,
    password: str | None,
    port: int = 22,
    from_worker_path: str = DEFAULT_WORKER_DATA_PATH,
    to_master_path: str | None = None,
    dry_run: bool = False,
) -> int:
    """Fold the worker's runtime config changes into the master's files.

    Run before the bundle is built, so a deploy ships *both* sides instead of overwriting the
    worker's away. Two kinds of file, because the worker changes them two different ways:

    * :data:`MERGED_ON_DEPLOY` — files the worker **appends records to**. Union by key; the
      master wins a shared key (see :func:`merge_record_lists`).
    * :data:`FIELD_MERGED_ON_DEPLOY` — files the worker **edits in place**. The master's record
      is the base, with the worker's value overlaid at the named leaf paths only (see
      :func:`merge_records_by_field`).

    Returns the number of records added *or* changed.
    """
    to_master = Path(to_master_path or DEFAULT_MASTER_DATA_PATH)
    client = ssh_connect(host, user, password, port)
    total = 0
    try:
        sftp = client.open_sftp()
        try:
            print(f"# merge worker config {user}@{host}:{from_worker_path} -> {to_master}"
                  f"{' (dry-run)' if dry_run else ''}", flush=True)

            plans = [(name, key, fields, ()) for name, key, fields in MERGED_ON_DEPLOY]
            plans += [(name, key, fields, paths)
                      for name, key, fields, paths in FIELD_MERGED_ON_DEPLOY]

            for name, list_key, key_fields, worker_owned_paths in plans:
                remote = f"{from_worker_path.rstrip('/')}/{name}"
                local = to_master / name
                try:
                    with tempfile.TemporaryDirectory() as tmpdir:
                        worker_copy = Path(tmpdir) / name
                        sftp.get(remote, str(worker_copy))
                        worker_data = json.loads(worker_copy.read_text(encoding="utf-8-sig"))
                except IOError as exc:
                    # A file that is *there but unreadable* is the dangerous case, and it used to
                    # be indistinguishable from an absent one: both printed "not on worker", the
                    # merge skipped, and the copy step then overwrote whatever the operator had
                    # changed. That is how a `/spbot_metric_toggle` was lost on 2026-08-05 — the
                    # container had rewritten the file as root 0600 and the master reads it as
                    # tuser. Refuse instead: nothing is built or shipped yet, so stopping is free,
                    # and continuing means destroying the very change this step exists to keep.
                    if _remote_exists(sftp, remote):
                        raise WorkerConfigUnreadable(
                            f"{name} exists on the worker but could not be read over SFTP as this "
                            f"user ({exc}). Merging would skip it and the deploy would then "
                            f"overwrite it, losing whatever was changed on the worker. Fix the "
                            f"file's ownership/permissions on the worker and deploy again — "
                            f"`ls -l {remote}` shows who owns it."
                        ) from exc
                    print(f"  MISSING  {name} (not on worker)", flush=True)
                    continue
                if not local.exists():
                    print(f"  MISSING  {name} (not on master)", flush=True)
                    continue
                master_text = local.read_text(encoding="utf-8-sig")
                master_data = json.loads(master_text)
                master_records = list(master_data.get(list_key) or [])
                worker_records = list(worker_data.get(list_key) or [])

                if worker_owned_paths:
                    merged, added, changed = merge_records_by_field(
                        master=master_records, worker=worker_records,
                        key_fields=key_fields, worker_owned_paths=worker_owned_paths,
                    )
                else:
                    merged, added = merge_record_lists(
                        master=master_records, worker=worker_records, key_fields=key_fields)
                    changed = []

                if not added and not changed:
                    print(f"  SAME     {name}", flush=True)
                    continue

                details = [f"+{len(added)} added"] if added else []
                if changed:
                    details.append(f"{len(changed)} field(s) taken from worker")
                shown = ", ".join(["/".join(k) for k in added[:4]] + changed[:4])
                extra = len(added) + len(changed) - len(added[:4]) - len(changed[:4])
                more = f" (+{extra} more)" if extra > 0 else ""
                verb = "WOULD" if dry_run else "MERGED"
                print(f"  {verb:<8} {name}: {', '.join(details)} [{shown}{more}]", flush=True)
                total += len(added) + len(changed)
                if not dry_run:
                    master_data[list_key] = merged
                    _write_json_atomic(local, master_data,
                                       indent=_detect_json_indent(master_text))
        finally:
            sftp.close()
    finally:
        client.close()
    verb = "would apply" if dry_run else "applied"
    print(f"{verb} {total} worker change(s) to the master config.", flush=True)
    return total


def _merge_secret_store(
    sftp,
    *,
    remote: str,
    local: Path,
    key: str | None,
    dry_run: bool,
    plaintext: Path | None = None,
) -> str:
    """Merge worker, master-encrypted, and optional master-plaintext stores.

    A plain file copy is last-writer-wins: a ref the master added since the last deploy exists
    only on the master, and pulling the worker's file would delete it without a word. So both
    encrypted stores are decrypted and unioned. When ``plaintext`` is supplied, its refs are
    included in the same union and both master stores are brought to the same content.

    A ref that exists on both sides with *different* values is a conflict, not a merge: it is
    reported and nothing is written. Guessing which one is current is how a production
    credential gets replaced by a lab password.
    """
    from db_ops.lib.secret_text import (
        encrypt_secret_text,
        load_secret_text_file,
        resolve_key,
    )

    resolved_key = resolve_key(key)
    with tempfile.TemporaryDirectory() as tmp:
        worker_copy = Path(tmp) / local.name
        sftp.get(remote, str(worker_copy))
        worker_secrets = load_secret_text_file(worker_copy, key=resolved_key)
    master_secrets = load_secret_text_file(local, key=resolved_key)
    plaintext_secrets = (
        load_secret_text_file(plaintext, key=resolved_key) if plaintext is not None else {}
    )

    worker_conflicts = sorted(
        ref for ref, value in worker_secrets.items()
        if ref in master_secrets and master_secrets[ref] != value
    )
    if worker_conflicts:
        raise SecretMergeConflict(
            "Refs differ between the master and the worker: "
            + ", ".join(worker_conflicts)
            + ". "
            "Decide which value is current and align them before pulling "
            "(no secret was written)."
        )

    encrypted_merged = {**master_secrets, **worker_secrets}
    plaintext_conflicts = sorted(
        ref for ref, value in encrypted_merged.items()
        if ref in plaintext_secrets and plaintext_secrets[ref] != value
    )
    if plaintext_conflicts:
        raise SecretMergeConflict(
            "Refs differ between the merged encrypted store and the master plaintext source: "
            + ", ".join(plaintext_conflicts)
            + ". Decide which value is current and align them before pulling "
            "(no secret was written)."
        )

    merged = {**plaintext_secrets, **encrypted_merged}
    encrypted_added = sorted(set(merged) - set(master_secrets))
    plaintext_added = sorted(set(merged) - set(plaintext_secrets)) if plaintext is not None else []
    master_only = sorted(set(master_secrets) - set(worker_secrets))
    if not encrypted_added and not plaintext_added:
        return "UNCHANGED"
    if dry_run:
        if encrypted_added:
            print(
                f"           encrypted store would add {len(encrypted_added)} ref(s): "
                f"{', '.join(encrypted_added)}",
                flush=True,
            )
        if plaintext_added:
            print(
                f"           plaintext source would add {len(plaintext_added)} ref(s): "
                f"{', '.join(plaintext_added)}",
                flush=True,
            )
        return "WOULD"

    if encrypted_added:
        local.write_text(
            json.dumps(encrypt_secret_text(merged, resolved_key), indent=2, ensure_ascii=False)
            + "\n",
            encoding="utf-8",
        )
        print(
            f"           encrypted store added {len(encrypted_added)} ref(s): "
            f"{', '.join(encrypted_added)}",
            flush=True,
        )
    if master_only:
        print(f"           kept {len(master_only)} master-only ref(s)", flush=True)
    if plaintext is not None and plaintext_added:
        plaintext.parent.mkdir(parents=True, exist_ok=True)
        plaintext.write_text(
            json.dumps(merged, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(
            f"           plaintext source added {len(plaintext_added)} ref(s): "
            f"{', '.join(plaintext_added)}",
            flush=True,
        )
    return "MERGED"


def _remote_json_files(sftp, remote_dir: str, *, include_secrets: bool) -> list[str]:
    names: list[str] = []
    for name in sorted(sftp.listdir(remote_dir)):
        if not name.endswith(".json"):
            continue
        if name in SECRET_FILES and not include_secrets:
            continue
        names.append(name)
    return names


def pull_data_config(
    *,
    host: str,
    user: str,
    password: str | None,
    port: int = 22,
    from_worker_path: str = DEFAULT_WORKER_DATA_PATH,
    to_master_path: str | None = None,
    files: list[str] | None = None,
    all_json: bool = False,
    include_secrets: bool = False,
    merge_secrets: bool = False,
    secret_key: str | None = None,
    plaintext_secret_path: str | Path | None = None,
    overwrite: bool = False,
    dry_run: bool = False,
) -> int:
    """Copy config files from the worker's data dir back to the master.

    By default only the docker-db connection registry is pulled (the file
    ``create-db-docker`` updates). ``--all-json`` widens this to every ``*.json``
    in the worker data dir (still excluding the secret store unless
    ``--include-secrets``).
    """
    to_master = Path(to_master_path or DEFAULT_MASTER_DATA_PATH)
    plaintext_secret = Path(plaintext_secret_path or DEFAULT_PLAINTEXT_SECRET_PATH)
    client = ssh_connect(host, user, password, port)
    copied: list[str] = []
    skipped: list[str] = []
    try:
        sftp = client.open_sftp()
        try:
            if all_json:
                names = _remote_json_files(sftp, from_worker_path, include_secrets=include_secrets)
            else:
                names = list(files or [REGISTRY_FILENAME])

            print(f"# pull {user}@{host}:{from_worker_path} -> {to_master} "
                  f"({'dry-run' if dry_run else 'copy'}; overwrite={overwrite})", flush=True)
            to_master.mkdir(parents=True, exist_ok=True)

            for name in names:
                remote = f"{from_worker_path.rstrip('/')}/{name}"
                local = to_master / name
                try:
                    remote_stat = sftp.stat(remote)
                except IOError:
                    print(f"  MISSING  {name} (not on worker)", flush=True)
                    continue
                if local.exists() and not overwrite:
                    print(f"  SKIP     {name} (exists; pass --overwrite)", flush=True)
                    skipped.append(name)
                    continue
                if name in SECRET_FILES and merge_secrets:
                    action = _merge_secret_store(
                        sftp, remote=remote, local=local, key=secret_key, dry_run=dry_run,
                        plaintext=plaintext_secret,
                    )
                    print(f"  {action:<8} {name}", flush=True)
                    copied.append(name)
                    continue
                if dry_run:
                    print(f"  WOULD    {name} ({remote_stat.st_size} bytes)", flush=True)
                    copied.append(name)
                    continue
                sftp.get(remote, str(local))
                if remote_stat.st_mode is not None:
                    try:
                        os.chmod(local, stat_mod.S_IMODE(remote_stat.st_mode))
                    except OSError:
                        pass
                print(f"  COPIED   {name} ({remote_stat.st_size} bytes)", flush=True)
                copied.append(name)
        finally:
            sftp.close()
    finally:
        client.close()

    verb = "would copy" if dry_run else "copied"
    print(f"\n{verb} {len(copied)} file(s); skipped {len(skipped)}.", flush=True)
    return 0


def _iter_remote_sql_files(sftp, remote_dir: str, rel: str = "") -> list[str]:
    """Return sql-tree files as posix relative paths, recursing into subdirs.

    Only ``*.sql`` files are pulled; directories are walked but never fetched.

    **Only the asset kinds the operator owns are walked at all.** The pull exists for one thing:
    task SQL and bot-command SQL that were registered on the worker at runtime, which live under
    ``assets/tasks/`` and ``assets/sql_telegram_commands/``. Everything else under the worker's
    ``assets/`` is a leftover from an older layout, and pulling it is worse than useless — it
    **recreates the directory on the master**, so the next bundle ships it back and the prune in
    ``deploy.superseded_dirs`` correctly concludes the bundle still carries it.

    That is not hypothetical: the 2026-08-22 redeploy moved three stale directories aside and left
    ``assets/metrics.superseded-20260822`` in place, because ``--merge`` had just pulled its 189
    ``.sql`` files onto the master minutes earlier. A merge that resurrects exactly what the prune
    exists to remove makes a stale tree permanent.
    """
    out: list[str] = []
    base = f"{remote_dir.rstrip('/')}/{rel}".rstrip("/")
    try:
        entries = sftp.listdir_attr(base)
    except IOError:
        return out
    for entry in sorted(entries, key=lambda item: item.filename):
        child_rel = f"{rel}/{entry.filename}".lstrip("/")
        if entry.st_mode is not None and stat_mod.S_ISDIR(entry.st_mode):
            if not rel and entry.filename not in OPERATOR_ASSET_KINDS:
                print(f"  SKIP     {entry.filename}/ (not an operator asset kind)", flush=True)
                continue
            out.extend(_iter_remote_sql_files(sftp, remote_dir, child_rel))
        elif entry.filename.endswith(".sql"):
            out.append(child_rel)
    return out


def pull_sql_tree(
    *,
    host: str,
    user: str,
    password: str | None,
    port: int = 22,
    from_worker_path: str = DEFAULT_WORKER_SQL_PATH,
    to_master_path: str | None = None,
    overwrite: bool = False,
    dry_run: bool = False,
) -> int:
    """Copy the worker's ``sql/`` tree (``*.sql`` files) back to the master.

    New task scripts registered at runtime (Telegram add-sql / ``config_admin``)
    live under ``assets/tasks/<db_type>/<server>/``; this mirrors them to the master
    so a subsequent deploy does not overwrite them away.
    """
    to_master = Path(to_master_path or DEFAULT_MASTER_SQL_PATH)
    client = ssh_connect(host, user, password, port)
    copied: list[str] = []
    skipped: list[str] = []
    try:
        sftp = client.open_sftp()
        try:
            rel_files = _iter_remote_sql_files(sftp, from_worker_path)
            print(f"# pull-sql {user}@{host}:{from_worker_path} -> {to_master} "
                  f"({'dry-run' if dry_run else 'copy'}; overwrite={overwrite}); {len(rel_files)} file(s)", flush=True)
            for rel in rel_files:
                remote = f"{from_worker_path.rstrip('/')}/{rel}"
                local = to_master / Path(rel)
                if local.exists() and not overwrite:
                    print(f"  SKIP     {rel} (exists; pass --overwrite)", flush=True)
                    skipped.append(rel)
                    continue
                if dry_run:
                    print(f"  WOULD    {rel}", flush=True)
                    copied.append(rel)
                    continue
                local.parent.mkdir(parents=True, exist_ok=True)
                sftp.get(remote, str(local))
                print(f"  COPIED   {rel}", flush=True)
                copied.append(rel)
        finally:
            sftp.close()
    finally:
        client.close()
    verb = "would copy" if dry_run else "copied"
    print(f"\n{verb} {len(copied)} sql file(s); skipped {len(skipped)}.", flush=True)
    return 0


def create_db_docker_on_worker(
    *,
    host: str,
    user: str,
    password: str | None,
    port: int = 22,
    container: str = DEFAULT_CONTAINER,
    sre_args: list[str],
    pull_config: bool = False,
    pull_kwargs: dict | None = None,
) -> int:
    """Run ``sre.cli create-db-docker`` inside the worker container, then
    optionally pull the updated data config back to the master."""
    command = ["python", "-m", "db_ops.sre.cli", "create-db-docker", *sre_args]
    rc = run_worker_command(host=host, user=user, password=password, port=port,
                            container=container, command=command)
    if rc == 0 and pull_config:
        print("\n=== worker-pull-data-config ===", flush=True)
        pull_data_config(host=host, user=user, password=password, port=port, **(pull_kwargs or {}))
    return rc
