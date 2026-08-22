"""The one way an app queues a Telegram message.

Every app needs to say the same thing — *put this text in front of an operator* — and until
now each one called :meth:`DbOpsStore.insert_telegram_send_message` directly with its own
conventions. That was survivable while a row was just text, but it stopped being survivable
once the row had to declare **what kind of message it is**: seven producers inventing seven
vocabularies is how a "critical" from one app renders differently from a "critical" in
another, and how a new producer silently ships with no type at all.

So the insert is owned here, once, the same way ``common.notify`` owns the notify block and
``common.time_window`` owns scheduling. Apps call :func:`queue_telegram_message`; the store
method stays public for the store's own use and for tests, but application code should not
reach for it.

What this layer adds over a raw insert:

* **A validated ``message_type``** — one of :data:`db_ops.lib.telegram_severity.MESSAGE_TYPES`.
  Unknown values are dropped to ``None`` rather than stored, so a typo degrades to "the send
  layer guesses from the header" instead of writing a value nothing understands.
* **Derivation from what a producer already has.** Almost nobody holds a display type; they
  hold a *level* (`logging`/`warning`/`error`/`critical`) and often a *phase* (`START`/`END`/
  `ERROR`) or a status. :func:`message_type_for` turns those into the display type, so the
  mapping lives in one place instead of being re-decided per app.

The emoji itself is still applied at send time (:mod:`db_ops.lib.telegram_severity`, via the Telegram send path) — the stored
type is data, not presentation, so changing the emoji set never means rewriting stored rows.
"""

from __future__ import annotations

from typing import Any

from db_ops.lib.telegram_severity import MESSAGE_TYPES, PLAIN, normalize_message_type

__all__ = [
    "MESSAGE_TYPES",
    "PLAIN",
    "message_type_for",
    "queue_telegram_message",
]


# Where in a run's lifecycle the event sits, or what a run concluded. Every spelling here is
# one a producer in this repo actually emits: backup_restore uses START/END/ERROR, sql_tasks
# uses running/done/error, and the SLA app concludes OK/PASSED/AT_RISK/FAILED/NO_DATA. Adding a
# producer means adding its vocabulary here, not inventing a private mapping in that app.
_PHASE_TYPES = {
    "START": "started",
    "STARTED": "started",
    "BEGIN": "started",
    "RUNNING": "running",
    "PROGRESS": "running",
    "END": "success",
    "DONE": "success",
    "FINISHED": "success",
    "SUCCESS": "success",
    "OK": "success",
    "PASS": "success",
    "PASSED": "success",
    "ERROR": "failed",
    "FAIL": "failed",
    "FAILED": "failed",
    # A command rejected for bad input still failed from the operator's side — they asked for
    # something and did not get it. This one reached production as NULL.
    "VALIDATION_ERROR": "failed",
    # A run killed by its own timeout did not succeed, whatever it managed before the clock ran out.
    "TIMEOUT": "failed",
    "TIMED_OUT": "failed",
    "AT_RISK": "warning",
    "AT RISK": "warning",
    "WARNING": "warning",
    "STALE": "warning",
    # "Cannot tell" is not "fine": both mean the check did not produce an answer.
    "UNKNOWN": "warning",
    "INSUFFICIENT_DATA": "warning",
    # Deliberately not done is not a failure and needs no symbol.
    "SKIPPED": PLAIN,
    "SKIPPED_EXISTS": PLAIN,
    "DRY_RUN": PLAIN,
    "CRITICAL": "critical",
    # An SLA run that produced no data did not pass — the SLI could not be computed, and an
    # operator who reads that as "fine" stops looking at the one check that stopped working.
    "NO_DATA": "warning",
    "ABORTED": "critical",
}

