"""An Oracle or PostgreSQL task's parameters are bound where its script says ``:name``.

A task's parameters were T-SQL ``DECLARE @name ... = ?`` lines put in front of every batch - right
for SQL Server and read by nothing else. On a direct Oracle connection those lines reached Oracle as
they were, so an Oracle task with parameters failed at its first run; and a PostgreSQL task could
declare none, refused at registration (0.23.0 section 1.55, carried to 0.24.0). Registration could
not simply refuse the Oracle one: the same command is right on an 8i bridge target, where a
parameter is a SQL*Plus ``&name`` substitution.

So on those two engines the script says which it is. ``:name`` is bound by name - ``run-sql``'s
``named_params``, written as the driver's own placeholder, so a value from a chat is never SQL text.
On Oracle ``&name`` is the SQL*Plus substitution it is on the bridge, so one command means the same
on both transports. And a declared parameter no script says is refused, because it would take a
value and bind it to nothing.
"""

from __future__ import annotations

import json

import pytest

from db_ops.common import sql_run, sql_task_admin
from db_ops.lib import sql_text
from db_ops.lib.sql_text import SqlParameterError
from db_ops.sql_tasks import runner
from db_ops.sql_tasks.runner import SqlCommand, SqlTarget


# --------------------------------------------------------------------------- #
# Which :name is a parameter
# --------------------------------------------------------------------------- #
def test_a_colon_in_a_string_a_comment_a_cast_or_an_assignment_is_not_a_parameter():
    sql = ("select :a, x::int, 'HH24:MI :b', \"col:c\" from t -- :d\n"
           "/* :e */ where arr[1:n] = :F and v := 1")
    assert sql_text.named_placeholders(sql, "postgresql") == {"a", "f"}


def test_an_oracle_q_quote_keeps_its_colons_to_itself():
    assert sql_text.named_placeholders("select q'[it's :x]', nq'{:y}', :z from dual", "oracle") == {"z"}


def test_a_postgresql_dollar_body_is_text_not_a_statement_with_parameters():
    assert sql_text.named_placeholders("DO $$ begin perform :a; end $$", "postgresql") == set()


def test_sqlplus_substitutions_are_found_inside_quotes_as_sqlplus_finds_them():
    assert sql_text.sqlplus_substitution_names(
        "DEFINE JOB_NO = 'x'\nselect * from t where j = '&JOB_NO' and k = &&kk.") == {"job_no", "kk"}


# --------------------------------------------------------------------------- #
# How a :name is written for the driver
# --------------------------------------------------------------------------- #
def test_postgresql_numbers_each_name_once_so_the_server_can_type_it():
    """``$1 IS NULL OR day = $2`` would leave ``$1`` with no type to infer; ``$1 ... = $1`` has one."""
    statement, values = sql_text.bind_named_values(
        "select * from t where :d is null or day = :d and n = :n",
        {"d": "2026-09-01", "n": 5}, db_type="postgresql", style="format")
    assert statement == "select * from t where $1 is null or day = $1 and n = $2"
    assert values == ["2026-09-01", 5]


def test_oracle_numbers_each_occurrence_and_matches_names_ignoring_case():
    statement, values = sql_text.bind_named_values(
        "select * from t where a = :job_no or b = :JOB_NO", {"JOB_NO": "A1"},
        db_type="oracle", style="numeric")
    assert statement == "select * from t where a = :1 or b = :2"
    assert values == ["A1", "A1"]


def test_a_percent_is_doubled_for_pg8000_only_when_something_is_bound():
    """pg8000 reads ``%`` outside a literal in its ``format`` style - but only on the extended
    protocol, which it uses only when values are passed. A statement with nothing to bind goes as
    it is, where a doubled ``%`` would reach the server doubled."""
    convert_paramstyle = pytest.importorskip("pg8000.dbapi").convert_paramstyle

    statement, values = sql_text.bind_named_values(
        "select a % 2, 'x%' from t where b = :b", {"b": 1}, db_type="postgresql", style="format")
    assert statement == "select a %% 2, 'x%' from t where b = $1"
    # What pg8000 itself sends to the server: one `%` each, the literal untouched.
    assert convert_paramstyle("format", statement, tuple(values))[0] == \
        "select a % 2, 'x%' from t where b = $1"

    assert sql_text.bind_named_values("select a % 2", {"b": 1}, db_type="postgresql",
                                      style="format") == ("select a % 2", [])


