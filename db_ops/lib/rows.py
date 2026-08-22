"""Reading one column out of a store row that may not have it.

A stored row is read on whichever backend is live, by code that may be older or newer than the
row. Three things make an index raise where a caller only wanted a value:

* the column was added by a migration this store has not run;
* the row was written before the column existed;
* the caller was handed a trimmed row, or a tuple where it expected a mapping.

None of the three is worth losing the work over, and losing the work is what indexing does. A
queued Telegram row missing ``message_type`` would fail the *send*, turning a cosmetic gap into an
undelivered alert; a stored metric sample missing a column would raise mid-evaluation and lose
every policy in an SLA run, which is far worse than one imprecise coverage figure.

So both readers here answer with a default instead. They were three near-copies until 2026-08-16 —
``reports.metrics_reports._field``, ``sla.compliance._row_field`` and
``telegram.send_queue.row_value`` — agreeing on the intent and disagreeing on the details: two
returned ``""`` and one ``None``, and one of them did not catch ``TypeError``, so a tuple row
raised there and not in the other two. That is the shape this rule exists to prevent: the same
decision, made three times, differently.
"""

from __future__ import annotations

from typing import Any

__all__ = ["row_text", "row_value"]


def row_value(row: Any, column: str, default: Any = None) -> Any:
    """``row[column]``, or ``default`` when the row does not carry it."""
    try:
        return row[column]
    except (KeyError, IndexError, TypeError):
        return default


def row_text(row: Any, column: str, default: str = "") -> str:
    """The same as text, with ``NULL`` reading as ``default`` rather than as ``"None"``."""
    value = row_value(row, column)
    return default if value is None else str(value or default)
