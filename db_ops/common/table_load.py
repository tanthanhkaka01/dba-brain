"""Create one table from a spreadsheet and load its rows, on any engine db_ops knows.

The operational shape this exists for: somebody has a file and needs it queryable *now* —
a list of job numbers to join against, a vendor's stock file, a reconciliation extract. What
happened before was a hand-built `CREATE TABLE` and an import wizard on somebody's laptop, which
is how a column silently becomes `float` and an order number loses its leading zero.

**Either an .xlsx or a delimited text file** — .txt, .csv, .tsv, a block pasted out of Excel.
The format is detected from the bytes, not from the file name, because the name is the least
reliable thing about an attachment: a workbook is a zip and announces itself as one, and anything
else is read as text through :mod:`db_ops.lib.delimited_import`. Both produce the same header
row plus text rows, so the rest of this module does not know which arrived.

**Input is a JSON object** carrying the file as base64, so the same call works from a shell,
from another program, and from a Telegram message with a file attached — nothing has to reach a
shared filesystem for the two sides to agree on a path:

    {"target": "ACME-192-0-2-248",
     "database": "Staging",
     "schema": "dbo",
     "file_base64": "UEsDBBQ...",     // or "file_path" for a local file
     "delimiter": "",                 // text files only; blank -> guessed from the header line
     "table_name": "",                // optional; blank -> temp_<random>
     "if_exists": "error",            // error (default) | drop | append
     "load_rows": true,               // false = create the structure only
     "text_length": 4000}

``xlsx_base64`` / ``xlsx_path`` are accepted as the older names for ``file_base64`` /
``file_path`` and mean exactly the same thing — a stored Telegram command config and a saved
shell payload both still name them.

**Every column is text.** A spreadsheet column holds whatever its author typed, and a type
guessed from the first twenty rows is wrong on row two thousand in a way nobody notices until a
join returns nothing. `NVARCHAR(4000)` — or the engine's equivalent — is the honest type for a
value that came out of Excel; the caller who knows better can ALTER afterwards, having seen the
data.

Connection, target resolution and credentials all come from :mod:`db_ops.common.sql_run`. This
module owns exactly three things that module does not: quoting an identifier per engine, binding
values per driver, and the create-then-load transaction.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from db_ops.common import db_connect
from db_ops.lib import delimited_import
from db_ops.common import sql_run
from db_ops.lib import xlsx_import


class TableLoadError(RuntimeError):
    """A user-facing failure: bad workbook, table already there, a value too long, load refused."""


#: How each engine spells "text, up to N characters". PostgreSQL is given `varchar(n)` rather
#: than `text` so the declared width means the same thing on every engine and an over-long value
#: is refused by the database as well as by the check here. MySQL gets `TEXT` instead: its
#: 65,535-byte row limit is on the *row*, so a 40-column table of `varchar(4000)` utf8mb4 cannot
#: be created at all, and the failure names the row size rather than the column that caused it.
_TEXT_TYPE = {
    "sqlserver": lambda n: f"NVARCHAR({n})" if n <= 4000 else "NVARCHAR(MAX)",
    "postgresql": lambda n: f"varchar({n})",
    "oracle": lambda n: f"VARCHAR2({min(n, 4000)} CHAR)",
    "mysql": lambda _n: "TEXT",
}

#: Doubling the closing quote is the escape on all four engines; only the character differs.
_QUOTES = {
    "sqlserver": ("[", "]"),
    "postgresql": ('"', '"'),
    "oracle": ('"', '"'),
    "mysql": ("`", "`"),
}

#: An identifier cannot be a bind parameter, so anything that reaches the SQL text is checked
#: rather than trusted. Quoting makes spaces and punctuation safe; a control character or an
#: embedded NUL is refused outright because no legitimate column heading contains one.
_FORBIDDEN_IN_IDENTIFIER = re.compile(r"[\x00-\x1f\x7f]")

#: Rows per `executemany`. Large enough that a 100k-row sheet is not 100k round trips, small
#: enough that a failed batch names a bounded range of rows.
BATCH_SIZE = 1000

DEFAULT_TEXT_LENGTH = 4000
IF_EXISTS_CHOICES = ("error", "drop", "append")

#: Engines where DDL commits implicitly, so create-then-load cannot be one transaction. Named
#: here because the caller is told about it in the response rather than discovering it after a
#: failed load left an empty table behind.
_DDL_IS_TRANSACTIONAL = {"sqlserver": True, "postgresql": True, "oracle": False, "mysql": False}


def quote_identifier(db_type: str, name: str) -> str:
    """One identifier, quoted for this engine, with its closing quote doubled."""
    text = str(name or "").strip()
    if not text:
        raise TableLoadError("An identifier cannot be empty.")
    if _FORBIDDEN_IN_IDENTIFIER.search(text):
        raise TableLoadError(f"Identifier {text!r} contains a control character.")
    try:
        opening, closing = _QUOTES[db_type]
    except KeyError:
        raise TableLoadError(f"No identifier quoting rule for engine {db_type!r}.") from None
    return f"{opening}{text.replace(closing, closing * 2)}{closing}"


def placeholder_style(db_type: str, connection: Any = None) -> str:
    """The paramstyle the *open connection* will read, asked rather than assumed.

    Delegates to :func:`db_ops.common.db_connect.parameter_style`, which owns every fact about
    which driver answers. Kept as a name here because it is what this module's callers and tests
    reach for, and because the error it raises should be this module's.

    The connection is not optional in practice: on SQL Server it decides between ``?`` (pyodbc)
    and ``%s`` (the pymssql fallback), and getting it from ``db_type`` alone is right on a
    developer's Windows box and wrong in the Linux worker.
    """
    try:
        return db_connect.parameter_style(db_type, connection)
    except db_connect.DbConnectError as exc:
        raise TableLoadError(str(exc)) from exc


def build_placeholders(style: str, count: int) -> str:
    """``?, ?, ?`` / ``%s, %s, %s`` / ``:1, :2, :3`` for one row's worth of values."""
    if style == "qmark":
        return ", ".join("?" for _ in range(count))
    if style == "numeric":
        return ", ".join(f":{index}" for index in range(1, count + 1))
    if style in ("format", "pyformat"):
        return ", ".join("%s" for _ in range(count))
    raise TableLoadError(f"Unsupported driver paramstyle {style!r}.")


