"""An exclusive lock on ``<file>.lock``, held for one read-modify-write of a file.

Written for the secret stores (review 0.25.0, B9.1): a console password change and a
`rotate-password` at the same moment kept one of the two changes. The Telegram routing files have
the same two-writer shape - the workflow every second, `group-add` / `group-level` / `user-level`
from an operator - and an operator's change landing between the workflow's read and write was lost
(F8.2). One lock, both callers.
"""

from __future__ import annotations

from dbabrain.lib import errors
import os
from pathlib import Path
from typing import Any

#: How long a writer waits for the lock on Windows before giving up. A write takes
#: well under a second; two minutes is a holder that is not coming back.
_LOCK_WAIT_SECONDS = 120


class LockHeld(errors.Refused):
    """The lock is held by another open handle and ``wait=False`` was requested."""


class FileLock:
    """An exclusive lock on ``<path>.lock`` for one read-modify-write of ``path``."""

    def __init__(self, path: Path | str, wait: bool = True) -> None:
        self.path = Path(str(path) + ".lock")
        self.wait = bool(wait)
        self.handle: Any = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, "a+b")  # noqa: SIM115 - held for the lock's lifetime
        if os.name == "nt":
            import msvcrt
            import time

            self.handle.seek(0)
            if not self.wait:
                # Both msvcrt.locking on Windows and fcntl.flock on POSIX lock per open handle,
                # so two handles in one process exclude each other too; the collector relies on it.
                try:
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    self.handle.close()
                    raise LockHeld(f"{self.path}: lock is held by another handle") from exc
                return self

            # Not LK_LOCK: it tries ten times a second apart and then raises EDEADLOCK ("Resource
            # deadlock avoided"), so on a Windows node the third of three writers arriving together
            # failed instead of waiting - found running the review's own concurrency test on the
            # master (0.26.0). Wait as flock does, with a ceiling so a writer never hangs on a
            # holder that died without unlocking.
            deadline = time.monotonic() + _LOCK_WAIT_SECONDS
            while True:
                try:
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        self.handle.close()
                        raise TimeoutError(
                            f"{self.path}: another writer held the lock for "
                            f"{_LOCK_WAIT_SECONDS}s") from None
                    time.sleep(0.05)
        else:
            import fcntl

            if not self.wait:
                # Both msvcrt.locking on Windows and fcntl.flock on POSIX lock per open handle,
                # so two handles in one process exclude each other too; the collector relies on it.
                try:
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except (BlockingIOError, OSError) as exc:
                    self.handle.close()
                    raise LockHeld(f"{self.path}: lock is held by another handle") from exc
                return self

            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc: Any) -> None:
        try:
            if self.handle is not None and not self.handle.closed:
                if os.name == "nt":
                    import msvcrt

                    self.handle.seek(0)
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            if self.handle is not None and not self.handle.closed:
                self.handle.close()


__all__ = ["FileLock", "LockHeld"]
