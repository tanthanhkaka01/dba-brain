"""Carrying a stand-in's history home when the stand-in was given a schema, not a file.

`TEST_MOVE_ESTATE_TO_FRESH_DBABRAIN_INSTALL.md` §3a says a moved node runs on a store of its own
before it writes where every other node's record lives — so that its first hours are disposable.
That store used to be a local SQLite file. It can equally be its own PostgreSQL *schema*, which is
the same idea in a different container, and a node that has proved itself still has history to
bring home.

These tests are about the **source selection and its refusals**. The carrying itself — dropping
identity keys, rewriting every child link through its parent's new mapping, the watermark window —
is unchanged and covered by `test_backfill_from_sqlite.py`; the whole point of putting both sources
through one command is that there is one copy of those rules to be right about.

The PostgreSQL path needs a server, so what is asserted here is everything that can be decided
without one: which source was chosen, and every way of asking for a source that must be refused.
"""

from pathlib import Path

import pytest

from db_ops.db import backfill


class _FakePostgres:
    """The shape `_resolve_source` reads off a store: backend name and destination schema."""

    class _Target:
        def __init__(self, schema: str) -> None:
            self.key = None
            self.password = None
            self.store = _FakePostgres._Store(schema)

    class _Store:
        def __init__(self, schema: str) -> None:
            self.backend = "postgresql"
            self.postgresql = _FakePostgres._Pg(schema)

    class _Pg:
        def __init__(self, schema: str) -> None:
            self.schema = schema

    def __init__(self, schema: str = "db_ops") -> None:
        self.target = _FakePostgres._Target(schema)


class _FakeSqlite:
    class _Target:
        def __init__(self) -> None:
            self.key = None
            self.password = None
            self.store = _FakeSqlite._Store()

    class _Store:
        backend = "sqlite"
        postgresql = None

    def __init__(self) -> None:
        self.target = _FakeSqlite._Target()


def test_exactly_one_source_is_required() -> None:
    """Neither is a caller who has not said where the rows come from; both is a caller whose two
    answers disagree. Choosing one silently would pick the wrong one often enough to matter."""
    with pytest.raises(backfill.BackfillError, match="exactly one source"):
        backfill._resolve_source(None, None, _FakePostgres())

    with pytest.raises(backfill.BackfillError, match="exactly one source"):
        backfill._resolve_source("some.sqlite", "dba_brain", _FakePostgres())


def test_carrying_a_schema_into_itself_is_refused() -> None:
    """The failure that would otherwise be quiet and destructive.

    The watermark is read from the destination. Point both ends at one schema and the watermark
    comes from the very table being written, so the window is meaningless and the run would append
    rows to the table it just read them from.
    """
    with pytest.raises(backfill.BackfillError, match="same schema"):
        backfill._resolve_source(None, "db_ops", _FakePostgres(schema="db_ops"))


def test_a_schema_source_needs_a_postgresql_destination() -> None:
    """"Another schema on this server" means nothing when the store is a file. Said plainly rather
    than failing later inside a connection attempt nobody asked for."""
    with pytest.raises(backfill.BackfillError, match="destination is PostgreSQL"):
        backfill._resolve_source(None, "dba_brain", _FakeSqlite())


def test_a_missing_sqlite_source_is_named_by_path(tmp_path: Path) -> None:
    """Unchanged behaviour, asserted here because the selection now runs before it."""
    missing = tmp_path / "not-there.sqlite"
    with pytest.raises(backfill.BackfillError, match="source store not found"):
        backfill._resolve_source(missing, None, _FakeSqlite())


def test_a_sqlite_source_still_opens_read_only(tmp_path: Path) -> None:
    """The SQLite half of the same promise the schema half makes with SET TRANSACTION READ ONLY:
    this command never writes to the store it is reading."""
    import sqlite3

    path = tmp_path / "source.sqlite"
    sqlite3.connect(path).execute("CREATE TABLE job_runs (log_id INTEGER PRIMARY KEY)")

    source = backfill._resolve_source(path, None, _FakeSqlite())
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            source.execute("CREATE TABLE zz_probe (i INTEGER)")
    finally:
        backfill._close_source(source)


def test_closing_a_source_works_for_either_kind(tmp_path: Path) -> None:
    """`_close_source` is what the `finally` in plan and apply calls, and a schema source hides a
    session holder behind the connection. Closing the wrong one leaks a PostgreSQL session per run."""
    import sqlite3

    path = tmp_path / "source.sqlite"
    sqlite3.connect(path).execute("CREATE TABLE job_runs (log_id INTEGER PRIMARY KEY)")
    source = backfill._resolve_source(path, None, _FakeSqlite())
    backfill._close_source(source)

    with pytest.raises(sqlite3.ProgrammingError):
        source.execute("SELECT 1")


def test_both_sources_reach_the_same_carrying_rules() -> None:
    """The reason this is one command and not two.

    `TABLES` carries the id-remapping rules — which parents are mapped, which children are rewritten
    through them, and which link is a real foreign key. A second command with its own copy would
    drift, and the drift would show up as measurements attached to somebody else's collection run,
    which nothing reports.
    """
    mapped = {spec.name for spec in backfill.TABLES if spec.mapped}
    linked = {spec.name: spec.links for spec in backfill.TABLES if spec.links}

    assert mapped == {"metric_runs", "sla_runs", "telegram_send_messages"}
    assert linked["metric_results"] == {"run_id": "metric_runs"}
    assert linked["sla_results"] == {"sla_run_id": "sla_runs"}
    assert linked["reports"] == {"telegram_send_message_id": "telegram_send_messages"}
