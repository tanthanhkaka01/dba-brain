"""Building a table out of a spreadsheet, without letting the spreadsheet write the SQL.

Two things here are not ordinary code and are why most of these tests exist.

**An identifier cannot be a bind parameter.** Column names come out of a file an operator was
emailed, and they end up inside `CREATE TABLE` as text. Quoting is the whole defence, so it is
tested against a heading that is a deliberate injection attempt rather than against a tidy one.

**The placeholder depends on the process, not the engine.** pg8000 reads `paramstyle` from a
*module global*, and `db/backend.py` sets it to `qmark` for the runtime store's own SQL. So the
correct placeholder for PostgreSQL is `?` inside the daemon and `%s` in a bare CLI run. Guessing
either way produces a syntax error in half the callers, so the module asks the driver — and a
regression here is silent until it reaches a real PostgreSQL.

Everything below runs offline: no database, no connection. What is under test is the SQL that
would be sent and the decisions taken before sending it.
"""

from __future__ import annotations

import base64

import pytest

from db_ops.common import table_load
from db_ops.lib import xlsx_export


# --------------------------------------------------------------------------- #
# Identifiers: the file writes the column names, not the author of this module
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("db_type,expected", [
    ("sqlserver", "[Job No]"),
    ("postgresql", '"Job No"'),
    ("oracle", '"Job No"'),
    ("mysql", "`Job No`"),
])
def test_each_engine_gets_its_own_quoting(db_type, expected):
    assert table_load.quote_identifier(db_type, "Job No") == expected


@pytest.mark.parametrize("db_type,heading,expected", [
    ("sqlserver", "x] ); DROP TABLE users --", "[x]] ); DROP TABLE users --]"),
    ("postgresql", 'x" ); DROP TABLE users --', '"x"" ); DROP TABLE users --"'),
    ("mysql", "x` ); DROP TABLE users --", "`x`` ); DROP TABLE users --`"),
])
def test_a_heading_that_tries_to_close_the_quote_stays_one_identifier(db_type, heading, expected):
    """The closing quote is doubled, so the statement still declares exactly one column with a
    silly name — which is the correct outcome, not an error. Rejecting it would mean a file with
    a stray bracket in a heading could not be loaded at all."""
    assert table_load.quote_identifier(db_type, heading) == expected


def test_a_control_character_in_a_heading_is_refused_rather_than_quoted():
    """No legitimate column heading contains a NUL, and what quoting does with one is
    driver-specific."""
    with pytest.raises(table_load.TableLoadError, match="control character"):
        table_load.quote_identifier("sqlserver", "bad\x00name")


def test_an_empty_identifier_is_refused():
    with pytest.raises(table_load.TableLoadError, match="cannot be empty"):
        table_load.quote_identifier("sqlserver", "   ")


# --------------------------------------------------------------------------- #
# Placeholders
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("style,expected", [
    ("qmark", "?, ?, ?"),
    ("numeric", ":1, :2, :3"),
    ("format", "%s, %s, %s"),
    ("pyformat", "%s, %s, %s"),
])
def test_placeholders_are_built_for_the_style_the_driver_reads(style, expected):
    assert table_load.build_placeholders(style, 3) == expected


def test_two_placeholders_are_distinct_under_numeric_binding():
    """The bug this function exists for: calling build_placeholders(style, 1) twice gives `:1`
    for both parameters, so Oracle binds the table name to the owner column as well and the
    existence check matches nothing — the table is then created a second time."""
    assert table_load.two_placeholders("numeric") == (":1", ":2")
    assert table_load.two_placeholders("qmark") == ("?", "?")


def test_the_postgresql_style_is_read_from_the_driver_not_assumed(monkeypatch):
    """It changes with what else has been imported into the process. See the module docstring."""
    pg8000_dbapi = pytest.importorskip("pg8000.dbapi")

    monkeypatch.setattr(pg8000_dbapi, "paramstyle", "qmark", raising=False)
    assert table_load.placeholder_style("postgresql") == "qmark"

    monkeypatch.setattr(pg8000_dbapi, "paramstyle", "format", raising=False)
    assert table_load.placeholder_style("postgresql") == "format"


def test_the_fixed_engines_do_not_change_style_with_the_process():
    assert table_load.placeholder_style("sqlserver") == "qmark"
    assert table_load.placeholder_style("oracle") == "numeric"
    assert table_load.placeholder_style("mysql") == "format"


# --------------------------------------------------------------------------- #
# The CREATE statement
# --------------------------------------------------------------------------- #

def test_every_column_is_text_because_a_guessed_type_is_wrong_on_the_row_nobody_checked():
    sql = table_load.build_create_table(
        "sqlserver", schema="dbo", table="temp_x", columns=["Job No", "Qty"], text_length=4000)

    assert sql.startswith("CREATE TABLE [dbo].[temp_x]")
    assert sql.count("NVARCHAR(4000)") == 2


