"""Which names a request selected, and — just as important — which patterns matched nothing.

Every operation that acts on *some* of a set of objects needs the same three answers: keep this
one, skip this one, and did the caller's pattern actually hit anything. The first two are
`fnmatch`; the third is the one that keeps being written and forgotten, and it is where the
failures come from. A deployment on `2026-08-22` carried an ``exclude_tables`` list into
production; a typo in one entry would have shipped six golden-harness staging tables onto a
payroll database and reported success, because "excluded nothing" and "excluded exactly what you
named" produce identical output everywhere except here.

Case-insensitive throughout, because these patterns name SQL Server objects and SQL Server's
default collation is. A caller on a case-sensitive engine that genuinely wants ``Foo`` and ``foo``
to differ is asking a question this module deliberately does not answer — it would be right once
and wrong on every other estate.

Pure: names in, names out. No engine, no connection, no config.
"""

from __future__ import annotations

import fnmatch
from typing import Iterable, Sequence


def matches(name: str, pattern: str) -> bool:
    """One name against one shell-glob pattern (``*``, ``?``, ``[seq]``), case-insensitively."""
    return fnmatch.fnmatch(str(name).lower(), str(pattern).lower())


def matches_any(name: str, patterns: Iterable[str]) -> bool:
    """True when *any* pattern matches. An empty pattern list matches nothing, never everything.

    The empty case is the one worth stating: ``exclude=[]`` must mean "exclude nothing", and the
    natural ``all()`` spelling of the same loop would make it mean "exclude everything".
    """
    return any(matches(name, pattern) for pattern in patterns or ())


def select(names: Iterable[str], *, include: Sequence[str] = (),
           exclude: Sequence[str] = ()) -> list[str]:
    """The kept names, in the order given.

    ``include`` empty means *everything* — the common case, where a caller only names the
    exceptions. ``exclude`` wins over ``include``, so a narrow "not this one" beside a broad
    "all of these" reads the way it looks.
    """
    kept, _ = split(names, include=include, exclude=exclude)
    return kept


def split(names: Iterable[str], *, include: Sequence[str] = (),
          exclude: Sequence[str] = ()) -> tuple[list[str], list[str]]:
    """``(kept, skipped)`` — both halves, because a caller that reports what it skipped is the
    one whose operator can tell a deliberate omission from a pattern that silently ate a table."""
    kept: list[str] = []
    skipped: list[str] = []
    for name in names:
        wanted = matches_any(name, include) if include else True
        if wanted and not matches_any(name, exclude):
            kept.append(name)
        else:
            skipped.append(name)
    return kept, skipped


def unused_patterns(names: Iterable[str], patterns: Sequence[str]) -> list[str]:
    """The patterns that matched nothing in ``names``.

    A pattern that hits nothing is almost always a typo or a name that has since changed, and it
    is invisible in the result: the object it was meant to catch is simply *there*, indistinguishable
    from one nobody thought about. Report these; do not silently drop them.
    """
    pool = list(names)
    return [pattern for pattern in patterns or ()
            if not any(matches(name, pattern) for name in pool)]


def describe(kept: Sequence[str], skipped: Sequence[str], *, noun: str = "object") -> str:
    """One line for a human: how many of what, and how many were left out."""
    total = len(kept) + len(skipped)
    plural = "" if total == 1 else "s"
    if not skipped:
        return f"{total} {noun}{plural}"
    return f"{len(kept)} of {total} {noun}{plural} ({len(skipped)} skipped)"
