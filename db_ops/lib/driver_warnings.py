"""A driver *warning* is not a failure, and a reader must not treat it as one.

SQLSTATE class ``01`` is a warning: ``01003`` is SQL Server's message 8153, *"Null value is
eliminated by an aggregate"*, sent whenever ``SUM``/``MAX``/``COUNT(col)`` skips a NULL, and the
statement that raised it completed. pyodbc nevertheless raises it out of ``cursor.nextset()``
when ``SQLMoreResults`` returns an error whose only diagnostic record is that warning - and every
result loop here called ``nextset()`` bare, so a benign warning ended the run exactly as an error
would. ``SQL033-NIGHTLY-ENGINE`` was recorded ``error`` on 2026-09-24 after its 59-day loop had
finished and committed.

What pyodbc does at that moment decides what a reader may conclude, and it was measured on a lab
SQL Server (2026-09-24) rather than assumed: it **closes the statement**, the driver **drains the
rest of the batch** - every later statement still runs on the server - and the next ``nextset()``
answers ``False``. So nothing after that point reaches the client: not its result sets, and not an
error raised after it either (a second ``RAISERROR`` in the drained part was never seen). A reader
that stops at a warning therefore knows the batch ran to its end on the server and does *not* know
that the rest of it succeeded. :func:`read_next_set` records the warning with exactly that caveat;
what to do with an open transaction is the caller's decision, because only the caller knows
whether it holds one.

Pure: it reads an exception and a cursor and imports nothing from ``db_ops``.
"""

from __future__ import annotations

import re
from typing import Any

#: A SQLSTATE as ODBC drivers print it inside a message: ``[01003]``, ``[42000]``, ``[08S01]``.
#: The driver and server tags beside it (``[Microsoft]``, ``[SQL Server]``) never match: they are
#: not five upper-case letters or digits.
_SQLSTATE_IN_MESSAGE = re.compile(r"\[([0-9A-Z]{5})\]")

#: Appended to every warning :func:`read_next_set` records, because the warning alone would read as
#: "one harmless message" when it also marks the point after which the run saw nothing.
AFTER_WARNING = ("the driver closed the statement at this warning: the rest of the batch still ran on "
                 "the server, but its result sets - and any error raised after this point - could not "
                 "be read")


def sqlstates(exc: BaseException) -> list[str]:
    """Every SQLSTATE the exception carries, the first one first.

    pyodbc raises ``(sqlstate, message)`` and joins *all* the diagnostic records into the message,
    each with its own ``[SQLSTATE]``. The first argument alone names only the first record, and a
    warning followed by a real error would pass as a warning if that were all that was read.
    """
    args = getattr(exc, "args", ()) or ()
    first = str(args[0]) if args and isinstance(args[0], str) else ""
    message = str(args[1]) if len(args) > 1 else ""
    found = _SQLSTATE_IN_MESSAGE.findall(message)
    if first and (not found or found[0] != first):
        found.insert(0, first)
    return found


def warning_text(exc: BaseException) -> str | None:
    """The exception's text when **every** SQLSTATE it carries is class ``01``, else ``None``.

    ``None`` for anything that is not shaped like a driver error at all - an exception with no
    SQLSTATE is not known to be a warning, so it is not treated as one.
    """
    states = sqlstates(exc)
    if not states or not all(state.startswith("01") for state in states):
        return None
    args = getattr(exc, "args", ()) or ()
    return str(args[1] if len(args) > 1 else args[0]).strip()


def read_next_set(cursor: Any, warnings: list[str]) -> bool:
    """``cursor.nextset()``, except that a warning-only error is recorded in ``warnings`` and ends
    the reading instead of propagating.

    Returns ``True`` when there is another result to read. A warning returns ``False``: pyodbc has
    already closed the statement, so there is nothing left that *could* be read (see the module
    docstring). Any error that is not purely a warning propagates unchanged.
    """
    nextset = getattr(cursor, "nextset", None)
    if not callable(nextset):
        return False
    try:
        return bool(nextset())
    except Exception as exc:
        text = warning_text(exc)
        if text is None:
            raise
        warnings.append(f"{text} - {AFTER_WARNING}")
        return False
