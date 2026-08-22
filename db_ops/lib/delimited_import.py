"""Read a delimited text file — tab, comma, semicolon or pipe — as a header row plus data rows.

The sibling of :mod:`db_ops.lib.xlsx_import`, with the same output shape and the same promise
(every value is text), for the file people actually have. What arrives in practice is rarely a
clean .xlsx: it is a block selected in Excel and pasted into Notepad, an export from an old
reporting tool, a `.txt` somebody renamed. Those are tab-delimited text, and before this module
they were refused with *"Not an XLSX file (it is not a zip)"* — technically true and useless,
because the file on the operator's screen looks exactly like a spreadsheet.

Three things about such a file are guessed rather than declared, and each has a wrong guess that
corrupts silently rather than failing:

* **The encoding.** Excel's "Unicode Text (*.txt)" is UTF-16LE with a BOM; its "Text (Tab
  delimited)" is the Windows ANSI codepage; anything from a Unix tool is UTF-8. Decoding UTF-16
  as UTF-8 does not raise — it produces NUL-riddled column names.
* **The delimiter.** Counted on the header line rather than assumed, because a comma is data in
  half the files that use tabs (`Smith, John`) and a tab is never data in a file that uses commas.
* **Where the header is.** Leading blank lines are normal in an export; the header is the first
  line that says anything.

Quoting is handled by :mod:`csv`, not by ``str.split``: a cell containing the delimiter, or a
newline, is written quoted by every tool that writes these files, and splitting on the character
turns one such row into two wrong ones.
"""

from __future__ import annotations

import csv
import io
from typing import Any

from db_ops.lib.xlsx_import import (
    DEFAULT_MAX_ROWS,
    XlsxImportError,
    decode_payload,
    header_names,
)


class DelimitedImportError(RuntimeError):
    """A user-facing failure: undecodable bytes, an empty file, a row wider than the header."""


#: Candidate delimiters, in the order a tie is broken. Tab first because it is the one that is
#: never data: a file that uses commas has no tabs, but a comma-using file *and* a tab-using file
#: both contain commas.
_CANDIDATES = ("\t", ";", ",", "|")

#: What a caller may name in ``"delimiter"`` instead of the character itself.
_DELIMITER_WORDS = {
    "tab": "\t", "\\t": "\t", "tsv": "\t",
    "comma": ",", "csv": ",",
    "semicolon": ";", "semi": ";",
    "pipe": "|", "bar": "|",
    "space": " ",
}

#: How many bytes to look at when guessing an encoding that declares no BOM. One line is enough
#: to see UTF-16's interleaved NULs and cheap enough to do on a file of any size.
_SNIFF_BYTES = 4096


