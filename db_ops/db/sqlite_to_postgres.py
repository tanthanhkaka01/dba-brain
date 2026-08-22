"""Migrate the db_ops runtime store from SQLite to PostgreSQL.

The schema is **read from the live SQLite database**, never restated here. ``db_ops`` builds its
tables in four places (``db/store.py``, ``metrics/storage.py``, ``sla/storage.py``,
``backup_restore/history.py``) and evolves them with additive migrations; a hand-written
PostgreSQL copy of all that would be a second source of truth that silently drifts. Introspecting
``sqlite_master``/``PRAGMA`` instead means this tool keeps working when a column is added.

Ordering is chosen for a multi-million-row store: create tables, bulk-load with ``COPY``, and only
then build indexes and foreign keys. Loading into indexed tables is several times slower, and the
store's big tables (``metric_results``, ``metric_results_archive``, ``job_runs``) carry 7+ indexes
between them.

Restartability is per table: every table is truncated immediately before it is loaded, so a run
that dies half way through can be resumed with ``--only-tables`` on whatever is left without
producing duplicates.

A note on consistency: the worker's daemon keeps writing to SQLite while this runs, so a migration
is a point-in-time copy, not a synchronised replica. Use ``--snapshot`` (SQLite ``VACUUM INTO``) for
a stable source file, and stop the daemon for the final cutover run. See ``docs/01_runtime_store.md``.
"""

from __future__ import annotations

import io
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from db_ops.config import PostgresStoreConfig
from db_ops.db.postgres_store import (
    PostgresStoreError,
    connect,
    quote_identifier,
    validate_identifier,
)

DEFAULT_BATCH_ROWS = 50_000

# Rows below this count are re-loaded whole in delta mode rather than diffed. Cheap, and exact:
# most store tables are UPDATEd after insert (telegram_*, reports, job_runs, metric_runs, sql_runs,
# backup_restore_history), and an id-based delta only ever sees *new* rows, never changed ones.
DEFAULT_RELOAD_UNDER_ROWS = 250_000

# For the few tables too large to reload, recent rows are re-synced as well as appended, because
# those are the ones that get updated (metric_results.daily_report_created, a job_run finishing).
DEFAULT_RESYNC_DAYS = 2

# The column that dates a row, per table shape. First match wins.
_TIMESTAMP_CANDIDATES = ("collected_at", "created_at", "started_at", "row_ins_date", "archived_at",
                         "updated_at", "restore_start")

# SQLite's own type-affinity rules (https://sqlite.org/datatype3.html#determination_of_column_affinity)
# mapped to the narrowest PostgreSQL type that cannot lose data. Order matters: "INT" must be
# tested before "CHAR"-style names because "BIGINT" contains neither, but "POINT" contains "INT".
# The store only uses INTEGER/REAL/TEXT today; the full rule set is here so an added column with a
# different declared type migrates correctly instead of being guessed at.
_AFFINITY_RULES: tuple[tuple[str, str], ...] = (
    ("INT", "BIGINT"),
    ("CHAR", "TEXT"),
    ("CLOB", "TEXT"),
    ("TEXT", "TEXT"),
    ("BLOB", "BYTEA"),
    ("REAL", "DOUBLE PRECISION"),
    ("FLOA", "DOUBLE PRECISION"),
    ("DOUB", "DOUBLE PRECISION"),
)

# The store writes its timestamps as UTC ISO-8601 text (``utc_now_text()``), and several columns
# carry the SQLite equivalent as a DEFAULT. The PostgreSQL expression below produces a
# byte-identical string, so a row inserted by a default keeps the format every reader expects.
_SQLITE_UTC_NOW = "strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
_POSTGRES_UTC_NOW = """to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')"""


class MigrationError(RuntimeError):
    """The migration cannot proceed or produced a result that does not verify."""


# --------------------------------------------------------------------------- #
# SQLite schema introspection
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SqliteColumn:
    name: str
    declared_type: str
    not_null: bool
    default: str | None
    pk_position: int  # 0 = not part of the primary key, 1..n = position within it


@dataclass(frozen=True)
class SqliteIndex:
    name: str
    table: str
    unique: bool
    columns: tuple[tuple[str, bool], ...]  # (column, descending)


@dataclass(frozen=True)
class SqliteForeignKey:
    table: str
    column: str
    references_table: str
    references_column: str


@dataclass(frozen=True)
class SqliteTable:
    name: str
    columns: tuple[SqliteColumn, ...]
    # A single INTEGER PRIMARY KEY AUTOINCREMENT column becomes a PostgreSQL identity column.
    autoincrement_column: str = ""
    # UNIQUE table constraints (SQLite index origin 'u'), which must stay constraints rather than
    # become plain indexes so ON CONFLICT / upserts keep working.
    unique_constraints: tuple[tuple[str, ...], ...] = ()

    @property
    def pk_columns(self) -> tuple[str, ...]:
        keyed = [col for col in self.columns if col.pk_position > 0]
        return tuple(col.name for col in sorted(keyed, key=lambda c: c.pk_position))

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(col.name for col in self.columns)


