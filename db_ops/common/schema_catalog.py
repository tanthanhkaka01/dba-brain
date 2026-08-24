"""Read one SQL Server schema out of ``sys.*`` and hand it back as plain dicts.

The read half of the schema-copy pair; :mod:`db_ops.lib.mssql_ddl` is the render half. Nothing
here writes, nothing here decides — every function takes an **open cursor** plus a schema name and
returns lists of dicts whose keys are the catalogue column names. That is what lets a caller use
one of these on its own: "which of this schema's indexes are on a partition scheme" and "what
does this table's change tracking say" are questions people ask outside a copy, and each is one
function here rather than a section of somebody's deployment script.

**Why a cursor and not a target.** Resolving a target reads the data folder; this module must not,
so the caller resolves and connects (through :mod:`db_ops.common.sql_run`) and passes the cursor
in. It also means the *same* functions read the source and the destination, which is the whole of
the verification step.

**Literals, not bind parameters.** Every value that reaches these statements is an identifier —
a schema or object name — quoted through :func:`db_ops.lib.mssql_ddl.quote_string`. Bind
placeholders are spelled differently by pyodbc (``?``) and the pymssql fallback (``%s``), and a
catalogue reader that has to know which driver opened is a reader that breaks in the Linux worker
and not on the developer's box.

**Version tolerance.** A probe for a feature that does not exist on the target's version raises
rather than answering "no". :func:`unsupported_features` catches that per probe and reports it as
*unknown*, because "SQL Server 2012 has no ``sys.masked_columns``" and "there are no masked
columns" are different facts and only one of them is safe to act on.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from db_ops.lib import mssql_ddl


class SchemaCatalogError(RuntimeError):
    """A catalogue read failed, or a schema/object the caller named is not there."""


def rows(cursor: Any, sql: str) -> list[dict[str, Any]]:
    """Run one catalogue query and return its rows as dicts keyed by column name."""
    try:
        cursor.execute(sql)
        # Skip forward to the first statement that actually returned rows. A batch whose answer
        # comes after an `EXEC` — `sp_getapplock` then `SELECT @rc` — leaves the cursor on a
        # rowset-less result, and reading `description` there answers "no rows" for a query that
        # returned one.
        while cursor.description is None:
            if not (hasattr(cursor, "nextset") and cursor.nextset()):
                return []
        columns = [str(item[0]) for item in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    except Exception as exc:  # noqa: BLE001 - a catalogue failure is an operator message.
        raise SchemaCatalogError(f"catalogue query failed: {exc}") from exc


def scalar(cursor: Any, sql: str) -> Any:
    """The first column of the first row, or ``None``."""
    result = rows(cursor, sql)
    if not result:
        return None
    return next(iter(result[0].values()))


def _s(value: Any) -> str:
    return mssql_ddl.quote_string(value)


def _object(schema: str, name: str) -> str:
    return _s(f"{schema}.{name}")


# --------------------------------------------------------------------------- where am I


def server_identity(cursor: Any) -> dict[str, Any]:
    """Which instance and database this cursor is actually on.

    Both halves matter and only one is usually checked. ``DB_NAME()`` does not identify a server:
    the same database name commonly exists on several instances of the same estate — on
    ``2026-08-22`` the production and UAT tiers both had a database called ``APPDB_Prod`` — and a
    guard that reads only the database name passes on the wrong machine.
    """
    return rows(cursor, """
        SELECT CONVERT(sysname, SERVERPROPERTY('ServerName')) AS server_name,
               DB_NAME() AS database_name,
               CONVERT(nvarchar(128), SERVERPROPERTY('Edition')) AS edition,
               CONVERT(nvarchar(128), SERVERPROPERTY('ProductVersion')) AS product_version
    """)[0]


def schema_exists(cursor: Any, schema: str) -> bool:
    return bool(scalar(cursor, f"SELECT SCHEMA_ID({_s(schema)})") is not None)


# ------------------------------------------------------------------------------- tables


def list_tables(cursor: Any, schema: str) -> list[str]:
    """Every user table in the schema, by name, in a stable order."""
    return [str(row["name"]) for row in rows(cursor, f"""
        SELECT t.name
        FROM sys.tables t
        JOIN sys.schemas s ON s.schema_id = t.schema_id
        WHERE s.name = {_s(schema)} AND t.is_ms_shipped = 0
        ORDER BY t.name
    """)]


def table_columns(cursor: Any, schema: str, table: str) -> list[dict[str, Any]]:
    """One table's columns in ordinal order, with everything a ``CREATE TABLE`` line needs."""
    return rows(cursor, f"""
        SELECT c.name, ty.name AS type_name, c.max_length, c.precision, c.scale,
               c.is_nullable, c.is_identity, c.is_rowguidcol, c.collation_name,
               cc.definition AS computed_definition, cc.is_persisted,
               CAST(ic.seed_value AS BIGINT) AS seed_value,
               CAST(ic.increment_value AS BIGINT) AS increment_value,
               dc.name AS default_name, dc.definition AS default_definition
        FROM sys.columns c
        JOIN sys.types ty ON ty.user_type_id = c.user_type_id
        LEFT JOIN sys.computed_columns cc
               ON cc.object_id = c.object_id AND cc.column_id = c.column_id
        LEFT JOIN sys.identity_columns ic
               ON ic.object_id = c.object_id AND ic.column_id = c.column_id
        LEFT JOIN sys.default_constraints dc
               ON dc.parent_object_id = c.object_id AND dc.parent_column_id = c.column_id
        WHERE c.object_id = OBJECT_ID({_object(schema, table)})
        ORDER BY c.column_id
    """)


