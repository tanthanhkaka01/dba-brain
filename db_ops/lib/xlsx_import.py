"""Read one sheet out of an XLSX, and build a table from it on any engine db_ops knows.

The mirror of :mod:`db_ops.lib.xlsx_export`, and written the same way and for the same
reason: **standard library only**. openpyxl would be a new image dependency, so every operator
who wanted to load a spreadsheet would first need a rebuild and a redeploy. An XLSX is a zip of
XML parts; reading the handful that matter is less code than the dependency is trouble.

What a caller gets is deliberately narrow — *one* sheet, a header row, then data rows, every
value as text:

    columns, rows = read_sheet(xlsx_bytes)

Everything lands as ``NVARCHAR(4000)`` (or the engine's equivalent) because the source is a
spreadsheet: a column of order numbers is text in one row and a number in the next, and Excel
itself does not know which the author meant. Guessing a type per column and being wrong once
costs more than one CAST at read time. The caller who knows better can ALTER afterwards.

Dates are the one place this module *does* interpret. Excel stores a date as a number plus a
style, so a date column read literally arrives as ``45678`` — not wrong, but useless, and the
most common column in an operational spreadsheet. So ``xl/styles.xml`` is parsed far enough to
know which cells are date-formatted, and those are rendered ISO. Nothing else is converted.
"""

from __future__ import annotations

import base64
import datetime as _datetime
import io
import re
import secrets as _secrets
import zipfile
from typing import Any, Iterator
from xml.etree import ElementTree as ET


class XlsxImportError(RuntimeError):
    """A user-facing failure: not a workbook, no sheet, an unreadable part, an empty header."""


# The main SpreadsheetML namespace. Every part uses it, and every reader here strips it rather
# than matching on the full name — a workbook written by LibreOffice or by Google Sheets carries
# the same local names under prefixes that are not worth enumerating.
_RELS_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

#: Excel's built-in number formats that mean "this is a date/time". Anything else numeric is a
#: number. 14-22 are the date and time formats; 45-47 are the elapsed-time ones.
_BUILTIN_DATE_FORMATS = frozenset(range(14, 23)) | {45, 46, 47}

#: A custom format code (numFmtId >= 164) is a date when it contains a date/time token outside
#: quoted literal text. `[Red]`-style colour blocks and quoted strings are stripped first so a
#: format like `"Delivered"0.00` is not read as a date because of the `d` in the word.
_DATE_TOKEN = re.compile(r"[ymdhs]", re.IGNORECASE)
_FORMAT_NOISE = re.compile(r'"[^"]*"|\[[^\]]*\]|\\.')

#: Excel's epoch. Serial 1 is 1900-01-01, but Excel also believes 1900 was a leap year, so every
#: serial from 61 up is one day ahead of reality against a 1899-12-31 base. Anchoring at
#: 1899-12-30 makes serial >= 61 correct, which is every date anyone actually stores.
_EXCEL_EPOCH = _datetime.datetime(1899, 12, 30)

#: What one cell may carry into a column typed NVARCHAR(4000). Longer is refused rather than
#: silently clipped: a truncated value that looks complete is worse than a rejected load.
MAX_TEXT_LENGTH = 4000

#: Upper bound on rows read from one file, so a runaway upload cannot exhaust memory on the
#: worker. The caller is told when it bites rather than getting a short table it believes is whole,
#: and any caller who needs more passes ``max_rows``. Raised from 100k on 2026-08-15: the cap was
#: sized for a Telegram attachment when Telegram attachments were capped at 1 MB, and a normal
#: operational extract — a year of leave records, a stock file — goes past 100k rows.
DEFAULT_MAX_ROWS = 1_000_000


def _local(tag: str) -> str:
    """The local name of a namespaced XML tag."""
    return tag.rsplit("}", 1)[-1]


def decode_payload(data: Any, *, field: str = "xlsx_base64") -> bytes:
    """Accept the workbook as base64 text (what a JSON request can carry) or as raw bytes."""
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    text = str(data or "").strip()
    if not text:
        raise XlsxImportError(f"{field} is empty.")
    # A data: URI or a stray newline-wrapped paste is the normal way this arrives from a form.
    if text.startswith("data:"):
        text = text.split(",", 1)[-1]
    text = "".join(text.split())
    try:
        return base64.b64decode(text, validate=True)
    except Exception as exc:  # noqa: BLE001 - a bad paste is a message, not a trace.
        raise XlsxImportError(f"{field} is not valid base64: {exc}") from exc


