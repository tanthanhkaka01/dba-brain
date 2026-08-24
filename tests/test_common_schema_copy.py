"""The schema copy, exercised without a SQL Server.

Three layers, and the split between them is what makes this file possible at all:

* :mod:`db_ops.lib.name_filter` and :mod:`db_ops.lib.mssql_ddl` are pure — names and dicts in,
  names and SQL text out — so the shapes that broke real deployments (a partitioned index, a
  computed column, a clustered columnstore, a filtered unique index) are asserted directly.
* :mod:`db_ops.common.schema_catalog` and the phase planners take a **cursor**, so a fake one
  that answers catalogue queries from a dict is enough to plan a whole copy.
* Only :func:`db_ops.common.schema_copy.copy_schema` opens a connection, and it is not called
  here.

What is being defended, in order of what it has already cost:

1. **A partitioned index keeps its ``ON scheme(column)``.** Losing it is silent — the index is
   created, on PRIMARY, and every count except one still matches.
2. **A clustered columnstore index is not skipped.** The obvious "no key columns, skip it" loop
   turns a columnstore table into a heap and reports success.
3. **``plan`` defaults to true**, and no phase planner writes.
4. **Change tracking is planned before the modules phase**, because a procedure calling
   ``CHANGETABLE()`` fails to *create* without it.
5. **A pattern that matches nothing is reported**, rather than silently excluding nothing.
"""

from __future__ import annotations

import datetime

import pytest

from db_ops.common import schema_catalog, schema_copy
from db_ops.lib import mssql_ddl, name_filter


# ------------------------------------------------------------------------------- name_filter


def test_exclude_wins_over_include_and_an_empty_include_means_everything() -> None:
    names = ["config", "configStaging", "dataLock", "CalendarDay"]
    assert name_filter.select(names) == names
    assert name_filter.select(names, exclude=["*Staging", "dataLock"]) == ["config", "CalendarDay"]
    assert name_filter.select(names, include=["config*"], exclude=["*Staging"]) == ["config"]


def test_matching_is_case_insensitive_because_sql_server_is() -> None:
    assert name_filter.matches("EmployeeProfileDay", "employeeprofile*")
    assert name_filter.matches_any("USP_Run", ["usp_*"])
    #: An empty pattern list excludes nothing. The `all()` spelling of the same loop excludes
    #: everything, which is the difference between "copy the schema" and "copy nothing".
    assert not name_filter.matches_any("anything", [])


def test_a_pattern_that_matches_nothing_is_reported() -> None:
    """A typo in an exclude list is invisible in the result: the table is simply there."""
    names = ["EmployeeResultStaging", "config"]
    assert name_filter.unused_patterns(names, ["*Stagng", "*Staging"]) == ["*Stagng"]
    assert name_filter.unused_patterns(names, ["*Staging"]) == []


# --------------------------------------------------------------------------------- mssql_ddl


def test_an_identifier_with_a_closing_bracket_is_escaped_not_truncated() -> None:
    assert mssql_ddl.quote("od]d") == "[od]]d]"
    assert mssql_ddl.quote_string("O'Brien") == "N'O''Brien'"


@pytest.mark.parametrize("column, expected", [
    ({"type_name": "nvarchar", "max_length": 200}, "nvarchar(100)"),
    ({"type_name": "varchar", "max_length": 50}, "varchar(50)"),
    ({"type_name": "nvarchar", "max_length": -1}, "nvarchar(MAX)"),
    ({"type_name": "decimal", "precision": 18, "scale": 4}, "decimal(18,4)"),
    ({"type_name": "datetime2", "scale": 3}, "datetime2(3)"),
    ({"type_name": "bigint"}, "bigint"),
])
def test_max_length_is_bytes_so_the_unicode_types_are_halved(column, expected) -> None:
    """`nvarchar(100)` stores as `max_length = 200`. Writing that out doubles every such column."""
    assert mssql_ddl.render_type(column) == expected


