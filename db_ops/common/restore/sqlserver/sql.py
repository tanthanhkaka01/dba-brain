"""The RESTORE statements for a chosen chain. Text in, text out - nothing is executed here.

Separate from :mod:`.chain` because the two fail differently and are worth reviewing apart: a
chain bug restores the wrong data, a statement bug fails loudly at the instance. Keeping the
statement builder pure also means the exact SQL a recovery will run can be printed and read by a
human before anything happens, which is the only review that matters at 3am.

Two rules are enforced by construction rather than left to the caller:

* every restore before the last is ``WITH NORECOVERY``, and only the last is ``WITH RECOVERY`` -
  a chain that recovers early cannot have the remaining logs applied at all, and the only fix is
  to start the whole restore again;
* ``STOPAT`` goes on the final log and nowhere else - SQL Server accepts it on a full or a
  differential and silently ignores it there, which reads as "the point in time was honoured".
"""

from __future__ import annotations
from db_ops.common.restorestep.sqlserver import _quote_literal  # noqa: F401 - one definition, see that module

from datetime import datetime

from db_ops.common.restore.sqlserver.chain import LOG, RestoreChain


def _quote_name(name: str) -> str:
    """Bracket-quote an identifier, doubling any closing bracket inside it."""
    return "[" + str(name).replace("]", "]]") + "]"




def build_restore_statements(
    chain: RestoreChain,
    *,
    database: str,
    move: dict[str, str] | None = None,
    replace: bool = True,
    checksum: bool = True,
    stats: int = 10,
) -> list[str]:
    """The ordered statements that restore ``chain`` into ``database``.

    ``move`` maps a logical file name to its path on the target, for restoring onto a machine
    whose data directory differs from the source's - which is every cross-machine restore.
    """
    target = _quote_name(database)
    backups = chain.ordered
    if not backups:
        return []

    move_clause = "".join(
        f", MOVE {_quote_literal(logical)} TO {_quote_literal(path)}"
        for logical, path in sorted((move or {}).items())
    )

    statements: list[str] = []
    for index, header in enumerate(backups, start=1):
        is_last = index == len(backups)
        options = ["NORECOVERY"] if not is_last else ["RECOVERY"]
        if checksum:
            options.append("CHECKSUM")
        if stats:
            options.append(f"STATS = {int(stats)}")
        # REPLACE only on the full: it means "overwrite the existing database", which is a
        # statement about starting the chain, not about continuing it.
        if replace and index == 1:
            options.append("REPLACE")
        # Only the final log carries STOPAT. On a full or a differential SQL Server accepts it
        # and ignores it, which would read as a point-in-time restore that never happened.
        if is_last and chain.stopat and header.backup_type == LOG:
            options.append(f"STOPAT = {_quote_literal(_format_stopat(chain.stopat))}")

        verb = "RESTORE LOG" if header.backup_type == LOG else "RESTORE DATABASE"
        statements.append(
            f"{verb} {target} FROM DISK = {_quote_literal(header.path)}\n"
            f"    WITH {', '.join(options)}"
            + (move_clause if index == 1 else "")
            + ";"
        )
    return statements


def _format_stopat(moment: datetime) -> str:
    """ISO-ish, second precision, no timezone suffix.

    SQL Server reads ``STOPAT`` in the *server's* local time and rejects an offset outright, so the
    caller converts to that timezone before getting here; carrying an offset this far would make a
    rejected statement look like a formatting bug rather than the timezone decision it is.
    """
    return moment.strftime("%Y-%m-%dT%H:%M:%S")