def insertable_columns(cursor: Any, schema: str, table: str) -> list[str]:
    """The columns a data load may write: everything except computed ones.

    A computed column is rejected by ``INSERT`` outright, so a copy that selects ``*`` from the
    source builds a column list the destination refuses — and the failure names a column count,
    not the column.
    """
    return [str(row["name"]) for row in rows(cursor, f"""
        SELECT c.name
        FROM sys.columns c
        LEFT JOIN sys.computed_columns cc
               ON cc.object_id = c.object_id AND cc.column_id = c.column_id
        WHERE c.object_id = OBJECT_ID({_object(schema, table)}) AND cc.definition IS NULL
        ORDER BY c.column_id
    """)]


def has_identity(cursor: Any, schema: str, table: str) -> bool:
    """Whether the table has an identity column, i.e. whether the load needs IDENTITY_INSERT."""
    return bool(scalar(cursor, "SELECT COUNT(*) FROM sys.identity_columns "
                               f"WHERE object_id = OBJECT_ID({_object(schema, table)})"))


def row_count(cursor: Any, schema: str, table: str) -> int:
    """An exact ``COUNT_BIG`` — this is used to compare tiers, so an estimate will not do."""
    return int(scalar(cursor, f"SELECT COUNT_BIG(*) FROM {mssql_ddl.qualify(schema, table)}") or 0)


# --------------------------------------------------------------------------- keys, indexes


def index_columns(cursor: Any, schema: str, table: str) -> dict[int, dict[str, Any]]:
    """Every index's columns for one table, in one round trip, keyed by ``index_id``.

    Returns ``{index_id: {"key": [...], "included": [...], "partition_column": str}}``. One query
    per table rather than three per index: a 60-table schema is otherwise several hundred round
    trips, and on a WAN link that is the difference between a minute and twenty.
    """
    grouped: dict[int, dict[str, Any]] = {}
    for row in rows(cursor, f"""
            SELECT ic.index_id, c.name, ic.is_descending_key, ic.is_included_column,
                   ic.key_ordinal, ic.index_column_id, ic.partition_ordinal
            FROM sys.index_columns ic
            JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
            WHERE ic.object_id = OBJECT_ID({_object(schema, table)})
            ORDER BY ic.index_id, ic.is_included_column, ic.key_ordinal, ic.index_column_id
        """):
        entry = grouped.setdefault(int(row["index_id"]),
                                   {"key": [], "included": [], "partition_column": ""})
        item = {"name": str(row["name"]),
                "is_descending_key": bool(row["is_descending_key"])}
        if row["is_included_column"]:
            entry["included"].append(item)
        else:
            entry["key"].append(item)
        if int(row["partition_ordinal"] or 0) > 0:
            entry["partition_column"] = str(row["name"])
    return grouped


