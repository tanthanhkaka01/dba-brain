"""Render one result set the way the caller needs to read it.

``run-sql`` and every other JSON-request command answered in exactly one shape: a JSON object on
stdout. That is right for a program and wrong for a person — an operator holding a runbook wants a
table they can read, a ticket wants a file they can attach, and a shell pipeline wants the values
with nothing around them. Each of those was being produced by piping the JSON into something else,
which meant the formatting lived in whatever shell history happened to survive.

``xlsx`` already existed here as :mod:`db_ops.lib.xlsx_export`, reached only from the
``sql_tasks`` app through its ``output`` config. Putting the other renderings beside it, behind one
selector, is what makes "how do I get this as a file" have a single answer instead of one per app.

The formats and what each is for:

``json``  the default, and the only one that survives being parsed by a program.
``txt``   an aligned table with a header — for a human reading a terminal.
``csv``   for a spreadsheet or another tool, without writing a file.
``xml``   for a consumer that wants structure without a JSON parser; column names become
          attributes rather than tags, because a column may be named ``1`` or ``order by`` and
          neither is a legal tag name.
``xlsx``  writes a workbook and returns where it went; the only format that produces a file.
``raw``   values only, tab-separated, no header and no wrapper — for piping into ``cut``/``awk``.

``NULL`` is deliberately distinguishable in every text format: an empty cell and a SQL NULL mean
different things, and a report that renders both as nothing has thrown away the difference. In
``csv`` that distinction follows PostgreSQL's ``COPY ... WITH CSV`` convention rather than the
literal ``NULL`` the other text formats print — an empty **unquoted** field is NULL, an empty
**quoted** field (``""``) is the empty string. Writing the word NULL there would make it
indistinguishable from a four-character string that happens to say NULL, which in a file destined
for a spreadsheet is the more likely reading.
"""

from __future__ import annotations

import datetime
import decimal
import json
from pathlib import Path
from typing import Any
from db_ops.lib.paths import TOOL_ROOT  # noqa: F401 - one definition, see that module


#: Every format the CLI accepts. ``json`` first because it is the default.
RESULT_FORMATS = ("json", "txt", "csv", "xml", "xlsx", "raw")

#: What a SQL NULL renders as in the text formats. Chosen over "" so it cannot be confused with
#: an empty string, and over "None" so it does not look like a Python object leaked through.
NULL_TEXT = "NULL"

__all__ = [
    "NULL_TEXT",
    "RESULT_FORMATS",
    "ResultFormatError",
    "normalize_format",
    "render_result",
    "write_result",
]


class ResultFormatError(ValueError):
    """An unusable format name, or a format that needs an output path and was not given one."""


def normalize_format(raw: Any) -> str:
    text = str(raw or "").strip().lower()
    if not text:
        return "json"
    if text in {"text", "plain", "table"}:  # what an operator types when they mean txt
        return "txt"
    if text not in RESULT_FORMATS:
        raise ResultFormatError(
            f"format must be one of {', '.join(RESULT_FORMATS)}; got {raw!r}."
        )
    return text


def _cell_text(value: Any) -> str:
    if value is None:
        return NULL_TEXT
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return format(value, "f")
    if isinstance(value, (bytes, bytearray)):
        # Bytes have no faithful text rendering; say how many rather than mangle them into
        # replacement characters that look like data.
        return f"<{len(value)} bytes>"
    return str(value)


