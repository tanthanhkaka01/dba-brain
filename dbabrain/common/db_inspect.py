"""What is *inside* a database and what it is doing now: a table, an object's source, the activity, a plan (1.1.0).

The questions an agent asks next once it knows which database - and, without a command for them,
guessed: at least 8 of 22 failed MCP `run-sql` calls on 2026-11-22 were *Invalid column name*, the
model writing a column it had not read. Four commands, each one SQL of this module's per engine, run
through :mod:`dbabrain.common.sql_run` like every catalog command (:mod:`dbabrain.common.db_catalog`):

* ``table-info`` - one table's columns, its indexes, its rows and its size;
* ``object-source`` - the text of a procedure, function, view or trigger;
* ``db-activity`` - ``requests`` running now, ``blocking``, ``waits`` now, ``top_queries`` since the
  plans were cached;
* ``query-plan`` - the estimated plan of one statement: **compiled, never run** (SQL Server
  ``SHOWPLAN_XML``, PostgreSQL and MySQL ``EXPLAIN`` without ``ANALYZE``). Oracle has none here:
  ``EXPLAIN PLAN`` writes to a table.

Each runs with the rights of the login the request states (rules R09) - the one the node stores for
the server. A name the request gives is checked to be a name and written in as a literal; nothing
else of the request reaches the SQL, but for ``query-plan``'s statement.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from typing import Any

from dbabrain.common import db_catalog, sql_run
from dbabrain.lib import errors
from dbabrain.lib.sql_text import split_postgresql_statements


class DbInspectError(errors.RequestError):
    """A request this module cannot answer: no object, an engine it does not cover, a statement it will not compile."""


#: A name as a person writes it: `schema.table`, `[dbo].[Orders]`, `"Sales"."Order Lines"`. No quote of the
#: literal kind, no comment, no separator - it is written into the SQL as a literal.
_NAME = re.compile(r"""^[\w$#@ .\[\]"`-]{1,256}$""", re.UNICODE)
VIEWS = ("requests", "blocking", "waits", "top_queries")
DEFAULT_MAX_ROWS = 50
MAX_PLAN_CHARS = 200_000


def _literal(value: str) -> str:
    return value.replace("'", "''")


def _object(request: dict[str, Any]) -> tuple[str, str, str]:
    """``(whole, schema, name)`` - each part's brackets or quotes taken off."""
    raw = str(request.get("object") or "").strip()
    if not raw:
        raise DbInspectError('"object" is required: the name, `schema.name` or `name`.')
    if not _NAME.match(raw) or "--" in raw:
        raise DbInspectError(f"object {raw!r} is not a name: letters, digits, _ $ # @ - space, dots, and "
                             "[ ] \" ` around a part.")
    parts = [part.strip().strip('[]"`') for part in _split_name(raw)]
    parts = [part for part in parts if part]
    if not parts or len(parts) > 3:
        raise DbInspectError(f"object {raw!r}: `name`, `schema.name` or `database.schema.name`.")
    name = parts[-1]
    schema = parts[-2] if len(parts) >= 2 else ""
    return raw, schema, name


def _split_name(raw: str) -> list[str]:
    """``raw`` cut at the dots that are not inside ``[...]``, ``"..."`` or a backtick pair."""
    parts, current, closing = [], "", ""
    for char in raw:
        if closing:
            current += char
            if char == closing:
                closing = ""
        elif char in '["`':
            closing = {"[": "]", '"': '"', "`": "`"}[char]
            current += char
        elif char == ".":
            parts.append(current)
            current = ""
        else:
            current += char
    return parts + [current]


def _run(parsed: dict[str, Any], sql: str, *, database: str = "", max_rows: int = 0) -> list[list[dict[str, Any]]]:
    """Every result set of ``sql`` (its statements separated by GO lines), as lists of dicts, lower-case keys."""
    request: dict[str, Any] = {"target": parsed["target"], "connection": parsed["connection"], "sql": sql,
                               "sql_access": parsed["sql_access"], "capture": "all", "max_result_sets": 0}
    if database:
        request["database"] = database
    if max_rows:
        request["max_rows"] = max_rows
    if parsed["timeout_seconds"]:
        request["timeout_seconds"] = parsed["timeout_seconds"]
    try:
        result = sql_run.json_safe_result(sql_run.run_sql(request))
    except sql_run.SqlRunError as exc:
        raise DbInspectError(str(exc)) from exc
    sets = []
    for item in result.get("result_sets") or []:
        columns = [str(name).lower() for name in item.get("columns") or []]
        sets.append([dict(zip(columns, row)) for row in item.get("rows") or []])
    return sets


def _setup(request: Any, command: str) -> tuple[dict[str, Any], dict[str, Any], str]:
    try:
        parsed = db_catalog._parsed_request(request)  # noqa: SLF001 - the one parser of a catalog request
        resolved = db_catalog._resolve(parsed)  # noqa: SLF001
    except db_catalog.DbCatalogError as exc:
        raise DbInspectError(str(exc)) from exc
    return parsed, resolved, str(resolved["db_type"])


def _head(resolved: dict[str, Any], db_type: str, database: str) -> dict[str, Any]:
    return {"server_id": resolved["server_id"], "db_type": db_type,
            "database_name": resolved.get("database_name", "") or database,
            "credential_name": resolved.get("credential_name", ""), "username": resolved.get("username", "")}


# --------------------------------------------------------------------------- #
# table-info
# --------------------------------------------------------------------------- #
def _table_sql(db_type: str, whole: str, schema: str, name: str) -> str:
    w, s, n = _literal(whole), _literal(schema), _literal(name)
    if db_type == "sqlserver":
        return f"""
SELECT OBJECT_SCHEMA_NAME(c.object_id) AS schema_name, OBJECT_NAME(c.object_id) AS table_name, c.column_id AS ordinal,
       c.name AS column_name, TYPE_NAME(c.user_type_id) AS data_type,
       CASE WHEN TYPE_NAME(c.user_type_id) IN ('nvarchar', 'nchar') AND c.max_length > 0 THEN c.max_length / 2
            ELSE c.max_length END AS max_length,
       c.precision AS numeric_precision, c.scale AS numeric_scale, CAST(c.is_nullable AS int) AS is_nullable,
       CAST(c.is_identity AS int) AS is_identity, CAST(c.is_computed AS int) AS is_computed, dc.definition AS default_value
FROM sys.columns c LEFT JOIN sys.default_constraints dc ON dc.object_id = c.default_object_id
WHERE c.object_id = OBJECT_ID(N'{w}') ORDER BY c.column_id
GO
SELECT i.name AS index_name, i.type_desc AS index_type, CAST(i.is_unique AS int) AS is_unique,
       CAST(i.is_primary_key AS int) AS is_primary_key, CAST(i.is_disabled AS int) AS is_disabled,
       STUFF((SELECT ', ' + c.name + CASE WHEN ic.is_descending_key = 1 THEN ' DESC' ELSE '' END
              FROM sys.index_columns ic JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
              WHERE ic.object_id = i.object_id AND ic.index_id = i.index_id AND ic.is_included_column = 0
              ORDER BY ic.key_ordinal FOR XML PATH(''), TYPE).value('.', 'nvarchar(max)'), 1, 2, '') AS key_columns,
       STUFF((SELECT ', ' + c.name
              FROM sys.index_columns ic JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
              WHERE ic.object_id = i.object_id AND ic.index_id = i.index_id AND ic.is_included_column = 1
              ORDER BY ic.index_column_id FOR XML PATH(''), TYPE).value('.', 'nvarchar(max)'), 1, 2, '') AS included_columns,
       i.filter_definition AS filter
FROM sys.indexes i WHERE i.object_id = OBJECT_ID(N'{w}') AND i.index_id > 0 ORDER BY i.index_id
GO
SELECT SUM(CASE WHEN ps.index_id IN (0, 1) THEN ps.row_count ELSE 0 END) AS row_count,
       CAST(SUM(ps.reserved_page_count) * 8 / 1024.0 AS decimal(18, 1)) AS reserved_mb
FROM sys.dm_db_partition_stats ps WHERE ps.object_id = OBJECT_ID(N'{w}')
"""
    if db_type == "postgresql":
        rel = f"to_regclass('{w}')"
        return f"""
SELECT n.nspname AS schema_name, c.relname AS table_name, a.attnum AS ordinal, a.attname AS column_name,
       format_type(a.atttypid, a.atttypmod) AS data_type, NULL AS max_length, NULL AS numeric_precision,
       NULL AS numeric_scale, CASE WHEN a.attnotnull THEN 0 ELSE 1 END AS is_nullable,
       CASE WHEN a.attidentity <> '' THEN 1 ELSE 0 END AS is_identity,
       CASE WHEN a.attgenerated <> '' THEN 1 ELSE 0 END AS is_computed, pg_get_expr(d.adbin, d.adrelid) AS default_value
FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
WHERE a.attrelid = {rel} AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum
GO
SELECT ic.relname AS index_name, am.amname AS index_type, CASE WHEN x.indisunique THEN 1 ELSE 0 END AS is_unique,
       CASE WHEN x.indisprimary THEN 1 ELSE 0 END AS is_primary_key, CASE WHEN x.indisvalid THEN 0 ELSE 1 END AS is_disabled,
       pg_get_indexdef(x.indexrelid) AS key_columns, NULL AS included_columns, pg_get_expr(x.indpred, x.indrelid) AS filter
FROM pg_index x JOIN pg_class ic ON ic.oid = x.indexrelid JOIN pg_am am ON am.oid = ic.relam
WHERE x.indrelid = {rel} ORDER BY ic.relname
GO
SELECT c.reltuples::bigint AS row_count, round(pg_total_relation_size(c.oid) / 1048576.0, 1) AS reserved_mb
FROM pg_class c WHERE c.oid = {rel}
"""
    if db_type == "oracle":
        owner = f"'{s.upper()}'" if s else "USER"
        tab = f"'{n.upper()}'"
        return f"""
SELECT owner AS schema_name, table_name, column_id AS ordinal, column_name, data_type, char_length AS max_length,
       data_precision AS numeric_precision, data_scale AS numeric_scale,
       CASE nullable WHEN 'Y' THEN 1 ELSE 0 END AS is_nullable, 0 AS is_identity, 0 AS is_computed,
       NULL AS default_value
FROM all_tab_columns WHERE owner = {owner} AND table_name = {tab} ORDER BY column_id
GO
SELECT i.index_name, i.index_type, CASE i.uniqueness WHEN 'UNIQUE' THEN 1 ELSE 0 END AS is_unique, 0 AS is_primary_key,
       CASE i.status WHEN 'UNUSABLE' THEN 1 ELSE 0 END AS is_disabled,
       (SELECT LISTAGG(ic.column_name, ', ') WITHIN GROUP (ORDER BY ic.column_position) FROM all_ind_columns ic
        WHERE ic.index_owner = i.owner AND ic.index_name = i.index_name) AS key_columns,
       NULL AS included_columns, NULL AS filter
FROM all_indexes i WHERE i.table_owner = {owner} AND i.table_name = {tab} ORDER BY i.index_name
GO
SELECT t.num_rows AS row_count,
       -- A login's own segments only: DBA_SEGMENTS needs a grant, and there is no ALL_SEGMENTS.
       (SELECT ROUND(SUM(sg.bytes) / 1048576, 1) FROM user_segments sg WHERE t.owner = USER
        AND sg.segment_name = t.table_name) AS reserved_mb
FROM all_tables t WHERE t.owner = {owner} AND t.table_name = {tab}
"""
    if db_type == "mysql":
        sch = f"'{s}'" if s else "DATABASE()"
        return f"""
SELECT table_schema AS schema_name, table_name, ordinal_position AS ordinal, column_name, column_type AS data_type,
       character_maximum_length AS max_length, numeric_precision, numeric_scale,
       CASE is_nullable WHEN 'YES' THEN 1 ELSE 0 END AS is_nullable,
       CASE WHEN extra LIKE '%auto_increment%' THEN 1 ELSE 0 END AS is_identity,
       CASE WHEN extra LIKE '%GENERATED%' THEN 1 ELSE 0 END AS is_computed, column_default AS default_value
FROM information_schema.columns WHERE table_schema = {sch} AND table_name = '{n}' ORDER BY ordinal_position
GO
SELECT index_name, MAX(index_type) AS index_type, MAX(1 - non_unique) AS is_unique,
       MAX(CASE WHEN index_name = 'PRIMARY' THEN 1 ELSE 0 END) AS is_primary_key, 0 AS is_disabled,
       GROUP_CONCAT(column_name ORDER BY seq_in_index SEPARATOR ', ') AS key_columns, NULL AS included_columns, NULL AS filter
FROM information_schema.statistics WHERE table_schema = {sch} AND table_name = '{n}' GROUP BY index_name ORDER BY index_name
GO
SELECT table_rows AS row_count, ROUND((data_length + index_length) / 1048576, 1) AS reserved_mb
FROM information_schema.tables WHERE table_schema = {sch} AND table_name = '{n}'
"""
    raise DbInspectError(f"table-info does not know engine {db_type!r}; supported: sqlserver, postgresql, oracle, mysql.")


def table_info(request: Any) -> dict[str, Any]:
    """One table's columns, indexes, rows and size - what to read before writing a query against it."""
    parsed, resolved, db_type = _setup(request, "table-info")
    whole, schema, name = _object(request)
    database = parsed["database"]
    sets = _run(parsed, _table_sql(db_type, whole, schema, name), database=database, max_rows=5000)
    columns, indexes, size = (sets + [[], [], []])[:3]
    if not columns:
        raise DbInspectError(f"no table or view {whole!r} in {database or 'the login default database'} on "
                             f"{resolved['server_id']} that this login can see.")
    first = columns[0]
    return {**_head(resolved, db_type, database),
            "object": f"{first.get('schema_name') or schema}.{first.get('table_name') or name}",
            "columns": columns, "indexes": indexes, "column_count": len(columns), "index_count": len(indexes),
            "row_count": (size[0] if size else {}).get("row_count"),
            "reserved_mb": (size[0] if size else {}).get("reserved_mb")}


# --------------------------------------------------------------------------- #
# object-source
# --------------------------------------------------------------------------- #
def _source_sql(db_type: str, whole: str, schema: str, name: str) -> str:
    w, s, n = _literal(whole), _literal(schema), _literal(name)
    if db_type == "sqlserver":
        return f"""
SELECT OBJECT_SCHEMA_NAME(o.object_id) + '.' + o.name AS object_name, o.type_desc AS object_type,
       OBJECT_DEFINITION(o.object_id) AS definition, o.modify_date AS modified_at
FROM sys.objects o WHERE o.object_id = OBJECT_ID(N'{w}')
"""
    if db_type == "postgresql":
        sch = f"n.nspname = '{s}'" if s else "n.nspname = ANY (current_schemas(false))"
        return f"""
SELECT n.nspname || '.' || p.proname AS object_name, CASE p.prokind WHEN 'p' THEN 'PROCEDURE' ELSE 'FUNCTION' END AS object_type,
       pg_get_functiondef(p.oid) AS definition, NULL AS modified_at
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE p.proname = '{n}' AND {sch} AND p.prokind IN ('f', 'p')
UNION ALL
SELECT n.nspname || '.' || c.relname, CASE c.relkind WHEN 'm' THEN 'MATERIALIZED VIEW' ELSE 'VIEW' END,
       pg_get_viewdef(c.oid, true), NULL
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.relname = '{n}' AND {sch} AND c.relkind IN ('v', 'm')
UNION ALL
SELECT n.nspname || '.' || t.tgname, 'TRIGGER', pg_get_triggerdef(t.oid, true), NULL
FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE t.tgname = '{n}' AND {sch} AND NOT t.tgisinternal
"""
    if db_type == "oracle":
        owner = f"'{s.upper()}'" if s else "USER"
        # Line by line from ALL_SOURCE, joined here: a CLOB (DBMS_METADATA) cannot be read once the
        # connection that fetched it is closed, and sql_run closes it before the rows are read.
        return f"""
SELECT s.owner || '.' || s.name AS object_name, s.type AS object_type, s.line, s.text AS definition,
       NULL AS modified_at
FROM all_source s WHERE s.owner = {owner} AND s.name = '{n.upper()}' ORDER BY s.type, s.line
GO
SELECT v.owner || '.' || v.view_name AS object_name, 'VIEW' AS object_type, 1 AS line, v.text_vc AS definition,
       NULL AS modified_at
FROM all_views v WHERE v.owner = {owner} AND v.view_name = '{n.upper()}'
"""
    if db_type == "mysql":
        sch = f"'{s}'" if s else "DATABASE()"
        return f"""
SELECT CONCAT(routine_schema, '.', routine_name) AS object_name, routine_type AS object_type,
       routine_definition AS definition, last_altered AS modified_at
FROM information_schema.routines WHERE routine_schema = {sch} AND routine_name = '{n}'
UNION ALL
SELECT CONCAT(table_schema, '.', table_name), 'VIEW', view_definition, NULL
FROM information_schema.views WHERE table_schema = {sch} AND table_name = '{n}'
UNION ALL
SELECT CONCAT(trigger_schema, '.', trigger_name), 'TRIGGER', action_statement, created
FROM information_schema.triggers WHERE trigger_schema = {sch} AND trigger_name = '{n}'
"""
    raise DbInspectError(f"object-source does not know engine {db_type!r}; supported: sqlserver, postgresql, oracle, mysql.")


def object_source(request: Any) -> dict[str, Any]:
    """The text of one procedure, function, view or trigger (every overload, on PostgreSQL)."""
    parsed, resolved, db_type = _setup(request, "object-source")
    whole, schema, name = _object(request)
    database = parsed["database"]
    sets = _run(parsed, _source_sql(db_type, whole, schema, name), database=database, max_rows=200000)
    if db_type == "oracle":
        joined: dict[tuple[str, str], dict[str, Any]] = {}
        for row in (row for rows in sets for row in rows):
            key = (str(row.get("object_name")), str(row.get("object_type")))
            item = joined.setdefault(key, {"object_name": key[0], "object_type": key[1], "definition": "",
                                           "modified_at": None})
            item["definition"] += str(row.get("definition") or "")
        rows = list(joined.values())
    else:
        rows = (sets or [[]])[0]
    rows = [row for row in rows if row.get("definition") not in (None, "")]
    if not rows:
        raise DbInspectError(f"no procedure, function, view or trigger {whole!r} with a readable definition in "
                             f"{database or 'the login default database'} on {resolved['server_id']} (an encrypted one "
                             "has none, and a login without VIEW DEFINITION sees none).")
    return {**_head(resolved, db_type, database), "object": whole, "objects": rows, "count": len(rows)}


# --------------------------------------------------------------------------- #
# db-activity
# --------------------------------------------------------------------------- #
_ACTIVITY = {
    ("sqlserver", "requests"): """
SELECT TOP ({n}) r.session_id, s.login_name, s.host_name, s.program_name, DB_NAME(r.database_id) AS database_name,
       r.status, r.command, r.wait_type, r.wait_time AS wait_ms, r.blocking_session_id, r.cpu_time AS cpu_ms,
       r.total_elapsed_time AS elapsed_ms, r.logical_reads, r.open_transaction_count AS open_tran,
       LEFT(t.text, 2000) AS sql_text
FROM sys.dm_exec_requests r JOIN sys.dm_exec_sessions s ON s.session_id = r.session_id
OUTER APPLY sys.dm_exec_sql_text(r.sql_handle) t
WHERE s.is_user_process = 1 AND r.session_id <> @@SPID ORDER BY r.total_elapsed_time DESC
""",
    ("sqlserver", "blocking"): """
SELECT TOP ({n}) r.session_id AS waiting_session_id, r.blocking_session_id, r.wait_type, r.wait_time AS wait_ms,
       DB_NAME(r.database_id) AS database_name, s.login_name, s.host_name, s.program_name,
       bs.login_name AS blocker_login, bs.host_name AS blocker_host, bs.program_name AS blocker_program,
       bs.status AS blocker_status, LEFT(t.text, 1000) AS waiting_sql, LEFT(bt.text, 1000) AS blocker_sql
FROM sys.dm_exec_requests r JOIN sys.dm_exec_sessions s ON s.session_id = r.session_id
LEFT JOIN sys.dm_exec_sessions bs ON bs.session_id = r.blocking_session_id
LEFT JOIN sys.dm_exec_connections bc ON bc.session_id = r.blocking_session_id
OUTER APPLY sys.dm_exec_sql_text(r.sql_handle) t OUTER APPLY sys.dm_exec_sql_text(bc.most_recent_sql_handle) bt
WHERE r.blocking_session_id > 0 ORDER BY r.wait_time DESC
""",
    ("sqlserver", "waits"): """
SELECT TOP ({n}) wt.wait_type, COUNT(*) AS waiting_tasks, MAX(wt.wait_duration_ms) AS max_wait_ms,
       SUM(wt.wait_duration_ms) AS total_wait_ms, MIN(wt.resource_description) AS a_resource
FROM sys.dm_os_waiting_tasks wt JOIN sys.dm_exec_sessions s ON s.session_id = wt.session_id
WHERE s.is_user_process = 1 GROUP BY wt.wait_type ORDER BY SUM(wt.wait_duration_ms) DESC
""",
    ("sqlserver", "top_queries"): """
SELECT TOP ({n}) qs.execution_count, qs.total_worker_time / 1000 AS total_cpu_ms,
       qs.total_worker_time / NULLIF(qs.execution_count, 0) / 1000 AS avg_cpu_ms,
       qs.total_elapsed_time / NULLIF(qs.execution_count, 0) / 1000 AS avg_elapsed_ms,
       qs.total_logical_reads / NULLIF(qs.execution_count, 0) AS avg_logical_reads, DB_NAME(t.dbid) AS database_name,
       qs.last_execution_time, LEFT(SUBSTRING(t.text, qs.statement_start_offset / 2 + 1,
         (CASE qs.statement_end_offset WHEN -1 THEN DATALENGTH(t.text) ELSE qs.statement_end_offset END
          - qs.statement_start_offset) / 2 + 1), 2000) AS sql_text
FROM sys.dm_exec_query_stats qs CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) t
ORDER BY qs.total_worker_time DESC
""",
    ("postgresql", "requests"): """
SELECT pid AS session_id, usename AS login_name, client_addr::text AS host_name, application_name AS program_name,
       datname AS database_name, state AS status, wait_event_type, wait_event,
       pg_blocking_pids(pid) AS blocking_session_ids, round(extract(epoch FROM now() - query_start) * 1000) AS elapsed_ms,
       round(extract(epoch FROM now() - xact_start)) AS open_tran_seconds, left(query, 2000) AS sql_text
FROM pg_stat_activity WHERE pid <> pg_backend_pid() AND backend_type = 'client backend' AND state <> 'idle'
ORDER BY query_start NULLS LAST LIMIT {n}
""",
    ("postgresql", "blocking"): """
SELECT w.pid AS waiting_session_id, b.pid AS blocking_session_id, w.wait_event_type, w.wait_event,
       round(extract(epoch FROM now() - w.query_start) * 1000) AS wait_ms, w.datname AS database_name,
       w.usename AS login_name, b.usename AS blocker_login, b.state AS blocker_status,
       left(w.query, 1000) AS waiting_sql, left(b.query, 1000) AS blocker_sql
FROM pg_stat_activity w JOIN LATERAL unnest(pg_blocking_pids(w.pid)) AS bp(pid) ON true
JOIN pg_stat_activity b ON b.pid = bp.pid ORDER BY w.query_start LIMIT {n}
""",
    ("postgresql", "waits"): """
SELECT wait_event_type || ':' || wait_event AS wait_type, count(*) AS waiting_tasks,
       max(round(extract(epoch FROM now() - query_start) * 1000)) AS max_wait_ms
FROM pg_stat_activity WHERE wait_event IS NOT NULL AND backend_type = 'client backend' AND state <> 'idle'
GROUP BY 1 ORDER BY 2 DESC LIMIT {n}
""",
    ("postgresql", "top_queries"): """
SELECT calls AS execution_count, round(total_exec_time) AS total_elapsed_ms, round(mean_exec_time) AS avg_elapsed_ms,
       rows, shared_blks_hit + shared_blks_read AS blocks, left(query, 2000) AS sql_text
FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT {n}
""",
    ("oracle", "requests"): """
SELECT s.sid AS session_id, s.username AS login_name, s.machine AS host_name, s.program AS program_name,
       s.status, s.event AS wait_type, s.wait_class, s.seconds_in_wait, s.blocking_session, s.last_call_et AS elapsed_seconds,
       SUBSTR(q.sql_text, 1, 1000) AS sql_text
FROM v$session s LEFT JOIN v$sql q ON q.sql_id = s.sql_id AND q.child_number = 0
WHERE s.type = 'USER' AND s.status = 'ACTIVE' AND s.sid <> SYS_CONTEXT('USERENV', 'SID') AND ROWNUM <= {n}
""",
    ("oracle", "blocking"): """
SELECT w.sid AS waiting_session_id, w.blocking_session AS blocking_session_id, w.event AS wait_type,
       w.seconds_in_wait, w.username AS login_name, b.username AS blocker_login, b.machine AS blocker_host,
       b.status AS blocker_status
FROM v$session w JOIN v$session b ON b.sid = w.blocking_session WHERE w.blocking_session IS NOT NULL AND ROWNUM <= {n}
""",
    ("oracle", "waits"): """
SELECT * FROM (SELECT event AS wait_type, wait_class, COUNT(*) AS waiting_tasks, MAX(seconds_in_wait) AS max_wait_seconds
FROM v$session WHERE type = 'USER' AND wait_class <> 'Idle' AND state = 'WAITING' GROUP BY event, wait_class
ORDER BY COUNT(*) DESC) WHERE ROWNUM <= {n}
""",
    ("oracle", "top_queries"): """
SELECT * FROM (SELECT executions AS execution_count, ROUND(cpu_time / 1000) AS total_cpu_ms,
       ROUND(elapsed_time / NULLIF(executions, 0) / 1000) AS avg_elapsed_ms,
       ROUND(buffer_gets / NULLIF(executions, 0)) AS avg_logical_reads, parsing_schema_name AS schema_name,
       sql_id, SUBSTR(sql_text, 1, 1000) AS sql_text
FROM v$sql ORDER BY cpu_time DESC) WHERE ROWNUM <= {n}
""",
    ("mysql", "requests"): """
SELECT id AS session_id, user AS login_name, host AS host_name, db AS database_name, command, state AS status,
       time AS elapsed_seconds, LEFT(info, 2000) AS sql_text
FROM information_schema.processlist WHERE command <> 'Sleep' AND id <> CONNECTION_ID() ORDER BY time DESC LIMIT {n}
""",
    ("mysql", "top_queries"): """
SELECT count_star AS execution_count, ROUND(sum_timer_wait / 1000000000) AS total_elapsed_ms,
       ROUND(avg_timer_wait / 1000000000) AS avg_elapsed_ms, sum_rows_examined AS rows_examined, schema_name,
       LEFT(digest_text, 2000) AS sql_text
FROM performance_schema.events_statements_summary_by_digest ORDER BY sum_timer_wait DESC LIMIT {n}
""",
}


def db_activity(request: Any) -> dict[str, Any]:
    """What the server is doing now - one ``view`` of it."""
    parsed, resolved, db_type = _setup(request, "db-activity")
    view = str(request.get("view") or "requests").strip().lower()
    if view not in VIEWS:
        raise DbInspectError(f"view must be one of {', '.join(VIEWS)}; got {view!r}.")
    sql = _ACTIVITY.get((db_type, view))
    if sql is None:
        raise DbInspectError(f"db-activity has no {view!r} view for {db_type}; it has: "
                             f"{', '.join(v for e, v in _ACTIVITY if e == db_type) or 'none'}.")
    limit = _limit(request)
    try:
        rows = (_run(parsed, sql.format(n=limit), database=parsed["database"], max_rows=limit) or [[]])[0]
    except DbInspectError as exc:
        if "pg_stat_statements" in str(exc):
            raise DbInspectError("top_queries on PostgreSQL reads pg_stat_statements, which this database does not "
                                 "have: it needs shared_preload_libraries = 'pg_stat_statements' and CREATE EXTENSION "
                                 "pg_stat_statements - a change the DBA makes.") from exc
        raise
    return {**_head(resolved, db_type, parsed["database"]), "view": view, "rows": rows, "count": len(rows)}


def _limit(request: dict[str, Any]) -> int:
    value = request.get("max_rows", DEFAULT_MAX_ROWS)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1000:
        raise DbInspectError(f"max_rows is a whole number 1 to 1000; got {value!r}.")
    return value


# --------------------------------------------------------------------------- #
# query-plan
# --------------------------------------------------------------------------- #
_GO_LINE = re.compile(r"(?im)^\s*GO\s*$")


def _statement(request: dict[str, Any], db_type: str) -> str:
    text = str(request.get("sql_text") or "").strip().rstrip(";").strip()
    if not text:
        raise DbInspectError('"sql_text" is required: the one statement to compile.')
    if db_type == "sqlserver":
        # The plan is asked with SHOWPLAN_XML on, in a batch of its own. A GO line in the statement would
        # make a batch that could switch it off and run what follows.
        if _GO_LINE.search(text) or re.search(r"(?i)\b(SHOWPLAN\w*|NOEXEC|PARSEONLY|FMTONLY)\b", text):
            raise DbInspectError("the statement is compiled with SHOWPLAN on: a GO line or a SHOWPLAN, NOEXEC, "
                                 "PARSEONLY or FMTONLY setting in it is refused.")
    elif db_type in ("postgresql", "mysql"):
        if len(split_postgresql_statements(text)) != 1:
            raise DbInspectError("one statement only: EXPLAIN compiles one, and a second would run.")
    return text


def _sqlserver_plan_summary(plan: str) -> dict[str, Any]:
    """The estimated cost, the operators that cost most, and the indexes the optimizer asks for."""
    try:
        root = ET.fromstring(plan)
    except ET.ParseError:
        return {}
    ns = {"p": root.tag.split("}")[0].strip("{")} if root.tag.startswith("{") else {}
    q = "p:" if ns else ""
    statements = root.findall(f".//{q}StmtSimple", ns)
    cost = sum(float(s.get("StatementSubTreeCost") or 0) for s in statements)
    operators = sorted(({"operator": op.get("PhysicalOp"), "logical": op.get("LogicalOp"),
                         "estimated_rows": float(op.get("EstimateRows") or 0),
                         "subtree_cost": float(op.get("EstimatedTotalSubtreeCost") or 0)}
                        for op in root.findall(f".//{q}RelOp", ns)), key=lambda item: -item["subtree_cost"])
    missing = []
    for group in root.findall(f".//{q}MissingIndexGroup", ns):
        for index in group.findall(f"{q}MissingIndex", ns):
            cols = {usage.get("Usage"): ", ".join(c.get("Name", "").strip("[]") for c in usage.findall(f"{q}Column", ns))
                    for usage in index.findall(f"{q}ColumnGroup", ns)}
            missing.append({"impact_pct": float(group.get("Impact") or 0),
                            "table": f"{index.get('Schema', '').strip('[]')}.{index.get('Table', '').strip('[]')}",
                            "equality": cols.get("EQUALITY", ""), "inequality": cols.get("INEQUALITY", ""),
                            "include": cols.get("INCLUDE", "")})
    return {"estimated_cost": round(cost, 4), "top_operators": operators[:10], "missing_indexes": missing}


def query_plan(request: Any) -> dict[str, Any]:
    """The estimated plan of one statement, which is compiled and never run."""
    parsed, resolved, db_type = _setup(request, "query-plan")
    text = _statement(request, db_type)
    database = parsed["database"]
    if db_type == "sqlserver":
        sets = _run(parsed, f"SET SHOWPLAN_XML ON\nGO\n{text}\nGO\nSET SHOWPLAN_XML OFF", database=database)
        plans = [str(next(iter(row.values()), "")) for rows in sets for row in rows]
        plan = "\n".join(plan for plan in plans if plan)
        summary = _sqlserver_plan_summary(plans[0]) if plans else {}
        form = "showplan_xml"
    elif db_type == "postgresql":
        rows = (_run(parsed, f"EXPLAIN (FORMAT JSON) {text}", database=database) or [[]])[0]
        value = next(iter(rows[0].values()), "") if rows else ""
        plan = value if isinstance(value, str) else json.dumps(value)
        try:
            top = (json.loads(plan) or [{}])[0].get("Plan", {})
            summary = {"estimated_cost": top.get("Total Cost"), "estimated_rows": top.get("Plan Rows"),
                       "top_operators": [{"operator": top.get("Node Type"), "subtree_cost": top.get("Total Cost")}],
                       "missing_indexes": []}
        except (ValueError, AttributeError, IndexError):
            summary = {}
        form = "explain_json"
    elif db_type == "mysql":
        rows = (_run(parsed, f"EXPLAIN FORMAT=JSON {text}", database=database) or [[]])[0]
        plan = str(next(iter(rows[0].values()), "")) if rows else ""
        summary = {}
        form = "explain_json"
    else:
        raise DbInspectError(f"query-plan has no plan for {db_type}: Oracle's EXPLAIN PLAN writes to a table, and "
                             "this command changes nothing.")
    cut = len(plan) > MAX_PLAN_CHARS
    return {**_head(resolved, db_type, database), "format": form, "plan": plan[:MAX_PLAN_CHARS],
            "plan_truncated": cut, **summary}
