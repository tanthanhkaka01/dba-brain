"""CLI face of :mod:`db_ops.common.db_catalog` and :mod:`db_ops.common.table_load`.

Three commands, one dispatcher, the same contract as every other `common` command: **the input
is a JSON object** — inline, ``@file`` or stdin (``-``) — and the output is the five-key response
from :mod:`db_ops.lib.response`.

    list-databases          what databases a server has, and their state
    list-schemas            what schemas one database has
    create-table-from-xlsx  build a table from a spreadsheet and load it

Split out of ``cli.py`` for the same reason ``cli_backup`` and ``cli_backup_files`` were: the
shared CLI is a dispatcher, and a command's argument handling belongs next to the command.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Callable

from db_ops.lib import response


LIST_DATABASES_USAGE = (
    "usage: python -m db_ops.common.cli list-databases '<json>'|@<file>|-\n"
    "\n"
    "Every database on one server, with the state its engine reports. On Oracle this is the\n"
    "container list: a CDB answers with its root, its seed and every PDB, each with its\n"
    "open_mode; a non-CDB answers with its single database.\n"
    "\n"
    '  {"target": "ACME-192-0-2-248"}\n'
    '  {"target": "CLOUD-203-0-113-188-ORA-1521", "include_system": true}\n'
    "\n"
    "Fields:\n"
    "  target          (required) server_id, or \"<db_type> <ip> [port]\"\n"
    "  credential_name which login to connect as (default: the instance's)\n"
    "  include_system  show system databases too (default: false)\n"
    "  timeout_seconds connect/statement timeout\n"
    "  data_dir        folder holding db_instances.json (default: data/)\n"
)

LIST_SCHEMAS_USAGE = (
    "usage: python -m db_ops.common.cli list-schemas '<json>'|@<file>|-\n"
    "\n"
    "Every schema inside one database. SQL Server and PostgreSQL require \"database\": schemas\n"
    "live inside one, and without it the answer would describe the login's default instead.\n"
    "\n"
    '  {"target": "ACME-192-0-2-248", "database": "APPDB"}\n'
    "\n"
    "Fields:\n"
    "  target          (required) server_id, or \"<db_type> <ip> [port]\"\n"
    "  database        (required on sqlserver/postgresql) which database to look inside\n"
    "  credential_name which login to connect as (default: the instance's)\n"
    "  include_system  show system schemas too (default: false)\n"
    "  timeout_seconds connect/statement timeout\n"
    "  data_dir        folder holding db_instances.json (default: data/)\n"
)

LIST_JOBS_USAGE = (
    "usage: python -m db_ops.common.cli list-jobs '<json>'|@<file>|-\n"
    '\n'
    'Every scheduled job on one target, with whether it is currently enabled.\n'
    'SQL Server reads the Agent (msdb), Oracle reads BOTH schedulers (DBMS_SCHEDULER and the\n'
    'older DBMS_JOB) and says which owns each entry, PostgreSQL reads pg_cron -- and reports\n'
    'plainly when pg_cron is not installed, rather than answering with an empty list.\n'
    '\n'
    '  {"target": "ACME-192-0-2-115"}\n'
    '  {"target": "ACME-192-0-2-115", "enabled_only": true}\n'
    '\n'
    'Fields:\n'
    '  target          (required) server_id, or "<db_type> <ip> [port]"\n'
    '  enabled_only    only the switched-on jobs (default: false)\n'
    "  credential_name which login to connect as (default: the instance's)\n"
    '  timeout_seconds connect/statement timeout\n'
    '  data_dir        folder holding db_instances.json (default: data/)\n'
)

CREATE_TABLE_USAGE = (
    "usage: python -m db_ops.common.cli create-table-from-xlsx '<json>'|@<file>|-\n"
    "\n"
    "Create a table shaped like a spreadsheet's first sheet and load its rows. Every column is\n"
    "NVARCHAR(4000) (or the engine's equivalent) because a type guessed from a spreadsheet is\n"
    "wrong on the row nobody checked. The file travels as base64 so the same call works\n"
    "from a shell and from a Telegram message with a file attached.\n"
    "\n"
    "Takes an .xlsx OR a delimited text file (.txt/.csv/.tsv, tab/comma/semicolon/pipe),\n"
    "detected from the bytes rather than the file name.\n"
    "\n"
    '  {"target": "ACME-192-0-2-248", "database": "Staging", "schema": "dbo",\n'
    '   "file_base64": "UEsDBBQ..."}\n'
    "\n"
    "Fields:\n"
    "  target        (required) server_id, or \"<db_type> <ip> [port]\"\n"
    "  file_base64   (required) the file, base64-encoded  [or file_path for a local file]\n"
    "                also accepted as xlsx_base64 / xlsx_path, the original names\n"
    "  delimiter     text files only; blank = guessed from the header line\n"
    "  database      which database to create the table in\n"
    "  schema        default: dbo (sqlserver) / public (postgresql) / the login (oracle)\n"
    "  table_name    default: temp_<random>, echoed back in the response\n"
    "  if_exists     error (default) | drop | append\n"
    "  load_rows     false to create the structure only (default: true)\n"
    "  text_length   column width (default: 4000)\n"
    "  max_rows      cap on rows read from the sheet (default: 100000)\n"
    "  credential_name / timeout_seconds / data_dir  as for run-sql\n"
)

_USAGE = {
    "list-databases": LIST_DATABASES_USAGE,
    "list-schemas": LIST_SCHEMAS_USAGE,
    "list-jobs": LIST_JOBS_USAGE,
    "create-table-from-xlsx": CREATE_TABLE_USAGE,
}


def _split_key_flags(argv: list[str]) -> tuple[list[str], str | None, str | None]:
    """Pull ``--key`` / ``--key-base64`` out of the arguments, leaving the JSON payload.

    The payload is the contract and can carry the passphrase itself. The flags are accepted
    anyway because every other command that connects to a database takes them, and an operator
    who has been pasting ``--key-base64`` all session should not be told their argument count is
    wrong by the one command that does not.
    """
    rest: list[str] = []
    key = key_base64 = None
    tokens = list(argv)
    while tokens:
        token = tokens.pop(0)
        if token == "--key" and tokens:
            key = tokens.pop(0)
        elif token == "--key-base64" and tokens:
            key_base64 = tokens.pop(0)
        elif token.startswith("--key="):
            key = token.split("=", 1)[1]
        elif token.startswith("--key-base64="):
            key_base64 = token.split("=", 1)[1]
        else:
            rest.append(token)
    return rest, key, key_base64


def run(command: str, argv: list[str],
        read_request: Callable[[str, str], tuple[dict | None, int]]) -> int:
    """Dispatch one catalog/loader command. ``read_request`` is ``cli._read_json_request``."""
    usage = _USAGE[command]
    if not argv or argv[0] in {"-h", "--help"}:
        print(usage, file=sys.stderr)
        return 2
    payload_args, key, key_base64 = _split_key_flags(argv)
    if len(payload_args) != 1:
        return response.emit(response.fail(
            command,
            f"{command} takes one JSON payload; got {len(payload_args)}. "
            "Pass the request as one object: '<json>', @<file>, or - for stdin."))

    request, code = read_request(payload_args[0], usage)
    if request is None:
        return code

    from db_ops.lib.secret_text import set_key_env

    # The passphrase may ride in the request itself, like every other command that connects.
    set_key_env(key or request.get("key"), key_base64 or request.get("key_base64"))

    if command == "create-table-from-xlsx":
        return _create_table(request)
    return _list(command, request)


def _list(command: str, request: dict[str, Any]) -> int:
    from db_ops.common import db_catalog

    runner = {"list-databases": db_catalog.list_databases,
              "list-schemas": db_catalog.list_schemas,
              "list-jobs": db_catalog.list_jobs}[command]
    try:
        data = runner(request)
    except db_catalog.DbCatalogError as exc:
        return response.emit(response.fail(command, str(exc)))

    if command == "list-databases":
        kind = "container" if data.get("container_type") == "CDB" else "database"
        message = (f"{data['count']} {kind}{'' if data['count'] == 1 else 's'} on "
                   f"{data['server_id']}.")
    elif command == "list-jobs":
        # The enabled count is in the headline because it is the number the operator is acting on:
        # "31 jobs" and "3 of them switched on" are very different situations to walk into.
        message = (f"{data['count']} job{'' if data['count'] == 1 else 's'} on "
                   f"{data['server_id']} ({data['enabled_count']} enabled).")
        if data.get("disabled_hidden"):
            message += (f" {data['disabled_hidden']} disabled hidden "
                        f'(set "enabled_only": false to show them).')
    else:
        message = (f"{data['count']} schema{'' if data['count'] == 1 else 's'} in "
                   f"{data['database']} on {data['server_id']}.")
    if data.get("system_hidden"):
        message += f" {data['system_hidden']} system entries hidden (include_system: true)."
    if data.get("note"):
        message += f" {data['note']}"
    return response.emit(response.ok(command, message=message, data=data,
                             metrics={"count": data["count"]}))


def _create_table(request: dict[str, Any]) -> int:
    from db_ops.common import table_load

    try:
        data = table_load.create_table_from_xlsx(request)
    except table_load.TableLoadError as exc:
        return response.emit(response.fail("create-table-from-xlsx", str(exc)))

    what = "Created" if data["created"] else "Appended to"
    message = (f"{what} {data['qualified_name']} on {data['server_id']} "
               f"({data['column_count']} columns, {data['rows_inserted']} rows).")
    if data["dropped_existing"]:
        message += " The existing table was dropped first."
    if data["source_format"] == "delimited":
        # Named because it is a guess: a text file read with the wrong delimiter still produces
        # columns, and this is the only place the reader's decision is visible.
        from db_ops.lib import delimited_import

        message += (f" Read as {delimited_import.describe_delimiter(data['source_delimiter'])} "
                    f"text ({data['source_encoding']}).")
    if data["sheet_truncated"]:
        message += " The file was longer than max_rows; not every row was read."
    return response.emit(response.ok(
        "create-table-from-xlsx", message=message, data=data,
        metrics={"rows_inserted": data["rows_inserted"],
                 "column_count": data["column_count"],
                 "duration_ms": data["duration_ms"]},
    ))
