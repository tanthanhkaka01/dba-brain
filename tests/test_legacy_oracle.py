"""Reaching an Oracle 8i host, which db_ops cannot connect to at all.

No modern driver speaks to 8.1.7, so the SQL is handed to a separate Win32 Python 2.7 tool —
either as a child process or over the HTTP bridge that runs it on another host. What these
tests protect is everything that decides *what the tool is asked to do*, because a wrong
answer there is silent: a credential that is quietly not the one the target declares, a
CURRENT_SCHEMA that makes a query read a different schema's tables and still succeed, a
password that ends up somewhere it can be read. The tool itself is exercised against a real
8i database, not here.
"""

import json
from pathlib import Path

import pytest

from db_ops.common import oracle_bridge, sql_run
from db_ops.lib.target_profile import TargetProfile
from db_ops.metrics import executor
from db_ops.metrics.models import MetricTarget
from db_ops.common.sql_execution import SqlParameterError
from db_ops.sql_tasks import runner
from db_ops.sql_tasks.runner import SqlCommand, SqlTarget


# --------------------------------------------------------------------------- #
# The connect string is assembled, never stored
# --------------------------------------------------------------------------- #
def test_the_connect_string_is_built_from_the_targets_own_credential():
    connect = oracle_bridge.resolve_connect(
        {"method": "subprocess"},
        {"ORACLE_2_236_SYS": "s3cret"},
        credential={"username": "sys", "password_ref": "ORACLE_2_236_SYS", "role": "SYSDBA"},
        host="192.0.2.236",
        port=1521,
        service_name="LEGACYDB",
    )
    assert connect == "sys/s3cret@192.0.2.236:1521/LEGACYDB"


def test_a_connect_ref_still_wins_for_a_login_that_lives_nowhere_else():
    connect = oracle_bridge.resolve_connect(
        {"method": "api", "connect_ref": "ORACLE8I_CONNECT"},
        {"ORACLE8I_CONNECT": "app/pw@10.0.0.1:1521/LEGACYDB"},
        credential={"username": "sys", "password": "ignored"},
        host="192.0.2.236",
        service_name="LEGACYDB",
    )
    assert connect == "app/pw@10.0.0.1:1521/LEGACYDB"


def test_a_connect_ref_that_is_not_in_the_store_is_an_error_not_a_silent_fallback():
    """It names a specific login. Quietly connecting as the instance default instead would run
    the SQL as somebody else — possibly a DBA — and report success."""
    with pytest.raises(oracle_bridge.LegacyOracleError, match="connect_ref"):
        oracle_bridge.resolve_connect(
            {"method": "api", "connect_ref": "GONE"},
            {},
            credential={"username": "sys", "password": "x"},
            host="192.0.2.236",
            service_name="LEGACYDB",
        )


def test_a_target_with_no_credential_says_so_instead_of_building_half_a_connect_string():
    with pytest.raises(oracle_bridge.LegacyOracleError, match="No credential"):
        oracle_bridge.resolve_connect(
            {"method": "subprocess"}, {}, host="192.0.2.236", service_name="LEGACYDB",
        )


def test_a_missing_service_name_is_named_because_oracle_connects_by_service():
    with pytest.raises(oracle_bridge.LegacyOracleError, match="service_name"):
        oracle_bridge.build_connect_string(
            username="sys", password="x", host="192.0.2.236", service_name="",
        )


# --------------------------------------------------------------------------- #
# Mode and schema
# --------------------------------------------------------------------------- #
def test_a_sysdba_credential_connects_as_sysdba_without_config_saying_so():
    assert oracle_bridge.connect_mode({}, {"username": "sys", "role": "SYSDBA"}) == "sysdba"
    assert oracle_bridge.connect_mode({}, {"username": "app", "role": ""}) == "normal"


def test_sql_access_mode_overrides_the_credentials_role():
    assert oracle_bridge.connect_mode({"mode": "normal"}, {"role": "SYSDBA"}) == "normal"


def test_a_schema_becomes_an_alter_session_so_a_dba_login_can_run_an_apps_script():
    assert oracle_bridge.schema_prelude("LTR") == ["alter session set current_schema = LTR"]
    assert oracle_bridge.schema_prelude("") == []


def test_a_schema_name_that_is_not_an_identifier_is_refused():
    """It is concatenated into ALTER SESSION — a statement that takes no bind variable."""
    with pytest.raises(oracle_bridge.LegacyOracleError, match="identifier"):
        oracle_bridge.schema_prelude("LTR; drop table x")


