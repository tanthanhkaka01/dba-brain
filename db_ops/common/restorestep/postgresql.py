"""PostgreSQL: the "file" is a directory, the "log" step is a configuration, and it runs itself.

There is no per-file restore here and pretending otherwise would mislead:

* **full** — a base backup directory *becomes* the data directory.
* **diff** — incrementals are not applied one at a time; ``pg_combinebackup`` reads the full and
  every incremental after it **together** and writes one complete data directory. So this takes
  the whole chain in ``backup_paths``, base first. Handing it only the newest would produce a data
  directory missing everything in between, and it would start.
* **log** — WAL is replayed by the server itself at startup, from ``restore_command`` and
  ``recovery.signal``. The log step *writes that configuration* rather than applying anything.

**Everything else is done here too, because doing it by hand is how it goes wrong.** An earlier
cut of this module left three things to the caller and each of them bit on the first real run:

* ``pg_combinebackup`` is **not on PATH** on Debian or the official image — /usr/bin carries
  wrappers for ``psql`` and ``pg_basebackup`` and not this one. It lives in
  ``/usr/lib/postgresql/<major>/bin``.
* Writing as **root** (which is what ``docker exec`` does) leaves a data directory PostgreSQL
  refuses to start on. The fix is not to chown afterwards but to never write as root: the combine
  runs ``-u postgres``.
* ``rm -rf $PGDATA`` cannot work: the *parent* is root-owned, so the database user can neither
  delete nor recreate the directory. Its **contents** are replaced instead.

* **A path the caller listed on the host is not a path the container can open.** The chain is
  found by listing directories on the host; the combine runs inside the container. When no volume
  serves that path the files must be copied in first (``stage_into_target`` in the script). Missing
  this was the fourth one: the combine kept reading a previous run's leftover copy and failed on
  whichever piece was newest, reporting `could not open file ".../PG_VERSION"` for a directory that
  was sitting on the host in plain sight.

None of that is new knowledge - ``assets/restore/postgresql/pg_restore_basebackup.sh`` has done it
correctly for months, and this module was worse than the script it was meant to generalise. These
are its rules, moved into the API.
"""

from __future__ import annotations

import shlex
from pathlib import PurePosixPath
from typing import Any

from db_ops.common.hostcmd import DOCKER, Host, parse_host, run
from db_ops.common.restorestep import DIFF, FULL, LOG, RestoreStepError

#: Written as this OS user so nothing in the data directory ends up owned by root.
DEFAULT_RUN_AS = "postgres"


def _in_container(host: Host, command: str, *, run_as: str) -> str:
    """``docker exec -u <user>`` — the ``-u`` is the whole point, see the module docstring."""
    inner = (f"docker exec -u {shlex.quote(run_as)} -i {shlex.quote(host.container)} "
             f"sh -lc {shlex.quote(command)}")
    return f"sudo {inner}" if host.sudo else inner


def _container_mounts(on_host: Host, container: str, *, sudo: bool) -> list[dict[str, Any]]:
    """``docker inspect``'s ``Mounts``, read once. Both directions of the translation need them."""
    import json as _json

    prefix = "sudo " if sudo else ""
    result = run(on_host, f"{prefix}docker inspect {shlex.quote(container)}", timeout=120)
    if result["exit_code"] != 0:
        raise RestoreStepError(f"could not inspect {container}: {result['stderr'][-200:]}")
    try:
        return _json.loads(result["stdout"])[0].get("Mounts") or []
    except Exception as exc:  # noqa: BLE001
        raise RestoreStepError(f"could not read the mounts of {container}: {exc}") from exc


def _to_host_path(mounts: list[dict[str, Any]], container: str, path: str) -> str:
    """Translate a path inside the container into the path the HOST sees.

    The combine runs inside the container and the swap runs on the host - the container is stopped
    by then - so the two halves describe the same directory with different strings. Building the
    swap out of container paths and running it on the host failed with
    `mv: cannot stat '/var/lib/postgresql/dbops_staging/*'`, which reads like a missing directory
    rather than the address-space mistake it is. Docker knows the mapping; it is asked.
    """
    # Longest destination first: /var/lib/postgresql/18 must win over /var/lib/postgresql.
    for mount in sorted(mounts, key=lambda m: len(str(m.get("Destination") or "")), reverse=True):
        dest = str(mount.get("Destination") or "").rstrip("/")
        src = str(mount.get("Source") or "").rstrip("/")
        if dest and src and (path == dest or path.startswith(dest + "/")):
            return src + path[len(dest):]
    raise RestoreStepError(
        f"{path} is not inside any volume of {container}, so the host cannot reach it while the "
        "container is stopped. Put the staging directory inside a mounted path."
    )