def build_create_table(db_type: str, *, schema: str, table: str, columns: list[str],
                       text_length: int) -> str:
    """The CREATE TABLE for this sheet: every column text, in the sheet's own order."""
    try:
        text_type = _TEXT_TYPE[db_type](text_length)
    except KeyError:
        raise TableLoadError(f"create-table-from-xlsx does not know engine {db_type!r}.") from None
    qualified = _qualified_name(db_type, schema, table)
    body = ",\n    ".join(f"{quote_identifier(db_type, name)} {text_type}" for name in columns)
    return f"CREATE TABLE {qualified} (\n    {body}\n)"


def _qualified_name(db_type: str, schema: str, table: str) -> str:
    quoted_table = quote_identifier(db_type, table)
    if not schema:
        return quoted_table
    return f"{quote_identifier(db_type, schema)}.{quoted_table}"


def two_placeholders(style: str) -> tuple[str, str]:
    """The first two placeholders of this style, as separate strings.

    Built together rather than by calling :func:`build_placeholders` twice: under ``numeric``
    that would produce ``:1`` for both parameters, and Oracle would bind the table name to the
    schema as well — a lookup that silently matches nothing.
    """
    first, second = build_placeholders(style, 2).split(", ")
    return first, second


def _table_exists(cursor: Any, db_type: str, schema: str, table: str, style: str) -> bool:
    """Whether the target table is already there, asked of the catalog rather than by trying.

    `CREATE TABLE` and catching the error would work on one engine and mean four different error
    codes on four; and on Oracle the failed statement would already have committed the DDL that
    preceded it.
    """
    first, second = two_placeholders(style)
    if db_type == "sqlserver":
        sql = (f"SELECT COUNT(*) FROM sys.tables t JOIN sys.schemas s "
               f"ON s.schema_id = t.schema_id WHERE t.name = {first} AND s.name = {second}")
        params: tuple[Any, ...] = (table, schema or "dbo")
    elif db_type == "postgresql":
        sql = (f"SELECT COUNT(*) FROM information_schema.tables "
               f"WHERE table_name = {first} AND table_schema = {second}")
        params = (table, schema or "public")
    elif db_type == "mysql":
        sql = (f"SELECT COUNT(*) FROM information_schema.tables "
               f"WHERE table_name = {first} AND table_schema = {second}")
        params = (table, schema)
    elif db_type == "oracle":
        # Oracle folds an unquoted identifier to upper case when it stores it, and this module
        # creates its tables unquoted-equivalent; comparing the caller's spelling as typed would
        # miss the table it just made.
        sql = f"SELECT COUNT(*) FROM all_tables WHERE table_name = {first} AND owner = {second}"
        params = (table.upper(), (schema or "").upper())
    else:
        raise TableLoadError(f"No existence check for engine {db_type!r}.")
    cursor.execute(sql, params)
    row = cursor.fetchone()
    return bool(row and int(row[0]) > 0)


