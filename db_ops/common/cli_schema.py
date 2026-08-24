"""CLI face of :mod:`db_ops.common.schema_copy`.

One command, the same contract as every other `common` command: **the input is a JSON object** —
inline, ``@file`` or stdin (``-``) — and the output is the response envelope from
:mod:`db_ops.lib.response`.

    copy-schema   reproduce one SQL Server schema from instance A on instance B

Split out of ``cli.py`` for the reason ``cli_catalog`` and ``cli_backup`` were: the shared CLI is
a dispatcher, and a command's argument handling belongs beside the command.

**Progress goes to stderr.** A copy is minutes long and an operator watching it needs to see which
phase is running; stdout stays a single parseable JSON document, so a program piping this still
reads one object and not a log with an object at the end.
"""

from __future__ import annotations

import sys
from typing import Any, Callable

from db_ops.lib import response


COPY_SCHEMA_USAGE = (
    "usage: python -m db_ops.common.cli copy-schema '<json>'|@<file>|- "
    "[--apply] [--show-sql] [--quiet] [--key ... | --key-base64 ...]\n"
    "\n"
    "Reproduce one SQL Server schema from instance A on instance B: partition objects, tables,\n"
    "change tracking, indexes, checks, catalogue data, modules, then foreign keys - in that\n"
    "order, because that order is the dependency graph.\n"
    "\n"
    "PLAN IS THE DEFAULT. A plan reads only the source, prints every statement and count, and\n"
    "opens no connection to the destination at all. Pass \"plan\": false (or --apply) to write.\n"
    "\n"
    '  {"source": {"target": "ACME-192-0-2-111", "database": "APPDB_TEST", "schema": "sched"},\n'
    '   "dest":   {"target": "ACME-192-0-2-250", "database": "APPDB",      "schema": "sched"},\n'
    '   "assert_dest_instance": "APP-DB\\\\PROD",\n'
    '   "exclude_tables": ["dataLock", "*Staging"],\n'
    '   "with_data": ["config", "config_version", "CalendarDay"],\n'
    '   "exclude_modules": ["usp_*_Golden*"],\n'
    '   "plan": true}\n'
    "\n"
    "Fields:\n"
    "  source / dest        (required) {target, database, schema, credential_name}\n"
    "                       target is a server_id, or \"<db_type> <ip> [port]\"\n"
    "  plan                 true (DEFAULT) = read and report only; false = write\n"
    "  mode                 plan | apply | report - the other spelling of the same choice\n"
    "  assert_dest_instance SERVERPROPERTY('ServerName') the destination MUST match, or abort.\n"
    "                       DB_NAME() alone is not an identity: one database name commonly\n"
    "                       exists on several instances of an estate.\n"
    "  include_tables       glob patterns; empty = every table in the schema\n"
    "  exclude_tables       glob patterns; wins over include_tables\n"
    "  with_data            which tables' ROWS to copy as well as their structure\n"
    "  include_modules      glob patterns for procedures/views/functions\n"
    "  exclude_modules      glob patterns; wins over include_modules\n"
    "  phases               subset of: partitions, change_tracking_database, tables,\n"
    "                       change_tracking_tables, indexes, checks, data, modules,\n"
    "                       foreign_keys. Always run in that order whatever order you list.\n"
    "  partition_boundaries \"all\" (default) | \"none\" | [explicit boundary values]\n"
    "  map_filegroups       {\"FG_ARCHIVE\": \"PRIMARY\"} - redirect a missing filegroup\n"
    "  report_unsupported   list what this will NOT carry (default: true)\n"
    "  skip_nonempty_tables leave a with_data table alone if it already has rows (default: true)\n"
    "  create_schema        CREATE SCHEMA on the destination if absent (default: true)\n"
    "  verify               compare source and destination afterwards (default: true)\n"
    "  batch_size           rows per executemany during the data phase (default: 2000)\n"
    "  lock_name            application-lock resource; default is the destination schema\n"
    "  lock_timeout_seconds how long to wait for that lock (default: 300)\n"
    "  timeout_seconds      statement/connect budget (default: 900)\n"
    "  data_dir             folder holding db_instances.json (default: data/)\n"
    "\n"
    "Flags:\n"
    "  --apply     same as \"plan\": false\n"
    "  --show-sql  include the rendered statements in the human summary on stderr\n"
    "  --quiet     no progress on stderr; stdout is unaffected either way\n"
)

_USAGE = {"copy-schema": COPY_SCHEMA_USAGE}