def _columns_and_rows(result: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    columns = [str(name) for name in (result.get("columns") or [])]
    rows = [list(row) for row in (result.get("rows") or [])]
    return columns, rows


def render_result(
    result: dict[str, Any],
    *,
    fmt: str = "json",
    output_path: str | Path | None = None,
    sheet_name: str = "Result",
) -> tuple[str, dict[str, Any]]:
    """Return ``(text_for_stdout, extra)`` for ``result`` in ``fmt``.

    ``extra`` carries what the caller should know that the rendering itself cannot say — where an
    ``xlsx`` landed, how many cells Excel forced us to clamp. It is empty for the text formats.
    """
    fmt = normalize_format(fmt)
    columns, rows = _columns_and_rows(result)

    if fmt == "json":
        return json.dumps(result, ensure_ascii=False, default=str, indent=1), {}
    if fmt == "txt":
        return _render_txt(result, columns, rows), {}
    if fmt == "csv":
        return _render_csv(columns, rows), {}
    if fmt == "xml":
        return _render_xml(result, columns, rows), {}
    if fmt == "raw":
        return _render_raw(columns, rows), {}
    return _render_xlsx(result, columns, rows, output_path=output_path, sheet_name=sheet_name)


def write_result(
    result: dict[str, Any],
    *,
    fmt: str,
    path: str | Path,
    sheet_name: str = "Result",
) -> dict[str, Any]:
    """Write ``result`` to ``path`` in ``fmt`` and return what was written.

    The single entry point for "put this result set in a file", whatever the format. Callers that
    export get one call and no branch — before this, a caller had to know that ``xlsx`` writes
    itself while the text formats hand back a string, and every exporter grew the same
    ``if fmt == "xlsx"`` in a slightly different place.

    Returns ``{path, format, bytes, row_count}`` plus ``truncated_cells`` for xlsx.
    """
    fmt = normalize_format(fmt)
    if fmt == "json":
        # Allowed, and occasionally what an operator wants attached to a ticket — but it is the
        # one format render_result does not treat as a *file*, so say so rather than guess.
        text, _ = render_result(result, fmt="json")
        return _write_text(result, text, fmt=fmt, path=path)

    destination = Path(path)
    if not destination.is_absolute():
        destination = TOOL_ROOT / destination
    destination.parent.mkdir(parents=True, exist_ok=True)

    if fmt == "xlsx":
        _text, extra = render_result(
            result, fmt="xlsx", output_path=destination, sheet_name=sheet_name,
        )
        return {"path": str(destination), "format": fmt, **extra}

    text, _ = render_result(result, fmt=fmt)
    return _write_text(result, text, fmt=fmt, path=destination)


def _write_text(result: dict[str, Any], text: str, *, fmt: str, path: str | Path) -> dict[str, Any]:
    destination = Path(path)
    if not destination.is_absolute():
        destination = TOOL_ROOT / destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    # newline="" so the csv writer's own "\n" is not translated to "\r\n" on Windows — a file
    # written on the master and read on Linux would otherwise show a blank line per row.
    destination.write_text(text + "\n", encoding="utf-8", newline="")
    return {
        "path": str(destination),
        "format": fmt,
        "bytes": destination.stat().st_size,
        "row_count": len(result.get("rows") or []),
    }


def _render_txt(result: dict[str, Any], columns: list[str], rows: list[list[Any]]) -> str:
    if not columns:
        # A statement with no result set (an UPDATE, a DDL) still has something to report, and
        # printing nothing at all reads as "it did not run".
        return _no_result_set_line(result)
    text_rows = [[_cell_text(value) for value in row] for row in rows]
    widths = [len(name) for name in columns]
    for row in text_rows:
        for index, cell in enumerate(row):
            if index < len(widths):
                widths[index] = max(widths[index], len(cell))
    lines = [
        "  ".join(name.ljust(widths[index]) for index, name in enumerate(columns)),
        "  ".join("-" * width for width in widths),
    ]
    lines.extend(
        "  ".join(cell.ljust(widths[index]) if index < len(widths) else cell
                  for index, cell in enumerate(row))
        for row in text_rows
    )
    lines.append("")
    lines.append(_row_count_line(result, len(rows)))
    return "\n".join(lines)


def _render_csv(columns: list[str], rows: list[list[Any]]) -> str:
    """RFC 4180 through the stdlib writer, never hand-rolled quoting.

    ``QUOTE_STRINGS`` is what keeps NULL and the empty string apart: it quotes strings and leaves
    ``None`` as a bare empty field, which is exactly PostgreSQL's ``COPY ... WITH CSV`` output and
    what a spreadsheet reads back correctly. It also leaves numbers unquoted, so Excel treats them
    as numbers instead of text — ``QUOTE_ALL`` would have been unambiguous and unusable.
    """
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer, quoting=csv.QUOTE_STRINGS, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_csv_value(value) for value in row])
    return buffer.getvalue().rstrip("\n")


