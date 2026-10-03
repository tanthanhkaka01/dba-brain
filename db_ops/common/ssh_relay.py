"""Stream one file from one open SSH session to another, hash-verified, without staging it here.

Two callers and one stream: ``common.cli relay-file`` (two hosts named as targets, resolved by
``file_transfer``) and ``move-db-docker`` (two hosts it already holds open, from the logins in its
request). The mover used to launch ``relay-file`` as a subprocess to get this - which also made
the subprocess re-resolve both hosts out of ``db_instances.json`` - and ``common`` does not launch
a CLI (0.23.0). So the stream is here, taking two sessions and nothing else, and both use it.

The rules are ``relay-file``'s, and each one is a failure already had:

* the source is hashed **before** the stream, so a file still being written cannot hash one set
  of bytes and send another, with the mismatch blamed on the network;
* the bytes land under ``<name>.dbops_partial`` and are renamed onto the real name only after
  the destination's sha256 matches - a corrupt partial is deleted, never left to be mistaken for
  a resumable transfer;
* both stderr streams are drained on their own threads for the whole copy, or a command that
  complains on every chunk fills its window and the pipeline seizes with no error anywhere.
"""

from __future__ import annotations

from db_ops.lib import errors
import shlex
from pathlib import PurePosixPath
from typing import Any

#: Written beside the destination while the bytes are in flight, then renamed onto it.
PARTIAL_SUFFIX = ".dbops_partial"

_RELAY_CHUNK = 1 << 18


class RelayError(errors.OperationFailed):
    """The stream could not be completed, or what arrived is not what was sent."""


def remote_size(session, path: str) -> int | None:
    try:
        return int(session.sftp().stat(str(PurePosixPath(path))).st_size)
    except FileNotFoundError:
        return None
    except OSError:
        return None


#: The two ways a staged file takes its real name, as an answer reports them (``replace_mode``).
REPLACE_ATOMIC = "posix-rename"
REPLACE_IN_TWO_STEPS = "remove+rename"


def atomic_replace(session, staged: str, destination: str) -> str:
    """Move ``staged`` onto ``destination`` in one step where the server supports it.

    Same rule and same fallback as ``common.backup_copy``: prefer the OpenSSH
    ``posix-rename`` extension, which overwrites in a single syscall, and only fall back to
    remove-then-rename when the server lacks it.

    Returns which of the two it was. The second is not atomic - between its two steps the
    destination does not exist - and until the owner's rule that a fallback in *how* is always
    reported (review notes G4) nothing said a file had been replaced that way.
    """
    sftp = session.sftp()
    try:
        sftp.posix_rename(staged, destination)
        return REPLACE_ATOMIC
    except (IOError, OSError, AttributeError):
        pass
    try:
        sftp.remove(destination)
    except (IOError, OSError):
        pass
    sftp.rename(staged, destination)
    return REPLACE_IN_TWO_STEPS


class _Drained:
    """Read a channel's stderr on its own thread for the whole transfer.

    Nothing reads these streams until after ``recv_exit_status()``, which cannot be reached
    while the copy is still running — so a command that complains on every chunk fills its
    stderr window, blocks on the write, stops reading stdin, and the pipeline seizes with no
    error anywhere. ``common.backup_copy`` learned that by sitting at RUNNING for two
    hours; the lesson applies to one file exactly as it did to four thousand.
    """

    def __init__(self, handle) -> None:
        import threading

        self._chunks: list[str] = []
        self._thread = threading.Thread(target=self._pump, args=(handle,), daemon=True)
        self._thread.start()

    def _pump(self, handle) -> None:
        try:
            for line in handle:
                self._chunks.append(
                    line if isinstance(line, str) else line.decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001 - diagnostics must never raise over the real error
            pass

    def text(self) -> str:
        self._thread.join(timeout=5)
        return "".join(self._chunks)


def _sha256(session, path: str, *, name: str) -> str:
    result = session.run(f"sha256sum {shlex.quote(path)}")
    if not result.ok:
        raise RelayError(
            f"{name}: sha256sum failed (exit {result.exit_code}): "
            f"{(result.stderr or result.stdout or '').strip()[:400]}")
    return (result.stdout or "").strip().split()[0]


def relay(source, destination, source_path: str, dest_path: str, *, overwrite: bool = False,
          make_dirs: bool = True, source_name: str = "source",
          dest_name: str = "destination", source_user: str = "") -> dict[str, Any]:
    """Copy ``source_path`` on ``source`` to ``dest_path`` on ``destination``.

    Both are open SSH sessions (:class:`db_ops.common.remote_exec.SshSession`); Linux on both
    ends, because the stream is ``cat`` and ``sha256sum``. ``*_name`` is what an error calls each
    end. Returns ``size_bytes``, ``sha256``, ``verified`` and ``replace_mode`` - how the staged
    file took its name (:func:`atomic_replace`).
    """
    size = remote_size(source, source_path)
    if size is None:
        raise RelayError(
            f"{source_name}: {source_path} does not exist or is not readable as "
            f"{source_user or 'the configured user'}.")
    if not overwrite and remote_size(destination, dest_path) is not None:
        raise RelayError(
            f"{dest_name}: {dest_path} already exists; pass "
            '"overwrite": true to replace it.')

    # Hash the source BEFORE the stream, not after: a file still being written by whatever
    # produced it would otherwise hash one set of bytes and send another, and the mismatch
    # would be blamed on the network.
    source_sha = _sha256(source, source_path, name=source_name)

    staged = f"{dest_path}{PARTIAL_SUFFIX}"
    directory = str(PurePosixPath(dest_path).parent)
    prefix = f"mkdir -p {shlex.quote(directory)} && " if make_dirs else ""

    src_in, src_out, src_err = source.open_stream(
        f"cat {shlex.quote(source_path)}")
    dst_in, dst_out, dst_err = destination.open_stream(
        f"{prefix}cat > {shlex.quote(staged)}")
    src_errors, dst_errors = _Drained(src_err), _Drained(dst_err)
    moved = 0
    try:
        src_in.close()
        while True:
            chunk = src_out.read(_RELAY_CHUNK)
            if not chunk:
                break
            dst_in.write(chunk)
            moved += len(chunk)
        dst_in.flush()
        dst_in.channel.shutdown_write()
        src_rc = src_out.channel.recv_exit_status()
        dst_rc = dst_out.channel.recv_exit_status()
    finally:
        for handle in (src_in, dst_in):
            try:
                handle.close()
            except Exception:  # noqa: BLE001
                pass
    if src_rc != 0 or dst_rc != 0:
        detail = (src_errors.text() + dst_errors.text()).strip()
        raise RelayError(
            f"relay {source_path} -> {dest_path} failed (source exit {src_rc}, destination "
            f"exit {dst_rc}): {detail[:400]}")

    dest_sha = _sha256(destination, staged, name=dest_name)
    if dest_sha != source_sha:
        # Leave nothing behind under either name: a corrupt .partial that a later run
        # mistakes for a resumable transfer is the failure this check exists to prevent.
        destination.run(f"rm -f {shlex.quote(staged)}")
        raise RelayError(
            f"relay {source_path} -> {dest_path}: sha256 mismatch (source {source_sha}, "
            f"destination {dest_sha}); the copy was discarded.")
    replaced_by = atomic_replace(destination, staged, dest_path)
    return {"size_bytes": int(moved), "sha256": source_sha, "verified": True,
            "replace_mode": replaced_by}
