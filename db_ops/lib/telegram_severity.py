"""Severity emoji for outgoing Telegram messages — tagged once, at the send layer.

Lives in ``common`` rather than in the Telegram app because the *vocabulary* is shared:
``db.telegram_queue`` validates every queued row's ``message_type`` against
:data:`MESSAGE_TYPES`, and ``common.cli`` echoes the resolved type back to its caller. Both
were importing the Telegram app from the shared layer to get it — an inversion, since every
app already depends on ``common``. Same resolution as ``metrics/storage.py`` before it:
shared API moves down, and ``db_ops/telegram/severity.py`` stays as a re-export shim so the
send path and its tests keep their existing import.


An operator reads a Telegram group as a wall of text. A restore that failed and a restore
that started look the same at a glance, so the one message that needed action gets missed.
A leading emoji fixes that: the phone notification, the chat list preview and the message
itself all lead with a symbol that says how urgent this is before a single word is read.

The tag is applied **here**, in the send path, not in the dozen producers that queue rows
into ``telegram_send_messages`` (backup/restore events, reports, SLA, the jobs daemon, the
command processor). One place means one vocabulary, and a new producer is tagged without
knowing this module exists.

The vocabulary — six levels, no more, because a set an operator has to look up is a set that
does not help:

===========================  =====  ==========================================
Meaning                      Emoji  Example header
===========================  =====  ==========================================
Workflow started             ▶️      ``▶️ LOGGING|host|Restore workflow started.``
Success                      ✅      ``✅ LOGGING|host|Restore workflow finished. status=done``
Failure                      ❌      ``❌ ERROR|host|Restore workflow FAILED.``
Warning                      ⚠️      ``⚠️ [Metrics Warning Report]``
Long running / in progress   ⏳      ``⏳ [SQL] SQL task running``
Critical / aborted           🚨      ``🚨 [Metrics Critical Report]``
===========================  =====  ==========================================

Severity is read off the **header line** only — the first line, or the line after a
``[part i/n]`` chunk marker. Bodies carry words like "error" and "running" inside a JSON
payload or a lock dump, and classifying on those would tag half the traffic wrongly. A
message whose header says nothing about severity (a command reply, a listing, a routine
summary) is left exactly as it was: silence is better than a misleading symbol.
"""

from __future__ import annotations

import re


__all__ = [
    "MESSAGE_TYPES",
    "PLAIN",
    "SEVERITY_EMOJI",
    "STATUS_EMOJI_CHARS",
    "classify_message",
    "decorate_message",
    "normalize_message_type",
]


# The six meanings, in the order they win when a header carries more than one signal:
# "Restore workflow ABORTED after failure" is critical, not a failure.
SEVERITY_EMOJI: dict[str, str] = {
    "critical": "🚨",
    "failed": "❌",
    "warning": "⚠️",
    "started": "▶️",
    "running": "⏳",
    "success": "✅",
}

#: A message that carries no status at all: a command reply, a listing, a prompt, a routine
#: summary. It is **not** the same as "unset". Unset (``NULL`` in the column) means nobody said,
#: so the header is read as a guess; ``plain`` means a producer said *there is nothing to say* —
#: so no emoji is added **and the guess is switched off**. That second half is the point: a
#: listing whose text happens to contain "error" or "running" would otherwise be tagged ❌ or ⏳
#: by a header rule that was never meant to judge it.
PLAIN = "plain"

#: Everything a producer may store in ``telegram_send_messages.message_type``.
MESSAGE_TYPES: tuple[str, ...] = (*SEVERITY_EMOJI, PLAIN)


#: Spellings accepted on input and stored as their canonical type. These are the *notify level*
#: words (``error``, ``warn``, ``logging``), which everyone reaches for by habit because that is
#: the vocabulary the rest of the config uses. Accepting them costs nothing and stops a caller's
#: reasonable guess from being silently dropped; the stored value stays canonical, so the two
#: axes never blur into one.
_TYPE_ALIASES = {
    "error": "failed",
    "failure": "failed",
    "fail": "failed",
    "warn": "warning",
    "logging": PLAIN,
    "log": PLAIN,
    "info": PLAIN,
    "none": PLAIN,
    "start": "started",
    "ok": "success",
    "done": "success",
}


def normalize_message_type(message_type: str | None) -> str:
    """A caller's ``message_type`` reduced to a canonical value, or ``""`` when it says nothing.

    Unknown junk is treated as unset rather than raising: a mistyped type must not stop an
    operational message from being delivered. The wrong tag is a cosmetic bug; a swallowed
    alert is an outage nobody hears about.
    """
    value = str(message_type or "").strip().lower()
    if value in MESSAGE_TYPES:
        return value
    return _TYPE_ALIASES.get(value, "")