def table_key_constraints(cursor: Any, schema: str, table: str,
                          columns_by_index: Mapping[int, Mapping[str, Any]] | None = None
                          ) -> list[dict[str, Any]]:
    """The PRIMARY KEY and UNIQUE constraints, with their columns and clustering."""
    columns_by_index = columns_by_index if columns_by_index is not None else index_columns(
        cursor, schema, table)
    result = []
    for row in rows(cursor, f"""
            SELECT k.name, k.type, i.index_id, i.type_desc
            FROM sys.key_constraints k
            JOIN sys.indexes i ON i.object_id = k.parent_object_id
                              AND i.index_id = k.unique_index_id
            WHERE k.parent_object_id = OBJECT_ID({_object(schema, table)})
            ORDER BY k.type DESC, k.name
        """):
        entry = columns_by_index.get(int(row["index_id"]), {})
        result.append({"name": str(row["name"]), "type": str(row["type"]).strip(),
                       "type_desc": str(row["type_desc"]),
                       "columns": list(entry.get("key") or ())})
    return result


def table_indexes(cursor: Any, schema: str, table: str,
                  columns_by_index: Mapping[int, Mapping[str, Any]] | None = None
                  ) -> list[dict[str, Any]]:
    """The standalone indexes — those not backing a PK or UNIQUE constraint — with storage.

    ``storage`` is the shape :func:`db_ops.lib.mssql_ddl.render_storage` reads, so an index on a
    partition scheme carries its scheme *and its partitioning column* out of here. Reading
    ``sys.indexes`` without ``sys.data_spaces`` is how a partitioned schema arrives unpartitioned
    with every statement reporting success.
    """
    columns_by_index = columns_by_index if columns_by_index is not None else index_columns(
        cursor, schema, table)
    result = []
    for row in rows(cursor, f"""
            SELECT i.name, i.index_id, i.is_unique, i.type_desc, i.filter_definition,
                   ds.name AS data_space, ds.type_desc AS data_space_type_desc
            FROM sys.indexes i
            LEFT JOIN sys.data_spaces ds ON ds.data_space_id = i.data_space_id
            WHERE i.object_id = OBJECT_ID({_object(schema, table)})
              AND i.index_id > 0 AND i.is_primary_key = 0 AND i.is_unique_constraint = 0
              AND i.name IS NOT NULL
            ORDER BY i.name
        """):
        entry = columns_by_index.get(int(row["index_id"]), {})
        result.append({
            "name": str(row["name"]),
            "is_unique": bool(row["is_unique"]),
            "type_desc": str(row["type_desc"]),
            "filter_definition": row["filter_definition"],
            "key_columns": list(entry.get("key") or ()),
            "included_columns": list(entry.get("included") or ()),
            "storage": {"data_space": row["data_space"],
                        "data_space_type_desc": row["data_space_type_desc"],
                        "partition_column": entry.get("partition_column") or ""},
        })
    return result


def table_storage(cursor: Any, schema: str, table: str,
                  columns_by_index: Mapping[int, Mapping[str, Any]] | None = None
                  ) -> dict[str, Any]:
    """Where the table itself lives — its clustered index's data space, or the heap's."""
    columns_by_index = columns_by_index if columns_by_index is not None else index_columns(
        cursor, schema, table)
    found = rows(cursor, f"""
        SELECT i.index_id, ds.name AS data_space, ds.type_desc AS data_space_type_desc
        FROM sys.indexes i
        LEFT JOIN sys.data_spaces ds ON ds.data_space_id = i.data_space_id
        WHERE i.object_id = OBJECT_ID({_object(schema, table)}) AND i.index_id IN (0, 1)
    """)
    if not found:
        return {}
    row = found[0]
    entry = columns_by_index.get(int(row["index_id"]), {})
    return {"data_space": row["data_space"],
            "data_space_type_desc": row["data_space_type_desc"],
            "partition_column": entry.get("partition_column") or ""}


def table_check_constraints(cursor: Any, schema: str, table: str) -> list[dict[str, Any]]:
    """CHECK constraints, with the trust state the source has them in."""
    return [{"name": str(row["name"]), "definition": str(row["definition"]),
             "is_not_trusted": bool(row["is_not_trusted"]),
             "is_disabled": bool(row["is_disabled"])}
            for row in rows(cursor, f"""
                SELECT name, definition, is_not_trusted, is_disabled
                FROM sys.check_constraints
                WHERE parent_object_id = OBJECT_ID({_object(schema, table)})
                ORDER BY name
            """)]