def _check_lengths(columns: list[str], rows: list[list[str]], limit: int) -> None:
    """Refuse an over-long value instead of clipping it.

    A clipped value looks complete. An order number one character short joins to nothing and the
    report is quietly wrong, which is a far more expensive outcome than a load that stops and
    says which cell was too long.
    """
    for row_number, row in enumerate(rows, start=2):  # row 1 is the header, in the user's terms
        for index, value in enumerate(row):
            if value is not None and len(value) > limit:
                name = columns[index] if index < len(columns) else f"column_{index + 1}"
                raise TableLoadError(
                    f"Row {row_number}, column {name!r}: value is {len(value)} characters, over "
                    f"the {limit}-character column width. Raise \"text_length\" (SQL Server "
                    f"supports NVARCHAR(MAX) above 4000) or fix the cell."
                )


#: The first four bytes of every zip, and therefore of every .xlsx. Anything else is read as
#: delimited text — see :func:`read_source`.
_ZIP_MAGIC = b"PK\x03\x04"

#: The OLE2 compound-document header: a real `.xls` from Excel 97-2003. Recognised only to say so,
#: because it is neither a zip nor text and reading it as either produces nonsense.
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def read_source(payload: Any, *, max_rows: Any = None, delimiter: Any = "") -> dict[str, Any]:
    """Read the file, whatever it is: a workbook's first sheet, or a delimited text file.

    Detection is on the bytes. A `.xlsx` is a zip and starts `PK\\x03\\x04`; nothing else does.
    Trusting the file name instead would misread the two attachments operators actually send —
    a `.txt` that is really tab-separated data, and a `.xlsx` that somebody renamed.

    Both readers raise their own error type; both become :class:`TableLoadError` here, so every
    caller of this module has one exception to catch and the CLI cannot traceback on a bad file.
    """
    try:
        data = xlsx_import.decode_payload(payload, field="file_base64")
    except xlsx_import.XlsxImportError as exc:
        raise TableLoadError(str(exc)) from exc

    read_kwargs: dict[str, Any] = {}
    if max_rows:
        read_kwargs["max_rows"] = int(max_rows)

    if data.startswith(_OLE2_MAGIC):
        raise TableLoadError(
            "This is an old .xls (Excel 97-2003), which is a different format entirely — not a "
            "zip and not text. Re-save it as .xlsx, or as .txt / .csv, and send that."
        )
    try:
        if data.startswith(_ZIP_MAGIC):
            return xlsx_import.read_sheet(data, **read_kwargs)
        return delimited_import.read_table(data, delimiter=delimiter, **read_kwargs)
    except (xlsx_import.XlsxImportError, delimited_import.DelimitedImportError) as exc:
        raise TableLoadError(str(exc)) from exc


