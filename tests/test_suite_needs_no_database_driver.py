"""The test suite must import with no database driver installed.

That is the claim `[project.optional-dependencies]` makes: `pip install dbabrain` brings
`cryptography` and `PyYAML` and nothing else, and a driver arrives only when somebody asks for
`[mssql]`, `[oracle]`, `[postgres]` or `[mysql]`. Every driver in the source is imported inside the
function that needs it, so a PostgreSQL-only install never touches an ODBC driver.

**The suite broke that claim and nothing noticed for months.** `tests/test_sqlserver.py` opened with
`import pyodbc` at module scope, so on a machine without the driver pytest could not *collect* — it
exited 2 before running a single test. It passed everywhere it was ever run because a developer's
virtualenv has the drivers; the first environment that did not was CI, on the day CI first existed.

The file itself was not a test. It had no `def test_`, no assertion, and a hardcoded connection
string: a scratch "can I connect" script that pytest picked up because of its name. It is deleted,
and `db-ops common run-sql` is the supported way to ask that question — a real command, with tests,
that takes its target from configuration instead of a literal.

So this guard is about the class, not the file. It is cheap, it is static, and it fails in the
place where the mistake is made rather than in a CI log a week later.
"""

from __future__ import annotations

import ast
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parent

#: Every distribution behind an extra. Importing one at module scope makes the whole file - and
#: therefore the whole collection - depend on an optional install.
DRIVERS: frozenset[str] = frozenset({
    "pyodbc", "pymssql",        # [mssql]
    "oracledb", "cx_Oracle",    # [oracle]
    "pg8000", "psycopg", "psycopg2",  # [postgres]
    "pymysql", "mysql",         # [mysql]
    "paramiko",                 # [ssh]
    "pypsrp", "winrm",          # [winrm]
})

#: Test files allowed to import a driver at module scope, and why. Empty, and it should stay that
#: way: `pytest.importorskip("pyodbc")` gives a file the same access with a clean skip instead of a
#: collection error, so an entry here needs an argument for why that is not enough.
ALLOWED: dict[str, str] = {}


def _module_level_driver_imports(path: Path) -> list[str]:
    """Drivers this file imports at module scope. A lazy import inside a function is fine."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return []

    found: list[str] = []
    # `tree.body` only: an import inside a function or a fixture costs nothing until it is called,
    # which is the whole pattern the source uses for drivers.
    for node in tree.body:
        names: list[str] = []
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.append(node.module)
        elif isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        for name in names:
            root = name.split(".")[0]
            if root in DRIVERS:
                found.append(root)
    return found


def test_no_test_module_imports_a_database_driver_at_module_scope() -> None:
    offenders: list[str] = []
    for path in sorted(TESTS_ROOT.rglob("test_*.py")):
        if path.name in ALLOWED:
            continue
        for driver in _module_level_driver_imports(path):
            offenders.append(f"{path.name}: {driver}")

    assert not offenders, (
        "These test modules import a database driver at module scope, so pytest cannot collect "
        "them on an install without that optional extra - it exits 2 before running anything, and "
        "the failure names the driver rather than the claim it broke. Move the import inside the "
        "test, or use pytest.importorskip:\n  " + "\n  ".join(offenders)
    )


def test_conftest_imports_no_driver_either() -> None:
    """`conftest.py` is worse than a test file: it takes the whole directory down with it."""
    for conftest in sorted(TESTS_ROOT.rglob("conftest.py")):
        drivers = _module_level_driver_imports(conftest)
        assert not drivers, (
            f"{conftest.name} imports {', '.join(drivers)} at module scope. Shared machinery is "
            "loaded before every test in its directory, so this makes the whole suite need an "
            "optional install."
        )


def test_the_sweep_reads_the_whole_suite() -> None:
    """A walk that silently matched nothing would make the guard pass by finding nothing."""
    files = list(TESTS_ROOT.rglob("test_*.py"))

    assert len(files) > 100, f"only {len(files)} test modules found; the sweep is broken"


def test_every_exception_is_still_earned() -> None:
    """An exception that stops being needed must be deleted, not left as folklore."""
    for name, reason in ALLOWED.items():
        path = TESTS_ROOT / name
        assert path.exists(), f"ALLOWED names a file that is gone: {name}"
        assert _module_level_driver_imports(path), (
            f"{name} no longer imports a driver at module scope, so it should lose its "
            f"exception ({reason})"
        )
