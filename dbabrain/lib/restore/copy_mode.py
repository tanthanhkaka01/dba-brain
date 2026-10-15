"""How a backup directory crosses from one host to another - and that the answer says which way.

The copy between two hosts has a fast way and a slow one: one ``tar`` stream, or one SFTP transfer
per file. It tried the stream and, when tar could not be used on either end, went file by file -
correct, and across two internet hops the difference between minutes and hours (10 KB/s measured).
Which of the two had run was a line on stderr.

The owner's rule (2026-10-01, review notes G4): a fallback in *how* something is done is kept, but
it is **reported in the answer, always**, and it **can be pinned** - and a pinned way that cannot be
used is an error, never a reason to try the other. The words are here because both sides use them:
the restore entry states ``copy_mode`` and ``common.cli copy-backup-dir`` takes it.
"""

from __future__ import annotations

#: Try the tar stream; go file by file when tar cannot be used, and say so.
COPY_AUTO = "auto"
#: One tar stream, or an error.
COPY_TAR = "tar"
#: One SFTP transfer per file, without trying tar.
COPY_SFTP = "sftp"

COPY_MODES = (COPY_AUTO, COPY_TAR, COPY_SFTP)

#: What an answer reports when there was nothing to copy.
COPY_NONE = "none"


def parse_copy_mode(value: object) -> str:
    """``auto`` when absent; anything outside :data:`COPY_MODES` is refused, not read as ``auto`` -
    a misspelled pin is a copy that steps down while its entry says it may not."""
    chosen = str(value or COPY_AUTO).strip().lower()
    if chosen not in COPY_MODES:
        raise ValueError(f"copy_mode must be one of {', '.join(COPY_MODES)}, got: {value!r}")
    return chosen


__all__ = ["COPY_AUTO", "COPY_MODES", "COPY_NONE", "COPY_SFTP", "COPY_TAR", "parse_copy_mode"]
