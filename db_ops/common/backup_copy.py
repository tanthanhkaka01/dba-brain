"""Move backup files from the host that holds them to the host that will restore them.

Library tier: SSH clients arrive already open, from values the caller resolved. The CLI face is
:mod:`db_ops.common.cli_backup_copy` (``backup-chain``, ``copy-backup-dir``,
``prune-staged-backups``); until 0.23.0 this was ``common.backup_copy`` and ran inside the
app, so the copy and the staging cleanup were the two steps of a restore that were not
``common.cli`` commands of their own.

The copy goes through the orchestrator (SFTP down from the source, SFTP up to the target) rather
than having the target pull directly from the source. That is deliberate: the two database hosts
are not assumed to reach each other - the worker sits inside the internal network and the cloud
lab host is reachable over the internet, and a direct pull would need an SSH trust between the
database hosts that does not exist and should not be created just to run a drill. The
orchestrator already holds credentials for both ends, so it is the one place that can bridge
them without widening anyone's access.

The cost is bandwidth: every byte crosses the orchestrator twice. Files already present on the
target with the same size are skipped, so a repeated drill re-copies only what changed - which
for a backup directory that grows by one incremental a day is the difference between minutes and
hours.

**One stream, not one transfer per file.** A PostgreSQL backup directory is thousands of tiny
files (a 362 MB set measured here was 3901 files, 3764 of them under 64 KB). Copying them one
at a time over SFTP costs several network round trips *each*, and the orchestrator sits between
two hosts that are each an internet hop away - which measured 10 KB/s, i.e. eight hours for that
set, while the link itself was never the limit. So the files that need copying are streamed as a
single ``tar`` from the source into a single ``tar -x`` on the target: latency is paid once
instead of ~4000 times. Which files to copy is still decided the same way (same-size files are
skipped), so nothing about the semantics changes - only the number of round trips.
"""

from __future__ import annotations

import posixpath
import shlex
import stat
import threading
from dataclasses import dataclass
from typing import Any


@dataclass
class TransferResult:
    copied: int = 0
    skipped: int = 0
    bytes_copied: int = 0
    removed: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"copied": self.copied, "skipped": self.skipped, "bytes_copied": self.bytes_copied,
                "removed_absent_at_source": self.removed}


def _walk_remote(sftp, root: str, mtimes: dict[str, int] | None = None) -> tuple[list[tuple[str, int]], list[str]]:
    """((relative path, size) for every file, relative path of every directory) under ``root``.

    Directories are returned separately because copying only files loses the empty ones - and a
    PostgreSQL backup needs several of them (``pg_tblspc``, ``pg_replslot``, ``pg_stat_tmp``).
    Dropping them produces a directory that looks complete until pg_combinebackup or the server
    refuses it with "No such file or directory" for a path nobody deleted.
    """
    files: list[tuple[str, int]] = []
    dirs: list[str] = []
    unreadable: list[str] = []
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = sftp.listdir_attr(current)
        except IOError as exc:
            # Never swallow this. A backup directory the SSH user cannot read looks exactly like
            # an empty one, and a transfer that "succeeded" with almost nothing copied is worse
            # than one that failed: the restore then fails far away from the real cause.
            unreadable.append(f"{current} ({exc})")
            continue
        for entry in entries:
            path = posixpath.join(current, entry.filename)
            if stat.S_ISDIR(entry.st_mode or 0):
                dirs.append(posixpath.relpath(path, root))
                stack.append(path)
            else:
                files.append((posixpath.relpath(path, root), int(entry.st_size or 0)))
                if mtimes is not None and getattr(entry, "st_mtime", None) is not None:
                    mtimes[posixpath.relpath(path, root)] = int(entry.st_mtime)
    if unreadable:
        raise PermissionError(
            f"{len(unreadable)} director(ies) under {root} could not be read by the SSH user - "
            f"the transfer would silently copy an incomplete backup. First: {unreadable[0]}"
        )
    return files, dirs


def _older(target_mtime: int | None, source_mtime: int | None) -> bool:
    """Is the target's copy older than the source file? Unknown on either side is not older."""
    return target_mtime is not None and source_mtime is not None and target_mtime < source_mtime


