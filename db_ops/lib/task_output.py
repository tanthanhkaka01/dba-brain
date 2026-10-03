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

from db_ops.lib import errors
from typing import Any


class TaskOutputError(errors.ConfigError):
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

#: The keys of an ``output`` block, in the order ``shared_config_objects.json`` describes them.
OUTPUT_FIELDS = ("format", "telegram_chat", "chat_id", "max_rows")

#: Ceiling on ``output.max_rows``. Rows go out as ~30 per Telegram message and a group is
#: rate-limited to about 20 messages a minute, so one config edit must not be able to flood it.
MAX_INLINE_MAX_ROWS = 5000

__all__ = [
    "FILE_OUTPUT_FORMATS", "MAX_INLINE_MAX_ROWS", "OUTPUT_FIELDS", "OUTPUT_FORMATS",
    "TaskOutputError", "merge_result_sets", "normalize_output", "parse_output",
]


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


def parse_output(raw: Any) -> dict[str, Any]:
    """An ``output`` block as ``{format, telegram_chat, chat_id, max_rows}``.

    The one parser for the block, so ``sql_tasks`` reading a target and ``check-objects`` reading
    the file hold it to the same rule. ``max_rows`` is ``0`` when unset (the caller's default
    applies). A value out of range is refused rather than clamped: a quietly reduced number looks
    like it worked until somebody counts the rows.
    """
    if not isinstance(raw, dict):
        raise TaskOutputError("'output' is required and must be an object.")
    fmt = str(raw.get("format") or "").strip().lower() or "none"
    if fmt not in OUTPUT_FORMATS:
        raise TaskOutputError(
            f"output.format must be one of {OUTPUT_FORMATS}, got {raw.get('format')!r}.")
    max_rows_text = str(raw.get("max_rows") if raw.get("max_rows") is not None else "").strip()
    if max_rows_text and (not max_rows_text.isdigit()
                          or not 0 < int(max_rows_text) <= MAX_INLINE_MAX_ROWS):
        raise TaskOutputError(
            f"output.max_rows must be a whole number between 1 and {MAX_INLINE_MAX_ROWS}, got "
            f"{raw.get('max_rows')!r}. Rows go out as ~30-per-Telegram-message and a group is "
            f"rate-limited to about 20 messages a minute; export a file for more than this.")
    return {
        "format": fmt,
        "telegram_chat": str(raw.get("telegram_chat") or "").strip(),
        "chat_id": str(raw.get("chat_id") or "").strip(),
        "max_rows": int(max_rows_text) if max_rows_text else 0,
    }


def merge_result_sets(result_sets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Consecutive result sets with the same columns, joined into one set.

    A task that runs its SQL once per batch returns one small result set per batch - 69 one-row
    sets for one run of a ten-day load. Rendered one by one they are 69 tables of one row each;
    a file export that takes only the first set drops the other 68. Joined, they are
    one table. Sets without columns (a statement that returns no rows) are skipped, and a change
    of columns starts a new set, so different shapes are never mixed.
    """
    merged: list[dict[str, Any]] = []
    for rset in result_sets:
        columns = list(rset.get("columns") or [])
        if not columns:
            continue
        rows = [list(row) for row in (rset.get("rows") or [])]
        if merged and merged[-1]["columns"] == columns:
            merged[-1]["rows"].extend(rows)
        else:
            merged.append({"columns": columns, "rows": rows})
    return merged
