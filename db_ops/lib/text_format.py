"""One-line text helpers that more than one component has to agree on.

Each of these was written out twice, in two different apps, and each is small enough that copying
looked cheaper than sharing — which is exactly the size at which two copies quietly stop matching.
The cost is never the code. It is the agreement: a timestamp rendered two ways is two formats in
one report, and a log field escaped two ways is a log line that no longer splits on its delimiter.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone


def format_utc(value: datetime) -> str:
    """The one timestamp format the tool stores and renders."""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def format_log_value(value: object) -> str:
    """Make a value safe to put in a ``|``-delimited log line.

    The delimiter and the newlines are the whole point: a value carrying either one splits a
    record in two, and the second half is then read as a field it was never meant to be.
    """
    return str(value).replace("|", "/").replace("\n", " ").replace("\r", " ")


#: Env var naming the offset operator-facing times are rendered in, in hours (``7``, ``5.5``,
#: ``-3``). Zero — plain UTC — is the default because every timestamp the tool *stores* is UTC:
#: a message showing a local time without saying which one is a second, unlabelled clock, and a
#: reader comparing it against ``sql_runs.started_at`` would be an hour out with no way to tell.
#: One env var rather than a per-producer setting, so two messages about the same failure can
#: never be stamped in two zones.
MESSAGE_UTC_OFFSET_ENV = "DB_OPS_MESSAGE_UTC_OFFSET_HOURS"
DEFAULT_MESSAGE_UTC_OFFSET_HOURS = 0.0


def message_utc_offset_hours() -> float:
    """The configured offset, or 0 when it is unset or unreadable.

    Never raises: a malformed value must not stop an alert from being sent — the whole point of
    the message is that something already went wrong.
    """
    raw = os.getenv(MESSAGE_UTC_OFFSET_ENV, "").strip()
    if not raw:
        return DEFAULT_MESSAGE_UTC_OFFSET_HOURS
    try:
        hours = float(raw)
    except ValueError:
        return DEFAULT_MESSAGE_UTC_OFFSET_HOURS
    # Beyond this a value is not an offset but a typo (minutes, or a timestamp), and applying it
    # would move the reported minute into another day.
    if not -14.0 <= hours <= 14.0:
        return DEFAULT_MESSAGE_UTC_OFFSET_HOURS
    return hours


def message_timezone() -> timezone:
    """The zone :func:`format_message_time` renders in."""
    return timezone(timedelta(hours=message_utc_offset_hours()))


def format_message_time(value: datetime | None = None) -> str:
    """The wall-clock time an operator reads off a message: ``2026-09-04 13:05:22 UTC+00:00``.

    Separate from :func:`format_utc`, which is the *stored* format. A stored timestamp is a key
    that has to sort and compare; this one is read by a person at the moment they are asked to do
    something about it, so it says the date, the minute and — always — which clock it is on.

    A naive ``value`` is taken as UTC, because that is what every timestamp column here holds.
    """
    moment = datetime.now(timezone.utc) if value is None else value
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    zone = message_timezone()
    offset = moment.astimezone(zone).utcoffset() or timedelta(0)
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    hours, minutes = divmod(abs(total_minutes), 60)
    return (f"{moment.astimezone(zone).strftime('%Y-%m-%d %H:%M:%S')} "
            f"UTC{sign}{hours:02d}:{minutes:02d}")
