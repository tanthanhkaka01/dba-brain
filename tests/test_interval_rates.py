"""Pulling a collector's structured fields back out of the message it wrote them into.

A metric row keeps one derived number in ``metric_value``; everything else the collector measured
survives only as ``key=value`` pairs inside ``message``. The report re-reads them from there, so
this parser is shared — a page that split those fields differently from the collector that wrote
them would disagree with the alert about the same sample.

The interval arithmetic this module used to hold (differencing two stored samples of a cumulative
counter, with its restart and too-wide-a-gap refusals) was removed on 2026-08-11 together with the
rest of ``PERFORMANCE_IO_LATENCY``'s bespoke path; metrics grade themselves in their SQL or command
now. Those tests are in git history at 2.75.04 and come back with the code if interval grading ever
returns as something any cumulative metric can declare.
"""

from db_ops.lib import interval_rates as ir


def test_a_windows_path_does_not_swallow_the_next_field():
    """The value contains backslashes and a colon, and the field after it must still be found. A
    path is the most common value in these messages and the easiest one for a parser to run past."""
    fields = ir.message_fields(
        r"database=SALESDB, file_type=ROWS, file=E:\MSSQL15\DATA\SALESDB.mdf, reads=3590421")

    assert fields["file"] == r"E:\MSSQL15\DATA\SALESDB.mdf"
    assert fields["reads"] == "3590421"
    assert fields["file_type"] == "ROWS"


def test_a_value_containing_an_equals_sign_is_not_read_as_a_new_key():
    """Error texts carry ``=``. The key is anchored on a comma or whitespace precisely so the tail
    of a value cannot be mistaken for the start of the next field."""
    fields = ir.message_fields("status=ERROR, detail=login failed for user=sa, code=18456")

    assert fields["detail"] == "login failed for user=sa"
    assert fields["code"] == "18456"


def test_keys_are_lower_cased_so_a_reader_never_guesses_the_casing():
    assert ir.message_fields("Database=SALESDB, File_Type=LOG")["file_type"] == "LOG"


def test_a_message_with_no_fields_is_an_empty_mapping_not_an_error():
    """Most metrics write prose. Asking one of those for its fields must be answerable."""
    assert ir.message_fields("SQL returned no rows.") == {}
    assert ir.message_fields(None) == {}
