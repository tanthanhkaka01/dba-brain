"""What a scheduled SQL task does with its result set — the vocabulary, in one place.

Three components need the same words and none of them may learn them from another: ``common``
writes them into ``sql_targets.json`` when ``add-sql`` registers a task, ``sql_tasks`` reads them
back to decide whether to write a file or paste rows into a message, and ``telegram`` offers the
file subset as the ``format`` argument of ``/spbot_sql_export``. An app does not import ``common``,
so the vocabulary cannot live there; it is a value rather than an operation, so it lives here.

The distinction between the two tuples is the one that keeps being got wrong: ``none`` and
``plain`` are *deliveries*, not renderings — nothing is written and there is no document to send.
Everything else produces a file, which is why the subset is named rather than re-listed at each
of the four places that ask "does this task attach something?".
"""

from __future__ import annotations

from typing import Any


class TaskOutputError(ValueError):
    """An output word that is not one of :data:`OUTPUT_FORMATS`."""


#: Everything a task's ``output`` may say. ``none`` = status only, ``plain`` = rows in the message
#: body, the rest = a document.
#:
#: ``json`` was missing until 2026-08-27, and its absence had a cost. The renderer
#: (``db_ops.lib.result_format.RESULT_FORMATS``) has always produced it, so ``run-sql --format
#: json`` worked while a *scheduled* task could not ask for the same artifact — and the way round
#: that was a bespoke Telegram command with ``--sql-id 14`` written into its argv, which then
#: shipped to every install as a command that ran one estate's task. Two vocabularies for one
#: question is how that happens.
OUTPUT_FORMATS = ("none", "plain", "xlsx", "csv", "txt", "xml", "json")

#: The subset of :data:`OUTPUT_FORMATS` that produces a file. Named once because four separate
#: decisions read it — whether to build a document, whether to paste rows instead, whether a chat
#: is required, and which formats ``/spbot_sql_export`` will accept.
FILE_OUTPUT_FORMATS = ("xlsx", "csv", "txt", "xml", "json")

__all__ = ["FILE_OUTPUT_FORMATS", "OUTPUT_FORMATS", "TaskOutputError", "normalize_output"]


def normalize_output(raw: Any) -> str:
    """``none`` / ``plain`` / ``xlsx`` / …; empty, JSON null and ``-`` all mean ``none``.

    The three spellings of "nothing" are accepted because they arrive from three places: an
    omitted config key, a JSON ``null``, and the ``-`` an operator types at a Telegram prompt that
    cannot take an empty message.
    """
    text = str(raw or "").strip().lower()
    if text in {"", "null", "-"}:
        return "none"
    if text not in OUTPUT_FORMATS:
        raise TaskOutputError(f"output must be one of {OUTPUT_FORMATS}, got {raw!r}.")
    return text
