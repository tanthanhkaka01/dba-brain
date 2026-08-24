"""Render SQL Server DDL from catalogue rows. Pure text in, pure text out — no connection.

The counterpart of :mod:`db_ops.common.schema_catalog`, which reads ``sys.*`` and hands back
plain dicts. Splitting the two is what makes either testable: a renderer that owns a cursor can
only be exercised against a live instance, and a nine-column ``CREATE TABLE`` with a computed
column, a filtered unique index and a partition scheme is exactly the thing you want to assert on
without one.

**Every function here takes dicts and returns a string.** The dict keys are the ``sys.*`` column
names, unchanged, so a reader can go from the rendered statement back to the catalogue view it
came from without a translation table in between:

``column``      ``name, type_name, max_length, precision, scale, is_nullable, is_identity,
                collation_name, computed_definition, is_persisted, seed_value, increment_value,
                default_name, default_definition``
``key``         ``name, type (PK|UQ), type_desc, columns[{name, is_descending_key}]``
``index``       ``name, is_unique, type_desc, filter_definition, key_columns[], included_columns[],
                storage{}``
``storage``     ``data_space, data_space_type_desc, partition_column`` — what an ``ON`` clause needs
``check``       ``name, definition``
``foreign_key`` ``name, table, columns[], referenced_schema, referenced_table,
                referenced_columns[], delete_action, update_action``

**Idempotence is rendered separately from the statement**, as a `guard_*` predicate the caller
puts in front. That is not stylistic: applying it as ``<guard> EXEC(?)`` keeps the statement a
*bound parameter* rather than a string doubled into a literal, which is the difference between a
module body with an apostrophe in a comment working and silently truncating the batch. The plan
output can still show guard and statement side by side, so nothing is hidden from the reader.
"""

from __future__ import annotations

import datetime
import decimal
from typing import Any, Iterable, Mapping, Sequence


#: Types whose declaration carries a length. ``max_length`` is in *bytes*, so the Unicode ones
#: are halved — the classic off-by-two that produces ``nvarchar(200)`` for a ``nvarchar(100)``.
LENGTH_TYPES = frozenset({"char", "varchar", "nchar", "nvarchar", "binary", "varbinary"})
#: Types declared with a scale only.
SCALE_TYPES = frozenset({"datetime2", "time", "datetimeoffset"})
#: Types declared with precision *and* scale.
PRECISION_TYPES = frozenset({"decimal", "numeric"})
#: Where a COLLATE clause is meaningful. Binary types have `max_length` but no collation.
COLLATABLE_TYPES = frozenset({"char", "varchar", "nchar", "nvarchar", "text", "ntext"})

#: Object types this module can rewrite into `CREATE OR ALTER`. Keyed by the word that appears
#: in the definition; the value is what replaces it.
_MODULE_KEYWORDS = ("procedure", "proc", "view", "function", "trigger")


def quote(name: Any) -> str:
    """``[name]``, with an embedded ``]`` doubled. The only correct way to write an identifier."""
    text = str(name)
    return "[" + text.replace("]", "]]") + "]"


def qualify(schema: str, name: str) -> str:
    """``[schema].[name]``."""
    return f"{quote(schema)}.{quote(name)}"


def quote_string(value: Any) -> str:
    """``N'...'`` — a Unicode string literal with its quotes doubled."""
    return "N'" + str(value).replace("'", "''") + "'"


