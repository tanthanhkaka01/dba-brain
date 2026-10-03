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

from db_ops.lib import errors
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


#: Engines whose SQL names a supplied value ``:name`` - a PL/SQL or SQL*Plus bind variable on
#: Oracle, a psql variable on PostgreSQL. Neither reads a T-SQL ``DECLARE``, which is what
#: `build_parameter_prelude` writes: until 0.24.0 an Oracle task with parameters failed at its first
#: run on a direct connection, and a PostgreSQL task could not declare any (0.23.0 section 1.55).
NAMED_BIND_DB_TYPES: tuple[str, ...] = ("oracle", "postgresql")


def check_named_values(raw: Any) -> dict[str, Any]:
    """``named_params`` as a mapping of lower-case names to scalar values, or refused by name.

    The name goes into no SQL text - the placeholder written for it is the driver's own - but it
    has to be one the script can say as ``:name``, so it is held to the same identifier pattern as
    a T-SQL parameter. A value is text, a number, true/false or null: a list or an object would
    reach PostgreSQL as an array or as JSON, which is not what a task parameter from a chat means.
    """
    if raw in (None, ""):
        return {}
    if not isinstance(raw, dict):
        raise SqlParameterError(
            f"named_params must be an object of name: value; got {type(raw).__name__}.")
    values: dict[str, Any] = {}
    for name, value in raw.items():
        key = str(name).strip()
        if not _SQL_IDENTIFIER_RE.fullmatch(key):
            raise SqlParameterError(
                f"Invalid parameter name {key!r}: letters, digits and underscore only.")
        if key.lower() in values:
            raise SqlParameterError(
                f"named_params names {key.lower()!r} twice; the SQL's :name ignores case.")
        if value is not None and not isinstance(value, (str, int, float, bool)):
            raise SqlParameterError(
                f"Parameter {key!r} is {type(value).__name__}; a bound value is text, a number, "
                "true/false or null.")
        values[key.lower()] = value
    return values


def named_placeholders(sql_text: str, db_type: str) -> set[str]:
    """The ``:name`` placeholders ``sql_text`` binds, lower-case - never one in a string or comment.

    What is *not* a placeholder matters as much: PostgreSQL's ``::int`` cast, PL/SQL's ``:=``, an
    array slice ``[1:n]``, a time format ``'HH24:MI'``, and anything inside quotes or comments.
    """
    return {match.group(1).lower()
            for kind, chunk in _code_pieces(sql_text, db_type) if kind == _CODE
            for match in _NAMED_BIND_RE.finditer(chunk)}


def sqlplus_substitution_names(sql_text: str) -> set[str]:
    """The ``&name`` / ``&&name`` markers an Oracle script substitutes, lower-case.

    Anywhere in the text, quotes included - SQL*Plus substitutes inside a literal too, which is
    how an archived script writes ``WHERE job_no = '&JOB_NO'``.
    """
    return {match.group(1).lower() for match in _SQLPLUS_MARKER_RE.finditer(str(sql_text or ""))}


