"""A SQL task's parameters: the values a run was given - named, positional or legacy DEFINEs - and how they bind into the SQL.

Split out of ``sql_tasks/runner.py`` on 2026-10-03 (Q11,
``audits/20261002_audit_typed_requests_and_errors.md`` section 5). ``runner`` re-exports every
name, so no import changes.
"""

from __future__ import annotations
from db_ops.lib.text_format import format_log_value, format_message_time  # noqa: F401 - one definition, see that module
from db_ops.lib.data_sources import _server_id_from_instance  # noqa: F401 - one definition, see that module
import json
from typing import Any
from db_ops.lib.sql_task_catalog import (  # noqa: F401 - re-exported for this module's callers
    _AMBIGUOUS_CREDENTIAL, DEFAULT_INLINE_MAX_ROWS, DEFAULT_SQL_TIMEOUT_SECONDS, INPUT_TYPES,
    SQL_TARGET_NOTIFY_DEFAULTS, XLSX_MAX_ROWS, SqlCommand, SqlTarget, _opt_str, collect_sql_tasks,
    load_default_credential_names, load_input_definition, load_sql_access_by_server,
    load_sql_commands, load_sql_script_definition, load_sql_targets, resolve_sql_folder)
from db_ops.lib.sql_text import (SqlParameterError, check_sqlplus_define_value, named_placeholders, sqlplus_substitution_names)
from db_ops.lib.paths import DEFAULT_DATA_DIR, REPO_ROOT, TOOL_ROOT  # noqa: F401 - one definition, see that module


def legacy_define_values(
    command: SqlCommand, parameter_values: dict[str, Any] | None,
) -> dict[str, str]:
    """The SQL*Plus substitutions for this run: supplied value, else the parameter's declared
    default, else whatever the script's own ``DEFINE`` line says.

    **A name the command does not declare is refused.** It would otherwise match no ``&VAR`` in
    the script and be silently dropped — so a run with ``jobno=`` instead of ``job_no=`` would
    quietly export the job number the archived script was last saved with, and look like it
    worked. A required parameter with no value is refused for the same reason.
    """
    declared = {str(p.get("name") or "").strip().lower(): dict(p) for p in command.parameters or ()}
    supplied = {str(name).strip().lower(): str(value)
                for name, value in (parameter_values or {}).items()}

    unknown = sorted(set(supplied) - set(declared))
    if unknown and declared:
        raise SqlParameterError(
            f"{command.sql_code} does not declare parameter(s) {', '.join(unknown)}; "
            f"it takes {', '.join(sorted(declared)) or 'none'}."
        )

    values: dict[str, str] = {}
    for name, parameter in declared.items():
        if supplied.get(name, "").strip():
            values[name] = supplied[name]
        elif str(parameter.get("default") or "").strip():
            values[name] = str(parameter["default"])
        elif bool(parameter.get("required", False)):
            raise SqlParameterError(
                f"{command.sql_code} requires parameter {name}: pass --param {name}=<value>."
            )
        else:
            # Left out on purpose: the script's own DEFINE line is then the value, which is how
            # an archived script keeps running exactly as it was saved.
            continue
        check_sqlplus_define_value(name, values[name])
    return values


def named_parameter_values(
    command: SqlCommand, parameter_values: dict[str, Any] | None, sql_text: str, *, db_type: str,
) -> tuple[dict[str, str], dict[str, Any]]:
    """This file's parameters on a direct Oracle or PostgreSQL connection, as ``(define, named)``.

    **The script says which it is.** ``:name`` is bound: the value never becomes SQL text, so a
    quote in it is data. On Oracle ``&name`` is a SQL*Plus substitution, exactly as on an 8i bridge
    target, so a command registered for both transports (``ORACLE-019`` is one) means the same on
    either; its value is checked as :func:`legacy_define_values` checks it, and with no value the
    script's own ``DEFINE`` stands. A parameter this file does not mention is left out: a folder
    task's other files may, and ``run-sql`` refuses a value no statement reads.

    As on SQL Server, a supplied value wins, then the declared ``default``, and a bound parameter
    with neither is NULL; ``required`` with no value is refused before anything connects. A name the
    command does not declare is refused, as on the bridge.
    """
    declared = {str(p.get("name") or "").strip().lower(): dict(p) for p in command.parameters or ()}
    supplied = {str(name).strip().lower(): value for name, value in (parameter_values or {}).items()}
    unknown = sorted(set(supplied) - set(declared))
    if unknown and declared:
        raise SqlParameterError(
            f"{command.sql_code} does not declare parameter(s) {', '.join(unknown)}; "
            f"it takes {', '.join(sorted(declared)) or 'none'}."
        )

    bound_here = named_placeholders(sql_text, db_type)
    substituted_here = sqlplus_substitution_names(sql_text) if db_type == "oracle" else set()
    defines: dict[str, str] = {}
    named: dict[str, Any] = {}
    for name, parameter in declared.items():
        given = supplied.get(name)
        if given is not None and str(given).strip():
            value = given
        elif "default" in parameter:
            value = parameter["default"]
        elif bool(parameter.get("required", False)):
            raise SqlParameterError(
                f"{command.sql_code} requires parameter {name}: pass --param {name}=<value>.")
        else:
            value = None
        if name in bound_here:
            named[name] = value
        if name in substituted_here and str(value if value is not None else "").strip():
            check_sqlplus_define_value(name, str(value))
            defines[name] = str(value)
    return defines, named


