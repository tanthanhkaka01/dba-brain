"""What did I last ask the bot? — one person's own commands, written back as one line each.

Most of this bot's useful commands take arguments a person cannot keep in their head: a sql_id
from one listing, a server_id from another, a date range. Getting them again means scrolling the
chat, and a command that was answered **one prompt at a time** cannot be scrolled to at all — the
message that started it says ``/spbot_run_sql_task`` and nothing else, and the ``18`` and the
``0 30`` are separate messages further down, indistinguishable from conversation.

So this rebuilds the line: the command, then its answers in argument order, exactly as the
dispatcher would read them back. ``/spbot_run_sql_task`` + ``18`` + ``0 30`` is
``/spbot_run_sql_task 18 0 30``, one line to copy.

Distinct by that line, newest first: someone who ran the same command nine times while chasing a
problem wants their other commands back, not nine copies of one. The repeats are counted rather
than dropped silently.

Only commands that actually ran are listed. A conversation the bot asked into and never got an
answer for is not a command anyone can repeat — it is half a question — so it is skipped, and the
listing says how many, because hiding without accounting is indistinguishable from losing.

Reading and rendering only. It does not run commands and does not know how to: that is the
Telegram app's job, and the two never import each other — the same split as
:mod:`db_ops.common.sql_run_history`, whose question ("what did the SQL tasks do") this one
mirrors for the person side.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

# The size budget and the default depth are the rule every /spbot_list_* reply follows; a second
# copy of them here is how two listings end up disagreeing about what fits in one message.
from db_ops.lib.listing import DEFAULT_LISTING_LIMIT, LISTING_CHARACTER_BUDGET
from db_ops.lib.telegram_command_text import (
    command_key_from_message,
    parse_command_message,
    render_command_line,
)

#: A ceiling, so a typo in a request cannot ask the store for the whole table.
MAX_LIMIT = 50

#: How many recent messages are read to find that many *distinct* commands. Repeats are the norm
#: — one estate ran ``/spbot_run_sql_task 28`` five times in an hour — so the window has to be
#: several times the answer or a busy morning returns three lines.
SCAN_MULTIPLIER = 25

#: And a ceiling on the window itself, for the same reason MAX_LIMIT exists.
MAX_SCAN = 500

#: A prompt chain that ended in one of these produced a command that was actually dispatched.
#: ``waiting`` (still being asked) and ``replaced`` (the person started over) did not: their
#: arguments are incomplete, and offering half a command invites someone to run it.
RAN_CONVERSATION_STATUSES = frozenset({"done", "error"})


def collect(
    store: Any,
    *,
    user_id: str,
    limit: int = DEFAULT_LISTING_LIMIT,
    exclude: Sequence[str] = (),
) -> dict[str, Any]:
    """The caller's own distinct commands, newest first, through the store's own reader.

    ``exclude`` drops command names from the answer — the history command itself passes its own
    name, since a listing whose first entry is always "you just asked for this listing" spends a
    line of ten on nothing.
    """
    bounded = max(1, min(int(limit), MAX_LIMIT))
    if not str(user_id).strip():
        # Not an error: a channel post carries no user, and there is no "my commands" for it.
        return {"entries": [], "unfinished": 0, "scanned": 0, "user_id": ""}

    scan = min(bounded * SCAN_MULTIPLIER, MAX_SCAN)
    rows = store.fetch_recent_telegram_command_messages(user_id=str(user_id), limit=scan)
    skip = {str(name).lstrip("/").strip().lower() for name in exclude if str(name).strip()}

    entries: list[dict[str, Any]] = []
    by_line: dict[str, dict[str, Any]] = {}
    unfinished = 0
    for row in rows:
        rebuilt = rebuild_command(row)
        if rebuilt is None:
            unfinished += 1
            continue
        if rebuilt["command_text"].lower() in skip:
            continue
        seen = by_line.get(rebuilt["line"])
        if seen is not None:
            # Newest first, so the entry already recorded is the most recent one. A repeat only
            # adds to the count; overwriting would date the entry by its oldest run.
            seen["times"] += 1
            continue
        if len(entries) >= bounded:
            # The window is still worth finishing: a repeat of something already listed must
            # keep counting, and only a *new* line is refused once the answer is full.
            continue
        by_line[rebuilt["line"]] = rebuilt
        entries.append(rebuilt)
    return {
        "entries": entries,
        "unfinished": unfinished,
        "scanned": len(rows),
        "user_id": str(user_id),
    }


def rebuild_command(row: Any) -> dict[str, Any] | None:
    """One stored command message as the line a person would type, or ``None`` if it never ran.

    The arguments come from the prompt conversation when there was one, because the message text
    holds only what preceded the first question. When there was no conversation the message text
    *is* the whole command, and it is used verbatim.
    """
    command_text = str(row["conversation_command_text"] or "").strip()
    if not command_text:
        command_text = command_key_from_message(
            str(row["command_prefix"] or ""), str(row["command_payload"] or ""),
        )
    if not command_text:
        return None

    status = str(row["conversation_status"] or "").strip().lower()
    if status:
        if status not in RAN_CONVERSATION_STATUSES:
            return None
        args = _conversation_args(row["conversation_state_json"])
    else:
        args = _inline_args(str(row["text"] or ""))

    return {
        "command_text": command_text,
        "args": args,
        "line": render_command_line(command_text, args),
        "sent_at": str(row["created_at"] or ""),
        "chat_id": str(row["chat_id"] or ""),
        "failed": status == "error",
        "times": 1,
    }


def _conversation_args(state_json: Any) -> list[str]:
    """The answers, in argument order — the prompt loop stores them by position, not by turn."""
    try:
        state = json.loads(state_json or "{}")
    except (TypeError, ValueError):
        return []
    if not isinstance(state, dict):
        return []
    return [str(value) for value in (state.get("args") or []) if value is not None]


def _inline_args(text: str) -> list[str]:
    """Everything after the command word of a message that needed no prompt.

    Parsed the way the dispatcher parses it rather than split on spaces, so a quoted argument
    stays one argument here too and the rebuilt line keeps the same shape.
    """
    parsed = parse_command_message(text)
    if not parsed["command_key"]:
        return []
    return list(parsed["args"])


def render(result: dict[str, Any]) -> str:
    """The history as a chat message: the command line, then when it was last sent."""
    entries = list(result.get("entries") or [])
    if not str(result.get("user_id") or "").strip():
        return ("This chat does not identify a sender, so there is no command history for it. "
                "Run the command from your own chat with the bot.")
    if not entries:
        return "You have not run any bot command yet."

    blocks = [_one_entry(index, entry) for index, entry in enumerate(entries, start=1)]
    kept: list[str] = []
    used = 0
    for block in blocks:
        if used + len(block) + 1 > LISTING_CHARACTER_BUDGET and kept:
            break
        kept.append(block)
        used += len(block) + 1

    header = f"Your last {len(kept)} command(s), newest first:"
    body = "\n".join(kept)
    dropped = len(blocks) - len(kept)
    if dropped:
        body += f"\n... {dropped} more not shown (message size limit)."
    unfinished = int(result.get("unfinished") or 0)
    if unfinished:
        body += (f"\n({unfinished} unanswered prompt(s) skipped: the bot asked a question and "
                 "never got an answer, so there is no whole command to repeat.)")
    return f"{header}\n{body}"


def _one_entry(index: int, entry: dict[str, Any]) -> str:
    """The line to copy comes first; everything under it is context for deciding to copy it."""
    line = f"{index}. {entry['line']}"
    sent = str(entry.get("sent_at") or "")[:19].replace("T", " ").replace("Z", "")
    parts = [sent or "date unknown"]
    times = int(entry.get("times") or 1)
    if times > 1:
        parts.append(f"x{times}")
    # A rejected argument is still in the line - it is what was typed - so it has to say so, or
    # the listing offers a command that was already refused once as if it had worked.
    if entry.get("failed"):
        parts.append("last one failed")
    return f"{line}\n    {'  '.join(parts)}"
