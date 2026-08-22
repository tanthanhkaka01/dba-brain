"""Minimal, dependency-free XLSX writer for a single result-set sheet.

Two callers turn a SELECT result set into a workbook: the Telegram ``spbot_sql_to_xlsx``
command (ad-hoc) and a SQL task whose target sets ``output.format = "xlsx"`` (scheduled). It
lives in ``common`` so neither app imports the other — see the "No cross-app imports" rule in
``docs/13_common.md``.

Rather than pull in openpyxl (a new image dependency that would need a rebuild/redeploy before
the command could run), this writes the handful of XML parts an XLSX actually needs, zipped,
using only the standard library. One worksheet, a header row, then the data rows. Numbers are
written as numeric cells; everything else as inline strings, so Excel/LibreOffice show text as
text and numbers as numbers.
"""

from __future__ import annotations

import datetime as _datetime
import zipfile
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any


# Excel's hard limit on the text of one cell. A longer string does not fail to write — Excel
# opens the workbook, declares it damaged ("Repaired Records: String properties from
# /xl/worksheets/sheet1.xml part") and DROPS the string. Query Store exports hit this
# routinely: one `query_plan` was 1.6 M characters. So the writer clamps instead, and says so
# both in the cell and in the count it returns.
MAX_CELL_TEXT = 32_767
_TRUNCATION_MARKER = "…[truncated]"


_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/worksheets/sheet1.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
    "</Types>"
)

_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="xl/workbook.xml"/>'
    "</Relationships>"
)

_WORKBOOK_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
    'Target="worksheets/sheet1.xml"/>'
    "</Relationships>"
)


@dataclass
class XlsxWriteResult:
    """Where the workbook went, and what had to be cut to make Excel accept it."""

    path: Path
    truncated_cells: int = 0


def write_result_set_xlsx(
    path: str | Path,
    *,
    columns: list[Any],
    rows: list[list[Any]],
    sheet_name: str = "Result",
) -> XlsxWriteResult:
    """Write ``columns`` (header) + ``rows`` to an .xlsx workbook at ``path``.

    Cells that are ``int``/``float``/``Decimal`` become numeric cells; ``None`` becomes an
    empty cell; datetimes and everything else become inline text (ISO 8601 for datetimes).
    Text longer than :data:`MAX_CELL_TEXT` is clamped — Excel would otherwise discard it and
    report the file as damaged — and counted in the returned
    :class:`XlsxWriteResult.truncated_cells` so the caller can warn.
    """
    out_path = Path(path)
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets><sheet name="{_escape_sheet_name(sheet_name)}" sheetId="1" r:id="rId1"/></sheets>'
        "</workbook>"
    )
    sheet_xml, truncated_cells = _build_sheet_xml(columns=columns, rows=rows)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("_rels/.rels", _ROOT_RELS)
        archive.writestr("xl/workbook.xml", workbook_xml)
        archive.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS)
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return XlsxWriteResult(path=out_path, truncated_cells=truncated_cells)


def _build_sheet_xml(*, columns: list[Any], rows: list[list[Any]]) -> tuple[str, int]:
    parts: list[str] = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
        "<sheetData>",
    ]
    truncated = 0
    header_cells = []
    for col_index, value in enumerate(columns):
        cell, was_truncated = _string_cell(_cell_ref(col_index, 1), value)
        truncated += int(was_truncated)
        header_cells.append(cell)
    parts.append(f'<row r="1">{"".join(header_cells)}</row>')
    for row_offset, row in enumerate(rows):
        row_number = row_offset + 2  # header is row 1
        cells = []
        for col_index, value in enumerate(row):
            cell, was_truncated = _cell_xml(_cell_ref(col_index, row_number), value)
            truncated += int(was_truncated)
            cells.append(cell)
        parts.append(f'<row r="{row_number}">{"".join(cells)}</row>')
    parts.append("</sheetData></worksheet>")
    return "".join(parts), truncated


def _cell_xml(ref: str, value: Any) -> tuple[str, bool]:
    """The cell's XML, and whether its text had to be clamped to Excel's limit."""
    if value is None or value == "":
        return f'<c r="{ref}"/>', False
    if isinstance(value, bool):
        # bool is an int subclass; keep it readable as text rather than 0/1.
        return _string_cell(ref, "TRUE" if value else "FALSE")
    if isinstance(value, int):
        return f'<c r="{ref}"><v>{value}</v></c>', False
    if isinstance(value, float):
        return f'<c r="{ref}"><v>{_number_text(value)}</v></c>', False
    if isinstance(value, Decimal):
        return f'<c r="{ref}"><v>{_number_text(value)}</v></c>', False
    if isinstance(value, (_datetime.datetime, _datetime.date, _datetime.time)):
        return _string_cell(ref, value.isoformat())
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _string_cell(ref, bytes(value).hex())
    return _string_cell(ref, str(value))


def _string_cell(ref: str, text: Any) -> tuple[str, bool]:
    clamped, truncated = _clamp_cell_text(_sanitize_text(str(text)))
    escaped = _escape_xml(clamped)
    return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{escaped}</t></is></c>', truncated


def _clamp_cell_text(text: str) -> tuple[str, bool]:
    """Cut text to what Excel accepts in one cell, marking it so the loss is visible.

    A `query_plan` or `query_sql_text` column blows past the limit easily; clamping keeps the
    workbook openable and the rest of the row intact. Anyone who needs the full value should
    read it outside a spreadsheet — a workbook cannot hold it.
    """
    if len(text) <= MAX_CELL_TEXT:
        return text, False
    return text[: MAX_CELL_TEXT - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER, True


def _number_text(value: Any) -> str:
    text = str(value)
    # NaN / Infinity are not valid XLSX numbers — fall back to text-safe zero-ish repr.
    if text in ("nan", "inf", "-inf"):
        return "0"
    return text


def _cell_ref(col_index: int, row_number: int) -> str:
    return f"{_column_letter(col_index)}{row_number}"


def _column_letter(col_index: int) -> str:
    """0-based column index -> Excel column letters (0 -> A, 25 -> Z, 26 -> AA)."""
    letters = ""
    index = col_index
    while True:
        index, remainder = divmod(index, 26)
        letters = chr(ord("A") + remainder) + letters
        if index == 0:
            break
        index -= 1
    return letters


def _escape_xml(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _sanitize_text(text: str) -> str:
    """Drop characters XML forbids: control codes (keeping tab/newline/CR), lone surrogates
    and the non-characters U+FFFE/U+FFFF. Any of them makes Excel report a damaged file."""
    return "".join(char for char in text if _is_xml_char(char))


def _is_xml_char(char: str) -> bool:
    code = ord(char)
    if code < 0x20:
        return char in ("\t", "\n", "\r")
    if 0xD800 <= code <= 0xDFFF:  # surrogate halves are not valid XML characters
        return False
    return code not in (0xFFFE, 0xFFFF)


def _escape_sheet_name(name: str) -> str:
    # Excel forbids : \ / ? * [ ] in sheet names and caps length at 31.
    cleaned = "".join(" " if char in ':\\/?*[]' else char for char in str(name)).strip()
    cleaned = cleaned[:31] or "Result"
    return _escape_xml(cleaned)
