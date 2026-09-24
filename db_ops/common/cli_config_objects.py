"""``describe-object`` and ``due-check`` — the shared config objects, answerable from outside Python.

Two questions about the blocks every ``data/*.json`` shares, and neither of them had an answer a
program could ask for:

* *what does this field mean?* — read from ``data/shared_config_objects.json``
  (:mod:`db_ops.lib.shared_objects`), so the console and the bot can answer "what is
  ``retry_interval``" without anybody opening a docstring;
* *why is this not running?* — evaluated by :func:`db_ops.lib.time_window.explain_due`, **the same
  function the schedulers call**, so the explanation cannot disagree with the decision;
* *does one config file's pointer into another land anywhere?* — ``check-references``, the rule that
  a live failure asked for: an active backup named a ``server_id`` its node's inventory did not have,
  and the first report of it was the run that needed it;
* *does this estate's own configuration obey the reference?* — ``check-objects``, which walks every
  record at every path the reference declares and reports a required field missing, a value out of
  range, and a field nothing reads. Its first run over this estate found three things nobody had
  reported: ``sql_access.mode`` and ``sql_access.timeout_seconds`` used by 7 instances and described
  nowhere, and two of the reference's own paths pointing at ``backups[]`` where the schedule
  actually lives on ``backups[].jobs[]``.

``due-check`` is deliberately not how the daemon checks due-ness. The daemon sweeps every second
and metrics evaluate a window per target per metric, which is thousands of verdicts a pass: those
call the rule in-process, as an import, because ``lib`` is values and rules and a subprocess per
verdict would cost more than the work it schedules. This command is the same rule for everything
that is *not* that hot path — a Telegram question, a console panel, a runbook, another tool.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import Any

from db_ops.common import config_upgrade, field_migration
from db_ops.lib import config_references, response, shared_objects
from db_ops.lib.time_window import explain_due, parse_time_window_config, run_anchor

DESCRIBE_USAGE = """\
Usage: python -m db_ops.common.cli describe-object '<json>'|@file|-