# --------------------------------------------------------------------------- #
# What the tool is sent
# --------------------------------------------------------------------------- #
def test_a_trailing_semicolon_is_stripped_because_cx_oracle_rejects_it():
    assert oracle_bridge.prepare_sql("select 1 from dual;") == "select 1 from dual"


def test_non_ascii_is_dropped_rather_than_failing_the_whole_run():
    """The Python 2.7 tool encodes the statement as ASCII; one em-dash in a comment used to
    come back as an opaque HTTP 500."""
    assert oracle_bridge.prepare_sql("select 1 from dual -- — note") == (
        "select 1 from dual --  note"
    )


def test_the_subprocess_transport_sends_the_password_on_stdin_never_in_the_arguments(monkeypatch):
    """An argument is readable in the process table by anyone on the host for as long as the
    query runs."""
    captured = {}

    class _Completed:
        returncode = 0
        stdout = json.dumps({"ok": True, "columns": ["N"], "rows": [[1]], "row_count": 1,
                             "truncated": False, "db_version": "8.1.7.0.0"})
        stderr = ""

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["input"] = kwargs["input"]
        return _Completed()

    monkeypatch.setattr(oracle_bridge.subprocess, "run", fake_run)

    result = oracle_bridge.run_query(
        sql="select 1 from dual",
        sql_access={"method": "subprocess", "launcher": ["docker", "run", "--rm", "-i", "legacy"]},
        secrets={},
        credential={"username": "sys", "password": "s3cret", "role": "SYSDBA"},
        host="192.0.2.236", port=1521, service_name="LEGACYDB", schema="LTR", now=0.0,
    )

    assert result["rows"] == [[1]]
    assert result["transport"] == "subprocess"
    assert "s3cret" not in " ".join(captured["argv"])
    request = json.loads(captured["input"])
    assert request["connect"] == "sys/s3cret@192.0.2.236:1521/LEGACYDB"
    assert request["mode"] == "sysdba"
    assert request["prelude"] == ["alter session set current_schema = LTR"]
    # A launcher states the whole command; the tool still reads its request on stdin.
    assert captured["argv"] == ["docker", "run", "--rm", "-i", "legacy", "--request", "-"]