def foreign_keys(cursor: Any, schema: str) -> list[dict[str, Any]]:
    """Every foreign key whose *parent* table is in this schema, with both column lists.

    The referenced table may be in another schema, and its own schema is carried, because a copy
    that assumes both sides are in the scope it was given produces a constraint pointing at the
    wrong table when they are not.
    """
    columns: dict[int, list[dict[str, str]]] = {}
    for row in rows(cursor, f"""
            SELECT fc.constraint_object_id,
                   pc.name AS parent_column, rc.name AS referenced_column
            FROM sys.foreign_key_columns fc
            JOIN sys.foreign_keys f ON f.object_id = fc.constraint_object_id
            JOIN sys.tables t ON t.object_id = f.parent_object_id
            JOIN sys.schemas s ON s.schema_id = t.schema_id
            JOIN sys.columns pc ON pc.object_id = fc.parent_object_id
                               AND pc.column_id = fc.parent_column_id
            JOIN sys.columns rc ON rc.object_id = fc.referenced_object_id
                               AND rc.column_id = fc.referenced_column_id
            WHERE s.name = {_s(schema)}
            ORDER BY fc.constraint_object_id, fc.constraint_column_id
        """):
        columns.setdefault(int(row["constraint_object_id"]), []).append(
            {"parent": str(row["parent_column"]), "referenced": str(row["referenced_column"])})

    result = []
    for row in rows(cursor, f"""
            SELECT f.object_id, f.name, t.name AS parent_table,
                   SCHEMA_NAME(rt.schema_id) AS referenced_schema, rt.name AS referenced_table,
                   f.delete_referential_action_desc AS delete_action,
                   f.update_referential_action_desc AS update_action,
                   f.is_not_trusted, f.is_disabled
            FROM sys.foreign_keys f
            JOIN sys.tables t ON t.object_id = f.parent_object_id
            JOIN sys.schemas s ON s.schema_id = t.schema_id
            JOIN sys.tables rt ON rt.object_id = f.referenced_object_id
            WHERE s.name = {_s(schema)}
            ORDER BY f.name
        """):
        pairs = columns.get(int(row["object_id"]), [])
        result.append({
            "name": str(row["name"]),
            "table": str(row["parent_table"]),
            "columns": [pair["parent"] for pair in pairs],
            "referenced_schema": str(row["referenced_schema"]),
            "referenced_table": str(row["referenced_table"]),
            "referenced_columns": [pair["referenced"] for pair in pairs],
            "delete_action": str(row["delete_action"]),
            "update_action": str(row["update_action"]),
            "is_not_trusted": bool(row["is_not_trusted"]),
            "is_disabled": bool(row["is_disabled"]),
        })
    return result


# ------------------------------------------------------------------------------- modules


#: Module kinds, in the order they must be created. Functions before views before procedures:
#: a view over a missing view fails at create time, while a procedure's references are resolved
#: late and do not. The dependency retry in the copy handles the rest — this only reduces how
#: many passes it needs.
MODULE_TYPE_ORDER = ("FN", "IF", "TF", "V", "P")


def modules(cursor: Any, schema: str) -> list[dict[str, Any]]:
    """The schema's programmable objects with their definitions, in creation order."""
    order = ", ".join(f"WHEN {_s(kind)} THEN {position}"
                      for position, kind in enumerate(MODULE_TYPE_ORDER))
    return [{"name": str(row["name"]), "type": str(row["type"]).strip(),
             "type_desc": str(row["type_desc"]), "definition": str(row["definition"] or "")}
            for row in rows(cursor, f"""
                SELECT o.name, o.type, o.type_desc, m.definition
                FROM sys.sql_modules m
                JOIN sys.objects o ON o.object_id = m.object_id
                JOIN sys.schemas s ON s.schema_id = o.schema_id
                WHERE s.name = {_s(schema)} AND o.is_ms_shipped = 0
                  AND RTRIM(o.type) IN ({', '.join(_s(kind) for kind in MODULE_TYPE_ORDER)})
                ORDER BY CASE RTRIM(o.type) {order} ELSE 99 END, o.name
            """)]


# ---------------------------------------------------------------------------- partitioning


