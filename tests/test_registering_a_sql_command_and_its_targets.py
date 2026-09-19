"""Why registering a SQL task is two commands, and what each one refuses.

Until 2026-09-18 the only way to register a task was `add-sql`, which writes one file, one command
and one target in a single call. Everything it cannot say was a hand-edit of `sql_commands.json`
and `sql_targets.json`: a python-fed task, a folder of scripts, a second target for a task that
already had one. Hand-edits produced a task pointing at a script nobody had written, and an
`input.parameter` naming a parameter the command did not declare.

They are two commands because they are two decisions. `sql-command-add` says WHAT runs;
`sql-target-add` says WHERE. A task on Testing, UAT and production is one command and three
targets, which is what the reserved `{target_server_id}` / `{target_database}` placeholders exist
for — three copies of the command is the state they were written to end.
"""

from __future__ import annotations

import json

import pytest

from db_ops.common import sql_task_admin


@pytest.fixture()
def estate(tmp_path):
    """A tool root with an empty config pair and one script already written."""
    data = tmp_path / "data"
    data.mkdir()
    (data / "sql_commands.json").write_text(json.dumps({"sql_commands": []}), encoding="utf-8")
    (data / "sql_targets.json").write_text(json.dumps({"sql_targets": []}), encoding="utf-8")
    script = tmp_path / "assets" / "tasks" / "sqlserver" / "summary.sql"
    script.parent.mkdir(parents=True)
    script.write_text("SELECT 1;\n", encoding="utf-8")
    program = tmp_path / "assets" / "tasks" / "python" / "drain.py"
    program.parent.mkdir(parents=True)
    program.write_text("print('{}')\n", encoding="utf-8")
    return tmp_path


def add_command(estate, **overrides):
    request = {"sql_name": "Drain the queue", "db_type": "sqlserver",
               "script_path": "assets/tasks/sqlserver/summary.sql"}
    request.update(overrides)
    return sql_task_admin.add_sql_command(request, data_dir=estate / "data", tool_root=estate)


def add_target(estate, **overrides):
    request = {"sql_id": 1, "server_id": "ACME-192-0-2-111", "database_name": "PAYROLL_Test"}
    request.update(overrides)
    return sql_task_admin.add_sql_target(request, data_dir=estate / "data")


def read(estate, name):
    return json.loads((estate / "data" / name).read_text(encoding="utf-8"))


def test_a_command_and_three_targets_is_one_command_and_three_targets(estate):
    """The shape the placeholders exist for, proved rather than described."""
    add_command(estate, input_type="python", input={
        "script": "assets/tasks/python/drain.py",
        "args": ["--target", "{target_server_id}", "--database", "{target_database}"]},
        parameters=[{"name": "payload", "type": "nvarchar(max)"}])
    for server, database in (("ACME-192-0-2-111", "PAYROLL_Test"),
                             ("ACME-192-0-2-111", "PAYROLL_Uat"),
                             ("ACME-192-0-2-250", "PAYROLL_Prod")):
        add_target(estate, server_id=server, database_name=database)

    commands = read(estate, "sql_commands.json")["sql_commands"]
    targets = read(estate, "sql_targets.json")["sql_targets"]
    assert len(commands) == 1, "one command"
    assert [t["target_no"] for t in targets] == [1, 2, 3], "numbered in the order they arrived"
    assert [t["database_name"] for t in targets] == ["PAYROLL_Test", "PAYROLL_Uat", "PAYROLL_Prod"]


def test_a_script_nobody_has_written_is_refused_here_not_at_three_in_the_morning(estate):
    with pytest.raises(sql_task_admin.SqlTaskAdminError, match="script_path not found"):
        add_command(estate, script_path="assets/tasks/sqlserver/does_not_exist.sql")


def test_a_placeholder_no_parameter_declares_is_refused_and_says_what_is_allowed(estate):
    """The runner raises this at run time. Raising it at registration is the whole point."""
    with pytest.raises(sql_task_admin.SqlTaskAdminError) as caught:
        add_command(estate, input_type="python", input={
            "script": "assets/tasks/python/drain.py",
            "args": ["--from", "{fromdate}"]},
            parameters=[{"name": "payload", "type": "nvarchar(max)"}])
    message = str(caught.value)
    assert "{fromdate}" in message
    assert "target_server_id" in message, "it names the reserved ones a caller may use"


def test_the_reserved_target_placeholders_need_no_parameter(estate):
    outcome = add_command(estate, input_type="python", input={
        "script": "assets/tasks/python/drain.py",
        "args": ["--target", "{target_server_id}", "--database", "{target_database}"]},
        parameters=[{"name": "payload", "type": "nvarchar(max)"}])
    assert outcome["input_type"] == "python"


