"""Loading the file people actually have: a `.txt` that is a spreadsheet in everything but name.

The command that builds a table from an upload used to take a workbook and nothing else, and it
refused everything else with *"Not an XLSX file (it is not a zip)"* — a true sentence that helps
nobody, because what the operator is looking at is a grid of rows and columns. What they attach is
a block selected in Excel and pasted into Notepad, or an export from a reporting tool that only
writes text.

Such a file declares almost nothing about itself, and each of the three things that must be
guessed has a wrong guess that corrupts quietly rather than failing:

* **The encoding.** Excel's "Unicode Text" is UTF-16 with a BOM. Read as UTF-8 it does not raise
  — it produces column names full of NULs.
* **The delimiter.** A comma is data in half the files that use tabs (`Smith, John`).
* **The shape.** A line copied out of Excel ends with the delimiter, which parses as one extra
  empty column, and a row with *more* values than the header means the delimiter guess was wrong
  — padding it would load every later column shifted by one and say nothing.

The tests below are that list. Quoting is `csv`'s job and is exercised here only where it decides
whether a row is one row or two.
"""

from __future__ import annotations

import base64

import pytest

from db_ops.common import table_load
from db_ops.lib import delimited_import


# --------------------------------------------------------------------------- #
# The ordinary case: a tab-delimited paste
# --------------------------------------------------------------------------- #

def test_a_tab_delimited_paste_reads_as_a_header_row_and_data_rows():
    text = "Emp No\tName\tLeave Date\r\n001\tTrieu Thanh\t2026-08-10\r\n002\tNguyen A\t2026-08-11\r\n"

    table = delimited_import.read_table(text.encode("utf-8"))

    assert table["columns"] == ["Emp No", "Name", "Leave Date"]
    assert table["rows"] == [["001", "Trieu Thanh", "2026-08-10"],
                            ["002", "Nguyen A", "2026-08-11"]]
    assert table["delimiter"] == "\t"
    assert table["row_count"] == 2 and table["truncated"] is False


def test_a_leading_zero_survives_because_every_value_stays_text():
    """The whole reason this path exists rather than an import wizard: `001` is an employee
    number, and a reader that types the column loses it on the first row."""
    table = delimited_import.read_table(b"code\tqty\n007\t0012\n")

    assert table["rows"] == [["007", "0012"]]


def test_a_comma_file_is_read_as_csv_not_as_one_column():
    table = delimited_import.read_table(b"a,b,c\n1,2,3\n")

    assert table["columns"] == ["a", "b", "c"]
    assert table["delimiter"] == ","


def test_a_tab_file_whose_data_contains_commas_is_still_tab_delimited():
    """The guess that matters. `Smith, John` in a name column is normal; a tab in a comma file
    is not, which is why tabs are counted first."""
    table = delimited_import.read_table(b"id\tname\n1\tSmith, John\n")

    assert table["delimiter"] == "\t"
    assert table["rows"] == [["1", "Smith, John"]]


def test_a_quoted_cell_containing_the_delimiter_stays_one_value():
    table = delimited_import.read_table(b'id,note\n1,"a, b, c"\n')

    assert table["rows"] == [["1", "a, b, c"]]


def test_a_semicolon_export_is_recognised():
    """A European Excel writes CSV with semicolons; read with commas it is one column."""
    table = delimited_import.read_table("id;name\n1;Müller\n".encode("utf-8"))

    assert table["delimiter"] == ";" and table["columns"] == ["id", "name"]


# --------------------------------------------------------------------------- #
# Encoding
# --------------------------------------------------------------------------- #

def test_excel_unicode_text_utf16_is_decoded_rather_than_read_as_nul_riddled_ascii():
    """"Unicode Text (*.txt)" is the save format an operator picks when their data has accents.
    It is UTF-16LE with a BOM, and a reader that assumes UTF-8 builds columns named `E\\x00m\\x00`."""
    data = "Mã NV\tTên\n001\tTriệu Thành\n".encode("utf-16")  # writes the BOM

    table = delimited_import.read_table(data)

    assert table["columns"] == ["Mã NV", "Tên"]
    assert table["rows"] == [["001", "Triệu Thành"]]
    assert table["encoding"].startswith("utf-16")


def test_utf16_without_a_bom_is_still_recognised_by_its_nul_bytes():
    """A file that lost its BOM in transit is still UTF-16, and no 8-bit codec fails on it —
    it just returns nonsense."""
    data = "id\tname\n1\tThanh\n".encode("utf-16-le")

    table = delimited_import.read_table(data)

    assert table["columns"] == ["id", "name"]


def test_a_windows_ansi_file_is_read_rather_than_refused():
    """Excel's plain "Text (Tab delimited)" writes the ANSI codepage. Refusing it with a
    UnicodeDecodeError leaves the operator with nothing they can act on, so cp1252 is the last
    resort — it maps every byte."""
    data = "id\tname\n1\tMüller\n".encode("cp1252")

    table = delimited_import.read_table(data)

    assert table["rows"] == [["1", "Müller"]]
    assert table["encoding"] == "cp1252"


def test_a_utf8_bom_is_not_carried_into_the_first_column_name():
    """A BOM left on the front of the header makes a column called `\\ufeffid`, which the
    operator then cannot spell in a WHERE clause."""
    table = delimited_import.read_table("id\tname\n1\tx\n".encode("utf-8-sig"))

    assert table["columns"] == ["id", "name"]