def test_a_question_mark_cannot_share_a_statement_with_named_values_under_qmark():
    """pg8000's ``qmark`` has no escape: a jsonb ``?`` would take the next value."""
    with pytest.raises(SqlParameterError, match=r"'\?' outside a string"):
        sql_text.bind_named_values("select data ? 'k', :a", {"a": 1}, db_type="postgresql",
                                   style="qmark")
    assert sql_text.bind_named_values("select '?', :a", {"a": 1}, db_type="postgresql",
                                      style="qmark") == ("select '?', $1", [1])


def test_a_name_that_was_not_given_is_left_for_the_engine():
    """``:new`` in a trigger is Oracle's own; a real missing value is then the driver's error."""
    assert sql_text.bind_named_values("select :new, :a from dual", {"a": 1}, db_type="oracle",
                                      style="numeric") == ("select :new, :1 from dual", [1])


def test_named_values_are_identifiers_with_scalar_values():
    assert sql_text.check_named_values({"Job_No": "x", "n": 1, "flag": None}) == {
        "job_no": "x", "n": 1, "flag": None}
    for bad, words in (({"a;b": 1}, "Invalid parameter name"), ({"a": [1]}, "is list"),
                       ({"a": 1, "A": 2}, "twice"), (["a"], "must be an object")):
        with pytest.raises(SqlParameterError, match=words):
            sql_text.check_named_values(bad)


# --------------------------------------------------------------------------- #
# run-sql
# --------------------------------------------------------------------------- #
class _Cursor:
    def __init__(self):
        self.executed: list[tuple] = []
        self.description = [("n",)]
        self.rowcount = 1

    def execute(self, statement, params=None):
        self.executed.append((statement, params))

    def fetchmany(self, _size):
        return []


def test_run_sql_binds_named_values_statement_by_statement_on_postgresql():
    """Each statement gets the values it says, so the script is still split - positional
    ``params`` have to keep it whole."""
    cursor = _Cursor()
    sql_run.execute_capture(cursor, "select :a; select :b, :a;", db_type="postgresql",
                            named_params={"a": "x", "b": "y"}, capture_all=True, max_result_sets=0)
    assert cursor.executed == [("select $1", ("x",)), ("select $1, $2", ("y", "x"))]


def test_run_sql_binds_named_values_on_oracle_and_keeps_a_blocks_semicolon():
    cursor = _Cursor()
    sql_run.execute_capture(cursor, "BEGIN DBMS_SESSION.SLEEP(:s); END;\nGO\nSELECT :s FROM dual;",
                            db_type="oracle", named_params={"s": 1}, capture_all=True,
                            max_result_sets=0)
    assert cursor.executed == [("BEGIN DBMS_SESSION.SLEEP(:1); END;", (1,)),
                               ("SELECT :1 FROM dual", (1,))]


def test_a_named_value_no_statement_says_is_refused_before_anything_runs():
    cursor = _Cursor()
    with pytest.raises(sql_run.SqlRunError, match="never says :jobno"):
        sql_run.execute_capture(cursor, "select :job_no from dual", db_type="oracle",
                                named_params={"jobno": "A1"})
    assert cursor.executed == []


def test_named_values_are_refused_on_sql_server_which_reads_at_names():
    with pytest.raises(sql_run.SqlRunError, match="On SQL Server the SQL says @name"):
        sql_run.check_named_params("select @a", "sqlserver", {"a": 1})


def test_named_and_positional_values_are_not_mixed_in_one_request():
    with pytest.raises(sql_run.SqlRunError, match="Pass one or the other"):
        sql_run.SqlRunRequest.from_json({**_STATED, "sql": "select :a", "params": [1],
                                         "named_params": {"a": 1}})


#: The login a caller states - run-sql reads no configuration since 0.24.0 (rules R09).
_STATED = {"target": "ACME-LAB", "connection": {"db_type": "postgresql", "host": "192.0.2.10",
                                                "port": 5432, "username": "u", "password": "x"}}


