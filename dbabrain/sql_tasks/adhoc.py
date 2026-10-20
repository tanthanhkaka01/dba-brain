"""``run-sql-adhoc`` - SQL that is not a registered task, against a ``server_id``, the login this node's.

Reading a server's DMVs by hand took ``common.cli run-sql`` and a scratch wrapper: ``run-sql`` works
from its request alone and reads no secret store (rules R09), so somebody had to decrypt the password
and send it on stdin - and a script is a missing command. It cannot be a ``common.cli`` command,
because reading the store is what R09 forbids there. It is an app's, the way a task's run is: the app
finishes the request from ``data/`` (``request_fill`` - the instance, its default login or the one
named, the password) and calls ``run-sql`` over ``dbabrain.transport``, the password on stdin.

**Read-only unless it says otherwise.** ``run-sql`` rolls back unless asked to commit, and that is
this command's default too: the operator reading a server's state is the common case and must cost
nothing. ``commit`` or ``autocommit`` is a write on a server nobody scheduled it for, so it costs one
typed ``yes`` through ``common.cli authorize`` (operation ``run-sql-adhoc``, level 50) - the ladder
every other operation of that weight is priced on.

The answer is ``run-sql``'s own, passed through: its JSON envelope, or the rendering the request asked
for in ``format``. Nothing is written to the runtime store - this is not a task run.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable

from dbabrain.lib import errors, response
from dbabrain.lib.json_io import read_json_request_answered
from dbabrain.transport import common_cli

COMMAND = "run-sql-adhoc"

#: Its name in ``emergency_operations.json``: what a write costs to authorize.
WRITE_OPERATION = "run-sql-adhoc"

#: Passed to ``run-sql`` as they are; ``run-sql`` checks their values.
RUN_SQL_OPTIONS = ("format", "output_path", "max_rows", "capture", "timeout_seconds",
                   "connect_timeout_seconds", "max_result_sets")
#: What this command reads itself.
OWN_FIELDS = ("server_id", "database", "credential_name", "sql_text", "sql_file",
              "commit", "autocommit", "confirm", "assume_yes", "reason")
ACCEPTED = OWN_FIELDS + RUN_SQL_OPTIONS
_FLAGS = ("commit", "autocommit", "assume_yes")

USAGE = (
    'run-sql-adhoc {"server_id": "<server_id>", "database": "master", "sql_file": "q.sql"}\n'
    "\n"
    "  One JSON object: inline, @file or - (on Windows use @file; single-quoted JSON breaks there).\n"
    "  server_id        the instance in db_instances.json (required)\n"
    "  database         the database to run in; omitted, run-sql's own choice (master on SQL Server)\n"
    "  credential_name  the login in users.json; omitted, the instance's default_credential_name\n"
    "  sql_text | sql_file   the SQL, exactly one; sql_file is read here (a leading @ is allowed)\n"
    "  commit / autocommit   true to keep what the SQL changed - asks for one typed yes first.\n"
    "                   Omitted, everything is rolled back.\n"
    "  confirm \"yes\", assume_yes, reason   the answer to that question, when not at a terminal\n"
    "  " + ", ".join(RUN_SQL_OPTIONS) + "   passed to run-sql as they are\n"
)


class AdhocRequestError(errors.InvalidRequest):
    """The request cannot be run as it is written."""


def checked(request: dict[str, Any], *, cwd: Path | None = None) -> dict[str, Any]:
    """``request`` checked, with the SQL read: every field this command or ``run-sql`` takes, and
    nothing else - a misspelt ``comit`` read as "not asked" would roll back what the operator meant
    to keep, and say success."""
    unknown = sorted(set(request) - set(ACCEPTED))
    if unknown:
        raise AdhocRequestError(f"{COMMAND}: unknown field(s) {', '.join(unknown)}; "
                                f"accepted: {', '.join(ACCEPTED)}.")
    server_id = request.get("server_id")
    if not isinstance(server_id, str) or not server_id.strip():
        raise AdhocRequestError(f"{COMMAND}: server_id is required, as a string.")
    for name in ("database", "credential_name", "sql_text", "sql_file", "reason"):
        if name in request and not isinstance(request[name], str):
            raise AdhocRequestError(f"{COMMAND}: {name} must be a string.")
    # `true` is intent and the terminal still asks; the word `yes` is an answer (common.confirm).
    if "confirm" in request and not (isinstance(request["confirm"], str) or request["confirm"] is True):
        raise AdhocRequestError(f'{COMMAND}: confirm must be "yes" or true.')
    for name in _FLAGS:
        if name in request and not isinstance(request[name], bool):
            raise AdhocRequestError(f"{COMMAND}: {name} must be true or false.")

    has_text = bool(str(request.get("sql_text") or "").strip())
    has_file = bool(str(request.get("sql_file") or "").strip())
    if has_text == has_file:
        raise AdhocRequestError(f"{COMMAND}: exactly one of sql_text and sql_file.")
    result = dict(request)
    result["server_id"] = server_id.strip()
    if has_file:
        # Read here, so the request run-sql receives is the whole of what runs: it never opens a
        # file the operator named relative to a directory it does not share.
        path = Path(str(request["sql_file"]).strip().removeprefix("@"))
        if not path.is_absolute() and cwd is not None:
            path = cwd / path
        try:
            result["sql_text"] = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            raise AdhocRequestError(f"{COMMAND}: sql_file {path}: {exc}") from exc
        result.pop("sql_file")
        if not result["sql_text"].strip():
            raise AdhocRequestError(f"{COMMAND}: sql_file {path} is empty.")
    return result


def writes(request: dict[str, Any]) -> bool:
    return request.get("commit") is True or request.get("autocommit") is True


def run_sql_request(request: dict[str, Any]) -> dict[str, Any]:
    """The ``run-sql`` request, before this node's login is filled in."""
    built: dict[str, Any] = {"target": request["server_id"], "sql_text": request["sql_text"]}
    if request.get("database"):
        built["database_name"] = request["database"]
    if request.get("credential_name"):
        built["credential_name"] = request["credential_name"]
    # Stated either way: run-sql's default is the rollback, and saying so keeps the request a
    # record of what was asked.
    built["commit"] = request.get("commit") is True
    built["autocommit"] = request.get("autocommit") is True
    built.update({name: request[name] for name in RUN_SQL_OPTIONS if name in request})
    return built