def _assert_writable(sftp, directory: str) -> None:
    """Fail here, cheaply, rather than inside a tar stream that cannot report.

    The mirror of the unreadable-source check in :func:`_walk_remote`, and it exists for a
    sharper reason: a staging directory the SSH user cannot write is invisible to every check
    the transfer makes - it lists fine, it just refuses every file. `tar -x` then complains once
    per file, and since the copy is one long stream there is no round trip in which that error
    can surface. One probe file up front turns a two-hour hang into a sentence.
    """
    probe = posixpath.join(directory, ".db_ops_write_probe")
    try:
        with sftp.open(probe, "w") as handle:
            handle.write("probe")
    except IOError as exc:
        raise PermissionError(
            f"{directory} is not writable by the SSH user ({exc}). The transfer would stream a "
            f"whole backup into a directory that refuses every file. Check its ownership - a "
            f"staging directory recreated with sudo ends up root-owned."
        ) from exc
    try:
        sftp.remove(probe)
    except IOError:
        # Written but not removable is odd, not fatal: the copy itself will still work, and a
        # stray zero-byte probe is harmless next to failing the restore over it.
        pass


def _mkdirs(sftp, directory: str) -> None:
    parts = [p for p in directory.split("/") if p]
    current = ""
    for part in parts:
        current = f"{current}/{part}"
        try:
            sftp.stat(current)
        except IOError:
            try:
                sftp.mkdir(current)
            except IOError:
                # Another mkdir raced us, or it exists with a stat we cannot read; a later
                # put() will surface the real problem with a useful message.
                pass


class _Drained:
    """A stderr stream being emptied by a background thread, readable once the command ends."""

    def __init__(self, handle) -> None:
        self._chunks: list[bytes] = []
        self._thread = threading.Thread(target=self._pump, args=(handle,), daemon=True)
        self._thread.start()

    def _pump(self, handle) -> None:
        try:
            while True:
                chunk = handle.read(4096)
                if not chunk:
                    break
                self._chunks.append(chunk)
        except Exception:  # noqa: BLE001 - a closed channel is the normal end of the stream.
            pass

    def text(self) -> str:
        self._thread.join(timeout=5)
        return b"".join(self._chunks).decode("utf-8", "replace")


def _drain(handle) -> _Drained:
    return _Drained(handle)


def _stream_files(
    *,
    source_client,
    source_dir: str,
    target_client,
    target_dir: str,
    files: list[tuple[str, int]],
    log: Any = None,
    chunk_size: int = 1 << 18,
) -> bool:
    """Copy exactly ``files`` as one ``tar`` stream. False when tar is unusable on either end.

    Only the names decided by the caller are sent (``tar -T -`` reads them from stdin), so the
    "already copied, skip it" rule is unchanged — this collapses the *round trips*, not the
    decision. One stream instead of one transfer per file is the difference between minutes and
    hours when the two hosts are each an internet hop from the orchestrator.
    """
    names = "\n".join(rel.replace("\\", "/") for rel, _size in files) + "\n"
    total = sum(size for _rel, size in files)
    if log:
        log(f"streaming {len(files)} file(s), {total} bytes, as one tar")

    quoted_src = shlex.quote(source_dir)
    quoted_dst = shlex.quote(target_dir)
    src_in, src_out, src_err = source_client.exec_command(
        f"tar -cf - -C {quoted_src} --ignore-failed-read -T -", timeout=None
    )
    dst_in, dst_out, dst_err = target_client.exec_command(
        f"mkdir -p {quoted_dst} && tar -xf - -C {quoted_dst}", timeout=None
    )
    # Drain both stderr streams while the copy runs. Nothing read them until after
    # recv_exit_status(), which cannot be reached while the transfer is still going - so a tar
    # that complains on every file filled its stderr window, blocked on the write, stopped
    # reading stdin, and the whole pipeline seized with no error anywhere. Measured: a target
    # directory owned by root, with the SSH user unable to write, produced exactly that - the
    # source tar pushed 8.25 GB into a pipe nobody drained, the target extracted zero files,
    # and the run sat at RUNNING for the full two-hour timeout before anyone learned why.
    src_errors, dst_errors = _drain(src_err), _drain(dst_err)
    try:
        src_in.write(names)
        src_in.flush()
        src_in.channel.shutdown_write()

        moved = 0
        while True:
            chunk = src_out.read(chunk_size)
            if not chunk:
                break
            dst_in.write(chunk)
            moved += len(chunk)
        dst_in.flush()
        dst_in.channel.shutdown_write()

        src_rc = src_out.channel.recv_exit_status()
        dst_rc = dst_out.channel.recv_exit_status()
    except Exception as exc:  # noqa: BLE001 - fall back rather than fail the whole restore.
        if log:
            log(f"tar stream failed ({exc}); falling back")
        return False
    finally:
        for handle in (src_in, dst_in):
            try:
                handle.close()
            except Exception:  # noqa: BLE001
                pass

    if src_rc != 0 or dst_rc != 0:
        if log:
            detail = (src_errors.text() + dst_errors.text()).strip()
            log(f"tar stream exit src={src_rc} dst={dst_rc}: {detail[:300]}")
        return False
    if log:
        log(f"tar stream done: {moved} bytes")
    return True