def _resolved(db_type, sql_access=None):
    from db_ops.lib.target_profile import TargetProfile

    return {"server_id": "ACME-LAB", "db_type": db_type, "database_name": "labtest",
            "credential_name": "c", "username": "u", "password": "x", "credential_role": "",
            "ip": "192.0.2.10", "port": 5432, "service_name": "", "instance_name": "",
            "sql_access": sql_access or {}, "profile": TargetProfile(db_type=db_type),
            "tool": {"tool": "pg8000", "chosen_by": "rule", "reason": "test"}}


def test_run_sql_refuses_an_unsaid_name_before_it_connects(monkeypatch):
    """A connection opened only to refuse the request is a login the server's audit shows for
    nothing."""
    monkeypatch.setattr(sql_run, "resolve_connection_spec", lambda _spec, **_kw: _resolved("postgresql"))

    def no_connect(*_args, **_kwargs):
        raise AssertionError("connected")

    monkeypatch.setattr(sql_run, "connect_target", no_connect)
    with pytest.raises(sql_run.SqlRunError, match="never says :b"):
        sql_run.run_sql({**_STATED, "sql": "select :a", "named_params": {"a": 1, "b": 2}})


def test_the_legacy_bridge_refuses_named_values_rather_than_dropping_them(monkeypatch):
    monkeypatch.setattr(sql_run, "resolve_connection_spec",
                        lambda _spec, **_kw: _resolved("oracle", {"method": "subprocess"}))
    with pytest.raises(sql_run.SqlRunError, match="binds no parameters"):
        sql_run.run_sql({**_STATED, "sql": "select :a from dual", "named_params": {"a": 1}})


# --------------------------------------------------------------------------- #
# The runner
# --------------------------------------------------------------------------- #
def _command(db_type="oracle", parameters=({"name": "job_no", "type": "varchar(50)", "required": True},)):
    return SqlCommand(sql_id=55, sql_code=f"{db_type.upper()}-055-BY_JOB", sql_name="by job",
                      db_type=db_type, script_type="single", script_path=None, script_paths=(),
                      script_files=(), active=True, parameters=tuple(parameters))


def _target(db_type="oracle", sql_access=None):
    from db_ops.lib.time_window import TimeWindow

    return SqlTarget(sql_id=55, target_no=1, server_id="ACME-LAB", db_type=db_type,
                     service_name="FREEPDB1", instance_name="", credential_name="c",
                     time_window=TimeWindow(), active=True, database_name="labtest",
                     output_format="none", sql_access=sql_access or {})


def test_a_colon_name_is_bound_and_an_ampersand_name_is_substituted_on_a_direct_oracle():
    command = _command(parameters=({"name": "job_no"}, {"name": "top"}))
    defines, named = runner.named_parameter_values(
        command, {"job_no": "O'Brien", "top": "5"},
        "select * from jobs where job_no = :job_no fetch first &top rows only", db_type="oracle")
    # The quote is data in a bind; the substitution is text and checked as on the bridge.
    assert named == {"job_no": "O'Brien"}
    assert defines == {"top": "5"}


def test_a_quote_is_refused_in_a_substitution_as_on_the_bridge():
    with pytest.raises(sql_run.SqlRunError, match="not allowed"):
        runner.named_parameter_values(_command(), {"job_no": "x' or '1'='1"},
                                      "select '&JOB_NO' from dual", db_type="oracle")


def test_a_default_is_bound_nothing_binds_null_and_required_is_refused():
    command = _command(db_type="postgresql", parameters=(
        {"name": "d", "default": "2026-09-01"}, {"name": "n"}, {"name": "must", "required": True}))
    sql = "select :d, :n, :must"
    assert runner.named_parameter_values(command, {"must": "1"}, sql, db_type="postgresql") == (
        {}, {"d": "2026-09-01", "n": None, "must": "1"})
    with pytest.raises(SqlParameterError, match="requires parameter must"):
        runner.named_parameter_values(command, {}, sql, db_type="postgresql")


def test_a_name_the_command_does_not_declare_is_refused():
    with pytest.raises(SqlParameterError, match="does not declare parameter"):
        runner.named_parameter_values(_command(), {"jobno": "A1"}, "select :job_no from dual",
                                      db_type="oracle")


def test_a_file_that_does_not_say_a_parameter_is_not_sent_it():
    """A folder task's other files may say it; ``run-sql`` refuses a value no statement reads."""
    assert runner.named_parameter_values(_command(), {"job_no": "A1"}, "select 1 from dual",
                                         db_type="oracle") == ({}, {})


