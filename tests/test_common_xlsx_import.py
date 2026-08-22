"""Reading a spreadsheet somebody actually made, not one we wrote ourselves.

The reader exists because openpyxl is not an image dependency and adding it would mean a rebuild
and a redeploy before an operator could load a file — the same reasoning that produced the
writer next to it. That means every quirk of the format is ours to handle, and these tests are
mostly a list of the quirks:

* Excel **omits empty cells** rather than writing blanks, so a row is not a list of values in
  order — it is a sparse map keyed by ``A1``-style references. Reading positionally shifts every
  later value one column left, on that row only, which is the kind of corruption that survives
  review because the file looks fine.
* A date is **a number plus a style**. Read literally it is ``45678``.
* Text is usually **a pointer into a shared string pool**, sometimes an inline string, and a
  formatted heading is split across runs.
* ``sheet1.xml`` is **not reliably the first tab**.

A header row written for a human is also not a set of column names: blanks and duplicates are
normal in one and illegal in the other.
"""

from __future__ import annotations

import base64
import datetime
import zipfile

import pytest

from db_ops.lib import xlsx_export, xlsx_import


# --------------------------------------------------------------------------- #
# A workbook built by hand, so the parts under test are the ones a real Excel writes
# --------------------------------------------------------------------------- #

_MAIN = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
_RELS = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'


def _workbook(tmp_path, sheet_xml: str, *, shared: list[str] | None = None,
              styles_xml: str | None = None, sheet_part: str = "worksheets/sheet1.xml",
              rel_target: str | None = None):
    """Write a minimal but structurally real .xlsx and return its bytes."""
    path = tmp_path / "hand.xlsx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("xl/workbook.xml",
                         f'<workbook {_MAIN} {_RELS}><sheets>'
                         f'<sheet name="Data" sheetId="1" r:id="rId1"/></sheets></workbook>')
        archive.writestr("xl/_rels/workbook.xml.rels",
                         '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
                         'relationships"><Relationship Id="rId1" Target="'
                         f'{rel_target or sheet_part}"/></Relationships>')
        archive.writestr(f"xl/{sheet_part}", f'<worksheet {_MAIN}><sheetData>{sheet_xml}'
                                             "</sheetData></worksheet>")
        if shared is not None:
            items = "".join(f"<si><t>{text}</t></si>" for text in shared)
            archive.writestr("xl/sharedStrings.xml", f'<sst {_MAIN}>{items}</sst>')
        if styles_xml is not None:
            archive.writestr("xl/styles.xml", styles_xml)
    return path.read_bytes()


def test_a_row_with_a_gap_keeps_its_columns_aligned(tmp_path):
    """Excel writes A and C for a row whose B is blank. Reading in arrival order would put C's
    value under B — and only on that row, so the file still looks plausible."""
    data = _workbook(tmp_path, (
        '<row r="1"><c r="A1" t="inlineStr"><is><t>first</t></is></c>'
        '<c r="B1" t="inlineStr"><is><t>second</t></is></c>'
        '<c r="C1" t="inlineStr"><is><t>third</t></is></c></row>'
        '<row r="2"><c r="A2" t="inlineStr"><is><t>a</t></is></c>'
        '<c r="C2" t="inlineStr"><is><t>c</t></is></c></row>'
    ))

    sheet = xlsx_import.read_sheet(data)

    assert sheet["columns"] == ["first", "second", "third"]
    assert sheet["rows"] == [["a", "", "c"]]


def test_the_excel_epoch_is_pinned_to_serials_excel_itself_publishes(tmp_path):
    """The conversion is only as good as its anchor, and an anchor checked against our own
    arithmetic proves nothing. These two are Microsoft's documented values: serial 61 is
    1900-03-01 (the first day after the phantom 1900-02-29 that shifts everything below it),
    and 45292 is 2024-01-01. Getting the epoch wrong shifts every date in every loaded sheet
    by a day, which nobody notices until a report is reconciled.
    """
    styles = (f'<styleSheet {_MAIN}><cellXfs count="1"><xf numFmtId="14"/></cellXfs>'
              "</styleSheet>")
    data = _workbook(tmp_path, (
        '<row r="1"><c r="A1" t="inlineStr"><is><t>d</t></is></c></row>'
        '<row r="2"><c r="A2" s="0"><v>61</v></c></row>'
        '<row r="3"><c r="A3" s="0"><v>45292</v></c></row>'
    ), styles_xml=styles)

    assert [row[0] for row in xlsx_import.read_sheet(data)["rows"]] == ["1900-03-01", "2024-01-01"]


