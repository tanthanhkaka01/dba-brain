"""The one timezone db_ops shows its operator, and the one place that decides what it is.

Every timestamp the store holds is UTC (``...Z``) and that does not change here: a stored value is
a key that has to sort and compare, and it can only do that in one zone. This module is about the
*other* half — the wall-clock time a person reads off a report header, a Telegram alert or a
filename, and the wall-clock hour a ``time_window`` means when it says ``from_hour: 1``.

Before this existed the answer came from three places, none of them configuration: the machine's
``TZ`` (so a report built on the Windows master and the same report built in the worker container
carried different clocks), a hardcoded ``+07`` in the reports app, and an env var nobody set. A
published tool cannot ask its operator to configure their timezone by editing a compose file, and
a timestamp printed without an offset is a second, unlabelled clock that nothing can be compared
against.

So: one declaration (``config.json`` -> ``timezone``), resolved once at config load, and one
rendered format::

    2026-09-07 07:32:56 +07        Asia/Ho_Chi_Minh
    2026-09-07 00:32:56 +00        UTC
    2026-09-07 06:02:56 +05:30     Asia/Kolkata - minutes appear only when they are not zero

This module is :mod:`db_ops.lib` and therefore pure: it imports nothing from ``db_ops``, is never a
CLI, and holds no config-file knowledge. :mod:`db_ops.config` parses the field and calls
:func:`bind_display_timezone` once; the operation that *records* the resolved zone in the store is
``python -m db_ops.common.cli timezone``. Apps call :func:`display_now` and :func:`format_display`
and never learn where the value came from — the same shape as ``node_role``.
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

#: Env var naming this node's zone, overriding ``config.json``. The worker gets a *copy* of the
#: master's config file, so a per-node answer must not require editing it - exactly the reason
#: ``DB_OPS_NODE_ROLE`` exists, and deliberately the same mechanism.
TIMEZONE_ENV_VAR = "DB_OPS_TIMEZONE"

#: The offset-only predecessor of this module. Still read when neither the config field nor
#: :data:`TIMEZONE_ENV_VAR` says anything, so an install that set it keeps its clock across the
#: upgrade instead of silently reverting to UTC. Deprecated; see :func:`declaration_from_env`.
LEGACY_OFFSET_ENV_VAR = "DB_OPS_MESSAGE_UTC_OFFSET_HOURS"

#: What an install gets when it declares nothing. UTC, because that is what the store already
#: holds: a default of "the machine's clock" is how the tool got three answers in the first place.
DEFAULT_TIMEZONE = "UTC"

#: ``+07``, ``+07:00``, ``-0330``, ``UTC+7``, ``Z``. The forms an operator actually types.
_OFFSET_PATTERN = re.compile(
    r"^(?:UTC|GMT)?(?P<sign>[+-])(?P<hours>\d{1,2})(?::?(?P<minutes>\d{2}))?$",
    re.IGNORECASE,
)

#: Past this a value is not an offset but a typo - minutes mistaken for hours, or a stray digit -
#: and applying it would move the reported minute into another day. The real range is -12..+14.
_MAX_OFFSET_MINUTES = 14 * 60


class TimezoneError(ValueError):
    """The declared timezone cannot be honoured as written."""


def parse_declaration(value: Any, *, context: str = "timezone") -> str:
    """Normalise what an operator wrote into what :func:`resolve` accepts.

    Two shapes and no third: an IANA name (``Asia/Ho_Chi_Minh``) or a fixed offset (``UTC``,
    ``+07:00``, ``-03:30``). A fixed offset is normalised to ``+HH:MM`` so the stored declaration
    and the rendered one cannot disagree about spelling; an IANA name is passed through untouched
    and is only *validated* by :func:`resolve`, which is where the zone database is needed.

    Empty means :data:`DEFAULT_TIMEZONE` rather than an error: a config that predates the field is
    an upgrade, not a mistake. Anything else raises, naming what was read - a silently ignored
    timezone is a whole estate of timestamps in the wrong clock with nothing to point at.
    """
    text = str(value or "").strip()
    if not text:
        return DEFAULT_TIMEZONE
    if text.upper() in {"UTC", "GMT", "Z", "UTC+0", "UTC+00", "+0", "+00", "+00:00"}:
        return "UTC"
    match = _OFFSET_PATTERN.match(text)
    if match:
        minutes = _offset_minutes_from_match(match, context=context, source=text)
        return format_offset(minutes, always_minutes=True)
    if "/" in text or text.isalpha():
        # An IANA name. Not resolved here: this function is the *grammar* check, and the zone
        # database may legitimately be absent on a node that only ever uses fixed offsets.
        return text
    raise TimezoneError(
        f"{context}: {text!r} is not a timezone. Use an IANA name (e.g. 'Asia/Ho_Chi_Minh', "
        f"'America/New_York') or a fixed offset (e.g. 'UTC', '+07:00', '-03:30')."
    )


def _offset_minutes_from_match(match: re.Match[str], *, context: str, source: str) -> int:
    hours = int(match.group("hours"))
    minutes = int(match.group("minutes") or 0)
    total = hours * 60 + minutes
    if match.group("sign") == "-":
        total = -total
    if not -_MAX_OFFSET_MINUTES <= total <= _MAX_OFFSET_MINUTES:
        raise TimezoneError(
            f"{context}: {source!r} is outside the range of a real UTC offset (-14:00..+14:00)."
        )
    if minutes >= 60:
        raise TimezoneError(f"{context}: {source!r} has more than 59 minutes in its offset.")
    return total


def resolve(declaration: Any, *, context: str = "timezone"):
    """Turn a declaration into something ``datetime.astimezone`` accepts.

    A fixed offset needs nothing installed. An IANA name needs a zone database, and on Windows
    there is none in the standard library - ``zoneinfo`` looks for the ``tzdata`` package, which is
    why it is a core dependency rather than an extra. When it is missing anyway (an air-gapped
    install, a stripped image) the error says which package and offers the fixed-offset way out,
    because "no time zone found with key" on its own tells the reader nothing they can act on.
    """
    text = parse_declaration(declaration, context=context)
    if text == "UTC":
        return timezone.utc
    match = _OFFSET_PATTERN.match(text)
    if match:
        return timezone(timedelta(minutes=_offset_minutes_from_match(
            match, context=context, source=text)))

    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError  # local: only IANA names need it

    try:
        return ZoneInfo(text)
    except ZoneInfoNotFoundError as exc:
        # ``ZoneInfoNotFoundError`` is raised both for "no zone database at all" and for "that is
        # not a zone". They need different answers - one is an install step, the other a typo - and
        # only asking for a zone everybody has tells them apart.
        try:
            ZoneInfo("UTC")
        except ZoneInfoNotFoundError:
            raise TimezoneError(
                f"{context}: no zone database on this machine, so {text!r} cannot be resolved. "
                f"Install the 'tzdata' package (`pip install tzdata`), or declare a fixed offset "
                f"such as '+07:00' instead - a fixed offset needs no zone database but does not "
                f"follow daylight saving."
            ) from exc
        raise TimezoneError(
            f"{context}: {text!r} is not a known IANA timezone name (e.g. 'Asia/Ho_Chi_Minh')."
        ) from exc
    except (ValueError, KeyError) as exc:
        raise TimezoneError(f"{context}: {text!r} is not a known IANA timezone name.") from exc


def format_offset(minutes: int, *, always_minutes: bool = False) -> str:
    """``+07``, ``+00``, ``+05:30``, ``-03:30``.

    Minutes are dropped when they are zero, which is the whole reason this is a function: most of
    the world is on a whole hour and ``+07:00`` is noise beside a time that is already 19 characters
    long. ``always_minutes`` is for the *stored* declaration, where a normalised ``+HH:MM`` means
    the value can be compared as a string.
    """
    sign = "-" if minutes < 0 else "+"
    hours, remainder = divmod(abs(int(minutes)), 60)
    if remainder or always_minutes:
        return f"{sign}{hours:02d}:{remainder:02d}"
    return f"{sign}{hours:02d}"


def offset_minutes(zone: Any = None, *, at: datetime | None = None) -> int:
    """This zone's offset from UTC, in minutes, **at a moment**.

    The moment matters and is not optional in spirit: under daylight saving the answer changes
    twice a year, so an offset recorded without the instant it was true for is a value that goes
    quietly wrong in March. That is why the store keeps the declaration *and* the resolved offset
    rather than deriving one from the other.
    """
    resolved = zone if _is_tzinfo(zone) else resolve(zone if zone is not None else display_zone())
    moment = (at or datetime.now(timezone.utc))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int((moment.astimezone(resolved).utcoffset() or timedelta(0)).total_seconds() // 60)


def zone_abbreviation(zone: Any = None, *, at: datetime | None = None) -> str:
    """``ICT``, ``UTC`` - whatever the zone calls itself at that moment, or ``""``.

    Recorded beside the offset because it is what an operator recognises; never parsed back, since
    abbreviations are not unique (``IST`` is three different zones).

    A fixed offset has no abbreviation - Python answers ``UTC+07:00``, which is the offset written
    out again. Empty is the honest answer, and it keeps the stored column meaning "the zone had a
    name" rather than carrying a second copy of a column beside it.
    """
    resolved = zone if _is_tzinfo(zone) else resolve(zone if zone is not None else display_zone())
    moment = (at or datetime.now(timezone.utc))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    name = str(moment.astimezone(resolved).tzname() or "")
    if name.upper().startswith(("UTC+", "UTC-", "GMT+", "GMT-")):
        return ""
    return name


# --------------------------------------------------------------------------------------------- #
# The process-wide display zone
#
# One binding, set by db_ops.config when the config file is read, because the alternative is
# threading a timezone through forty producers that have no other reason to know about one - and
# the producer that gets missed is the one that prints a second clock. Apps never call
# bind_display_timezone; they call display_now/format_display and get whatever config resolved.
# --------------------------------------------------------------------------------------------- #

_display_declaration: str = DEFAULT_TIMEZONE
_display_zone: Any = timezone.utc


def bind_display_timezone(declaration: Any, *, context: str = "config.json:timezone") -> str:
    """Set the zone every later :func:`display_now` / :func:`format_display` renders in.

    Called once, from :func:`db_ops.config.parse_config`. Raises on a bad declaration: a config
    error must stop the process at the point the config is read, not produce forty timestamps in
    the wrong clock and be discovered from a report.
    """
    global _display_declaration, _display_zone
    text = parse_declaration(declaration, context=context)
    _display_zone = resolve(text, context=context)
    _display_declaration = text
    return text


def display_declaration() -> str:
    """The declaration as written (``Asia/Ho_Chi_Minh``, ``+07:00``, ``UTC``)."""
    return _display_declaration


def display_zone():
    """The bound ``tzinfo``. UTC until :func:`bind_display_timezone` says otherwise."""
    return _display_zone


def declaration_from_env() -> str | None:
    """This node's zone from the environment, or ``None`` when the environment is silent.

    :data:`TIMEZONE_ENV_VAR` wins. :data:`LEGACY_OFFSET_ENV_VAR` - an offset in hours, the only
    mechanism there used to be - is honoured after it so an install that set it does not silently
    revert to UTC on upgrade. Neither raises here: :func:`bind_display_timezone` is where a bad
    value has to be reported, with the config context attached.
    """
    named = os.getenv(TIMEZONE_ENV_VAR, "").strip()
    if named:
        return named
    legacy = os.getenv(LEGACY_OFFSET_ENV_VAR, "").strip()
    if not legacy:
        return None
    try:
        hours = float(legacy)
    except ValueError:
        return None
    if not -14.0 <= hours <= 14.0:
        return None
    return format_offset(int(round(hours * 60)), always_minutes=True)


# --------------------------------------------------------------------------------------------- #
# Rendering and "now"
# --------------------------------------------------------------------------------------------- #

#: The date/time half of :func:`format_display`. The offset is appended by :func:`format_offset`.
DISPLAY_FORMAT = "%Y-%m-%d %H:%M:%S"

#: The stamp that names a generated file (``20260907_073256``). Rendered in the display zone, so
#: the filename and the header inside the file say the same hour - they did not before, because the
#: stamp came from the machine clock and the row it describes was UTC.
FILE_STAMP_FORMAT = "%Y%m%d_%H%M%S"


def display_now(zone: Any = None) -> datetime:
    """Now, as an aware datetime in the display zone.

    This is what a ``time_window`` is evaluated against. ``from_hour: 1`` then means 01:00 where
    the operator declared they are, on every node, rather than 01:00 on whatever clock the host
    happens to keep.
    """
    resolved = zone if _is_tzinfo(zone) else (display_zone() if zone is None else resolve(zone))
    return datetime.now(timezone.utc).astimezone(resolved)


def display_today(zone: Any = None) -> date:
    """Today's calendar date in the display zone.

    The day a daily report belongs to, and the day a log file rotates on. Both end at the
    operator's midnight; a UTC day boundary lets a once-a-day report out twice in ``+07``.
    """
    return display_now(zone).date()


def to_display(value: datetime | None = None, zone: Any = None) -> datetime:
    """``value`` moved into the display zone. A naive ``value`` is read as UTC.

    Naive means UTC and not "local" on purpose: every naive datetime in this tree came out of a
    column that stores ``%Y-%m-%dT%H:%M:%SZ``, and reading one as local time shifts it by the
    offset and then renders the shifted value with the offset - wrong twice.
    """
    if value is None:
        return display_now(zone)
    moment = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    resolved = zone if _is_tzinfo(zone) else (display_zone() if zone is None else resolve(zone))
    return moment.astimezone(resolved)


def format_display(value: datetime | None = None, zone: Any = None) -> str:
    """``2026-09-07 07:32:56 +07`` - the one wall-clock format a person reads anywhere in db_ops.

    Always carries the offset. A report header that says ``Snapshot 2026-09-07 07:32:56`` and
    nothing else cannot be lined up against ``metric_results.collected_at``, and the reader has no
    way to tell whose 07:32 it is.

    Deliberately *not* the stored format (:func:`db_ops.lib.text_format.format_utc`). One value,
    two audiences: a column that sorts, and a sentence someone acts on.
    """
    moment = to_display(value, zone)
    minutes = int((moment.utcoffset() or timedelta(0)).total_seconds() // 60)
    return f"{moment.strftime(DISPLAY_FORMAT)} {format_offset(minutes)}"


def format_display_date(value: datetime | None = None, zone: Any = None) -> str:
    """``2026-09-07`` in the display zone. For a header that names a day, not a moment."""
    return to_display(value, zone).strftime("%Y-%m-%d")


def file_stamp(value: datetime | None = None, zone: Any = None) -> str:
    """``20260907_073256`` in the display zone - the prefix of every generated report file."""
    return to_display(value, zone).strftime(FILE_STAMP_FORMAT)


def format_display_text(stored: Any, zone: Any = None) -> str:
    """Render a **stored** timestamp string for a person: ``2026-09-07T00:32:56Z`` -> ``... +07``.

    The one replacement for the ``value.replace("T", " ").replace("Z", "")`` that appeared in six
    listings. That idiom did the worst possible thing to a UTC timestamp: it removed the only mark
    saying which clock it was on and changed nothing else, so a reader saw a wall-clock time that
    was neither theirs nor labelled.

    Anything that does not parse comes back unchanged, minus nothing. A listing is not worth an
    exception, and a value that is not a timestamp is usually a column that legitimately holds
    something else ("never", "").
    """
    text = str(stored or "").strip()
    if not text:
        return ""
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    return format_display(moment, zone)


def label_from_file_stamp(stamp: str, zone: Any = None) -> str:
    """Read a ``20260907_073256`` filename prefix back out as ``2026-09-07 07:32:56 +07``.

    The reports are served from an archive: ``?date=20260905_144812`` renders the snapshot that
    file belongs to, so the header has to show *that* moment and not the viewer's. The stamp is
    already in the display zone (:func:`file_stamp` wrote it), so this only labels it — no
    conversion, because converting an already-local time would move it.

    Shared rather than repeated in each report builder: four of them parse this same prefix, and
    the one that was missed is the one whose header shows a different hour from its own filename.
    A stamp that is not a stamp comes back unchanged; a header is not worth an exception.
    """
    text = str(stamp or "").strip()
    day = text[:8]
    if len(day) != 8 or not day.isdigit():
        return text
    label = f"{day[:4]}-{day[4:6]}-{day[6:8]}"
    clock = text[9:15]
    if len(clock) == 6 and clock.isdigit():
        label = f"{label} {clock[:2]}:{clock[2:4]}:{clock[4:6]}"
    # The offset is read at the stamp's own instant, so a report archived in January and one
    # archived in July carry the offsets that were true then rather than the one true now.
    try:
        moment = datetime.strptime(text[:15], "%Y%m%d_%H%M%S") if len(text) >= 15 else \
            datetime.strptime(day, "%Y%m%d")
        resolved = zone if _is_tzinfo(zone) else (display_zone() if zone is None else resolve(zone))
        at_utc = moment.replace(tzinfo=resolved).astimezone(timezone.utc)
    except ValueError:
        return label
    return f"{label} {format_offset(offset_minutes(resolved, at=at_utc))}"


def format_stored(value: datetime | None = None) -> str:
    """The **stored** UTC format, restated here so a caller reaching for a timestamp finds both.

    Identical to :func:`db_ops.lib.text_format.format_utc`, which remains the definition; this
    delegates rather than reimplements, because two spellings of the stored format is the one
    drift that would break every range query in the tool.
    """
    from db_ops.lib.text_format import format_utc

    moment = datetime.now(timezone.utc) if value is None else value
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return format_utc(moment)


def describe(zone: Any = None, *, at: datetime | None = None) -> dict[str, Any]:
    """What this node's clock is, as data - for the store row and for ``common.cli timezone``.

    ``utc_offset_minutes`` is a snapshot true as of ``at``; ``timezone`` is the setting. Both, not
    one, because under daylight saving the first changes twice a year and only the second can be
    written back into a config file.
    """
    declaration = display_declaration() if zone is None else parse_declaration(zone)
    resolved = resolve(declaration)
    moment = at or datetime.now(timezone.utc)
    return {
        "timezone": declaration,
        "utc_offset_minutes": offset_minutes(resolved, at=moment),
        "utc_offset": format_offset(offset_minutes(resolved, at=moment)),
        "tz_abbreviation": zone_abbreviation(resolved, at=moment),
        "now_display": format_display(moment, resolved),
        "now_utc": format_stored(moment),
    }


def _is_tzinfo(value: Any) -> bool:
    return value is not None and hasattr(value, "utcoffset") and not isinstance(value, str)
