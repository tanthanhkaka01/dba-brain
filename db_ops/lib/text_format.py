"""One-line text helpers that more than one component has to agree on.

Each of these was written out twice, in two different apps, and each is small enough that copying
looked cheaper than sharing — which is exactly the size at which two copies quietly stop matching.
The cost is never the code. It is the agreement: a timestamp rendered two ways is two formats in
one report, and a log field escaped two ways is a log line that no longer splits on its delimiter.
"""

from __future__ import annotations

from datetime import datetime, timezone


def format_utc(value: datetime) -> str:
    """The one timestamp format the tool stores and renders."""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def format_log_value(value: object) -> str:
    """Make a value safe to put in a ``|``-delimited log line.

    The delimiter and the newlines are the whole point: a value carrying either one splits a
    record in two, and the second half is then read as a field it was never meant to be.
    """
    return str(value).replace("|", "/").replace("\n", " ").replace("\r", " ")


#: The offset-only predecessor of the ``timezone`` config field. Kept as a name so an install that
#: set it keeps its clock across the upgrade; the value is read by
#: :func:`db_ops.lib.timezone.declaration_from_env`, which is where the fallback order lives.
MESSAGE_UTC_OFFSET_ENV = "DB_OPS_MESSAGE_UTC_OFFSET_HOURS"


def format_message_time(value: datetime | None = None) -> str:
    """The wall-clock time an operator reads off a message: ``2026-09-04 13:05:22 +07``.

    Separate from :func:`format_utc`, which is the *stored* format. A stored timestamp is a key
    that has to sort and compare; this one is read by a person at the moment they are asked to do
    something about it, so it says the date, the minute and — always — which clock it is on.

    One function deep, now, rather than a second implementation. It used to resolve its own offset
    from an env var and render ``UTC+07:00``, which made two spellings of one value: this module's
    own docstring is about exactly that drift, and a message and a report header describing the
    same failure were stamped in two formats and, on a worker, two zones. The zone comes from
    ``config.json`` and the format from :func:`db_ops.lib.timezone.format_display`.
    """
    from db_ops.lib.timezone import format_display

    return format_display(value)