def partition_schemes_used(cursor: Any, schema: str) -> list[dict[str, str]]:
    """The partition schemes this schema's indexes actually sit on, and their functions.

    Scoped to what is used rather than to the whole database on purpose: a copy should carry the
    partitioning its own tables need and not silently recreate every scheme on the source.
    """
    return [{"scheme": str(row["scheme"]), "function": str(row["function"])}
            for row in rows(cursor, f"""
                SELECT DISTINCT ps.name AS scheme, pf.name AS [function]
                FROM sys.indexes i
                JOIN sys.tables t ON t.object_id = i.object_id
                JOIN sys.schemas s ON s.schema_id = t.schema_id
                JOIN sys.partition_schemes ps ON ps.data_space_id = i.data_space_id
                JOIN sys.partition_functions pf ON pf.function_id = ps.function_id
                WHERE s.name = {_s(schema)}
                ORDER BY 1
            """)]


def partition_function(cursor: Any, name: str) -> dict[str, Any]:
    """One partition function: its parameter type, its direction, and its boundary values."""
    found = rows(cursor, f"""
        SELECT pf.name, pf.boundary_value_on_right, pf.fanout,
               ty.name AS type_name, pp.max_length, pp.precision, pp.scale
        FROM sys.partition_functions pf
        JOIN sys.partition_parameters pp ON pp.function_id = pf.function_id
        JOIN sys.types ty ON ty.user_type_id = pp.user_type_id
        WHERE pf.name = {_s(name)}
    """)
    if not found:
        raise SchemaCatalogError(f"partition function {name!r} not found.")
    row = found[0]
    values = [next(iter(item.values())) for item in rows(cursor, f"""
        SELECT v.value
        FROM sys.partition_range_values v
        JOIN sys.partition_functions f ON f.function_id = v.function_id
        WHERE f.name = {_s(name)}
        ORDER BY v.boundary_id
    """)]
    return {
        "name": str(row["name"]),
        "boundary_right": bool(row["boundary_value_on_right"]),
        "parameter_type": mssql_ddl.render_type(row),
        "values": values,
        "fanout": int(row["fanout"] or 0),
    }


def partition_scheme(cursor: Any, name: str) -> dict[str, Any]:
    """One partition scheme: which function it partitions by, and its filegroups in order."""
    found = rows(cursor, f"""
        SELECT ps.name, pf.name AS function_name
        FROM sys.partition_schemes ps
        JOIN sys.partition_functions pf ON pf.function_id = ps.function_id
        WHERE ps.name = {_s(name)}
    """)
    if not found:
        raise SchemaCatalogError(f"partition scheme {name!r} not found.")
    filegroups = [str(next(iter(item.values()))) for item in rows(cursor, f"""
        SELECT ds.name
        FROM sys.destination_data_spaces dds
        JOIN sys.partition_schemes ps ON ps.data_space_id = dds.partition_scheme_id
        JOIN sys.data_spaces ds ON ds.data_space_id = dds.data_space_id
        WHERE ps.name = {_s(name)}
        ORDER BY dds.destination_id
    """)]
    return {"name": str(found[0]["name"]),
            "function": str(found[0]["function_name"]),
            "filegroups": filegroups}


def partitioned_index_count(cursor: Any, schema: str) -> int:
    """How many of this schema's indexes sit on a partition scheme — the number to compare."""
    return int(scalar(cursor, f"""
        SELECT COUNT(*)
        FROM sys.indexes i
        JOIN sys.tables t ON t.object_id = i.object_id
        JOIN sys.schemas s ON s.schema_id = t.schema_id
        JOIN sys.data_spaces d ON d.data_space_id = i.data_space_id
        WHERE s.name = {_s(schema)} AND d.type_desc = 'PARTITION_SCHEME'
    """) or 0)


# -------------------------------------------------------------------------- change tracking


def change_tracking_database(cursor: Any) -> dict[str, Any] | None:
    """The database's change-tracking settings, or ``None`` when it is off."""
    found = rows(cursor, """
        SELECT retention_period, retention_period_units_desc, is_auto_cleanup_on
        FROM sys.change_tracking_databases
        WHERE database_id = DB_ID()
    """)
    if not found:
        return None
    row = found[0]
    return {"retention": int(row["retention_period"] or 0),
            "retention_units": str(row["retention_period_units_desc"]),
            "auto_cleanup": bool(row["is_auto_cleanup_on"])}