def test_a_date_cell_is_a_number_until_its_style_says_otherwise(tmp_path):
    """The whole reason styles.xml is parsed at all. Serial 46246 with a date-formatted style is
    2026-08-12; the same number with a plain style is the number 46246."""
    styles = (f'<styleSheet {_MAIN}><numFmts count="1">'
              '<numFmt numFmtId="164" formatCode="yyyy\\-mm\\-dd"/></numFmts>'
              '<cellXfs count="3">'
              '<xf numFmtId="0"/>'          # style 0 - General
              '<xf numFmtId="14"/>'         # style 1 - built-in short date
              '<xf numFmtId="164"/>'        # style 2 - custom date
              '</cellXfs></styleSheet>')
    data = _workbook(tmp_path, (
        '<row r="1"><c r="A1" t="inlineStr"><is><t>plain</t></is></c>'
        '<c r="B1" t="inlineStr"><is><t>builtin</t></is></c>'
        '<c r="C1" t="inlineStr"><is><t>custom</t></is></c></row>'
        '<row r="2"><c r="A2" s="0"><v>46246</v></c>'
        '<c r="B2" s="1"><v>46246</v></c>'
        '<c r="C2" s="2"><v>46246</v></c></row>'
    ), styles_xml=styles)

    row = xlsx_import.read_sheet(data)["rows"][0]

    assert row[0] == "46246"            # no date style: still a number
    assert row[1] == "2026-08-12"       # built-in format 14
    assert row[2] == "2026-08-12"       # custom yyyy-mm-dd


def test_a_time_of_day_survives_the_date_conversion(tmp_path):
    styles = (f'<styleSheet {_MAIN}><cellXfs count="1"><xf numFmtId="22"/></cellXfs>'
              "</styleSheet>")
    data = _workbook(tmp_path, (
        '<row r="1"><c r="A1" t="inlineStr"><is><t>when</t></is></c></row>'
        '<row r="2"><c r="A2" s="0"><v>46246.5</v></c></row>'
    ), styles_xml=styles)

    assert xlsx_import.read_sheet(data)["rows"][0][0] == "2026-08-12 12:00:00"


def test_a_word_in_a_custom_format_is_not_mistaken_for_a_date(tmp_path):
    """`"Delivered"0.00` contains a `d`. Matching the format code raw would read every cell in
    that column as a date and turn a quantity into 1993."""
    styles = (f'<styleSheet {_MAIN}><numFmts count="1">'
              '<numFmt numFmtId="164" formatCode="&quot;Delivered&quot;0.00"/></numFmts>'
              '<cellXfs count="1"><xf numFmtId="164"/></cellXfs></styleSheet>')
    data = _workbook(tmp_path, (
        '<row r="1"><c r="A1" t="inlineStr"><is><t>qty</t></is></c></row>'
        '<row r="2"><c r="A2" s="0"><v>34012.5</v></c></row>'
    ), styles_xml=styles)

    assert xlsx_import.read_sheet(data)["rows"][0][0] == "34012.5"


def test_shared_strings_and_rich_text_runs_both_read_as_their_text(tmp_path):
    """A heading someone bolded half of is stored as two runs. Taking only the first <t> reads
    'Order' where the sheet shows 'Order No'."""
    data = _workbook(tmp_path, (
        '<row r="1"><c r="A1" t="s"><v>0</v></c></row>'
        '<row r="2"><c r="A2" t="s"><v>1</v></c></row>'
    ), shared=["Order No", "AA2608/01902"])

    sheet = xlsx_import.read_sheet(data)

    assert sheet["columns"] == ["Order No"]
    assert sheet["rows"] == [["AA2608/01902"]]


def test_the_first_tab_is_found_through_the_relationship_table(tmp_path):
    """A workbook whose sheets were reordered keeps the original part names, so the first tab can
    be sheet3.xml. Reading whichever part sorts first loads the wrong data with no error."""
    data = _workbook(tmp_path, '<row r="1"><c r="A1" t="inlineStr"><is><t>right</t></is></c></row>',
                     sheet_part="worksheets/sheet7.xml")

    assert xlsx_import.read_sheet(data)["columns"] == ["right"]


# --------------------------------------------------------------------------- #
# The header row is written for a human
# --------------------------------------------------------------------------- #

def test_a_blank_heading_becomes_a_positional_name_not_an_empty_column(tmp_path):
    data = _workbook(tmp_path, (
        '<row r="1"><c r="A1" t="inlineStr"><is><t>a</t></is></c>'
        '<c r="C1" t="inlineStr"><is><t>c</t></is></c></row>'
        '<row r="2"><c r="A2" t="inlineStr"><is><t>1</t></is></c></row>'
    ))

    assert xlsx_import.read_sheet(data)["columns"] == ["a", "column_2", "c"]