@pytest.mark.parametrize("db_type,sql", [("oracle", "select * from jobs where job_no = :job_no"),
                                         ("postgresql", "select * from jobs where job_no = :job_no")])
def test_a_direct_target_sends_named_values_and_no_declare(monkeypatch, db_type, sql):
    """The failure of 1.55: the request carried ``DECLARE @job_no varchar(50) = ?;``."""
    seen = {}

    def fake_run(command, request, **_kwargs):
        seen.update(request)
        return True, {"ok": True, "affected_rows": 0, "result_sets": []}, ""

    monkeypatch.setattr(runner.common_cli, "run_allowing_failure", fake_run)
    runner.execute_on_target(command=_command(db_type), target=_target(db_type), database={},
                             credential={}, password="x", sql_text=sql,
                             parameter_values={"job_no": "A1"})
    assert seen["named_params"] == {"job_no": "A1"}
    assert "prelude" not in seen and "params" not in seen and "define" not in seen


def test_sql_server_keeps_its_declare_prelude(monkeypatch):
    seen = {}
    monkeypatch.setattr(runner.common_cli, "run_allowing_failure",
                        lambda command, request, **_k: (seen.update(request) or True,
                                                        {"result_sets": []}, ""))
    runner.execute_on_target(command=_command("sqlserver", ({"name": "spid", "type": "int"},)),
                             target=_target("sqlserver"), database={}, credential={}, password="x",
                             sql_text="select @spid", parameter_values={"spid": "5"})
    assert seen["prelude"] == "DECLARE @spid int = ?;\n" and seen["params"] == ["5"]
    assert "named_params" not in seen


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #
@pytest.fixture
def estate(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "sql_commands.json").write_text('{"sql_commands": []}', encoding="utf-8")
    return tmp_path


def _add(estate, **request):
    return sql_task_admin.add_sql_command({"display_name": "by job", **request},
                                          data_dir=estate / "data", tool_root=estate)


def test_a_postgresql_task_with_a_parameter_its_script_says_registers(estate):
    _add(estate, db_type="postgresql", sql_text="select * from jobs where job_no = :job_no;",
         parameters=[{"name": "job_no", "type": "nvarchar(50)"}])
    saved = json.loads((estate / "data" / "sql_commands.json").read_text(encoding="utf-8"))
    assert saved["sql_commands"][0]["parameters"] == [{"name": "job_no", "type": "nvarchar(50)"}]


def test_an_oracle_task_written_for_sqlplus_registers(estate):
    _add(estate, db_type="oracle", sql_text="DEFINE JOB_NO = 'x'\nSELECT '&JOB_NO' FROM DUAL",
         parameters=[{"name": "job_no", "type": "varchar(50)"}])


def test_a_parameter_no_script_says_is_refused_and_no_file_is_written(estate):
    with pytest.raises(sql_task_admin.SqlTaskAdminError, match="no script of this task says"):
        _add(estate, db_type="postgresql", sql_text="select * from jobs where job_no = :jobno;",
             parameters=[{"name": "job_no"}])
    assert not (estate / "assets").exists()
    assert json.loads((estate / "data" / "sql_commands.json").read_text(encoding="utf-8")) == {
        "sql_commands": []}


def test_a_parameter_in_a_script_file_is_read_from_the_file(estate):
    script = estate / "assets" / "tasks" / "oracle" / "by_job.sql"
    script.parent.mkdir(parents=True)
    script.write_text("select * from jobs where job_no = :job_no", encoding="utf-8")
    _add(estate, db_type="oracle", script_path="assets/tasks/oracle/by_job.sql",
         parameters=[{"name": "job_no"}])
    with pytest.raises(sql_task_admin.SqlTaskAdminError, match="top"):
        _add(estate, db_type="oracle", script_path="assets/tasks/oracle/by_job.sql",
             parameters=[{"name": "job_no"}, {"name": "top"}], sql_id=2)


def test_sql_server_is_not_held_to_it(estate):
    """There the prelude declares every parameter, and a script may use one only in dynamic SQL."""
    _add(estate, db_type="sqlserver", sql_text="select 1", parameters=[{"name": "spid", "type": "int"}])