def test_a_computed_column_is_its_expression_and_never_its_type() -> None:
    """Rendering the type produces a column that no longer tracks the expression, silently."""
    line = mssql_ddl.render_column({
        "name": "TotalMinutes", "type_name": "int", "is_nullable": True,
        "computed_definition": "([EndMinute]-[StartMinute])", "is_persisted": True})
    assert line == "[TotalMinutes] AS ([EndMinute]-[StartMinute]) PERSISTED"


def test_a_create_table_carries_identity_default_collation_and_its_partition_scheme() -> None:
    statement = mssql_ddl.render_create_table(
        "sched", "Result",
        columns=[
            {"name": "Id", "type_name": "bigint", "is_identity": True, "seed_value": 1,
             "increment_value": 1, "is_nullable": False},
            {"name": "PayrollPeriodKey", "type_name": "char", "max_length": 8,
             "is_nullable": False, "collation_name": "SQL_Latin1_General_CP1_CI_AS"},
            {"name": "Status", "type_name": "tinyint", "is_nullable": False,
             "default_name": "DF_Result_Status", "default_definition": "((0))"},
        ],
        keys=[{"name": "PK_Result", "type": "PK", "type_desc": "CLUSTERED",
               "columns": [{"name": "Id"}, {"name": "PayrollPeriodKey"}]}],
        storage={"data_space": "ps_Result", "data_space_type_desc": "PARTITION_SCHEME",
                 "partition_column": "PayrollPeriodKey"})

    assert "[Id] bigint IDENTITY(1,1) NOT NULL" in statement
    assert "COLLATE SQL_Latin1_General_CP1_CI_AS" in statement
    assert "CONSTRAINT [DF_Result_Status] DEFAULT ((0))" in statement
    assert "CONSTRAINT [PK_Result] PRIMARY KEY CLUSTERED ([Id], [PayrollPeriodKey])" in statement
    # The clause whose absence shipped 0 of 32 partitioned indexes and failed nothing.
    assert statement.rstrip().endswith("ON [ps_Result]([PayrollPeriodKey]);")


def test_an_index_on_a_partition_scheme_keeps_its_on_clause() -> None:
    statement = mssql_ddl.render_create_index("sched", "Result", {
        "name": "IX_Result_Employee", "is_unique": False, "type_desc": "NONCLUSTERED",
        "filter_definition": "([Status]=(1))",
        "key_columns": [{"name": "EmployeeId"}, {"name": "WorkDate", "is_descending_key": True}],
        "included_columns": [{"name": "Minutes"}],
        "storage": {"data_space": "ps_Result", "data_space_type_desc": "PARTITION_SCHEME",
                    "partition_column": "PayrollPeriodKey"}})
    assert statement == (
        "CREATE NONCLUSTERED INDEX [IX_Result_Employee] ON [sched].[Result] "
        "([EmployeeId], [WorkDate] DESC) INCLUDE ([Minutes]) WHERE ([Status]=(1)) "
        "ON [ps_Result]([PayrollPeriodKey]);")


def test_a_clustered_columnstore_index_has_no_key_columns_and_is_still_scripted() -> None:
    """`if not key_columns: continue` turns a columnstore table into a heap, silently."""
    statement = mssql_ddl.render_create_index("sched", "Fact", {
        "name": "CCI_Fact", "type_desc": "CLUSTERED COLUMNSTORE", "key_columns": []})
    assert statement == "CREATE CLUSTERED COLUMNSTORE INDEX [CCI_Fact] ON [sched].[Fact];"


def test_a_partition_scheme_named_without_its_column_is_refused_not_rendered_wrong() -> None:
    with pytest.raises(ValueError, match="partitioning column"):
        mssql_ddl.render_storage({"data_space": "ps_X",
                                  "data_space_type_desc": "PARTITION_SCHEME"})


