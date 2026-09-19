"""Which store failures are worth waiting out, and for how long before giving up.

**The run this exists for.** On 2026-09-18 the container holding the runtime store was restarted.
It was back in two seconds. The daemon was not: it raised ``FATAL 57P03`` — *the database system is
starting up* — the scan loop let it out, ``main`` logged one line and returned 1, and nothing
restarted the process. The only sign for the next nineteen hours was the absence of rows, and
0.18.0 was abandoned at hour 21.8 because of it.

A store that is *coming back* and a store that is *gone* are different facts and want different
answers. The loop already knew that for SQLite — ``database is locked`` has been retried with a
backoff since the beginning — and knew nothing about it for PostgreSQL, where the same category has
a name the server itself supplies: the SQLSTATE class ``08`` (connection exception) and the ``57Pxx``
shutdown codes.

**Bounded, deliberately.** Retrying forever turns a store that has really gone away into a daemon
that looks alive and schedules nothing, which is the failure this module is against, one level up.
The budget below is what separates "restarted" from "gone": past it the error is raised exactly as
it is today, and the process exits loudly.

This module holds the *rule* and no I/O, so it can be read and tested without a database — the whole
point of :mod:`db_ops.lib`. The sleeping and the logging belong to the caller.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: SQLSTATEs that mean *ask again in a moment*, not *this statement was wrong*. PostgreSQL sends
#: the code with the error, so this is the server's own classification and not a guess at its text.
#:
#: ``08xxx`` is the connection-exception class. ``57P01``/``57P02``/``57P03`` are the three ways a
#: server says it is on its way down or not yet up — ``57P03`` is the one that ended 0.18.0.
#: ``53300`` is *too many connections*: a real limit, but one that clears, and every db_ops process
#: opens its connection per statement and closes it, so waiting is the correct response.
TRANSIENT_SQLSTATES: frozenset[str] = frozenset({
    "08000", "08001", "08003", "08004", "08006", "08007", "08P01",
    "57P01", "57P02", "57P03",
    "53300",
})

#: Read only when there is no SQLSTATE to read: a connection lost at the socket never reaches the
#: server, so nothing sends a code back. pg8000 raises ``InterfaceError("network error")`` for it,
#: and the operating system raises its own. Kept short on purpose — every phrase here is one this
#: estate has actually seen, and a list that grows by imagination starts swallowing real bugs.
TRANSIENT_PHRASES: tuple[str, ...] = (
    "network error",
    "connection is closed",
    "connection already closed",
    "server closed the connection",
    "the database system is starting up",
    "the database system is shutting down",
    "connection reset",
    "connection refused",
    "connection aborted",
    "broken pipe",
    "no route to host",
    "timed out",
    "timeout expired",
)

#: How long the daemon may wait in total for one outage before letting the error out. Ten minutes
#: covers a container restart, a failover and a service restart on the store host; it does not cover
#: a store that has been moved, taken down for maintenance or lost its credentials, and those must
#: still reach a person.
BUDGET_SECONDS: int = 600

#: The longest single wait. A short first wait catches the two-second restart without losing a
#: scheduling cycle; the cap keeps the budget from being spent in three sleeps.
MAX_WAIT_SECONDS: int = 30


def sqlstate(error: BaseException) -> str:
    """The SQLSTATE PostgreSQL sent with this error, or ``""``.

    pg8000 raises its errors carrying the server's response fields as a mapping, where ``C`` is the
    code: ``DatabaseError({'S': 'FATAL', 'C': '57P03', 'M': 'the database system is starting up'})``.
    Read by shape rather than by class, because :mod:`db_ops.lib` may not import a database driver —
    and because a driver that grows a ``sqlstate`` attribute should be read through it instead.
    """
    direct = getattr(error, "sqlstate", None) or getattr(error, "pgcode", None)
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    for arg in getattr(error, "args", ()) or ():
        if isinstance(arg, dict):
            code = arg.get("C") or arg.get("code")
            if isinstance(code, str) and code.strip():
                return code.strip()
    return ""


def is_transient(error: BaseException) -> bool:
    """True when the store said *not now* rather than *no*.

    The SQLSTATE is read first and trusted alone: a code outside the transient set is a definite
    answer — a syntax error, a missing table, a permission — and retrying it only delays the report.
    """
    code = sqlstate(error)
    if code:
        return code in TRANSIENT_SQLSTATES
    text = f"{type(error).__name__}: {error}".lower()
    return any(phrase in text for phrase in TRANSIENT_PHRASES)


@dataclass
class OutageWaiter:
    """How long to wait for the next attempt, and when to stop waiting.

    One instance lives for the length of the loop. :meth:`wait_for` answers a failed pass, and
    :meth:`recovered` is called after a good one — the budget is per *outage*, not per process, or
    a daemon that has been up for a month would give up on its first hiccup.
    """

    budget_seconds: int = BUDGET_SECONDS
    max_wait_seconds: int = MAX_WAIT_SECONDS
    attempts: int = 0
    waited_seconds: int = field(default=0)

    def wait_for(self, error: BaseException) -> int | None:
        """Seconds to sleep before trying again, or ``None`` to let the error out.

        ``None`` means one of two things and the caller need not tell them apart: this is not a
        transient failure, or the budget for this outage is spent. Both end the same way — the
        error is raised as it would have been without this module.
        """
        if not is_transient(error):
            return None
        remaining = self.budget_seconds - self.waited_seconds
        if remaining <= 0:
            return None
        self.attempts += 1
        # 2, 4, 8, 16, 30, 30 … — the first wait is short because the outage this was written for
        # was two seconds long, and a scheduling cycle missed is a metric not collected.
        wait = min(self.max_wait_seconds, 2 ** self.attempts, remaining)
        self.waited_seconds += wait
        return wait

    def recovered(self) -> None:
        """A pass succeeded: this outage is over and the next one starts with a full budget."""
        self.attempts = 0
        self.waited_seconds = 0


__all__ = [
    "BUDGET_SECONDS",
    "MAX_WAIT_SECONDS",
    "TRANSIENT_PHRASES",
    "TRANSIENT_SQLSTATES",
    "OutageWaiter",
    "is_transient",
    "sqlstate",
]