What a config object's fields mean. Two kinds are described:
  - the SHARED blocks that appear inside many data/*.json records and are parsed once in
    db_ops/lib - time_window, notify, cmd_access, sql_access, ... and the shared field
    cleanup_retention;
  - the RECORDS a person edits whole - app_command, backup_entry, sql_command, db_instance,
    telegram_support_command, metric_definition, ...
{} lists them all; the list is the reference file's, so it never needs repeating here.

  {}                                  // list every object, with its field count
  {"object": "time_window"}           // one object: every field, required or not, range, default
  {"object": "time_window", "field": "repeat_interval"}   // one field

Answers from data/shared_config_objects.json, or from the copy shipped inside the package when a
node has none. It is reference: reading it changes nothing, and neither does editing it.
"""

DUE_USAGE = """\
Usage: python -m db_ops.common.cli due-check '<json>'|@file|-

Would this schedule run now, and if not, why not - evaluated by the same function the daemon,
sql_tasks, metrics and backup_restore all call, so the answer cannot disagree with the behaviour.

  {"time_window": {"from_hour": 1, "to_hour": 6, "repeat_interval": 1800,
                   "retry_interval": 600, "timeout": 600},
   "last_run": "2026-09-19T01:36:29Z",   // the previous run's START. Omit = never ran
   "last_status": "done",                // done | error | timeout | running | "" (never ran)
   "now": "2026-09-19T02:13:00Z",        // optional, UTC; default = this instant
   "local_now": "2026-09-19T09:13:00+07:00",  // optional; without it the hour window is NOT
                                              // checked, only the interval
   "retry_default": 60,                  // what this app falls back to (daemon 60, metrics 600,
   "timeout_default": 300,               //  sql_tasks/backup_restore: the repeat interval)
   "default_repeat": 300}

A store row may be passed as "row" instead of last_run/last_status - started_at, created_at or
finished_at, whichever it carries, read the way every scheduler reads it.

data: {"due", "reason", "last_run", "next_due_at"}
EVERY interval is measured from the previous run's START. repeat_interval 300 on a task that runs
for 240 seconds is due again 60 seconds after it finishes.
"""


def _describe(request: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    name = str(request.get("object") or request.get("name") or "").strip()
    field = str(request.get("field") or "").strip()
    if not name:
        objects = [
            {"object": item.get("object"), "kind": item.get("kind"),
             "one_line": item.get("one_line"), "field_count": item.get("field_count"),
             "required_field_count": item.get("required_field_count"),
             "parsed_in": item.get("parsed_in")}
            for item in shared_objects.load()
        ]
        return (f"{len(objects)} shared config object(s). Ask for one by name to see its fields.",
                {"objects": objects, "reference": str(shared_objects.reference_path())})
    found = shared_objects.describe(name, field=field)
    if field:
        return (f"{name}.{field}: {found.get('purpose') or ''}".strip(), {"field": found})
    return (f"{name}: {found.get('one_line') or ''} "
            f"{found.get('field_count')} field(s), {found.get('required_field_count')} required.",
            {"object": found})


def _parse_moment(value: Any, *, name: str) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 instant; got {value!r}.") from exc
    # A naive value is read as UTC rather than refused: every stored timestamp in db_ops is UTC
    # and written without an offset in places, so the common case is not a mistake.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _due(request: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    window = parse_time_window_config(request, context="due-check").time_window
    row = request.get("row")
    if isinstance(row, dict):
        last_run = run_anchor(row)
        last_status = str(row.get("status") or "")
    else:
        last_run = _parse_moment(request.get("last_run"), name="last_run")
        last_status = str(request.get("last_status") or "")
    now = _parse_moment(request.get("now"), name="now") or datetime.now(timezone.utc)

    local_now = _parse_moment(request.get("local_now"), name="local_now")
    if local_now is not None:
        from db_ops.lib.time_window import time_window_closed_reason

        closed = time_window_closed_reason(window, local_now)
        if closed:
            return (f"not due: {closed}",
                    {"due": False, "reason": closed,
                     "last_run": last_run.isoformat() if last_run else None,
                     "next_due_at": None})

    verdict = explain_due(
        last_run=last_run,
        last_status=last_status,
        repeat_interval=window.repeat_interval,
        retry_interval=(window.retry_interval if window.retry_interval is not None
                        else request.get("retry_default")),
        now=now,
        timeout=(window.timeout if window.timeout is not None
                 else request.get("timeout_default")),
        timeout_disabled=window.timeout == 0,
        default_repeat=request.get("default_repeat"),
    )
    message = "due now" if verdict.due else f"not due: {verdict.reason}"
    return (message, {
        "due": verdict.due,
        "reason": verdict.reason,
        "last_run": verdict.last_run.isoformat() if verdict.last_run else None,
        "next_due_at": verdict.next_due_at.isoformat() if verdict.next_due_at else None,
    })


CHECK_USAGE = """Usage: python -m db_ops.common.cli check-objects '<json>'|@file|-

Hold this node's own data/*.json to the shared reference. It walks every record at every path
data/shared_config_objects.json declares in `used_in`, and reports three things:

  missing   a required field is not there (six restore entries once carried no cleanup_retention,
            each running on a default nobody had chosen)
  value     the value is outside what the reference accepts - a range, an enum, or a secret field
            that must never appear in a config file at all
  unknown   a field the reference does not describe, so nothing reads it. Usually a typo, and a
            typo in a schedule is silent: the parser ignores what it does not recognise and the
            record runs on the default while looking configured

  {}                                  // this node's data/ directory
  {"data_dir": "D:/other/data"}       // another root, e.g. a bundle before importing it
  {"format": "txt"}                   // one line per finding, for a chat or a terminal

Two kinds are reported and never failed: `deprecated` (an older spelling the parser still reads)
and `unlisted` (a value outside an open enum, such as a Telegram level this estate defined itself).

data: {"records_walked", "objects_checked", "violations", "notices", "deprecated", "unlisted", "ok"}
Exit code is 0 when there are no violations, 1 when there are.
"""


def _check(request: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    result = shared_objects.check_data_dir(request.get("data_dir") or None)
    counts = {kind: sum(1 for item in result["violations"] if item["kind"] == kind)
              for kind in shared_objects.VIOLATION_KINDS}
    message = (
        f"{result['objects_checked']} shared object(s) in {result['records_walked']} record(s): "
        + ("no violations"
           if result["ok"] else
           ", ".join(f"{count} {kind}" for kind, count in counts.items() if count))
        + (f"; {len(result['notices'])} notice(s)" if result["notices"] else "")
    )
    if str(request.get("format") or "").strip().lower() == "txt":
        for item in result["violations"] + result["notices"]:
            print(f"{item['kind']:<10} {item['where']}.{item['field']}: {item['detail']}")
    return message, result


REFERENCES_USAGE = """\
Usage: python -m db_ops.common.cli check-references '<json>'|@file|-

Follow every pointer one config file makes into another, and report the ones that land nowhere.
data/ is a small relational database with no foreign keys: a backup job names a server_id, a SQL
target names a credential_name, an instance names the OS login that reaches its host.

  {}                                  // this node's data/ directory
  {"data_dir": "D:/other/data"}       // another root - a bundle, or a worker's copy
  {"format": "txt"}                   // one line per finding

The rules are data: data/config_references.json. Only ACTIVE records are failed; an inactive entry
pointing at something that is gone is how an estate retires a target, so those are listed under
`inactive` instead. A password_ref is NOT checked here - the secret store is encrypted, so that
question belongs to `check-secret`, which answers it by authenticating.

data: {"pointers_checked", "dangling", "inactive", "unreadable", "ok"}
Exit code is 0 when nothing active dangles, 1 when something does.

This is the check that would have caught, a day early:
  backup_restore.backup ERROR: .../wal: server_id not found in db_instances.json: A1A-...-PG-5433
"""


def _references(request: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    result = config_references.check(request.get("data_dir") or None)
    message = (f"{result['pointers_checked']} pointer(s): "
               + ("none dangling" if result["ok"] else f"{len(result['dangling'])} DANGLING")
               + (f", {len(result['inactive'])} on inactive record(s)" if result["inactive"] else "")
               + (f", {len(result['unreadable'])} rule(s) skipped" if result["unreadable"] else ""))
    if str(request.get("format") or "").strip().lower() == "txt":
        for item in result["dangling"]:
            print(f"DANGLING  {item['where']} -> {item['value']}: {item['detail']}")
        for item in result["inactive"]:
            print(f"inactive  {item['where']} -> {item['value']}: {item['detail']}")
        for item in result["unreadable"]:
            print(f"skipped   {item['rule']}: {item['file']} {item['detail']}")
    return message, result


def _upgrade(request: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    return config_upgrade.upgrade(request)


def _standardize(request: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    return field_migration.standardize(request)


_COMMANDS = {
    "describe-object": (_describe, DESCRIBE_USAGE),
    "due-check": (_due, DUE_USAGE),
    "check-objects": (_check, CHECK_USAGE),
    "check-references": (_references, REFERENCES_USAGE),
    "standardize-field-names": (_standardize, field_migration.USAGE),
    "upgrade-config": (_upgrade, config_upgrade.USAGE),
}


def run(operation: str, argv: list[str], *, read_request: Any) -> int:
    action, usage = _COMMANDS[operation]

    if argv and argv[0] in {"-h", "--help"}:
        print(usage)
        return 0
    if len(argv) > 1:
        return response.emit(response.fail(
            operation, f"{operation} takes one JSON payload; got {len(argv)} arguments."))

    # `describe-object` with no argument lists what there is, which is the one thing somebody who
    # does not know the vocabulary can usefully ask for.
    request: dict[str, Any] = {}
    if argv:
        request, code = read_request(argv[0], usage)
        if request is None:
            return code
    elif operation == "due-check":
        print(usage, file=sys.stderr)
        return 2

    try:
        message, data = action(request)
    except Exception as exc:  # noqa: BLE001 - reported as JSON, like every command here.
        return response.emit(response.fail(operation, str(exc)))
    code = response.emit(response.ok(operation, message=message, data=data))
    # A check that finds violations must not exit 0: this is run in a gate, and a green exit with a
    # populated `violations` list is exactly the shape nobody reads.
    if operation in {"check-objects", "check-references"} and not data.get("ok", True):
        return 1
    # A migration that left a file unwritten for a conflict has not done what it was asked.
    if operation in {"standardize-field-names", "upgrade-config"} and data.get("conflicts"):
        return 1
    return code
