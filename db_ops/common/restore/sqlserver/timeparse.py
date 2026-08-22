"""Reading the moment a caller asked to recover to.

Its own module because the timezone decision here is the kind that is invisible when wrong. An
operator in +07:00 asking for ``14:00`` means 14:00 *their* time; SQL Server reads ``STOPAT`` in
the **server's** local clock and rejects an offset outright. Restoring seven hours off is not an
error anyone sees - the database comes up, and the missing afternoon is discovered by someone
looking for a row that should be there.

So the offset is required to be explicit when it matters, converted once, and the naive result is
what reaches the statement builder.
"""

from __future__ import annotations

from datetime import datetime, timezone

#: Accepted forms, most explicit first. An offset is what makes the answer unambiguous.
_FORMATS = (
    "%Y-%m-%d %H:%M:%S %z",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M",
)


class MomentError(ValueError):
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