def _parse_request(request: Any) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise TableLoadError("request must be a JSON object.")
    target = str(request.get("target") or "").strip()
    if not target:
        raise TableLoadError('request needs a "target" (a server_id, or "<db_type> <ip> [port]").')

    # `xlsx_*` are the original names and stay: they are what `data/telegram_support_commands.json`
    # declares as the awaited parameter, and renaming the key would have broken every stored
    # command config on the worker at the moment the image was deployed.
    payload = request.get("file_base64") or request.get("xlsx_base64")
    path = str(request.get("file_path") or request.get("xlsx_path") or "").strip()
    if not payload and path:
        file_path = Path(path)
        if not file_path.is_file():
            raise TableLoadError(f"file_path not found: {file_path}")
        payload = file_path.read_bytes()
    if not payload:
        raise TableLoadError('request needs "file_base64" (or "file_path").')

    if_exists = str(request.get("if_exists") or "error").strip().lower()
    if if_exists not in IF_EXISTS_CHOICES:
        raise TableLoadError(
            f'if_exists must be one of {", ".join(IF_EXISTS_CHOICES)}; got {if_exists!r}.'
        )
    try:
        text_length = int(request.get("text_length") or DEFAULT_TEXT_LENGTH)
    except (TypeError, ValueError):
        raise TableLoadError('"text_length" must be a whole number.') from None
    if text_length < 1:
        raise TableLoadError('"text_length" must be at least 1.')

    return {
        "target": target,
        "database": str(request.get("database") or "").strip(),
        "schema": str(request.get("schema") or "").strip(),
        "table_name": str(request.get("table_name") or "").strip(),
        "payload": payload,
        "if_exists": if_exists,
        "load_rows": bool(request.get("load_rows", True)),
        "text_length": text_length,
        "max_rows": request.get("max_rows"),
        "delimiter": request.get("delimiter") or "",
        "credential_name": str(request.get("credential_name")
                               or request.get("user_ref") or "").strip(),
        "timeout_seconds": request.get("timeout_seconds"),
        "data_dir": request.get("data_dir"),
        "sql_access": request.get("sql_access"),
    }


def _default_schema(db_type: str, resolved: dict[str, Any]) -> str:
    """The schema to use when the caller names none."""
    if db_type == "sqlserver":
        return "dbo"
    if db_type == "postgresql":
        return "public"
    if db_type == "oracle":
        # The login's own schema. Naming it explicitly keeps the response honest about where the
        # table landed, which a bare CREATE TABLE would leave the reader to infer.
        return str(resolved.get("username") or "").upper()
    return str(resolved.get("database_name") or "")


