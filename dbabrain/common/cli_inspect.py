"""``table-info``, ``object-source``, ``db-activity``, ``query-plan`` - plumbing only (1.1.0, 1.421).

:mod:`dbabrain.common.db_inspect` does the work; this reads the request, runs it and answers in the
envelope of :mod:`dbabrain.lib.response`.
"""

from __future__ import annotations

import sys
from typing import Any, Callable

from dbabrain.lib import response

_FIELDS = """\
  {"connection": {...},            // (required) the login, complete - an app fills it from a server_id
   "target": "ACME-192-0-2-10",     // the label the answer carries
   "database_name": "SALESDB",      // the database to read in (SQL Server: else master)
   "timeout_seconds": 30}
"""

USAGE = {
    "table-info": "usage: python -m dbabrain.common.cli table-info '<json>'|@<file>|-\n\n"
                  "One table's columns, indexes, rows and size.\n\n" + _FIELDS +
                  '  plus "object": "dbo.Orders" (a table or view; `name` or `schema.name`)\n',
    "object-source": "usage: python -m dbabrain.common.cli object-source '<json>'|@<file>|-\n\n"
                     "The text of one procedure, function, view or trigger.\n\n" + _FIELDS +
                     '  plus "object": "dbo.usp_Orders"\n',
    "db-activity": "usage: python -m dbabrain.common.cli db-activity '<json>'|@<file>|-\n\n"
                   "What the server is doing now.\n\n" + _FIELDS +
                   '  plus "view": requests (default) | blocking | waits | top_queries, "max_rows": 1-1000 (default 50)\n',
    "query-plan": "usage: python -m dbabrain.common.cli query-plan '<json>'|@<file>|-\n\n"
                  "The estimated plan of one statement - compiled, never run (SQL Server SHOWPLAN_XML,\n"
                  "PostgreSQL and MySQL EXPLAIN without ANALYZE; none on Oracle).\n\n" + _FIELDS +
                  '  plus "sql_text": "SELECT ..." (one statement)\n',
}
COMMANDS = tuple(USAGE)


def run(command: str, argv: list[str], *, read_request: Callable[[str, str], tuple[dict | None, int]]) -> int:
    usage = USAGE[command]
    if not argv or argv[0] in {"-h", "--help"}:
        print(usage, file=sys.stderr)
        return 0 if argv else 2
    if len(argv) != 1:
        return response.emit(response.fail(command, f"{command} takes one JSON payload; got {len(argv)} arguments."))
    request, code = read_request(argv[0], usage)
    if request is None:
        return code
    from dbabrain.common import db_inspect

    work: dict[str, Callable[[Any], dict[str, Any]]] = {
        "table-info": db_inspect.table_info, "object-source": db_inspect.object_source,
        "db-activity": db_inspect.db_activity, "query-plan": db_inspect.query_plan}
    try:
        data = work[command](request)
    except db_inspect.DbInspectError as exc:
        return response.emit(response.fail(command, str(exc)))
    except Exception as exc:  # noqa: BLE001 - an answer, never a traceback (rules R15)
        return response.emit(response.fail(command, f"{type(exc).__name__}: {exc}"))
    where = f"{data['server_id']}" + (f".{data['database_name']}" if data.get("database_name") else "")
    if command == "table-info":
        message = f"{data['object']} on {where}: {data['column_count']} column(s), {data['index_count']} index(es)."
    elif command == "object-source":
        message = f"{data['count']} definition(s) of {data['object']} on {where}."
    elif command == "db-activity":
        message = f"{data['count']} row(s) of {data['view']} on {where}."
    else:
        message = f"the estimated plan on {where} ({data['format']})" + (", cut" if data.get("plan_truncated") else "") + "."
    return response.emit(response.ok(command, message=message, data=data))