def literal(value: Any) -> str:
    """One value as a SQL Server literal, for the places a bind parameter cannot go.

    Partition boundary values are the reason this exists: ``CREATE PARTITION FUNCTION ... FOR
    VALUES (...)`` takes no parameters, so the boundaries read out of
    ``sys.partition_range_values`` have to be written into the statement text. Getting a
    ``datetime`` boundary wrong there silently repartitions the table.
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float, decimal.Decimal)):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "0x" + bytes(value).hex().upper()
    if isinstance(value, datetime.datetime):
        # ODBC canonical form, and the one SQL Server parses the same way under every DATEFORMAT
        # / language setting. `21` keeps the fractional seconds a datetime2 boundary may carry.
        return "'" + value.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "'"
    if isinstance(value, datetime.date):
        return "'" + value.isoformat() + "'"
    if isinstance(value, datetime.time):
        return "'" + value.strftime("%H:%M:%S.%f")[:-3] + "'"
    return quote_string(value)


# --------------------------------------------------------------------------- columns and tables


def render_type(column: Mapping[str, Any]) -> str:
    """The type as it must be declared: ``nvarchar(100)``, ``decimal(18,4)``, ``datetime2(3)``."""
    type_name = str(column.get("type_name") or "").lower()
    if type_name in LENGTH_TYPES:
        max_length = int(column.get("max_length") or 0)
        if max_length == -1:
            size = "MAX"
        elif type_name in ("nchar", "nvarchar"):
            size = str(max_length // 2)
        else:
            size = str(max_length)
        return f"{type_name}({size})"
    if type_name in PRECISION_TYPES:
        return f"{type_name}({int(column.get('precision') or 0)},{int(column.get('scale') or 0)})"
    if type_name in SCALE_TYPES:
        return f"{type_name}({int(column.get('scale') or 0)})"
    return type_name


def render_column(column: Mapping[str, Any]) -> str:
    """One column's line inside a ``CREATE TABLE``.

    A computed column is *only* its expression: it has a type in ``sys.columns``, and writing
    that type out produces a table whose column is no longer computed — the values silently stop
    tracking the expression and nothing reports it.
    """
    name = quote(column["name"])
    computed = column.get("computed_definition")
    if computed:
        line = f"{name} AS {computed}"
        return line + " PERSISTED" if column.get("is_persisted") else line

    line = f"{name} {render_type(column)}"
    collation = column.get("collation_name")
    if collation and str(column.get("type_name") or "").lower() in COLLATABLE_TYPES:
        line += f" COLLATE {collation}"
    if column.get("is_identity"):
        seed = int(column.get("seed_value") or 1)
        increment = int(column.get("increment_value") or 1)
        line += f" IDENTITY({seed},{increment})"
    if column.get("is_rowguidcol"):
        line += " ROWGUIDCOL"
    line += " NULL" if column.get("is_nullable") else " NOT NULL"
    if column.get("default_definition"):
        # Named, not anonymous: an auto-named default constraint gets a different name on every
        # instance, and the next comparison between the two tiers reports a difference that is not
        # one.
        if column.get("default_name"):
            line += f" CONSTRAINT {quote(column['default_name'])}"
        line += f" DEFAULT {column['default_definition']}"
    return line


def render_key_constraint(key: Mapping[str, Any]) -> str:
    """A PRIMARY KEY / UNIQUE line inside a ``CREATE TABLE``, clustered as the source has it."""
    columns = ", ".join(
        quote(item["name"]) + (" DESC" if item.get("is_descending_key") else "")
        for item in key.get("columns") or ()
    )
    kind = "PRIMARY KEY" if str(key.get("type") or "").upper() == "PK" else "UNIQUE"
    clustering = str(key.get("type_desc") or "").upper()
    clustering = f" {clustering}" if clustering in ("CLUSTERED", "NONCLUSTERED") else ""
    return f"CONSTRAINT {quote(key['name'])} {kind}{clustering} ({columns})"


def render_storage(storage: Mapping[str, Any] | None) -> str:
    """The ``ON ...`` clause: a partition scheme with its column, or a filegroup.

    Returned empty when there is nothing to say, so a caller can concatenate it unconditionally.
    **This is the clause whose absence cost the 2026-08-14 UAT hop 32 partitioned indexes** — the
    tables were created, every index landed on PRIMARY, and nothing failed.
    """
    if not storage:
        return ""
    name = storage.get("data_space")
    if not name:
        return ""
    if str(storage.get("data_space_type_desc") or "").upper() == "PARTITION_SCHEME":
        column = storage.get("partition_column")
        if not column:
            raise ValueError(
                f"partition scheme {name!r} named with no partitioning column; the ON clause "
                "cannot be written without one."
            )
        return f" ON {quote(name)}({quote(column)})"
    return f" ON {quote(name)}"


def render_create_table(schema: str, table: str, columns: Sequence[Mapping[str, Any]], *,
                        keys: Sequence[Mapping[str, Any]] = (),
                        storage: Mapping[str, Any] | None = None) -> str:
    """The whole ``CREATE TABLE``, with its inline key constraints and storage clause."""
    if not columns:
        raise ValueError(f"{schema}.{table} has no columns to create.")
    body = [render_column(column) for column in columns]
    # PK first, then unique constraints by name — a stable order, so two runs of this against the
    # same source produce byte-identical text and a diff between tiers means something.
    body += [render_key_constraint(key) for key in
             sorted(keys, key=lambda k: (str(k.get("type") or "") != "PK", str(k.get("name"))))]
    lines = ",\n".join("    " + line for line in body)
    return f"CREATE TABLE {qualify(schema, table)} (\n{lines}\n){render_storage(storage)};"


# ------------------------------------------------------------------------------------- indexes


def render_create_index(schema: str, table: str, index: Mapping[str, Any]) -> str:
    """One ``CREATE INDEX``, including the shapes a key-column loop drops on the floor.

    A **clustered columnstore index has no key columns at all**, so the obvious
    ``if not keys: continue`` skips it — and the table is created as a heap that reports as
    "copied". Columnstore, filtered, included and descending are all handled here so that no
    caller has to know which of them it is looking at.
    """
    type_desc = str(index.get("type_desc") or "NONCLUSTERED").upper().replace("_", " ")
    keys = index.get("key_columns") or ()
    included = index.get("included_columns") or ()
    unique = "UNIQUE " if index.get("is_unique") else ""

    if "COLUMNSTORE" in type_desc:
        if type_desc.startswith("CLUSTERED"):
            statement = (f"CREATE CLUSTERED COLUMNSTORE INDEX {quote(index['name'])} "
                         f"ON {qualify(schema, table)}")
        else:
            columns = ", ".join(quote(item["name"]) for item in keys)
            statement = (f"CREATE NONCLUSTERED COLUMNSTORE INDEX {quote(index['name'])} "
                         f"ON {qualify(schema, table)} ({columns})")
    else:
        if not keys:
            raise ValueError(
                f"index {index.get('name')!r} on {schema}.{table} is {type_desc} with no key "
                "columns; it cannot be scripted from sys.index_columns alone."
            )
        columns = ", ".join(
            quote(item["name"]) + (" DESC" if item.get("is_descending_key") else "")
            for item in keys
        )
        statement = (f"CREATE {unique}{type_desc} INDEX {quote(index['name'])} "
                     f"ON {qualify(schema, table)} ({columns})")
        if included:
            statement += " INCLUDE (" + ", ".join(quote(item["name"]) for item in included) + ")"
    if index.get("filter_definition"):
        statement += f" WHERE {index['filter_definition']}"
    return statement + render_storage(index.get("storage")) + ";"


# --------------------------------------------------------------------------------- constraints


def render_check_constraint(schema: str, table: str, check: Mapping[str, Any]) -> str:
    """``ALTER TABLE ... ADD CONSTRAINT ... CHECK (...)``, trusted unless the source was not."""
    keyword = "WITH NOCHECK ADD" if check.get("is_not_trusted") else "WITH CHECK ADD"
    return (f"ALTER TABLE {qualify(schema, table)} {keyword} CONSTRAINT "
            f"{quote(check['name'])} CHECK {check['definition']};")


def render_foreign_key(schema: str, foreign_key: Mapping[str, Any]) -> str:
    """One foreign key, with its referential actions and the source's own trust state."""
    parent = ", ".join(quote(name) for name in foreign_key["columns"])
    referenced = ", ".join(quote(name) for name in foreign_key["referenced_columns"])
    keyword = "WITH NOCHECK ADD" if foreign_key.get("is_not_trusted") else "WITH CHECK ADD"
    statement = (
        f"ALTER TABLE {qualify(schema, foreign_key['table'])} {keyword} CONSTRAINT "
        f"{quote(foreign_key['name'])} FOREIGN KEY ({parent}) REFERENCES "
        f"{qualify(foreign_key['referenced_schema'], foreign_key['referenced_table'])} ({referenced})"
    )
    for clause, action in (("ON DELETE", foreign_key.get("delete_action")),
                           ("ON UPDATE", foreign_key.get("update_action"))):
        action = str(action or "NO_ACTION").upper()
        if action != "NO_ACTION":
            statement += f" {clause} {action.replace('_', ' ')}"
    return statement + ";"