def test_partition_boundaries_are_written_as_literals_in_the_source_order() -> None:
    statement = mssql_ddl.render_partition_function(
        "pf_Month", parameter_type="char(8)", boundary_right=True,
        values=["20260101", "20260201"])
    assert statement == ("CREATE PARTITION FUNCTION [pf_Month] (char(8)) "
                        "AS RANGE RIGHT FOR VALUES (N'20260101', N'20260201');")
    assert " RANGE LEFT " in mssql_ddl.render_partition_function(
        "pf", parameter_type="int", boundary_right=False, values=[1])


def test_a_datetime_boundary_is_written_in_a_form_no_dateformat_setting_can_reinterpret() -> None:
    assert mssql_ddl.literal(datetime.datetime(2026, 3, 1, 0, 0)) == "'2026-03-01T00:00:00.000'"
    assert mssql_ddl.literal(None) == "NULL"
    assert mssql_ddl.literal(b"\x0a\xff") == "0x0AFF"


def test_a_foreign_key_carries_its_referential_actions() -> None:
    statement = mssql_ddl.render_foreign_key("sched", {
        "name": "FK_Detail_Header", "table": "Detail", "columns": ["HeaderId"],
        "referenced_schema": "sched", "referenced_table": "Header",
        "referenced_columns": ["Id"], "delete_action": "CASCADE",
        "update_action": "NO_ACTION"})
    assert statement == ("ALTER TABLE [sched].[Detail] WITH CHECK ADD CONSTRAINT "
                        "[FK_Detail_Header] FOREIGN KEY ([HeaderId]) REFERENCES [sched].[Header] "
                        "([Id]) ON DELETE CASCADE;")


@pytest.mark.parametrize("definition, expected_head", [
    ("CREATE PROCEDURE [sched].[usp_X] AS SELECT 1", "CREATE OR ALTER PROCEDURE"),
    ("CREATE   PROCEDURE [sched].[usp_X] AS SELECT 1", "CREATE OR ALTER PROCEDURE"),
    ("CREATE PROC [sched].[usp_X] AS SELECT 1", "CREATE OR ALTER PROCEDURE"),
    ("create view [sched].[vw_X] AS SELECT 1", "CREATE OR ALTER VIEW"),
    ("CREATE FUNCTION [sched].[fn_X]() RETURNS int AS BEGIN RETURN 1 END",
     "CREATE OR ALTER FUNCTION"),
])
def test_a_module_is_rewritten_to_create_or_alter_so_the_phase_can_be_re_run(
        definition, expected_head) -> None:
    assert mssql_ddl.as_create_or_alter(definition).startswith(expected_head)


def test_the_rewrite_leaves_a_leading_comment_and_the_body_untouched() -> None:
    body = ("-- owner: payroll, do not create a PROCEDURE by hand\nSET ANSI_NULLS ON\n"
            "CREATE PROCEDURE [sched].[usp_X]\nAS\n-- creates a view? no.\nSELECT 1")
    rewritten = mssql_ddl.as_create_or_alter(body)
    assert rewritten.startswith("-- owner: payroll, do not create a PROCEDURE by hand")
    assert "CREATE OR ALTER PROCEDURE [sched].[usp_X]" in rewritten
    assert rewritten.count("CREATE OR ALTER") == 1


def test_a_guard_embeds_the_statement_with_its_quotes_doubled() -> None:
    guarded = mssql_ddl.guarded(
        mssql_ddl.guard_object_absent("sched", "T", object_type="U"),
        "ALTER TABLE [sched].[T] ADD CONSTRAINT [CK] CHECK ([Code] <> 'X');")
    assert guarded.startswith("IF OBJECT_ID(N'sched.T', N'U') IS NULL")
    assert "[Code] <> ''X''" in guarded


# ------------------------------------------------------------------------------- the request


def _minimal(**overrides):
    payload = {"source": {"target": "SRC", "database": "A", "schema": "sched"},
               "dest": {"target": "DST", "database": "B", "schema": "sched"}}
    payload.update(overrides)
    return payload