def change_tracking_tables(cursor: Any, schema: str) -> list[dict[str, Any]]:
    """Which of the schema's tables have change tracking on, and with what option.

    Carried because it is a *create-time* dependency, not a runtime nicety: a procedure whose body
    calls ``CHANGETABLE()`` over a table without tracking fails to create with Msg 22105, and that
    failure lands in the middle of the modules phase where it reads as a broken procedure.
    """
    return [{"table": str(row["name"]),
             "track_columns_updated": bool(row["is_track_columns_updated_on"])}
            for row in rows(cursor, f"""
                SELECT t.name, ct.is_track_columns_updated_on
                FROM sys.change_tracking_tables ct
                JOIN sys.tables t ON t.object_id = ct.object_id
                JOIN sys.schemas s ON s.schema_id = t.schema_id
                WHERE s.name = {_s(schema)}
                ORDER BY t.name
            """)]


# --------------------------------------------------------------- what will NOT be carried


#: The features a ``sys.tables``-based copy does not reproduce, each with the query that finds
#: them. Ordered by how expensive it is to discover them the other way — the first two have each
#: already broken a real deployment.
#:
#: ``{schema}`` is substituted with the quoted schema name. A probe that raises (because the
#: catalogue view does not exist on this version) is reported as *unknown*, never as *none*.
UNSUPPORTED_PROBES: tuple[dict[str, str], ...] = (
    {"feature": "temporal_tables",
     "note": "system-versioned tables need their history table and PERIOD clause; not scripted.",
     "sql": "SELECT t.name FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id "
            "WHERE s.name = {schema} AND t.temporal_type <> 0"},
    {"feature": "memory_optimized_tables",
     "note": "MEMORY_OPTIMIZED tables need a memory-optimized filegroup on the destination.",
     "sql": "SELECT t.name FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id "
            "WHERE s.name = {schema} AND t.is_memory_optimized = 1"},
    {"feature": "data_compression",
     "note": "ROW/PAGE/COLUMNSTORE compression is not carried; the copy lands uncompressed.",
     "sql": "SELECT DISTINCT t.name FROM sys.partitions p "
            "JOIN sys.tables t ON t.object_id = p.object_id "
            "JOIN sys.schemas s ON s.schema_id = t.schema_id "
            "WHERE s.name = {schema} AND p.data_compression <> 0"},
    {"feature": "non_primary_filegroups",
     "note": "an index on a filegroup other than PRIMARY needs that filegroup to exist first; "
             "use map_filegroups to redirect it.",
     "sql": "SELECT DISTINCT d.name FROM sys.indexes i "
            "JOIN sys.tables t ON t.object_id = i.object_id "
            "JOIN sys.schemas s ON s.schema_id = t.schema_id "
            "JOIN sys.data_spaces d ON d.data_space_id = i.data_space_id "
            "WHERE s.name = {schema} AND d.type_desc = 'ROWS_FILEGROUP' AND d.name <> 'PRIMARY'"},
    {"feature": "triggers",
     "note": "DML triggers on the schema's tables are not copied.",
     "sql": "SELECT tr.name FROM sys.triggers tr "
            "JOIN sys.tables t ON t.object_id = tr.parent_id "
            "JOIN sys.schemas s ON s.schema_id = t.schema_id WHERE s.name = {schema}"},
    {"feature": "extended_properties",
     "note": "descriptions and MS_Description properties are not carried.",
     "sql": "SELECT DISTINCT ep.name FROM sys.extended_properties ep "
            "JOIN sys.objects o ON o.object_id = ep.major_id "
            "JOIN sys.schemas s ON s.schema_id = o.schema_id "
            "WHERE s.name = {schema} AND ep.class = 1"},
    {"feature": "object_permissions",
     "note": "GRANT/DENY on the schema's objects are not carried; grant them on the destination.",
     "sql": "SELECT DISTINCT o.name FROM sys.database_permissions dp "
            "JOIN sys.objects o ON o.object_id = dp.major_id "
            "JOIN sys.schemas s ON s.schema_id = o.schema_id "
            "WHERE s.name = {schema} AND dp.class = 1"},
    {"feature": "sequences",
     "note": "sequence objects and their current values are not copied.",
     "sql": "SELECT sq.name FROM sys.sequences sq "
            "JOIN sys.schemas s ON s.schema_id = sq.schema_id WHERE s.name = {schema}"},
    {"feature": "user_defined_types",
     "note": "table types and alias types must exist on the destination before the modules that "
             "declare them.",
     "sql": "SELECT ty.name FROM sys.types ty "
            "JOIN sys.schemas s ON s.schema_id = ty.schema_id "
            "WHERE s.name = {schema} AND ty.is_user_defined = 1"},
    {"feature": "synonyms",
     "note": "synonyms are not copied; a module referencing one will not resolve.",
     "sql": "SELECT sy.name FROM sys.synonyms sy "
            "JOIN sys.schemas s ON s.schema_id = sy.schema_id WHERE s.name = {schema}"},
    {"feature": "clr_modules",
     "note": "assembly-backed procedures and functions need their assembly deployed first.",
     "sql": "SELECT o.name FROM sys.assembly_modules am "
            "JOIN sys.objects o ON o.object_id = am.object_id "
            "JOIN sys.schemas s ON s.schema_id = o.schema_id WHERE s.name = {schema}"},
    {"feature": "fulltext_indexes",
     "note": "full-text indexes and their catalogues are not copied.",
     "sql": "SELECT t.name FROM sys.fulltext_indexes fi "
            "JOIN sys.tables t ON t.object_id = fi.object_id "
            "JOIN sys.schemas s ON s.schema_id = t.schema_id WHERE s.name = {schema}"},
    {"feature": "xml_and_spatial_indexes",
     "note": "XML and spatial indexes are not scripted from sys.index_columns.",
     "sql": "SELECT i.name FROM sys.indexes i "
            "JOIN sys.tables t ON t.object_id = i.object_id "
            "JOIN sys.schemas s ON s.schema_id = t.schema_id "
            "WHERE s.name = {schema} AND i.type IN (3, 4)"},
    {"feature": "masked_columns",
     "note": "dynamic data masking is not carried; the copy exposes the raw values.",
     "sql": "SELECT c.name FROM sys.masked_columns c "
            "JOIN sys.tables t ON t.object_id = c.object_id "
            "JOIN sys.schemas s ON s.schema_id = t.schema_id WHERE s.name = {schema}"},
    {"feature": "encrypted_columns",
     "note": "Always Encrypted columns need their column encryption keys on the destination.",
     "sql": "SELECT c.name FROM sys.columns c "
            "JOIN sys.tables t ON t.object_id = c.object_id "
            "JOIN sys.schemas s ON s.schema_id = t.schema_id "
            "WHERE s.name = {schema} AND c.encryption_type IS NOT NULL"},
    {"feature": "graph_tables",
     "note": "node and edge tables carry hidden graph columns that are not scripted.",
     "sql": "SELECT t.name FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id "
            "WHERE s.name = {schema} AND (t.is_node = 1 OR t.is_edge = 1)"},
    {"feature": "filetables",
     "note": "FileTables need FILESTREAM configured on the destination instance.",
     "sql": "SELECT t.name FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id "
            "WHERE s.name = {schema} AND t.is_filetable = 1"},
)