def column_index(reference: str) -> int:
    """``A1`` -> 0, ``B2`` -> 1, ``AA7`` -> 26. Zero-based, ignoring the row part.

    Cells are placed by this rather than by arrival order, because Excel omits empty cells
    entirely: a row whose second column is blank writes A and C, and reading positionally
    shifts every later value one column left for that row only.
    """
    index = 0
    for char in reference:
        if not char.isalpha():
            break
        index = index * 26 + (ord(char.upper()) - ord("A") + 1)
    return index - 1 if index else 0


def _date_style_ids(archive: zipfile.ZipFile) -> set[int]:
    """Which style indexes format their cell as a date.

    Two hops: ``cellXfs`` maps a cell's ``s`` attribute to a ``numFmtId``, and the id is either
    one of Excel's built-ins or defined in ``numFmts``. A workbook with no styles part (rare, but
    a machine-generated one can omit it) simply has no date columns.
    """
    try:
        raw = archive.read("xl/styles.xml")
    except KeyError:
        return set()
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return set()

    custom: dict[int, str] = {}
    for node in root.iter():
        if _local(node.tag) == "numFmt":
            try:
                custom[int(node.get("numFmtId", "-1"))] = node.get("formatCode", "")
            except ValueError:
                continue

    def is_date_format(fmt_id: int) -> bool:
        if fmt_id in _BUILTIN_DATE_FORMATS:
            return True
        code = custom.get(fmt_id)
        if not code:
            return False
        return bool(_DATE_TOKEN.search(_FORMAT_NOISE.sub("", code)))

    date_styles: set[int] = set()
    for container in root.iter():
        if _local(container.tag) != "cellXfs":
            continue
        for position, xf in enumerate(child for child in container if _local(child.tag) == "xf"):
            try:
                if is_date_format(int(xf.get("numFmtId", "0"))):
                    date_styles.add(position)
            except ValueError:
                continue
    return date_styles


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    """The workbook's string pool. Most text cells are a pointer into this rather than a value."""
    try:
        raw = archive.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise XlsxImportError(f"sharedStrings.xml is not readable: {exc}") from exc
    pool: list[str] = []
    for si in root:
        if _local(si.tag) != "si":
            continue
        # A rich-text cell splits its text across <r><t> runs; concatenating them is what Excel
        # displays. Ignoring the runs would read a formatted header as empty.
        pool.append("".join(node.text or "" for node in si.iter() if _local(node.tag) == "t"))
    return pool


def _first_sheet_path(archive: zipfile.ZipFile) -> str:
    """The part path of the first sheet in the workbook's own order.

    Zip order is not sheet order, and ``sheet1.xml`` is not reliably the first tab — a workbook
    whose sheets were reordered keeps its original file names. So the answer comes from
    ``workbook.xml`` plus the relationship table, exactly as Excel resolves it.
    """
    try:
        book = ET.fromstring(archive.read("xl/workbook.xml"))
    except KeyError as exc:
        raise XlsxImportError("Not an XLSX workbook: xl/workbook.xml is missing.") from exc
    except ET.ParseError as exc:
        raise XlsxImportError(f"xl/workbook.xml is not readable: {exc}") from exc

    relationship_id = ""
    for node in book.iter():
        if _local(node.tag) == "sheet":
            relationship_id = node.get(f"{{{_RELS_NS}}}id", "")
            break
    if not relationship_id:
        raise XlsxImportError("The workbook declares no sheets.")

    try:
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    except (KeyError, ET.ParseError) as exc:
        raise XlsxImportError(f"The workbook's relationship table is unreadable: {exc}") from exc
    for node in rels:
        if node.get("Id") == relationship_id:
            target = node.get("Target", "")
            return target[1:] if target.startswith("/") else f"xl/{target.lstrip('./')}"
    raise XlsxImportError(f"The workbook names sheet {relationship_id} but no part matches it.")


def _cell_text(value: str, cell_type: str, style: str, pool: list[str],
               date_styles: set[int]) -> str:
    """One cell as the text that will be stored."""
    if cell_type == "s":
        try:
            return pool[int(value)]
        except (ValueError, IndexError):
            return ""
    if cell_type == "b":
        return "TRUE" if value == "1" else "FALSE"
    if cell_type in ("str", "inlineStr", "e"):
        return value
    if value == "":
        return ""
    # Numeric. Only a date-styled cell is reinterpreted; see the module docstring.
    try:
        style_index = int(style) if style else 0
    except ValueError:
        style_index = 0
    if style_index in date_styles:
        try:
            moment = _EXCEL_EPOCH + _datetime.timedelta(days=float(value))
        except (ValueError, OverflowError):
            return value
        if moment.time() == _datetime.time(0, 0):
            return moment.date().isoformat()
        return moment.isoformat(sep=" ")
    # An integer-valued float reads as `12` rather than `12.0`: the spreadsheet showed `12`, and
    # a loaded table that says otherwise is a difference the operator has to explain.
    try:
        number = float(value)
    except ValueError:
        return value
    if number.is_integer() and abs(number) < 1e15:
        return str(int(number))
    return value