def finished(built: dict[str, Any], *, data_dir: Path) -> dict[str, Any]:
    """``built`` with the connection this node states for its target (rules R09)."""
    from dbabrain.lib.data_sources import request_fill

    try:
        return request_fill.fill_request("run-sql", built, data_dir=data_dir)
    except request_fill.RequestFillError as exc:
        raise errors.NotConfigured(f"{COMMAND}: {exc}") from exc


def masked(request: dict[str, Any]) -> dict[str, Any]:
    """``request`` fit to print: the password and every bridge secret replaced."""
    shown = json.loads(json.dumps(request, default=str))
    connection = shown.get("connection")
    if isinstance(connection, dict) and connection.get("password"):
        connection["password"] = "***"
    if isinstance(shown.get("secrets"), dict):
        shown["secrets"] = {name: "***" for name in shown["secrets"]}
    return shown


def authorize_write(request: dict[str, Any], *, data_dir: Path | None = None,
                    echo: Callable[[str], Any] | None = None) -> bool:
    """One typed ``yes`` before a write - asked at the terminal, or answered in the request, and
    priced on the ladder of the ``data_dir`` the login was read from."""
    from dbabrain.lib.data_sources import request_fill

    database = request.get("database") or "(default database)"
    gate: dict[str, Any] = {
        "operation": WRITE_OPERATION,
        "target_id": request["server_id"],
        "target_label": f"{request['server_id']} {database}",
        "effects": [f"runs {len(request['sql_text'])} characters of SQL that is not a registered task",
                    "autocommit: every statement is kept as it completes" if request.get("autocommit")
                    else "commit: what the SQL changed is kept"],
        # The field itself is the declared intent; the word `yes` is the separate answer.
        "confirm": (request["confirm"].strip() if isinstance(request.get("confirm"), str) else "")
                   or True,
        "reason": str(request.get("reason") or "ad-hoc SQL that writes"),
    }
    if request.get("assume_yes") is True:
        gate["assume_yes"] = True
    try:
        gate = request_fill.fill_request("authorize", gate, data_dir=data_dir)
        authorized, report, error = common_cli.run_allowing_failure("authorize", gate)
    except (common_cli.CommonCliError, request_fill.RequestFillError) as exc:
        # The gate could not be asked at all: a confirmation that fails open is not one.
        if echo is not None:
            echo(f"[FAIL] confirm: {exc}")
        return False
    if not authorized and echo is not None:
        for line in report.get("gates") or ():
            if str(line.get("status")) != "OK":
                echo(f"[{line.get('status')}] {line.get('name')}: {line.get('detail')}")
        if not (report.get("gates") or ()) and error:
            echo(f"[FAIL] confirm: {error}")
    return bool(authorized)


def main(source: str, *, data_dir: Path, dry_run: bool = False, logger: Any = None,
         log: Callable[..., Any] | None = None) -> int:
    """Run the request in ``source`` (inline JSON, ``@file`` or ``-``); the exit code of the answer."""
    def note(level: str, message: str) -> None:
        if logger is not None and log is not None:
            log(logger, level=level, message=message)

    def stderr(line: str) -> None:
        print(line, file=sys.stderr, flush=True)

    raw, code = read_json_request_answered(source, operation=COMMAND)
    if raw is None:
        return code
    try:
        request = checked(raw, cwd=Path.cwd())
        built = finished(run_sql_request(request), data_dir=data_dir)
    except (errors.InvalidRequest, errors.NotConfigured) as exc:
        return response.emit(response.fail(COMMAND, str(exc), kind=exc.kind))

    if dry_run:
        # A rehearsal asks nobody and runs nothing: it shows the request run-sql would receive,
        # which is how to see which login a server_id resolves to.
        print(json.dumps({"dry_run": True, "writes": writes(request), "request": masked(built)},
                         ensure_ascii=False, indent=1))
        return 0

    where = f"server_id={request['server_id']}|database={request.get('database') or ''}"
    if writes(request):
        if not authorize_write(request, data_dir=data_dir, echo=stderr):
            note("logging", f"sql_tasks.adhoc.refused|{where}|reason=not_confirmed")
            # An answer like every other: a caller parsing stdout reads the refusal, not silence.
            return response.emit(response.fail(
                COMMAND, f"{COMMAND} on {request['server_id']} was not authorized. Nothing ran.",
                kind=errors.KIND_REFUSED))
    note("logging", f"sql_tasks.adhoc.start|{where}|writes={writes(request)}"
                    f"|sql_chars={len(request['sql_text'])}")
    result, error = common_cli.spawn("run-sql", built)
    if result is None:
        note("error", f"sql_tasks.adhoc.failed|{where}|error={error}")
        return response.emit(response.fail(COMMAND, error))
    # run-sql's answer as it gave it - the envelope, or the rendering `format` asked for.
    sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    note("logging" if result.returncode == 0 else "error",
         f"sql_tasks.adhoc.done|{where}|returncode={result.returncode}")
    return int(result.returncode or 0)