# `execute_legacy_oracle` lived here until 2026-08-16. It opened nothing — an 8i host has no
# connection db_ops can make — but it did decide *which transport* to use and then reshaped the
# bridge's answer into the runner's. `run-sql` routes `sql_access` itself and answers in one shape
# whichever transport replied, so both halves went away with it. What could not: deciding what a
# parameter *means* on that transport, which is `legacy_define_values` above and is config
# knowledge, not transport knowledge.


def parse_parameter_arguments(pairs: list[str] | None, joined: str = "") -> dict[str, str]:
    """``--param name=value`` repeated and/or ``--params "a=1 b=2"``, into one mapping.

    Two spellings because two kinds of caller: a shell or a scheduled command repeats a flag
    naturally, while a Telegram command renders one template per argv entry and has exactly one
    slot to put everything in. ``--params`` is split with shell quoting rules, so a value with
    spaces in it survives as ``"note=needs a look"``.

    Each pair splits on the FIRST ``=`` only: a value may legitimately contain one (a date range,
    a LIKE pattern), and splitting on all of them would silently truncate it.
    """
    import shlex

    items = list(pairs or [])
    if str(joined or "").strip():
        items.extend(shlex.split(str(joined)))
    values: dict[str, str] = {}
    positional: list[str] = []
    for pair in items:
        text = str(pair)
        # "-" is this bot's standing sentinel for "nothing here" (the conversation flow already
        # fills it in for a question that does not apply, see command_processor.skip_when). A
        # prompt that has to be answered needs a way to say "no values", and the alternative -
        # an empty message - is not something Telegram lets someone send.
        if text.strip() == "-":
            continue
        name, sep, value = text.partition("=")
        if not sep or not name.strip():
            # A bare value. Someone answering a prompt on a phone types the job number, not
            # `job_no=<job number>`, and being told "--param expects NAME=VALUE" for an answer
            # to a question that just named the parameter is the bot being obtuse about
            # something it already knows. It is bound to a declared parameter by position in
            # bind_parameter_values, where the task's own declaration is in scope.
            positional.append(text)
            continue
        values[name.strip()] = value
    if positional:
        values[POSITIONAL_PARAMETERS_KEY] = json.dumps(positional)
    return values


#: Where :func:`parse_parameter_arguments` parks values given without a name, until the task
#: that declares the names is loaded. A key no parameter can have (``=`` cannot appear in one),
#: so it can never collide with a real value.
POSITIONAL_PARAMETERS_KEY = "=positional="


def bind_parameter_values(
    command: SqlCommand, parameter_values: dict[str, Any] | None,
) -> dict[str, Any]:
    """Resolve values given without a name against the parameters this task declares, in order.

    ``--param session_id=1068`` and ``1068`` mean the same thing for a task whose first
    parameter is ``session_id``. Named values are placed first, then the bare ones fill the
    remaining parameters in declared order — so naming one and leaving the other positional
    still lands where the operator meant.

    A bare value for a task that declares nothing, or more bare values than there are
    parameters left, is an error that names what the task actually takes: silently dropping it
    would run the task with different arguments than the person typed.
    """
    values = dict(parameter_values or {})
    raw = values.pop(POSITIONAL_PARAMETERS_KEY, None)
    if not raw:
        return values
    positional = json.loads(raw) if isinstance(raw, str) else list(raw)

    declared = [
        str(item.get("name") or "").strip()
        for item in (command.parameters or ())
        if str(item.get("name") or "").strip()
    ]
    unfilled = [name for name in declared if name not in values]
    if len(positional) > len(unfilled):
        takes = ", ".join(declared) if declared else "no parameters"
        raise SqlParameterError(
            f"{command.sql_code} takes {takes}; got {len(positional)} value(s) with no name and "
            f"only {len(unfilled)} parameter(s) left to fill. Name them as NAME=VALUE."
        )
    for name, value in zip(unfilled, positional):
        values[name] = value
    return values