# Base characters of every status emoji this module (or a producer) may already have put at
# the front of a message. Tagging is idempotent: the SLA app writes its own "❌ SLA/SLO
# compliance: FAILED" header, and re-sending a message must never stack "❌ ❌".
STATUS_EMOJI_CHARS = ("🚨", "❌", "⚠", "▶", "⏳", "✅", "⬜", "🟢", "🟡", "🔴", "ℹ")

# Level tokens the shared header format leads with: "ERROR|hostname|message".
_LEVEL_HEADER = re.compile(r"^\s*(LOGGING|INFO|WARNING|WARN|ERROR|CRITICAL)\s*\|", re.IGNORECASE)

# The chunk marker split_telegram_message prepends to a long report.
_PART_MARKER = re.compile(r"^\s*\[part\s+(\d+)\s*/\s*(\d+)\]\s*$", re.IGNORECASE)

# Keywords per severity, matched against the upper-cased header line. Kept deliberately
# short: every word here is one an operator would accept as a summary of the whole message.
_CRITICAL_WORDS = ("ABORT", "CRITICAL", "FATAL", "EMERGENCY")
_FAILED_WORDS = ("FAILED", "FAILURE", "FAILING", "ERROR", "EXCEPTION", "TIMED OUT", "TIMEOUT")
_WARNING_WORDS = ("WARNING", "WARN", "AT RISK", "PARTIAL", "STALE")
_STARTED_WORDS = ("STARTED", "START:", "STARTING", "BEGIN")
_RUNNING_WORDS = ("STILL RUNNING", "IN PROGRESS", "RUNNING", "PENDING", "QUEUED")
_SUCCESS_WORDS = ("SUCCESS", "COMPLETED", "FINISHED", "DONE", "PASSED", "END:", "OK (")


def decorate_message(text: str, message_type: str | None = None) -> str:
    """``text`` with its severity emoji in front, or unchanged when there is nothing to say.

    ``message_type`` is what the **producer** declared (the stored column). It wins over the
    header heuristic, because the producer knows: ``backup_restore`` has the phase and level in
    hand when it queues the row, and a report knows its own level for *every* chunk — including
    the ``[part 2/2]`` continuations that have no header to read and so were going out untagged.
    The heuristic stays as the fallback for rows that declare nothing, so old rows and any
    producer not yet migrated keep their emoji.
    """
    if not text:
        return text
    if text.lstrip().startswith(STATUS_EMOJI_CHARS):
        return text
    declared = normalize_message_type(message_type)
    if declared == PLAIN:
        return text
    severity = declared or classify_message(text)
    if not severity or severity == PLAIN:
        return text
    return f"{SEVERITY_EMOJI[severity]} {text.lstrip()}"


def classify_message(text: str) -> str:
    """The severity of a message: a key of :data:`SEVERITY_EMOJI`, or ``""`` when unclear."""
    header = _header_line(text)
    if not header:
        return ""
    upper = header.upper()
    level = _header_level(header)

    if level == "critical" or _has_word(upper, _CRITICAL_WORDS):
        return "critical"
    if level == "error" or _has_word(upper, _FAILED_WORDS):
        return "failed"
    if level == "warning" or _has_word(upper, _WARNING_WORDS):
        return "warning"
    # Start before running, so "backup START: ... started." is not read as "in progress".
    if _has_word(upper, _STARTED_WORDS):
        return "started"
    if _has_word(upper, _RUNNING_WORDS):
        return "running"
    if _has_word(upper, _SUCCESS_WORDS):
        return "success"
    return ""


def _header_line(text: str) -> str:
    """The line that states what this message is about, or ``""`` when it has none.

    A ``[part i/n]`` marker is not a header, so the real header underneath it is used — but
    only for the first chunk. A later chunk starts mid-body, where "running" is a column in a
    lock dump rather than a status, and guessing from it produces exactly the wrong symbol.
    """
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        marker = _PART_MARKER.match(line)
        if marker is None:
            return line
        if int(marker.group(1)) != 1:
            return ""
    return ""


def _header_level(header: str) -> str:
    """The severity token of a ``LEVEL|host|message`` header, normalized, or ``""``."""
    match = _LEVEL_HEADER.match(header)
    if match is None:
        return ""
    token = match.group(1).lower()
    if token in ("warn", "warning"):
        return "warning"
    if token == "error":
        return "error"
    if token == "critical":
        return "critical"
    return "logging"


def _has_word(upper_header: str, words: tuple[str, ...]) -> bool:
    return any(word in upper_header for word in words)