def test_plan_is_the_default_so_a_request_that_forgets_to_say_gets_the_harmless_one() -> None:
    assert schema_copy.SchemaCopyRequest.from_json(_minimal()).plan_only is True
    assert schema_copy.SchemaCopyRequest.from_json(_minimal(plan=False)).plan_only is False
    assert schema_copy.SchemaCopyRequest.from_json(_minimal(mode="apply")).plan_only is False
    assert schema_copy.SchemaCopyRequest.from_json(_minimal(mode="report")).plan_only is True


def test_phases_run_in_dependency_order_however_they_were_listed() -> None:
    parsed = schema_copy.SchemaCopyRequest.from_json(
        _minimal(phases=["modules", "tables", "indexes"]))
    assert parsed.phases == ("tables", "indexes", "modules")


def test_an_unknown_phase_names_the_known_ones() -> None:
    with pytest.raises(schema_copy.SchemaCopyError, match="unknown phase"):
        schema_copy.SchemaCopyRequest.from_json(_minimal(phases=["triggers"]))


def test_copying_a_schema_onto_itself_is_refused() -> None:
    payload = {"source": {"target": "SRC", "database": "A", "schema": "sched"},
               "dest": {"target": "src", "database": "a", "schema": "SCHED"}}
    with pytest.raises(schema_copy.SchemaCopyError, match="same schema"):
        schema_copy.SchemaCopyRequest.from_json(payload)


def test_the_lock_resource_is_the_destination_schema_not_the_process() -> None:
    parsed = schema_copy.SchemaCopyRequest.from_json(_minimal())
    assert parsed.resource_name() == "db_ops:schema_copy:B.sched"


# ------------------------------------------------------------------- planning, with a fake cursor


class FakeCursor:
    """A cursor that answers catalogue queries from a table of substring matches.

    Matching on a fragment of each query rather than the whole text: the assertions here are about
    what the planner *does with* the answers, and pinning the SQL byte for byte would make every
    query edit look like a behaviour change.
    """

    def __init__(self, answers: list[tuple[str, list[dict]]]) -> None:
        self.answers = answers
        self.description = None
        self._rows: list[dict] = []
        self.executed: list[str] = []

    def execute(self, sql, *params):
        self.executed.append(sql)
        for fragment, rows in self.answers:
            if all(part in sql for part in fragment.split("|")):
                self._rows = rows
                self.description = ([(name,) for name in rows[0]] if rows else None)
                return self
        self._rows = []
        self.description = None
        return self

    def fetchall(self):
        return [tuple(row.values()) for row in self._rows]

    def nextset(self):
        return False