def test_an_input_parameter_the_command_does_not_declare_is_refused(estate):
    """The runner writes the DECLARE from `parameters`; without the entry the SQL has no @payload."""
    with pytest.raises(sql_task_admin.SqlTaskAdminError, match="must also appear in parameters"):
        add_command(estate, input_type="python", input={
            "script": "assets/tasks/python/drain.py", "parameter": "rows"},
            parameters=[{"name": "payload", "type": "nvarchar(max)"}])


def test_sql_text_writes_the_file_so_telegram_and_a_shell_take_the_same_road(estate):
    """A task typed into Telegram has no file yet; one written in the repo has nothing to type."""
    outcome = add_command(estate, sql_name="Typed in a chat", script_path=None,
                          sql_text="SELECT GETDATE();")
    written = estate / outcome["script_written"]
    assert written.read_text(encoding="utf-8") == "SELECT GETDATE();\n"
    assert outcome["files_written"][0] == outcome["script_written"], "the file is written first"


def test_two_typed_tasks_get_two_files_rather_than_one_overwriting_the_other(estate):
    """The derived name carries the sql_id, so the second task cannot land on the first's file."""
    first = add_command(estate, sql_name="Typed in a chat", script_path=None, sql_text="SELECT 1;")
    second = add_command(estate, sql_name="Typed in a chat", script_path=None, sql_text="SELECT 2;")
    assert first["script_written"] != second["script_written"]
    assert (estate / first["script_written"]).read_text(encoding="utf-8") == "SELECT 1;\n"


def test_sql_text_does_not_overwrite_a_named_file_unless_replace_is_asked_for(estate):
    """Naming `script_path` with text is how a task is re-edited; silently was how one was lost."""
    with pytest.raises(sql_task_admin.SqlTaskAdminError, match="already exists"):
        add_command(estate, sql_text="SELECT 2;",
                    script_path="assets/tasks/sqlserver/summary.sql")
    add_command(estate, sql_text="SELECT 2;", replace=True,
                script_path="assets/tasks/sqlserver/summary.sql")
    assert (estate / "assets/tasks/sqlserver/summary.sql").read_text(encoding="utf-8") == "SELECT 2;\n"


def test_a_second_command_on_the_same_id_is_refused_unless_replace_is_asked_for(estate):
    add_command(estate)
    with pytest.raises(sql_task_admin.SqlTaskAdminError, match="already registered"):
        add_command(estate, sql_id=1)
    outcome = add_command(estate, sql_id=1, sql_name="Renamed", replace=True)
    assert outcome["replaced"] is True
    assert len(read(estate, "sql_commands.json")["sql_commands"]) == 1, "replaced, not appended"


def test_a_target_for_a_command_that_does_not_exist_is_refused(estate):
    """It would be a row the runner never reads, and nothing else would ever say so."""
    with pytest.raises(sql_task_admin.SqlTaskAdminError, match="no SQL command with sql_id"):
        add_target(estate, sql_id=99)


def test_a_target_number_already_taken_is_refused_unless_replace_is_asked_for(estate):
    add_command(estate)
    add_target(estate)
    with pytest.raises(sql_task_admin.SqlTaskAdminError, match="already has target_no"):
        add_target(estate, target_no=1)
    outcome = add_target(estate, target_no=1, database_name="PAYROLL_Other", replace=True)
    assert outcome["replaced"] is True
    assert len(read(estate, "sql_targets.json")["sql_targets"]) == 1


def test_a_target_inherits_the_command_db_type_rather_than_asking_again(estate):
    add_command(estate)
    add_target(estate)
    target = read(estate, "sql_targets.json")["sql_targets"][0]
    assert target["db_type"] == "sqlserver"


def test_manual_only_is_the_repeat_interval_the_scheduler_skips(estate):
    add_command(estate)
    outcome = add_target(estate, manual_only=True)
    assert outcome["manual_only"] is True
    assert outcome["repeat_interval"] == -1


def test_a_target_writes_the_notify_object_in_its_canonical_nested_form(estate):
    """A half-migrated file is how a convention rots - `add_sql_task` says the same."""
    add_command(estate)
    add_target(estate, logging_on_run=False, alert_on_error=True)
    target = read(estate, "sql_targets.json")["sql_targets"][0]
    assert target["notify"]["logging_on_run"]["enabled"] is False
    assert target["notify"]["alert_on_error"]["enabled"] is True
    assert "logging_on_run" not in target, "never the old top-level spelling"