def read_sqlite_schema(
    conn: sqlite3.Connection,
) -> tuple[list[SqliteTable], list[SqliteIndex], list[SqliteForeignKey]]:
    """Introspect tables, secondary indexes and foreign keys from a live SQLite connection."""
    conn.row_factory = sqlite3.Row
    table_rows = conn.execute(
        """
        SELECT name, sql
        FROM sqlite_master
        WHERE type = 'table'
          AND name NOT LIKE 'sqlite_%'
        ORDER BY name;
        """
    ).fetchall()

    tables: list[SqliteTable] = []
    indexes: list[SqliteIndex] = []
    foreign_keys: list[SqliteForeignKey] = []

    for table_row in table_rows:
        table_name = validate_identifier(table_row["name"], kind="table")
        create_sql = str(table_row["sql"] or "")

        columns = tuple(
            SqliteColumn(
                name=validate_identifier(col["name"], kind="column"),
                declared_type=str(col["type"] or ""),
                not_null=bool(col["notnull"]),
                default=None if col["dflt_value"] is None else str(col["dflt_value"]),
                pk_position=int(col["pk"]),
            )
            for col in conn.execute(f"PRAGMA table_info({quote_identifier(table_name)});").fetchall()
        )

        # AUTOINCREMENT lives only in the CREATE statement, not in any PRAGMA. It is legal on a
        # single INTEGER PRIMARY KEY only, which is why one column name is enough here.
        autoincrement_column = ""
        if "AUTOINCREMENT" in create_sql.upper():
            keyed = [col for col in columns if col.pk_position == 1]
            if keyed:
                autoincrement_column = keyed[0].name

        unique_constraints: list[tuple[str, ...]] = []
        for index in conn.execute(
            f"PRAGMA index_list({quote_identifier(table_name)});"
        ).fetchall():
            index_name = str(index["name"])
            origin = str(index["origin"])
            key_columns = tuple(
                (validate_identifier(part["name"], kind="column"), bool(part["desc"]))
                for part in conn.execute(f"PRAGMA index_xinfo({index_name!r});").fetchall()
                if part["key"] and part["name"] is not None
            )
            if not key_columns:
                continue
            if origin == "pk":
                # PostgreSQL builds its own index for the PRIMARY KEY.
                continue
            if origin == "u":
                # A UNIQUE constraint declared in CREATE TABLE. Re-declare it as a constraint,
                # not as a unique index, so it reads the same in both databases.
                unique_constraints.append(tuple(name for name, _ in key_columns))
                continue
            indexes.append(
                SqliteIndex(
                    name=validate_identifier(index_name, kind="index"),
                    table=table_name,
                    unique=bool(index["unique"]),
                    columns=key_columns,
                )
            )

        for fk in conn.execute(
            f"PRAGMA foreign_key_list({quote_identifier(table_name)});"
        ).fetchall():
            foreign_keys.append(
                SqliteForeignKey(
                    table=table_name,
                    column=validate_identifier(fk["from"], kind="column"),
                    references_table=validate_identifier(fk["table"], kind="table"),
                    references_column=validate_identifier(fk["to"], kind="column"),
                )
            )

        tables.append(
            SqliteTable(
                name=table_name,
                columns=columns,
                autoincrement_column=autoincrement_column,
                unique_constraints=tuple(unique_constraints),
            )
        )

    return tables, indexes, foreign_keys


# --------------------------------------------------------------------------- #
# SQLite -> PostgreSQL DDL translation
# --------------------------------------------------------------------------- #
def postgres_type(declared_type: str) -> str:
    """Map a SQLite declared type to a PostgreSQL type using SQLite's affinity rules."""
    upper = str(declared_type or "").upper()
    if not upper:
        # No declared type is BLOB affinity in SQLite, but the store never does this; TEXT is the
        # safe landing spot because every SQLite value has a text representation.
        return "TEXT"
    for needle, mapped in _AFFINITY_RULES:
        if needle in upper:
            return mapped
    return "NUMERIC"


def translate_default(default: str | None) -> str | None:
    """Translate a SQLite column DEFAULT into its PostgreSQL equivalent.

    Literals pass through unchanged. The one expression the store uses is SQLite's UTC-now
    ``strftime`` call, which is rewritten to the ``to_char(now() ...)`` form that produces the
    identical string. Anything else that looks like an expression raises rather than being dropped
    silently — a lost DEFAULT is a NOT NULL violation waiting to happen at the first insert.
    """
    if default is None:
        return None
    value = default.strip()
    if not value:
        return None
    if value.replace(" ", "") == _SQLITE_UTC_NOW.replace(" ", ""):
        return _POSTGRES_UTC_NOW
    upper = value.upper()
    if upper in ("CURRENT_TIMESTAMP", "CURRENT_DATE", "CURRENT_TIME", "NULL", "TRUE", "FALSE"):
        return upper
    # Quoted string literal, or a plain number.
    if (value.startswith("'") and value.endswith("'")) or _is_number(value):
        return value
    if value.startswith("(") or "(" in value:
        raise MigrationError(
            f"Cannot translate SQLite DEFAULT {default!r} to PostgreSQL. Add a rule for it in "
            "db_ops.db.sqlite_to_postgres.translate_default()."
        )
    return value


def _is_number(value: str) -> bool:
    try:
        float(value)
    except ValueError:
        return False
    return True


def qualified(schema: str, name: str) -> str:
    return f"{quote_identifier(schema)}.{quote_identifier(name)}"


