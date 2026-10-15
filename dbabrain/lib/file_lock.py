"""An exclusive lock on ``<file>.lock``, held for one read-modify-write of a file.

Written for the secret stores (review 0.25.0, B9.1): a console password change and a
`rotate-password` at the same moment kept one of the two changes. The Telegram routing files have
the same two-writer shape - the workflow every second, `group-add` / `group-level` / `user-level`
from an operator - and an operator's change landing between the workflow's read and write was lost
(F8.2). One lock, both callers.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

#: How long a writer waits for the lock on Windows before giving up. A write takes
#: well under a second; two minutes is a holder that is not coming back.
_LOCK_WAIT_SECONDS = 120


class FileLock:
    """An exclusive lock on ``<path>.lock`` for one read-modify-write of ``path``."""

    def __init__(self, path: Path) -> None:
        self.path = Path(str(path) + ".lock")
        self.handle: Any = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, "a+b")  # noqa: SIM115 - held for the lock's lifetime
        if os.name == "nt":
            import msvcrt
            import time

            # Not LK_LOCK: it tries ten times a second apart and then raises EDEADLOCK ("Resource
            # deadlock avoided"), so on a Windows node the third of three writers arriving together
            # failed instead of waiting - found running the review's own concurrency test on the
            # master (0.26.0). Wait as flock does, with a ceiling so a writer never hangs on a
            # holder that died without unlocking.
            self.handle.seek(0)
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

            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc: Any) -> None:
        try:
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()


__all__ = ["FileLock"]
