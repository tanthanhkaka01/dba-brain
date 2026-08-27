import json
import sys

import pytest

from db_ops.lib.sql_access import is_legacy as sql_access_is_legacy
from db_ops.lib.time_window import TimeWindow
from db_ops.common import data_sources, sql_execution
from db_ops.sql_tasks import runner
from db_ops.sql_tasks.runner import parse_args, run_sql_id_tasks


class FakeSqlRunStore:
    def __init__(self):
        self.inserted = []
        self.updated = []

    def insert_sql_run(self, **kwargs):
        self.inserted.append(kwargs)
        return 123

    def update_sql_run(self, **kwargs):
        self.updated.append(kwargs)


def make_target(database_name="db", db_type="sqlserver", service_name="svc"):
    return runner.SqlTarget(
        sql_id=9,
        target_no=1,
        server_id="server",
        db_type=db_type,
        service_name=service_name,
        instance_name="inst",
        credential_name="cred",
        time_window=TimeWindow(from_day=1, to_day=31, from_hour=0, to_hour=23, repeat_interval=60),
        active=True,
        database_name=database_name,
    )


def make_inventory_and_credentials():
    inventory = [
        {
            "server_id": "server",
            "ip": "127.0.0.1",
            "databases": [{"db_type": "sqlserver", "service_name": "svc", "instance_name": "inst"}],
        }
    ]
    credentials = [
        {
            "server_id": "server",
            "db_type": "sqlserver",
            "service_name": "svc",
            "instance_name": "inst",
            "credentials": [{"credential_name": "cred", "username": "user", "password": "pass"}],
        }
    ]
    return inventory, credentials


def run_command(command, data_dir, monkeypatch):
    executed_sql = []
    log_messages = []

    def fake_execute_sql(**kwargs):
        executed_sql.append(kwargs["sql_text"].strip())
        return {"row_count": 1, "result_sets": []}

    monkeypatch.setattr(runner, "execute_sql", fake_execute_sql)
    monkeypatch.setattr(runner, "log_event", lambda logger, level, message: log_messages.append(message))
    inventory, credentials = make_inventory_and_credentials()

    success = runner.run_one_sql_task(
        store=FakeSqlRunStore(),
        data_dir=data_dir,
        telegram_groups={},
        command=command,
        target=make_target(),
        inventory=inventory,
        credentials=credentials,
        secrets={},
        logger=object(),
    )

    assert success is True
    return executed_sql, log_messages


def write_sql_commands(data_dir, commands):
    (data_dir / "sql_commands.json").write_text(json.dumps({"sql_commands": commands}), encoding="utf-8")


def test_parse_run_sql_id_force_command():
    args = parse_args(["--config", "config.json", "run-sql-id", "--sql-id", "9", "--force"])

    assert args.command == "run-sql-id"
    assert args.sql_id == 9
    assert args.force is True


