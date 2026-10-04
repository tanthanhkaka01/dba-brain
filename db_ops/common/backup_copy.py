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

from db_ops.lib import errors
import errno
import posixpath
import shlex
import stat
import threading
from dataclasses import dataclass
from typing import Any


# How the files cross (``copy_mode``): ``auto`` tries one tar stream and steps down to a transfer
# per file when tar cannot be used on either end; ``tar`` and ``sftp`` pin one of the two, and a
# pinned mode that cannot do it is an error, not a reason to try the other (owner decision G4).
# The words are ``lib``'s - the restore entry states them too.
from db_ops.lib import restore_space
from db_ops.lib.restore.copy_mode import COPY_AUTO, COPY_MODES, COPY_NONE, COPY_SFTP, COPY_TAR


class CopyModeError(errors.Refused):
    """The copy was pinned to a mode that could not do it."""


class CopySpaceError(errors.Refused):
    """The files to copy do not fit on the target, or its free space could not be read."""


@dataclass
class TransferResult:
    copied: int = 0
    skipped: int = 0
    bytes_copied: int = 0
    removed: int = 0
    # Which way the files went - ``tar``, ``sftp``, or ``none`` when nothing needed copying - and
    # whether that was the step down from a tar stream. It was only ever a line on stderr: a drill
    # that took hours instead of minutes looked, in its answer, like any other (G4).
    copy_mode: str = COPY_NONE
    fell_back: bool = False
    # What the space check measured before the first byte moved - ``None`` when there was nothing
    # to copy, or no rule was given.
    space: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        answer = {"copied": self.copied, "skipped": self.skipped, "bytes_copied": self.bytes_copied,
                  "removed_absent_at_source": self.removed,
                  "copy_mode": self.copy_mode, "copy_fell_back": self.fell_back}
        if self.space is not None:
            answer["space_check"] = self.space
        return answer


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
    vanished: list[str] = []
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = sftp.listdir_attr(current)
        except IOError as exc:
            # Never swallow this. A backup directory the SSH user cannot read looks exactly like
            # an empty one, and a transfer that "succeeded" with almost nothing copied is worse
            # than one that failed: the restore then fails far away from the real cause.
            # A folder that is not there is not one that cannot be read, and saying so matters: a
            # restore scheduled before its first backup was told the SSH user could not read a
            # folder no backup had created yet (the 0.25.0 soak, 2026-09-29).
            if _is_missing(exc):
                if current == root:
                    raise FileNotFoundError(
                        f"{root} does not exist on that host - no backup has been written there "
                        f"yet, or the path is wrong. Nothing was copied.") from exc
                vanished.append(current)
                continue
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
    if vanished:
        raise FileNotFoundError(
            f"{len(vanished)} director(ies) under {root} disappeared while it was being listed - "
            f"removed as it was read, retention most likely; a copy of a folder that is changing "
            f"is not a backup. The next run lists it again. First: {vanished[0]}"
        )
    return files, dirs


