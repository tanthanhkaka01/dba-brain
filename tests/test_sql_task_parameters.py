"""A SQL task that takes an argument, without letting the argument reach the SQL text.

Until 2026-08-12 a task ran a fixed script: `/spbot_run_sql_task 12` could not say *which* SPID,
*which* database, *which* date. The obvious implementation — paste the value into the script — is
also the one that turns a Telegram message into arbitrary SQL, and the caller is a chat.

So the value is a **bind parameter**. `build_parameter_prelude` writes `DECLARE @name <type> = ?`
in front of every batch and hands the value to the driver; the script author writes ordinary T-SQL
against `@name`. Only two things reach the SQL text — the name and the type — and both are checked
against a pattern and an allow-list first, because neither can be bound.

The prelude is repeated per batch on purpose: a T-SQL variable does not survive a `GO`.
"""

from __future__ import annotations

import pytest

from db_ops.common.sql_execution import (
    SQL_PARAMETER_TYPES,
    SqlParameterError,
    build_parameter_prelude,
    execute_cursor_batches,
)
from db_ops.sql_tasks import runner


class _Cursor:
    def __init__(self):
        self.calls = []
        self.description = None
        self.rowcount = 0

    def execute(self, sql, *params):
        self.calls.append((sql, params))

    def fetchmany(self, n):
        return []

    def nextset(self):
        return False


class _Conn:
    def cursor(self):
        return _Cursor()

    def commit(self):
        pass


# --------------------------------------------------------------------------------------
# The prelude
# --------------------------------------------------------------------------------------

def test_the_value_is_bound_and_never_written_into_the_sql():
    """The whole reason this exists. A value from a chat message must not be SQL."""
    prelude, bound = build_parameter_prelude(
        [{"name": "spid", "type": "int"}], {"spid": "505; DROP TABLE x"})

    assert prelude == "DECLARE @spid int = ?;\n"
    assert "DROP" not in prelude
    assert bound == ["505; DROP TABLE x"]


def test_a_parameter_name_is_held_to_an_identifier():
    """The name is not bindable — it goes into the DECLARE — so it is validated instead."""
    with pytest.raises(SqlParameterError, match="Invalid parameter name"):
        build_parameter_prelude([{"name": "spid; DROP TABLE x", "type": "int"}], {})


def test_a_type_outside_the_allow_list_is_refused():
    """Neither is the type. An allow-list rather than a pattern, because a pattern that admits
    `sysname` admits whatever else looks like a word."""
    with pytest.raises(SqlParameterError, match="allowed"):
        build_parameter_prelude([{"name": "x", "type": "sysname"}], {})


def test_a_type_with_a_malformed_length_is_refused():
    with pytest.raises(SqlParameterError):
        build_parameter_prelude([{"name": "x", "type": "varchar(10); DROP TABLE y"}], {})


@pytest.mark.parametrize("declared", ["int", "nvarchar(128)", "nvarchar(max)", "decimal(18,2)", "date"])
def test_the_ordinary_types_are_accepted(declared):
    prelude, _ = build_parameter_prelude([{"name": "x", "type": declared}], {"x": "1"})

    assert prelude == f"DECLARE @x {declared} = ?;\n"
    assert declared.split("(")[0] in SQL_PARAMETER_TYPES


def test_a_missing_value_falls_back_to_the_declared_default():
    _prelude, bound = build_parameter_prelude(
        [{"name": "db", "type": "nvarchar(128)", "default": "SALESDB"}], {})

    assert bound == ["SALESDB"]


def test_a_required_parameter_with_no_value_is_refused_before_connecting():
    with pytest.raises(SqlParameterError, match="Missing required parameter: spid"):
        build_parameter_prelude([{"name": "spid", "type": "int", "required": True}], {})


def test_an_optional_parameter_with_no_value_binds_null():
    """NULL, not the empty string: `WHERE @db IS NULL OR name = @db` is how a script says
    "all of them", and '' would match nothing instead."""
    _prelude, bound = build_parameter_prelude([{"name": "db", "type": "nvarchar(128)"}], {})

    assert bound == [None]


def test_a_task_with_no_parameters_produces_no_prelude():
    """Every task that existed before this feature must run byte-identically."""
    assert build_parameter_prelude([], {}) == ("", [])
    assert build_parameter_prelude(None, {}) == ("", [])


# --------------------------------------------------------------------------------------
# Reaching the driver
# --------------------------------------------------------------------------------------

def test_every_batch_gets_the_declaration_because_a_variable_dies_at_go():
    conn, cursor = _Conn(), _Cursor()

    execute_cursor_batches(conn, cursor, ["SELECT @spid;", "SELECT 2;"], commit=False,
                           prelude="DECLARE @spid int = ?;\n", params=[505])

    assert len(cursor.calls) == 2
    for sql, params in cursor.calls:
        assert sql.startswith("DECLARE @spid int = ?;\n")
        assert params == (505,)


def test_without_parameters_the_batch_is_passed_through_untouched():
    conn, cursor = _Conn(), _Cursor()

    execute_cursor_batches(conn, cursor, ["SELECT 1;"], commit=False)

    assert cursor.calls == [("SELECT 1;", ())]


# --------------------------------------------------------------------------------------
# The CLI form
# --------------------------------------------------------------------------------------

def test_param_pairs_split_on_the_first_equals_only():
    """A value may contain '=' — a LIKE pattern, a date expression. Splitting on all of them
    would silently truncate it."""
    assert runner.parse_parameter_arguments(["spid=505", "filter=a=b"]) == {
        "spid": "505", "filter": "a=b"}


def test_a_param_without_an_equals_is_kept_as_a_positional_value():
    """It used to be refused. But the Telegram prompt names the parameter it wants and then the
    person answers with the value — being told "--param expects NAME=VALUE" for that answer is
    the bot being obtuse about something it just said. The name is filled in by
    `bind_parameter_values` once the task that declares it is loaded; a value that cannot be
    bound is still an error there."""
    parsed = runner.parse_parameter_arguments(["505"])

    assert parsed == {runner.POSITIONAL_PARAMETERS_KEY: '["505"]'}


def test_no_params_is_an_empty_mapping():
    assert runner.parse_parameter_arguments([]) == {}
    assert runner.parse_parameter_arguments(None) == {}


def test_a_command_loads_its_declared_parameters(tmp_path):
    import json

    path = tmp_path / "sql_commands.json"
    path.write_text(json.dumps({"sql_commands": [{
        "sql_id": 1, "sql_code": "T-001", "sql_name": "t", "db_type": "sqlserver",
        "script_type": "single", "script_path": "x.sql", "active": True,
        "parameters": [{"name": "spid", "type": "int", "required": True}],
    }]}), encoding="utf-8")

    commands = runner.load_sql_commands(path)

    assert commands[1].parameters == ({"name": "spid", "type": "int", "required": True},)


def test_a_command_without_parameters_loads_as_empty(tmp_path):
    import json

    path = tmp_path / "sql_commands.json"
    path.write_text(json.dumps({"sql_commands": [{
        "sql_id": 1, "sql_code": "T-001", "sql_name": "t", "db_type": "sqlserver",
        "script_type": "single", "script_path": "x.sql", "active": True,
    }]}), encoding="utf-8")

    assert runner.load_sql_commands(path)[1].parameters == ()
