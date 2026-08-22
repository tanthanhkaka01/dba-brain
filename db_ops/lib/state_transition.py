"""Decide whether a recurring check has anything *new* to say.

An hourly job that reports its full findings every hour teaches the reader to ignore it. The SLA
app sent 78 Telegram messages in 76 hours averaging 3,852 characters, and the content barely moved
between them: the same failing policies, re-listed, because `source_id` carried the window end and
so every run looked unique to the queue's de-duplication.

The fix is not a shorter message or a longer interval — it is to notify on *change*. Compare this
run's state against the previous run's and speak when something crossed a line: a finding that is
new, one that got worse, one that recovered. Otherwise stay quiet and let the web report carry the
standing detail, with a reminder no more often than once a day so an unresolved problem cannot be
silently forgotten.

Two distinctions here are the whole point, and both were learned from getting them wrong:

* **A key that vanished did not recover.** When a policy is retired or a target leaves the
  inventory, its failure disappears from the current run. Calling that a recovery announces good
  news that never happened — the same class of lie as reading "we could not connect" as "the
  service is fine". Vanished keys are reported separately, or not at all.
* **The reminder clock runs from the last message actually sent**, not from the last run. If a
  silent run reset it, the daily reminder would never fire: something is always suppressing it.

Generic on purpose — the state map is ``{key: status}`` and the severity ranking is supplied by the
caller, so the metrics and reports apps can route on transitions without a second implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Mapping, Sequence


#: One day. An unresolved finding is re-stated at most this often: often enough that it cannot be
#: forgotten, rarely enough that the message still means something when it arrives.
DEFAULT_REMINDER_SECONDS = 24 * 3600


@dataclass(frozen=True)
class StateDiff:
    """What changed between two runs of the same recurring check."""

    new_bad: tuple[str, ...] = ()
    """Keys that are failing now and were healthy (or unseen) before."""

    recovered: tuple[str, ...] = ()
    """Keys that were failing and are healthy now. Only keys present in *both* runs qualify."""

    worsened: tuple[tuple[str, str, str], ...] = ()
    """``(key, before, after)`` for a severity increase that is not a new failure."""

    improved: tuple[tuple[str, str, str], ...] = ()
    """``(key, before, after)`` for a severity decrease that is not a full recovery."""

    unchanged_bad: tuple[str, ...] = ()
    """Still failing, at the same severity. The standing backlog."""

    vanished_bad: tuple[str, ...] = ()
    """Was failing; absent from this run. Not a recovery — nobody measured it."""

    appeared_ok: tuple[str, ...] = ()
    """First seen in this run and healthy. Never worth waking anyone for."""

    baseline: bool = False
    """True when there was no previous run to compare against."""

    @property
    def has_transition(self) -> bool:
        """Did anything cross a line worth interrupting someone for?

        ``unchanged_bad`` is deliberately excluded — a backlog that has not moved is what the
        daily reminder is for. ``vanished_bad`` is excluded too: it is a monitoring change, not a
        service event, and it is reported in the body rather than used to force a send.
        """
        return bool(self.new_bad or self.recovered or self.worsened or self.improved)

    @property
    def counts(self) -> dict[str, int]:
        """The header numbers: new / recovered / unchanged, as the audit asked for."""
        return {
            "new_failed": len(self.new_bad),
            "recovered": len(self.recovered),
            "worsened": len(self.worsened),
            "improved": len(self.improved),
            "unchanged": len(self.unchanged_bad),
            "vanished": len(self.vanished_bad),
        }


@dataclass(frozen=True)
class NotifyDecision:
    """Whether to send, and which story the message should tell."""

    send: bool
    kind: str
    """``transition`` (something changed), ``reminder`` (daily re-statement), ``baseline``
    (nothing to compare against yet), or ``silent``."""
    reason: str
    """Human-readable, and logged: a suppressed notification must be explainable afterwards."""


def diff_states(
    previous: Mapping[str, str] | None,
    current: Mapping[str, str],
    *,
    severity_order: Sequence[str],
    healthy: Iterable[str] = ("PASSED", "OK"),
) -> StateDiff:
    """Compare two ``{key: status}`` maps.

    ``severity_order`` is worst-first; a status not listed ranks below every listed one, so an
    unknown status can never masquerade as an escalation and trigger a page.
    """
    rank = {str(status).strip().upper(): index for index, status in enumerate(severity_order)}
    healthy_set = {str(status).strip().upper() for status in healthy}
    unknown_rank = len(rank)

    def severity(status: str) -> int:
        return rank.get(str(status).strip().upper(), unknown_rank)

    def is_bad(status: str) -> bool:
        return str(status).strip().upper() not in healthy_set

    baseline = previous is None
    before = {str(key): str(value) for key, value in (previous or {}).items()}
    after = {str(key): str(value) for key, value in current.items()}

    new_bad: list[str] = []
    recovered: list[str] = []
    worsened: list[tuple[str, str, str]] = []
    improved: list[tuple[str, str, str]] = []
    unchanged_bad: list[str] = []
    appeared_ok: list[str] = []

    for key in sorted(after):
        now_status = after[key]
        was_status = before.get(key)
        if was_status is None:
            # Unseen before. Bad is news; healthy is not, and on a baseline run nothing is news
            # at all — a first run has no transition to report, only a starting position.
            if is_bad(now_status) and not baseline:
                new_bad.append(key)
            elif is_bad(now_status):
                unchanged_bad.append(key)
            else:
                appeared_ok.append(key)
            continue
        if is_bad(was_status) and not is_bad(now_status):
            recovered.append(key)
        elif not is_bad(was_status) and is_bad(now_status):
            new_bad.append(key)
        elif is_bad(now_status):
            if severity(now_status) < severity(was_status):
                worsened.append((key, was_status, now_status))
            elif severity(now_status) > severity(was_status):
                improved.append((key, was_status, now_status))
            else:
                unchanged_bad.append(key)

    # Gone from this run entirely. Deliberately not a recovery: see the module docstring.
    vanished_bad = sorted(key for key, status in before.items() if is_bad(status) and key not in after)

    return StateDiff(
        new_bad=tuple(new_bad),
        recovered=tuple(recovered),
        worsened=tuple(worsened),
        improved=tuple(improved),
        unchanged_bad=tuple(unchanged_bad),
        vanished_bad=tuple(vanished_bad),
        appeared_ok=tuple(appeared_ok),
        baseline=baseline,
    )


def decide_notification(
    diff: StateDiff,
    *,
    last_sent_at: str | datetime | None,
    now: datetime | None = None,
    reminder_after_seconds: int = DEFAULT_REMINDER_SECONDS,
    always: bool = False,
) -> NotifyDecision:
    """Send on a transition; otherwise re-state a standing backlog once a day; otherwise stay quiet.

    ``last_sent_at`` is when this channel last actually received a message — not when the check
    last ran. A run that decided to stay silent must leave the reminder clock alone, or the daily
    re-statement is postponed forever by its own suppression.
    """
    if always:
        return NotifyDecision(True, "transition", "notify_always requested")
    if diff.baseline:
        # Nothing to compare against. Send only if there is something wrong to establish as the
        # starting position; a clean first run is not worth announcing.
        if diff.unchanged_bad or diff.new_bad:
            return NotifyDecision(True, "baseline", "first run with findings; establishing baseline")
        return NotifyDecision(False, "silent", "first run, nothing failing")
    if diff.has_transition:
        counts = diff.counts
        return NotifyDecision(
            True,
            "transition",
            f"state changed: new_failed={counts['new_failed']} recovered={counts['recovered']} "
            f"worsened={counts['worsened']} improved={counts['improved']}",
        )
    if not diff.unchanged_bad:
        return NotifyDecision(False, "silent", "no transitions and nothing outstanding")

    moment = now or datetime.now(timezone.utc)
    previous = _parse_moment(last_sent_at)
    if previous is None:
        return NotifyDecision(True, "reminder", "outstanding findings and no previous notification on record")
    due_at = previous + timedelta(seconds=max(0, int(reminder_after_seconds)))
    if moment >= due_at:
        return NotifyDecision(
            True, "reminder", f"{len(diff.unchanged_bad)} findings outstanding since {previous.isoformat()}"
        )
    return NotifyDecision(
        False,
        "silent",
        f"{len(diff.unchanged_bad)} unchanged findings; next reminder at {due_at.isoformat()}",
    )


def _parse_moment(value: str | datetime | None) -> datetime | None:
    """Read a stored timestamp. Unparseable is treated as *unknown*, which makes the reminder due.

    Erring toward sending is the safe direction: the cost of a duplicate reminder is noise, while
    the cost of swallowing one is an unresolved failure nobody is told about again.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip().replace("Z", "+00:00").replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