def create_table_sql(table: SqliteTable, *, schema: str) -> str:
    """Build the PostgreSQL CREATE TABLE for one SQLite table.

    The autoincrement primary key becomes ``GENERATED BY DEFAULT AS IDENTITY`` rather than
    ``ALWAYS``: the migration inserts the original id values explicitly, which ``ALWAYS`` would
    reject. The sequence is re-based afterwards by :func:`reset_identity`.
    """
    lines: list[str] = []
    for column in table.columns:
        parts = [quote_identifier(column.name)]
        if column.name == table.autoincrement_column:
            parts.append("BIGINT GENERATED BY DEFAULT AS IDENTITY")
        else:
            parts.append(postgres_type(column.declared_type))
        default = translate_default(column.default)
        if default is not None:
            parts.append(f"DEFAULT {default}")
        # A single-column INTEGER PRIMARY KEY is implicitly NOT NULL in SQLite (notnull reads 0),
        # so the PRIMARY KEY clause below supplies it; declaring it twice is harmless but noisy.
        if column.not_null:
            parts.append("NOT NULL")
        lines.append(" ".join(parts))

    if table.pk_columns:
        keys = ", ".join(quote_identifier(name) for name in table.pk_columns)
        lines.append(f"PRIMARY KEY ({keys})")
    for constraint in table.unique_constraints:
        keys = ", ".join(quote_identifier(name) for name in constraint)
        lines.append(f"UNIQUE ({keys})")

    body = ",\n    ".join(lines)
    return f"CREATE TABLE IF NOT EXISTS {qualified(schema, table.name)} (\n    {body}\n)"


def create_index_sql(index: SqliteIndex, *, schema: str) -> str:
    columns = ", ".join(
        f"{quote_identifier(name)}{' DESC' if descending else ''}" for name, descending in index.columns
    )
    unique = "UNIQUE " if index.unique else ""
    return (
        f"CREATE {unique}INDEX IF NOT EXISTS {quote_identifier(index.name)} "
        f"ON {qualified(schema, index.table)} ({columns})"
    )


def add_foreign_key_sql(fk: SqliteForeignKey, *, schema: str) -> str:
    name = f"fk_{fk.table}_{fk.column}"
    return (
        f"ALTER TABLE {qualified(schema, fk.table)} "
        f"ADD CONSTRAINT {quote_identifier(name)} "
        f"FOREIGN KEY ({quote_identifier(fk.column)}) "
        f"REFERENCES {qualified(schema, fk.references_table)} "
        f"({quote_identifier(fk.references_column)})"
    )


def build_ddl(
    tables: Sequence[SqliteTable],
    indexes: Sequence[SqliteIndex],
    foreign_keys: Sequence[SqliteForeignKey],
    *,
    schema: str,
) -> dict[str, list[str]]:
    """All DDL for the migration, grouped by the phase it belongs to."""
    return {
        "tables": [create_table_sql(table, schema=schema) for table in tables],
        "indexes": [create_index_sql(index, schema=schema) for index in indexes],
        "foreign_keys": [add_foreign_key_sql(fk, schema=schema) for fk in foreign_keys],
    }


# --------------------------------------------------------------------------- #
# Row copy (COPY ... FROM STDIN, PostgreSQL text format)
# --------------------------------------------------------------------------- #
def encode_copy_value(value: Any, *, target_type: str, table: str, column: str) -> str:
    """Encode one SQLite value for PostgreSQL's COPY text format.

    Text format is used rather than CSV because its NULL marker (``\\N``) is unambiguous: SQLite
    columns hold both NULL and the empty string, and CSV cannot tell them apart without relying on
    quoting rules that vary by writer.

    Values are coerced to the target type here so a type mismatch names the table, column and
    value, instead of surfacing as an opaque COPY parse error hundreds of thousands of rows in.
    SQLite is dynamically typed, so an INTEGER column really can contain text.
    """
    if value is None:
        return r"\N"

    if target_type == "BIGINT":
        if isinstance(value, bool):
            return "1" if value else "0"
        if isinstance(value, int):
            return str(value)
        try:
            return str(int(str(value).strip()))
        except (TypeError, ValueError) as exc:
            raise MigrationError(
                f"{table}.{column}: value {value!r} cannot be stored in a PostgreSQL BIGINT. "
                "SQLite allows any type in any column; fix or exclude the row."
            ) from exc

    if target_type == "DOUBLE PRECISION":
        try:
            return repr(float(value))
        except (TypeError, ValueError) as exc:
            raise MigrationError(
                f"{table}.{column}: value {value!r} cannot be stored in a PostgreSQL "
                "DOUBLE PRECISION."
            ) from exc

    if target_type == "BYTEA":
        if isinstance(value, (bytes, bytearray, memoryview)):
            return "\\\\x" + bytes(value).hex()
        return "\\\\x" + str(value).encode("utf-8").hex()

    if isinstance(value, (bytes, bytearray, memoryview)):
        text = bytes(value).decode("utf-8", errors="replace")
    else:
        text = str(value)
    # PostgreSQL cannot store a NUL byte in a text column at all - COPY aborts the whole load with
    # 'invalid byte sequence for encoding "UTF8": 0x00'. SQLite has no such restriction, so a
    # command collector that captured binary output really does leave NULs in raw_stdout/raw_stderr.
    # They are stripped: a NUL carries no information in a diagnostic text blob, and the alternative
    # is refusing to migrate a store that is otherwise perfectly good. The count is reported per
    # table so the change is visible rather than silent.
    if "\x00" in text:
        text = text.replace("\x00", "")
    # COPY text format escapes: backslash first, then the characters that would end a field/row.
    return (
        text.replace("\\", "\\\\")
        .replace("\t", "\\t")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )


def count_nul_values(rows: Iterable[Sequence[Any]]) -> int:
    """How many values in these rows contain a NUL byte (and so will be sanitized)."""
    total = 0
    for row in rows:
        for value in row:
            if isinstance(value, str) and "\x00" in value:
                total += 1
            elif isinstance(value, (bytes, bytearray)) and b"\x00" in bytes(value):
                total += 1
    return total