# ---------------------------------------------------------------------------------- partitions


def render_partition_function(name: str, *, parameter_type: str, boundary_right: bool,
                              values: Sequence[Any]) -> str:
    """``CREATE PARTITION FUNCTION``.

    ``boundary_right`` is ``sys.partition_functions.boundary_value_on_right``. Getting it
    backwards puts every row one partition out — a table that still queries correctly and whose
    partition elimination is silently wrong.
    """
    boundaries = ", ".join(literal(value) for value in values)
    direction = "RIGHT" if boundary_right else "LEFT"
    return (f"CREATE PARTITION FUNCTION {quote(name)} ({parameter_type}) "
            f"AS RANGE {direction} FOR VALUES ({boundaries});")


def render_partition_scheme(name: str, *, function_name: str,
                            filegroups: Sequence[str]) -> str:
    """``CREATE PARTITION SCHEME``. ``ALL TO`` when every partition lands on one filegroup."""
    unique = {str(group) for group in filegroups}
    if len(unique) == 1:
        return (f"CREATE PARTITION SCHEME {quote(name)} AS PARTITION {quote(function_name)} "
                f"ALL TO ({quote(next(iter(unique)))});")
    targets = ", ".join(quote(group) for group in filegroups)
    return (f"CREATE PARTITION SCHEME {quote(name)} AS PARTITION {quote(function_name)} "
            f"TO ({targets});")


# ----------------------------------------------------------------------------- change tracking