def _served_by_a_mount(mounts: list[dict[str, Any]], path: str) -> bool:
    """Does the container reach this path through one of its volumes?

    Then host and container are looking at one directory and there is nothing to copy - and
    nothing to delete either, because "the previous copy" would be the source backups themselves.
    """
    target = path.rstrip("/")
    for mount in mounts:
        dest = str(mount.get("Destination") or "").rstrip("/")
        if dest and (target == dest or target.startswith(dest + "/")):
            return True
    return False


def _stage_command(host: Host, path: str) -> str:
    """Put a directory the host has *inside* the container, at the same path.

    The combine and the server's ``restore_command`` both run in the container's namespace, so a
    path the caller listed on the host is only usable there if the container can actually see it.
    When no volume serves it, the files are copied in - which is what
    ``assets/restore/postgresql/pg_restore_basebackup.sh`` has always done and what this module
    failed to bring across when the scheduled restore moved onto these primitives. Without it the
    combine read whatever a previous run had left behind and died on the one piece that was new:
    `pg_combinebackup: could not open file ".../20260807T180432Z_INCR/PG_VERSION"` for a directory
    sitting on the host in plain sight.

    The ``rm -rf`` is not tidiness. ``docker cp`` into an existing directory *nests* the copy, and
    keeping a previous run's copy silently pins the restore to whatever was staged the first time -
    pieces deleted at the source stay here and get read hours later.
    """
    clean = path.rstrip("/")
    prefix = "sudo " if host.sudo else ""
    quoted = shlex.quote(clean)
    parent = shlex.quote(str(PurePosixPath(clean).parent))
    # -u 0: the database user owns its own directories but not /, and the staging path sits
    # outside them. The tree is handed back with a+rX because the database user has to read it.
    exec_as_root = f"{prefix}docker exec -u 0 -i {shlex.quote(host.container)} sh -lc "
    return (
        f'[ -d {quoted} ] || {{ echo "{clean} is not served by a mount inside '
        f'{host.container} and does not exist on this host either - nothing to stage." >&2; '
        f"exit 3; }}; "
        + exec_as_root + shlex.quote(f"rm -rf {quoted} && mkdir -p {parent}") + " && "
        + f"{prefix}docker cp {quoted} {shlex.quote(host.container + ':' + clean)} && "
        + exec_as_root + shlex.quote(f"chmod -R a+rX {quoted}")
    )


def _stage_into_container(on_host: Host, host: Host, paths: list[str],
                          mounts: list[dict[str, Any]]) -> list[str]:
    """Make every path readable inside the container. Returns the ones that had to be copied."""
    staged: list[str] = []
    for path in paths:
        clean = path.rstrip("/")
        if not clean or _served_by_a_mount(mounts, clean):
            continue
        result = run(on_host, _stage_command(host, clean), timeout=3600)
        if result["exit_code"] != 0:
            raise RestoreStepError(
                f"could not stage {clean} into {host.container}: "
                f"{(result['stderr'] or result['stdout']).strip()[-300:]}")
        staged.append(clean)
    return staged


def _host_of(host: Host) -> Host:
    """The same machine, addressed as a plain host.

    Stopping a container cannot be done from inside it, and neither can moving a directory the
    stopped container owns. Those steps are the host's, and the request already said which host.
    """
    from dataclasses import replace

    return replace(host, runtime="linux", container="")