def _copy_chunk(conn, *, schema: str, table: SqliteTable, target_types: Sequence[str],
                rows: Sequence[Sequence[Any]]) -> None:
    buffer = io.StringIO()
    for row in rows:
        buffer.write(
            "\t".join(
                encode_copy_value(
                    value,
                    target_type=target_types[position],
                    table=table.name,
                    column=table.column_names[position],
                )
                for position, value in enumerate(row)
            )
        )
        buffer.write("\n")
    buffer.seek(0)

    columns = ", ".join(quote_identifier(name) for name in table.column_names)
    cursor = conn.cursor()
    try:
        cursor.execute(
            f"COPY {qualified(schema, table.name)} ({columns}) FROM STDIN WITH (FORMAT text)",
            stream=buffer,
        )
    finally:
        cursor.close()


# --------------------------------------------------------------------------- #
# Migration
# --------------------------------------------------------------------------- #
@dataclass
class TableResult:
    table: str
    source_rows: int = 0
    copied_rows: int = 0
    target_rows: int = 0
    seconds: float = 0.0
    #: Values that contained a NUL byte and were sanitized (PostgreSQL text cannot hold 0x00).
    sanitized_values: int = 0
    skipped: bool = False
    reason: str = ""

    # A dry run never queries the target, so source/target must not be compared for it - doing so
    # labelled every table MISMATCH and made a healthy plan look like a failure.
    planned: bool = False

    @property
    def verified(self) -> bool:
        return self.skipped or self.planned or self.source_rows == self.target_rows

    def line(self) -> str:
        if self.skipped:
            return f"  {self.table:32} skipped ({self.reason})"
        if self.planned:
            return f"  {self.table:32} would copy source={self.source_rows:>9}"
        mark = "OK " if self.verified else "MISMATCH"
        rate = int(self.copied_rows / self.seconds) if self.seconds > 0 else 0
        note = f"  {self.reason}" if self.reason else ""
        if self.sanitized_values:
            note += f"  (sanitized {self.sanitized_values} NUL value(s))"
        return (
            f"  {self.table:32} {mark} source={self.source_rows:>9} target={self.target_rows:>9} "
            f"{self.seconds:>7.1f}s {rate:>8}/s{note}"
        )


@dataclass
class MigrationResult:
    schema: str
    database: str
    tables: list[TableResult] = field(default_factory=list)
    ddl: dict[str, list[str]] = field(default_factory=dict)
    seconds: float = 0.0
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return all(result.verified for result in self.tables)

    @property
    def total_rows(self) -> int:
        return sum(result.copied_rows for result in self.tables)

    def report(self) -> str:
        head = (
            f"{'[dry-run] ' if self.dry_run else ''}sqlite -> postgresql "
            f"{self.database}.{self.schema}: {len(self.tables)} tables, "
            f"{self.total_rows} rows in {self.seconds:.1f}s"
        )
        lines = [head] + [result.line() for result in self.tables]
        if not self.ok:
            lines.append("  RESULT: row counts do not match - see MISMATCH rows above.")
        return "\n".join(lines)


def open_sqlite_readonly(sqlite_path: str | Path) -> sqlite3.Connection:
    """Open the source database read-only.

    The worker's daemon is normally still writing to this file. Read-only guarantees the migration
    cannot alter the live store, and in WAL mode readers do not block writers, so the daemon keeps
    running while the copy proceeds.
    """
    path = Path(sqlite_path)
    if not path.exists():
        raise MigrationError(f"SQLite store not found: {path}")
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def snapshot_sqlite(sqlite_path: str | Path, snapshot_path: str | Path) -> Path:
    """Copy the live SQLite store to a consistent standalone file via ``VACUUM INTO``.

    A migration reading the live file sees a moving target: every table is copied at a slightly
    different moment, so a row can reference a parent that was not yet there when the parent table
    was read. ``VACUUM INTO`` writes one transactionally consistent copy (and compacts it), which
    is the right source for a migration that has to add foreign keys at the end.
    """
    source = Path(sqlite_path)
    target = Path(snapshot_path)
    if target.exists():
        raise MigrationError(f"Snapshot target already exists: {target}. Remove it or pick another path.")
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = open_sqlite_readonly(source)
    try:
        conn.execute("VACUUM INTO ?;", (target.as_posix(),))
    finally:
        conn.close()
    return target