def unsupported_features(cursor: Any, schema: str,
                         probes: Sequence[Mapping[str, str]] = UNSUPPORTED_PROBES
                         ) -> list[dict[str, Any]]:
    """What is present in this schema that a copy will **not** carry.

    Every entry that comes back with objects is a thing the operator has to do by hand or decide
    to live without. The request asked for this mode explicitly, and it is worth as much as the
    copying: the alternative to a list is finding out one feature at a time, each time as a failed
    deployment.

    Entries with no objects are omitted. Entries whose probe could not run — an older SQL Server
    without that catalogue view — are returned with ``"status": "unknown"``, because that is not
    the same answer as "none".
    """
    findings: list[dict[str, Any]] = []
    for probe in probes:
        sql = str(probe["sql"]).format(schema=_s(schema))
        try:
            found = [str(next(iter(row.values()))) for row in rows(cursor, sql)]
        except SchemaCatalogError as exc:
            findings.append({"feature": probe["feature"], "status": "unknown",
                             "objects": [], "count": 0, "note": str(probe["note"]),
                             "detail": str(exc)})
            continue
        if found:
            findings.append({"feature": probe["feature"], "status": "present",
                             "objects": sorted(found), "count": len(found),
                             "note": str(probe["note"])})
    return findings


# ----------------------------------------------------------------------------- comparison