def test_a_metrics_row_cap_reaches_the_legacy_tool_like_it_reaches_every_other_engine(monkeypatch):
    """A metric declares `max_rows`; on the direct path the cursor enforces it. The bridge branch
    used to send no limit, which the tool reads as "no cap" — so the one target kind that cannot
    be connected to directly was the one that would fetch a whole result set into memory and ship
    it over HTTP."""
    captured = {}

    def fake_run_bridge_query(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(executor.oracle_bridge, "run_bridge_query", fake_run_bridge_query)

    executor.execute_metric_sql(
        target=MetricTarget(
            target_id="ACME-192-0-2-236/oracle/LEGACYDB",
            server_id="ACME-192-0-2-236",
            ip="192.0.2.236",
            db_type="oracle",
            db_name="LEGACYDB",
            credential_name="oracle_2.236_LEGACYDB_sys",
            port=1521,
            sql_access={"method": "api", "bridge_url": "http://127.0.0.1:8765/query"},
            service_name="LEGACYDB",
            credential={"username": "sys", "password": "x"},
        ),
        sql_text="select 1 from dual",
        secrets={},
        max_rows=25,
    )

    assert captured["limit"] == 25


def test_a_bridge_that_does_not_answer_names_the_url_and_the_way_out(monkeypatch):
    def fake_urlopen(*_args, **_kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(oracle_bridge.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(oracle_bridge.LegacyOracleError, match="did not answer"):
        oracle_bridge.run_query(
            sql="select 1 from dual",
            sql_access={"method": "api", "bridge_url": "http://127.0.0.1:8765/query",
                        "secret_ref": "TOK"},
            secrets={"TOK": "shared"},
            credential={"username": "sys", "password": "x"},
            host="192.0.2.236", service_name="LEGACYDB", now=0.0,
        )


def test_a_token_changes_every_time_so_a_captured_one_cannot_be_replayed():
    first = oracle_bridge.encrypt_token("sys/x@h:1521/LEGACYDB", "sysdba", "shared", now=1_000_000.0)
    second = oracle_bridge.encrypt_token("sys/x@h:1521/LEGACYDB", "sysdba", "shared", now=1_000_000.0)
    assert first != second
    assert "sys/x@h:1521/LEGACYDB" not in first


# --------------------------------------------------------------------------- #
# sql_access, and how run_sql routes on it
# --------------------------------------------------------------------------- #
def test_a_target_that_says_nothing_connects_to_its_database_as_before():
    assert oracle_bridge.normalize_sql_access(None) == {"method": "direct"}
    assert oracle_bridge.is_legacy({"method": "direct"}) is False


def test_the_api_method_without_a_bridge_url_is_refused_at_config_time():
    with pytest.raises(oracle_bridge.LegacyOracleError, match="bridge_url"):
        oracle_bridge.normalize_sql_access({"method": "api"}, label="ACME-192-0-2-236")


def test_an_unknown_method_names_the_instance_it_came_from():
    with pytest.raises(oracle_bridge.LegacyOracleError, match="ACME-192-0-2-236"):
        oracle_bridge.normalize_sql_access({"method": "ssh"}, label="ACME-192-0-2-236")


def test_run_sql_routes_a_legacy_target_through_the_tool_and_reports_the_normal_shape(monkeypatch):
    """An export must not have to know which transport answered it."""
    monkeypatch.setattr(
        sql_run, "resolve_sqlserver_target",
        lambda spec, data_dir=None, database="", credential_name="", sql_access=None,
        profile=None, driver="", oracle_client_mode="": {
            "server_id": "ACME-192-0-2-236", "db_type": "oracle", "database_name": "LEGACYDB",
            "credential_name": "oracle_2.236_LEGACYDB_sys", "username": "sys", "password": "x",
            "credential_role": "SYSDBA", "ip": "192.0.2.236", "port": 1521,
            "service_name": "LEGACYDB", "instance_name": "LEGACYDB",
            "sql_access": {"method": "subprocess"},
            "profile": TargetProfile(db_type="oracle", major_version=8),
            "tool": {"tool": "subprocess", "chosen_by": "config", "reason": "legacy bridge"},
        })
    monkeypatch.setattr(sql_run.data_sources, "load_secret_text", lambda _dir: {})
    monkeypatch.setattr(
        oracle_bridge, "run_query",
        lambda **kwargs: {"columns": ["N"], "rows": [[1], [2]], "row_count": 2,
                          "truncated": False, "db_version": "8.1.7.0.0",
                          "transport": "subprocess"})

    result = sql_run.run_sql({"target": "ACME-192-0-2-236", "sql": "select 1 from dual"})

    assert result["ok"] is True
    assert result["columns"] == ["N"] and result["row_count"] == 2
    assert result["server_id"] == "ACME-192-0-2-236"
    assert result["db_version"] == "8.1.7.0.0"
    # There is no connection to commit or roll back, and the fields say so rather than lying.
    assert result["committed"] is False and result["affected_rows"] == 0


def test_more_rows_than_the_cap_are_reported_as_truncated(monkeypatch):
    """The tool is asked for one row more than the cap, so 'there were more' is a fact rather
    than a guess about a result set that happens to be exactly max_rows long."""
    asked = {}

    monkeypatch.setattr(
        sql_run, "resolve_sqlserver_target",
        lambda spec, data_dir=None, database="", credential_name="", sql_access=None,
        profile=None, driver="", oracle_client_mode="": {
            "server_id": "ACME-192-0-2-236", "db_type": "oracle", "database_name": "LEGACYDB",
            "credential_name": "c", "username": "sys", "password": "x", "credential_role": "",
            "ip": "192.0.2.236", "port": 1521, "service_name": "LEGACYDB",
            "sql_access": {"method": "subprocess"},
            "profile": TargetProfile(db_type="oracle", major_version=8),
            "tool": {"tool": "subprocess", "chosen_by": "config", "reason": "legacy bridge"},
        })
    monkeypatch.setattr(sql_run.data_sources, "load_secret_text", lambda _dir: {})

    def fake_run_query(**kwargs):
        asked.update(kwargs)
        return {"columns": ["N"], "rows": [[1], [2], [3]], "row_count": 3, "truncated": True,
                "db_version": "8.1.7.0.0", "transport": "subprocess"}

    monkeypatch.setattr(oracle_bridge, "run_query", fake_run_query)

    result = sql_run.run_sql(
        {"target": "ACME-192-0-2-236", "sql": "select 1 from dual", "max_rows": 2},
    )

    assert asked["limit"] == 3
    assert result["truncated"] is True
    assert result["rows"] == [[1], [2]] and result["row_count"] == 2


def test_the_request_may_override_the_instances_transport(monkeypatch):
    """What lets one run be pointed at a bridge on this machine without editing the deployed
    inventory — the same escape hatch run-cmd gives for cmd_access."""
    seen = {}
    monkeypatch.setattr(
        sql_run.target_resolve, "resolve_target_instance",
        lambda spec, data_dir=None: {
            "server_id": "ACME-192-0-2-236", "db_type": "oracle", "ip": "192.0.2.236",
            "port": 1521, "service_name": "LEGACYDB", "default_credential_name": "c",
            "sql_access": {"method": "api", "bridge_url": "http://192.0.2.246:8765/query"},
        })
    monkeypatch.setattr(
        sql_run, "_find_sqlserver_credential",
        lambda instance, data_dir=None, credential_name="", db_type="": {
            "credential_name": "c", "username": "sys", "role": "SYSDBA"})
    monkeypatch.setattr(sql_run.data_sources, "load_secret_text", lambda _dir: {})
    monkeypatch.setattr(sql_run.sql_execution, "resolve_password", lambda *_args: "x")

    def fake_run_query(**kwargs):
        seen.update(kwargs)
        return {"columns": [], "rows": [], "row_count": 0, "truncated": False,
                "db_version": "", "transport": "api"}

    monkeypatch.setattr(oracle_bridge, "run_query", fake_run_query)

    sql_run.run_sql({
        "target": "ACME-192-0-2-236", "sql": "select 1 from dual",
        "sql_access": {"method": "api", "bridge_url": "http://127.0.0.1:8765/query"},
    })

    assert seen["sql_access"]["bridge_url"] == "http://127.0.0.1:8765/query"


# --------------------------------------------------------------------------- #
# SQL*Plus DEFINE / &substitution
# --------------------------------------------------------------------------- #
def test_a_saved_sqlplus_script_runs_with_its_own_define_values():
    """`DEFINE`/`&VAR` are client syntax SQL*Plus resolves before the server sees the statement;
    passing them to a driver fails on the literal ampersand."""
    expanded = sql_run.expand_sqlplus_defines(
        "DEFINE JOB_NO = 'AA2503/00818';\nSELECT * FROM CT_CUT WHERE JOB_NO = '&JOB_NO'",
    )
    assert expanded == "SELECT * FROM CT_CUT WHERE JOB_NO = 'AA2503/00818'"


def test_the_request_overrides_the_scripts_define_so_the_stored_script_stays_the_shipped_one():
    expanded = sql_run.expand_sqlplus_defines(
        "DEFINE JOB_NO = 'AA2503/00818'\nSELECT '&JOB_NO' FROM DUAL",
        {"job_no": "AA2510/00001"},
    )
    assert expanded == "SELECT 'AA2510/00001' FROM DUAL"


def test_the_double_ampersand_form_leaves_no_stray_ampersand_behind():
    assert sql_run.expand_sqlplus_defines(
        "DEFINE X = 7\nSELECT &&X, &X FROM DUAL",
    ) == "SELECT 7, 7 FROM DUAL"


def test_a_variable_nobody_defined_is_left_alone_so_the_error_names_it():
    """Blanking it would produce a silently different query that still runs."""
    assert sql_run.expand_sqlplus_defines(
        "DEFINE A = 1\nSELECT &A, &B FROM DUAL",
    ) == "SELECT 1, &B FROM DUAL"


def test_a_script_with_no_defines_is_untouched():
    sql = "SELECT 1 FROM DUAL WHERE x = 'a & b'"
    assert sql_run.expand_sqlplus_defines(sql) == sql


# --------------------------------------------------------------------------- #
# As a scheduled/forced SQL task
# --------------------------------------------------------------------------- #
def _command(**overrides):
    base = dict(
        sql_id=19, sql_code="ORACLE-019-GET_JOB_DETAILS", sql_name="get_job_details",
        db_type="oracle", script_type="single", script_path=None, script_paths=(),
        script_files=(), active=True,
        parameters=({"name": "job_no", "type": "varchar(50)", "required": True},),
    )
    base.update(overrides)
    return SqlCommand(**base)


def _target(**overrides):
    from db_ops.lib.time_window import TimeWindow

    base = dict(
        sql_id=19, target_no=1, server_id="ACME-192-0-2-236", db_type="oracle",
        service_name="LEGACYDB", instance_name="", credential_name="oracle_2.236_LEGACYDB_sys",
        time_window=TimeWindow(), active=True, database_name="LTR",
        output_format="xlsx", sql_access={"method": "api", "bridge_url": "http://h:8765/query"},
    )
    base.update(overrides)
    return SqlTarget(**base)


def test_a_task_parameter_becomes_the_scripts_substitution_value():
    assert runner.legacy_define_values(_command(), {"job_no": "AA2608/01902"}) == {
        "job_no": "AA2608/01902"}


def test_a_parameter_name_the_task_does_not_declare_is_refused():
    """It would match no `&VAR` and be dropped, so `jobno=` instead of `job_no=` would quietly
    export whatever job number the archived script was last saved with."""
    with pytest.raises(SqlParameterError, match="does not declare"):
        runner.legacy_define_values(_command(), {"jobno": "AA2608/01902"})


def test_a_required_parameter_with_no_value_is_refused_rather_than_defaulted():
    with pytest.raises(SqlParameterError, match="requires parameter job_no"):
        runner.legacy_define_values(_command(), {})


def test_an_optional_parameter_left_out_falls_back_to_the_scripts_own_define():
    command = _command(parameters=({"name": "job_no", "type": "varchar(50)"},))
    assert runner.legacy_define_values(command, {}) == {}


def test_a_declared_default_is_used_when_the_caller_says_nothing():
    command = _command(parameters=({"name": "job_no", "default": "AA2503/00007"},))
    assert runner.legacy_define_values(command, {}) == {"job_no": "AA2503/00007"}


def test_a_parameter_value_that_could_change_the_statement_is_refused():
    """Substitution is textual, and the value arrives from a Telegram message."""
    with pytest.raises(sql_run.SqlRunError, match="not allowed"):
        runner.legacy_define_values(_command(), {"job_no": "x' or '1'='1"})


def test_a_legacy_task_sends_substitutions_instead_of_bind_parameters(monkeypatch):
    """An 8i target cannot bind: the tool runs one statement with no bind list. So the task's
    parameter values travel as SQL*Plus substitutions in the request's `define`, and the request
    must carry **no** `params` or `prelude` — `run-sql` refuses those on this transport rather
    than running the statement with the values missing.

    Retargeted 2026-08-16, when `runner.execute_legacy_oracle` was deleted: it opened nothing, but
    it chose the transport and reshaped the bridge's answer, and `run-sql` does both now. What is
    still this app's — and still asserted — is what a parameter *means* here.
    """
    seen = {}

    def fake_run(command, request, **_kwargs):
        assert command == "run-sql"
        seen.update(request)
        return True, {"ok": True, "affected_rows": 0, "result_sets_truncated": False,
                      "result_sets": [{"columns": ["JOB_NO"], "rows": [["AA2608/01902"]],
                                       "row_count": 1, "truncated": False}]}, ""

    monkeypatch.setattr(runner.common_cli, "run_allowing_failure", fake_run)

    result = runner.execute_on_target(
        command=_command(),
        target=_target(),
        database={"ip": "192.0.2.236", "port": 1521, "service_name": "LEGACYDB"},
        credential={"username": "sys", "role": "SYSDBA"},
        password="s3cret",
        sql_text="\n".join(["DEFINE JOB_NO = 'AA2503/00818'",
                            "SELECT '&JOB_NO' AS JOB_NO FROM DUAL"]),
        parameter_values={"job_no": "AA2608/01902"},
        secrets={"TOK": "shared"},
    )

    # Substituted, not bound — and the bind fields are absent, not empty.
    assert seen["define"] == {"job_no": "AA2608/01902"}
    assert "params" not in seen and "prelude" not in seen
    # The target's own transport rides along, so `run-sql` routes it to the legacy tool.
    assert seen["sql_access"]["method"] == "api"
    # `database_name` means SCHEMA on this transport; it travels in the same field either way.
    assert seen["database"] == "LTR"

    # The store row, the Telegram table and the export all read this shape.
    assert result == {
        "row_count": 1,
        "result_sets": [{"columns": ["JOB_NO"], "rows": [["AA2608/01902"]], "truncated": False}],
        "truncated": False,
    }


def test_a_task_inherits_its_servers_transport_from_db_instances(tmp_path):
    """The transport belongs to the server: an 8i host is unreachable by every task alike."""
    (tmp_path / "db_instances.json").write_text(json.dumps({"db_instances": [{
        "server_id": "ACME-192-0-2-236", "db_type": "oracle", "ip": "192.0.2.236",
        "default_credential_name": "oracle_2.236_LEGACYDB_sys",
        "sql_access": {"method": "api", "bridge_url": "http://h:8765/query"},
    }]}), encoding="utf-8")
    (tmp_path / "sql_targets.json").write_text(json.dumps({"sql_targets": [{
        "sql_id": 19, "target_no": 1, "server_id": "ACME-192-0-2-236", "db_type": "oracle",
        "service_name": "LEGACYDB", "credential_name": "oracle_2.236_LEGACYDB_sys", "active": True,
    }]}), encoding="utf-8")

    targets = runner.load_sql_targets(tmp_path / "sql_targets.json")

    assert targets[0].sql_access["method"] == "api"


# --------------------------------------------------------------------------- #
# /spbot_run_sql_task has to ask for the parameters the chosen task requires
# --------------------------------------------------------------------------- #
def _run_sql_task_command(monkeypatch, declared_names):
    """The real command config, with the sql_tasks CLI faked to declare `declared_names`.

    The fake is at the *process boundary* — what the CLI printed — not at
    `sql_task_parameter_names`. Stubbing that function is what let the first version of this fix
    ship broken: it called a helper that did not exist, the NameError was swallowed, and every
    test still passed because none of them ran the real lookup.
    """
    from db_ops.telegram import command_processor

    class _Completed:
        returncode = 0
        stderr = ""
        stdout = json.dumps({
            "ok": True, "command_count": 1, "target_count": 1, "hidden_count": 0,
            "sql_tasks": [{
                "sql_id": 19, "sql_code": "ORACLE-019-GET_JOB_DETAILS",
                "parameter_names": list(declared_names),
                "required_parameter_names": list(declared_names),
                "parameters": [{"name": name, "required": True} for name in declared_names],
                "targets": [],
            }],
        })

    class _Empty(_Completed):
        stdout = json.dumps({"ok": True, "command_count": 0, "target_count": 0,
                             "hidden_count": 0, "sql_tasks": []})

    def fake_run(argv, **_kwargs):
        return _Completed() if "19" in argv else _Empty()

    monkeypatch.setattr(command_processor.subprocess, "run", fake_run)
    return [
        {"name": "sql_id", "source": "arg", "position": 1, "required": True,
         "prompt_text": "Which sql_id?"},
        {"name": "task_params", "source": "arg", "position": 2, "required": False,
         "consume_rest": True,
         "prompt_when": {"condition": "sql_task_has_parameters", "parameter": "sql_id"},
         "prompt_text": "This task takes: {sql_task_parameters}. Send NAME=VALUE pairs."},
    ]


def test_a_task_that_declares_parameters_is_asked_about_them(monkeypatch):
    """The failure this exists for: task 19 requires job_no, the optional `task_params` was never
    prompted, so the run went out with params=[] and failed telling the operator to pass a
    --param they were never asked for."""
    from db_ops.telegram import command_processor

    parameters = _run_sql_task_command(monkeypatch, ["job_no"])
    assert command_processor.prompt_condition_holds(parameters[1], parameters, ["19"]) is True


def test_a_task_with_no_parameters_is_not_asked_and_runs_as_before(monkeypatch):
    from db_ops.telegram import command_processor

    parameters = _run_sql_task_command(monkeypatch, ["job_no"])
    assert command_processor.prompt_condition_holds(parameters[1], parameters, ["8"]) is False


def test_the_prompt_names_the_parameters_instead_of_leaving_the_operator_to_guess(monkeypatch):
    from db_ops.telegram import command_processor

    parameters = _run_sql_task_command(monkeypatch, ["job_no", "ou_code"])
    text = command_processor.render_prompt_text(parameters[1], parameters, ["19"])
    assert "job_no, ou_code" in text


def test_the_prompt_flow_stops_on_the_conditional_parameter(monkeypatch):
    from db_ops.telegram import command_processor
    from db_ops.telegram.command_processor import SupportCommand

    parameters = _run_sql_task_command(monkeypatch, ["job_no"])
    command = SupportCommand(
        command_id=20, command_text="spbot_run_sql_task", command_type=10, reply_default=0,
        reply_text="", is_group=1, is_private=1, need_file=0, action_type="cli_execute",
        action_config={"parameters": parameters},
    )
    missing = command_processor.first_missing_prompt_parameter(command, ["19"])
    assert missing is not None and missing["name"] == "task_params"


def test_a_dash_answers_the_prompt_with_no_values():
    """Telegram will not send an empty message, so the prompt needs a way to say 'none' — and
    "-" is the sentinel the conversation flow already fills in elsewhere."""
    assert runner.parse_parameter_arguments(None, "-") == {}
    assert runner.parse_parameter_arguments(["-"], "") == {}
    assert runner.parse_parameter_arguments(None, "job_no=AA2608/01902") == {
        "job_no": "AA2608/01902"}


# --------------------------------------------------------------------------- #
# The sql_tasks app owns "what tasks are there"
# --------------------------------------------------------------------------- #
def _write_task_config(tmp_path, *, parameters=None, active=True, target_active=True):
    (tmp_path / "db_instances.json").write_text(json.dumps({"db_instances": [{
        "server_id": "ACME-192-0-2-236", "db_type": "oracle", "ip": "192.0.2.236",
        "default_credential_name": "oracle_2.236_LEGACYDB_sys",
        "sql_access": {"method": "api", "bridge_url": "http://h:8765/query"},
    }]}), encoding="utf-8")
    (tmp_path / "sql_commands.json").write_text(json.dumps({"sql_commands": [{
        "sql_id": 19, "sql_code": "ORACLE-019-GET_JOB_DETAILS", "sql_name": "get_job_details",
        "db_type": "oracle", "script_type": "single",
        "script_path": "assets/tasks/oracle/019.sql", "active": active,
        "parameters": parameters if parameters is not None else [
            {"name": "job_no", "type": "varchar(50)", "required": True}],
    }]}), encoding="utf-8")
    (tmp_path / "sql_targets.json").write_text(json.dumps({"sql_targets": [{
        "sql_id": 19, "target_no": 1, "server_id": "ACME-192-0-2-236", "db_type": "oracle",
        "service_name": "LEGACYDB", "database_name": "LTR",
        "credential_name": "oracle_2.236_LEGACYDB_sys", "active": target_active,
        "output": {"format": "xlsx"},
    }]}), encoding="utf-8")
    return tmp_path


def test_the_listing_reports_the_parameters_a_task_declares(tmp_path):
    """The fact the bot needs to know whether to ask the operator anything."""
    payload = runner.collect_sql_tasks(_write_task_config(tmp_path))

    assert payload["ok"] is True and payload["command_count"] == 1
    task = payload["sql_tasks"][0]
    assert task["parameter_names"] == ["job_no"]
    assert task["required_parameter_names"] == ["job_no"]
    assert task["targets"][0]["sql_access_method"] == "api"
    assert task["targets"][0]["database_name"] == "LTR"


def test_a_task_whose_targets_are_all_inactive_is_hidden_not_listed(tmp_path):
    """It runs nowhere, so listing it as a task that does nothing is a lie about the estate."""
    payload = runner.collect_sql_tasks(_write_task_config(tmp_path, target_active=False))

    assert payload["command_count"] == 0 and payload["hidden_count"] == 1


def test_asking_for_one_task_by_id_returns_it_even_when_it_is_inactive(tmp_path):
    payload = runner.collect_sql_tasks(_write_task_config(tmp_path, active=False), sql_id=19)

    assert payload["command_count"] == 1
    assert payload["sql_tasks"][0]["runnable"] is False


def test_a_task_with_no_parameters_reports_none(tmp_path):
    payload = runner.collect_sql_tasks(_write_task_config(tmp_path, parameters=[]))

    assert payload["sql_tasks"][0]["parameter_names"] == []


def test_the_bot_reads_the_parameters_out_of_the_cli_answer(monkeypatch):
    """Exercises the real lookup end to end (only the subprocess is faked). The first version of
    this call chain used a helper that did not exist; every test passed because they stubbed the
    function being tested rather than the process boundary."""
    from db_ops.telegram import command_processor

    captured = {}

    class _Completed:
        returncode = 0
        stderr = ""
        stdout = json.dumps({"ok": True, "sql_tasks": [
            {"sql_id": 19, "parameter_names": ["job_no"]}]})

    def fake_run(argv, **_kwargs):
        captured["argv"] = argv
        return _Completed()

    monkeypatch.setattr(command_processor.subprocess, "run", fake_run)

    assert command_processor.sql_task_parameter_names("19") == ["job_no"]
    assert captured["argv"][1:] == [
        "-m", "db_ops.sql_tasks.cli", "list-tasks", "--sql-id", "19"]


def test_a_cli_that_fails_does_not_wedge_the_conversation(monkeypatch):
    """No prompt is the behaviour every task had before parameters existed; a broken listing must
    not stop the bot answering."""
    from db_ops.telegram import command_processor

    class _Failed:
        returncode = 1
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(command_processor.subprocess, "run", lambda argv, **_k: _Failed())

    assert command_processor.sql_task_parameter_names("19") == []


def test_the_listing_command_reports_a_cli_failure_instead_of_an_empty_list(monkeypatch):
    """An empty list reads as "there are no SQL tasks", which is a different and wrong answer."""
    from db_ops.telegram import command_processor

    monkeypatch.setattr(command_processor, "sql_tasks_listing",
                        lambda *a, **k: {"ok": False, "error": "config unreadable"})

    result = command_processor.execute_list_sql_tasks_command(
        store=None, row=None, command=None, source_id="")

    assert "config unreadable" in result["listing"]
    assert result["command_count"] == 0


# --------------------------------------------------------------------------- #
# Answering the prompt: a value, not a lecture about NAME=VALUE
# --------------------------------------------------------------------------- #
def test_a_bare_value_binds_to_the_tasks_first_parameter():
    """Someone answering a prompt on a phone types the job number, not `job_no=<number>` — and
    the bot has just told them which parameter it wants."""
    command = _command(parameters=({"name": "job_no", "required": True},))
    parsed = runner.parse_parameter_arguments(None, "AA2608/01902")

    assert runner.bind_parameter_values(command, parsed) == {"job_no": "AA2608/01902"}


def test_bare_values_fill_the_declared_parameters_in_order():
    command = _command(parameters=(
        {"name": "session_id", "required": True}, {"name": "min_tran_seconds"}))
    parsed = runner.parse_parameter_arguments(None, "1068 300")

    assert runner.bind_parameter_values(command, parsed) == {
        "session_id": "1068", "min_tran_seconds": "300"}


def test_naming_one_and_leaving_the_other_bare_still_lands_where_it_was_meant():
    command = _command(parameters=(
        {"name": "session_id"}, {"name": "min_tran_seconds"}))
    parsed = runner.parse_parameter_arguments(None, "min_tran_seconds=900 1068")

    assert runner.bind_parameter_values(command, parsed) == {
        "session_id": "1068", "min_tran_seconds": "900"}


def test_named_values_keep_working_exactly_as_before():
    command = _command(parameters=({"name": "job_no"},))
    parsed = runner.parse_parameter_arguments(["job_no=AA2608/01902"], "")

    assert runner.bind_parameter_values(command, parsed) == {"job_no": "AA2608/01902"}


def test_more_bare_values_than_parameters_names_what_the_task_takes():
    """Dropping the extra silently would run the task with different arguments than were typed."""
    command = _command(parameters=({"name": "job_no"},))
    parsed = runner.parse_parameter_arguments(None, "one two")

    with pytest.raises(SqlParameterError, match="takes job_no"):
        runner.bind_parameter_values(command, parsed)


def test_the_dash_answer_still_means_no_values():
    command = _command(parameters=({"name": "job_no"},))

    assert runner.bind_parameter_values(command, runner.parse_parameter_arguments(None, "-")) == {}


# --------------------------------------------------------------------------- #
# The rows come back to whoever asked
# --------------------------------------------------------------------------- #
def _queued(monkeypatch):
    messages = []
    monkeypatch.setattr(runner, "queue_message",
                        lambda payload, **_kwargs: messages.append(payload))
    monkeypatch.setattr(runner, "store_block_from", lambda _store: {})
    return messages


def test_an_inline_result_is_delivered_to_the_chat_that_asked(monkeypatch):
    """A file already did this; an inline table reported "finished" in the requesting chat and
    printed the answer in a group the person may not even be in."""
    messages = _queued(monkeypatch)
    result = {"row_count": 2, "files": [{"result_sets": [
        {"columns": ["SPID"], "rows": [[1], [2]]}]}]}

    delivered = runner.enqueue_sql_task_result_text(
        store=None, command=_command(), target=_target(output_format="plain"),
        sql_run_id=42, result=result, chat_id="-100999",
    )

    assert delivered is True
    assert messages[0]["chat_id"] == "-100999"
    assert messages[0]["message_type"] == "plain"
    assert "SPID" in messages[0]["text"]


def test_a_target_that_exports_a_file_is_left_to_the_document_path(monkeypatch):
    messages = _queued(monkeypatch)

    delivered = runner.enqueue_sql_task_result_text(
        store=None, command=_command(), target=_target(output_format="xlsx"),
        sql_run_id=42, result={"row_count": 1, "files": []}, chat_id="-100999",
    )

    assert delivered is False and messages == []


def test_nothing_is_queued_when_no_chat_asked(monkeypatch):
    """A scheduled run has no requester; its rows stay on the log line as before."""
    messages = _queued(monkeypatch)

    delivered = runner.enqueue_sql_task_result_text(
        store=None, command=_command(), target=_target(output_format="plain"),
        sql_run_id=42, result={"row_count": 1, "files": [{"result_sets": [
            {"columns": ["A"], "rows": [[1]]}]}]}, chat_id="",
    )

    assert delivered is False and messages == []


def test_the_log_line_drops_the_table_once_the_requester_has_it(monkeypatch):
    """Otherwise the same rows go out twice for one run."""
    messages = _queued(monkeypatch)
    from db_ops.lib.notify import NotifyRule

    runner.enqueue_sql_task_message(
        store=None, telegram_groups={"sql": "-100111"},
        rule=NotifyRule(enabled=True, telegram_chat="sql"),
        command=_command(), target=_target(output_format="plain"), status="done",
        message="finished", sql_run_id=42,
        result={"row_count": 1, "files": [{"result_sets": [
            {"columns": ["SPID"], "rows": [[7]]}]}]},
        include_result_table=False,
    )

    assert messages[0]["chat_id"] == "-100111"
    assert "SPID" not in messages[0]["text"]