def test_run_sql_id_force_selects_inactive_targets(tmp_path, capsys):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "sql_commands.json").write_text(
        json.dumps(
            {
                "sql_commands": [
                    {
                        "sql_id": 9,
                        "output": {"format": "plain", "telegram_chat": "sql",
                                   "chat_id": ""},
                        "notify": {
                            "logging_on_run": {"enabled": True, "telegram_chat": "sql"},
                            "alert_on_error": {"enabled": True, "telegram_chat": "sql"},
                        },
                        "sql_code": "SQLSERVER-009",
                        "sql_name": "manual force task",
                        "db_type": "sqlserver",
                        "script_type": "single",
                        "script_path": "sql/tasks/sqlserver/test.sql",
                        "active": False,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (data_dir / "sql_targets.json").write_text(
        json.dumps(
            {
                "sql_targets": [
                    {
                        "sql_id": 9,
                        "output": {"format": "plain", "telegram_chat": "sql",
                                   "chat_id": ""},
                        "notify": {
                            "logging_on_run": {"enabled": True, "telegram_chat": "sql"},
                            "alert_on_error": {"enabled": True, "telegram_chat": "sql"},
                        },
                        "target_no": 1,
                        "server_id": "server",
                        "db_type": "sqlserver",
                        "service_name": "svc",
                        "instance_name": "inst",
                        "credential_name": "cred",
                        "time_window": {
                            "from_day": 1,
                            "to_day": 1,
                            "from_hour": 0,
                            "to_hour": 0,
                            "repeat_interval": 999999,
                        },
                        "active": False,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = run_sql_id_tasks(
        store=object(),
        data_dir=data_dir,
        sql_id=9,
        force=True,
        dry_run=True,
        telegram_groups={},
        logger=None,
    )

    output = capsys.readouterr().out
    assert result.due_count == 1
    assert "SQLSERVER-009 target=1" in output
    assert "script_type=single" in output
    assert "force=True" in output


def test_single_script_execution(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    script_dir = data_dir / "sql" / "tasks" / "sqlserver"
    script_dir.mkdir(parents=True)
    (script_dir / "single.sql").write_text("SELECT 'single';", encoding="utf-8")
    write_sql_commands(
        data_dir,
        [
            {
                "sql_id": 9,
                "sql_code": "SQLSERVER-009",
                "db_type": "sqlserver",
                "script_type": "single",
                "script_path": "sql/tasks/sqlserver/single.sql",
            }
        ],
    )
    command = runner.load_sql_commands(data_dir / "sql_commands.json")[9]

    executed_sql, log_messages = run_command(command, data_dir, monkeypatch)

    assert executed_sql == ["SELECT 'single';"]
    assert any("script_type=single" in message and "actual_file=" in message for message in log_messages)


def test_sql_target_database_name_is_loaded(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "db_instances.json").write_text(json.dumps({"db_instances": []}), encoding="utf-8")
    (data_dir / "sql_targets.json").write_text(
        json.dumps(
            {
                "sql_targets": [
                    {
                        "sql_id": 9,
                        "output": {"format": "plain", "telegram_chat": "sql",
                                   "chat_id": ""},
                        "notify": {
                            "logging_on_run": {"enabled": True, "telegram_chat": "sql"},
                            "alert_on_error": {"enabled": True, "telegram_chat": "sql"},
                        },
                        "target_no": 1,
                        "server_id": "server",
                        "db_type": "sqlserver",
                        "service_name": "svc",
                        "instance_name": "inst",
                        "database_name": "Globex_Prod",
                        "credential_name": "cred",
                        "time_window": {"repeat_interval": 60},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    target = runner.load_sql_targets(data_dir / "sql_targets.json")[0]

    assert target.database_name == "Globex_Prod"


def test_a_target_that_skipped_instance_name_does_not_carry_the_string_None(tmp_path):
    """`/spbot_add_sql` writes JSON null for whatever the operator skipped, and
    ``str(item.get(key, ""))`` turned that null into the literal ``"None"``.

    Because ``"None"`` is a truthy string, find_database_inventory compared it against every
    real instance name and matched none, so SQLSERVER-017 failed at run time with
    "Target database not found in database-inventory.json: ACME-192-0-2-248/None" while its
    config looked correct. A skipped field must read as absent, not as the word None."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "sql_targets.json").write_text(
        json.dumps({"sql_targets": [{
            "sql_id": 17, "target_no": 1,
            "output": {"format": "plain", "telegram_chat": "sql", "chat_id": ""},
            "notify": {"logging_on_run": {"enabled": True, "telegram_chat": "sql"},
                       "alert_on_error": {"enabled": True, "telegram_chat": "sql"}},
            "server_id": "ACME-192-0-2-248", "db_type": "sqlserver",
            "service_name": None, "instance_name": None,
            "database_name": None, "credential_name": None,
            "time_window": {"repeat_interval": 300},
        }]}),
        encoding="utf-8",
    )

    target = runner.load_sql_targets(data_dir / "sql_targets.json")[0]

    assert (target.service_name, target.instance_name) == ("", "")
    assert target.database_name is None

    # ... and with the instance blank, the server's only sqlserver instance still matches.
    servers = [{"server_id": "ACME-192-0-2-248", "ip": "192.0.2.248", "databases": [
        {"db_type": "sqlserver", "instance_name": "SQLEXPRESS", "database_names": ["Globex_Prod"]},
    ]}]
    assert runner.find_database_inventory(target, servers) is not None


def test_server_id_alone_finds_the_credential_of_a_single_instance_server(tmp_path):
    """`/spbot_add_sql` no longer asks for the instance, so a target names only its server. The
    credential index has to answer that, but only while the server runs one instance — guessing
    on a two-instance server would run the SQL against the wrong database."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "db_instances.json").write_text(
        json.dumps({"db_instances": [{
            "server_id": "ACME-192-0-2-248", "ip": "192.0.2.248", "db_type": "sqlserver",
            "service_name": "GLOBEX", "instance_name": "SQLEXPRESS",
            "default_credential_name": "sqlserver_2.248_MSSQLSERVER_dba_user",
        }]}),
        encoding="utf-8",
    )
    (data_dir / "sql_targets.json").write_text(
        json.dumps({"sql_targets": [{
            "sql_id": 17, "target_no": 1,
            "output": {"format": "plain", "telegram_chat": "sql", "chat_id": ""},
            "notify": {"logging_on_run": {"enabled": True, "telegram_chat": "sql"},
                       "alert_on_error": {"enabled": True, "telegram_chat": "sql"}}, "server_id": "ACME-192-0-2-248",
            "db_type": "sqlserver", "service_name": None, "instance_name": None,
            "credential_name": None, "time_window": {"repeat_interval": 300},
        }]}),
        encoding="utf-8",
    )

    target = runner.load_sql_targets(data_dir / "sql_targets.json")[0]

    assert target.credential_name == "sqlserver_2.248_MSSQLSERVER_dba_user"


def test_two_instances_on_one_server_refuse_to_guess_a_credential(tmp_path):
    """The ambiguity is reported as ambiguity. Picking either instance would run the task
    against a database the operator never named."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "db_instances.json").write_text(
        json.dumps({"db_instances": [
            {"server_id": "ACME-10-0-0-9", "db_type": "sqlserver", "service_name": "PROD",
             "instance_name": "MSSQLSERVER", "default_credential_name": "cred_prod"},
            {"server_id": "ACME-10-0-0-9", "db_type": "sqlserver", "service_name": "TEST",
             "instance_name": "SQLEXPRESS", "default_credential_name": "cred_test"},
        ]}),
        encoding="utf-8",
    )

    # Takes the records, not the path: db_instances.json has exactly one reader
    # (common.data_sources) since 2026-08-15.
    defaults = runner.load_default_credential_names(data_sources.load_db_instances(data_dir))

    loose = runner._target_default_key(
        server_id="ACME-10-0-0-9", db_type="sqlserver", service_name="", instance_name="")
    assert defaults[loose] == runner._AMBIGUOUS_CREDENTIAL
    # The named instances still resolve exactly.
    named = runner._target_default_key(
        server_id="ACME-10-0-0-9", db_type="sqlserver", service_name="prod",
        instance_name="mssqlserver")
    assert defaults[named] == "cred_prod"


def _command(*, autocommit=False, db_type="sqlserver"):
    return runner.SqlCommand(
        sql_id=9,
        sql_code="SQLSERVER-009",
        sql_name="test",
        db_type=db_type,
        script_type="single",
        script_path="test.sql",
        script_paths=(),
        script_files=("test.sql",),
        active=True,
        autocommit=autocommit,
    )


def _capture_request(monkeypatch, answer=None):
    """Record the request the runner sends to ``common.cli run-sql``, and answer it.

    Stubbed at the CLI boundary since 2026-08-16. Before that this patched
    ``db_connect.connect_engine`` and asserted on connection arguments — but the runner no longer
    opens a connection, and the thing worth holding is one step earlier: **the request states the
    whole run**, so a task that says `Globex_Prod` and twenty minutes still says exactly that
    once the connecting moved out of this app.
    """
    seen: dict = {}

    def fake_run(command, request, **_kwargs):
        assert command == "run-sql"
        seen.clear()
        seen.update(request)
        return True, {"ok": True, "result_sets": [], "affected_rows": 0,
                      "result_sets_truncated": False, **(answer or {})}, ""

    monkeypatch.setattr(runner.common_cli, "run_allowing_failure", fake_run)
    return seen


def _run_one(monkeypatch, *, command=None, target=None, answer=None):
    seen = _capture_request(monkeypatch, answer)
    result = runner.execute_on_target(
        command=command or _command(),
        target=target or make_target(database_name="Globex_Prod"),
        database={"ip": "127.0.0.1", "port": 1433},
        credential={"username": "user"},
        password="pass",
        sql_text="SELECT 1;",
    )
    return seen, result


def test_execution_runs_against_the_database_the_target_names(monkeypatch):
    """A task declaring `Globex_Prod` must run there, not in the instance's default database."""
    seen, _ = _run_one(monkeypatch)

    assert seen["database"] == "Globex_Prod"
    assert seen["target"] == make_target(database_name="x").server_id
    assert seen["sql"] == "SELECT 1;"


def test_the_task_timeout_bounds_the_statements_not_the_connect(monkeypatch):
    """Different questions, so they never share a number: a task allowed twenty minutes must not
    wait twenty minutes to find out the host is down. `run-sql` grew
    `connect_timeout_seconds` for exactly this pairing — without it the conversion would have
    handed the task's whole budget to the connect."""
    seen, _ = _run_one(monkeypatch)

    assert seen["timeout_seconds"] == make_target(database_name="x").timeout_seconds
    assert seen["connect_timeout_seconds"] == runner.DEFAULT_CONNECT_TIMEOUT_SECONDS


def test_an_autocommit_task_is_not_committed_again(monkeypatch):
    """Autocommit exists for procs that refuse to run with @@TRANCOUNT > 0. Committing on top of
    a connection that already committed each batch is the bug this pairing prevents."""
    seen, _ = _run_one(monkeypatch, command=_command(autocommit=True))

    assert seen["autocommit"] is True and seen["commit"] is False


def test_an_ordinary_task_commits_once_at_the_end(monkeypatch):
    """The mirror of the above, and the one that would silently lose a task's writes: `run-sql`
    rolls back unless asked."""
    seen, _ = _run_one(monkeypatch)

    assert seen["autocommit"] is False and seen["commit"] is True


def test_an_oracle_task_takes_the_same_path_and_carries_its_transport(monkeypatch):
    """Oracle used to be a second connect function in this file with its own makedsn, and an 8i
    target a third path with its own bridge call. It is one request now; the target's own
    `sql_access` says which transport answers it."""
    seen, _ = _run_one(
        monkeypatch,
        command=_command(db_type="oracle"),
        target=make_target(database_name="", db_type="oracle", service_name="ORCLPDB"),
    )

    assert not sql_access_is_legacy(seen["sql_access"])   # a direct target, so binds
    assert "params" in seen                  # bound, because this one is not the legacy bridge


def test_every_result_set_is_asked_for_but_only_five_are_kept(monkeypatch):
    """`sql_runs.result_json` has always held five. The rows of the rest are still **counted**,
    which is why the request asks for all of them (`max_result_sets: 0`) and the cut happens
    here — dropping them earlier would make a run report fewer rows than it read."""
    sets = [{"columns": ["c"], "rows": [[n]], "row_count": 1, "truncated": False}
            for n in range(8)]
    seen, result = _run_one(monkeypatch, answer={"result_sets": sets, "affected_rows": 3})

    assert seen["capture"] == "all" and seen["max_result_sets"] == 0
    assert len(result["result_sets"]) == runner.MAX_STORED_RESULT_SETS
    assert result["row_count"] == 8 + 3      # every set's rows, plus what the script wrote


def test_a_set_cut_by_the_row_cap_is_reported_even_when_it_is_not_kept(monkeypatch):
    """Truncation is about whether the answer is complete, and the count above includes the sets
    beyond the fifth — so their truncation counts too."""
    sets = [{"columns": ["c"], "rows": [[n]], "row_count": 1, "truncated": n == 7}
            for n in range(8)]
    _seen, result = _run_one(monkeypatch, answer={"result_sets": sets})

    assert result["truncated"] is True


def test_a_failed_run_is_raised_with_its_reason(monkeypatch):
    monkeypatch.setattr(runner.common_cli, "run_allowing_failure",
                        lambda command, request, **_kw: (False, {}, "Login failed for user 'svc'."))

    with pytest.raises(RuntimeError, match="Login failed"):
        runner.execute_on_target(
            command=_command(), target=make_target(database_name="x"),
            database={}, credential={"username": "u"}, password="p", sql_text="SELECT 1;")


def test_find_database_inventory_respects_target_database_name():
    inventory = [
        {
            "server_id": "server",
            "ip": "127.0.0.1",
            "databases": [
                {
                    "db_type": "sqlserver",
                    "service_name": "svc",
                    "instance_name": "inst",
                    "database_names": ["Globex_Prod"],
                }
            ],
        }
    ]

    assert runner.find_database_inventory(make_target(database_name="Globex_Prod"), inventory) is not None
    assert runner.find_database_inventory(make_target(database_name="WrongDb"), inventory) is None


def test_array_script_execution_uses_configured_order(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    script_dir = data_dir / "sql" / "tasks" / "sqlserver"
    script_dir.mkdir(parents=True)
    (script_dir / "second.sql").write_text("SELECT 'second';", encoding="utf-8")
    (script_dir / "first.sql").write_text("SELECT 'first';", encoding="utf-8")
    write_sql_commands(
        data_dir,
        [
            {
                "sql_id": 9,
                "sql_code": "SQLSERVER-009",
                "db_type": "sqlserver",
                "script_type": "array",
                "script_paths": [
                    "sql/tasks/sqlserver/second.sql",
                    "sql/tasks/sqlserver/first.sql",
                ],
            }
        ],
    )
    command = runner.load_sql_commands(data_dir / "sql_commands.json")[9]

    executed_sql, _log_messages = run_command(command, data_dir, monkeypatch)

    assert executed_sql == ["SELECT 'second';", "SELECT 'first';"]


def test_folder_execution_sorts_by_filename(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    folder = data_dir / "sql" / "tasks" / "sqlserver" / "folder_task"
    folder.mkdir(parents=True)
    (folder / "020_second.sql").write_text("SELECT 'second';", encoding="utf-8")
    (folder / "010_first.sql").write_text("SELECT 'first';", encoding="utf-8")
    (folder / "readme.txt").write_text("ignored", encoding="utf-8")
    write_sql_commands(
        data_dir,
        [
            {
                "sql_id": 9,
                "sql_code": "SQLSERVER-009",
                "db_type": "sqlserver",
                "script_type": "folder",
                "script_path": "sql/tasks/sqlserver/folder_task",
            }
        ],
    )
    command = runner.load_sql_commands(data_dir / "sql_commands.json")[9]

    executed_sql, log_messages = run_command(command, data_dir, monkeypatch)

    assert executed_sql == ["SELECT 'first';", "SELECT 'second';"]
    assert [path.name for path in map(runner.Path, command.script_files)] == ["010_first.sql", "020_second.sql"]
    assert any("sql_tasks.runner.script.discovered" in message for message in log_messages)


def test_run_sql_id_dry_run_prints_folder_file_order(tmp_path, capsys):
    data_dir = tmp_path / "data"
    folder = data_dir / "sql" / "tasks" / "sqlserver" / "folder_task"
    folder.mkdir(parents=True)
    (folder / "020_second.sql").write_text("SELECT 'second';", encoding="utf-8")
    (folder / "010_first.sql").write_text("SELECT 'first';", encoding="utf-8")
    write_sql_commands(
        data_dir,
        [
            {
                "sql_id": 9,
                "sql_code": "SQLSERVER-009",
                "db_type": "sqlserver",
                "script_type": "folder",
                "script_path": "sql/tasks/sqlserver/folder_task",
            }
        ],
    )
    (data_dir / "sql_targets.json").write_text(
        json.dumps(
            {
                "sql_targets": [
                    {
                        "sql_id": 9,
                        "output": {"format": "plain", "telegram_chat": "sql",
                                   "chat_id": ""},
                        "notify": {
                            "logging_on_run": {"enabled": True, "telegram_chat": "sql"},
                            "alert_on_error": {"enabled": True, "telegram_chat": "sql"},
                        },
                        "target_no": 1,
                        "server_id": "server",
                        "db_type": "sqlserver",
                        "service_name": "svc",
                        "instance_name": "inst",
                        "credential_name": "cred",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = run_sql_id_tasks(
        store=object(),
        data_dir=data_dir,
        sql_id=9,
        force=True,
        dry_run=True,
        telegram_groups={},
        logger=None,
    )

    output = capsys.readouterr().out
    assert result.due_count == 1
    assert "file_order=[010_first.sql, 020_second.sql]" in output


def test_invalid_script_configuration_handling(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    write_sql_commands(
        data_dir,
        [
            {
                "sql_id": 9,
                "sql_code": "SQLSERVER-009",
                "db_type": "sqlserver",
                "script_type": "single",
                "script_path": "sql/tasks/sqlserver/single.sql",
                "script_paths": ["sql/tasks/sqlserver/extra.sql"],
            }
        ],
    )

    with pytest.raises(RuntimeError, match="script_type=single must not define script_paths"):
        runner.load_sql_commands(data_dir / "sql_commands.json")


def test_empty_folder_configuration_handling(tmp_path):
    data_dir = tmp_path / "data"
    folder = data_dir / "sql" / "tasks" / "sqlserver" / "empty_task"
    folder.mkdir(parents=True)
    write_sql_commands(
        data_dir,
        [
            {
                "sql_id": 9,
                "sql_code": "SQLSERVER-009",
                "db_type": "sqlserver",
                "script_type": "folder",
                "script_path": "sql/tasks/sqlserver/empty_task",
            }
        ],
    )

    with pytest.raises(RuntimeError, match=r"has no \*\.sql files"):
        runner.load_sql_commands(data_dir / "sql_commands.json")


def test_run_sql_id_requires_force(tmp_path):
    with pytest.raises(RuntimeError, match="requires --force"):
        run_sql_id_tasks(
            store=object(),
            data_dir=tmp_path,
            sql_id=9,
            force=False,
            dry_run=True,
            telegram_groups={},
            logger=None,
        )


def test_sql_task_log_uses_neutral_task_identity(monkeypatch):
    messages = []
    command = runner.SqlCommand(
        sql_id=9,
        sql_code="DATA_FINALIZE_10SECOND",
        sql_name="Data finalize workflow every 10 seconds",
        db_type="sqlserver",
        script_type="single",
        script_path="test.sql",
        script_paths=(),
        script_files=("test.sql",),
        active=True,
    )
    monkeypatch.setattr(runner, "log_event", lambda logger, level, message: messages.append(message))

    runner.log_sql_task_event(object(), "sql_tasks.runner.task.start", command=command, status="running", run_id=123)

    assert messages == [
        "sql_tasks.runner.task.start|scope=sql_tasks|sql_task_code=DATA_FINALIZE_10SECOND|sql_task_name=Data finalize workflow every 10 seconds|workflow=data_finalize|status=running|run_id=123"
    ]
    assert "JOB03.STEP1" not in messages[0]
    assert "JOB03.STEP6" not in messages[0]
    assert "jobs.step.run" not in messages[0]


def test_a_due_task_actually_reaches_the_executor_on_a_scheduled_scan(tmp_path, monkeypatch):
    """The scan must *run* the task it found, not just find it.

    Between 2026-08-12 and 2026-08-13 it did not. `run_scheduler_scan` passed
    `parameter_values=parameter_values` to the executor — a name that exists only on the
    single-task path — so the call raised NameError before the first task ever started. The scan
    only reaches that line when something is due, so the process exited 0 whenever nothing was
    due and 1 whenever anything was: from the outside, a scheduler that runs every minute and
    never runs a task. Manual runs and `--dry-run` both kept working (dry-run returns before the
    call), which is why nothing looked broken for a day and 1445 scans failed silently.

    So this asserts the one thing no other test did: that the executor is entered.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "sql_commands.json").write_text(
        json.dumps({"sql_commands": [{
            "sql_id": 9, "sql_code": "SQLSERVER-009", "sql_name": "scheduled task",
            "db_type": "sqlserver", "script_type": "single",
            "script_path": "sql/tasks/sqlserver/test.sql", "active": True,
        }]}),
        encoding="utf-8",
    )
    (data_dir / "sql_targets.json").write_text(
        json.dumps({"sql_targets": [{
            "sql_id": 9, "target_no": 1,
            "output": {"format": "plain", "telegram_chat": "sql", "chat_id": ""},
            "notify": {"logging_on_run": {"enabled": True, "telegram_chat": "sql"},
                       "alert_on_error": {"enabled": True, "telegram_chat": "sql"}}, "server_id": "server", "db_type": "sqlserver",
            "service_name": "svc", "instance_name": "inst", "credential_name": "cred",
            "time_window": {"repeat_interval": 60}, "active": True,
        }]}),
        encoding="utf-8",
    )

    class _Store:
        def fetch_latest_done_or_running_sql_runs_by_run_key(self):
            return {}

        def fetch_latest_sql_runs_by_run_key(self):
            return {}

    executed = []
    monkeypatch.setattr(runner, "mark_stale_running_sql_runs", lambda **kwargs: None)
    monkeypatch.setattr(runner.data_sources, "load_secret_text", lambda _dir: {})
    monkeypatch.setattr(runner.data_sources, "load_inventory", lambda _dir: [])
    monkeypatch.setattr(runner.data_sources, "load_all_credentials", lambda _dir: {})
    monkeypatch.setattr(runner, "run_one_sql_task",
                        lambda **kwargs: (executed.append(kwargs), True)[1])

    result = runner.run_scheduler_scan(store=_Store(), data_dir=data_dir, dry_run=False,
                                       telegram_groups={}, logger=None)

    assert result.error_count == 0
    assert len(executed) == 1, "the due task was found but never executed"
    # A scheduled scan has no operator to take parameters from: each task falls back to the
    # defaults it declares itself.
    assert executed[0]["parameter_values"] is None


def test_a_folder_task_reports_each_file_as_it_finishes():
    """A folder task can run for hours per file. Until 2026-08-17 the only two signals were
    "started" and silence, which is indistinguishable from a task that died — so a six-file task
    sent 2 messages across a working day. It now sends 8: start, one per finished file with a
    `[n/6]` counter, then done.

    The counter is the point: `[2/6]` says both that file 2 finished and that four remain.
    """
    from db_ops.sql_tasks.runner import SqlCommand

    command = SqlCommand(
        sql_id=1, sql_code="T", sql_name="t", db_type="sqlserver", script_type="folder",
        script_path="f", script_paths=(), script_files=("a.sql",) * 6, active=True)

    # None = automatic, and automatic means "on" for more than one file.
    assert command.progress_per_file is None
    assert (len(command.script_files) > 1 if command.progress_per_file is None
            else command.progress_per_file) is True


def test_a_single_file_task_does_not_announce_its_one_file():
    """start + "[1/1] done" + finished is three messages saying one thing."""
    from db_ops.sql_tasks.runner import SqlCommand

    command = SqlCommand(
        sql_id=1, sql_code="T", sql_name="t", db_type="sqlserver", script_type="single",
        script_path=None, script_paths=(), script_files=("only.sql",), active=True)

    assert (len(command.script_files) > 1 if command.progress_per_file is None
            else command.progress_per_file) is False


def test_progress_per_file_can_be_forced_either_way_in_config():
    """`config is data`: a noisy folder task can switch it off, a slow single file can switch on."""
    from db_ops.sql_tasks.runner import load_sql_commands
    import json
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "sql_commands.json"
        path.write_text(json.dumps({"sql_commands": [
            {"sql_id": 1, "sql_code": "A", "sql_name": "a", "db_type": "sqlserver",
             "script_type": "single", "script_path": "x.sql", "progress_per_file": True},
            {"sql_id": 2, "sql_code": "B", "sql_name": "b", "db_type": "sqlserver",
             "script_type": "single", "script_path": "x.sql", "progress_per_file": False},
            {"sql_id": 3, "sql_code": "C", "sql_name": "c", "db_type": "sqlserver",
             "script_type": "single", "script_path": "x.sql"},
        ]}), encoding="utf-8")
        (Path(tmp) / "x.sql").write_text("SELECT 1;", encoding="utf-8")
        commands = load_sql_commands(path)

    assert commands[1].progress_per_file is True
    assert commands[2].progress_per_file is False
    assert commands[3].progress_per_file is None      # automatic
