"""Carry the history a stand-in node recorded back into the shared store.

The situation this exists for: the worker is stopped and the estate is run from somewhere else —
a laptop, a fresh install, a stand-in node — whose store is somewhere nobody queries. The work is
real: metrics are collected, reports are built, SQL tasks run, alerts are delivered. But the
*record* of it lands out of the way, so the shared store shows a hole exactly as wide as the
outage, and every question asked of history afterwards ("how did this instance trend last week",
"was that SLA met") is answered from a series with a gap in it.

**The source is either a SQLite file or another schema on the same PostgreSQL server.** A stand-in
on its own SQLite file is the original case. A stand-in given its own *schema* is the same case
with a different container — proving a node before it writes where every other node's record lives
is the whole point of `TEST_MOVE_ESTATE_TO_FRESH_DBABRAIN_INSTALL.md` §3a, and a node that proved
itself still has history to bring home. Nothing below this paragraph knows which it was reading:
the carrying logic reads rows, drops the key, rewrites links and inserts, and the three places
that needed SQLite's catalog now ask `db_ops.db.backend`, which answers for both.

Measured after one eleven-hour stand-in run: 214,591 metric results, 14,215 job runs, 431 reports
and 3,660 SLA results — all of it about the production estate, none of it in the production store.

**Ids are never carried.** Every primary key here is an identity column and a SQLite store starts
numbering at 1, so its ids are already taken in a store with millions of rows. Rows are inserted
without their key and the new key is read back, which makes the *links* the problem:

* ``sla_results.sla_run_id`` is a real foreign key and would be **rejected**;
* ``metric_results.run_id`` and ``reports.telegram_send_message_id`` are not enforced, and would be
  silently **wrong** — pointing at whatever row in the destination happens to hold that number.

The second is worse than the first, because nothing reports it. So each parent is inserted first,
its old-to-new mapping is kept, and every child is rewritten through that mapping before it lands.

**Only what is missing.** The watermark is the destination's own newest row per table, read when
the work starts: a row crosses only if it is strictly newer. That makes the command re-runnable —
a second run against an unchanged source carries nothing — and it is why the plan is worth reading
before the apply.

``target_health`` is deliberately not carried. It is current state, rebuilt by the next metrics
run, not history; carrying it would describe the estate as the stand-in last saw it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Rows per ``executemany``. Large enough that 200,000 rows is minutes rather than hours, small
#: enough that one statement is not holding a lock on a table the daemon appends to.
BATCH_ROWS = 1000


@dataclass(frozen=True)
class TableSpec:
    """One table, and what has to be true before its rows can cross."""

    #: The table, by the same name in both stores.
    name: str
    #: The column that says when a row happened. The watermark is read from it.
    time_column: str
    #: Its identity primary key: dropped on the way in, read back on the way out.
    key: str
    #: ``{column: parent table}``. A value in ``column`` is an id in that parent's key and is
    #: rewritten through the parent's mapping. Enforced foreign key or not — a soft link pointing
    #: at the wrong parent is worse than one the database refuses, because nothing reports it.
    links: dict[str, str] = field(default_factory=dict)
    #: Keep this table's old-to-new mapping for its children to read.
    mapped: bool = False


#: Parents before children; a table nobody links to can go anywhere.
TABLES: tuple[TableSpec, ...] = (
    TableSpec("metric_runs", "started_at", "run_id", mapped=True),
    TableSpec("metric_results", "collected_at", "result_id", links={"run_id": "metric_runs"}),
    TableSpec("sla_runs", "started_at", "sla_run_id", mapped=True),
    TableSpec("sla_results", "collected_at", "sla_result_id", links={"sla_run_id": "sla_runs"}),
    TableSpec("telegram_send_messages", "row_ins_date", "send_tlgmsg_id", mapped=True),
    TableSpec("reports", "created_at", "report_id",
              links={"telegram_send_message_id": "telegram_send_messages"}),
    TableSpec("job_runs", "created_at", "log_id"),
    TableSpec("sql_runs", "created_at", "sql_run_id"),
)


class BackfillError(RuntimeError):
    """The source or the destination is not in a state where carrying rows is safe."""


@dataclass
class TablePlan:
    """What one table would contribute."""

    table: str
    watermark: str
    source_rows: int
    carried: int
    columns: tuple[str, ...]


def open_source(sqlite_path: str | Path) -> sqlite3.Connection:
    """The stand-in's SQLite store, read-only. It is never written by this command."""
    path = Path(sqlite_path)
    if not path.is_file():
        raise BackfillError(f"source store not found: {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


class _SchemaSource:
    """Another schema on the destination's own PostgreSQL server, read like a source store.

    Its own connection, with ``search_path`` pinned to the source schema, so the two ends are two
    connections and never one with a path that has to be flipped between statements. A flipped path
    is the failure that would be silent: the read and the write would both succeed and land in the
    same schema.

    Read-only is enforced on the session, not by convention — this command has no reason to write
    to the source, and a bug that made it try must fail rather than mutate a store somebody is
    still deciding whether to trust.
    """

    def __init__(self, store: Any, schema: str) -> None:
        self._store = store
        self.schema = str(schema)
        self._conn = None

    def __enter__(self):
        from dataclasses import replace as _replace

        from db_ops.db.backend import StoreTarget

        source_store = _replace(
            self._store.target.store,
            postgresql=_replace(self._store.target.store.postgresql, schema=self.schema,
                                explicit_connection_string=""),
        )
        target = StoreTarget(source_store, key=self._store.target.key,
                             password=self._store.target.password)
        self._conn = target.connect().__enter__()
        self._conn.execute("SET TRANSACTION READ ONLY")
        return self._conn

    def __exit__(self, *exc) -> None:
        if self._conn is not None:
            try:
                self._conn.__exit__(*exc)
            finally:
                self._conn = None

    def close(self) -> None:
        self.__exit__(None, None, None)


def _source_columns(conn: Any, table: str) -> list[str]:
    """Columns of *table* in whichever kind of store the source turned out to be."""
    return _destination_columns(conn, table)


def _destination_columns(conn: Any, table: str) -> list[str]:
    from db_ops.db import backend as backend_mod

    if isinstance(conn, backend_mod.PostgresConnection):
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = ? ORDER BY ordinal_position",
            (table,),
        ).fetchall()
        return [str(row["column_name"]) for row in rows]
    return [str(row["name"]) for row in conn.execute(f'PRAGMA table_info("{table}")')]