def prune_target_dir(client, target_dir: str, older_than_seconds: int, *, log: Any = None) -> dict[str, Any]:
    """Delete files under ``target_dir`` older than ``older_than_seconds``. 0 disables it.

    The staging directory on the restore target only ever grew: the transfer adds what the source
    has and never removes what the source dropped, so a target that restores daily fills its disk
    with backup sets nobody can reach any more.

    Deletion is by age rather than by chain because a single cutoff is safe here by construction -
    the full backup is weekly, so the newest full is at most 7 days old and an 8-day cutoff keeps
    it together with every incremental chained to it. And deleting too much is recoverable: the
    next transfer compares against the source and re-copies whatever is missing. That is the
    reason this can be a one-line ``find`` rather than something that has to understand each
    engine's chain format.

    Run as one remote command: the alternative walks the tree over SFTP and issues a round trip
    per file, which is the cost this module already went to some length to remove.
    """
    result: dict[str, Any] = {"pruned": 0, "retention_seconds": int(older_than_seconds)}
    if not older_than_seconds or not target_dir:
        result["skipped"] = "retention disabled"
        return result
    minutes = max(1, int(older_than_seconds) // 60)
    quoted = shlex.quote(target_dir)
    # -mmin over -mtime: the threshold is configured in seconds, and -mtime's day granularity
    # would silently round a 24h setting to something else.
    prune_files = (
        f"find {quoted} -mindepth 1 -type f -mmin +{minutes} -print -delete 2>/dev/null | wc -l"
    )
    # Then remove the husk a fully pruned backup set leaves behind — but NOT a directory that is
    # empty *by design* inside a live one. `find -type d -empty -delete` deleted both, and a
    # PostgreSQL piece always carries empty pg_tblspc / pg_replslot / pg_stat_tmp: the transfer
    # recreated them, this ran seconds later and took them away again, and the restore then died
    # with `pg_combinebackup: could not open directory ".../pg_tblspc"` for a path nobody deleted
    # on the source. So a directory goes only when nothing anywhere beneath it is a file, and
    # only at the top two levels — deep enough to reach a backup set (`base/<stamp>_FULL`),
    # never deep enough to step inside one.
    # -depth (post-order): act on a directory only after its children, so find never tries to
    # descend into one this same command has just removed — which it reports on stderr and
    # exits 1 for, turning a successful prune into "prune command exited 1".
    drop_husks = (
        f"find {quoted} -mindepth 1 -maxdepth 2 -depth -type d -exec sh -c "
        f"'test -n \"$(find \"$1\" -type f -print -quit 2>/dev/null)\" || rm -rf \"$1\"' "
        f"_ {{}} \\; 2>/dev/null"
    )
    command = f"if [ -d {quoted} ]; then {prune_files}; {drop_husks}; else echo 0; fi"
    _in, out, _err = client.exec_command(command, timeout=None)
    text = out.read().decode("utf-8", errors="replace").strip()
    exit_status = out.channel.recv_exit_status()
    if exit_status != 0:
        result["error"] = f"prune command exited {exit_status}"
        return result
    try:
        result["pruned"] = int(text.splitlines()[0]) if text else 0
    except (ValueError, IndexError):
        result["pruned"] = 0
    if log:
        log(f"pruned {result['pruned']} file(s) older than {older_than_seconds}s from {target_dir}")
    return result


def sync_backup_dir(
    *,
    source_client,
    source_dir: str,
    target_client,
    target_dir: str,
    include: tuple[str, ...] = (),
    log: Any = None,
) -> TransferResult:
    """Copy ``source_dir`` to ``target_dir`` on another host, skipping identical files.

    ``include`` limits the copy to relative paths starting with one of the given prefixes, so a
    restore can pull only the parts of a backup directory it needs.
    """
    result = TransferResult()
    source_sftp = source_client.open_sftp()
    target_sftp = target_client.open_sftp()
    try:
        source_mtimes: dict[str, int] = {}
        files, dirs = _walk_remote(source_sftp, source_dir, source_mtimes)
        at_source = {rel.replace("\\", "/") for rel, _size in files}
        if include:
            files = [(rel, size) for rel, size in files if rel.replace("\\", "/").startswith(include)]
            dirs = [rel for rel in dirs if rel.replace("\\", "/").startswith(include)]
        # Created before it is walked. The walk refuses a directory it cannot list - rightly, on
        # the source - and a target folder a first run has not made yet is exactly that, so every
        # new cross-machine restore failed its first run with "[Errno 2] No such file".
        _mkdirs(target_sftp, target_dir)
        _assert_writable(target_sftp, target_dir)
        target_mtimes: dict[str, int] = {}
        existing_files, _ = _walk_remote(target_sftp, target_dir, target_mtimes)
        existing = {rel: size for rel, size in existing_files}

        # A mirror, not an accumulation: what the source no longer has goes here too. The copy
        # only ever added, so a source rebuilt under the same name left its previous life's pieces
        # beside the new ones - and a gvenzl Oracle lab has the image's DBID and incarnation in
        # every life, so RMAN could not tell them apart: a point-in-time duplicate followed the old
        # life's logs and asked for a sequence the database never reached (RMAN-06054, the
        # point-in-time drill, 2026-09-25). Judged against the WHOLE source listing, not the
        # `include`d part - an older point in time may need pieces a newest-chain copy skips.
        for rel in sorted(existing):
            if rel.replace("\\", "/") in at_source:
                continue
            try:
                target_sftp.remove(posixpath.join(target_dir, rel.replace("\\", "/")))
            except OSError:
                continue
            existing.pop(rel)
            result.removed += 1
        if result.removed and log:
            log(f"removed {result.removed} staged file(s) the source no longer has")

        # Every directory is recreated, including the empty ones a file-only copy would drop.
        for rel in sorted(dirs):
            _mkdirs(target_sftp, posixpath.join(target_dir, rel.replace("\\", "/")))

        pending: list[tuple[str, int]] = []
        for rel, size in sorted(files):
            # Size is the only cheap identity check available over SFTP. A backup piece that
            # changed content but kept its exact size would be missed - which is why backup
            # pieces are written under unique names and never rewritten in place.
            #
            # Same size is not enough when the target copy is OLDER than the source file. A
            # rebuilt source cluster starts its WAL names again at ...0001 - 16 MB each, the
            # size of the previous cluster's files still staged here - and skipping them
            # replayed the old cluster's WAL: "WAL file is from different database system",
            # a target crash-looping (2026-09-24). The tar stream keeps each file's time, so a
            # repeat run still skips what it sent before.
            if existing.get(rel) == size and not _older(target_mtimes.get(rel), source_mtimes.get(rel)):
                result.skipped += 1
                continue
            pending.append((rel, size))

        # Nothing new: the common case for a repeated drill, and it costs two directory walks
        # rather than a transfer.
        if pending:
            streamed = _stream_files(
                source_client=source_client, source_dir=source_dir,
                target_client=target_client, target_dir=target_dir,
                files=pending, log=log,
            )
            if streamed:
                result.copied += len(pending)
                result.bytes_copied += sum(size for _rel, size in pending)
            else:
                # tar missing or refused on either end: fall back to the per-file copy, which
                # is slow over a high-latency link but always works.
                if log:
                    log("tar stream unavailable; falling back to per-file SFTP copy")
                for rel, size in pending:
                    rel_posix = rel.replace("\\", "/")
                    remote_path = posixpath.join(target_dir, rel_posix)
                    _mkdirs(target_sftp, posixpath.dirname(remote_path))
                    with source_sftp.open(posixpath.join(source_dir, rel_posix), "rb") as reader:
                        reader.prefetch(size)
                        target_sftp.putfo(reader, remote_path, file_size=size)
                    result.copied += 1
                    result.bytes_copied += size
                    if log:
                        log(f"copied {rel_posix} ({size} bytes)")
    finally:
        source_sftp.close()
        target_sftp.close()
    return result


# --------------------------------------------------------------------------- #
# Which files a restore needs (moved here from backup_restore.restore_script with the copy, 0.23.0)
# --------------------------------------------------------------------------- #
def open_for_the_engine(target_client, directory: str, *, log: Any = None) -> bool:
    """Make the staged pieces readable by the database engine that restores them.

    The copy lands owned by the SSH user with the source's mode - `0660` for a SQL Server backup -
    and the engine reading it runs as someone else inside the target container: SQL Server as uid
    10001, which got "Operating system error 5 (Access is denied)" (Msg 3201) on every piece of the
    2026-09-24 lab drill. The SSH user owns what it has just written, so no sudo is needed. Not
    fatal when it fails - a piece left from an older run may belong to another user - but said,
    because the engine's own error then names the file.
    """
    _stdin, out, err = target_client.exec_command(f"chmod -R a+rX {shlex.quote(directory)}")
    code = out.channel.recv_exit_status()
    if code != 0 and log:
        log(f"could not open every staged piece to the engine under {directory} (chmod exit "
            f"{code}): {err.read().decode('utf-8', 'replace').strip()[:300]}")
    return code == 0


def chain_include(db_type: str, source_client, *, source_dir: str, backup_dir: str = "",
                  container: str = "", point_in_time: str = "", log: Any = None) -> tuple[str, ...]:
    """Which parts of the source backup directory a restore needs, as path prefixes.

    An empty tuple means "everything", which is what SQL Server gets and what every engine falls
    back to when the chain cannot be established.

    PostgreSQL's directory layout states the chain, so it is read from the names (see
    :func:`postgresql_chain_include`). An RMAN directory does not - level 0, level 1, archivelogs,
    controlfile autobackups and spfiles all sit side by side under generated names - so Oracle is
    narrowed by *asking RMAN*, never by parsing those names (:func:`oracle_chain_include`).

    **A point in time copies everything.** Both narrowings take the chain of the NEWEST full, and a
    moment before that full needs an older one: the staging folder would have held a chain that
    cannot reach the moment (found splitting the copy into its own command, 2026-09-25 - the lab
    drills had all picked moments after the newest full).
    """
    engine = str(db_type or "").strip().lower()
    if point_in_time:
        if log:
            log(f"point in time {point_in_time}: copying the whole backup directory - the chain it "
                "needs may start before the newest full")
        return ()
    if engine in {"postgresql", "postgres"}:
        return postgresql_chain_include(source_client, source_dir=source_dir, log=log)
    if engine == "oracle" and container:
        return oracle_chain_include(source_client, backup_dir=backup_dir or source_dir,
                                    container=container, log=log)
    return ()


def postgresql_chain_include(source_client, *, source_dir: str, log: Any = None) -> tuple[str, ...]:
    """``("base/<newest _FULL>", "base/<each _INCR after it>", "wal/")``.

    The restore combines exactly this set (``pg_combinebackup`` over the newest ``_FULL`` plus
    every ``_INCR`` whose stamp sorts after it), so anything else in ``base/`` is a chain the
    drill will not touch. Deciding it here, on the source, is what keeps those older chains from
    being copied and then pruned on every run.

    Falls back to "everything" whenever the listing is unusable: a narrowed copy that guessed
    wrong would fail the restore, while an un-narrowed one only costs bandwidth.
    """
    base = f"{source_dir.rstrip('/')}/base"
    command = f"ls -1d {shlex.quote(base)}/*_FULL {shlex.quote(base)}/*_INCR 2>/dev/null | sort"
    _stdin, stdout, _stderr = source_client.exec_command(command)
    names = [line.strip().rsplit("/", 1)[-1]
             for line in stdout.read().decode("utf-8", "replace").splitlines() if line.strip()]
    fulls = [name for name in names if name.endswith("_FULL")]
    if not fulls:
        if log:
            log("no _FULL backup found on the source; copying the whole backup directory")
        return ()
    newest_full = fulls[-1]
    chain = [newest_full] + [name for name in names
                             if name.endswith("_INCR") and name > newest_full]
    # wal/ always travels whole: recovery replays forward from the base backup, and which
    # segments it needs is decided by PostgreSQL at replay time, not by us here.
    include = tuple([f"base/{name}" for name in chain] + ["wal/"])
    if log:
        log(f"transfer narrowed to the restore chain: {len(chain)} backup(s) + wal/ "
            f"(source holds {len(names)} backup directories)")
    return include


# The restore needs every piece from the newest usable level 0 onward: that level 0, the level 1s
# chained to it, the archived-log backups that roll it forward, and the controlfile/spfile
# autobackups DUPLICATE starts from. The cutoff is not guessed - RESTORE ... PREVIEW is RMAN
# stating which datafile pieces it would use, and the cutoff is when the oldest of those sets
# completed. Everything the catalog recorded at or after that moment travels.
_ORACLE_PREVIEW = "RESTORE DATABASE PREVIEW;\nEXIT;\n"

_ORACLE_CHAIN_SQL = """set pagesize 0 feedback off heading off linesize 32767 trimspool on
SELECT p.handle
FROM v$backup_piece p
JOIN v$backup_set s ON s.set_stamp = p.set_stamp AND s.set_count = p.set_count
WHERE p.status = 'A' AND p.handle IS NOT NULL
  AND s.completion_time >= (
      SELECT MIN(s2.completion_time)
      FROM v$backup_set s2
      JOIN v$backup_piece p2 ON p2.set_stamp = s2.set_stamp AND p2.set_count = s2.set_count
      WHERE p2.handle IN ({handles})
  );
EXIT;
"""


def oracle_chain_include(source_client, *, backup_dir: str, container: str,
                         log: Any = None) -> tuple[str, ...]:
    """The basenames of every backup piece from the newest level 0 onward.

    An RMAN directory is flat, so a basename *is* the relative path and the prefix filter in
    :func:`sync_backup_dir` matches it exactly.

    Why this exists: the CLOUD lab's backup directory reached 90 GB, of which the pieces a restore
    actually needs were ~5 GB - the rest was seven days of controlfile autobackups, one every 15
    minutes at ~45 MB each, for a database whose data is 3.5 GB. The whole directory crossed the
    link on every drill and was then copied a second time *into* the target container by the restore
    script, so the drill needed roughly twice the backup directory in free space and eventually
    stopped fitting on a 193 GB disk at all.

    Why it asks RMAN rather than reading the names: which pieces form the chain is RMAN's decision,
    recorded in its catalog. Inferring it from ``FREE_L0_<date>_...`` would be a second, weaker copy
    of that logic, and being wrong here does not fail loudly - DUPLICATE restores to whatever point
    the pieces present allow, so a chain missing its incrementals still "succeeds", just at an older
    point than the operator believes.

    Falls back to "everything" whenever the answer is unusable: an un-narrowed copy only costs
    bandwidth, while a narrowed one that guessed wrong costs the restore.
    """
    directory = backup_dir.rstrip("/")
    handles = _oracle_preview_handles(source_client, container, log=log)
    if not handles:
        if log:
            log("RMAN preview named no backup pieces; copying the whole backup directory")
        return ()

    quoted = ", ".join("'" + h.replace("'", "''") + "'" for h in handles)
    rows = _oracle_sql(source_client, container, _ORACLE_CHAIN_SQL.format(handles=quoted))
    names: list[str] = []
    for line in rows:
        line = line.strip()
        # Only pieces inside the directory being transferred; a handle elsewhere on the source
        # (an FRA copy, say) has no counterpart to include here.
        if line.startswith(directory + "/"):
            names.append(line.rsplit("/", 1)[-1])
    if not names:
        if log:
            log("no catalog pieces resolved under the backup dir; copying the whole directory")
        return ()
    if log:
        log(f"transfer narrowed to the RMAN chain: {len(names)} piece(s) from the newest level 0")
    return tuple(sorted(set(names)))


def _oracle_preview_handles(source_client, container: str, *, log: Any = None) -> list[str]:
    """The datafile piece handles from ``RESTORE DATABASE PREVIEW`` - RMAN's own answer."""
    out = _run_in_container(
        source_client, container,
        f"printf {shlex.quote(_ORACLE_PREVIEW)} | rman target / log /dev/stdout 2>&1",
    )
    handles = []
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.startswith("Piece Name:"):
            handles.append(stripped.split(":", 1)[1].strip())
    if log and not handles:
        log("RESTORE DATABASE PREVIEW returned no piece names")
    return handles


def _oracle_sql(source_client, container: str, script: str) -> list[str]:
    out = _run_in_container(
        source_client, container,
        f"printf {shlex.quote(script)} | sqlplus -s -L / as sysdba 2>&1",
    )
    return [line for line in out.splitlines() if line.strip()]


def _run_in_container(source_client, container: str, command: str) -> str:
    """Run a read-only query inside the source database container over the host's SSH access."""
    from db_ops.lib.shell import docker_cli

    # Plain docker first, sudo only as the fallback (db_ops.lib.shell.docker_cli): `sudo` alone fails
    # where sudo wants a password, and the SSH user is usually in the docker group.
    inner = f"{docker_cli(True)} exec -i {shlex.quote(container)} bash -lc {shlex.quote(command)}"
    _stdin, stdout, _stderr = source_client.exec_command(inner)
    return stdout.read().decode("utf-8", "replace")
