"""Reading a value that may not be the type it means.

``data/*.json`` is edited by people and by the bot, so ``"ssl": "true"``, ``"ssl": true`` and an
absent key all arrive at the same reader, and ``"port": "5985"`` is as common as ``5985``. Both
helpers take the default as a keyword because the *absence* of a value and a value that fails to
parse are different facts with the same answer, and naming the answer at the call site is what
keeps a caller from inventing its own fallback three lines later.

They lived as ``_as_bool`` / ``_as_int`` inside ``common/remote_exec.py`` until 2026-08-15, where
``cmd_access`` parsing needed them and ``lib`` may not import ``common``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

__all__ = ["as_bool", "as_float", "as_int", "as_optional_int", "as_text", "as_utc_datetime"]

#: The spellings of "yes" that appear in this project's config and in shell-set environment
#: variables. Anything else is false — a config that says "maybe" is not a config that says yes.
_TRUE_WORDS = {"1", "true", "yes", "y", "on"}


def as_bool(value: Any, *, default: bool) -> bool:
    """``True``/``False`` from a JSON boolean, a word, or an absent value."""
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _TRUE_WORDS


def as_int(value: Any, *, default: int) -> int:
    """A whole number, falling back to ``default`` for absent or unparseable input."""
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def as_optional_int(value: Any) -> int | None:
    """A whole number, or ``None`` when there is not one — *unknown*, not zero.

    The integer twin of :func:`as_float`'s ``default=None``, and shared for the same reason: a
    version field that could not be parsed must stay unknown, because every rule that reads one
    treats ``None`` as "do not gate on this" and would read ``0`` as "older than everything".
    ``metrics/collector.py`` and ``lib/target_profile.py`` both need exactly that.
    """
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def as_float(value: Any, *, default: float | None = None) -> float | None:
    """A number, or ``default`` when the value is not one.

    ``default=None`` rather than ``0.0``, and that is the whole reason this is shared: a metric
    value that could not be parsed is **unknown**, and zero is a measurement. Grading `None` as
    "0% used" is how a disk with an unreadable size reports healthy.

    Existed three times under three names until 2026-08-16 — ``health_model.to_number`` (which
    nothing called), ``inventory_render._num`` and ``policy_engine._float_or_none`` — all three in
    ``lib``, all three byte-identical, and invisible to the duplicate guard because it matches on
    the name.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def as_text(value: Any) -> str:
    """Text, from ``str``, ``bytes`` or nothing. Never raises on an undecodable byte.

    ``errors="replace"`` rather than ``strict``: this reads a remote command's stdout and a
    collector's output, and a single bad byte in a log line must not lose the whole line — the
    line is usually the reason someone is looking.

    Was ``remote_exec._text`` and ``metrics.collector._safe_text``, byte for byte, until
    2026-08-16.
    """
    if value in (None, ""):
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def as_epoch(value: Any) -> float | None:
    """Seconds since the epoch from an ISO timestamp, or ``None`` when it is not one.

    :func:`as_utc_datetime` with ``.timestamp()`` on it, because arithmetic on two collection
    times is subtraction and not calendar work. Existed twice under the same name until
    2026-09-03 — ``capacity_forecast._epoch`` and ``interval_rates._epoch``, byte for byte — one
    on the path that decides when a disk runs out, the other on the path that decides how much
    work an instance did in the last hour. Both are timestamps out of ``collected_at``.
    """
    parsed = as_utc_datetime(value)
    return parsed.timestamp() if parsed is not None else None


def as_utc_datetime(value: Any) -> "datetime | None":
    """An aware UTC datetime from an ISO timestamp, or ``None`` when it is not one.

    Two details do the work. ``Z`` is rewritten to ``+00:00`` because ``fromisoformat`` did not
    accept it before Python 3.11 and stored timestamps are written with it; and a value that
    parses **without** a timezone is read as UTC rather than as local time — every timestamp this
    reads was written as UTC, and treating one as local silently shifts a backup's age by the
    host's offset, which is how a stale backup passes a freshness check.

    Was ``backup_restore.schedule._parse_utc`` and ``common.restore_drill._epoch``, byte for byte,
    until 2026-08-16 — one on the path that decides whether a backup is due, the other on the path
    that decides whether a restore drill counts.
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