def render_change_tracking_database(database: str, *, retention: int, retention_units: str,
                                    auto_cleanup: bool) -> str:
    """``ALTER DATABASE ... SET CHANGE_TRACKING = ON``.

    Not cosmetic and not deferrable: a module that calls ``CHANGETABLE()`` fails to *create* with
    Msg 22105 when tracking is off, so this has to run before the modules phase or half the
    schema does not compile. Schema scripting from ``sys.tables`` carries no trace of it.
    """
    return (f"ALTER DATABASE {quote(database)} SET CHANGE_TRACKING = ON "
            f"(CHANGE_RETENTION = {int(retention)} {retention_units}, "
            f"AUTO_CLEANUP = {'ON' if auto_cleanup else 'OFF'});")


def render_change_tracking_table(schema: str, table: str, *,
                                 track_columns_updated: bool) -> str:
    """``ALTER TABLE ... ENABLE CHANGE_TRACKING``. The table must already exist."""
    return (f"ALTER TABLE {qualify(schema, table)} ENABLE CHANGE_TRACKING "
            f"WITH (TRACK_COLUMNS_UPDATED = {'ON' if track_columns_updated else 'OFF'});")


# -------------------------------------------------------------------------------------- modules


def as_create_or_alter(definition: str) -> str:
    """Rewrite a module's ``CREATE`` into ``CREATE OR ALTER``, leaving everything else alone.

    This is what makes the modules phase resumable: re-running it after a failure updates what is
    there instead of failing on all of it. The rewrite is deliberately narrow — the *first*
    ``CREATE <keyword>`` only, whitespace between the two words tolerated (``CREATE   PROCEDURE``
    is what SSMS writes after a rename), and the original text preserved on either side so
    comments, ``SET`` options and the body are untouched.
    """
    text = str(definition)
    lowered = text.lower()
    best: tuple[int, int, str] | None = None
    for keyword in _MODULE_KEYWORDS:
        index = 0
        while True:
            index = lowered.find(keyword, index)
            if index == -1:
                break
            head = lowered.rfind("create", 0, index)
            # Only the gap between the two words, and only whitespace in it.
            if head != -1 and not lowered[head + len("create"):index].strip():
                span_end = index + len(keyword)
                if best is None or head < best[0]:
                    best = (head, span_end, keyword)
                break
            index += len(keyword)
    if best is None:
        return text
    head, span_end, keyword = best
    word = "PROCEDURE" if keyword in ("procedure", "proc") else keyword.upper()
    return text[:head] + f"CREATE OR ALTER {word}" + text[span_end:]


# --------------------------------------------------------------------------------------- guards


def guard_object_absent(schema: str, name: str, *, object_type: str = "") -> str:
    """``IF OBJECT_ID(...) IS NULL`` — put in front of a statement that creates that object."""
    target = quote_string(f"{schema}.{name}")
    kind = f", {quote_string(object_type)}" if object_type else ""
    return f"IF OBJECT_ID({target}{kind}) IS NULL"


def guard_index_absent(schema: str, table: str, index: str) -> str:
    """``IF NOT EXISTS (SELECT 1 FROM sys.indexes ...)``.

    An index is not an object: ``OBJECT_ID`` never finds one, so the object guard would re-run
    every ``CREATE INDEX`` on a second pass and fail on the first one that already exists.
    """
    return (f"IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = {quote_string(index)} "
            f"AND object_id = OBJECT_ID({quote_string(f'{schema}.{table}')}))")


def guard_partition_function_absent(name: str) -> str:
    return f"IF NOT EXISTS (SELECT 1 FROM sys.partition_functions WHERE name = {quote_string(name)})"


def guard_partition_scheme_absent(name: str) -> str:
    return f"IF NOT EXISTS (SELECT 1 FROM sys.partition_schemes WHERE name = {quote_string(name)})"


def guard_table_empty(schema: str, table: str) -> str:
    """``IF NOT EXISTS (SELECT 1 FROM <table>)`` — the guard the data phase needs.

    Read-then-write, and **that is its limit**: two processes both read empty and both load. It
    is the reason the copy runs under an application lock rather than trusting this.
    """
    return f"IF NOT EXISTS (SELECT 1 FROM {qualify(schema, table)})"


def guard_schema_absent(schema: str) -> str:
    return f"IF SCHEMA_ID({quote_string(schema)}) IS NULL"


def guarded(guard: str, statement: str) -> str:
    """Guard and statement as one readable line, for a plan a human is about to approve.

    Not what gets executed — the apply path binds the statement as a parameter instead of
    embedding it. This is the printable form.
    """
    if not guard:
        return statement
    body = statement.rstrip().rstrip(";")
    return f"{guard}\n    EXEC({quote_string(body)});"


def batch_lines(statements: Iterable[str]) -> str:
    """Statements joined into one script with ``GO`` between them, for writing to a file."""
    return "\nGO\n".join(str(statement).rstrip().rstrip(";") for statement in statements) + "\nGO\n"