def resolve_delimiter(value: Any) -> str:
    """The delimiter a caller asked for, as one character. Blank means "guess it"."""
    text = str(value or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    if lowered in _DELIMITER_WORDS:
        return _DELIMITER_WORDS[lowered]
    if len(text) == 1:
        return text
    raise DelimitedImportError(
        f'"delimiter" must be a single character or one of '
        f'{", ".join(sorted(_DELIMITER_WORDS))}; got {text!r}.'
    )


def decode_text(data: bytes) -> tuple[str, str]:
    """Decode the file, returning ``(text, encoding-that-worked)``.

    BOM first, because it is a declaration and not a guess. Then UTF-16 for BOM-less bytes that
    are half NULs — an Excel "Unicode Text" file that lost its BOM in transit still reads as
    garbage under any 8-bit codec, and the garbage becomes column names. Then UTF-8, then the
    Windows codepage, which is what Excel's plain "Text (Tab delimited)" writes and which cannot
    fail: cp1252 maps every byte, so this function always returns rather than leaving the operator
    with an encoding error they have no way to act on.
    """
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig"), "utf-8-sig"
    if data.startswith(b"\xff\xfe"):
        return data.decode("utf-16"), "utf-16-le"
    if data.startswith(b"\xfe\xff"):
        return data.decode("utf-16"), "utf-16-be"

    head = data[:_SNIFF_BYTES]
    if head.count(b"\x00") > len(head) // 4:
        # Interleaved NULs at this density are UTF-16 text, not binary: which half of each pair
        # is the NUL says which byte order.
        even_nulls = head[0::2].count(b"\x00")
        odd_nulls = head[1::2].count(b"\x00")
        encoding = "utf-16-be" if even_nulls > odd_nulls else "utf-16-le"
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            pass
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace"), "cp1252"


def sniff_delimiter(line: str) -> str:
    """Which of tab/semicolon/comma/pipe separates this header line.

    Counted outside quoted text, so a single quoted heading like ``"Qty, total"`` in an otherwise
    tab-delimited file does not win the count for the comma. A line containing none of them is a
    one-column file; tab is returned so the caller still gets a reader.
    """
    counts = {candidate: 0 for candidate in _CANDIDATES}
    in_quotes = False
    for char in line:
        if char == '"':
            in_quotes = not in_quotes
        elif not in_quotes and char in counts:
            counts[char] += 1
    best = max(_CANDIDATES, key=lambda candidate: counts[candidate])
    return best if counts[best] else "\t"


def _first_content_line(text: str) -> str:
    """The first line with anything on it — the one the delimiter is guessed from."""
    for line in text.splitlines():
        if line.strip():
            return line
    return ""


def _trim_trailing_blanks(cells: list[str]) -> list[str]:
    """Drop trailing empty header cells.

    A line copied out of Excel usually ends with the delimiter, which parses as one more empty
    column. Naming it ``column_7`` and creating it would put a permanently NULL column in every
    table built this way. Blanks *between* named columns are kept and named — those are a real
    column whose heading the author left empty.
    """
    end = len(cells)
    while end > 0 and not str(cells[end - 1] or "").strip():
        end -= 1
    return list(cells[:end])


def read_table(payload: Any, *, max_rows: int = DEFAULT_MAX_ROWS,
               delimiter: Any = "") -> dict[str, Any]:
    """Read a delimited text file. Same return shape as :func:`xlsx_import.read_sheet`::

        {"columns": [...], "rows": [[...], ...], "row_count": N, "truncated": bool,
         "sheet": "tab-delimited text (utf-16-le)", "format": "delimited",
         "delimiter": "\\t", "encoding": "utf-16-le"}

    ``payload`` is raw bytes or base64 text, exactly as for a workbook, so a caller that has one
    of the two does not have to know which before it reads.
    """
    try:
        data = decode_payload(payload, field="file_base64")
    except XlsxImportError as exc:  # the shared base64 decoder speaks in its own error type
        raise DelimitedImportError(str(exc)) from exc
    if not data.strip():
        raise DelimitedImportError("The file is empty.")

    text, encoding = decode_text(data)
    separator = resolve_delimiter(delimiter) or sniff_delimiter(_first_content_line(text))

    # `csv` wants a file whose newlines it controls; io.StringIO with newline="" is the documented
    # way to hand it text that already lives in memory without breaking quoted multi-line cells.
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=separator)

    columns: list[str] | None = None
    rows: list[list[str]] = []
    truncated = False
    for line_number, raw_row in enumerate(reader, start=1):
        if not any(str(cell or "").strip() for cell in raw_row):
            # Leading blank lines precede the header in most exports, a trailing newline produces
            # one at the end of every file, and a blank line in a paste is separator noise. None of
            # the three is a row of data.
            continue
        if columns is None:
            columns = header_names(_trim_trailing_blanks(list(raw_row)))
            continue
        if len(rows) >= max_rows:
            truncated = True
            break
        row = [str(cell or "") for cell in raw_row]
        if len(row) > len(columns):
            extra = [cell for cell in row[len(columns):] if cell.strip()]
            if extra:
                # More values than headings means the delimiter guess was wrong, or the header
                # line is short. Padding or clipping here would load the file shifted or short by
                # one column and say nothing — the operator finds out when a join returns nothing.
                raise DelimitedImportError(
                    f"Line {line_number} has {len(row)} values but the header names "
                    f"{len(columns)} columns. The file was read as "
                    f"{describe_delimiter(separator)}; pass \"delimiter\" if that is wrong."
                )
            row = row[:len(columns)]
        while len(row) < len(columns):
            row.append("")
        rows.append(row)

    if columns is None:
        raise DelimitedImportError(
            "The file has no header line to take column names from — every line is blank."
        )
    return {
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "sheet": f"{describe_delimiter(separator)} text ({encoding})",
        "format": "delimited",
        "delimiter": separator,
        "encoding": encoding,
    }


def describe_delimiter(separator: str) -> str:
    """``\\t`` -> ``tab-delimited`` — for a message an operator has to act on."""
    names = {"\t": "tab", ",": "comma", ";": "semicolon", "|": "pipe", " ": "space"}
    return f"{names.get(separator, repr(separator))}-delimited"