def _is_missing(exc: OSError) -> bool:
    """SFTP's "no such file": paramiko raises it as an IOError carrying ENOENT."""
    return isinstance(exc, FileNotFoundError) or getattr(exc, "errno", None) == errno.ENOENT


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
    source_session,
    source_dir: str,
    target_session,
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
    src_in, src_out, src_err = source_session.open_stream(
        f"tar -cf - -C {quoted_src} --ignore-failed-read -T -"
    )
    dst_in, dst_out, dst_err = target_session.open_stream(
        f"mkdir -p {quoted_dst} && tar -xf - -C {quoted_dst}"
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


#: The file a staging directory carries once this module owns it. Mirroring and pruning delete what
#: they find under the directory, so they act only where this file says the directory is theirs: a
#: `target_backup_dir` pointed at the target host's own backups, at a shared folder or at another
#: entry's staging used to be emptied without a word (review 0.25.0, B4.7).
STAGING_MARKER = ".dbops-staging"


class StagingDirError(errors.Refused):
    """``target_dir`` holds files and is not marked as a db_ops staging directory."""


def _not_a_staging_dir(target_dir: str) -> StagingDirError:
    marker = posixpath.join(target_dir, STAGING_MARKER)
    return StagingDirError(
        f"{target_dir} already holds files, none of which is at the source, and has no "
        f"{STAGING_MARKER} marker, so it is not known to be a db_ops staging directory - nothing "
        f"was copied or deleted there. If it is one (a "
        f"staging folder from before 0.25.0), mark it: `touch {marker}`. Otherwise give the entry "
        f"its own, empty target_backup_dir.")


def prune_target_dir(session, target_dir: str, older_than_seconds: int, *, log: Any = None) -> dict[str, Any]:
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
    marker = shlex.quote(STAGING_MARKER)
    # -mmin over -mtime: the threshold is configured in seconds, and -mtime's day granularity
    # would silently round a 24h setting to something else.
    prune_files = (
        f"find {quoted} -mindepth 1 -type f ! -name {marker} -mmin +{minutes} -print -delete "
        f"2>/dev/null | wc -l"
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
    # Only a marked directory is pruned (B4.7); an unmarked one answers -1 and is refused below.
    command = (f"if [ ! -d {quoted} ]; then echo 0; "
               f"elif [ ! -f {quoted}/{marker} ]; then echo -1; "
               f"else {prune_files}; {drop_husks}; fi")
    _in, out, _err = session.open_stream(command)
    text = out.read().decode("utf-8", errors="replace").strip()
    exit_status = out.channel.recv_exit_status()
    if exit_status != 0:
        result["error"] = f"prune command exited {exit_status}"
        return result
    try:
        result["pruned"] = int(text.splitlines()[0]) if text else 0
    except (ValueError, IndexError):
        result["pruned"] = 0
    if result["pruned"] < 0:
        result["pruned"] = 0
        result["error"] = str(_not_a_staging_dir(target_dir))
        return result
    if log:
        log(f"pruned {result['pruned']} file(s) older than {older_than_seconds}s from {target_dir}")
    return result


def _target_free_bytes(target_session, directory: str) -> int | None:
    """Free bytes on the filesystem ``directory`` is on, as the target answers - ``None`` if not."""
    try:
        _stdin, out, _err = target_session.open_stream(restore_space.free_space_command(directory))
        text = out.read().decode("utf-8", "replace")
        out.channel.recv_exit_status()
    except Exception:  # noqa: BLE001 - whatever stopped the reading, the free space is not known.
        return None
    return restore_space.parse_df_free_bytes(text)


def check_room(target_session, target_dir: str, pending: list[tuple[str, int]],
               rule: restore_space.SpaceCheck, *, log: Any = None) -> dict[str, Any]:
    """Refuse a copy whose files do not fit on the target, with room to spare - before any moves.

    The rule is the restore entry's ``space_check`` (:mod:`db_ops.lib.restore_space`):
    ``free >= bytes to copy x factor``. It has stopped the copy of a share-driven SQL Server
    restore since 2026-09-19; the copy between two hosts - every script-driven restore onto
    another machine - asked nothing, while the entry's ``space_check`` was read by nobody and its
    reference said *absent means on* (found 2026-10-02, review notes R8). The operator's rule for
    it: the restore script checks nothing itself - the tool has checked before the script runs.
    ``pending`` is what this copy will write: a file the target already holds is not in it.

    Returns what it measured; raises :class:`CopySpaceError` on a shortfall, and on a target whose
    free space cannot be read unless the rule says ``on_unknown: proceed``.
    """
    def say(text: str) -> None:
        if log:
            log(f"space check: {text}")

    if not rule.enabled:
        say("disabled by space_check.enabled=false")
        return {"checked": False, "reason": "disabled"}
    incoming = sum(size for _rel, size in pending)
    free = _target_free_bytes(target_session, target_dir)
    if free is None:
        detail = f"could not read the free space of {target_dir} on the target"
        if rule.on_unknown == "proceed":
            say(detail + " - proceeding, because space_check.on_unknown=proceed")
            return {"checked": False, "reason": "unknown", "incoming_bytes": incoming}
        raise CopySpaceError(
            f"space check: {detail}. Refusing: an unmeasured copy is the one that filled the disk. "
            'Set space_check {"on_unknown": "proceed"} on this entry to accept that, or '
            '{"enabled": false} to turn the check off. No file was copied.')
    verdict = restore_space.judge(incoming, free, rule.factor)
    say(verdict.text)
    if not verdict.ok:
        raise CopySpaceError(
            f"{target_dir} will not fit: {verdict.text}. Free "
            f"{restore_space.format_gib(verdict.shortfall_bytes)} on the target, lower "
            f"space_check.factor (now {verdict.factor:g}), or stage somewhere else. No file was "
            "copied.")
    return {"checked": True, "ok": True, "incoming_bytes": verdict.incoming_bytes,
            "free_bytes": verdict.free_bytes, "required_bytes": verdict.required_bytes,
            "factor": verdict.factor, "detail": verdict.text}


def sync_backup_dir(
    *,
    source_session,
    source_dir: str,
    target_session,
    target_dir: str,
    include: tuple[str, ...] = (),
    log: Any = None,
    copy_mode: str = COPY_AUTO,
    space_check: restore_space.SpaceCheck | None = None,
    window_since: float | None = None,
) -> TransferResult:
    """Copy ``source_dir`` to ``target_dir`` on another host, skipping identical files.

    ``include`` limits the copy to relative paths starting with one of the given prefixes, so a
    restore can pull only the parts of a backup directory it needs. ``window_since`` (epoch seconds)
    widens a narrowed copy by every file the source wrote at or after it - ``copy_selection:
    window``: the chain, and every other restore point of the window. ``copy_mode`` is one of
    :data:`COPY_MODES`; the result says which way the files went. ``space_check`` is the rule the
    files to copy are held to before the first one moves (:func:`check_room`); ``None`` asks nothing.
    """
    if copy_mode not in COPY_MODES:
        raise ValueError(f"copy_mode must be one of {', '.join(COPY_MODES)}, got: {copy_mode!r}")
    result = TransferResult()
    # The sessions own their SFTP channels and close them with themselves.
    source_sftp = source_session.sftp()
    target_sftp = target_session.sftp()
    source_mtimes: dict[str, int] = {}
    files, dirs = _walk_remote(source_sftp, source_dir, source_mtimes)
    at_source = {rel.replace("\\", "/") for rel, _size in files}
    dirs_at_source = {rel.replace("\\", "/").rstrip("/") for rel in dirs}
    if include:
        # The chain, and - for a window copy - every file written inside the window. A window
        # never narrows the chain: the restore always finds the backups it is going to apply.
        def kept(rel: str) -> bool:
            if rel.replace("\\", "/").startswith(include):
                return True
            return window_since is not None and source_mtimes.get(rel, 0) >= window_since

        files = [(rel, size) for rel, size in files if kept(rel)]
        parents = {posixpath.dirname(rel.replace("\\", "/")) for rel, _size in files}
        ancestors = {"/".join(p.split("/")[:n]) for p in parents for n in range(1, p.count("/") + 2) if p}
        dirs = [rel for rel in dirs
                if rel.replace("\\", "/").startswith(include) or rel.replace("\\", "/") in ancestors]
    # Created before it is walked. The walk refuses a directory it cannot list - rightly, on
    # the source - and a target folder a first run has not made yet is exactly that, so every
    # new cross-machine restore failed its first run with "[Errno 2] No such file".
    _mkdirs(target_sftp, target_dir)
    _assert_writable(target_sftp, target_dir)
    target_mtimes: dict[str, int] = {}
    existing_files, existing_dirs = _walk_remote(target_sftp, target_dir, target_mtimes)
    existing = {rel: size for rel, size in existing_files if rel.replace("\\", "/") != STAGING_MARKER}
    if len(existing) == len(existing_files):
        # No marker. An empty directory becomes ours. One that already holds files is adopted when
        # it is plainly an earlier staging copy of THIS source - at least one of its files is also
        # at the source, under the same relative path - so a staging folder from before the marker
        # needs no manual step (owner, 2026-10-01: a fix must not make the tool harder to use).
        # Anything else is refused rather than mirrored: mirroring deletes whatever the source
        # does not have, and a folder sharing nothing with the source is someone else's (B4.7).
        if existing and not any(rel.replace("\\", "/") in at_source for rel in existing):
            raise _not_a_staging_dir(target_dir)
        if existing and log:
            log(f"{target_dir}: adopted as this source's staging directory (marked {STAGING_MARKER})")
        with target_sftp.open(posixpath.join(target_dir, STAGING_MARKER), "w") as handle:
            handle.write(b"db_ops restore staging directory - mirrored and pruned by db_ops.\n")
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
    # And the directories the source no longer has - the husk a removed backup set leaves. Only its
    # files went, so `base/<stamp>_FULL` stayed, empty and dated by the removal itself; a PostgreSQL
    # listing reads a backup off its directory name, so the husk became the newest full and the
    # restore combined it with every incremental after it: "pg_combinebackup: could not open file
    # .../<stamp>_INCR/global/pg_control" in most hours of the 0.26 node (2026-10-03). Judged against
    # the whole source listing, like the files. Deepest first, so a parent is empty by the time it
    # is tried; rmdir refuses a directory that is not, so nothing holding a file is touched.
    removed_dirs = 0
    for rel in sorted((d.replace("\\", "/").rstrip("/") for d in existing_dirs),
                      key=lambda d: d.count("/"), reverse=True):
        if not rel or rel in dirs_at_source:
            continue
        try:
            target_sftp.rmdir(posixpath.join(target_dir, rel))
        except OSError:
            continue
        removed_dirs += 1
    if removed_dirs and log:
        log(f"removed {removed_dirs} staged directory(ies) the source no longer has")
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
        if space_check is not None:
            # After the mirror above, which only frees room, and before the first byte.
            result.space = check_room(target_session, target_dir, pending, space_check, log=log)
        streamed = copy_mode != COPY_SFTP and _stream_files(
            source_session=source_session, source_dir=source_dir,
            target_session=target_session, target_dir=target_dir,
            files=pending, log=log,
        )
        if streamed:
            result.copy_mode = COPY_TAR
            result.copied += len(pending)
            result.bytes_copied += sum(size for _rel, size in pending)
        elif copy_mode == COPY_TAR:
            raise CopyModeError(
                f"copy_mode is tar, and the tar stream from {source_dir} to {target_dir} could not "
                "be used (the lines above say why). Nothing was copied file by file: the mode is "
                "pinned. Fix tar on both hosts, or set copy_mode to auto or sftp.")
        else:
            # tar missing or refused on either end: fall back to the per-file copy, which
            # is slow over a high-latency link but always works.
            result.copy_mode = COPY_SFTP
            result.fell_back = copy_mode == COPY_AUTO
            if log and result.fell_back:
                log("tar stream unavailable; falling back to per-file SFTP copy")
            for rel, size in pending:
                rel_posix = rel.replace("\\", "/")
                remote_path = posixpath.join(target_dir, rel_posix)
                _mkdirs(target_sftp, posixpath.dirname(remote_path))
                with source_sftp.open(posixpath.join(source_dir, rel_posix), "rb") as reader:
                    reader.prefetch(size)
                    target_sftp.putfo(reader, remote_path, file_size=size)
                # The piece keeps the source's time, as the tar stream does. Dated by the copy, a
                # PostgreSQL manifest copied after a newer one read as the newest backup - the
                # restore plan dates a backup by it - and a point in time found no full before it.
                mtime = source_mtimes.get(rel)
                if mtime is not None:
                    target_sftp.utime(remote_path, (mtime, mtime))
                result.copied += 1
                result.bytes_copied += size
                if log:
                    log(f"copied {rel_posix} ({size} bytes)")
    return result


# --------------------------------------------------------------------------- #
# Which files a restore needs (moved here from backup_restore.restore_script with the copy, 0.23.0)
# --------------------------------------------------------------------------- #
def open_for_the_engine(target_session, directory: str, *, log: Any = None) -> bool:
    """Make the staged pieces readable by the database engine that restores them.

    The copy lands owned by the SSH user with the source's mode - `0660` for a SQL Server backup -
    and the engine reading it runs as someone else inside the target container: SQL Server as uid
    10001, which got "Operating system error 5 (Access is denied)" (Msg 3201) on every piece of the
    2026-09-24 lab drill. The SSH user owns what it has just written, so no sudo is needed. Not
    fatal when it fails - a piece left from an older run may belong to another user - but said,
    because the engine's own error then names the file.
    """
    _stdin, out, err = target_session.open_stream(f"chmod -R a+rX {shlex.quote(directory)}")
    code = out.channel.recv_exit_status()
    if code != 0 and log:
        log(f"could not open every staged piece to the engine under {directory} (chmod exit "
            f"{code}): {err.read().decode('utf-8', 'replace').strip()[:300]}")
    return code == 0


def chain_include(db_type: str, source_session, *, source_dir: str, backup_dir: str = "",
                  container: str = "", point_in_time: str = "", log: Any = None) -> tuple[str, ...]:
    """Which parts of the source backup directory a restore needs, as path prefixes.

    An empty tuple means "everything" - a point in time, or a SQL Server layout the names cannot be
    read from.

    SQL Server's layout states the chain too (``<database>/FULL|DIFF|LOG/<name>_<stamp>Z.bak``):
    each database's newest FULL, its newest DIFF and the LOGs after them, by the rule the share
    copy already follows (:mod:`db_ops.lib.sqlserver_backup_chain`). Until 2026-10-03 a SQL Server
    copy to another machine took the whole directory, whatever its entry's ``copy_selection`` said.

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
        return postgresql_chain_include(source_session, source_dir=source_dir, log=log)
    if engine in {"sqlserver", "mssql"}:
        return sqlserver_chain_include(source_session, source_dir=source_dir, log=log)
    if engine == "oracle":
        if not container:
            # RMAN is asked inside the source container; without it the chain is unknown, and
            # copying the whole directory instead was a fallback (review 0.25.0, G2.9).
            raise ChainUnknownError("the Oracle source names no container to ask RMAN in.")
        return oracle_chain_include(source_session, backup_dir=backup_dir or source_dir,
                                    container=container, log=log)
    return ()


class ChainUnknownError(errors.OperationFailed):
    """The source cannot say which pieces form the restore chain, so nothing is copied.

    It used to copy the whole directory instead. Owner decision 2026-10-01 (review 0.25.0, G2.9):
    no fallback - a set nobody chose is not copied, and the restore fails with the reason.
    """


def sqlserver_chain_include(source_session, *, source_dir: str, log: Any = None) -> tuple[str, ...]:
    """Each database's restore chain, the certificate folder, as relative paths.

    Read from the layout the backup jobs write - ``<database>/FULL|DIFF|LOG/<file>``, the time in
    the name (or the file's mtime when the name carries none) - with the share copy's own rule, so
    both ways to a target copy the same backups. ``_cert/`` always travels: an encrypted backup
    cannot be read on the target without it.

    A database with no FULL is copied whole, and a layout with no ``FULL`` / ``DIFF`` / ``LOG``
    folders at all is copied whole - what every SQL Server copy did before: a narrowed copy that
    guessed wrong fails the restore, a wide one only costs time.
    """
    from db_ops.lib import sqlserver_backup_chain as chain_rule

    command = (f"cd {shlex.quote(source_dir.rstrip('/'))} && "
               "find . -type f -printf '%P|%T@\\n' 2>/dev/null")
    _stdin, stdout, _stderr = source_session.open_stream(command)
    candidates = []
    for line in stdout.read().decode("utf-8", "replace").splitlines():
        rel, _sep, stamp = line.strip().rpartition("|")
        parts = rel.split("/")
        if len(parts) < 3 or not chain_rule.kind_from_folder(parts[-2]):
            continue
        try:
            mtime = float(stamp)
        except ValueError:
            mtime = 0.0
        candidates.append(chain_rule.Candidate(
            item=rel, database=parts[-3], kind=chain_rule.kind_from_folder(parts[-2]),
            timestamp=chain_rule.backup_time_from_name(parts[-1]) or mtime))
    if not candidates:
        if log:
            log(f"no FULL / DIFF / LOG folders under {source_dir}: copying the whole directory")
        return ()
    choice = chain_rule.restore_chain(candidates)
    folders = {str(c.item).rsplit("/", 2)[0]: c.database for c in candidates}
    whole = sorted(f"{folder}/" for folder, database in folders.items()
                   if database in choice.unresolved)
    if log:
        for line in choice.lines:
            log(line.replace("copied by its window instead", "copied whole"))
    return tuple(sorted(str(item) for item in choice.selected)) + tuple(whole) + ("_cert/",)


def postgresql_chain_include(source_session, *, source_dir: str, log: Any = None) -> tuple[str, ...]:
    """``("base/<newest _FULL>", "base/<each _INCR after it>", "wal/")``.

    The restore combines exactly this set (``pg_combinebackup`` over the newest ``_FULL`` plus
    every ``_INCR`` whose stamp sorts after it), so anything else in ``base/`` is a chain the
    drill will not touch. Deciding it here, on the source, is what keeps those older chains from
    being copied and then pruned on every run.

    A listing with no ``_FULL`` raises :class:`ChainUnknownError`: there is no chain to restore,
    and copying the whole directory instead was a fallback (review 0.25.0, G2.9).
    """
    base = f"{source_dir.rstrip('/')}/base"
    command = f"ls -1d {shlex.quote(base)}/*_FULL {shlex.quote(base)}/*_INCR 2>/dev/null | sort"
    _stdin, stdout, _stderr = source_session.open_stream(command)
    names = [line.strip().rsplit("/", 1)[-1]
             for line in stdout.read().decode("utf-8", "replace").splitlines() if line.strip()]
    fulls = [name for name in names if name.endswith("_FULL")]
    if not fulls:
        raise ChainUnknownError(
            f"no _FULL backup under {base} on the source - there is no chain to restore from. "
            "Run the full backup job first.")
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

# Where the restore will STOP, which is not the newest level 0. A DUPLICATE from a backup location
# with no UNTIL recovers through the newest archived-log backup in it and ends there, so a level 0
# taken after that log is one it cannot use: it needs the level 0 before. Measured on the 0.26.0
# lab, 2026-10-04: a level 0 checkpointed at SCN 2899140, the newest log backup ending at 2898894,
# the chain cut at that level 0 - and RMAN-06023 "no backup or copy of datafile 1 found to
# restore" on every run until the next archivelog job. So the preview is asked UNTIL that SCN.
_ORACLE_NEWEST_LOG_SQL = """set pagesize 0 feedback off heading off
SELECT MAX(r.next_change#)
FROM v$backup_redolog r
JOIN v$backup_piece p ON p.set_stamp = r.set_stamp AND p.set_count = r.set_count
WHERE p.status = 'A' AND p.handle LIKE '{directory}/%';
EXIT;
"""

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


def oracle_chain_include(source_session, *, backup_dir: str, container: str,
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

    An unusable answer raises :class:`ChainUnknownError` rather than copying the whole directory
    (review 0.25.0, G2.9): RMAN could not name the chain, so neither can this.
    """
    directory = backup_dir.rstrip("/")
    until_scn = _oracle_newest_log_scn(source_session, container, directory)
    handles = _oracle_preview_handles(source_session, container, until_scn=until_scn, log=log)
    if not handles:
        raise ChainUnknownError(
            f"RMAN preview in {container} named no backup pieces - the restore chain is unknown.")

    quoted = ", ".join("'" + h.replace("'", "''") + "'" for h in handles)
    rows = _oracle_sql(source_session, container, _ORACLE_CHAIN_SQL.format(handles=quoted))
    names: list[str] = []
    for line in rows:
        line = line.strip()
        # Only pieces inside the directory being transferred; a handle elsewhere on the source
        # (an FRA copy, say) has no counterpart to include here.
        if line.startswith(directory + "/"):
            names.append(line.rsplit("/", 1)[-1])
    if not names:
        raise ChainUnknownError(
            f"no catalog piece of the restore chain lies under {directory} - the directory being "
            "transferred does not hold the chain.")
    if log:
        log(f"transfer narrowed to the RMAN chain: {len(names)} piece(s) from the newest level 0")
    return tuple(sorted(set(names)))


def _oracle_newest_log_scn(source_session, container: str, directory: str) -> int | None:
    """The SCN the newest archived-log backup in ``directory`` reaches, or ``None`` if it holds none."""
    rows = _oracle_sql(source_session, container,
                       _ORACLE_NEWEST_LOG_SQL.format(directory=directory.replace("'", "''")))
    for line in rows:
        if line.strip().isdigit():
            return int(line.strip())
    return None


def _oracle_preview_handles(source_session, container: str, *, until_scn: int | None = None,
                            log: Any = None) -> list[str]:
    """The datafile piece handles from ``RESTORE DATABASE PREVIEW`` - RMAN's own answer, for the
    point the restore will recover to when the backup location holds a log backup to stop at."""
    script = (f"RESTORE DATABASE UNTIL SCN {until_scn} PREVIEW;\nEXIT;\n" if until_scn
              else _ORACLE_PREVIEW)
    out = _run_in_container(
        source_session, container,
        f"printf {shlex.quote(script)} | rman target / log /dev/stdout 2>&1",
    )
    handles = []
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.startswith("Piece Name:"):
            handles.append(stripped.split(":", 1)[1].strip())
    if log and not handles:
        log("RESTORE DATABASE PREVIEW returned no piece names")
    return handles


def _oracle_sql(source_session, container: str, script: str) -> list[str]:
    out = _run_in_container(
        source_session, container,
        f"printf {shlex.quote(script)} | sqlplus -s -L / as sysdba 2>&1",
    )
    return [line for line in out.splitlines() if line.strip()]


def _run_in_container(source_session, container: str, command: str) -> str:
    """Run a read-only query inside the source database container over the host's SSH access."""
    from db_ops.lib.shell import docker_cli

    # Plain docker first, sudo only as the fallback (db_ops.lib.shell.docker_cli): `sudo` alone fails
    # where sudo wants a password, and the SSH user is usually in the docker group.
    inner = f"{docker_cli(True)} exec -i {shlex.quote(container)} bash -lc {shlex.quote(command)}"
    _stdin, stdout, _stderr = source_session.open_stream(inner)
    return stdout.read().decode("utf-8", "replace")
