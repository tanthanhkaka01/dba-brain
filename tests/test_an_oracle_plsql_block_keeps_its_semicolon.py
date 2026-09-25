"""An Oracle PL/SQL block reaches the driver with the ``;`` after its ``END``.

Oracle's two parsers want opposite things at the end of a batch. Its SQL parser refuses a trailing
``;`` (``SELECT 1 FROM dual;`` raises ORA-00911), so ``run-sql`` stripped it from every Oracle
batch. Its PL/SQL parser requires the one after ``END``: ``BEGIN ... END`` without it raises
PLS-00103, *Encountered the symbol "end-of-file"*. So every Oracle SQL task with a block - the
usual way to call a procedure or wait on ``DBMS_SESSION.SLEEP`` - failed on its first run. Found on
2026-09-25, registering the lab's Oracle tasks: the probe answered exactly that PLS-00103.
"""

from __future__ import annotations

from db_ops.common import sql_run
from db_ops.lib.sql_text import is_plsql_block, oracle_statement


def test_a_statement_loses_its_semicolon():
    assert oracle_statement("SELECT COUNT(*) FROM drill;") == "SELECT COUNT(*) FROM drill"
    assert oracle_statement("INSERT INTO drill(task) VALUES ('a');  \n") == "INSERT INTO drill(task) VALUES ('a')"


def test_an_anonymous_block_keeps_the_semicolon_after_its_end():
    assert oracle_statement("BEGIN DBMS_SESSION.SLEEP(1); END;") == "BEGIN DBMS_SESSION.SLEEP(1); END;"
    assert oracle_statement("declare n number; begin n := 1; end;\n") == "declare n number; begin n := 1; end;"


def test_the_create_of_a_stored_unit_is_a_block():
    for text in ("CREATE OR REPLACE PROCEDURE p AS BEGIN NULL; END;",
                 "create function f return number is begin return 1; end;",
                 "CREATE OR REPLACE EDITIONABLE PACKAGE BODY pk AS END pk;",
                 "CREATE TRIGGER t BEFORE INSERT ON drill FOR EACH ROW BEGIN NULL; END;"):
        assert is_plsql_block(text), text
        assert oracle_statement(text) == text


def test_a_leading_comment_does_not_hide_a_block():
    text = "-- wait for the load\n/* one minute */ BEGIN DBMS_SESSION.SLEEP(60); END;"
    assert oracle_statement(text) == text


def test_a_table_or_a_view_is_not_a_block():
    assert not is_plsql_block("CREATE TABLE begin_log (id NUMBER);")
    assert not is_plsql_block("SELECT begin_date FROM drill;")
    assert oracle_statement("CREATE TABLE begin_log (id NUMBER);") == "CREATE TABLE begin_log (id NUMBER)"


def test_a_sqlplus_slash_after_a_block_is_not_sent():
    assert oracle_statement("BEGIN NULL; END;\n/\n") == "BEGIN NULL; END;"


def test_run_sql_sends_each_oracle_batch_the_way_its_parser_wants():
    script = ("BEGIN DBMS_SESSION.SLEEP(1); END;\nGO\n"
              "INSERT INTO drill(task) VALUES ('ORA_1M');\nGO\n"
              "SELECT COUNT(*) AS n FROM drill;")

    assert sql_run.split_batches_for(script, "oracle") == [
        "BEGIN DBMS_SESSION.SLEEP(1); END;",
        "INSERT INTO drill(task) VALUES ('ORA_1M')",
        "SELECT COUNT(*) AS n FROM drill",
    ]
