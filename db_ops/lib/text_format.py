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