# How loud the event is. The fallback, and on its own it cannot tell "started" from "succeeded"
# — both are logging — which is why phase is consulted first.
#
# `logging` maps to `plain`, not to nothing: a producer that routes at logging level has *said*
# this is routine. Leaving it unset would mean "nobody knows", which sends the send layer back
# to guessing from the header — and guessing is what this column exists to stop. It is also why
# a routine summary's `[part 2/n]` chunks now come out consistently unadorned instead of
# occasionally picking up a symbol from a word in the body.
_LEVEL_TYPES = {
    "critical": "critical",
    "error": "failed",
    "warning": "warning",
    "warn": "warning",
    "logging": PLAIN,
    "log": PLAIN,
    "info": PLAIN,
}


def message_type_for(
    *,
    level: str | None = None,
    phase: str | None = None,
    status: str | None = None,
) -> str:
    """The display type for an event, from whatever the producer happens to know.

    Phase wins over level, because level cannot distinguish a start from a success: a restore
    that started and a restore that finished cleanly are both ``logging``. Level then decides
    how loud a phase-less event is, and a failure phase is never quietly downgraded — an
    ``END`` at ``error`` level reports as a failure, not a success.

    Returns ``""`` when nothing was supplied, which the caller stores as ``NULL``: "nobody
    said", leaving the send layer to read the header as before.
    """
    level_key = str(level or "").strip().lower()
    level_type = _LEVEL_TYPES.get(level_key, "")

    # A loud level overrides an optimistic phase. `phase=END, level=error` is a run that
    # finished by failing, and reporting that with ✅ is worse than reporting nothing.
    if level_type in ("critical", "failed"):
        return level_type

    for candidate in (phase, status):
        mapped = _PHASE_TYPES.get(str(candidate or "").strip().upper(), "")
        if mapped:
            return mapped

    return level_type


def last_queued_at(*, store: Any, source_type: str, note: str = "") -> str:
    """When this producer last put a message on the queue. Empty string when it never has.

    Callers that notify on change need this to time a periodic re-statement: the clock has to run
    from the last message a reader could actually have seen, not from the last time the job ran,
    or a check that stays silent postpones its own reminder indefinitely.

    Sends that failed outright (``send_status = -1``) do not count. Treating a message that never
    left the queue as "the reader has been told" would let a broken bot token silence the reminder
    as effectively as it silences everything else.
    """
    if not source_type:
        return ""
    clauses = ["source_type = ?", "send_status <> -1"]
    params: list[object] = [source_type]
    if note:
        clauses.append("note = ?")
        params.append(note)
    with store.connect() as conn:
        rows = list(
            conn.execute(
                f"""
                SELECT COALESCE(send_date, row_ins_date) AS last_at
                FROM telegram_send_messages
                WHERE {' AND '.join(clauses)}
                ORDER BY send_tlgmsg_id DESC
                LIMIT 1;
                """,
                params,
            )
        )
    return str(rows[0]["last_at"] or "") if rows else ""


def queue_telegram_message(
    *,
    store: Any,
    chat_id: str,
    text: str,
    message_type: str | None = None,
    level: str | None = None,
    phase: str | None = None,
    status: str | None = None,
    note: str = "",
    source_type: str | None = None,
    source_id: str | None = None,
    reply_message_id: int | None = None,
    entities: str | None = None,
    metadata: dict | None = None,
) -> int:
    """Queue one outgoing message and return its ``send_tlgmsg_id``.

    ``message_type`` is the display type when the caller knows it outright. When it does not,
    pass whatever it does have — ``level`` / ``phase`` / ``status`` — and the type is derived
    by :func:`message_type_for`. Pass ``message_type="plain"`` for a message that carries no
    status at all (a command reply, a listing): that suppresses the header guess, so a listing
    whose text happens to contain "error" is not tagged as a failure.
    """
    resolved = normalize_message_type(message_type)
    if not resolved:
        resolved = normalize_message_type(message_type_for(level=level, phase=phase, status=status))
    return store.insert_telegram_send_message(
        tlgchat_id=chat_id,
        message_text=text,
        reply_message_id=reply_message_id,
        entities=entities,
        note=note,
        source_type=source_type,
        source_id=source_id,
        metadata=metadata,
        message_type=resolved or None,
    )
