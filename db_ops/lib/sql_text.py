"""SQL text and result limits — the parts of running a query that are not the running.

Split out of ``common/sql_execution.py`` and ``common/sql_run.py`` on 2026-08-15, when apps
stopped importing ``common``. Connecting and executing are operations and stayed there; building a
``DECLARE`` prelude, expanding ``sqlplus`` defines, resolving a password out of a secrets dict
already in hand, and knowing how many rows is too many are all pure functions of their arguments,
and the apps need them while *preparing* a request — before anything is connected to.

``build_parameter_prelude`` is the one to read twice: the value is always a bind parameter, never
interpolated. That is the whole reason it exists, and it is why it must not be re-implemented
app-side where a shortcut would be invisible.
"""

from __future__ import annotations

import os
import re
from typing import Any


MAX_RESULT_ROWS = 100


#: T-SQL types a task parameter may declare. An allow-list because the type is written into a
#: ``DECLARE`` and therefore into SQL text — the *value* is bound, but the type cannot be. Anything
#: outside this set is refused rather than passed through, so a config edit cannot smuggle SQL in
#: through a field nobody thinks of as executable.
SQL_PARAMETER_TYPES: frozenset[str] = frozenset({
    "bit", "tinyint", "smallint", "int", "bigint",
    "decimal", "numeric", "float", "real", "money",
    "date", "time", "datetime", "datetime2", "smalldatetime",
    "char", "varchar", "nchar", "nvarchar",
    "uniqueidentifier",
})


def build_parameter_prelude(
    parameters: "list[dict[str, Any]] | tuple[dict[str, Any], ...]",
    values: "dict[str, Any]",
) -> "tuple[str, list[Any]]":
    """``DECLARE`` lines for a script's parameters, plus the values to bind to them.

    The script author writes ordinary T-SQL against ``@name``; this puts the declaration in front
    of it. The **value is a bind parameter (`?`), never interpolated** — the whole point, since a
    task parameter arrives from a Telegram message. Only the name and the type reach the SQL text,
    and both are validated against `SQL_PARAMETER_TYPES` and an identifier pattern first.

    Returned as `(prelude, bound_values)` so the caller can prepend the prelude to **each** batch:
    a T-SQL variable does not survive a `GO`, so a multi-batch script needs the declaration
    repeated, with the same values bound again.
    """
    prelude_parts: list[str] = []
    bound: list[Any] = []
    for parameter in parameters or ():
        name = str(parameter.get("name") or "").strip()
        if not _SQL_IDENTIFIER_RE.fullmatch(name):
            raise SqlParameterError(
                f"Invalid parameter name {name!r}: letters, digits and underscore only.")
        declared_type = str(parameter.get("type") or "nvarchar(4000)").strip()
        base_type = declared_type.split("(", 1)[0].strip().lower()
        if base_type not in SQL_PARAMETER_TYPES:
            raise SqlParameterError(
                f"Parameter {name!r} declares type {declared_type!r}; allowed: "
                f"{sorted(SQL_PARAMETER_TYPES)}.")
        if not _SQL_TYPE_RE.fullmatch(declared_type):
            raise SqlParameterError(f"Parameter {name!r} has a malformed type: {declared_type!r}.")
        if name in values and str(values[name]).strip() != "":
            value = values[name]
        elif "default" in parameter:
            value = parameter["default"]
        elif bool(parameter.get("required", False)):
            raise SqlParameterError(f"Missing required parameter: {name}.")
        else:
            value = None
        prelude_parts.append(f"DECLARE @{name} {declared_type} = ?;")
        bound.append(value)
    return ("\n".join(prelude_parts) + "\n" if prelude_parts else "", bound)


def resolve_password(credential: dict[str, Any], secrets: dict[str, str]) -> str:
    password_ref = str(credential.get("password_ref", "")).strip()
    if not password_ref:
        return str(credential.get("password", ""))
    env_value = os.getenv(password_ref, "").strip()
    if env_value:
        return env_value
    if password_ref in secrets:
        return secrets[password_ref]
    raise RuntimeError(f"Password ref not found in environment or secret_text.json: {password_ref}")


# Upper bound on rows returned by one run. A SELECT bigger than this is truncated (and the
# caller is told), so one careless query cannot pull an unbounded result into memory.
DEFAULT_MAX_ROWS = 50_000


