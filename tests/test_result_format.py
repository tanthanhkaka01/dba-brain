"""A result set has to survive being rendered for a person, a file, and a pipeline.

Everything used to come out as JSON, so getting a table, a spreadsheet or a pipeable column meant
piping the JSON into whatever the operator remembered — the formatting lived in shell history and
was different every time. These are the renderings, and what each one must not lose.

The one that keeps coming back is NULL. An empty cell and a SQL NULL mean different things, and a
rendering that prints both as nothing has silently answered a question it was not asked. So every
text format here has to keep them apart, and the tests say so.
"""

from __future__ import annotations

import datetime
import decimal
import pathlib
import zipfile

import pytest

from db_ops.lib import result_format


def _result(**overrides):
    base = {
        "ok": True,
        "server_id": "CLOUD-203-0-113-188-PG-5433",
        "database": "postgres",
        "columns": ["datname", "bytes", "note"],
        "rows": [["postgres", 7861951, None], ["template0", 7701007, "keep"]],
        "row_count": 2,
        "truncated": False,
    }
    base.update(overrides)
    return base


def test_a_null_is_never_rendered_as_an_empty_string_in_text():
    text, _ = result_format.render_result(_result(), fmt="txt")
    assert "NULL" in text
    assert "(2 rows)" in text


def test_the_text_table_aligns_on_the_widest_value_not_the_header():
    text, _ = result_format.render_result(_result(), fmt="txt")
    header, separator, first = text.splitlines()[:3]
    assert len(header) == len(separator)
    assert len(first) <= len(separator)


def test_raw_prints_values_only_so_a_pipeline_can_cut_a_column():
    """Anything else on stdout is something the pipeline has to be taught to skip."""
    text, _ = result_format.render_result(_result(), fmt="raw")
    assert text.splitlines() == ["postgres\t7861951\tNULL", "template0\t7701007\tkeep"]
    assert "datname" not in text
    assert "rows)" not in text


def test_csv_keeps_null_and_the_empty_string_apart_the_way_a_spreadsheet_reads_them():
    """PostgreSQL's COPY ... WITH CSV convention: an empty *unquoted* field is NULL, an empty
    *quoted* field is the empty string. Writing the word NULL instead would be indistinguishable
    from a four-character string that says NULL — the likelier reading in a file bound for Excel."""
    text, _ = result_format.render_result(
        _result(columns=["a", "b"], rows=[[None, ""]], row_count=1), fmt="csv")
    assert text.splitlines()[1] == ',""'


def test_csv_leaves_numbers_unquoted_so_a_spreadsheet_treats_them_as_numbers():
    """QUOTE_ALL would have been unambiguous and unusable — every number would arrive as text."""
    text, _ = result_format.render_result(
        _result(columns=["n", "d", "s"],
                rows=[[7861951, decimal.Decimal("1234.50"), "text"]], row_count=1),
        fmt="csv")
    assert text.splitlines()[1] == '7861951,1234.50,"text"'


def test_csv_escapes_a_comma_and_a_quote_rather_than_splitting_the_row():
    text, _ = result_format.render_result(
        _result(columns=["a"], rows=[['has,comma and "quote"']], row_count=1), fmt="csv")
    assert text.splitlines()[1] == '"has,comma and ""quote"""'


def test_csv_starts_with_the_header_row():
    text, _ = result_format.render_result(_result(), fmt="csv")
    assert text.splitlines()[0] == '"datname","bytes","note"'


def test_xml_carries_the_column_name_as_an_attribute_not_as_a_tag():
    """SQL will happily return a column called "1" or "count(*)", and neither can be an XML
    element name — so a renderer that makes tags out of column names breaks on real queries."""
    text, _ = result_format.render_result(
        _result(columns=["count(*)"], rows=[[3]], row_count=1), fmt="xml")
    assert '<value name="count(*)">3</value>' in text
    assert "<count(*)>" not in text


def test_xml_marks_a_null_rather_than_emitting_an_empty_element():
    text, _ = result_format.render_result(_result(), fmt="xml")
    assert '<value name="note" null="true"></value>' in text