def schema_fingerprint(cursor: Any, schema: str) -> dict[str, int]:
    """The counts that say whether two tiers hold the same schema.

    Deliberately counts rather than a hash. A hash answers "identical or not" and nothing else;
    when a copy comes up short, ``indexes: 41 vs 9`` names the phase that failed and a differing
    digest does not. ``partitioned_indexes`` is in here because it is the number that was
    **0 of 32** after a copy everything else reported as clean.
    """
    counts = {
        "tables": f"SELECT COUNT(*) FROM sys.tables t JOIN sys.schemas s "
                  f"ON s.schema_id = t.schema_id WHERE s.name = {_s(schema)}",
        "columns": f"SELECT COUNT(*) FROM sys.columns c JOIN sys.tables t "
                   f"ON t.object_id = c.object_id JOIN sys.schemas s "
                   f"ON s.schema_id = t.schema_id WHERE s.name = {_s(schema)}",
        "indexes": f"SELECT COUNT(*) FROM sys.indexes i JOIN sys.tables t "
                   f"ON t.object_id = i.object_id JOIN sys.schemas s "
                   f"ON s.schema_id = t.schema_id WHERE s.name = {_s(schema)} AND i.index_id > 0",
        "check_constraints": f"SELECT COUNT(*) FROM sys.check_constraints k JOIN sys.tables t "
                             f"ON t.object_id = k.parent_object_id JOIN sys.schemas s "
                             f"ON s.schema_id = t.schema_id WHERE s.name = {_s(schema)}",
        "foreign_keys": f"SELECT COUNT(*) FROM sys.foreign_keys f JOIN sys.tables t "
                        f"ON t.object_id = f.parent_object_id JOIN sys.schemas s "
                        f"ON s.schema_id = t.schema_id WHERE s.name = {_s(schema)}",
        "untrusted_foreign_keys": f"SELECT COUNT(*) FROM sys.foreign_keys f JOIN sys.tables t "
                                  f"ON t.object_id = f.parent_object_id JOIN sys.schemas s "
                                  f"ON s.schema_id = t.schema_id WHERE s.name = {_s(schema)} "
                                  f"AND f.is_not_trusted = 1",
        "procedures": f"SELECT COUNT(*) FROM sys.procedures p JOIN sys.schemas s "
                      f"ON s.schema_id = p.schema_id WHERE s.name = {_s(schema)}",
        "views": f"SELECT COUNT(*) FROM sys.views v JOIN sys.schemas s "
                 f"ON s.schema_id = v.schema_id WHERE s.name = {_s(schema)}",
        "functions": f"SELECT COUNT(*) FROM sys.objects o JOIN sys.schemas s "
                     f"ON s.schema_id = o.schema_id WHERE s.name = {_s(schema)} "
                     f"AND RTRIM(o.type) IN ('FN', 'IF', 'TF')",
        "change_tracked_tables": f"SELECT COUNT(*) FROM sys.change_tracking_tables ct "
                                 f"JOIN sys.tables t ON t.object_id = ct.object_id "
                                 f"JOIN sys.schemas s ON s.schema_id = t.schema_id "
                                 f"WHERE s.name = {_s(schema)}",
    }
    result = {name: int(scalar(cursor, sql) or 0) for name, sql in counts.items()}
    result["partitioned_indexes"] = partitioned_index_count(cursor, schema)
    return result


def compare_fingerprints(source: Mapping[str, int], destination: Mapping[str, int],
                         *, expect_equal: Iterable[str] = ()) -> list[dict[str, Any]]:
    """Source vs destination, one row per count, each marked ``match`` or ``differs``.

    ``expect_equal`` narrows what counts as a failure — a copy that deliberately skipped six
    staging tables *should* differ on ``tables``, and reporting that as a fault trains the reader
    to ignore the report. Anything not named is reported with ``"expected": false``.
    """
    wanted = set(expect_equal)
    result = []
    for key in sorted(set(source) | set(destination)):
        left, right = int(source.get(key, 0)), int(destination.get(key, 0))
        result.append({"count": key, "source": left, "destination": right,
                       "match": left == right, "expected_equal": key in wanted,
                       "status": ("match" if left == right
                                  else ("MISMATCH" if key in wanted else "differs"))})
    return result
