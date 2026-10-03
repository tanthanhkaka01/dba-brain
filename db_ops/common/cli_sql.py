"""The ``common.cli`` commands that run SQL on one target: ``run-sql``, ``trace-session`` and ``db-status``.

Split out of ``common/cli.py`` on 2026-10-03 (Q11,
``audits/20261002_audit_typed_requests_and_errors.md`` section 5). ``cli`` re-exports every
name, so no import changes.
"""

from __future__ import annotations
import sys
from db_ops.common.cli_request import _read_json_request
from db_ops.common.cli_usage import DB_STATUS_USAGE, RUN_SQL_USAGE, TRACE_SESSION_USAGE


def _run_sql_command(argv: list[str]) -> int:
    """Run one JSON request through :func:`db_ops.common.sql_run.run_sql` and print the result.

    The request travels as a JSON object (inline, ``@file``, or ``-`` for stdin) so a caller
    composes config, not command-line flags — the same object an app passes to the API. Failures
    print ``{"ok": false, "error": ...}`` and exit 1, so a shell caller can read either outcome
    from the same JSON instead of parsing stderr.
    """

    from db_ops.common import sql_run
    from db_ops.lib.secret_text import set_key_env

    if not argv or argv[0] in {"-h", "--help"}:
        print(RUN_SQL_USAGE, file=sys.stderr)
        return 0 if argv else 2

    source = argv[0]
    # Since 0.24.0 the request states its login (rules R09) and no secret store is opened, so the
    # key changes nothing here. It is still accepted: a 0.23.0 command line that passes it should
    # run, not fail on a flag that became redundant.
    rest = argv[1:]
    key = key_base64 = None
    while rest:
        flag = rest.pop(0)
        value = rest.pop(0) if rest else ""
        if flag == "--key":
            key = value
        elif flag in {"--key-base64", "--key_base64"}:
            key_base64 = value
        else:
            print(f"Unknown run-sql option: {flag}\n\n{RUN_SQL_USAGE}", file=sys.stderr)
            return 2
    try:
        set_key_env(key, key_base64)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    request, code = _read_json_request(source, RUN_SQL_USAGE)
    if request is None:
        return code

    # The rendering is chosen inside the request object, like everything else about the run: a
    # `--format` flag would be the one part of the contract a config file could not carry.
    fmt = request.get("format")
    output_path = request.get("output_path")

    from db_ops.lib import response

    try:
        result = sql_run.run_sql(request)
    except sql_run.SqlRunError as exc:
        return response.emit(response.fail("run-sql", str(exc)))
    except Exception as exc:  # noqa: BLE001 - an answer, never a traceback (rules R15, §1.60)
        return response.emit(response.fail("run-sql", f"{type(exc).__name__}: {exc}"))

    from db_ops.lib import result_format

    safe = sql_run.json_safe_result(result)
    rendered = str(fmt or "json").strip().lower()
    if rendered in ("", "json"):
        # The default answers in the response envelope, with the run under `data`. Every other
        # format is a **rendering the request asked for** — an aligned table, a csv, a workbook,
        # raw stdout — and wrapping those in JSON would defeat the point of asking for them. That
        # is the contract working, not an exception to it: the format is chosen *inside* the
        # request object, so a config file can carry it.
        sets = safe.get("result_sets") or []
        return response.emit(response.ok(
            "run-sql",
            message=(f"{safe.get('row_count', 0)} row(s)"
                     + (f", {safe['affected_rows']} affected" if safe.get("affected_rows") else "")
                     + f" from {safe.get('server_id')}"
                     + (f".{safe['database_name']}" if safe.get("database_name") else "")
                     + (" (truncated)" if safe.get("truncated") else "")
                     # A warning ended the reading: say so where the reader looks first, and the
                     # text itself is in data.warnings.
                     + (f"; {len(safe['warnings'])} SQL Server warning(s), nothing after the first "
                        "could be read" if safe.get("warnings") else "")),
            data=safe,
            metrics={"row_count": safe.get("row_count", 0),
                     "affected_rows": safe.get("affected_rows", 0),
                     "result_sets": len(sets)},
        ))

    try:
        text, _extra = result_format.render_result(
            safe, fmt=rendered, output_path=output_path,
        )
    except result_format.ResultFormatError as exc:
        return response.emit(response.fail("run-sql", str(exc)))
    print(text)
    return 0


def _trace_session_command(argv: list[str]) -> int:
    """``trace-session`` — the CLI face of :mod:`db_ops.common.session_trace`."""
    if not argv or argv[0] in {"-h", "--help"}:
        print(TRACE_SESSION_USAGE, file=sys.stderr)
        return 2
    request, code = _read_json_request(argv[0], TRACE_SESSION_USAGE)
    if request is None:
        return code
    from db_ops.common import session_trace
    from db_ops.lib.secret_text import set_key_env
    from db_ops.common.sql_run import SqlRunError

    from db_ops.lib import response

    set_key_env(request.get("key"), request.get("key_base64"))
    try:
        result = session_trace.trace_sessions(request)
    except (session_trace.SessionTraceError, SqlRunError) as exc:
        return response.emit(response.fail("trace-session", str(exc)))
    # The readable per-session lines stay on stderr, where a person watching sees them while the
    # answer on stdout stays one object.
    for session in result["sessions"]:
        print(session_trace.describe(session), file=sys.stderr)
    count = len(result["sessions"])
    return response.emit(response.ok(
        "trace-session",
        message=(f"{count} open transaction(s) on {result.get('server_id') or 'the target'}"
                 if count else
                 f"no open transaction older than the threshold on "
                 f"{result.get('server_id') or 'the target'}"),
        data=result,
        metrics={"session_count": count},
    ))


def _db_status_command(argv: list[str]) -> int:
    """``db-status`` - the verdict layer over what the catalog lists.

    Separate from `verify-restore`, which asks the same question *in the context of a restore* and
    reaches Oracle and PostgreSQL over ssh because that is how a container drill is reachable. This
    one goes through the inventory and the ordinary SQL path, which is what the SLA grader, the
    fleet page and the chat commands already use.
    """
    from db_ops.common import dbstatus
    from db_ops.lib import response

    source, config_path, rest = "", None, list(argv)
    while rest:
        token = rest.pop(0)
        if token in {"-h", "--help"}:
            print(DB_STATUS_USAGE)
            return 0
        if token == "--config":
            config_path = rest.pop(0) if rest else None
        elif not source:
            source = token
        else:
            print(f"Unexpected argument: {token}\n\n{DB_STATUS_USAGE}", file=sys.stderr)
            return 2
    _ = config_path

    request, code = _read_json_request(source or "{}", DB_STATUS_USAGE)
    if request is None:
        return code
    try:
        data = dbstatus.status(request)
    except (dbstatus.DbStatusError, Exception) as exc:  # noqa: BLE001 - reported, not raised
        return response.emit(response.fail("db-status", str(exc)))

    instance = data.get("instance") or {}
    where = data.get("server_id") or "target"
    if data["depth"] == "instance":
        message = f"{where}: instance {instance.get('state')}"
    else:
        message = (f"{where}: instance {instance.get('state')}, "
                   f"{data['checked']} {data['depth']}(s) checked, {data['failed']} not usable")
    # `success` is whether the QUESTION was answered; `data.ok` is the answer. A server that is
    # down is a successful call reporting `ok: false` - the same split `check-secret` makes, and
    # the reason a caller can tell "it is broken" from "I could not find out".
    return response.emit(response.ok(
        "db-status", message=message, data=data,
        metrics={"checked": data["checked"], "failed": data["failed"],
                 "ok": 1 if data["ok"] else 0},
    ))