def test_xml_escapes_values_that_would_otherwise_close_a_tag():
    text, _ = result_format.render_result(
        _result(columns=["c"], rows=[["<a & b>"]], row_count=1), fmt="xml")
    assert "&lt;a &amp; b&gt;" in text
    assert "<a & b>" not in text


def test_xlsx_writes_a_real_workbook_and_says_where_it_went(tmp_path):
    destination = tmp_path / "out" / "result.xlsx"
    text, extra = result_format.render_result(
        _result(), fmt="xlsx", output_path=str(destination))
    assert destination.is_file()
    assert extra["output_path"] == str(destination)
    assert extra["row_count"] == 2
    with zipfile.ZipFile(destination) as archive:
        assert "xl/worksheets/sheet1.xml" in archive.namelist()
    assert '"format": "xlsx"' in text


def test_xlsx_without_an_output_path_is_refused_rather_than_written_somewhere_guessed(tmp_path):
    with pytest.raises(result_format.ResultFormatError, match="output_path"):
        result_format.render_result(_result(), fmt="xlsx")


def test_typed_values_keep_their_meaning_in_text():
    """A Decimal rendered in scientific notation and a datetime rendered as a repr are both
    wrong in a report someone pastes into a ticket."""
    rendered, _ = result_format.render_result(
        _result(
            columns=["when", "amount", "flag", "blob"],
            rows=[[datetime.datetime(2026, 8, 4, 9, 30), decimal.Decimal("1234.50"), True, b"ab"]],
            row_count=1,
        ),
        fmt="raw",
    )
    assert rendered == "2026-08-04T09:30:00\t1234.50\ttrue\t<2 bytes>"


def test_a_statement_with_no_result_set_still_reports_what_it_did():
    """Printing nothing at all reads as "it did not run"."""
    text, _ = result_format.render_result(
        {"ok": True, "columns": [], "rows": [], "affected_rows": 7}, fmt="txt")
    assert "7 row(s) affected" in text


def test_a_truncated_result_says_so_instead_of_looking_complete():
    text, _ = result_format.render_result(_result(truncated=True), fmt="txt")
    assert "truncated" in text


def test_the_words_an_operator_actually_types_are_accepted():
    for spelling in ("text", "plain", "table", "TXT", " txt "):
        assert result_format.normalize_format(spelling) == "txt"
    assert result_format.normalize_format(None) == "json"
    assert result_format.normalize_format("") == "json"


def test_an_unknown_format_names_the_ones_that_exist():
    with pytest.raises(result_format.ResultFormatError, match="json, txt, csv, xml, xlsx, raw"):
        result_format.normalize_format("pdf")


def test_write_result_is_one_call_whatever_the_format(tmp_path):
    """The point of the function: a caller that exports must not have to know that xlsx writes
    itself while the text formats hand back a string. Every exporter that knew the difference
    grew its own `if fmt == "xlsx"`, and they did not stay identical."""
    for file_format in ("csv", "txt", "xml", "xlsx", "json"):
        written = result_format.write_result(
            _result(), fmt=file_format, path=tmp_path / f"out.{file_format}")
        path = pathlib.Path(written["path"])
        assert path.is_file(), file_format
        assert written["format"] == file_format
        assert written["bytes"] == path.stat().st_size


def test_write_result_creates_the_directory_rather_than_failing_on_it(tmp_path):
    written = result_format.write_result(
        _result(), fmt="csv", path=tmp_path / "does" / "not" / "exist" / "out.csv")
    assert pathlib.Path(written["path"]).is_file()


def test_a_written_csv_has_no_blank_line_between_rows(tmp_path):
    """Written with newline="" so the csv writer's own \n is not translated to \r\n: a file
    written on the Windows master and read on the Linux worker would otherwise show a blank line
    per row."""
    written = result_format.write_result(_result(), fmt="csv", path=tmp_path / "out.csv")
    raw = pathlib.Path(written["path"]).read_bytes()
    assert b"\r\n" not in raw
    assert raw.decode().count("\n") == 3  # header + 2 rows
