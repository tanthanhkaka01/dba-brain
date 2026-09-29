"""A SQL task connects first and asks the server only when that fails - and says what failed.

`SQL033-NIGHTLY-ENGINE` failed on 2026-09-24, every 30 minutes from 02:00, with

    Target database not found in database-inventory.json: ACME-192-0-2-50/APPDB-PROD/APPDB_PROD

and every part of it misled. The file consulted was `db_instances.json`. Nothing had been connected
to: a pre-check compared `database_name` case-sensitively with the record's `database_names`, a list
no code writes (`APPDB_PROD` against `APPDB_Prod`) - so a database created yesterday would have been
refused too. `APPDB-PROD` is a `service_name`, a label SQL Server does not have. And two of the three
targets named an instance the server does not have, which the message never said.

What these hold: the lookup finds the instance only; a database that does not open is diagnosed by
asking the server (`sys.databases`), with the name it probably meant; an instance that does not
exist is named with the ones that do, at run time and when the target is registered; SQL Server
messages say `server/instance.database`, `master` when no database is named, and never the
`service_name`.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from db_ops.common import sql_task_admin
from db_ops.lib import sql_task_target
from db_ops.sql_tasks import runner

from test_sql_task_claims import (RecordingSqlRunStore, inventory_and_credentials, sql_command,
                                  sql_target)

CANNOT_OPEN = ('[42000] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]Cannot open database '
               '"APPDB_PROD" requested by the login. The login failed. (4060) (SQLDriverConnect)')


# --------------------------------------------------------------------------- #
# The rules (lib)
# --------------------------------------------------------------------------- #
def test_sql_server_is_named_by_instance_and_database_never_by_service():
    assert sql_task_target.location(server_id="S", db_type="sqlserver", instance_name="APPINST",
                                    service_name="APPDB-PROD", database_name="APPDB_Prod") == "S/APPINST.APPDB_Prod"


def test_no_database_on_sql_server_is_master():
    """The operator, 2026-09-24. It is what `run-sql` connects to; the words now say so too."""
    assert sql_task_target.connect_database("sqlserver", None, "APPDB-PROD") == "master"
    assert sql_task_target.location(server_id="S", db_type="sqlserver", instance_name=None,
                                    service_name="APPDB-PROD", database_name="") == "S/MSSQLSERVER.master"


def test_oracle_is_still_named_by_its_service():
    assert sql_task_target.location(server_id="O", db_type="oracle", service_name="ORCLPDB",
                                    database_name="") == "O/ORCLPDB"


@pytest.mark.parametrize("text,kind", [
    (CANNOT_OPEN, "database"),
    ("[28000] ... Login failed for user 'x'. (18456) (SQLDriverConnect)", "login"),
    ("[08001] ... TCP Provider: No connection could be made (10061)", "unreachable"),
    ("[42S02] ... Invalid object name 'dbo.t'. (208) (SQLExecDirectW)", None),
])
def test_a_failure_to_connect_is_told_from_a_failure_in_the_script(text, kind):
    assert sql_task_target.classify_connect_failure(text) == kind


def test_the_name_meant_is_found_in_another_case_first():
    assert sql_task_target.near_match("APPDB_PROD", ["SALES_Prod", "APPDB_Prod", "APPDB_STG"]) == "APPDB_Prod"
    assert sql_task_target.near_match("APPDB_Prd", ["SALES_Prod", "APPDB_Prod"]) == "APPDB_Prod"
    assert sql_task_target.near_match("nothing_like", ["SALES_Prod"]) is None


def test_a_missing_database_is_named_with_what_the_server_has():
    message = sql_task_target.missing_database_message(
        database_name="APPDB_TESTNG", where="S/MSSQLSERVER", existing=["APPDB_Prod", "APPDB_Testing"])
    assert "does not exist on S/MSSQLSERVER" in message and "did you mean 'APPDB_Testing'" in message


def test_the_same_name_in_another_case_says_to_use_the_servers_spelling():
    message = sql_task_target.missing_database_message(
        database_name="APPDB_PROD", where="S/APPINST", existing=["APPDB_Prod"])
    assert "use that spelling" in message and "'APPDB_Prod'" in message


def test_an_existing_database_the_login_cannot_open_says_so():
    message = sql_task_target.missing_database_message(
        database_name="APPDB_Prod", where="S/APPINST", existing=["APPDB_Prod"])
    assert "cannot open it" in message


def test_an_instance_the_server_lacks_is_named_with_the_ones_it_has():
    records = [{"server_id": "S", "db_type": "sqlserver", "instance_name": "MSSQLSERVER"}]
    message = sql_task_target.instance_not_found_message(
        server_id="S", db_type="sqlserver", instance_name="APPINST", records=records)
    assert "no sqlserver instance 'APPINST'" in message and "it has: MSSQLSERVER" in message
    assert "not in db_instances.json" in sql_task_target.instance_not_found_message(
        server_id="T", db_type="sqlserver", instance_name=None, records=records)


# --------------------------------------------------------------------------- #
# The runner
# --------------------------------------------------------------------------- #
def _run(tmp_path, monkeypatch, *, target, inventory=None, execute=None, listing=None):
    data_dir = tmp_path / "data"
    (data_dir / "sql").mkdir(parents=True)
    (data_dir / "sql" / "task.sql").write_text("EXEC dbo.engine;", encoding="utf-8")
    monkeypatch.setattr(runner, "log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "execute_sql", execute or (lambda **_: {"row_count": 0, "result_sets": []}))
    asked = []

    def run_allowing_failure(command, request):
        asked.append((command, request))
        return listing if listing is not None else (True, {"databases": []}, "")

    monkeypatch.setattr(runner.common_cli, "run_allowing_failure", run_allowing_failure)
    sent = []
    monkeypatch.setattr(runner, "enqueue_sql_task_message", lambda **kwargs: sent.append(kwargs))
    store = RecordingSqlRunStore()
    default_inventory, credentials = inventory_and_credentials()
    runner.run_one_sql_task(
        store=store, data_dir=data_dir, telegram_groups={},
        command=sql_command(script_files=("sql/task.sql",)), target=target,
        inventory=inventory if inventory is not None else default_inventory,
        credentials=credentials, secrets={}, logger=None)
    return store.updated[-1], sent, asked


def test_a_database_the_records_list_does_not_name_still_runs(tmp_path, monkeypatch):
    inventory = [{"server_id": "server", "ip": "127.0.0.1", "databases": [
        {"db_type": "sqlserver", "service_name": "svc", "instance_name": "inst",
         "database_names": ["SomethingElse"]}]}]

    final, _, _ = _run(tmp_path, monkeypatch, target=sql_target(database_name="CreatedYesterday"),
                       inventory=inventory)

    assert final["status"] == "done"


def test_a_wrong_instance_is_named_with_the_one_the_server_has(tmp_path, monkeypatch):
    target = dataclasses.replace(sql_target(), instance_name="APPINST")

    final, _, _ = _run(tmp_path, monkeypatch, target=target)

    assert final["status"] == "error"
    assert "no sqlserver instance 'APPINST'" in final["error_text"] and "it has: inst" in final["error_text"]
    assert "database-inventory.json" not in final["error_text"]


def test_a_database_that_does_not_open_is_diagnosed_by_asking_the_server(tmp_path, monkeypatch):
    def cannot_open(**_):
        raise RuntimeError(CANNOT_OPEN)

    final, _, asked = _run(tmp_path, monkeypatch, target=sql_target(database_name="APPDB_PROD"),
                           execute=cannot_open,
                           listing=(True, {"databases": [{"name": "APPDB_Prod"}, {"name": "master"}]}, ""))

    command, request = asked[0]
    assert command == "list-databases" and request["include_system"] is True
    assert "database" not in request["connection"], "the listing lands in master, not the missing one"
    assert "'APPDB_Prod'" in final["error_text"] and "use that spelling" in final["error_text"]
    assert "(4060)" in final["error_text"], "the driver's own words are kept after the explanation"


def test_a_sql_error_inside_the_script_is_not_diagnosed(tmp_path, monkeypatch):
    def fails(**_):
        raise RuntimeError("[42S02] Invalid object name 'dbo.t'. (208)")

    final, _, asked = _run(tmp_path, monkeypatch, target=sql_target(), execute=fails)

    assert asked == [] and "Invalid object name" in final["error_text"]


def test_sql_server_messages_carry_no_service_name(tmp_path, monkeypatch):
    target = dataclasses.replace(sql_target(database_name=None), service_name="APPDB-PROD")

    _, sent, _ = _run(tmp_path, monkeypatch, target=target)

    started = next(m for m in sent if m["status"] == "running")
    assert "server/inst.master" in started["message"]
    assert all("APPDB-PROD" not in m["message"] for m in sent)


def test_the_telegram_block_shows_the_database_not_the_service_on_sql_server(monkeypatch):
    queued = []
    monkeypatch.setattr(runner, "queue_message", lambda payload, **_: queued.append(payload))
    rule = runner.NotifyRule(enabled=True, telegram_chat="sql", chat_id="-1")
    target = dataclasses.replace(sql_target(database_name=None), service_name="APPDB-PROD")

    runner.enqueue_sql_task_message(store=None, telegram_groups={"sql": "-1"}, rule=rule,
                                    command=sql_command(), target=target, status="done",
                                    message="m", sql_run_id=1)

    text = queued[0]["text"]
    assert "database_name: master" in text and "service_name" not in text


# --------------------------------------------------------------------------- #
# The registrar
# --------------------------------------------------------------------------- #
def _root(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "db_instances.json").write_text(json.dumps({"db_instances": [
        {"server_id": "S", "db_type": "sqlserver", "instance_name": "MSSQLSERVER", "ip": "192.0.2.9"}]}),
        encoding="utf-8")
    (data / "sql_commands.json").write_text(json.dumps({"sql_commands": [
        {"sql_id": 33, "sql_code": "X", "db_type": "sqlserver", "script_path": "x.sql"}]}), encoding="utf-8")
    (data / "sql_targets.json").write_text(json.dumps({"sql_targets": []}), encoding="utf-8")
    return data


def test_registering_a_target_on_an_instance_the_server_lacks_is_refused(tmp_path):
    data = _root(tmp_path)

    with pytest.raises(sql_task_admin.SqlTaskAdminError, match="it has: MSSQLSERVER"):
        sql_task_admin.add_sql_target({"sql_id": 33, "server_id": "S", "instance_name": "APPINST"},
                                      data_dir=data)


def test_the_instance_is_matched_ignoring_case(tmp_path):
    data = _root(tmp_path)

    written = sql_task_admin.add_sql_target({"sql_id": 33, "server_id": "S", "instance_name": "mssqlserver"},
                                            data_dir=data)

    assert written
