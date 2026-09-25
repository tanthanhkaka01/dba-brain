"""A SQL task can run on PostgreSQL, and its script's statements come back one by one.

Until 2026-09-25 a scheduled SQL task could run on SQL Server and Oracle only: `sql-command-add`
refused `postgresql` (after a 2026-09-21 node errored nine hours into its schedule with
``Unsupported db_type: postgresql``). The operator asked for PostgreSQL tasks beside the others.

The task itself runs through `run-sql`, which already reached PostgreSQL. What did not work was a
**script**: `run-sql` split only on SQL Server's ``GO``, so a PostgreSQL script went to pg8000 as
one execute, and pg8000 returns one result set per execute - a task of ``SELECT pg_sleep(1);
INSERT ...; SELECT count(*) ...`` came back as ONE set holding both SELECTs' rows under the last
one's column name, with ``affected_rows`` 0 for the INSERT. So a PostgreSQL script is now run a
statement at a time (``lib.sql_text.split_postgresql_statements``).

Task parameters stay T-SQL (``DECLARE @name`` lines), so a PostgreSQL task takes none yet, and is
told so at registration.
"""

from __future__ import annotations

import pytest

from db_ops.common import sql_run
from db_ops.lib.sql_access import SQL_TASK_DB_TYPES
from db_ops.lib.sql_text import split_postgresql_statements as split


# --------------------------------------------------------------------------- #
# The split
# --------------------------------------------------------------------------- #
def test_a_script_splits_on_its_semicolons():
    assert split("select pg_sleep(1);\ninsert into drill(phase) values ('a');\nselect count(*) from drill;") == [
        "select pg_sleep(1)", "insert into drill(phase) values ('a')", "select count(*) from drill"]


def test_a_semicolon_inside_a_string_or_a_quoted_name_is_not_a_split():
    assert split("""select 'a;b', 'it''s;x', "col;name" from t""") == [
        """select 'a;b', 'it''s;x', "col;name" from t"""]
    assert split(r"select E'it\'s; ok' from t; select 2") == [r"select E'it\'s; ok' from t", "select 2"]


def test_a_function_body_or_a_do_block_stays_whole():
    script = ("DO $$ begin perform 1; perform 2; end $$;\n"
              "create function f() returns int as $body$ select 1; $body$ language sql;")
    assert split(script) == ["DO $$ begin perform 1; perform 2; end $$",
                             "create function f() returns int as $body$ select 1; $body$ language sql"]


def test_comments_do_not_split_and_a_part_of_only_comments_is_no_statement():
    assert split("-- a; comment\nselect 1; /* one; /* nested; */ two */ select 2; -- trailing only\n") == [
        "-- a; comment\nselect 1", "/* one; /* nested; */ two */ select 2"]
    assert split(";; \n -- nothing\n") == []


def test_a_dollar_inside_a_name_or_a_placeholder_is_not_a_quote():
    assert split("select a$b from t; select $1::int") == ["select a$b from t", "select $1::int"]


# --------------------------------------------------------------------------- #
# run-sql runs PostgreSQL a statement at a time
# --------------------------------------------------------------------------- #
class _PgCursor:
    """pg8000's shape: one result set per execute, no nextset."""

    def __init__(self):
        self.executed: list[tuple] = []
        self.description = None
        self.rowcount = -1
        self._rows: list = []

    def execute(self, statement, params=None):
        self.executed.append((statement, params))
        text = statement.strip().lower()
        if text.startswith("select"):
            self.description = [("n",)]
            self._rows = [[len(self.executed)]]
            self.rowcount = 1
        else:
            self.description = None
            self._rows = []
            self.rowcount = 1

    def fetchmany(self, size):
        rows, self._rows = self._rows, []
        return rows


def test_each_select_is_its_own_result_set_and_the_insert_is_counted():
    cursor = _PgCursor()

    sets, affected, _cut = sql_run.execute_capture(
        cursor, "select pg_sleep(1);\ninsert into drill(phase) values ('a');\nselect count(*) from drill;",
        db_type="postgresql", capture_all=True, max_result_sets=0)

    assert [s["rows"] for s in sets] == [[[1]], [[3]]]
    assert affected == 1
    assert [statement for statement, _ in cursor.executed] == [
        "select pg_sleep(1)", "insert into drill(phase) values ('a')", "select count(*) from drill"]


def test_bound_values_keep_the_script_whole():
    """Values are numbered for the whole script; split, each statement would get all of them."""
    cursor = _PgCursor()

    sql_run.execute_capture(cursor, "select %s; select %s", db_type="postgresql", params=[1, 2],
                            capture_all=True, max_result_sets=0)

    assert cursor.executed == [("select %s; select %s", (1, 2))]


def test_the_other_engines_are_split_as_before():
    assert sql_run.split_batches_for("SELECT 1\nGO\nSELECT 2", "sqlserver") == ["SELECT 1", "SELECT 2"]
    assert sql_run.split_batches_for("SELECT 1 FROM dual;", "oracle") == ["SELECT 1 FROM dual"]
    assert sql_run.split_batches_for("SELECT 1; SELECT 2;", "mysql") == ["SELECT 1; SELECT 2;"]


# --------------------------------------------------------------------------- #
# Registering and running one
# --------------------------------------------------------------------------- #
def test_postgresql_is_an_engine_a_task_can_run_on():
    assert "postgresql" in SQL_TASK_DB_TYPES


@pytest.fixture
def estate(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "sql_commands.json").write_text('{"sql_commands": []}', encoding="utf-8")
    return tmp_path


def test_a_postgresql_task_registers(estate):
    import json

    from db_ops.common import sql_task_admin

    sql_task_admin.add_sql_command({"display_name": "count the drill", "db_type": "postgresql",
                                    "sql_text": "select count(*) from drill;"},
                                   data_dir=estate / "data", tool_root=estate)

    saved = json.loads((estate / "data" / "sql_commands.json").read_text(encoding="utf-8"))["sql_commands"]
    assert [c["db_type"] for c in saved] == ["postgresql"]


def test_a_postgresql_task_with_parameters_is_refused_by_name(estate):
    from db_ops.common import sql_task_admin

    with pytest.raises(sql_task_admin.SqlTaskAdminError, match="takes no parameters"):
        sql_task_admin.add_sql_command({"display_name": "by job", "db_type": "postgresql",
                                        "sql_text": "select 1;",
                                        "parameters": [{"name": "job_no", "type": "nvarchar(50)"}]},
                                       data_dir=estate / "data", tool_root=estate)
