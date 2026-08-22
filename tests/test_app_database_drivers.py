"""No app opens a database connection itself. One module owns that, and it is not an app.

``common/db_connect.py`` is the single source of truth for reaching a database: driver choice,
default port and database per engine, the connect timeout, and — the part a bypass always forgets —
a statement timeout enforced *inside the server*, because a socket that stays healthy while a
relation is locked is exactly the case a connect timeout does not cover.

Every bypass re-decides those four things locally and then drifts, silently, because a wrong
default only shows up on the one target that needed it. ``metrics/executor.py`` is what compliance
looks like: its own comment records that the file used to carry four near-identical connect
functions and now makes one call into ``common.db_connect``.

This rule was written down in ``docs/13_common.md`` and audited twice before it was tested. The
2026-08-06 audit named ``sql_tasks/runner.py``'s direct Oracle connect; the remediation pass closed
12 of 13 findings and deferred that one as "a behavioural change". Five days later the 2026-08-11
audit found it untouched, plus the SQL Server half of the same function pair and a third bypass in
``telegram/sql_commands.py``. A convention that survives two audits is not a convention anyone is
keeping — so it is a test now, and the allowlist is empty on purpose.

Note what this does *not* forbid: importing ``common.db_connect`` or ``common.sql_run``, which is
the whole point, and the ``sqlite3`` import that several apps use for its row/exception types
rather than to open anything.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


DB_OPS_ROOT = Path(__file__).resolve().parents[1] / "db_ops"

APPS = frozenset({
    "backup_restore", "control", "jobs", "metrics", "reports",
    "sla", "sql_tasks", "sre", "telegram", "webhost",
})

#: Drivers that open a connection to a *monitored* database. ``sqlite3`` is deliberately absent:
#: it is the runtime store's own driver, it ships with Python, and apps import it for
#: ``sqlite3.Row`` / ``sqlite3.OperationalError`` in type hints and except clauses. Store access is
#: a separate rule with its own owner (``db/backend.py``); this file is about target databases.
DATABASE_DRIVERS = frozenset({
    "pyodbc", "pymssql", "pg8000", "psycopg", "psycopg2", "oracledb", "cx_Oracle", "MySQLdb",
    "pymysql", "mysql",
})


def _module_files() -> list[Path]:
    return sorted(
        path for path in DB_OPS_ROOT.rglob("*.py")
        if "__pycache__" not in path.parts and _owner_of(path) in APPS
    )


def _owner_of(path: Path) -> str:
    parts = path.relative_to(DB_OPS_ROOT).parts
    return parts[0] if len(parts) > 1 else ""


def _relative(path: Path) -> str:
    return path.relative_to(DB_OPS_ROOT).as_posix()


def _imported_drivers(path: Path) -> list[str]:
    """Driver imports anywhere in the file — module level or inside a function.

    Inside a function is the form that matters: every bypass found so far imported its driver in
    the body of the function that connects, precisely so the module still imports on a node without
    that driver installed. A module-level-only check would have found none of them.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return sorted(found & DATABASE_DRIVERS)


@pytest.mark.parametrize("path", _module_files(), ids=_relative)
def test_an_app_never_imports_a_database_driver(path: Path) -> None:
    offenders = _imported_drivers(path)
    assert not offenders, (
        f"{_relative(path)} imports a database driver: {offenders}. "
        "Connecting to a database belongs to db_ops/common/db_connect.py (connect_engine), and "
        "running SQL on one target belongs to db_ops/common/sql_run.py (run_sql). An app that "
        "opens its own connection re-decides the driver, the default port and database, and both "
        "timeouts — and drifts from every other caller without anyone noticing."
    )


def test_the_owner_of_database_connections_is_still_the_one_module() -> None:
    """The rule above is only worth keeping while there is exactly one place that does connect.

    If a second module under ``common`` grows its own engine dispatch, the apps will be clean and
    the duplication will simply have moved down a layer — which is the failure this whole rule
    exists to prevent, one floor lower.
    """
    connectors = sorted(
        _relative(path) for path in (DB_OPS_ROOT / "common").rglob("*.py")
        if "__pycache__" not in path.parts and _imported_drivers(path)
    )

    assert connectors == ["common/db_connect.py", "common/sql_execution.py"], (
        f"Unexpected set of modules importing a database driver: {connectors}. "
        "common/db_connect.py owns engine dispatch and common/sql_execution.py owns the SQL Server "
        "driver/pymssql fallback it calls. A third entry means the engine knowledge has been "
        "copied again."
    )
