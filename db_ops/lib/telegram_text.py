"""Fitting an outgoing body into Telegram's message limit — by splitting it, never by cutting it.

Telegram rejects a `sendMessage` body over 4096 characters with HTTP 400, so something has to
give. For a long time two different things did, in two different places: the metrics reports
chunked themselves into ``[part i/n]`` messages here, while everything else was clipped — the SQL
task table stopped at 20 rows with "… 5 more row(s)", `ops_status` cut at 3880 with
"... (truncated)", and the send layer itself chopped anything still too long.

Clipping is the wrong trade. The reader cannot tell whether what they needed was in the part that
was dropped, and in practice it usually was: the rows that fell off the end of a
`/spbot_run_sql_task` result were the rows somebody ran the task to see. Deciding how much output
is reasonable belongs to whoever writes the query — `TOP` / `LIMIT` in the SQL — not to the
transport, which cannot know which half matters.

So this module is the one implementation, and it splits. `db_ops.telegram.api.send_message` applies
it to every outgoing body, which means every producer inherits the behaviour without knowing about
it; a producer that wants control over where the seams fall (the reports do, because each chunk is
queued as its own row) can call it directly.

**The ``[part i/n]`` marker is load-bearing**, not decoration: `db_ops.lib.telegram_severity`
reads it to tell a first chunk from a continuation. A continuation starts mid-body, where a word
like "running" is a column in a lock dump rather than a status, so guessing severity from it
produces exactly the wrong symbol — the marker is how that is avoided.
"""

from __future__ import annotations

#: Telegram's hard limit for a sendMessage text body.
TELEGRAM_MESSAGE_LIMIT = 4096

#: What producers aim at. Below the hard limit so the severity emoji, the ``[part i/n]`` marker
#: and any decoration the send layer adds still fit without pushing the body over.
TELEGRAM_SAFE_TEXT_LENGTH = 3900

#: Room left for the marker itself when chunking.
_PART_MARKER_ROOM = 80


def split_text_by_lines(text: str, *, max_length: int) -> list[str]:
    """``text`` as chunks of at most ``max_length``, broken at line boundaries.

    Line boundaries because the bodies that need splitting are tables and listings: cutting a
    markdown row in half yields a line with the wrong number of pipes, which renders as damage
    rather than as data.

    A single line longer than ``max_length`` has no boundary to offer and is hard-split — a
    stdout tail printed without newlines, say. It still has to arrive.
    """
    chunks: list[str] = []
    current_lines: list[str] = []
    current_length = 0

    for line in text.splitlines():
        line_length = len(line) + (1 if current_lines else 0)
        if current_lines and current_length + line_length > max_length:
            chunks.append("\n".join(current_lines))
            current_lines = []
            current_length = 0

        if len(line) > max_length:
            if current_lines:
                chunks.append("\n".join(current_lines))
                current_lines = []
                current_length = 0
            chunks.extend(line[start : start + max_length] for start in range(0, len(line), max_length))
            continue

        current_lines.append(line)
        current_length += len(line) + (1 if current_length else 0)

    if current_lines:
        chunks.append("\n".join(current_lines))
    return chunks or [""]


def split_telegram_message(text: str, *, max_length: int = TELEGRAM_SAFE_TEXT_LENGTH) -> list[str]:
    """``text`` as one or more sendable bodies, each marked ``[part i/n]`` when there is more
    than one.

    A body that already fits comes back as a single unmarked chunk — the overwhelming majority of
    what the bot sends is one short alert, and a part marker on those would be noise.
    """
    clean_text = text.strip()
    if len(clean_text) <= max_length:
        return [clean_text] if clean_text else [""]

    raw_chunks = split_text_by_lines(clean_text, max_length=max_length - _PART_MARKER_ROOM)
    part_count = len(raw_chunks)
    return [f"[part {index}/{part_count}]\n{chunk}"
            for index, chunk in enumerate(raw_chunks, start=1)]