def bind_named_values(
    statement: str, values: "dict[str, Any]", *, db_type: str, style: str,
) -> "tuple[str, list[Any]]":
    """``statement`` with each ``:name`` in ``values`` written as a positional placeholder, and the
    values to bind, in placeholder order.

    **Oracle** gets ``:1``, ``:2`` ... one per occurrence (``style`` is oracledb's ``numeric``): a name
    said twice is bound twice, which means the same in a SQL statement and in a PL/SQL block, where
    oracledb counts repeated names differently. **PostgreSQL** gets its own ``$1``, one per *name*,
    whatever pg8000's module-wide ``style`` is - pg8000 passes ``$n`` through and binds the list by
    number. One number per name is what lets the server type ``:d IS NULL OR day = :d``: it infers
    ``$1`` from ``day = $1``, where two numbers would leave the first with no type. A ``:name`` not
    in ``values`` is left as written: it may be something the engine reads itself (``:new`` in a
    trigger), and a real missing value is then the driver's own error, not a silent NULL.

    With nothing to bind the statement comes back untouched. That is not only tidy: pg8000 runs a
    statement without values through the simple protocol, where ``%`` is just ``%``, and through the
    extended one with them, where its ``format`` style reads every ``%`` outside a literal - so that
    is the only case in which one is doubled.
    """
    engine = str(db_type or "").strip().lower()
    wanted = {str(name).lower(): value for name, value in (values or {}).items()}
    pieces = _code_pieces(statement, engine)
    if not any(match.group(1).lower() in wanted
               for kind, chunk in pieces if kind == _CODE
               for match in _NAMED_BIND_RE.finditer(chunk)):
        return statement, []
    if (engine, style) not in _NAMED_STYLES:
        raise SqlParameterError(
            f"the {engine} driver in this process reads placeholders in the {style!r} style, which "
            "named values are not written in.")

    bound: list[Any] = []
    numbered: list[str] = []

    def _write(match: "re.Match[str]") -> str:
        name = match.group(1).lower()
        if name not in wanted:
            return match.group(0)
        if engine == "postgresql":
            if name not in numbered:
                numbered.append(name)
                bound.append(wanted[name])
            return f"${numbered.index(name) + 1}"
        bound.append(wanted[name])
        return f":{len(bound)}"

    parts: list[str] = []
    for kind, chunk in pieces:
        if kind == _QUOTED:
            parts.append(chunk)
            continue
        if style == "qmark" and "?" in chunk and not chunk.startswith("--"):
            # pg8000 has no escape for a `?` it would read as a placeholder: a jsonb `?` operator
            # would take a value meant for the next `:name`, and the rest would shift by one.
            raise SqlParameterError(
                "this statement has a '?' outside a string, which the driver in this process reads "
                "as a placeholder; it cannot also bind :name values. Write the value into the script.")
        if style == "format":
            chunk = chunk.replace("%", "%%")
        parts.append(_NAMED_BIND_RE.sub(_write, chunk) if kind == _CODE else chunk)
    return "".join(parts), bound


#: The driver styles a `$n` / `:n` statement survives. pg8000's `format` needs every other `%`
#: doubled and its `qmark` has no escape for a `?` - both handled in `bind_named_values`.
_NAMED_STYLES = {("oracle", "numeric"), ("postgresql", "format"), ("postgresql", "qmark")}

#: `:name`, not preceded by another `:` (a `::` cast) or by a name character (`[1:n]`, `a:b`).
_NAMED_BIND_RE = re.compile(r"(?<![:\w$#]):([A-Za-z_][A-Za-z0-9_$#]*)")

_SQLPLUS_MARKER_RE = re.compile(r"&&?([A-Za-z_][A-Za-z0-9_$#]*)")

_CODE, _COMMENT, _QUOTED = "code", "comment", "quoted"

#: Oracle's alternative quoting, `q'[ ... ]'`: the closing character for each opening one.
_Q_QUOTE_CLOSE = {"[": "]", "(": ")", "{": "}", "<": ">"}


