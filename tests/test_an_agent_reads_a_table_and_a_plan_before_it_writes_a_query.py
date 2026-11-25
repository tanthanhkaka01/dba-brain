"""An agent reads a table, an object and a plan before it writes a query (1.1.0, 1.421).

At least 8 of 22 failed MCP `run-sql` calls on 2026-11-22 were *Invalid column name*: the model wrote
a column it had not read. `table-info`, `object-source`, `db-activity` and `query-plan` answer what it
needed, each with SQL of `common.db_inspect` - so what is held here is what reaches that SQL:

* a name is a name, written as a literal - no quote, comment or separator gets in;
* `query-plan` compiles one statement and never runs it: no GO line, no setting that turns
  SHOWPLAN off, no second statement after an EXPLAIN; Oracle has none (EXPLAIN PLAN writes);
* a view or a row cap the command does not know is refused by name.

That the SQL reads what it should was run on SQL Server 2022, PostgreSQL 18, Oracle 23 and MySQL on
2026-11-23: a WAITFOR of 8 s and a pg_sleep(8) came back from query-plan in 0.2 s.
"""

from __future__ import annotations

import pytest

from dbabrain.common import db_inspect as D


@pytest.mark.parametrize(("raw", "schema", "name"), [
    ("orders", "", "orders"), ("dbo.orders", "dbo", "orders"), ("[dbo].[Order Lines]", "dbo", "Order Lines"),
    ('"Sales"."Order.Lines"', "Sales", "Order.Lines"), ("db.dbo.t", "dbo", "t"), ("`app`.`t`", "app", "t"),
])
def test_a_name_is_read_as_its_parts(raw, schema, name):
    assert D._object({"object": raw})[1:] == (schema, name)


@pytest.mark.parametrize("raw", ["", "t'; DROP TABLE x --", "t; DROP", "t -- x", "a.b.c.d", "t\nGO", "t/*x*/"])
def test_what_is_not_a_name_never_reaches_the_sql(raw):
    with pytest.raises(D.DbInspectError):
        D._object({"object": raw})


@pytest.mark.parametrize("engine", ["sqlserver", "postgresql", "oracle", "mysql"])
def test_every_engine_writes_the_name_as_a_literal(engine):
    sql = D._table_sql(engine, "dbo.Orders", "dbo", "Orders")
    assert "'" in sql and ";" not in sql.replace("';", "")


@pytest.mark.parametrize("text", ["SELECT 1\nGO\nDELETE t", "SET SHOWPLAN_XML OFF", "select 1\n  go  \n",
                                  "SET NOEXEC OFF", "SET PARSEONLY ON", "SET FMTONLY ON"])
def test_a_sql_server_statement_cannot_turn_the_showplan_off(text):
    with pytest.raises(D.DbInspectError, match="SHOWPLAN"):
        D._statement({"sql_text": text}, "sqlserver")


@pytest.mark.parametrize("engine", ["postgresql", "mysql"])
def test_explain_takes_one_statement_and_a_second_is_refused(engine):
    assert D._statement({"sql_text": "SELECT 1;"}, engine) == "SELECT 1"
    with pytest.raises(D.DbInspectError, match="one statement"):
        D._statement({"sql_text": "SELECT 1; DELETE FROM t"}, engine)


def test_a_statement_is_required():
    with pytest.raises(D.DbInspectError, match="sql_text"):
        D._statement({"sql_text": "  "}, "sqlserver")


@pytest.mark.parametrize("value", [0, 1001, "5", True, 2.5])
def test_the_row_cap_is_a_whole_number_from_1_to_1000(value):
    with pytest.raises(D.DbInspectError, match="max_rows"):
        D._limit({"max_rows": value})


def test_every_view_is_answered_on_sql_server_postgresql_and_oracle():
    for engine in ("sqlserver", "postgresql", "oracle"):
        assert {view for e, view in D._ACTIVITY if e == engine} == set(D.VIEWS), engine


def test_the_plan_summary_names_the_cost_the_costliest_operator_and_the_missing_index():
    plan = ('<ShowPlanXML xmlns="http://schemas.microsoft.com/sqlserver/2004/07/showplan"><StmtSimple '
            'StatementSubTreeCost="2.5"><MissingIndexGroup Impact="88.1"><MissingIndex Schema="[dbo]" Table="[t]">'
            '<ColumnGroup Usage="EQUALITY"><Column Name="[a]"/><Column Name="[b]"/></ColumnGroup><ColumnGroup '
            'Usage="INCLUDE"><Column Name="[c]"/></ColumnGroup></MissingIndex></MissingIndexGroup>'
            '<RelOp PhysicalOp="Index Scan" LogicalOp="Index Scan" EstimateRows="10" EstimatedTotalSubtreeCost="2.4"/>'
            '<RelOp PhysicalOp="Sort" LogicalOp="Sort" EstimateRows="10" EstimatedTotalSubtreeCost="2.5"/></StmtSimple>'
            '</ShowPlanXML>')
    summary = D._sqlserver_plan_summary(plan)

    assert summary["estimated_cost"] == 2.5 and summary["top_operators"][0]["operator"] == "Sort"
    assert summary["missing_indexes"] == [{"impact_pct": 88.1, "table": "dbo.t", "equality": "a, b",
                                           "inequality": "", "include": "c"}]