def _present(source: Any, conn: Any, spec: TableSpec) -> bool:
    """Do both stores have this table?

    A store whose SLA app never ran has no ``sla_runs``, and one built by an older release may not
    have a table a newer one adds. Neither is an error and neither is anything to carry: the tables
    here are owned by four different store classes, each of which creates its own the first time it
    is used. Skipping is the same judgement ``archive_old_job_runs`` makes about a console that was
    never opened.
    """
    from db_ops.db import backend as backend_mod

    # `sqlite_master` was asked directly here until the source could also be PostgreSQL. It is the
    # same question at both ends now, and `table_exists` is the one place that knows how to ask it
    # of either backend.
    return backend_mod.table_exists(source, spec.name) and backend_mod.table_exists(conn, spec.name)


def _nullable_columns(conn: Any, table: str) -> set[str]:
    """Which columns of *table* accept NULL in the destination.

    It decides what an unmappable link costs. `reports.telegram_send_message_id` is optional, so
    the report crosses without its message rather than not at all; `metric_results.run_id` is NOT
    NULL, so a result whose run predates the window cannot cross without being attached to some
    run - and attaching it to the wrong one is the failure this module exists to prevent. Those
    rows are left behind and counted, which is a small, stated loss rather than a silent lie.
    """
    from db_ops.db import backend as backend_mod

    if isinstance(conn, backend_mod.PostgresConnection):
        rows = conn.execute(
            "SELECT column_name, is_nullable FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = ?", (table,)).fetchall()
        return {str(r["column_name"]) for r in rows if str(r["is_nullable"]).upper() == "YES"}
    return {str(r["name"]) for r in conn.execute(f'PRAGMA table_info("{table}")')
            if not int(r["notnull"])}


def _watermark(conn: Any, spec: TableSpec) -> str:
    row = conn.execute(f"SELECT max({spec.time_column}) AS hi FROM {spec.name}").fetchone()
    return str(row["hi"] or "") if row is not None and row["hi"] is not None else ""


def _shared_columns(source: Any, conn: Any, spec: TableSpec) -> list[str]:
    """The columns both stores have, minus the key. Order differences do not matter: every
    statement names its columns, which is also why a store that gained a column later still
    works as either end."""
    destination = set(_destination_columns(conn, spec.name))
    return [name for name in _source_columns(source, spec.name)
            if name != spec.key and name in destination]


def _resolve_source(sqlite_path: str | Path | None, source_schema: str | None, store: Any):
    """The source, as something with `.execute` and `.close`, whichever kind was asked for.

    Exactly one of the two must be given. Accepting both would mean choosing one silently, and the
    one not chosen is the one the caller meant often enough to matter.
    """
    if (sqlite_path is None) == (source_schema is None):
        raise BackfillError(
            "give exactly one source: a SQLite path, or the name of another schema on this "
            "PostgreSQL server.")
    if sqlite_path is not None:
        return open_source(sqlite_path)

    from db_ops.db import backend as backend_mod

    if not isinstance(store.target.store.backend, str) or store.target.store.backend != "postgresql":
        raise BackfillError(
            "a schema source only makes sense when the destination is PostgreSQL; this store is "
            f"{store.target.store.backend}.")
    destination_schema = store.target.store.postgresql.schema or "public"
    if source_schema == destination_schema:
        raise BackfillError(
            f"source and destination are the same schema ({source_schema}). Nothing would be "
            f"carried, and a watermark read from the table being written is not a window.")
    holder = _SchemaSource(store, source_schema)
    conn = holder.__enter__()
    conn._backfill_holder = holder  # noqa: SLF001 - kept so the caller can close the session
    assert backend_mod is not None
    return conn


def _close_source(source: Any) -> None:
    holder = getattr(source, "_backfill_holder", None)
    (holder or source).close()