def _code_pieces(text: str, db_type: str) -> "list[tuple[str, str]]":
    """``text`` cut into code, comments and quoted text, in order - the pieces joined are ``text``.

    Quoted is a string, a quoted name, an Oracle ``q'[...]'`` or a PostgreSQL ``$tag$`` body; a
    placeholder in any of them is text, not a bind.
    """
    text = str(text or "")
    engine = str(db_type or "").strip().lower()
    pieces: list[tuple[str, str]] = []
    code: list[str] = []
    i, n = 0, len(text)

    def _word_before(pos: int) -> bool:
        return pos > 0 and (text[pos - 1].isalnum() or text[pos - 1] in "_$#")

    def _take(kind: str, end: int) -> int:
        if code:
            pieces.append((_CODE, "".join(code)))
            code.clear()
        pieces.append((kind, text[i:end]))
        return end

    while i < n:
        char = text[i]
        if text.startswith("--", i):
            end = text.find("\n", i)
            i = _take(_COMMENT, n if end < 0 else end)
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = _take(_COMMENT, n if end < 0 else end + 2)
            continue
        if (engine == "oracle" and char in "qQ" and text.startswith("'", i + 1) and i + 2 < n
                and (not _word_before(i) or (text[i - 1] in "nN" and not _word_before(i - 1)))):
            close = _Q_QUOTE_CLOSE.get(text[i + 2], text[i + 2]) + "'"
            end = text.find(close, i + 3)
            i = _take(_QUOTED, n if end < 0 else end + 2)
            continue
        if char in "'\"":
            backslash = (char == "'" and engine == "postgresql" and i > 0
                         and text[i - 1] in "eE" and not _word_before(i - 1))
            j = i + 1
            while j < n:
                if backslash and text[j] == "\\":
                    j += 2
                    continue
                if text[j] == char:
                    if j + 1 < n and text[j + 1] == char:
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            i = _take(_QUOTED, min(j, n))
            continue
        if engine == "postgresql" and char == "$" and not _word_before(i):
            match = _DOLLAR_TAG_RE.match(text, i)
            if match:
                close = text.find(match.group(0), match.end())
                i = _take(_QUOTED, n if close < 0 else close + len(match.group(0)))
                continue
        code.append(char)
        i += 1
    if code:
        pieces.append((_CODE, "".join(code)))
    return pieces


#: The node's own keys, never a login's password - whatever a credential names (F12.2, G3.5).
_NODE_KEYS = frozenset({"DB_OPS_SECRET_KEY", "DB_OPS_KEY_BASE64", "TELEGRAM_BOT_TOKEN"})


def resolve_password(credential: dict[str, Any], secrets: dict[str, str]) -> str:
    """A configured credential's password: its value, or its ref - from the environment, then the
    store's ``secrets``.

    The environment still answers first here because these refs are the node's own configuration
    (``users.json``), not a request's choice; the transports' resolver, which a request reaches, no
    longer asks it (``lib.secret_value``, G3.5). What no credential may name is one of the node's
    own keys.
    """
    password_ref = str(credential.get("password_ref", "")).strip()
    if not password_ref:
        return str(credential.get("password", ""))
    if password_ref.upper() in _NODE_KEYS or password_ref.upper().endswith("BOT_TOKEN"):
        raise errors.Refused(f"{password_ref!r} is one of this node's own keys - it is never a password.")
    env_value = os.getenv(password_ref, "").strip()
    if env_value:
        return env_value
    if password_ref in secrets:
        return secrets[password_ref]
    raise errors.NotConfigured(f"Password ref not found in environment or secret_text.json: {password_ref}")


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


class SqlParameterError(errors.RequestError):
    """A task parameter is undeclared, mistyped, or missing — an operator message."""


class SqlRunError(errors.OperationFailed):
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


_DOLLAR_TAG_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")


