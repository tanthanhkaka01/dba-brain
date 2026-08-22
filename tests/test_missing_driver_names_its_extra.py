"""A missing database driver has to say which install brings it, not merely which module it is.

Every driver here is imported inside the function that needs it, which is right: a PostgreSQL-only
operator should never be made to install an ODBC driver and an Oracle client to start. The cost of
that choice is that "it does not work" arrives as an `ImportError` at the moment of connecting,
far from the install that could have prevented it.

So the message carries the fix. `pyodbc is required to connect to SQL Server` tells a DBA what is
missing and leaves them to work out which project provides it and under what name; the extras in
`pyproject.toml` mean there is an exact answer — `pip install 'db_ops[mssql]'` — and printing it
turns a dead end into one command.

The guard below is the part that keeps being true: extras get renamed and added, and a message
naming an extra that no longer exists is worse than one naming none, because it sends the reader
to a command that fails.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE = REPO_ROOT / "db_ops"

#: Every extra a message is allowed to name.
DECLARED_EXTRAS = set(
    tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    ["project"]["optional-dependencies"]
)

EXTRA_IN_MESSAGE = re.compile(r"db_ops\[([a-z]+)\]")


def _messages() -> list[tuple[str, int, str]]:
    found = []
    for path in sorted(PACKAGE.rglob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if "is required" in line and "pip install" not in line:
                continue
            for match in EXTRA_IN_MESSAGE.finditer(line):
                found.append((path.relative_to(REPO_ROOT).as_posix(), number, match.group(1)))
    return found


def test_every_extra_a_message_names_is_one_that_exists() -> None:
    unknown = [
        f"{name}:{number} names db_ops[{extra}]"
        for name, number, extra in _messages()
        if extra not in DECLARED_EXTRAS
    ]

    assert not unknown, (
        "These send the reader to an install command that will fail, because the extra is not "
        f"declared in pyproject.toml (declared: {sorted(DECLARED_EXTRAS)}):\n  "
        + "\n  ".join(unknown)
    )


def test_every_engine_driver_message_offers_an_install() -> None:
    """Naming the module alone leaves the reader to guess the distribution and the extra.

    Only what is actually *raised* counts. A comment explaining a driver quirk and a docstring
    mentioning one in passing are not messages to anybody, and demanding that they advertise an
    install command would be nonsense — the guard has to know the difference or it becomes noise
    people learn to override.
    """
    drivers = ("pyodbc", "pg8000", "oracledb", "pymysql", "paramiko")
    silent = []
    for path in sorted(PACKAGE.rglob("*.py")):
        lines = path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if "is required" not in line or not any(d in line for d in drivers):
                continue
            if line.lstrip().startswith("#"):
                continue
            statement = "\n".join(lines[max(0, index - 3):index + 4])
            if "raise " not in statement:
                continue
            if "pip install" not in statement:
                silent.append(
                    f"{path.relative_to(REPO_ROOT).as_posix()}:{index + 1}: {line.strip()}")

    assert not silent, (
        "A driver is missing and the message does not say how to get it:\n  " + "\n  ".join(silent)
    )


def test_the_extras_cover_every_engine_the_toolkit_claims() -> None:
    """The four engines in the README have to be installable by name, or the claim is empty."""
    for engine in ("mssql", "oracle", "postgres", "mysql"):
        assert engine in DECLARED_EXTRAS, f"no extra installs the {engine} driver"