def _cursor_for_one_partitioned_table() -> FakeCursor:
    return FakeCursor([
        ("FROM sys.tables t|ORDER BY t.name", [{"name": "Result"}, {"name": "ResultStaging"}]),
        ("FROM sys.columns c|sys.default_constraints", [
            {"name": "Id", "type_name": "bigint", "max_length": 8, "precision": 19, "scale": 0,
             "is_nullable": False, "is_identity": True, "is_rowguidcol": False,
             "collation_name": None, "computed_definition": None, "is_persisted": None,
             "seed_value": 1, "increment_value": 1, "default_name": None,
             "default_definition": None},
            {"name": "PayrollPeriodKey", "type_name": "char", "max_length": 8, "precision": 0,
             "scale": 0, "is_nullable": False, "is_identity": False, "is_rowguidcol": False,
             "collation_name": None, "computed_definition": None, "is_persisted": None,
             "seed_value": None, "increment_value": None, "default_name": None,
             "default_definition": None}]),
        ("FROM sys.index_columns ic|sys.columns c", [
            {"index_id": 1, "name": "Id", "is_descending_key": False, "is_included_column": False,
             "key_ordinal": 1, "index_column_id": 1, "partition_ordinal": 0},
            {"index_id": 1, "name": "PayrollPeriodKey", "is_descending_key": False,
             "is_included_column": False, "key_ordinal": 2, "index_column_id": 2,
             "partition_ordinal": 1},
            {"index_id": 2, "name": "PayrollPeriodKey", "is_descending_key": False,
             "is_included_column": False, "key_ordinal": 1, "index_column_id": 1,
             "partition_ordinal": 1}]),
        ("FROM sys.key_constraints k", [
            {"name": "PK_Result", "type": "PK", "index_id": 1, "type_desc": "CLUSTERED"}]),
        ("FROM sys.indexes i|is_primary_key = 0", [
            {"name": "IX_Result_Period", "index_id": 2, "is_unique": False,
             "type_desc": "NONCLUSTERED", "filter_definition": None,
             "data_space": "ps_Result", "data_space_type_desc": "PARTITION_SCHEME"}]),
        ("FROM sys.indexes i|index_id IN (0, 1)", [
            {"index_id": 1, "data_space": "ps_Result",
             "data_space_type_desc": "PARTITION_SCHEME"}]),
        ("ps.name AS scheme", [{"scheme": "ps_Result", "function": "pf_Result"}]),
        ("pf.name AS function_name", [
            {"name": "ps_Result", "function_name": "pf_Result"}]),
        ("FROM sys.partition_functions pf|sys.partition_parameters", [
            {"name": "pf_Result", "boundary_value_on_right": True, "fanout": 2,
             "type_name": "char", "max_length": 8, "precision": 0, "scale": 0}]),
        ("FROM sys.partition_range_values", [{"value": "20260101"}]),
        ("FROM sys.destination_data_spaces", [{"name": "PRIMARY"}, {"name": "PRIMARY"}]),
        ("FROM sys.change_tracking_databases|retention_period", [
            {"retention_period": 5, "retention_period_units_desc": "DAYS",
             "is_auto_cleanup_on": True}]),
        ("FROM sys.change_tracking_tables ct|ORDER BY t.name", [
            {"name": "Result", "is_track_columns_updated_on": False}]),
        ("FROM sys.sql_modules m", [
            {"name": "vw_Result", "type": "V ", "type_desc": "VIEW",
             "definition": "CREATE VIEW [sched].[vw_Result] AS SELECT 1 AS x"},
            {"name": "usp_Run", "type": "P ", "type_desc": "SQL_STORED_PROCEDURE",
             "definition": "CREATE PROCEDURE [sched].[usp_Run] AS SELECT 1"}]),
    ])


def _plan(**overrides):
    cursor = _cursor_for_one_partitioned_table()
    request = schema_copy.SchemaCopyRequest.from_json(_minimal(**overrides))
    return schema_copy.build_plan(cursor, request), cursor


def test_a_plan_carries_the_partition_objects_before_the_tables_that_need_them() -> None:
    plan, _ = _plan(exclude_tables=["*Staging"])
    phases = [step["phase"] for step in plan["steps"]]
    assert phases.index("partitions") < phases.index("tables")
    assert phases.index("tables") < phases.index("indexes")
    # Change tracking before modules: a procedure calling CHANGETABLE() fails to CREATE without it.
    assert phases.index("change_tracking_tables") < phases.index("modules")
    # Foreign keys are last, so the data load order cannot violate one.
    assert phases == sorted(phases, key=schema_copy.PHASES.index)


def test_the_partition_function_and_scheme_are_planned_from_the_source_not_hardcoded() -> None:
    plan, _ = _plan(exclude_tables=["*Staging"])
    partitions = [step for step in plan["steps"] if step["phase"] == "partitions"]
    assert [step["name"] for step in partitions] == ["pf_Result", "ps_Result"]
    assert "AS RANGE RIGHT FOR VALUES (N'20260101')" in partitions[0]["sql"]
    assert "ALL TO ([PRIMARY])" in partitions[1]["sql"]


def test_every_partitioned_index_keeps_its_on_clause_and_is_counted() -> None:
    """The number that read 0 of 32 after a copy whose every other count matched."""
    plan, _ = _plan(exclude_tables=["*Staging"])
    index = next(step for step in plan["steps"] if step["phase"] == "indexes")
    assert index["sql"].endswith("ON [ps_Result]([PayrollPeriodKey]);")
    assert plan["counts"]["partitioned_indexes"] == 1


