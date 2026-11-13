"""How a backup's files cross from one host to another - and that the answer says which way.

Until 0.38.0 the copy between two hosts had a fast way and a slow one: one ``tar`` stream of the
whole directory, or one SFTP transfer per file. It tried the stream and, when tar could not be used
on either end, went file by file - correct, and across two internet hops the difference between
minutes and hours (10 KB/s measured). Which of the two had run was a line on stderr.

The owner's rule (2026-10-01, review notes G4): a fallback in *how* something is done is kept, but
it is **reported in the answer, always**, and it **can be pinned** - and a pinned way that cannot be
used is an error, never a reason to try the other.

Since 0.38.0 (1.329) the files travel by ``common.cli copy-files``, each held to its bytes and its
sha256, and the entry's ``copy_mode`` names that command's ``transfer``: ``cat`` (one cat channel
per file, the faster on this estate's links) or ``sftp``; ``auto`` is ``cat``. Nothing steps down
any more: a way that fails is tried again as itself, by the checksum gate's rounds, never as the
other. The tar stream is gone and so is its word - an entry still pinning ``tar`` is **refused by
name**, because by the rule above a pinned way that cannot be used is never served by another.
"""

from __future__ import annotations

#: The cat stream - the one way since nothing steps down; the word an entry without the field has.
COPY_AUTO = "auto"
#: One cat channel per file, pinned.
COPY_CAT = "cat"
#: One SFTP transfer per file, pinned.
COPY_SFTP = "sftp"

COPY_MODES = (COPY_AUTO, COPY_CAT, COPY_SFTP)

#: ``copy-files``' two transfers - how bytes travel over SSH, stated in every request and never
#: assumed (0.37.0, the operator 2026-11-10: *standardise SFTP and cat in the common cli, set in the
#: shared config*). ``cat`` - a plain ``cat`` channel with a large window, the source read ahead
#: (measured: 43 MB/s writing into the repository's NFS folder); ``sftp`` - paramiko's SFTP, read
#: ahead and pipelined (25 MB/s there), for a host whose login allows SFTP and no command. The one
#: list: ``common.filecopy`` and the offload's ``offload_transfer`` both read it here.
TRANSFERS = (COPY_CAT, COPY_SFTP)

#: What an answer reports when there was nothing to copy.
COPY_NONE = "none"

#: The word of the one tar stream (0.26.0 - 0.37): refused by name since the stream went.
COPY_TAR_GONE = "tar"


def parse_copy_mode(value: object) -> str:
    """``auto`` when absent; anything outside :data:`COPY_MODES` is refused, not read as ``auto`` -
    a misspelled pin is a copy that goes another way than its entry says."""
    chosen = str(value or COPY_AUTO).strip().lower()
    if chosen == COPY_TAR_GONE:
        raise ValueError(
            'copy_mode "tar" went with the tar stream in 0.38.0 - the files travel one by one, each held to '
            f'its sha256. State one of {", ".join(COPY_MODES)} ("cat" is one stream per file).')
    if chosen not in COPY_MODES:
        raise ValueError(f"copy_mode must be one of {', '.join(COPY_MODES)}, got: {value!r}")
    return chosen


def transfer_of(copy_mode: object) -> str:
    """``copy-files``' ``transfer`` for an entry's ``copy_mode``: ``sftp`` when pinned, else ``cat``."""
    return COPY_SFTP if parse_copy_mode(copy_mode) == COPY_SFTP else COPY_CAT


__all__ = ["COPY_AUTO", "COPY_CAT", "COPY_MODES", "COPY_NONE", "COPY_SFTP", "COPY_TAR_GONE", "TRANSFERS",
           "parse_copy_mode", "transfer_of"]