def plan(*, sqlite_path: str | Path | None = None, store: Any,
         source_schema: str | None = None) -> list[TablePlan]:
    """What would cross, per table, and from which point. Reads both stores and writes nothing."""
    source = _resolve_source(sqlite_path, source_schema, store)
    plans: list[TablePlan] = []
    try:
        with store.connect() as conn:
            for spec in TABLES:
                if not _present(source, conn, spec):
                    plans.append(TablePlan(spec.name, "", 0, 0, ()))
                    continue
                watermark = _watermark(conn, spec)
                columns = tuple(_shared_columns(source, conn, spec))
                total = source.execute(f"SELECT count(*) AS n FROM {spec.name}").fetchone()["n"]
                carried = source.execute(
                    f"SELECT count(*) AS n FROM {spec.name} WHERE {spec.time_column} > ?",
                    (watermark,),
                ).fetchone()["n"]
                plans.append(TablePlan(spec.name, watermark, int(total), int(carried), columns))
    finally:
        _close_source(source)
    return plans


def apply(*, sqlite_path: str | Path | None = None, store: Any, progress: Any = None,
          source_schema: str | None = None) -> dict[str, Any]:
    """Carry the missing rows. Returns what was inserted, per table.

    One transaction per table, not one for the whole run: a 200,000-row insert held open across
    every table would lock the tables the daemon appends to for the length of the slowest one, and
    an interrupted run would roll back work that was already correct. Per table, an interruption
    leaves the destination consistent and the next run carries what is still missing.
    """
    source = _resolve_source(sqlite_path, source_schema, store)
    mappings: dict[str, dict[int, int]] = {}
    inserted: dict[str, int] = {}
    unlinked: dict[str, int] = {}
    left_behind: dict[str, int] = {}
    try:
        for spec in TABLES:
            with store.connect() as conn:
                if not _present(source, conn, spec):
                    inserted[spec.name] = 0
                    if spec.mapped:
                        mappings[spec.name] = {}
                    continue
                watermark = _watermark(conn, spec)
                columns = _shared_columns(source, conn, spec)
                if not columns:
                    raise BackfillError(f"{spec.name}: the two stores share no columns.")
                rows = source.execute(
                    f"SELECT {spec.key}, {', '.join(columns)} FROM {spec.name} "
                    f"WHERE {spec.time_column} > ? ORDER BY {spec.key}",
                    (watermark,),
                ).fetchall()
                inserted[spec.name] = 0
                if not rows:
                    if spec.mapped:
                        mappings[spec.name] = {}
                    continue
                statement = (f"INSERT INTO {spec.name} ({', '.join(columns)}) "
                             f"VALUES ({', '.join('?' for _ in columns)})")
                nullable = _nullable_columns(conn, spec.name)
                mapping, lost, skipped, batch = {}, 0, 0, []
                for row in rows:
                    values, dropped, carryable = _row_values(row, columns, spec, mappings, nullable)
                    if not carryable:
                        skipped += 1
                        continue
                    lost += dropped
                    if spec.mapped:
                        # Row at a time, because the new key has to come back with the row it
                        # belongs to. Only the three parents pay this; the large tables are
                        # batched below.
                        cursor = conn.execute(f"{statement} RETURNING {spec.key}", tuple(values))
                        new_row = cursor.fetchone()
                        if new_row is not None:
                            mapping[int(row[spec.key])] = int(new_row[spec.key])
                    else:
                        batch.append(tuple(values))
                        if len(batch) >= BATCH_ROWS:
                            conn.executemany(statement, batch)
                            batch = []
                if batch:
                    conn.executemany(statement, batch)
                if spec.mapped:
                    mappings[spec.name] = mapping
                inserted[spec.name] = len(rows) - skipped
                if lost:
                    unlinked[spec.name] = lost
                if skipped:
                    left_behind[spec.name] = skipped
            if progress is not None:
                progress(spec.name, inserted[spec.name], unlinked.get(spec.name, 0))
    finally:
        _close_source(source)
    return {"inserted": inserted, "unlinked": unlinked, "left_behind": left_behind,
            "total": sum(inserted.values())}


def _row_values(row: Any, columns: list[str], spec: TableSpec,
                mappings: dict[str, dict[int, int]],
                nullable: set[str]) -> tuple[list[Any], int, bool]:
    """One row's values, with every link rewritten through its parent's mapping.

    A link whose parent is not in the mapping is set to NULL and counted. That happens when the
    parent is older than the window and therefore already in the destination under an id this run
    never saw: carrying the source's number would point the row at an unrelated parent, which is
    the failure this whole module is arranged to prevent. Dropping the link loses less than
    inventing one.
    """
    values: list[Any] = []
    dropped = 0
    for name in columns:
        value = row[name]
        parent = spec.links.get(name)
        if parent is not None and value is not None:
            replacement = mappings.get(parent, {}).get(int(value))
            if replacement is None:
                if name not in nullable:
                    # The column will not take NULL, so this row cannot cross without being
                    # attached to a parent - and the only parent available is the wrong one.
                    return values, dropped, False
                dropped += 1
                value = None
            else:
                value = replacement
        values.append(value)
    return values, dropped, True
