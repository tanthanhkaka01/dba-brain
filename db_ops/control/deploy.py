"""Deploy operations driven from the master: build the image + bundle locally, ship it
to the worker over SFTP, and (re)start the worker daemon. Ported from the standalone
``build_image.py`` / ``copy_to_ubuntu.py`` / ``start_daemon.py`` scripts.
"""

from __future__ import annotations

import base64
import shutil
import time
from pathlib import Path

from db_ops.control._support import (
    BUNDLE_DIR,
    CONTAINER_DATA_DIR_NAME,
    DB_OPS_ROOT,
    DEFAULT_CONTAINER,
    DEFAULT_REMOTE_DIR,
    IMAGE_REPO,
    IMAGE_TAR_NAME,
    read_version,
    require_docker,
    resolve_password,
    run_local,
    sftp_put_tree,
    ssh_capture,
    ssh_connect,
    ssh_run,
)
from db_ops.lib.data_files import local_only_names, required_in_bundle
from db_ops.lib.secret_text import resolve_cli_key
from db_ops.control.worker_data import merge_worker_config, merge_worker_secrets, pull_sql_tree

def required_in_bundle_paths() -> tuple[str, ...]:
    """What a built bundle is checked for before it is allowed to leave the master.

    The ``data/`` half is **read from the manifest** (``in_bundle``) rather than written out here.
    One list of data files per module is how the four that existed came to disagree; the manifest
    is the one that decides, and it states "this file must reach the worker" beside "this file is
    pushed", where the two can be read together. The rest are artefacts of the build itself and
    have nothing to do with ``data/``.

    **A function, not a module constant.** It was a constant computed at import for one afternoon,
    and a clean-room export caught what that costs: on a public checkout the manifest is not in
    ``data/`` yet, so importing this module raised `DataFileError` before anything ran. Reading
    configuration at import time makes a module's *importability* depend on a tool root, and a
    module that cannot be imported cannot even print its own help.
    """
    return ("db_ops_image.tar", "docker-compose.yml", "config.json",
            *required_in_bundle(), "assets",
            "runtime/reports/database-inventory.json")

#: Directories on the worker whose *shape* the bundle decides. Anything else under the remote
#: directory belongs to the worker — ``logs/``, ``runtime/``, ``containers/`` — and is never
#: touched.
#:
#: The upload writes files over files and has no opinion about a directory that stopped being
#: shipped, so one simply stays. On 2026-08-22 that cost a deploy: the built-in SQL moved into the
#: package, the bundle stopped carrying ``assets/metrics``, and the worker's copy from the previous
#: layout survived — where the asset lookup prefers the operator's tree, so the *stale* queries won
#: against the new image's. The image was correct and the worker ran the old SQL.
BUNDLE_OWNED_DIRS: tuple[str, ...] = ("data", "assets")

#: Where a superseded directory goes. Moved aside rather than deleted: this runs against a live
#: worker, driven by a diff with a locally built bundle, and a build that produced a thin bundle
#: would otherwise turn one bad build into data loss. The move is enough to fix the defect — the
#: lookup stops finding it — and it stays recoverable. Nothing reads this directory; it is not one
#: of the compose mounts. Old entries can be deleted whenever.
SUPERSEDED_DIR_NAME = ".superseded"


def _reset_bundle_dir() -> None:
    """Empty the bundle directory, and refuse to continue if anything survives.

    The bundle is *shipped over* the worker's files, so a leftover from a previous build is not
    junk — it is a stale copy that gets deployed. On 2026-08-22 a build failed here with
    `FileExistsError` on `data/` after the delete had apparently succeeded, and the two directories
    that survived carried timestamps from earlier runs. Whatever the cause on Windows — an open
    handle, a delete still pending — the answer is the same: verify rather than assume, and say so
    loudly, because the silent version of this bug ships yesterday's `assets/` to production.

    Merging into what is already there (`dirs_exist_ok=True`) would make the symptom go away and
    the hazard permanent.
    """
    for attempt in range(3):
        if BUNDLE_DIR.exists():
            shutil.rmtree(BUNDLE_DIR, ignore_errors=(attempt < 2))
        if not BUNDLE_DIR.exists():
            break
        time.sleep(0.5)
    if BUNDLE_DIR.exists():
        leftovers = sorted(child.name for child in BUNDLE_DIR.iterdir())
        raise SystemExit(
            f"Could not empty the deploy bundle at {BUNDLE_DIR}: {leftovers}. "
            "Something is holding a file open — close it and build again. Continuing would ship "
            "a previous build's files to the worker."
        )
    BUNDLE_DIR.mkdir(parents=True)