def _combine_command(paths: list[str], staging: str, bin_dir: str) -> str:
    binary = (f"{bin_dir.rstrip('/')}/pg_combinebackup" if bin_dir else
              "$(command -v pg_combinebackup "
              "|| ls -1 /usr/lib/postgresql/*/bin/pg_combinebackup 2>/dev/null | tail -1)")
    sources = " ".join(shlex.quote(p.rstrip("/")) for p in paths)
    return (f"BIN={binary if bin_dir else binary}; "
            f'[ -x "$BIN" ] || {{ echo "pg_combinebackup not found" >&2; exit 127; }}; '
            f"rm -rf {shlex.quote(staging)} && "
            f'"$BIN" -o {shlex.quote(staging)} {sources}')


def apply(level: str, request: dict[str, Any], paths: list[str]) -> dict[str, Any]:
    host = parse_host(request.get("host"))
    data_dir = str(request.get("data_dir") or "").strip()
    run_as = str(request.get("run_as") or DEFAULT_RUN_AS).strip()
    bin_dir = str(request.get("bin_dir") or "").strip()
    dry_run = bool(request.get("dry_run"))
    steps: list[str] = []

    if level == LOG:
        if not data_dir:
            raise RestoreStepError("data_dir is required to write the recovery configuration.")
        wal_dir = str(request.get("wal_dir") or (paths[0] if paths else "")).strip()
        target_time = str(request.get("stopat") or "").strip()
        conf = f"{data_dir.rstrip('/')}/postgresql.auto.conf"
        lines = [f"restore_command = 'cp {wal_dir.rstrip('/')}/%f %p'",
                 "recovery_target_timeline = 'latest'"]
        if target_time:
            lines.append(f"recovery_target_time = '{target_time}'")
        body = "\n".join(lines)
        # Written where the files are: inside the container when it is up, on the host when the
        # caller is mid-swap and it is down. `run` on the parsed host does the right one.
        steps = [f"printf %s\\n {shlex.quote(body)} > {shlex.quote(conf)}",
                 f"touch {shlex.quote(data_dir.rstrip('/') + '/recovery.signal')}"]
        command = " && ".join(steps)
        if dry_run:
            return {"engine": "postgresql", "level": level, "command": command, "dry_run": True}
        # The WAL directory is read by the *server*, inside the container, through the
        # restore_command written just below - so it has to be reachable there for the same reason
        # the chain does. Writing a configuration that points at a host path the server cannot see
        # produces a cluster that starts and silently replays nothing.
        staged: list[str] = []
        if host.runtime == DOCKER and wal_dir:
            on_host = _host_of(host)
            staged = _stage_into_container(
                on_host, host, [wal_dir],
                _container_mounts(on_host, host.container, sudo=host.sudo))
        result = run(host, command)
        if result["exit_code"] != 0:
            raise RestoreStepError(f"writing recovery config failed: {result['stderr'][-300:]}")
        return {"engine": "postgresql", "level": level, "wrote": conf,
                "recovery_target_time": target_time or None,
                "staged_into_container": staged,
                "note": "WAL is replayed by the server at startup; nothing was applied here"}

    if not data_dir:
        raise RestoreStepError("data_dir is required: where the restored cluster will live.")
    if level == FULL and len(paths) != 1:
        raise RestoreStepError("a full restore takes exactly one base backup directory.")
    if level == DIFF and len(paths) < 2:
        raise RestoreStepError(
            "combining takes the full AND every incremental after it, base first - "
            "pg_combinebackup reads them together, and a chain missing its middle produces a "
            "data directory that starts and is incomplete."
        )

    staging = str(request.get("staging_dir") or (data_dir.rstrip("/") + "_dbops_staging"))
    action = ("base backup copied into the data directory" if level == FULL
              else "chain combined with pg_combinebackup")

    if level == FULL:
        build = (f"rm -rf {shlex.quote(staging)} && mkdir -p {shlex.quote(staging)} && "
                 f"cp -a {shlex.quote(paths[0].rstrip('/'))}/. {shlex.quote(staging)}/")
    else:
        build = _combine_command(paths, staging, bin_dir)

    # PGDATA's CONTENTS are replaced, never PGDATA itself: its parent is root-owned, so the
    # database user can neither delete nor recreate the directory - but it owns what is inside.
    swap = (f"shopt -s dotglob nullglob 2>/dev/null; rm -rf {shlex.quote(data_dir.rstrip('/'))}/* && "
            f"mv {shlex.quote(staging)}/* {shlex.quote(data_dir.rstrip('/'))}/ && "
            f"rmdir {shlex.quote(staging)} && chmod 700 {shlex.quote(data_dir.rstrip('/'))}")

    if host.runtime != DOCKER:
        command = f"{build} && {swap}"
        if dry_run:
            return {"engine": "postgresql", "level": level, "command": command,
                    "action": action, "dry_run": True}
        result = run(host, command, timeout=int(request.get("timeout_seconds") or 7200))
        if result["exit_code"] != 0:
            raise RestoreStepError(
                f"{action} failed (exit {result['exit_code']}): "
                f"{(result['stderr'] or result['stdout']).strip()[-400:]}")
        return {"engine": "postgresql", "level": level, "applied": paths,
                "data_dir": data_dir, "action": action}

    # --- container: combine while it runs, swap while it is down, then bring it back ---------
    #
    # The combine is the long part (minutes) and needs the binaries inside the image; the swap
    # needs the server stopped. Doing them in this order keeps the database down for seconds
    # rather than for the whole combine.
    on_host = _host_of(host)
    plan = {
        "stage": [_stage_command(host, p) for p in paths],
        "combine": _in_container(host, build, run_as=run_as),
        "stop": f"{'sudo ' if host.sudo else ''}docker stop {shlex.quote(host.container)}",
        "swap": _in_container(host, swap, run_as=run_as),
        "start": f"{'sudo ' if host.sudo else ''}docker start {shlex.quote(host.container)}",
    }
    if dry_run:
        return {"engine": "postgresql", "level": level, "plan": plan,
                "action": action, "dry_run": True}

    mounts = _container_mounts(on_host, host.container, sudo=host.sudo)
    # Before the combine, not during it: the combine runs in the container's namespace and reads
    # every path in the chain, so a path no volume serves has to be put there first.
    staged = _stage_into_container(on_host, host, paths, mounts)

    # Resolved BEFORE the container is stopped: `docker inspect` still answers afterwards, but
    # failing here costs nothing while failing there leaves the database down.
    host_data_dir = _to_host_path(mounts, host.container, data_dir.rstrip("/"))
    host_staging = _to_host_path(mounts, host.container, staging.rstrip("/"))
    host_swap = (f"shopt -s dotglob nullglob 2>/dev/null; rm -rf {shlex.quote(host_data_dir)}/* && "
                 f"mv {shlex.quote(host_staging)}/* {shlex.quote(host_data_dir)}/ && "
                 f"rmdir {shlex.quote(host_staging)} && chmod 700 {shlex.quote(host_data_dir)}")
    plan["swap"] = f"{'sudo ' if host.sudo else ''}sh -lc {shlex.quote(host_swap)}"

    combined = run(on_host, plan["combine"], timeout=int(request.get("timeout_seconds") or 7200))
    if combined["exit_code"] != 0:
        raise RestoreStepError(
            f"{action} failed (exit {combined['exit_code']}): "
            f"{(combined['stderr'] or combined['stdout']).strip()[-400:]}")

    stopped = run(on_host, plan["stop"], timeout=300)
    if stopped["exit_code"] != 0:
        raise RestoreStepError(f"could not stop {host.container}: {stopped['stderr'][-200:]}")
    try:
        # The container is down, so the swap runs on the host against the same paths - the volume
        # is mounted there. `docker cp`-free, and a rename rather than a copy.
        swapped = run(on_host, plan["swap"], timeout=1800)
        if swapped["exit_code"] != 0:
            raise RestoreStepError(
                f"could not move the combined cluster into place: "
                f"{(swapped['stderr'] or swapped['stdout']).strip()[-300:]}")
    finally:
        # Always brought back, even when the swap failed: leaving a lab node down is a second
        # incident on top of the first, and the caller can see what happened from the error.
        run(on_host, plan["start"], timeout=300)

    return {"engine": "postgresql", "level": level, "applied": paths,
            "data_dir": data_dir, "action": action, "container": host.container,
            "staged_into_container": staged,
            "note": "combined while up, swapped while down, container restarted"}
