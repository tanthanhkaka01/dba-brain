"""Prepare a restore's staging folder for the backup files it needs - and say which ones.

Library tier: SSH clients arrive already open, from values the caller resolved. The CLI face is
:mod:`dbabrain.common.cli_backup_copy` (``prepare-staging``, ``finish-staging``).

**It moves no byte and deletes nothing** (0.38.0, 1.329). Until then this module was the copy
itself (``copy-backup-dir``): it chose the files, streamed them as one ``tar`` (or file by file
over SFTP), and mirrored - removing from the staging whatever the source no longer had. Three jobs
in one command, two of them now somebody else's:

* **the bytes** travel by ``copy-files``, each file with its name, path, bytes and sha256 stated,
  written under a partial name and put in place only when they hold - the one way a backup file is
  copied between hosts;
* **what the source no longer has** is deleted by the restore, each file by a verified command
  (:mod:`dbabrain.backup_restore.verified_cleanup`), or the restore does not run.

What stays here is everything a staging needs *besides* its bytes, moved as it was:

* :func:`prepare_staging` - which files the restore needs (``include``'s prefixes, widened by a
  window; never a certificate, never a partial), the target folder made and proven writable, the
  marker that says it is this tool's staging (an earlier staging of the same source adopted), every
  folder recreated **including the empty ones** (a PostgreSQL base backup's ``pg_tblspc`` is empty
  and required), which files still have to travel (a same-size copy that is not older is skipped,
  unless ``replace`` names it), and room for them before the first one moves. It answers ``pending``
  - each file with the source's own time - and what the staging holds that the source does not.
* :func:`finish_staging` - each copied file given its source's time (the skip rule above reads it on
  the next run, and PostgreSQL dates a backup by its manifest's), and the folder opened for the
  engine that restores from it.
* the chain: which parts of a backup directory a restore needs, per engine.

The cost of the split is round trips: a PostgreSQL base backup is thousands of small files (a
362 MB set measured here was 3901), and one ``tar`` stream paid the latency once where a copy per
file pays it per file. That price buys every staged file being held to its bytes and sha256 as it
lands.
"""

from __future__ import annotations

from dbabrain.lib import errors
import errno
import posixpath
import shlex
import stat
from dataclasses import dataclass, field
from typing import Any


from dbabrain.lib import restore_space


class CopySpaceError(errors.Refused):
    """The files to copy do not fit on the target, or its free space could not be read."""