def build_image(*, platform: str = "linux/amd64", no_cache: bool = False, skip_build: bool = False) -> Path:
    require_docker()
    version = read_version()
    version_tag = f"{IMAGE_REPO}:{version}"
    latest_tag = f"{IMAGE_REPO}:latest"
    print(f"Version: {version}  (image tags: {version_tag}, {latest_tag})")

    encrypted = DB_OPS_ROOT / "data" / "encrypted_secret_text.json"
    if not encrypted.exists():
        raise SystemExit(f"{encrypted} not found. Encrypt secrets first "
                         "(python -m db_ops.control.cli encrypt-secret-text).")

    _reset_bundle_dir()
    tar_path = BUNDLE_DIR / IMAGE_TAR_NAME

    if not skip_build:
        print("\n[1/4] Building image ...")
        build_cmd = ["docker", "build", "--platform", platform,
                     "--label", f"org.opencontainers.image.version={version}",
                     "-t", version_tag, "-t", latest_tag]
        if no_cache:
            build_cmd.append("--no-cache")
        build_cmd.append(".")
        run_local(build_cmd, cwd=DB_OPS_ROOT)
        print("\n[2/4] Exporting image to tar ...")
    else:
        print("\n[1-2/4] --skip-build: exporting existing image ...")
    run_local(["docker", "save", version_tag, latest_tag, "-o", str(tar_path)])

    print("\n[3/4] Assembling deploy bundle ...")
    shutil.copy(DB_OPS_ROOT / "docker-compose.runtime.yml", BUNDLE_DIR / "docker-compose.yml")
    shutil.copy(DB_OPS_ROOT / "config.json", BUNDLE_DIR / "config.json")
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    # `data/` is copied through the manifest, not wholesale. Two files in it are master-only —
    # a worked SLA sample and the SRE end-to-end test fixture — and neither is read by anything
    # the worker runs. Reported rather than silently dropped: a bundle that ships less than the
    # last one did is exactly the shape of the 2026-08-22 defect (`BUNDLE_OWNED_DIRS`), so the
    # build says what it left behind and why.
    left_behind = sorted(local_only_names())
    shutil.copytree(DB_OPS_ROOT / "data", BUNDLE_DIR / "data",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", *left_behind))
    if left_behind:
        print(f"  master-only, not in the bundle: {', '.join(left_behind)}")
    shutil.copytree(DB_OPS_ROOT / "assets", BUNDLE_DIR / "assets", ignore=ignore)
    (BUNDLE_DIR / "logs").mkdir()
    (BUNDLE_DIR / "runtime" / "reports").mkdir(parents=True)
    # Ship the canonical inventory the worker-side reports app reads/merges/renders. The
    # master's authoritative copy is the tool's data/database-inventory.json (where static
    # blocks like sqlserver_resources/deployment are edited); it lands on the worker under
    # the mounted runtime/reports (db_ops.sqlite there is preserved by `copy`). Health
    # blocks the worker had merged are rebuilt from SQLite on its next inventory-workflow
    # run, so overwriting them on deploy loses nothing durable. The old runtime/reports
    # location is kept as a fallback for trees without the data copy.
    inv_src = DB_OPS_ROOT / "data" / "database-inventory.json"
    if not inv_src.exists():
        inv_src = DB_OPS_ROOT / "runtime" / "reports" / "database-inventory.json"
    if inv_src.exists():
        shutil.copy(inv_src, BUNDLE_DIR / "runtime" / "reports" / "database-inventory.json")

    missing = [name for name in required_in_bundle_paths()
               if not (BUNDLE_DIR / name).exists()]
    if missing:
        raise SystemExit(f"Bundle is missing: {', '.join(missing)}")
    print(f"\n[4/4] Bundle ready at {BUNDLE_DIR} (image {tar_path.stat().st_size / 1048576:.0f} MB).")
    return BUNDLE_DIR


def superseded_dirs(bundle: Path, remote_subdirs: dict[str, list[str]]) -> dict[str, list[str]]:
    """Which of the worker's directories the bundle no longer carries, per owned directory.

    Compared at the **top level of each owned directory only**, because that is where the failure
    lives and going deeper would start deleting the operator's own work. A whole directory
    disappearing is a structural change made on the master; what happens *inside* ``assets/tasks``
    or ``assets/sql_telegram_commands`` is the bot writing SQL on the worker, which the deploy is
    supposed to mirror back rather than remove.

    An owned directory the bundle does not carry at all returns nothing. That is not a retirement,
    it is a bundle that failed to assemble, and acting on it would turn one bad build into a wiped
    worker.
    """
    superseded: dict[str, list[str]] = {}
    for owned in BUNDLE_OWNED_DIRS:
        local = bundle / owned
        if not local.is_dir():
            continue
        shipped = {child.name for child in local.iterdir() if child.is_dir()}
        if not shipped:
            continue
        stale = sorted(name for name in remote_subdirs.get(owned, []) if name not in shipped)
        if stale:
            superseded[owned] = stale
    return superseded


def _remote_subdirs(client, remote_dir: str) -> dict[str, list[str]]:
    """The top-level subdirectory names the worker currently has under each owned directory."""
    found: dict[str, list[str]] = {}
    for owned in BUNDLE_OWNED_DIRS:
        rc, out, _err = ssh_capture(
            client, f"find {remote_dir}/{owned} -mindepth 1 -maxdepth 1 -type d"
        )
        if rc != 0:
            # A first deploy has no such directory yet; there is nothing to compare against.
            continue
        found[owned] = [line.rsplit("/", 1)[-1] for line in out.splitlines() if line.strip()]
    return found


def prune_superseded_dirs(client, bundle: Path, remote_dir: str) -> dict[str, list[str]]:
    """Move aside every worker directory the bundle stopped carrying, and say what moved."""
    superseded = superseded_dirs(bundle, _remote_subdirs(client, remote_dir))
    if not superseded:
        return {}
    stamp = time.strftime("%Y%m%d_%H%M%S")
    quarantine = f"{remote_dir}/{SUPERSEDED_DIR_NAME}/{stamp}"
    named = ", ".join(f"{owned}/{name}" for owned, names in superseded.items() for name in names)
    print(f"The bundle no longer carries {named} - moving aside to {quarantine} ...")
    print("  Left in place they would shadow what the new image ships, which is what happened "
          "on 2026-08-22.")
    for owned, names in superseded.items():
        ssh_run(client, f"mkdir -p {quarantine}/{owned}", quiet=True)
        for name in names:
            ssh_run(client, f"mv {remote_dir}/{owned}/{name} {quarantine}/{owned}/{name}")
    return superseded


def copy_bundle(*, host: str, user: str, password: str, port: int = 22,
                remote_dir: str = DEFAULT_REMOTE_DIR, bundle: Path | None = None) -> None:
    bundle = Path(bundle) if bundle else BUNDLE_DIR
    if not (bundle / IMAGE_TAR_NAME).exists():
        raise SystemExit(f"{bundle / IMAGE_TAR_NAME} not found. Run build-image first.")
    client = ssh_connect(host, user, password, port)
    try:
        print(f"Preparing {remote_dir} (sudo) ...")
        # The deploy user must own what the bundle overwrites, but the chown must NOT reach
        # `containers/`: the lab DB containers bind-mount their data and backup directories from
        # there, and those are owned by the *database* users inside them (postgres is uid 999,
        # not the deploy user). A blanket `chown -R` on the whole tree silently takes write access
        # away from the database itself. That is not hypothetical - it happened on 2026-07-31:
        # a deploy re-owned /opt/db_ops/containers/pg_ha_01/backup to the SSH user, PostgreSQL
        # (uid 999) could no longer create files in its own archive destination, and
        # `archive_command` failed on every WAL segment from that moment. Nothing alerted on the
        # deploy; the only symptom was archived_count freezing while failed_count climbed into the
        # thousands, i.e. recoverability was gone with the database still healthy.
        ssh_run(client,
                f"sudo -S -p '' sh -c 'mkdir -p {remote_dir} && chown {user}:{user} {remote_dir} && "
                f"find {remote_dir} -mindepth 1 -maxdepth 1 ! -name {CONTAINER_DATA_DIR_NAME} "
                f"-exec chown -R {user}:{user} {{}} +'",
                sudo_password=password)
        print(f"Uploading bundle -> {host}:{remote_dir} (overwrites config/data/sql/image; "
              "keeps logs/ and runtime/db_ops.sqlite) ...")
        sftp_put_tree(client, bundle, remote_dir)
        # After the upload, not before: the comparison is against what was actually shipped, and a
        # transfer that dies half way leaves the worker's old directories where they were.
        prune_superseded_dirs(client, bundle, remote_dir)
        ssh_run(client, f"mkdir -p {remote_dir}/logs {remote_dir}/runtime", quiet=True)
        ssh_run(client, f"ls -la {remote_dir}")
    finally:
        client.close()


#: How long a deploy waits for the daemon and its children to finish before killing them. Long
#: enough for the short scheduled jobs (the Oracle archivelog backup runs ~30s) and short enough
#: that a deploy is never held hostage by a wedged process.
GRACEFUL_STOP_SECONDS = 60


def _daemon_key_arg(key_base64: str | None, key: str | None) -> str:
    if key_base64:
        return f"--key_base64 {key_base64}"
    if key:
        return f"--key_base64 {base64.b64encode(key.encode()).decode()}"
    import getpass
    plain = getpass.getpass("Secret passphrase (base64-encoded for the daemon): ")
    return f"--key_base64 {base64.b64encode(plain.encode()).decode()}"


def reclaim_worker_files(*, host: str, user: str, password: str, port: int = 22,
                         remote_dir: str = DEFAULT_REMOTE_DIR) -> None:
    """Give the whole of the worker's `data/` and `assets/` back to the SSH user, before reading it.

    The tree, not a list of files, and that is the point. Both directories are bind mounts shared
    between the container and its host, and the container runs as **root** — so anything it writes
    there lands owned by root, whatever wrote it. The master reads the worker over SFTP as an
    ordinary user, cannot open those files, and `merge_worker_config` used to skip them silently;
    the copy step that follows then overwrote whatever had been changed on the worker. Enumerating
    the writers that can cause this is a losing game: every future one, in any app, would have to
    be remembered. Reclaiming the tree costs one command and covers writers that do not exist yet.

    **`chown` only, never `chmod`.** `data/` holds `encrypted_secret_text.json` and `ssh_keys/*.key`
    at 0600 on purpose — ssh refuses a world-readable key outright. Ownership is the only thing
    missing: the owner can read its own 0600 file, so nothing has to be opened up.

    **Never `containers/`.** `copy_bundle` learned that the hard way on 2026-07-31 — the lab DB
    containers bind-mount their data out of there and it belongs to the *database* users inside
    them (postgres is uid 999). This runs the same `chown` the copy step already runs, only early
    enough to matter, and against two named directories rather than the tree that holds them.

    Best-effort: a worker that has never been deployed to has nothing to reclaim, and that is not
    a failure.
    """
    client = ssh_connect(host, user, password, port)
    try:
        print("\n=== reclaim worker files ===")
        # `sudo` because the files are root's; the same non-interactive form copy_bundle uses.
        # A directory the worker does not have is skipped, so a fresh host is silent, not an error.
        rc = ssh_run(
            client,
            f"sudo -S -p '' sh -c 'for d in {remote_dir}/data {remote_dir}/assets; do "
            f"[ -d \"$d\" ] && chown -R {user}:{user} \"$d\"; done; true'",
            sudo_password=password,
            check=False,
        )
        if rc != 0:
            print(f"  SKIPPED  could not reclaim {remote_dir}/data and {remote_dir}/assets "
                  f"(exit {rc}); the merge will refuse rather than lose anything", flush=True)
        else:
            print(f"  OK       data/ and assets/ are readable by {user} again", flush=True)
    finally:
        client.close()


def start_daemon(*, host: str, user: str, password: str, port: int = 22,
                 remote_dir: str = DEFAULT_REMOTE_DIR, container: str = DEFAULT_CONTAINER,
                 key_base64: str | None = None, key: str | None = None,
                 node_role: str = "worker") -> None:
    key_arg = _daemon_key_arg(key_base64, key)
    remote_tar = f"{remote_dir}/{IMAGE_TAR_NAME}"
    client = ssh_connect(host, user, password, port)
    try:
        print("\n[1/4] Loading image ...")
        ssh_run(client, f"docker load -i {remote_tar}")
        print("\n[2/4] Replacing existing container (if any) ...")
        # `docker stop` first, then remove. `rm -f` is SIGKILL: it takes the daemon and every
        # child it launched down mid-statement, and a child killed that way never writes its
        # completion row. A 30-second Oracle archivelog backup that started 12 seconds before a
        # deploy sat RUNNING until the stale reaper called it a timeout half an hour later and
        # paged CRITICAL about a backup that had been fine. `stop` sends SIGTERM and waits, so an
        # in-flight run gets its chance to finish and record itself; the wait is bounded so a
        # wedged process still cannot hold a deploy hostage.
        ssh_run(client, f"docker stop -t {GRACEFUL_STOP_SECONDS} {container} 2>/dev/null || true")
        ssh_run(client, f"docker rm -f {container} 2>/dev/null || true")
        print(f"\n[3/4] Starting daemon (node_role={node_role}, passphrase hidden) ...")
        # --service-ports publishes the compose `ports:` mapping (8080) so the webhost
        # app_command the daemon launches inside this container is reachable from outside.
        ssh_run(client,
                f"cd {remote_dir} && docker compose run -e DB_OPS_NODE_ROLE={node_role} "
                f"-d --service-ports --name {container} db_ops daemon {key_arg}",
                quiet=True)
        ssh_run(client, f"docker update --restart unless-stopped {container}", quiet=True)
        print("\n[4/5] Verifying ...")
        ssh_run(client, f"docker ps --filter name={container} --format '{{{{.Names}}}} | {{{{.Status}}}}'")
        ssh_run(client, f"docker exec {container} python -c \"import db_ops; print('version=', db_ops.__version__)\"")
        print("\n[5/5] Pruning superseded images ...")
        _prune_old_images(client, keep=KEEP_IMAGE_VERSIONS)
    finally:
        client.close()


#: How many superseded ``db_ops:<version>`` tags survive a deploy, beyond ``latest``. Enough that
#: a bad release can be rolled back to the one before it without a rebuild, and few enough that the
#: pile has a ceiling.
KEEP_IMAGE_VERSIONS = 5


def _prune_old_images(client, *, keep: int = KEEP_IMAGE_VERSIONS) -> None:
    """Remove the image tags this deploy just superseded, and the layers nothing points at.

    Every deploy ends in ``docker load``, which adds ``db_ops:<version>`` and repoints ``latest``.
    Nothing removed the version it replaced, so the tags accumulated once per deploy — measured on
    the worker on 2026-08-18: **383 `db_ops` tags and 526 dangling images**, on a host whose root
    volume had already been extended once from 293 GB to 589 GB. Deploy frequency is what made this
    a daily cost rather than a one-off; each build changes the layer holding the project source, so
    every version carries its own delta even though the base layers are shared.

    Removing a *tag* is not removing an image: the running container's ``db_ops:latest`` points at
    the newest one, and the newest ``keep`` versions stay tagged so a rollback is a `docker run`
    away. ``image prune -f`` then collects only what no tag references at all.

    Best-effort by design. A deploy that worked must not be reported as failed because a cleanup
    afterwards did not — the daemon is already up and verified by the time this runs.
    """
    # Handed to the remote shell as-is. Wrapping it in `sh -c '...'` looked tidier and was a trap:
    # the script's own single quotes (`--format '{{.Tag}}'`, `grep -v '^latest$'`) close the
    # wrapper's quote and reopen it, so those arguments arrive unquoted. It happened to survive —
    # `{{.Tag}}` has no comma for brace expansion and `^latest$` no variable to expand — but the
    # first quoted argument that did would break the prune silently, and a best-effort step that
    # fails silently is one nobody notices has stopped working.
    #
    # sort -V orders 2.85.9 before 2.85.10; a plain sort does not, and the versions that survived a
    # lexical sort would be an arbitrary set rather than the newest.
    ssh_run(client, (
        "keep=$(docker images db_ops --format '{{.Tag}}' | grep -v '^latest$' | sort -V | "
        f"tail -{int(keep)}); "
        "for t in $(docker images db_ops --format '{{.Tag}}' | grep -v '^latest$'); do "
        "echo \"$keep\" | grep -qx \"$t\" || docker rmi \"db_ops:$t\" >/dev/null 2>&1 || true; "
        "done; "
        "docker image prune -f | tail -1; "
        "echo \"db_ops tags kept: $(docker images db_ops --format '{{.Tag}}' | wc -l)\""
    ), check=False)


def deploy(*, host: str, user: str, password: str, port: int = 22,
           remote_dir: str = DEFAULT_REMOTE_DIR, container: str = DEFAULT_CONTAINER,
           key_base64: str | None = None, key: str | None = None, node_role: str = "worker",
           platform: str = "linux/amd64", no_cache: bool = False, skip_build: bool = False,
           merge_worker: bool = False) -> None:
    # Say which direction this deploy runs in before it runs. Both directions can destroy work —
    # without the merge, whatever the bot registered on the worker; with it, whatever was edited on
    # the master at a path the worker owns. Neither is recoverable afterwards, so the choice is
    # printed rather than left to be inferred from an absent flag.
    if merge_worker:
        print("=== direction: worker -> master -> worker (--merge) ===\n"
              "Worker-added config and .sql scripts are merged into the master first. The master's\n"
              "value LOSES at db_instances.json metrics.enabled / disabled_collector_types /\n"
              "metric_overrides and report_policy.disabled_metric_codes.")
    else:
        print("=== direction: master -> worker (default) ===\n"
              "The master's data/ and assets/ overwrite the worker's. Anything registered through\n"
              "the bot since the last deploy — SQL tasks, Telegram groups/users, docker db\n"
              "connections — is DELETED. Pass --merge to keep it.")
    if merge_worker:
        # BEFORE build-image, because the bundle is assembled from the master's data/ and assets/
        # and then overwrites the worker's wholesale. Anything the bot registered on the worker
        # since the last deploy — a SQL task, a chat, a user, a database password — exists only
        # there, so without this step the deploy destroys it. The .sql scripts come too: a merged
        # sql_commands entry whose script stayed on the worker would point at a file the bundle
        # does not carry.
        #
        # Secrets go first, and specifically *after* the caller has refreshed the master's
        # encrypted store from its plaintext source (`cli._refresh_encrypted_secret_store`):
        # that refresh REPLACES the encrypted store with exactly the plaintext content, so a
        # merge done before it would be silently thrown away. A ref that differs on both sides
        # raises SecretMergeConflict, which aborts the deploy here — nothing has been built or
        # shipped yet, so stopping costs nothing, and continuing could overwrite a production
        # credential with a lab one.
        # First of all, because every step below reads the worker's files as this SSH user and
        # anything the container wrote is owned by root. `copy_bundle` already chowns the tree —
        # but it runs *after* the merge, which is precisely too late: the merge skipped what it
        # could not read and the copy then overwrote it.
        reclaim_worker_files(host=host, user=user, password=password, port=port,
                             remote_dir=remote_dir)
        print("\n=== merge worker secrets ===")
        merge_worker_secrets(host=host, user=user, password=password, port=port,
                             key=resolve_cli_key(key, key_base64),
                             from_worker_path=f"{remote_dir}/data")
        print("\n=== merge worker config ===")
        merge_worker_config(host=host, user=user, password=password, port=port,
                            from_worker_path=f"{remote_dir}/data")
        pull_sql_tree(host=host, user=user, password=password, port=port,
                      from_worker_path=f"{remote_dir}/assets")
    print("\n=== build-image ===")
    build_image(platform=platform, no_cache=no_cache, skip_build=skip_build)
    print("\n=== copy ===")
    copy_bundle(host=host, user=user, password=password, port=port, remote_dir=remote_dir)
    print("\n=== start-daemon ===")
    start_daemon(host=host, user=user, password=password, port=port, remote_dir=remote_dir,
                 container=container, key_base64=key_base64, key=key, node_role=node_role)
    print("\nAll steps completed.")
