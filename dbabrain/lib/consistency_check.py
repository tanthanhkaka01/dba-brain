"""How long a restore drill's consistency check may run, and on how many connections (0.37.0).

The check after a restore - ``pg_amcheck --heapallindexed``, RMAN ``VALIDATE``, ``DBCC CHECKDB`` -
reads every block of the restored copy. Its time limit was a literal 3600 seconds in
``common.verifyrestore``: the runtime store's 60 GB restore drill on 2026-11-10 ran its single-job
``pg_amcheck`` past the hour on NFS, and the restore was reported failed with the data intact. Run
with 4 jobs the same check took 47 minutes. So both are the entry's own fields, ``checkdb_timeout_seconds``
and ``checkdb_jobs``, read here once for every loader of a restore entry.

``checkdb_jobs`` is PostgreSQL's ``pg_amcheck --jobs``; the other engines check on one session.
"""

from __future__ import annotations

from typing import Any

from dbabrain.lib import errors

DEFAULT_TIMEOUT_SECONDS = 3600
MIN_TIMEOUT_SECONDS = 60
#: A day: a check longer than that is not a drill's check, it is a broken one.
MAX_TIMEOUT_SECONDS = 86400
DEFAULT_JOBS = 1
MAX_JOBS = 16


class ConsistencyCheckError(errors.ConfigError):
    """A restore entry's check limits are not usable."""


def _whole(value: Any, *, field: str, where: str, low: int, high: int, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ConsistencyCheckError(f"{where}.{field} must be a whole number from {low} to {high} "
                                    f"(or absent: {default}); got {value!r}.")
    return value


def parse_timeout(value: Any, *, where: str = "restore entry") -> int:
    """``checkdb_timeout_seconds``: seconds the consistency check may run on the target."""
    return _whole(value, field="checkdb_timeout_seconds", where=where, low=MIN_TIMEOUT_SECONDS,
                  high=MAX_TIMEOUT_SECONDS, default=DEFAULT_TIMEOUT_SECONDS)


def parse_jobs(value: Any, *, where: str = "restore entry") -> int:
    """``checkdb_jobs``: connections ``pg_amcheck`` checks on at once."""
    return _whole(value, field="checkdb_jobs", where=where, low=1, high=MAX_JOBS, default=DEFAULT_JOBS)


def verify_fields(entry: Any) -> dict[str, int]:
    """The ``verify-restore`` request fields an entry's limits become."""
    return {"consistency_timeout_seconds": int(getattr(entry, "checkdb_timeout_seconds", DEFAULT_TIMEOUT_SECONDS)),
            "consistency_jobs": int(getattr(entry, "checkdb_jobs", DEFAULT_JOBS))}