#: How long to wait for a *statement* to finish once connected.
DEFAULT_TIMEOUT_SECONDS = 30

#: How long to wait for the connection itself. A different question from the one above,
#: and spelled out three times — db_connect, sql_execution and the sql_tasks runner all
#: carried their own copy of the same number for the same reason.
DEFAULT_CONNECT_TIMEOUT_SECONDS = 30


def check_sqlplus_define_value(name: str, value: str) -> None:
    """Refuse a substitution value that could change the statement's meaning.

    Substitution is textual — that is what SQL*Plus does and what makes ``&`` usable in places a
    bind variable is not legal — so a value is not escaped by the driver the way a bound
    parameter is. That is acceptable for a job number typed into a Telegram command and not
    acceptable for a value carrying a quote, a comment marker or a statement separator, which
    would be running the caller's SQL rather than the task's. A task that needs to pass such a
    value wants a bind variable and a target that supports one.
    """
    text = str(value)
    for marker in _UNSAFE_IN_DEFINE:
        if marker in text:
            raise SqlRunError(
                f"Parameter {name!r} contains {marker!r}, which is not allowed in a SQL*Plus "
                "substitution: the value is pasted into the statement text, so it could change "
                "what the statement does."
            )


def expand_sqlplus_defines(sql_text: str, overrides: Any = None) -> str:
    """Resolve SQL*Plus ``DEFINE``/``&var`` substitutions the way SQL*Plus would, then drop the
    ``DEFINE`` lines.

    Only what SQL*Plus itself does before sending a statement: no driver has ever seen ``&JOB_NO``
    and none should — a bind variable is the right tool for a *value the caller supplies*, but an
    archived script's ``&`` markers are textual and appear in places (a table name, a whole
    predicate) where a bind is not legal. ``overrides`` wins over the file's own DEFINE, so the
    stored script stays the shipped one.

    Substitution is textual, exactly like SQL*Plus: whatever the value is becomes part of the
    statement, quotes included or not as the script wrote them. Anything still undefined is left
    untouched rather than blanked, so the error names the variable instead of producing a
    silently different query.
    """
    values: dict[str, str] = {}
    for name, raw_value in _DEFINE_LINE.findall(sql_text or ""):
        value = raw_value.strip().rstrip(";").strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[name.upper()] = value
    for name, value in (overrides or {}).items():
        values[str(name).upper()] = str(value)
    if not values:
        return sql_text

    body = _DEFINE_LINE.sub("", sql_text)
    # `&&NAME` (SQL*Plus's "define it permanently" form) first: replacing `&NAME` first would
    # leave a stray `&` behind.
    for marker in ("&&", "&"):
        for name, value in values.items():
            body = re.sub(
                re.escape(marker) + name + r"\.?(?![A-Za-z0-9_$#])",
                lambda _match, replacement=value: replacement,
                body,
                flags=re.IGNORECASE,
            )
    return body.strip()


#: A parameter name goes into the SQL text as `@name`, so it is held to an identifier.
_SQL_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


#: And the type to `base` or `base(n)` / `base(n,m)` / `base(max)` — nothing else reaches SQL.
_SQL_TYPE_RE = re.compile(r"[A-Za-z0-9_]+(\(\s*(\d+|max)\s*(,\s*\d+\s*)?\))?", re.IGNORECASE)


class SqlParameterError(ValueError):
    """A task parameter is undeclared, mistyped, or missing — an operator message."""


class SqlRunError(RuntimeError):
    """A user-facing failure: unknown target, no credential, connect refused, bad SQL."""


# A SQL*Plus DEFINE line: `DEFINE name = value`, `DEF name value`, quoted or not. Anchored to the
# start of a line so the word DEFINE inside a string literal or a comment is left alone.
_DEFINE_LINE = re.compile(
    r"^[ \t]*DEF(?:INE)?[ \t]+([A-Za-z_][A-Za-z0-9_$#]*)[ \t]*(?:=[ \t]*)?(.*)$",
    re.IGNORECASE | re.MULTILINE,
)


#: Characters that end a SQL string literal, comment out the rest of a statement, or start a
#: second one. A DEFINE value is pasted into the SQL text, so any of them lets a supplied value
#: change what the statement means rather than what it selects.
_UNSAFE_IN_DEFINE = ("'", '"', ";", "--", "/*", "*/", "\n", "\r", "&")