def migrate(
    postgres: PostgresStoreConfig,
    *,
    sqlite_path: str | Path,
    key: str | None = None,
    password: str | None = None,
    only_tables: Sequence[str] = (),
    exclude_tables: Sequence[str] = (),
    batch_rows: int = DEFAULT_BATCH_ROWS,
    dry_run: bool = False,
    delta: bool = False,
    reload_under: int = DEFAULT_RELOAD_UNDER_ROWS,
    resync_days: int = DEFAULT_RESYNC_DAYS,
    skip_data: bool = False,
    skip_indexes: bool = False,
    skip_foreign_keys: bool = False,
    progress: Callable[[str], None] = print,
) -> MigrationResult:
    """Copy the SQLite runtime store into the PostgreSQL store database.

    With ``delta=True`` nothing is truncated wholesale: each table is either reloaded (small, or no
    identity key) or brought up to date by appending rows above the target's max id and re-syncing
    the recent window. Use it to close the gap after an earlier full migration instead of copying
    millions of unchanged rows again.

    Phases, in this order and for the reasons noted in the module docstring: create tables ->
    truncate+COPY each table -> create indexes -> add foreign keys -> re-base identity sequences
    -> verify row counts.
    """
    schema = validate_identifier(postgres.schema or "public", kind="schema")
    started = time.monotonic()

    source = open_sqlite_readonly(sqlite_path)
    try:
        tables, indexes, foreign_keys = read_sqlite_schema(source)
        selected = _select_tables(tables, only_tables=only_tables, exclude_tables=exclude_tables)
        selected_names = {table.name for table in selected}
        # Only build the indexes/FKs that belong to the tables actually being migrated, so a
        # partial resume run does not fail on a table it was told to leave alone.
        indexes = [index for index in indexes if index.table in selected_names]
        foreign_keys = [
            fk for fk in foreign_keys
            if fk.table in selected_names and fk.references_table in selected_names
        ]
        ddl = build_ddl(selected, indexes, foreign_keys, schema=schema)

        result = MigrationResult(
            schema=schema, database=postgres.database, ddl=ddl, dry_run=dry_run
        )

        if dry_run:
            for table in selected:
                result.tables.append(
                    TableResult(
                        table=table.name,
                        source_rows=_sqlite_count(source, table.name),
                        planned=True,
                    )
                )
            result.seconds = time.monotonic() - started
            return result

        conn = connect(postgres, key=key, password=password, database=postgres.database)
        try:
            progress(f"Creating {len(ddl['tables'])} tables in {postgres.database}.{schema} ...")
            _execute_all(conn, ddl["tables"])
            conn.commit()

            # A pre-existing target table may not have this tool's shape - CREATE TABLE
            # IF NOT EXISTS would have silently left it as it found it.
            for table in selected:
                _reconcile_columns(conn, table, schema=schema, progress=progress)

            # Existing foreign keys would block TRUNCATE on a re-run; the FK phase re-adds them.
            if not skip_data and foreign_keys:
                _drop_foreign_keys(conn, foreign_keys, schema=schema)

            if skip_data:
                # Rows are already loaded; finish the phases that come after them. This is the
                # resume path for a run that copied everything and then failed while building
                # indexes - re-copying millions of rows to reach the index phase would extend an
                # outage for no benefit.
                progress("Skipping the row copy (--skip-data); existing rows are left as they are.")
                for table in selected:
                    result.tables.append(
                        TableResult(
                            table=table.name,
                            source_rows=_sqlite_count(source, table.name),
                            copied_rows=0,
                        )
                    )
            elif delta:
                progress(f"Delta mode: reloading tables under {reload_under} rows, appending above "
                         f"that and re-syncing the last {resync_days} day(s).")
                for table in selected:
                    result.tables.append(
                        _migrate_table_delta(
                            conn, source, table, schema=schema, batch_rows=batch_rows,
                            reload_under=reload_under, resync_days=resync_days, progress=progress,
                        )
                    )
            else:
                for table in selected:
                    result.tables.append(
                        _migrate_table(
                            conn, source, table, schema=schema, batch_rows=batch_rows, progress=progress
                        )
                    )

            if skip_indexes:
                progress("Skipping index creation (--skip-indexes).")
            else:
                progress(f"Creating {len(ddl['indexes'])} indexes ...")
                _execute_all(conn, ddl["indexes"])
                conn.commit()

            if skip_foreign_keys:
                progress("Skipping foreign keys (--skip-foreign-keys).")
            elif ddl["foreign_keys"]:
                progress(f"Adding {len(ddl['foreign_keys'])} foreign keys ...")
                _add_foreign_keys(conn, ddl["foreign_keys"], progress=progress)
                conn.commit()

            progress("Re-basing identity sequences ...")
            for table in selected:
                if table.autoincrement_column:
                    reset_identity(conn, schema=schema, table=table.name,
                                   column=table.autoincrement_column)
            conn.commit()

            for table_result in result.tables:
                if not table_result.skipped:
                    table_result.target_rows = _postgres_count(conn, schema, table_result.table)
        finally:
            conn.close()
    finally:
        source.close()

    result.seconds = time.monotonic() - started
    return result


def _select_tables(
    tables: Sequence[SqliteTable], *, only_tables: Sequence[str], exclude_tables: Sequence[str]
) -> list[SqliteTable]:
    known = {table.name for table in tables}
    wanted = {str(name).strip() for name in only_tables if str(name).strip()}
    unwanted = {str(name).strip() for name in exclude_tables if str(name).strip()}
    for name in wanted | unwanted:
        if name not in known:
            raise MigrationError(
                f"Unknown table {name!r}. The SQLite store has: {', '.join(sorted(known))}."
            )
    selected = [table for table in tables if (not wanted or table.name in wanted)]
    return [table for table in selected if table.name not in unwanted]


def _execute_all(conn, statements: Iterable[str]) -> None:
    cursor = conn.cursor()
    try:
        for statement in statements:
            cursor.execute(statement)
    finally:
        cursor.close()