# --------------------------------------------------------------------------- #
# The header row, which was written for a human
# --------------------------------------------------------------------------- #

def test_leading_blank_lines_are_skipped_because_exports_start_with_them():
    table = delimited_import.read_table(b"\n\nid\tname\n1\tx\n")

    assert table["columns"] == ["id", "name"]


def test_a_trailing_delimiter_on_the_header_does_not_create_an_empty_column():
    """A line copied out of Excel ends with the delimiter. Naming that `column_3` would put a
    permanently NULL column in every table built this way."""
    table = delimited_import.read_table(b"id\tname\t\n1\tx\t\n")

    assert table["columns"] == ["id", "name"]
    assert table["rows"] == [["1", "x"]]


def test_a_blank_heading_between_named_ones_is_named_not_dropped():
    """Unlike a trailing one: a gap in the middle is a real column whose heading the author left
    empty, and dropping it would shift every column after it."""
    table = delimited_import.read_table(b"id\t\tname\n1\t2\t3\n")

    assert table["columns"] == ["id", "column_2", "name"]
    assert table["rows"] == [["1", "2", "3"]]


def test_duplicate_headings_are_disambiguated_the_same_way_a_workbook_is():
    """Both readers share `xlsx_import.header_names`, so the same data gives the same table
    whichever format it arrived in."""
    table = delimited_import.read_table(b"Qty\tqty\tQTY\n1\t2\t3\n")

    assert table["columns"] == ["Qty", "qty_2", "QTY_3"]


# --------------------------------------------------------------------------- #
# Refusing rather than corrupting
# --------------------------------------------------------------------------- #

def test_a_row_wider_than_the_header_is_refused_and_names_the_line():
    """More values than headings means the delimiter guess was wrong or the header is short.
    Clipping would load the file one column out of step, which surfaces weeks later as a join
    that returns nothing."""
    data = b"id\tname\n1\tx\n2\ty\tz\n"

    with pytest.raises(delimited_import.DelimitedImportError, match="Line 3"):
        delimited_import.read_table(data)


def test_a_row_with_trailing_empty_extras_is_not_refused():
    """The same shape, but the extra values are empty — that is the trailing-delimiter artifact
    again, not a misread file."""
    table = delimited_import.read_table(b"id\tname\n1\tx\t\t\n")

    assert table["rows"] == [["1", "x"]]


def test_a_short_row_is_padded_because_a_blank_tail_is_how_excel_writes_empties():
    table = delimited_import.read_table(b"id\tname\tnote\n1\tx\n")

    assert table["rows"] == [["1", "x", ""]]


def test_an_empty_file_says_so_instead_of_creating_a_table_with_no_columns():
    with pytest.raises(delimited_import.DelimitedImportError, match="empty"):
        delimited_import.read_table(b"   \n\n")


def test_max_rows_stops_the_read_and_says_it_stopped():
    body = b"id\n" + b"".join(f"{n}\n".encode() for n in range(50))

    table = delimited_import.read_table(body, max_rows=10)

    assert table["row_count"] == 10 and table["truncated"] is True


def test_an_explicit_delimiter_overrides_the_guess():
    """For the file where the guess is wrong — a pipe-delimited export whose data is full of
    commas, say. `tab` and `,` are both accepted spellings."""
    table = delimited_import.read_table(b"a|b\n1,2|3\n", delimiter="pipe")

    assert table["columns"] == ["a", "b"]
    assert table["rows"] == [["1,2", "3"]]


# --------------------------------------------------------------------------- #
# Which reader answers: decided by the bytes, never by the file name
# --------------------------------------------------------------------------- #

def test_a_zip_goes_to_the_workbook_reader_and_anything_else_to_the_text_one(tmp_path):
    """The file name is the least reliable thing about an attachment: a `.txt` that is really
    tab-separated data and a `.xlsx` that somebody renamed are both routine."""
    from db_ops.lib import xlsx_export

    path = tmp_path / "book.xlsx"
    xlsx_export.write_result_set_xlsx(path, columns=["a"], rows=[["1"]])

    assert table_load.read_source(path.read_bytes())["format"] == "xlsx"
    assert table_load.read_source(b"a\tb\n1\t2\n")["format"] == "delimited"


def test_a_text_file_arrives_base64_the_same_way_a_workbook_does():
    """Telegram hands every attachment over base64-encoded; the reader must not care which of
    the two formats is inside."""
    encoded = base64.b64encode(b"id\tname\n1\tx\n").decode()

    table = table_load.read_source(encoded)

    assert table["columns"] == ["id", "name"]


def test_an_old_xls_is_named_for_what_it_is_rather_than_read_as_text():
    """An OLE2 compound document is neither a zip nor text. Reading it as text produces a table
    of binary noise, which is worse than the refusal."""
    data = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64

    with pytest.raises(table_load.TableLoadError, match="old .xls"):
        table_load.read_source(data)


def test_a_reader_failure_arrives_as_a_table_load_error_so_the_cli_cannot_traceback():
    """Both readers raise their own type; `read_source` is where they become the one exception
    every caller of this module already catches."""
    with pytest.raises(table_load.TableLoadError):
        table_load.read_source(b"\n\n\n")
