"""Move backup files from the host that holds them to the host that will restore them.

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

    def as_dict(self) -> dict[str, Any]:
        return {"copied": self.copied, "skipped": self.skipped, "bytes_copied": self.bytes_copied}


def _walk_remote(sftp, root: str) -> tuple[list[tuple[str, int]], list[str]]:
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
    if unreadable:
        raise PermissionError(
            f"{len(unreadable)} director(ies) under {root} could not be read by the SSH user - "
            f"the transfer would silently copy an incomplete backup. First: {unreadable[0]}"
        )
    return files, dirs


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
        files, dirs = _walk_remote(source_sftp, source_dir)
        if include:
            files = [(rel, size) for rel, size in files if rel.replace("\\", "/").startswith(include)]
            dirs = [rel for rel in dirs if rel.replace("\\", "/").startswith(include)]
        existing_files, _ = _walk_remote(target_sftp, target_dir)
        existing = {rel: size for rel, size in existing_files}
        _mkdirs(target_sftp, target_dir)
        _assert_writable(target_sftp, target_dir)

        # Every directory is recreated, including the empty ones a file-only copy would drop.
        for rel in sorted(dirs):
            _mkdirs(target_sftp, posixpath.join(target_dir, rel.replace("\\", "/")))

        pending: list[tuple[str, int]] = []
        for rel, size in sorted(files):
            # Size is the only cheap identity check available over SFTP. A backup piece that
            # changed content but kept its exact size would be missed - which is why backup
            # pieces are written under unique names and never rewritten in place.
            if existing.get(rel) == size:
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