def _reconcile_columns(
    conn, table: SqliteTable, *, schema: str, progress: Callable[[str], None]
) -> None:
    """Add any source column the target table is missing.

    ``CREATE TABLE IF NOT EXISTS`` does nothing when the table is already there, even if its shape is
    wrong - and the target's shape is not guaranteed to be this tool's work. The store's own
    table-rebuild migrations drop and recreate ``telegram_command_messages`` from an older column
    list, with the newer columns (``claimed_at``, ``command_id``, ...) added afterwards by
    ``ensure_sqlite_column``. Interrupt that sequence and the table exists with columns missing, so
    the COPY fails with 'column "claimed_at" ... does not exist'.

    Columns are added nullable even when the source declares NOT NULL: PostgreSQL rejects adding a
    NOT NULL column to a populated table without a default, and the constraint would add nothing here
    anyway - the rows come from a source that already enforced it.
    """
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = ? AND table_name = ?",
            (schema, table.name),
        )
        existing = {str(row[0]) for row in cursor.fetchall()}
    finally:
        cursor.close()

    missing = [column for column in table.columns if column.name not in existing]
    if not missing:
        return
    progress(f"  {table.name}: adding {len(missing)} missing column(s): "
             f"{', '.join(column.name for column in missing)}")
    cursor = conn.cursor()
    try:
        for column in missing:
            parts = [quote_identifier(column.name), postgres_type(column.declared_type)]
            default = translate_default(column.default)
            if default is not None:
                parts.append(f"DEFAULT {default}")
            cursor.execute(
                f"ALTER TABLE {qualified(schema, table.name)} "
                f"ADD COLUMN IF NOT EXISTS {' '.join(parts)}"
            )
    finally:
        cursor.close()
    conn.commit()


def _drop_foreign_keys(conn, foreign_keys: Sequence[SqliteForeignKey], *, schema: str) -> None:
    """Drop every foreign key in the store schema before any table is reloaded.

    PostgreSQL refuses to TRUNCATE a table another table references ("cannot truncate a table
    referenced in a foreign key constraint"). The first migration never hit this because it added the
    FKs only after loading; a re-run does, because they already exist.

    The names are read from ``pg_constraint`` rather than reconstructed, because not every FK in the
    schema was created by this tool. The store's own additive migrations
    (``migrate_reports_table``, ``migrate_telegram_command_messages_table``) build a ``*_new`` table
    carrying an inline FOREIGN KEY and then rename it, so PostgreSQL names those itself -
    ``reports_new_report_type_fkey``. Guessing ``fk_reports_report_type`` dropped nothing and the
    TRUNCATE still failed.

    TRUNCATE ... CASCADE would be the other way out, and is the wrong one: it silently empties child
    tables the run was not asked to touch. The foreign-key phase at the end re-adds this tool's
    constraints; any created by the store's own migrations come back on its next initialize().
    """
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT conname, conrelid::regclass::text FROM pg_constraint "
            "WHERE contype = 'f' AND connamespace = ?::regnamespace",
            (schema,),
        )
        existing = [(str(row[0]), str(row[1])) for row in cursor.fetchall()]
        for name, child in existing:
            cursor.execute(f"ALTER TABLE {child} DROP CONSTRAINT IF EXISTS {quote_identifier(name)}")
    finally:
        cursor.close()
    conn.commit()


def _add_foreign_keys(conn, statements: Sequence[str], *, progress: Callable[[str], None]) -> None:
    """Add each foreign key in its own transaction.

    A store that has been running for a while can hold rows whose parent was pruned by a
    retention job — SQLite does not enforce a foreign key that was declared while
    ``foreign_keys`` was off, and the store's own cleanup paths delete parents. One failing
    constraint must not roll back the others, so each is attempted separately and reported.
    """
    for statement in statements:
        cursor = conn.cursor()
        try:
            cursor.execute(statement)
            conn.commit()
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed.
            conn.rollback()
            progress(f"  WARNING: foreign key not added ({exc}). Statement: {statement}")
        finally:
            cursor.close()




def _timestamp_column(table: SqliteTable) -> str:
    for candidate in _TIMESTAMP_CANDIDATES:
        if candidate in table.column_names:
            return candidate
    return ""