def test_partition_boundaries_can_be_stated_when_a_procedure_owns_them() -> None:
    """Pre-creating a month makes the procedure that owns the boundaries throw on that month."""
    plan, _ = _plan(exclude_tables=["*Staging"], partition_boundaries=["00000000"])
    function = next(step for step in plan["steps"] if step["name"] == "pf_Result")
    assert "FOR VALUES (N'00000000')" in function["sql"]
    assert function["detail"]["boundaries_on_source"] == 1


def test_an_excluded_table_is_reported_as_skipped_not_quietly_dropped() -> None:
    plan, _ = _plan(exclude_tables=["*Staging"])
    assert plan["tables"] == ["Result"]
    assert plan["skipped_tables"] == ["ResultStaging"]


def test_a_pattern_that_matched_nothing_reaches_the_plan_as_a_note() -> None:
    plan, _ = _plan(exclude_tables=["*Stagng"])
    assert any("'*Stagng' matched nothing" in note for note in plan["notes"])


def test_modules_are_rewritten_and_ordered_views_before_procedures() -> None:
    plan, _ = _plan(exclude_tables=["*Staging"])
    modules = [step for step in plan["steps"] if step["phase"] == "modules"]
    assert [step["name"] for step in modules] == ["vw_Result", "usp_Run"]
    assert all(step["sql"].startswith("CREATE OR ALTER") for step in modules)


def test_an_excluded_module_never_reaches_the_plan() -> None:
    plan, _ = _plan(exclude_tables=["*Staging"], exclude_modules=["usp_*"])
    assert plan["modules"] == ["vw_Result"]
    assert plan["skipped_modules"] == ["usp_Run"]


def test_naming_a_subset_of_phases_plans_only_those() -> None:
    plan, _ = _plan(exclude_tables=["*Staging"], phases=["modules"])
    assert {step["phase"] for step in plan["steps"]} == {"modules"}


def test_planning_writes_nothing_to_the_source() -> None:
    """A plan is safe to hand to anyone, which is only true while it issues no DDL."""
    _, cursor = _plan(exclude_tables=["*Staging"])
    forbidden = ("CREATE ", "ALTER ", "DROP ", "INSERT ", "UPDATE ", "DELETE ", "EXEC ")
    offenders = [sql for sql in cursor.executed
                 if any(word in sql.upper() for word in forbidden)]
    assert not offenders, f"the planner issued a write: {offenders[:1]}"


def test_a_filegroup_can_be_remapped_when_the_destination_does_not_have_it() -> None:
    cursor = _cursor_for_one_partitioned_table()
    cursor.answers.insert(0, ("FROM sys.indexes i|index_id IN (0, 1)", [
        {"index_id": 1, "data_space": "FG_ARCHIVE", "data_space_type_desc": "ROWS_FILEGROUP"}]))
    request = schema_copy.SchemaCopyRequest.from_json(
        _minimal(exclude_tables=["*Staging"], map_filegroups={"FG_ARCHIVE": "PRIMARY"}))
    plan = schema_copy.build_plan(cursor, request)
    table = next(step for step in plan["steps"] if step["phase"] == "tables")
    assert table["sql"].rstrip().endswith("ON [PRIMARY];")


# ----------------------------------------------------------------------------- the destination


class IdentityCursor(FakeCursor):
    def __init__(self, server: str, database: str) -> None:
        super().__init__([("SERVERPROPERTY('ServerName')", [
            {"server_name": server, "database_name": database, "edition": "Standard",
             "product_version": "15.0.4123"}])])


def test_the_wrong_database_is_refused() -> None:
    with pytest.raises(schema_copy.SchemaCopyError, match="expected 'APPDB_Prod'"):
        schema_copy.assert_destination(IdentityCursor("SRV\\A", "APPDB_Uat"),
                                       database="APPDB_Prod")