@pytest.mark.parametrize("db_type,fragment", [
    ("sqlserver", "NVARCHAR(4000)"),
    ("postgresql", "varchar(4000)"),
    ("oracle", "VARCHAR2(4000 CHAR)"),
    ("mysql", "TEXT"),
])
def test_the_text_type_is_the_one_that_engine_actually_has(db_type, fragment):
    """MySQL is the odd one: its 65,535-byte limit is on the row, so a wide table of
    varchar(4000) cannot be created at all and the error names the row size rather than the
    column that caused it."""
    sql = table_load.build_create_table(db_type, schema="s", table="t", columns=["a"],
                                        text_length=4000)

    assert fragment in sql


def test_over_4000_characters_becomes_max_on_sql_server_rather_than_an_illegal_width():
    """NVARCHAR(8000) is not a thing; the engine's error for it does not say so plainly."""
    sql = table_load.build_create_table("sqlserver", schema="dbo", table="t", columns=["a"],
                                        text_length=8000)

    assert "NVARCHAR(MAX)" in sql


def test_a_table_with_no_schema_is_unqualified_rather_than_prefixed_with_a_blank():
    sql = table_load.build_create_table("sqlserver", schema="", table="t", columns=["a"],
                                        text_length=10)

    assert "CREATE TABLE [t]" in sql


# --------------------------------------------------------------------------- #
# Values that will not fit
# --------------------------------------------------------------------------- #

def test_a_value_too_long_names_the_row_and_column_instead_of_being_clipped():
    """A clipped value looks complete. An order number one character short joins to nothing and
    the report is quietly wrong — much more expensive than a load that stops."""
    with pytest.raises(table_load.TableLoadError, match=r"Row 3, column 'Note'"):
        table_load._check_lengths(["Job", "Note"], [["a", "ok"], ["b", "x" * 4001]], 4000)


def test_the_row_number_in_the_message_is_the_one_the_operator_sees_in_excel():
    """Row 1 is the header in their file, so the first data row is row 2 — not row 1 and not
    row 0. An off-by-one here sends them to the wrong line of a 40,000-row sheet."""
    with pytest.raises(table_load.TableLoadError, match=r"Row 2,"):
        table_load._check_lengths(["a"], [["x" * 11]], 10)


# --------------------------------------------------------------------------- #
# Request parsing: the decisions taken before anything connects
# --------------------------------------------------------------------------- #

def _sample_xlsx_base64(tmp_path) -> str:
    path = tmp_path / "s.xlsx"
    xlsx_export.write_result_set_xlsx(path, columns=["a"], rows=[["1"]])
    return base64.b64encode(path.read_bytes()).decode()


def test_a_request_without_a_target_says_so_before_reading_the_workbook(tmp_path):
    with pytest.raises(table_load.TableLoadError, match='needs a "target"'):
        table_load._parse_request({"xlsx_base64": _sample_xlsx_base64(tmp_path)})


def test_a_request_without_a_workbook_is_refused():
    with pytest.raises(table_load.TableLoadError, match='needs "file_base64"'):
        table_load._parse_request({"target": "ACME-1"})


def test_the_original_xlsx_keys_still_name_the_file():
    """`file_base64` is the name now that a text file is accepted too, but `xlsx_base64` is what
    the shipped Telegram command config and every saved shell payload still say. Dropping it
    would have broken the deployed worker at the moment the image was replaced."""
    parsed = table_load._parse_request({"target": "ACME-1", "xlsx_base64": "UEsDBBQ="})

    assert parsed["payload"] == "UEsDBBQ="


def test_an_unknown_if_exists_is_refused_with_the_choices_listed():
    with pytest.raises(table_load.TableLoadError, match="error, drop, append"):
        table_load._parse_request({"target": "ACME-1", "xlsx_base64": "x", "if_exists": "replace"})


def test_load_rows_defaults_to_true_because_that_is_why_a_file_was_attached(tmp_path):
    parsed = table_load._parse_request(
        {"target": "ACME-1", "xlsx_base64": _sample_xlsx_base64(tmp_path)})

    assert parsed["load_rows"] is True
    assert parsed["if_exists"] == "error"       # never destroys without being asked
    assert parsed["text_length"] == 4000


def test_a_missing_xlsx_path_names_the_path(tmp_path):
    with pytest.raises(table_load.TableLoadError, match="file_path not found"):
        table_load._parse_request({"target": "ACME-1", "xlsx_path": str(tmp_path / "nope.xlsx")})


@pytest.mark.parametrize("db_type,expected", [
    ("sqlserver", "dbo"),
    ("postgresql", "public"),
])
def test_each_engine_has_the_default_schema_its_users_expect(db_type, expected):
    assert table_load._default_schema(db_type, {}) == expected


def test_oracle_defaults_to_the_login_because_that_is_where_an_unqualified_table_lands():
    """Naming it keeps the response honest about where the table went, instead of leaving the
    reader to infer it from the login."""
    assert table_load._default_schema("oracle", {"username": "dba_user"}) == "DBA_USER"