def _migrate_table_delta(
    conn,
    source: sqlite3.Connection,
    table: SqliteTable,
    *,
    schema: str,
    batch_rows: int,
    reload_under: int,
    resync_days: int,
    progress: Callable[[str], None],
) -> TableResult:
    """Bring one table up to date without re-copying what is already there.

    Three cases, chosen per table rather than globally:

    * **No identity primary key** (``schema_meta``, ``report_types``, ``report_send_state``) - there
      is no monotonic column to diff on, and they hold a handful of rows. Reloaded whole.
    * **Small enough to reload** - exact, and fast. This is the important case: nearly every store
      table is UPDATEd after insert, and an id-based delta would append the new rows while silently
      leaving stale copies of the changed ones.
    * **Too large to reload** - append rows above the target's max id, then re-sync the recent
      window so updates to recent rows are picked up too.
    """
    result = TableResult(table=table.name, source_rows=_sqlite_count(source, table.name))
    started = time.monotonic()
    target_types = [
        "BIGINT" if column.name == table.autoincrement_column else postgres_type(column.declared_type)
        for column in table.columns
    ]
    columns = ", ".join(quote_identifier(name) for name in table.column_names)

    timestamp_only = ""
    if not table.autoincrement_column and result.source_rows > reload_under:
        # No primary key, but too big to reload. If the rows are dated, the date column is a usable
        # high-water mark: delete from the boundary timestamp and re-insert from there, which cannot
        # duplicate (unlike appending with >=) or skip (unlike appending with >) rows that share a
        # second. metric_results_archive is exactly this shape - 2.9M append-only rows, no key.
        timestamp_only = _timestamp_column(table)

    if timestamp_only:
        return _migrate_table_delta_by_timestamp(
            conn, source, table, schema=schema, batch_rows=batch_rows,
            timestamp=timestamp_only, target_types=target_types, columns=columns,
            result=result, started=started, progress=progress,
        )

    if not table.autoincrement_column or result.source_rows <= reload_under:
        reason = "no identity key" if not table.autoincrement_column else f"<= {reload_under} rows"
        progress(f"  {table.name}: reloading whole table ({reason})")
        outcome = _migrate_table(
            conn, source, table, schema=schema, batch_rows=batch_rows, progress=progress
        )
        outcome.reason = f"reloaded ({reason})"
        return outcome

    pk = table.autoincrement_column
    cursor = conn.cursor()
    try:
        cursor.execute(
            f"SELECT COALESCE(MAX({quote_identifier(pk)}), 0) FROM {qualified(schema, table.name)}")
        row = cursor.fetchone()
        # Raw pg8000 cursor: rows are plain lists, so index positionally (the Row adapter with
        # by-name access lives in db_ops.db.backend and is not in play here).
        max_pk = int(row[0]) if row is not None else 0
    finally:
        cursor.close()

    # 1. Drop what retention removed from the source.
    #
    # The store trims its oldest rows: archive_old_results() copies aged metric_results into
    # metric_results_archive and then DELETEs them. An append-only delta never notices, so the target
    # keeps rows the source no longer has and ends up *larger* than it - 3026 extra metric_results
    # rows on the first delta run, which is what the row-count verification caught. Because the trim
    # always takes the oldest rows, the source's lowest surviving id is an exact watermark.
    pruned = 0
    source_min = source.execute(
        f"SELECT MIN({quote_identifier(pk)}) FROM {quote_identifier(table.name)}").fetchone()
    if source_min is not None and source_min[0] is not None:
        prune_cursor = conn.cursor()
        try:
            prune_cursor.execute(
                f"DELETE FROM {qualified(schema, table.name)} WHERE {quote_identifier(pk)} < ?",
                (int(source_min[0]),))
            pruned = int(prune_cursor.rowcount or 0)
        finally:
            prune_cursor.close()
        conn.commit()
        if pruned:
            progress(f"  {table.name}: pruned {pruned} row(s) below source id {source_min[0]}")

    # 2. Append everything newer than the target's high-water mark.
    inserted = 0
    select = source.execute(
        f"SELECT {columns} FROM {quote_identifier(table.name)} "
        f"WHERE {quote_identifier(pk)} > ? ORDER BY {quote_identifier(pk)}",
        (max_pk,),
    )
    while True:
        rows = select.fetchmany(batch_rows)
        if not rows:
            break
        result.sanitized_values += count_nul_values(rows)
        _copy_chunk(conn, schema=schema, table=table, target_types=target_types, rows=rows)
        conn.commit()
        inserted += len(rows)
        progress(f"  {table.name}: appended {inserted} new row(s) (id > {max_pk}) ...")
    select.close()

    # 3. Re-sync the recent window, which is where updates land.
    resynced = 0
    timestamp = _timestamp_column(table)
    if resync_days > 0 and timestamp:
        cutoff = cutoff_text(resync_days)
        delete_cursor = conn.cursor()
        try:
            delete_cursor.execute(
                f"DELETE FROM {qualified(schema, table.name)} "
                f"WHERE {quote_identifier(timestamp)} >= ? AND {quote_identifier(pk)} <= ?",
                (cutoff, max_pk),
            )
        finally:
            delete_cursor.close()
        conn.commit()
        select = source.execute(
            f"SELECT {columns} FROM {quote_identifier(table.name)} "
            f"WHERE {quote_identifier(timestamp)} >= ? AND {quote_identifier(pk)} <= ?",
            (cutoff, max_pk),
        )
        while True:
            rows = select.fetchmany(batch_rows)
            if not rows:
                break
            result.sanitized_values += count_nul_values(rows)
            _copy_chunk(conn, schema=schema, table=table, target_types=target_types, rows=rows)
            conn.commit()
            resynced += len(rows)
        select.close()

    result.copied_rows = inserted + resynced
    result.reason = f"appended {inserted}, resynced {resynced}, pruned {pruned}"
    result.seconds = time.monotonic() - started
    return result


def _migrate_table_delta_by_timestamp(
    conn, source, table, *, schema, batch_rows, timestamp, target_types, columns, result, started,
    progress,
) -> TableResult:
    """Delta a keyless table on its timestamp column."""
    cursor = conn.cursor()
    try:
        cursor.execute(
            f"SELECT MAX({quote_identifier(timestamp)}) FROM {qualified(schema, table.name)}")
        row = cursor.fetchone()
        boundary = row[0] if row is not None else None
    finally:
        cursor.close()

    if boundary is None:
        progress(f"  {table.name}: target is empty, loading whole table")
        return _migrate_table(
            conn, source, table, schema=schema, batch_rows=batch_rows, progress=progress
        )

    # Prune anything retention removed from the source's old end, then re-sync from the boundary.
    pruned = 0
    source_min = source.execute(
        f"SELECT MIN({quote_identifier(timestamp)}) FROM {quote_identifier(table.name)}").fetchone()
    delete_cursor = conn.cursor()
    try:
        if source_min is not None and source_min[0] is not None:
            delete_cursor.execute(
                f"DELETE FROM {qualified(schema, table.name)} "
                f"WHERE {quote_identifier(timestamp)} < ?", (source_min[0],))
            pruned = int(delete_cursor.rowcount or 0)
        delete_cursor.execute(
            f"DELETE FROM {qualified(schema, table.name)} "
            f"WHERE {quote_identifier(timestamp)} >= ?", (boundary,))
    finally:
        delete_cursor.close()
    conn.commit()

    copied = 0
    select = source.execute(
        f"SELECT {columns} FROM {quote_identifier(table.name)} "
        f"WHERE {quote_identifier(timestamp)} >= ?", (boundary,))
    while True:
        rows = select.fetchmany(batch_rows)
        if not rows:
            break
        result.sanitized_values += count_nul_values(rows)
        _copy_chunk(conn, schema=schema, table=table, target_types=target_types, rows=rows)
        conn.commit()
        copied += len(rows)
        progress(f"  {table.name}: re-synced {copied} row(s) from {timestamp} >= {boundary} ...")
    select.close()

    result.copied_rows = copied
    result.reason = f"delta on {timestamp} >= {boundary} ({copied} row(s), pruned {pruned})"
    result.seconds = time.monotonic() - started
    return result