def test_the_right_database_on_the_wrong_instance_is_refused() -> None:
    """One estate had `APPDB_Prod` on both the UAT and the production instance."""
    with pytest.raises(schema_copy.SchemaCopyError, match="does not match"):
        schema_copy.assert_destination(IdentityCursor("UATHOST\\APPINST", "APPDB_Prod"),
                                       database="APPDB_Prod", instance="PRODHOST\\APPINST")


def test_the_right_instance_passes_and_no_assertion_is_warned_about() -> None:
    ok = schema_copy.assert_destination(IdentityCursor("PRODHOST\\APPINST", "APPDB_Prod"),
                                        database="APPDB_Prod", instance="PRODHOST\\APPINST")
    assert "warning" not in ok
    loose = schema_copy.assert_destination(IdentityCursor("ANY\\THING", "APPDB_Prod"),
                                           database="APPDB_Prod")
    assert "several instances" in loose["warning"]


# ------------------------------------------------------------------------------------- locking


class LockCursor(FakeCursor):
    def __init__(self, result: int) -> None:
        super().__init__([("sp_getapplock", [{"result": result}])])
        self.released = False

    def execute(self, sql, *params):
        if "sp_releaseapplock" in sql:
            self.released = True
            self.description = None
            self._rows = []
            self.executed.append(sql)
            return self
        return super().execute(sql, *params)


def test_the_lock_is_released_even_when_the_body_raises() -> None:
    cursor = LockCursor(0)
    with pytest.raises(RuntimeError, match="boom"):
        with schema_copy.application_lock(cursor, "res"):
            raise RuntimeError("boom")
    assert cursor.released


def test_a_second_copy_against_one_destination_is_refused_by_name() -> None:
    """Two appliers reached one destination at once; the phase guards are read-then-write."""
    with pytest.raises(schema_copy.SchemaCopyError, match="another copy is running"):
        with schema_copy.application_lock(LockCursor(-1), "res", timeout_seconds=1):
            pass


def test_the_lock_is_session_owned_because_the_copy_runs_in_autocommit() -> None:
    cursor = LockCursor(0)
    with schema_copy.application_lock(cursor, "res"):
        pass
    assert "@LockOwner = 'Session'" in cursor.executed[0]


# --------------------------------------------------------------- what will not be carried


class ProbeCursor(FakeCursor):
    """Answers one probe with rows, one with an error, and everything else with nothing."""

    def execute(self, sql, *params):
        self.executed.append(sql)
        if "sys.masked_columns" in sql:
            raise RuntimeError("Invalid object name 'sys.masked_columns'.")
        if "temporal_type" in sql:
            self._rows = [{"name": "AuditTrail"}]
            self.description = [("name",)]
            return self
        self._rows = []
        self.description = None
        return self


def test_a_probe_that_cannot_run_is_unknown_and_never_reported_as_none() -> None:
    """"SQL Server 2012 has no sys.masked_columns" is not "there are no masked columns"."""
    findings = {item["feature"]: item
                for item in schema_catalog.unsupported_features(ProbeCursor([]), "sched")}
    assert findings["temporal_tables"]["status"] == "present"
    assert findings["temporal_tables"]["objects"] == ["AuditTrail"]
    assert findings["masked_columns"]["status"] == "unknown"
    # Features with nothing to report are omitted, so the list is only what needs acting on.
    assert "sequences" not in findings


def test_the_fingerprint_marks_an_expected_difference_apart_from_a_real_one() -> None:
    comparison = {item["count"]: item for item in schema_catalog.compare_fingerprints(
        {"tables": 60, "indexes": 41}, {"tables": 54, "indexes": 9},
        expect_equal=["indexes"])}
    assert comparison["indexes"]["status"] == "MISMATCH"
    # 6 tables were deliberately excluded; reporting that as a fault trains the reader to ignore
    # the report.
    assert comparison["tables"]["status"] == "differs"
