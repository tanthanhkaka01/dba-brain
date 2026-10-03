"""A SQL task's execution on its target: the SQL files resolved, the login stated, the text checked, the run bounded by a deadline, and a failed connect diagnosed.

Split out of ``sql_tasks/runner.py`` on 2026-10-03 (Q11,
``audits/20261002_audit_typed_requests_and_errors.md`` section 5). ``runner`` re-exports every
name, so no import changes.
"""

from __future__ import annotations
from db_ops.lib import errors
from db_ops.lib.text_format import format_log_value, format_message_time  # noqa: F401 - one definition, see that module
from db_ops.lib.data_sources import _server_id_from_instance  # noqa: F401 - one definition, see that module
from datetime import datetime
from pathlib import Path
from typing import Any
from db_ops.lib import sql_access
from db_ops.sql_tasks import python_source as python_source_module
from db_ops.sql_tasks.python_source import PythonSourceError, batches
from db_ops.lib.sql_task_catalog import (  # noqa: F401 - re-exported for this module's callers
    _AMBIGUOUS_CREDENTIAL, DEFAULT_INLINE_MAX_ROWS, DEFAULT_SQL_TIMEOUT_SECONDS, INPUT_TYPES,
    SQL_TARGET_NOTIFY_DEFAULTS, XLSX_MAX_ROWS, SqlCommand, SqlTarget, _opt_str, collect_sql_tasks,
    load_default_credential_names, load_input_definition, load_sql_access_by_server,
    load_sql_commands, load_sql_script_definition, load_sql_targets, resolve_sql_folder)
from db_ops.lib.sql_text import (DEFAULT_CONNECT_TIMEOUT_SECONDS, NAMED_BIND_DB_TYPES,
                                 build_parameter_prelude)
from db_ops.lib import data_sources
from db_ops.lib.data_sources import request_fill
from db_ops.transport import common_cli
from db_ops.lib import sql_task_target
from db_ops.lib.time_window import run_anchor
from db_ops.logging_ops import log_event
from db_ops.lib.paths import DEFAULT_DATA_DIR, REPO_ROOT, TOOL_ROOT  # noqa: F401 - one definition, see that module
from db_ops.lib.paths import asset_candidates
from db_ops.sql_tasks.runner_parameters import legacy_define_values, named_parameter_values
from db_ops.sql_tasks.runner_output import MAX_STORED_RESULT_SETS, log_sql_task_event


#: The Unicode range that only ever exists as half of a pair. A string holding one on its own
#: cannot be encoded to anything, which is why every driver refuses it - pyodbc most visibly,
#: because SQL Server takes NVARCHAR as UTF-16LE.
_SURROGATE_RANGE = range(0xD800, 0xE000)


def _surrogate_context(sql_text: str, index: int, *, window: int = 45) -> str:
    """The characters either side of *index*, escaped, so the string can be recognised.

    The whole point of the guard: knowing *which* string carried the surrogate is what a codec
    error never says. Printed as ``ascii`` so a second bad character in the window cannot make
    the report itself unprintable - which is exactly how this defect wasted an afternoon.
    """
    start = max(0, index - window)
    return ascii(sql_text[start:index + window])


def check_sql_text_is_encodable(sql_text: str, *, source: str) -> None:
    """Refuse SQL carrying a lone surrogate, and say which character and where.

    On 2026-08-27 one ``/spbot_run_sql_task 18`` run died with ``'utf-16-le' codec can't encode
    character U+DC97 in position 350: surrogates not allowed``.

    U+DC97 is byte 0x97 recovered by ``surrogateescape``, and 0x97 is the em dash in the Windows
    ANSI code page. The script is valid UTF-8, character 350 *is* an em dash, and the file on disk
    was byte-identical to the source tree's - so that one run received the text after a round trip
    through cp1252 that no other run made. Four faithful reproductions, up to and including the
    dispatch's exact argv under a detached console-less process, all succeeded.

    This does not fix that round trip, because I could not find it. What it does is turn an
    intermittent driver error naming only a codec into one that names **the file, the character
    and its position** - the difference between "it failed again" and a report somebody can act
    on. It costs a scan of a string already in memory, and it runs for every engine, because a
    lone surrogate is unencodable everywhere.

    **The script file is not the whole story, and that is the finding.** Its two non-ASCII
    characters are em dashes at 286 and 346, both inside ``--`` comments, and it contains no 0x97
    byte anywhere. The driver was handed a string whose character *350* is byte 0x97. So the
    statement that reached the driver was not simply this file's text - something composes it, or
    re-reads it, between the ``read_text(encoding="utf-8-sig")`` and the send. That is why the
    message below carries the surrounding characters: the next occurrence identifies the string.

    Writing this docstring hit the same class of bug: the patch script that inserted it put a real
    lone surrogate into the source and Python refused to save the file. Hence U+DC97 in prose
    rather than an escape.
    """
    for index, character in enumerate(sql_text):
        if ord(character) not in _SURROGATE_RANGE:
            continue
        byte = ord(character) - 0xDC00
        recovered = bytes([byte]).decode("cp1252", errors="replace") if 0 <= byte <= 0xFF else "?"
        raise errors.InvalidConfig(
            f"{source}: character {index} is a lone surrogate (U+{ord(character):04X}), which no "
            f"encoding accepts, so the driver cannot send this statement. It is byte 0x{byte:02x} "
            f"recovered by surrogateescape - {recovered!r} in the Windows ANSI code page - so the "
            f"script text passed through a non-UTF-8 round trip between being read and being "
            f"executed.\n"
            f"  context: {_surrogate_context(sql_text, index)}\n"
            f"  statement length: {len(sql_text)} characters."
        )


