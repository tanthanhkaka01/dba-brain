"""Reading the moment a caller asked to recover to - once, for every engine.

Its own module because the timezone decision here is the kind that is invisible when wrong. An
operator in +07:00 asking for ``14:00`` means 14:00 *their* time; SQL Server reads ``STOPAT`` in
the **server's** local clock and rejects an offset outright, Oracle's ``TO_DATE`` refuses one, and
the backup listings report finish times in the server's clock too. Restoring seven hours off is not
an error anyone sees - the database comes up, and the missing afternoon is discovered by someone
looking for a row that should be there.

So the offset is required to be explicit when it matters, converted once, and the naive result is
what reaches a statement or a comparison. In ``lib`` since 0.23.0: it was the SQL Server engine
path's alone, and the restore primitives - ``restore-log`` for SQL Server, the Oracle duplicate,
the listing's time window - each passed the operator's text through as it came, offset and all
(found by the point-in-time drill, 2026-09-25).
"""

from __future__ import annotations

from db_ops.lib import errors
from datetime import datetime, timezone

#: Accepted forms, most explicit first. An offset is what makes the answer unambiguous.
_FORMATS = (
    "%Y-%m-%d %H:%M:%S %z",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M",
)


class MomentError(errors.RequestError):
    """The moment cannot be read."""


def parse_moment(text: str, *, server_utc_offset_hours: float | None = None) -> datetime:
    """Parse ``text`` into a naive datetime in the target server's clock.

    ``server_utc_offset_hours`` says what the server's clock is. Without it, a value carrying an
    offset is converted to UTC and returned naive - correct whenever the server runs on UTC, which
    every container in this estate does. A value with no offset is taken as already being in the
    server's clock, because that is the only thing it can mean.
    """
    raw = str(text or "").strip()
    if not raw:
        raise MomentError("point_in_time is empty.")
    for fmt in _FORMATS:
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            return parsed
        if server_utc_offset_hours is None:
            return parsed.astimezone(timezone.utc).replace(tzinfo=None)
        from datetime import timedelta

        server_zone = timezone(timedelta(hours=server_utc_offset_hours))
        return parsed.astimezone(server_zone).replace(tzinfo=None)
    raise MomentError(
        f"point_in_time {raw!r} is not a moment this understands. Use "
        "'YYYY-MM-DD HH:MM:SS +HH:MM' - the offset is what makes it unambiguous, since STOPAT is "
        "read in the server's own clock."
    )


def server_clock_text(text: str, *, server_utc_offset_hours: float | None = None) -> str:
    """``text`` as ``YYYY-MM-DD HH:MM:SS`` in the server's clock - what STOPAT, TO_DATE and the
    listings' finish times all are. UTC unless the server's offset is stated: every container
    target here runs on UTC."""
    return parse_moment(text, server_utc_offset_hours=server_utc_offset_hours).strftime("%Y-%m-%d %H:%M:%S")