def _csv_value(value: Any) -> Any:
    """Numbers stay numbers so they land in the spreadsheet as numbers; everything else becomes
    the same text the other formats print, and ``None`` stays ``None`` so the writer can mark it."""
    if value is None or isinstance(value, (int, float, decimal.Decimal)) and not isinstance(value, bool):
        return value
    return _cell_text(value)


def _render_xml(result: dict[str, Any], columns: list[str], rows: list[list[Any]]) -> str:
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', "<result>"]
    for key in ("ok", "server_id", "database", "row_count", "truncated"):
        if key in result:
            parts.append(f"  <{key}>{_escape(_cell_text(result[key]))}</{key}>")
    parts.append("  <rows>")
    for row in rows:
        parts.append("    <row>")
        for index, value in enumerate(row):
            name = columns[index] if index < len(columns) else f"column{index + 1}"
            # The column name is an attribute, never a tag: SQL happily returns a column called
            # "1" or "count(*)", and neither can be an XML element name.
            null = ' null="true"' if value is None else ""
            body = "" if value is None else _escape(_cell_text(value))
            parts.append(f'      <value name="{_escape(name)}"{null}>{body}</value>')
        parts.append("    </row>")
    parts.append("  </rows>")
    parts.append("</result>")
    return "\n".join(parts)


def _render_raw(columns: list[str], rows: list[list[Any]]) -> str:
    # No header, no counts, no trailing summary: raw exists so `| cut -f2` works, and anything
    # extra on stdout is something the pipeline has to be taught to skip.
    return "\n".join("\t".join(_cell_text(value) for value in row) for row in rows)


def _render_xlsx(
    result: dict[str, Any], columns: list[str], rows: list[list[Any]], *,
    output_path: str | Path | None, sheet_name: str,
) -> tuple[str, dict[str, Any]]:
    if not output_path:
        raise ResultFormatError('format "xlsx" writes a file, so "output_path" is required.')
    from db_ops.lib import xlsx_export

    path = Path(output_path)
    if not path.is_absolute():
        # Same rule as file_transfer: relative resolves against the tool root, never the
        # process's working directory, which the daemon does not let a caller predict.
        path = TOOL_ROOT / path
    written = xlsx_export.write_result_set_xlsx(
        path, columns=columns, rows=rows, sheet_name=sheet_name,
    )
    extra = {
        "output_path": str(written.path),
        "bytes": written.path.stat().st_size,
        "row_count": len(rows),
        "truncated_cells": written.truncated_cells,
    }
    payload = {"ok": bool(result.get("ok", True)), "format": "xlsx", **extra}
    if written.truncated_cells:
        # Excel discards an over-long cell and then reports the whole file as damaged, so the
        # clamp is not cosmetic and the caller has to be told it happened.
        payload["note"] = (
            f"{written.truncated_cells} cell(s) exceeded Excel's per-cell limit and were clamped."
        )
    return json.dumps(payload, ensure_ascii=False, indent=1), extra


def _row_count_line(result: dict[str, Any], rendered: int) -> str:
    count = result.get("row_count", rendered)
    line = f"({count} row{'' if count == 1 else 's'})"
    if result.get("truncated"):
        line += " — truncated at the row cap; not the whole result set."
    return line


def _no_result_set_line(result: dict[str, Any]) -> str:
    affected = result.get("affected_rows")
    if affected is not None:
        return f"(no result set; {affected} row(s) affected)"
    return "(no result set)"


def _escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