@dataclass
class TransferResult:
    skipped: int = 0
    # What the staging holds that the source no longer has - files, and folders - as relative paths.
    # Named, never removed (0.38.0, 1.329): until then this copy MIRRORED, deleting each over SFTP
    # with nothing held to anything. The restore deletes them by verified commands, or does not run.
    absent: list[str] = field(default_factory=list)
    absent_dirs: list[str] = field(default_factory=list)
    # What the space check measured before the first byte moves - ``None`` when there is nothing
    # to copy, or no rule was given.
    space: dict[str, Any] | None = None
    # Every file the request selected, copied or not, as ``(relative path, size)`` (0.34.0, 1.213b):
    # the caller's checksum gate hashes each one on both hosts, and it cannot hash what it is not told.
    files: list[tuple[str, int]] = field(default_factory=list)
    # The files that still have to travel, as ``(relative path, size, the source's mtime)`` - what
    # the caller hands to ``copy-files``, and the times ``finish-staging`` then sets.
    pending: list[tuple[str, int, int | None]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        answer = {"skipped": self.skipped,
                  "absent_at_source": list(self.absent),
                  "folders_absent_at_source": list(self.absent_dirs),
                  "files": [{"path": rel, "size_bytes": size} for rel, size in self.files],
                  "pending": [{"path": rel, "size_bytes": size, "mtime": mtime}
                              for rel, size, mtime in self.pending]}
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
    probe = posixpath.join(directory, ".dbabrain_write_probe")
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


#: The file a staging directory carries once this module owns it. The cleanup of a staging and the
#: delete of what its source no longer has act only where this file says the directory is theirs: a
#: `target_backup_dir` pointed at the target host's own backups, at a shared folder or at another
#: entry's staging used to be emptied without a word (review 0.25.0, B4.7).
#:
#: The name is the request's to state (``staging_marker``); this is what it is when the request
#: states none. A node that marked its staging folders before 0.29.0 states the name they carry -
#: ``upgrade-config`` wrote it into its ``restore_config.json`` - because the name cannot simply
#: change under it: a folder retention has emptied down to its marker holds nothing that is at the
#: source, and with the marker under a name this code no longer looks for, the next copy refused
#: it as somebody else's.
STAGING_MARKER = ".dbabrain-staging"


class StagingDirError(errors.Refused):
    """``target_dir`` holds files and is not marked as a dbabrain staging directory."""


def marker_name(value: Any) -> str:
    """The marker a request names, or :data:`STAGING_MARKER`. One hidden file name, never a path:
    it is joined to the staging folder and handed to ``find -name``."""
    name = str(value or "").strip()
    if not name:
        return STAGING_MARKER
    if not name.startswith(".") or name in {".", ".."} or "/" in name or "\\" in name \
            or any(character.isspace() for character in name):
        raise ValueError("staging_marker must be one file name that begins with a dot - not a path, "
                         f"and nothing a backup set could be called; got {value!r}.")
    return name


def _not_a_staging_dir(target_dir: str, marker: str = STAGING_MARKER) -> StagingDirError:
    path = posixpath.join(target_dir, marker)
    return StagingDirError(
        f"{target_dir} already holds files, none of which is at the source, and has no "
        f"{marker} marker, so it is not known to be a dbabrain staging directory - nothing "
        f"was copied or deleted there. If it is one (a "
        f"staging folder from before 0.25.0), mark it: `touch {path}`. Otherwise give the entry "
        f"its own, empty target_backup_dir.")


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

    The rule is the restore entry's ``space_check`` (:mod:`dbabrain.lib.restore_space`):
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


def _selected(files: list[tuple[str, int]], dirs: list[str], mtimes: dict[str, int], *,
              include: tuple[str, ...], window_since: float | None) -> tuple[list[tuple[str, int]], list[str]]:
    """The files and directories a request selects from a source listing: ``include``'s prefixes,
    widened by the window. One rule for the copy and for :func:`plan_backup_dir`, so the files a
    gate is told about are the files the copy moves.

    A ``<name>.dbabrain_partial`` is never selected: it is this tool's own name for a file still being
    written (``copy-files``, ``relay-file``), and one left by a copy that died is not a backup - on a
    repository it would otherwise be staged beside the real files (0.34.0)."""
    from dbabrain.common.ssh_relay import PARTIAL_SUFFIX

    from dbabrain.lib.integrity import is_certificate_file

    files = [(rel, size) for rel, size in files if not rel.endswith(PARTIAL_SUFFIX)]
    # A backup certificate or key is never copied, whole-directory copy or chain (0.37.0): it is
    # kept in dbabrain's vault and imported from there - see lib.integrity.is_certificate_file.
    files = [(rel, size) for rel, size in files if not is_certificate_file(rel)]
    dirs = [rel for rel in dirs if not _is_cert_dir(rel)]
    if not include:
        return files, dirs

    # The chain, and - for a window copy - every file written inside the window. A window
    # never narrows the chain: the restore always finds the backups it is going to apply.
    def kept(rel: str) -> bool:
        if rel.replace("\\", "/").startswith(include):
            return True
        return window_since is not None and mtimes.get(rel, 0) >= window_since

    files = [(rel, size) for rel, size in files if kept(rel)]
    parents = {posixpath.dirname(rel.replace("\\", "/")) for rel, _size in files}
    ancestors = {"/".join(p.split("/")[:n]) for p in parents for n in range(1, p.count("/") + 2) if p}
    dirs = [rel for rel in dirs
            if rel.replace("\\", "/").startswith(include) or rel.replace("\\", "/") in ancestors]
    return files, dirs


def _is_cert_dir(rel: str) -> bool:
    return "_cert" in [part.lower() for part in rel.replace("\\", "/").split("/") if part]


def plan_backup_dir(*, source_session, source_dir: str, include: tuple[str, ...] = (),
                    window_since: float | None = None) -> TransferResult:
    """What :func:`prepare_staging` would select, read from the source alone - no target is opened.

    The checksum gate (0.34.0, 1.213b) hashes every file before anything is copied; this is how it
    learns which files those are without the copy moving them first."""
    mtimes: dict[str, int] = {}
    files, dirs = _walk_remote(source_session.sftp(), source_dir, mtimes)
    files, _dirs = _selected(files, dirs, mtimes, include=include, window_since=window_since)
    return TransferResult(files=sorted((rel.replace("\\", "/"), size) for rel, size in files))


def prepare_staging(
    *,
    source_session,
    source_dir: str,
    target_session,
    target_dir: str,
    include: tuple[str, ...] = (),
    log: Any = None,
    space_check: restore_space.SpaceCheck | None = None,
    window_since: float | None = None,
    marker: str = STAGING_MARKER,
    replace: tuple[str, ...] = (),
) -> TransferResult:
    """Make ``target_dir`` ready for ``source_dir``'s files and answer which still have to travel.

    Nothing is copied and nothing is removed here (module docstring): the result's ``pending`` is
    what the caller hands to ``copy-files``.

    ``marker`` is the file that says the target folder is a staging directory of this tool's - the
    name the request states, since a node's existing folders carry the one they were marked with.

    ``include`` limits the copy to relative paths starting with one of the given prefixes, so a
    restore can pull only the parts of a backup directory it needs. ``window_since`` (epoch seconds)
    widens a narrowed copy by every file the source wrote at or after it - ``copy_selection:
    window``: the chain, and every other restore point of the window. ``space_check`` is the rule the
    files to copy are held to before the first one moves (:func:`check_room`); ``None`` asks nothing.
    ``replace`` names relative paths copied even when the target holds them at the same size and not
    older: a copy the caller's checksum gate found different, which size and time cannot see.
    """
    result = TransferResult()
    # The sessions own their SFTP channels and close them with themselves.
    source_sftp = source_session.sftp()
    target_sftp = target_session.sftp()
    source_mtimes: dict[str, int] = {}
    files, dirs = _walk_remote(source_sftp, source_dir, source_mtimes)
    at_source = {rel.replace("\\", "/") for rel, _size in files}
    dirs_at_source = {rel.replace("\\", "/").rstrip("/") for rel in dirs}
    files, dirs = _selected(files, dirs, source_mtimes, include=include, window_since=window_since)
    result.files = sorted((rel.replace("\\", "/"), size) for rel, size in files)
    forced = {rel.replace("\\", "/") for rel in replace}
    # Created before it is walked. The walk refuses a directory it cannot list - rightly, on
    # the source - and a target folder a first run has not made yet is exactly that, so every
    # new cross-machine restore failed its first run with "[Errno 2] No such file".
    _mkdirs(target_sftp, target_dir)
    _assert_writable(target_sftp, target_dir)
    target_mtimes: dict[str, int] = {}
    existing_files, existing_dirs = _walk_remote(target_sftp, target_dir, target_mtimes)
    existing = {rel: size for rel, size in existing_files if rel.replace("\\", "/") != marker}
    if len(existing) == len(existing_files):
        # No marker. An empty directory becomes ours. One that already holds files is adopted when
        # it is plainly an earlier staging copy of THIS source - at least one of its files is also
        # at the source, under the same relative path - so a staging folder from before the marker
        # needs no manual step (owner, 2026-10-01: a fix must not make the tool harder to use).
        # Anything else is refused: what the source does not have is named for deletion, and a
        # folder sharing nothing with the source is someone else's (B4.7).
        if existing and not any(rel.replace("\\", "/") in at_source for rel in existing):
            raise _not_a_staging_dir(target_dir, marker)
        if existing and log:
            log(f"{target_dir}: adopted as this source's staging directory (marked {marker})")
        with target_sftp.open(posixpath.join(target_dir, marker), "w") as handle:
            handle.write(b"dbabrain restore staging directory - filled and cleaned by dbabrain.\n")
    # What the source no longer has is NAMED here and removed by nobody in this command (0.38.0,
    # 1.329). It used to be a mirror - each such file removed over SFTP, each such folder by rmdir -
    # for two reasons that still hold and are now the caller's to keep: a source rebuilt under the
    # same name left its previous life's pieces beside the new ones, and a gvenzl Oracle lab has the
    # image's DBID and incarnation in every life, so RMAN could not tell them apart (RMAN-06054, the
    # point-in-time drill, 2026-09-25); and the husk of a removed PostgreSQL base backup
    # (`base/<stamp>_FULL`, empty) read as the newest full ("could not open file .../global/pg_control",
    # the 0.26 node, 2026-10-03). So the restore deletes each named file by a verified command, each
    # emptied folder by delete-empty-folder, and does not run while one is left. Judged against the
    # WHOLE source listing, not the `include`d part - an older point in time may need pieces a
    # newest-chain copy skips. Folders deepest first, the order they have to be removed in.
    result.absent = sorted(rel.replace("\\", "/") for rel in existing
                           if rel.replace("\\", "/") not in at_source)
    result.absent_dirs = sorted(
        (rel for rel in (d.replace("\\", "/").rstrip("/") for d in existing_dirs)
         if rel and rel not in dirs_at_source),
        key=lambda rel: (-rel.count("/"), rel))
    if (result.absent or result.absent_dirs) and log:
        log(f"{len(result.absent)} staged file(s) and {len(result.absent_dirs)} folder(s) the source no "
            "longer has - named, not removed")
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
        # a target crash-looping (2026-09-24). finish_staging gives each copied file its
        # source's time, so a repeat run still skips what it sent before.
        if (rel.replace("\\", "/") not in forced and existing.get(rel) == size
                and not _older(target_mtimes.get(rel), source_mtimes.get(rel))):
            result.skipped += 1
            continue
        pending.append((rel, size))
    # Nothing new: the common case for a repeated drill, and it costs two directory walks
    # rather than a transfer.
    if pending and space_check is not None:
        # Before the first byte: the files still to travel must fit the target's free space.
        result.space = check_room(target_session, target_dir, pending, space_check, log=log)
    result.pending = [(rel.replace("\\", "/"), size, source_mtimes.get(rel)) for rel, size in pending]
    return result


def finish_staging(target_session, target_dir: str, times: list[tuple[str, int | None]], *,
                   log: Any = None) -> dict[str, Any]:
    """Give each copied file its source's time. ``{"timed": n, "not_timed": [{"path", "reason"}]}``.

    The piece keeps the source's time, as the tar stream it used to travel in kept it. Dated by the
    copy, a PostgreSQL manifest copied after a newer one read as the newest backup - the restore
    plan dates a backup by it - and a point in time found no full before it; and the skip rule of
    the next run (:func:`prepare_staging`) would copy every file again as "older than its source".
    A file that is not there to be timed is an answer, not an exception: the caller's checksum gate
    is what says whether the copy stands.
    """
    sftp = target_session.sftp()
    timed = 0
    not_timed: list[dict[str, str]] = []
    for rel, mtime in times:
        if mtime is None:
            continue
        path = posixpath.join(target_dir, str(rel).replace("\\", "/"))
        try:
            sftp.utime(path, (int(mtime), int(mtime)))
            timed += 1
        except OSError as exc:
            not_timed.append({"path": path, "reason": str(exc) or type(exc).__name__})
    if log and (timed or not_timed):
        log(f"{timed} staged file(s) given their source's time" + (f", {len(not_timed)} not" if not_timed else ""))
    return {"timed": timed, "not_timed": not_timed}


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
    copy already follows (:mod:`dbabrain.lib.sqlserver_backup_chain`). Until 2026-10-03 a SQL Server
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
    """Each database's restore chain, as relative paths.

    Read from the layout the backup jobs write - ``<database>/FULL|DIFF|LOG/<file>``, the time in
    the name (or the file's mtime when the name carries none) - with the share copy's own rule, so
    both ways to a target copy the same backups. ``_cert/`` never travels (0.37.0): the certificate
    an encrypted backup needs is imported on the target from dbabrain's vault.

    A database with no FULL is copied whole, and a layout with no ``FULL`` / ``DIFF`` / ``LOG``
    folders at all is copied whole - what every SQL Server copy did before: a narrowed copy that
    guessed wrong fails the restore, a wide one only costs time.
    """
    from dbabrain.lib import sqlserver_backup_chain as chain_rule

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
    return tuple(sorted(str(item) for item in choice.selected)) + tuple(whole)


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
#
# No `%` anywhere in it: the text reaches sqlplus through `printf`, which reads `%` as a format, and
# a `LIKE '<dir>/%'` failed the query on the lab - the chain then quietly fell back to the newest
# level 0. INSTR says "starts with" without one.
_ORACLE_NEWEST_LOG_SQL = """set pagesize 0 feedback off heading off
SELECT MAX(r.next_change#)
FROM v$backup_redolog r
JOIN v$backup_piece p ON p.set_stamp = r.set_stamp AND p.set_count = r.set_count
WHERE p.status = 'A' AND INSTR(p.handle, '{directory}/') = 1;
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
    :func:`prepare_staging` matches it exactly.

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
        point = f"recoverable to SCN {until_scn}, the newest log backup" if until_scn else "the newest"
        log(f"transfer narrowed to the RMAN chain: {len(names)} piece(s) from the level 0 {point}")
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
    from dbabrain.lib.shell import docker_cli

    # Plain docker first, sudo only as the fallback (dbabrain.lib.shell.docker_cli): `sudo` alone fails
    # where sudo wants a password, and the SSH user is usually in the docker group.
    inner = f"{docker_cli(True)} exec -i {shlex.quote(container)} bash -lc {shlex.quote(command)}"
    _stdin, stdout, _stderr = source_session.open_stream(inner)
    return stdout.read().decode("utf-8", "replace")
