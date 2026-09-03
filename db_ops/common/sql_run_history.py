"""What has the SQL task runner actually been doing? — the history, not the schedule.

``sql_tasks`` runs the tasks and records every run in ``sql_runs``. Nothing read that back for a
person. ``list-tasks`` answers "what tasks exist", which is the configuration; after an alert the
question is the other one — *what ran, and how did it end* — and answering it meant opening the
store by hand. On 2026-09-03 that was done four times in one session, each time with a hand-written
query, which is the shape of a missing command.

This module is only the reading and the rendering. It does not run tasks and does not know how to:
that is ``sql_tasks``' job, and the two never import each other. Same split as
:mod:`db_ops.common.restore_drill`, for the same reason — the question is asked *by* operators and
reports, not by the app that performs the work.

The output is lines rather than JSON on purpose. Its first caller is a Telegram command, read on a
phone by whoever just got an alert, so the status comes first on each line and the failure reason
comes with it. A caller that wants structure has :func:`collect` and the rows themselves.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

#: Telegram cuts a message at 4096 characters, and a listing truncated by the transport loses its
#: newest rows with nothing to say it happened. This stops earlier and says so itself.
LISTING_CHARACTER_BUDGET = 3500

#: How many runs a caller gets when it does not say. Ten is a screenful on a phone and, at the
#: five-minute tasks this estate runs, roughly the last hour.
DEFAULT_LIMIT = 10

#: A ceiling, so a typo in a request cannot ask the store for the whole table.
MAX_LIMIT = 200


def collect(store: Any, *, limit: int = DEFAULT_LIMIT, sql_id: int | None = None) -> list:
    """The most recent runs, newest first, through the store's own reader."""
    bounded = max(1, min(int(limit), MAX_LIMIT))
    return store.fetch_recent_sql_runs(limit=bounded, sql_id=sql_id)


def _one_line(row: Any) -> str:
    started = str(row["started_at"] or "")[:19].replace("T", " ").replace("Z", "")
    duration = row["duration_ms"]
    elapsed = f"{int(duration) / 1000:.0f}s" if duration is not None else "-"
    rowcount = row["row_count"]
    rows_text = f" rows={rowcount}" if rowcount is not None else ""
    status = str(row["status"] or "?").upper()
    line = (f"#{row['sql_run_id']} [{status}] sql_id={row['sql_id']} {row['sql_code']}\n"
            f"    {started} took {elapsed}{rows_text} on {row['server_id']}")
    # A failed run without its reason sends the reader to the store, which is the trip this
    # command exists to save. One line of it; the rest stays in the store for whoever needs it.
    failure = str(row["error_text"] or "").strip()
    if failure and status != "DONE":
        line += f"\n    {failure.splitlines()[0][:160]}"
    return line


def render(rows: Sequence[Any], *, sql_id: int | None = None) -> str:
    """The history as a chat message: one entry per run, newest first, within the size budget."""
    scope = f" for sql_id {sql_id}" if sql_id is not None else ""
    if not rows:
        return f"No SQL task runs recorded{scope} yet."

    lines = [_one_line(row) for row in rows]
    kept: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > LISTING_CHARACTER_BUDGET and kept:
            break
        kept.append(line)
        used += len(line) + 1

    header = f"Last {len(kept)} SQL task run(s){scope}, newest first:"
    body = "\n".join(kept)
    dropped = len(lines) - len(kept)
    if dropped:
        body += f"\n... {dropped} more not shown (message size limit)."
    return f"{header}\n{body}"