def _run_python_source(
    *,
    command: SqlCommand,
    target: SqlTarget,
    data_dir: Path,
    metadata: dict[str, Any],
    parameter_values: dict[str, Any] | None,
    logger: Any,
) -> list[str]:
    """Run the task's ``input_type: "python"`` step and return its rows, batched as JSON.

    Everything it learned goes on ``metadata`` before anything is sent to the database, so a run
    that then fails in the SQL still records what the fetch produced — how many rows, how long it
    took, what the script said on stderr, and whatever else its document carried at the top level
    (``status``, ``total``, ``errors`` on the estate's first one). A failed fetch that leaves no
    trace is a task that "failed" with nothing to look at.

    The tool root, not ``data_dir``: a script is addressed the way ``assets/`` is everywhere else
    in this tree, and ``data/`` is the folder beside it rather than the root.
    """
    source = command.python_source
    assert source is not None  # only called when input_type == python
    tool_root = Path(data_dir).parent
    log_sql_task_event(
        logger,
        "sql_tasks.runner.input.python.start",
        command=command,
        target=target,
        sql_id=command.sql_id,
        sql_code=command.sql_code,
        input_type=command.input_type,
        script=source.script_path,
    )
    try:
        produced = python_source_module.run(
            source, tool_root=tool_root, parameter_values=parameter_values,
            target={"target_server_id": target.server_id,
                    "target_database": target.database_name})
    except PythonSourceError as exc:
        metadata["input"] = {"type": command.input_type, "script": source.script_path,
                             "status": "failed", "error": str(exc)}
        log_sql_task_event(
            logger,
            "sql_tasks.runner.input.python.error",
            command=command,
            target=target,
            level="error",
            error=str(exc),
        )
        raise errors.OperationFailed(str(exc)) from exc

    payloads = batches(produced.rows, source.batch_rows)
    metadata["input"] = {
        "type": command.input_type,
        "script": source.script_path,
        "status": "done",
        "batches": len(payloads),
        "batch_rows": source.batch_rows,
        "parameter": source.parameter,
        **produced.summary(),
        # Kept whether or not the script failed: a fetcher that succeeds and warns is the case
        # nobody looks at, and it is the one where a silently halved pull hides.
        "stderr_tail": produced.stderr_tail,
    }
    log_sql_task_event(
        logger,
        "sql_tasks.runner.input.python.done",
        command=command,
        target=target,
        sql_id=command.sql_id,
        sql_code=command.sql_code,
        rows=len(produced.rows),
        batches=len(payloads),
        duration_ms=produced.duration_ms,
        exit_code=produced.exit_code,
    )
    return payloads


def execute_sql(
    *,
    command: SqlCommand,
    target: SqlTarget,
    database: dict[str, Any],
    credential: dict[str, Any],
    password: str,
    sql_text: str,
    parameter_values: dict[str, Any] | None = None,
    secrets: dict[str, str] | None = None,
) -> dict[str, Any]:
    check_sql_text_is_encodable(sql_text, source=f"{command.sql_code} target={target.target_no}")
    return execute_on_target(command=command, target=target, database=database,
                             credential=credential, password=password, sql_text=sql_text,
                             parameter_values=parameter_values, secrets=secrets)


def format_script_file_order(command: SqlCommand) -> str:
    names = [Path(value).name for value in command.script_files]
    return "[" + ", ".join(names) + "]"


