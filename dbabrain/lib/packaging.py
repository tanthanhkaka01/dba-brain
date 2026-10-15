"""What to tell a reader to `pip install` when a driver is missing.

Every optional driver — pyodbc, oracledb, pg8000, PyMySQL, paramiko, pypsrp — is reported the same
way when it is absent: the feature does not work, and here is the one command that fixes it. That
command contains the **distribution name**, and hard-coding it was wrong for half the readers.

Until 0.29.0 the repository installed under one name and the published package under another, and a
hint naming the first **installed nothing for everyone else** - pip resolved a different project or
none at all, and the reader concluded the toolkit was broken rather than that it was missing an
extra. That is what a clean-room install found on 2026-08-23: ten OS metrics warning at once, each
naming a package that does not exist on PyPI. Both are `dbabrain` now.

The name is still asked for rather than assumed. ``importlib.metadata`` knows which distribution
provided the module that is running, and a hint built from it is right whatever a build is called.
"""
from __future__ import annotations

from functools import lru_cache

#: What to fall back to when nothing is installed — a source checkout run straight from the
#: working directory, which is the one case ``importlib.metadata`` cannot answer. The published
#: name is the better guess: a developer reading it knows their own checkout, while a user reading
#: the repository name has nowhere to go with it.
FALLBACK_DISTRIBUTION = "dbabrain"


@lru_cache(maxsize=1)
def distribution_name() -> str:
    """The distribution that provides this package, as pip would name it."""
    from importlib.metadata import packages_distributions

    try:
        providers = packages_distributions().get(__name__.split(".")[0], [])
    except Exception:  # noqa: BLE001 - a broken metadata directory must not break an error message
        providers = []
    return providers[0] if providers else FALLBACK_DISTRIBUTION


def install_hint(extra: str) -> str:
    """The `pip install` command that adds *extra*, quoted for a shell that eats brackets."""
    return f"pip install '{distribution_name()}[{extra}]'"