def _iter_rows(archive: zipfile.ZipFile, sheet_path: str, pool: list[str],
               date_styles: set[int]) -> Iterator[list[str]]:
    """Stream the sheet's rows, each padded to its own last populated column."""
    with archive.open(sheet_path) as stream:
        row: list[str] = []
        for event, node in ET.iterparse(stream, events=("start", "end")):
            name = _local(node.tag)
            if event == "start" and name == "row":
                row = []
            elif event == "end" and name == "c":
                index = column_index(node.get("r", ""))
                cell_type = node.get("t", "")
                text = ""
                for child in node:
                    child_name = _local(child.tag)
                    if child_name == "v":
                        text = child.text or ""
                    elif child_name == "is":
                        text = "".join(t.text or "" for t in child.iter()
                                       if _local(t.tag) == "t")
                        cell_type = "inlineStr"
                while len(row) <= index:
                    row.append("")
                row[index] = _cell_text(text, cell_type, node.get("s", ""), pool, date_styles)
                node.clear()
            elif event == "end" and name == "row":
                yield row
                node.clear()


def header_names(raw: list[str]) -> list[str]:
    """Column names from the header row: never blank, never duplicated.

    A spreadsheet's header row is written for a human. Blank and repeated cells are normal in
    one and illegal as column names, so both are resolved here rather than at CREATE TABLE time
    where the engine's error would name a column the operator cannot find in their file.

    Public because :mod:`db_ops.lib.delimited_import` reads the same header row out of a text
    file: two implementations of "what are this file's column names" would disagree about blanks
    and duplicates, and the operator would get different tables from the same data depending on
    which format they happened to attach.
    """
    names: list[str] = []
    seen: dict[str, int] = {}
    for position, cell in enumerate(raw, start=1):
        name = " ".join(str(cell or "").split()).strip()
        if not name:
            name = f"column_{position}"
        if len(name) > 128:
            name = name[:128]
        key = name.lower()
        if key in seen:
            seen[key] += 1
            name = f"{name}_{seen[key]}"
        else:
            seen[key] = 1
        names.append(name)
    return names


def read_sheet(payload: Any, *, max_rows: int = DEFAULT_MAX_ROWS) -> dict[str, Any]:
    """Read the first sheet of a workbook.

    ``payload`` is raw bytes or base64 text. Returns::

        {"columns": ["Order No", "Ship Date", ...],
         "rows": [["AA2608/01902", "2026-08-12", ...], ...],
         "row_count": 1234, "truncated": false, "sheet": "xl/worksheets/sheet1.xml",
         "format": "xlsx"}

    Every value is a string; see the module docstring for why.
    """
    data = decode_payload(payload)
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise XlsxImportError(
            "Not an XLSX file (it is not a zip). A .xls saved by an old Excel is a different "
            "format entirely — re-save it as .xlsx."
        ) from exc

    with archive:
        sheet_path = _first_sheet_path(archive)
        pool = _shared_strings(archive)
        date_styles = _date_style_ids(archive)

        header: list[str] | None = None
        rows: list[list[str]] = []
        truncated = False
        for row in _iter_rows(archive, sheet_path, pool, date_styles):
            if header is None:
                # Leading blank rows are common in exported reports; the header is the first row
                # that says anything at all.
                if not any(cell.strip() for cell in row):
                    continue
                header = header_names(row)
                continue
            if len(rows) >= max_rows:
                truncated = True
                break
            while len(row) < len(header):
                row.append("")
            rows.append(row[:len(header)])

    if header is None:
        raise XlsxImportError("The sheet is empty: no header row to take column names from.")
    return {
        "columns": header,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "sheet": sheet_path,
        # The last three exist so a caller that took either this or `delimited_import.read_table`
        # can describe what it read without first knowing which one answered.
        "format": "xlsx",
        "delimiter": "",
        "encoding": "",
    }


def generate_table_name(prefix: str = "temp") -> str:
    """``temp_3f9a1c4e`` — the default when the caller names no table.

    Random rather than sequential or timestamped: two uploads in the same second must not
    collide, and nothing here can see the target's existing tables before it connects.
    """
    return f"{prefix}_{_secrets.token_hex(4)}"