def execute_on_target(
    *,
    command: SqlCommand,
    target: SqlTarget,
    database: dict[str, Any],
    credential: dict[str, Any],
    password: str,
    sql_text: str,
    parameter_values: dict[str, Any] | None = None,
    secrets: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run one task's SQL on its target, on whichever engine that target is.

    One call for every engine **and every transport**, through
    ``python -m db_ops.common.cli run-sql``. Three things used to be decided here that were never
    this app's to decide — how to reach a database, how to split a script into batches, and how to
    route an Oracle 8i target — and each was a second opinion that could drift from the shared one.
    The 2026-08-06 audit named the Oracle half, the 2026-08-11 audit found the SQL Server half
    still there, and on 2026-08-16 the whole thing became a request object.

    What stays here is what is genuinely a *task* concern:

    * **the commit mode.** An ``autocommit`` task runs with no wrapping transaction (each batch
      commits on its own), which is what procs that reject an open transaction
      (``@@TRANCOUNT > 0``) require; every other task commits once at the end.
    * **the two timeouts, kept apart.** The task's own timeout bounds the *statements*; the
      connect keeps its own short deadline, because a task allowed twenty minutes must not wait
      twenty minutes to discover the host is down.
    * **what a parameter means on this transport.** A normal target binds them; an 8i target
      cannot (the legacy tool runs one statement with no bind list), so its values are SQL*Plus
      substitutions instead — see :func:`legacy_define_values`, which refuses a name the command
      does not declare rather than letting it match no ``&VAR`` and vanish. On a direct Oracle or
      PostgreSQL connection the script says ``:name`` and the value is bound by name (or, on
      Oracle, ``&name`` as on the bridge) - :func:`named_parameter_values`.

    Returns the shape the rest of this runner reads — ``{"row_count", "result_sets",
    "truncated"}`` — which is not ``run-sql``'s own, so the mapping is right below and explained
    where it differs.
    """
    engine = sql_access.normalize_db_type(target.db_type)
    if engine not in sql_access.SQL_TASK_DB_TYPES:
        raise errors.InvalidConfig(f"Unsupported db_type: {target.db_type}")

    # The login this runner already resolved, stated whole: run-sql reads no configuration (rules
    # R09), so a server_id alone would be refused. The target stays as the answer's label.
    request: dict[str, Any] = {
        "target": target.server_id,
        "connection": task_connection(target=target, database=database, credential=credential,
                                      password=password),
        # The reference's spellings: `database` and `sql` are still read and never written, and
        # the first node to measure requests found this runner writing both on every run.
        "database_name": target.database_name or "",
        "sql_text": sql_text,
        "max_rows": target.capture_max_rows,
        "timeout_seconds": target.timeout_seconds,
        "connect_timeout_seconds": DEFAULT_CONNECT_TIMEOUT_SECONDS,
        "autocommit": bool(command.autocommit),
        "commit": not command.autocommit,
        # Every result set, uncapped, then cut to five below — the runner has always stored five
        # and counted the rows of all of them, and `row_count` would be short if the extra sets
        # were dropped before being counted.
        "capture": "all",
        "max_result_sets": 0,
        # The target's own transport, so `run-sql` routes an 8i host to the legacy tool exactly as
        # it would for an operator at a shell.
        "sql_access": target.sql_access or {},
    }
    if sql_access.is_legacy(target.sql_access):
        request["define"] = legacy_define_values(command, parameter_values)
        # The bridge's signing secret, from the store this runner already opened.
        request["secrets"] = request_fill.bridge_secrets(target.sql_access, secrets or {})
    elif engine in NAMED_BIND_DB_TYPES:
        # Not the T-SQL prelude: neither engine reads a DECLARE, and until 0.24.0 an Oracle task with
        # parameters failed at its first run on a direct connection (0.23.0 section 1.55).
        defines, named = named_parameter_values(command, parameter_values, sql_text, db_type=engine)
        if defines:
            request["define"] = defines
        if named:
            request["named_params"] = named
    else:
        prelude, bound = build_parameter_prelude(command.parameters, parameter_values or {})
        request["prelude"] = prelude
        request["params"] = bound

    success, result, error = common_cli.run_allowing_failure(
        "run-sql", request, timeout_seconds=run_deadline_seconds(target.timeout_seconds))
    if not success:
        raise errors.OperationFailed(error or "run-sql failed without a reason.")

    sets = result.get("result_sets") or []
    return {
        # `run-sql` reports fetched rows and affected rows separately; this runner has always
        # reported one number covering both, and `sql_runs.row_count` is read as such.
        "row_count": sum(int(item.get("row_count") or 0) for item in sets)
        + int(result.get("affected_rows") or 0),
        "result_sets": [
            {"columns": item.get("columns") or [], "rows": item.get("rows") or [],
             "truncated": bool(item.get("truncated"))}
            for item in sets[:MAX_STORED_RESULT_SETS]
        ],
        # Any set cut, not just a kept one: the count above included the rows of sets six and up,
        # so their truncation is part of whether this answer is complete.
        "truncated": any(bool(item.get("truncated")) for item in sets),
        # A SQL Server warning that ended the reading (lib/driver_warnings.py). The run is done;
        # the warning, and what it hid, are said rather than turned into a failure. Only present
        # when there is one, so a clean run's result keeps the shape every reader already has.
        **({"warnings": [str(item) for item in result["warnings"]]} if result.get("warnings") else {}),
    }


#: How many of its own timeouts a ``run-sql`` child may take before this runner stops it.
RUN_DEADLINE_FACTOR = 2


def run_deadline_seconds(timeout_seconds: int) -> int:
    """The wall-clock deadline on one ``run-sql`` child - the bound the task's timeout never was.

    ``timeout_seconds`` reaches the driver as a *query* timeout, and a query timeout is per call:
    it restarts on every batch and on every ``nextset()``, so a procedure that answers a stream of
    small results can run for ever without any single call timing out. The child had no other
    bound - it was started with no deadline at all - and the claim held by its live pid is never
    reaped (``run_claim``). On 2026-09-30 SQL033's third target, timeout 7200 s, logged its start
    at 02:00 +07 and nothing after it for 13 hours, until the container was stopped; 2026-09-28 had
    a 35-hour one (0.26.0 §1.70).

    Twice the timeout, not the timeout itself: the timeout bounds the statements, and a task of
    several batches may legitimately take longer in total than one of them may. Past twice, nothing
    is "legitimately longer" any more. Killing the child closes its connection; what the server does
    with a statement still running when that happens is the engine's, and is said in §1.70.
    """
    budget = max(int(timeout_seconds or 0), 1)
    return budget * RUN_DEADLINE_FACTOR + DEFAULT_CONNECT_TIMEOUT_SECONDS


def _deprecation_logger(logger: Any):
    """What the configuration reader hands back while reading, logged the way the runner always did."""
    if logger is None:
        return None
    return lambda message: log_deprecated_time_window_warnings(logger, (message,))


def log_deprecated_time_window_warnings(logger: Any, warnings: tuple[str, ...]) -> None:
    if logger is None:
        return
    for message in warnings:
        log_event(logger, level="warning", message=f"sql_tasks.runner.config.deprecated_time_window|scope=sql_tasks|message={format_log_value(message)}")


def resolve_sql_files(script_files: tuple[str, ...], *, data_dir: Path) -> list[Path]:
    return [resolve_sql_file(script_file, data_dir=data_dir) for script_file in script_files]


def resolve_sql_file(file_name: str, *, data_dir: Path) -> Path:
    path = Path(file_name)
    candidates = [path] if path.is_absolute() else [
        TOOL_ROOT / path,
        # The operator's own task SQL, then the built-ins that ship with the package. `tasks/` is
        # written per server and mirrored back from the worker, so the operator's copy wins.
        *asset_candidates("tasks", str(path)),
        data_dir / path,
    ]
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.exists():
            return resolved
    raise FileNotFoundError(f"SQL file not found: {file_name}")


def sql_run_time(row: Any | None) -> datetime | None:
    """When a ``sql_runs`` row **started**, as an aware UTC datetime.

    ``finished_at`` came first here, and it made this app the odd one out: a target declaring
    ``repeat_interval: 300`` whose task took 240 seconds ran every 540, and the config said 300.
    Every other ``time_window`` consumer anchors on the start, so the same number meant two things
    depending on which file it was written in. Changed 2026-09-19 to the shared
    :func:`db_ops.lib.time_window.run_anchor`, which is now the only place that picks the column.
    """
    return run_anchor(row)


def scrub_credential(credential: dict[str, Any] | None) -> dict[str, Any] | None:
    if credential is None:
        return None
    clean = dict(credential)
    clean.pop("password", None)
    return clean


def target_location(target: SqlTarget) -> str:
    """Where this target runs, as a message names it: ``server/instance.database`` on SQL Server."""
    return sql_task_target.location(server_id=target.server_id, db_type=target.db_type,
                                    instance_name=target.instance_name,
                                    service_name=target.service_name,
                                    database_name=target.database_name)


def task_connection(*, target: SqlTarget, database: dict[str, Any], credential: dict[str, Any],
                    password: str) -> dict[str, Any]:
    """The ``connection`` run-sql takes for this target: the instance and login resolved above."""
    return request_fill.connection_from(database, credential, password, server_id=target.server_id)


def diagnose_connect_failure(*, target: SqlTarget, error: str,
                             connection: dict[str, Any] | None = None) -> str | None:
    """What a failure to connect means, in the server's own terms - or ``None`` when ``error`` is
    not about connecting (a SQL error inside the script is reported as it is).

    Connect first, ask on failure: the database list is read from the server only when the
    database could not be opened, so a working target costs nothing and a new database needs no
    config change. The listing is ``common.cli list-databases`` - in ``master``, with the same
    login - the one command that answers it (rules R43); this app kept its own ``sys.databases``
    query through ``run-sql`` until 0.25.0.
    """
    kind = sql_task_target.classify_connect_failure(error)
    if kind is None:
        return None
    where = target_location(target)
    if kind == "login":
        return (f"the login of {target.credential_name or 'this target'} was refused on {where} "
                "(18456) - check its password_ref and that the login exists")
    if kind == "statement_timeout":
        return (f"a statement ran past this target's timeout ({target.timeout_seconds}s) on {where} - "
                "the instance answered; raise `timeout` in its time_window, or find what the "
                "statement waited on")
    if kind == "unreachable":
        return (f"could not reach {where} - the instance is down, its address or port is wrong, "
                "or something between them blocks it")
    database_name = sql_task_target.connect_database(target.db_type, target.database_name,
                                                     target.service_name)
    if not sql_task_target.is_sqlserver(target.db_type):
        return f"database '{database_name}' could not be opened on {where}"
    instance = f"{target.server_id}/{target.instance_name or sql_task_target.SQLSERVER_DEFAULT_INSTANCE}"
    if not connection:
        return (f"database '{database_name}' could not be opened on {instance}, and without the "
                "login the server's databases could not be listed")
    ok, answer, listing_error = common_cli.run_allowing_failure("list-databases", {
        "target": target.server_id,
        "connection": connection,
        # Every database the server has, `master` included: the message names what exists.
        "include_system": True,
        "timeout_seconds": 60,
        "sql_access": target.sql_access or {},
    })
    if not ok:
        return (f"database '{database_name}' could not be opened on {instance}, and the server's "
                f"databases could not be listed either: {listing_error}")
    names = [str(item.get("name")) for item in (answer or {}).get("databases") or []
             if isinstance(item, dict) and item.get("name")]
    return sql_task_target.missing_database_message(database_name=database_name, where=instance,
                                                    existing=names)


def find_database_inventory(target: SqlTarget, servers: list[dict[str, Any]]) -> dict[str, Any] | None:
    for server in servers:
        if str(server.get("server_id", "")) != target.server_id:
            continue
        for database in server.get("databases", []) or []:
            if str(database.get("db_type", "")).lower() != target.db_type.lower():
                continue
            instance_name = str(database.get("instance_name") or database.get("sid") or "")
            # The instance only: server_id + db_type + instance_name, which is what connecting needs.
            # `service_name` is not part of a SQL Server connection. `database_names` used to be a
            # gate here too, compared case-sensitively - and it is a list no code writes, so a
            # database created yesterday was refused and `APPDB_PROD` failed against `APPDB_Prod`
            # (SQL033, 2026-09-24). The server says whether a database exists, after connecting.
            if not sql_task_target.instance_matches(instance_name, target.instance_name, target.db_type):
                continue
            resolved = dict(database)
            resolved["server_id"] = server.get("server_id")
            resolved["company_code"] = server.get("company_code")
            resolved["ip"] = server.get("ip")
            return resolved
    return None


def find_database_credential(target: SqlTarget, credential_groups: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The credential this target runs as, or ``None`` — the caller reports the failed target.

    Selection is the shared :func:`db_ops.lib.data_sources.find_database_credential`; a task
    target must name its credential (it always has), and an unnamed or unknown one resolves to
    nothing rather than to a guess.
    """
    try:
        return data_sources.find_database_credential(
            credential_groups,
            server_id=target.server_id,
            credential_name=target.credential_name,
            db_type=target.db_type,
            service_name=target.service_name,
            instance_name=target.instance_name,
        )
    except data_sources.CredentialNotFound:
        return None


def credential_problem(target: SqlTarget, credential_groups: list[dict[str, Any]]) -> str:
    """Why :func:`find_database_credential` found nothing, in the shared lookup's words ("" if it did)."""
    try:
        data_sources.find_database_credential(
            credential_groups, server_id=target.server_id, credential_name=target.credential_name,
            db_type=target.db_type, service_name=target.service_name,
            instance_name=target.instance_name)
    except data_sources.CredentialNotFound as exc:
        return str(exc)
    return ""