def split_postgresql_statements(sql_text: str) -> list[str]:
    """A PostgreSQL script as its statements, in order - split on ``;`` outside quotes and comments.

    pg8000 runs a whole script in one execute and has no way to hand back more than one result set:
    a task of ``SELECT pg_sleep(1); INSERT ...; SELECT count(*) ...`` came back as one set whose
    rows were both SELECTs' under the last one's column name, and ``affected_rows`` 0 for the
    INSERT (measured 2026-09-25, adding PostgreSQL SQL tasks). Run one statement at a time, each
    set and each row count is its own.

    Aware of what may hold a ``;``: single-quoted strings (``''`` doubled, ``E'...'`` with
    backslash escapes), double-quoted identifiers, ``$$`` / ``$tag$`` dollar quoting (function
    bodies, ``DO`` blocks), ``--`` line comments and nested ``/* */`` block comments. A part that
    is only whitespace and comments is not a statement. The terminating ``;`` is not kept -
    PostgreSQL needs none.
    """
    text = str(sql_text or "")
    statements: list[str] = []
    buf: list[str] = []
    has_code = False
    i, n = 0, len(text)

    def _word_before(pos: int) -> bool:
        return pos > 0 and (text[pos - 1].isalnum() or text[pos - 1] in "_$")

    while i < n:
        char = text[i]
        if text.startswith("--", i):
            end = text.find("\n", i)
            end = n if end < 0 else end
            buf.append(text[i:end])
            i = end
            continue
        if text.startswith("/*", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if text.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif text.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            buf.append(text[i:j])
            i = j
            continue
        if char == "'":
            backslash = i > 0 and text[i - 1] in "eE" and not _word_before(i - 1)
            j = i + 1
            while j < n:
                if backslash and text[j] == "\\":
                    j += 2
                    continue
                if text[j] == "'":
                    if j + 1 < n and text[j + 1] == "'":
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            buf.append(text[i:j])
            has_code, i = True, j
            continue
        if char == '"':
            j = i + 1
            while j < n:
                if text[j] == '"':
                    if j + 1 < n and text[j + 1] == '"':
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            buf.append(text[i:j])
            has_code, i = True, j
            continue
        if char == "$" and not _word_before(i):
            match = _DOLLAR_TAG_RE.match(text, i)
            if match:
                tag = match.group(0)
                close = text.find(tag, match.end())
                end = n if close < 0 else close + len(tag)
                buf.append(text[i:end])
                has_code, i = True, end
                continue
        if char == ";":
            if has_code:
                statements.append("".join(buf).strip())
            buf, has_code, i = [], False, i + 1
            continue
        buf.append(char)
        if not char.isspace():
            has_code = True
        i += 1
    if has_code:
        statements.append("".join(buf).strip())
    return statements


_PLSQL_BLOCK_RE = re.compile(
    r"(?:BEGIN|DECLARE|CREATE\s+(?:OR\s+REPLACE\s+)?(?:(?:NON)?EDITIONABLE\s+)?"
    r"(?:PROCEDURE|FUNCTION|PACKAGE|TRIGGER|TYPE|LIBRARY))\b",
    re.IGNORECASE,
)


def _after_leading_comments(text: str) -> str:
    """``text`` from its first character that is neither whitespace nor inside a comment."""
    rest = str(text or "")
    while True:
        rest = rest.lstrip()
        if rest.startswith("--"):
            end = rest.find("\n")
            rest = "" if end < 0 else rest[end + 1:]
        elif rest.startswith("/*"):
            end = rest.find("*/")
            rest = "" if end < 0 else rest[end + 2:]
        else:
            return rest


def is_plsql_block(sql_text: str) -> bool:
    """Whether an Oracle batch is PL/SQL: an anonymous block, or the CREATE of a stored unit."""
    return bool(_PLSQL_BLOCK_RE.match(_after_leading_comments(sql_text)))


def oracle_statement(batch: str) -> str:
    """One Oracle batch as the driver must receive it: a statement without its ``;``, a block with.

    Oracle's SQL parser refuses a trailing ``;`` (``SELECT 1 FROM dual;`` raises ORA-00911), and its
    PL/SQL parser requires the one after ``END`` - without it ``BEGIN ... END`` raises PLS-00103.
    ``run-sql`` stripped the ``;`` from every batch, so an Oracle task with a block could never run
    (found 2026-09-25 registering the lab's Oracle tasks: ``BEGIN DBMS_SESSION.SLEEP(1); END;``).
    A SQL*Plus ``/`` line after a block ends it in a script and is not SQL, so it is not sent.
    """
    text = str(batch or "").rstrip()
    lines = text.splitlines()
    if lines and lines[-1].strip() == "/":
        text = "\n".join(lines[:-1]).rstrip()
    if is_plsql_block(text):
        return text
    return text.rstrip(";").rstrip()