def _split_flags(argv: list[str]) -> tuple[list[str], dict[str, Any]]:
    """Pull the option flags out, leaving the JSON payload.

    The payload is the contract and can carry every one of these as a field. The flags exist
    because ``--apply`` is what a person types at the end of a long day of ``--plan`` runs, and
    editing a JSON file to flip one boolean is where the wrong file gets edited.
    """
    rest: list[str] = []
    options: dict[str, Any] = {"key": None, "key_base64": None, "apply": False,
                               "show_sql": False, "quiet": False}
    tokens = list(argv)
    while tokens:
        token = tokens.pop(0)
        if token == "--key" and tokens:
            options["key"] = tokens.pop(0)
        elif token == "--key-base64" and tokens:
            options["key_base64"] = tokens.pop(0)
        elif token.startswith("--key="):
            options["key"] = token.split("=", 1)[1]
        elif token.startswith("--key-base64="):
            options["key_base64"] = token.split("=", 1)[1]
        elif token == "--apply":
            options["apply"] = True
        elif token in ("--plan", "--dry-run"):
            options["apply"] = False
        elif token == "--show-sql":
            options["show_sql"] = True
        elif token == "--quiet":
            options["quiet"] = True
        else:
            rest.append(token)
    return rest, options


def run(command: str, argv: list[str],
        read_request: Callable[[str, str], tuple[dict | None, int]]) -> int:
    """Dispatch ``copy-schema``. ``read_request`` is ``cli._read_json_request``."""
    usage = _USAGE[command]
    if not argv or argv[0] in {"-h", "--help"}:
        print(usage, file=sys.stderr)
        return 2
    payload_args, options = _split_flags(argv)
    if len(payload_args) != 1:
        return response.emit(response.fail(
            command,
            f"{command} takes one JSON payload; got {len(payload_args)}. "
            "Pass the request as one object: '<json>', @<file>, or - for stdin."))

    request, code = read_request(payload_args[0], usage)
    if request is None:
        return code

    from db_ops.lib.secret_text import set_key_env

    set_key_env(options["key"] or request.get("key"),
                options["key_base64"] or request.get("key_base64"))

    if options["apply"]:
        # The flag is an override, not a merge: somebody typing --apply has decided, and a "plan"
        # left true in the file they are pointing at must not quietly win.
        request = dict(request)
        request["plan"] = False
        request.pop("mode", None)

    return _copy_schema(request, show_sql=options["show_sql"], quiet=options["quiet"])


def _copy_schema(request: dict[str, Any], *, show_sql: bool, quiet: bool) -> int:
    from db_ops.common import schema_copy

    def say(message: str) -> None:
        if not quiet:
            print(message, file=sys.stderr, flush=True)

    try:
        data = schema_copy.copy_schema(request, progress=say)
    except schema_copy.SchemaCopyError as exc:
        return response.emit(response.fail("copy-schema", str(exc)))

    plan = data.get("plan") or {}
    counts = plan.get("counts") or {}
    if not quiet:
        say("")
        say(schema_copy.format_plan(plan, show_sql=show_sql))

    applied = data.get("applied")
    if applied is None:
        message = (f"PLAN ONLY - nothing was written. {len(plan.get('steps') or [])} statement(s) "
                   f"for {len(plan.get('tables') or [])} table(s) from {plan.get('source')} to "
                   f"{plan.get('dest')}.")
    else:
        errors = applied.get("errors") or []
        message = (f"Applied {sum(phase.get('done', 0) for phase in (applied.get('phases') or {}).values())}"
                   f" of {len(plan.get('steps') or [])} step(s) to {plan.get('dest')}"
                   f", {applied.get('rows_copied', 0):,} row(s) copied")
        message += f"; {len(errors)} step(s) FAILED." if errors else "."
        mismatches = (data.get("verification") or {}).get("mismatches") or []
        if mismatches:
            message += f" Verification differs on: {', '.join(str(item) for item in mismatches)}."

    # Named in the headline rather than left in `data`, because it is the half of the answer an
    # operator has to act on by hand and the half that is silent everywhere else.
    unsupported = [item for item in (plan.get("unsupported") or ())
                   if item.get("status") == "present"]
    if unsupported:
        message += (" NOT carried: "
                    + ", ".join(f"{item['feature']} ({item['count']})" for item in unsupported)
                    + ".")

    metrics = {"steps": len(plan.get("steps") or []),
               "tables": len(plan.get("tables") or []),
               "modules": len(plan.get("modules") or []),
               "rows_copied": (applied or {}).get("rows_copied", 0),
               "rows_planned": counts.get("rows_to_copy", 0),
               "partitioned_indexes": counts.get("partitioned_indexes", 0),
               "duration_ms": data.get("duration_ms", 0)}

    # A step that failed is a failed *operation*, even though the phases around it succeeded: an
    # exit code that says 0 for "the schema is half there" is the one thing a scheduled caller
    # cannot recover from.
    if applied is not None and (applied.get("errors") or []):
        return response.emit(response.fail("copy-schema", message, data=data, metrics=metrics))
    return response.emit(response.ok("copy-schema", message=message, data=data, metrics=metrics))