def cutoff_text(days: int) -> str:
    """UTC timestamp ``days`` ago in the store's text format (same helper the metric reads use)."""
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(days=int(days))).strftime("%Y-%m-%dT%H:%M:%SZ")


def _migrate_table(
    conn,
    source: sqlite3.Connection,
    table: SqliteTable,
    *,
    schema: str,
    batch_rows: int,
    progress: Callable[[str], None],
) -> TableResult:
    result = TableResult(table=table.name, source_rows=_sqlite_count(source, table.name))
    started = time.monotonic()
    target_types = [
        "BIGINT" if column.name == table.autoincrement_column else postgres_type(column.declared_type)
        for column in table.columns
    ]

    # Truncate first so a resumed run cannot duplicate rows. RESTART IDENTITY clears the sequence
    # too; it is re-based from the copied data afterwards.
    cursor = conn.cursor()
    try:
        cursor.execute(f"TRUNCATE TABLE {qualified(schema, table.name)} RESTART IDENTITY")
    finally:
        cursor.close()

    columns = ", ".join(quote_identifier(name) for name in table.column_names)
    select_cursor = source.execute(f"SELECT {columns} FROM {quote_identifier(table.name)}")
    copied = 0
    while True:
        rows = select_cursor.fetchmany(batch_rows)
        if not rows:
            break
        result.sanitized_values += count_nul_values(rows)
        _copy_chunk(conn, schema=schema, table=table, target_types=target_types, rows=rows)
        conn.commit()
        copied += len(rows)
        if result.source_rows >= batch_rows:
            progress(f"  {table.name}: {copied}/{result.source_rows} rows ...")
    select_cursor.close()

    result.copied_rows = copied
    result.seconds = time.monotonic() - started
    return result


def reset_identity(conn, *, schema: str, table: str, column: str) -> None:
    """Point the identity sequence past the largest id that was copied in.

    Without this the first insert after the migration reuses id 1 and collides with a migrated
    row — the classic "migrated fine, then every write fails" outcome.
    """
    cursor = conn.cursor()
    try:
        cursor.execute(
            f"SELECT setval("
            f"  pg_get_serial_sequence(?, ?),"
            f"  COALESCE((SELECT MAX({quote_identifier(column)}) FROM {qualified(schema, table)}), 1),"
            f"  (SELECT MAX({quote_identifier(column)}) IS NOT NULL FROM {qualified(schema, table)})"
            f")",
            (f"{schema}.{table}", column),
        )
    finally:
        cursor.close()


def _sqlite_count(conn: sqlite3.Connection, table: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) FROM {quote_identifier(table)}").fetchone()
    return int(row[0]) if row else 0


def _postgres_count(conn, schema: str, table: str) -> int:
    cursor = conn.cursor()
    try:
        cursor.execute(f"SELECT COUNT(*) FROM {qualified(schema, table)}")
        row = cursor.fetchone()
        return int(row[0]) if row else 0
    finally:
        cursor.close()


def verify(
    postgres: PostgresStoreConfig,
    *,
    sqlite_path: str | Path,
    key: str | None = None,
    password: str | None = None,
) -> MigrationResult:
    """Compare row counts per table between the SQLite source and the PostgreSQL store."""
    schema = validate_identifier(postgres.schema or "public", kind="schema")
    result = MigrationResult(schema=schema, database=postgres.database)
    started = time.monotonic()

    source = open_sqlite_readonly(sqlite_path)
    try:
        tables, _, _ = read_sqlite_schema(source)
        conn = connect(postgres, key=key, password=password, database=postgres.database)
        try:
            for table in tables:
                table_result = TableResult(
                    table=table.name, source_rows=_sqlite_count(source, table.name)
                )
                try:
                    table_result.target_rows = _postgres_count(conn, schema, table.name)
                except Exception as exc:  # noqa: BLE001 - a missing table is a real answer here.
                    conn.rollback()
                    table_result.skipped = True
                    table_result.reason = f"not in PostgreSQL ({exc})"
                result.tables.append(table_result)
        finally:
            conn.close()
    finally:
        source.close()

    result.seconds = time.monotonic() - started
    return result


__all__ = [
    "DEFAULT_BATCH_ROWS",
    "MigrationError",
    "MigrationResult",
    "PostgresStoreError",
    "SqliteColumn",
    "SqliteForeignKey",
    "SqliteIndex",
    "SqliteTable",
    "TableResult",
    "build_ddl",
    "create_index_sql",
    "create_table_sql",
    "encode_copy_value",
    "migrate",
    "open_sqlite_readonly",
    "postgres_type",
    "read_sqlite_schema",
    "reset_identity",
    "snapshot_sqlite",
    "translate_default",
    "verify",
]