def create_table_from_xlsx(request: Any) -> dict[str, Any]:
    """Create a table shaped like the workbook's first sheet and load its rows.

    Returns the ``data`` half of a `common` CLI response — see the module docstring for the
    request. ``table_name`` is echoed back always, because when the caller left it blank the
    generated name is the only way to find the table again.
    """
    started = time.time()
    parsed = _parse_request(request)

    sheet = read_source(parsed["payload"], max_rows=parsed["max_rows"],
                        delimiter=parsed["delimiter"])
    columns: list[str] = sheet["columns"]
    rows: list[list[str]] = sheet["rows"]
    if not columns:
        raise TableLoadError("The file has no columns to build a table from.")

    try:
        resolved = sql_run.resolve_sqlserver_target(
            parsed["target"],
            data_dir=parsed["data_dir"] or None,
            database=parsed["database"],
            credential_name=parsed["credential_name"],
            sql_access=parsed["sql_access"],
        )
    except sql_run.SqlRunError as exc:
        raise TableLoadError(str(exc)) from exc

    db_type = str(resolved["db_type"])
    if db_type not in _TEXT_TYPE:
        raise TableLoadError(
            f"create-table-from-xlsx does not know engine {db_type!r}; supported: "
            f"{', '.join(sorted(_TEXT_TYPE))}."
        )
    # The legacy Oracle bridge runs one statement at a time over HTTP with no binds and no
    # transaction. Loading a table through it would be one round trip per row with the values
    # inlined into the SQL text — say so rather than half-work.
    from db_ops.common import oracle_bridge
    if oracle_bridge.is_legacy(resolved.get("sql_access")):
        raise TableLoadError(
            f"{resolved['server_id']} reaches its SQL through the legacy Oracle bridge, which "
            "takes one statement at a time and binds no parameters. Loading a spreadsheet that "
            "way is not supported."
        )

    schema = parsed["schema"] or _default_schema(db_type, resolved)
    table = parsed["table_name"] or xlsx_import.generate_table_name()
    if parsed["load_rows"]:
        _check_lengths(columns, rows, parsed["text_length"])

    qualified = _qualified_name(db_type, schema, table)
    create_sql = build_create_table(db_type, schema=schema, table=table, columns=columns,
                                    text_length=parsed["text_length"])

    timeout = parsed["timeout_seconds"] or sql_run.DEFAULT_TIMEOUT_SECONDS
    try:
        conn = sql_run.connect_target(resolved, timeout_seconds=int(timeout))
    except sql_run.SqlRunError as exc:
        raise TableLoadError(str(exc)) from exc

    # Only now: on SQL Server the placeholder depends on which driver actually opened, and that
    # is not known until it has. Deciding before connecting built `?` on a pymssql connection.
    style = placeholder_style(db_type, conn)

    created = False
    dropped = False
    inserted = 0
    committed = False
    try:
        cursor = conn.cursor()
        exists = _table_exists(cursor, db_type, schema, table, style)
        if exists and parsed["if_exists"] == "error":
            raise TableLoadError(
                f"{qualified} already exists on {resolved['server_id']}. Pass "
                '"if_exists": "drop" to replace it or "append" to add to it, or name a '
                "different table."
            )
        if exists and parsed["if_exists"] == "drop":
            cursor.execute(f"DROP TABLE {qualified}")
            dropped = True
            exists = False
        if not exists:
            cursor.execute(create_sql)
            created = True
        elif parsed["if_exists"] == "append":
            _assert_columns_match(cursor, db_type, schema, table, columns, style)

        if parsed["load_rows"] and rows:
            insert_sql = (
                f"INSERT INTO {qualified} "
                f"({', '.join(quote_identifier(db_type, name) for name in columns)}) "
                f"VALUES ({build_placeholders(style, len(columns))})"
            )
            for start in range(0, len(rows), BATCH_SIZE):
                batch = [tuple(row) for row in rows[start:start + BATCH_SIZE]]
                cursor.executemany(insert_sql, batch)
                inserted += len(batch)
        conn.commit()
        committed = True
    except TableLoadError:
        _rollback(conn, committed)
        raise
    except Exception as exc:  # noqa: BLE001 - a driver error is a message for the operator.
        _rollback(conn, committed)
        raise TableLoadError(f"{qualified}: {exc}") from exc
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 - closing a broken connection is not the failure.
            pass

    return {
        "server_id": resolved["server_id"],
        "db_type": db_type,
        "database": resolved.get("database_name", ""),
        "schema": schema,
        "table_name": table,
        "qualified_name": qualified,
        "credential_name": resolved.get("credential_name", ""),
        "username": resolved.get("username", ""),
        "columns": columns,
        "column_count": len(columns),
        "column_type": _TEXT_TYPE[db_type](parsed["text_length"]),
        "created": created,
        "dropped_existing": dropped,
        "rows_inserted": inserted,
        "rows_in_sheet": sheet["row_count"],
        "sheet_truncated": sheet["truncated"],
        # What the file turned out to be, echoed back: when a text file is read with the wrong
        # delimiter the columns look plausible and only this says why.
        "source_format": sheet.get("format", ""),
        "source_delimiter": sheet.get("delimiter", ""),
        "source_encoding": sheet.get("encoding", ""),
        "ddl_transactional": _DDL_IS_TRANSACTIONAL.get(db_type, False),
        "duration_ms": int((time.time() - started) * 1000),
    }


def _rollback(conn: Any, committed: bool) -> None:
    """Undo what can be undone. On Oracle and MySQL the CREATE has already committed itself."""
    if committed:
        return
    try:
        conn.rollback()
    except Exception:  # noqa: BLE001 - some drivers roll back on close anyway.
        pass


def _assert_columns_match(cursor: Any, db_type: str, schema: str, table: str,
                          columns: list[str], style: str) -> None:
    """For ``append``: the sheet's columns must be the table's, or the INSERT is nonsense.

    Checked by name rather than by position: a spreadsheet with the same columns in a different
    order is the normal case, and inserting it positionally would put ship dates in the quantity
    column without any error at all.
    """
    first, second = two_placeholders(style)
    if db_type == "oracle":
        sql = (f"SELECT column_name FROM all_tab_columns "
               f"WHERE table_name = {first} AND owner = {second}")
        params: tuple[Any, ...] = (table.upper(), (schema or "").upper())
    else:
        sql = (f"SELECT column_name FROM information_schema.columns "
               f"WHERE table_name = {first} AND table_schema = {second}")
        params = (table, schema)
    cursor.execute(sql, params)
    existing = {str(row[0]).strip().lower() for row in cursor.fetchall()}
    wanted = {name.strip().lower() for name in columns}
    missing = sorted(wanted - existing)
    if missing:
        raise TableLoadError(
            f"Cannot append: {', '.join(repr(name) for name in missing)} "
            f"{'is' if len(missing) == 1 else 'are'} in the spreadsheet but not in the table."
        )