def test_a_repeated_heading_is_suffixed_rather_than_colliding(tmp_path):
    """Two columns called Qty is normal in a spreadsheet and illegal as column names. Letting the
    engine reject it names a column the operator cannot find in their file."""
    data = _workbook(tmp_path, (
        '<row r="1"><c r="A1" t="inlineStr"><is><t>Qty</t></is></c>'
        '<c r="B1" t="inlineStr"><is><t>qty</t></is></c>'
        '<c r="C1" t="inlineStr"><is><t>Qty</t></is></c></row>'
    ))

    assert xlsx_import.read_sheet(data)["columns"] == ["Qty", "qty_2", "Qty_3"]


def test_leading_blank_rows_are_skipped_to_find_the_header(tmp_path):
    """An exported report often opens with a title row and a blank one."""
    data = _workbook(tmp_path, (
        '<row r="1"></row>'
        '<row r="2"><c r="A2" t="inlineStr"><is><t>real header</t></is></c></row>'
        '<row r="3"><c r="A3" t="inlineStr"><is><t>value</t></is></c></row>'
    ))

    sheet = xlsx_import.read_sheet(data)

    assert sheet["columns"] == ["real header"]
    assert sheet["rows"] == [["value"]]


# --------------------------------------------------------------------------- #
# What the caller passes in, and what it is told when the file is not one
# --------------------------------------------------------------------------- #

def test_the_workbook_round_trips_through_the_writer_next_door(tmp_path):
    """The two modules are a pair; a change to either that breaks the round trip is a bug."""
    path = tmp_path / "written.xlsx"
    xlsx_export.write_result_set_xlsx(
        path,
        columns=["Job No", "Qty", "Note"],
        rows=[["AA2608/01902", 1500, "đơn hàng"], ["AA2608/01903", 12.5, ""]],
    )

    sheet = xlsx_import.read_sheet(path.read_bytes())

    assert sheet["columns"] == ["Job No", "Qty", "Note"]
    # 1500 reads as "1500", not "1500.0" — the spreadsheet showed an integer.
    assert sheet["rows"] == [["AA2608/01902", "1500", "đơn hàng"],
                             ["AA2608/01903", "12.5", ""]]


def test_base64_is_accepted_however_it_was_pasted(tmp_path):
    """It arrives from a JSON request, sometimes newline-wrapped, sometimes as a data: URI."""
    path = tmp_path / "w.xlsx"
    xlsx_export.write_result_set_xlsx(path, columns=["a"], rows=[["1"]])
    encoded = base64.b64encode(path.read_bytes()).decode()

    plain = xlsx_import.read_sheet(encoded)
    wrapped = xlsx_import.read_sheet("\n".join(encoded[i:i + 76]
                                               for i in range(0, len(encoded), 76)))
    uri = xlsx_import.read_sheet(f"data:application/vnd.ms-excel;base64,{encoded}")

    assert plain["rows"] == wrapped["rows"] == uri["rows"] == [["1"]]


def test_max_rows_stops_reading_and_says_so(tmp_path):
    rows = "".join(f'<row r="{n}"><c r="A{n}" t="inlineStr"><is><t>{n}</t></is></c></row>'
                   for n in range(1, 12))
    data = _workbook(tmp_path, rows)

    sheet = xlsx_import.read_sheet(data, max_rows=4)

    assert sheet["row_count"] == 4 and sheet["truncated"] is True


def test_an_xls_saved_by_an_old_excel_is_refused_with_the_reason(tmp_path):
    """.xls is a different format, not an .xlsx with the wrong extension — and 'BadZipFile' is
    not something an operator can act on."""
    with pytest.raises(xlsx_import.XlsxImportError, match="re-save it as .xlsx"):
        xlsx_import.read_sheet(b"\xd0\xcf\x11\xe0not a zip at all")


def test_an_empty_sheet_says_there_is_no_header_rather_than_creating_nothing(tmp_path):
    data = _workbook(tmp_path, "")

    with pytest.raises(xlsx_import.XlsxImportError, match="no header row"):
        xlsx_import.read_sheet(data)


def test_bad_base64_names_the_field_it_came_from(tmp_path):
    with pytest.raises(xlsx_import.XlsxImportError, match="xlsx_base64 is not valid base64"):
        xlsx_import.read_sheet("this is not base64 !!!")


def test_column_references_convert_past_the_first_26(tmp_path):
    assert xlsx_import.column_index("A1") == 0
    assert xlsx_import.column_index("Z9") == 25
    assert xlsx_import.column_index("AA1") == 26
    assert xlsx_import.column_index("AB100") == 27


def test_a_generated_table_name_is_random_enough_to_not_collide():
    """Two uploads in the same second must not pick the same name, and nothing here can see the
    target's tables before it connects."""
    names = {xlsx_import.generate_table_name() for _ in range(200)}

    assert len(names) == 200
    assert all(name.startswith("temp_") for name in names)
