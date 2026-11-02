"""A task's program may read its rows from a second database, logged in by a credential's name.

The attendance tasks (SQL029, SQL036) read an open HTTP API and a clock-in device, and needed no
login. The first source that does is an asset system on MariaDB feeding a SQL Server table
(2026-10-27). A fetcher could not log in to it: it is never given the passphrase (review 0.25.0,
F4.2), and ``input.target_connection`` hands it only its own target's login. The way left was a
password in the script or a file beside it - the copy of a credential this tool exists to end.

So the command names the second server, ``input.source_connection``, by ``server_id``, ``db_type``
and ``credential_name`` - never a password - and the runner resolves it exactly as it resolves a
target: the instance from the inventory, the login from ``users.json``, the password by its
``password_ref`` from the secret store. It reaches the program on stdin beside ``connection``.
Switching the login later is one edit in ``users.json``; the task and the program do not change.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from dbabrain.common import sql_task_admin
from dbabrain.lib import task_input
from dbabrain.sql_tasks import python_source as ps

SOURCE = {"server_id": "ACME-192-0-2-57", "db_type": "mysql", "credential_name": "assets_reader"}

READS_STDIN = ("import json, os, sys\n"
               "document = json.loads(sys.stdin.read() or 'null')\n"
               "print(json.dumps({'data': [{'stdin': document,\n"
               "                            'key': os.environ.get('DBABRAIN_SECRET_KEY', '')}]}))\n")


def write(root, name, body):
    path = root / "assets" / "tasks" / "python" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return f"assets/tasks/python/{name}"


# ------------------------------------------------------------------ what the command may say

def test_the_source_is_read_off_the_command_by_name():
    source = ps.parse({"script": "a.py", "source_connection": dict(SOURCE, db_type="MySQL")},
                      command_name="TEST-099").source_connection

    assert source == task_input.SourceRef(server_id="ACME-192-0-2-57", db_type="mysql",
                                          credential_name="assets_reader")


def test_a_command_that_names_no_source_has_none():
    assert ps.parse({"script": "a.py"}, command_name="TEST-099").source_connection is None


@pytest.mark.parametrize("missing", ["server_id", "db_type", "credential_name"])
def test_a_source_missing_what_resolves_it_is_refused(missing):
    with pytest.raises(ps.PythonSourceError, match=f"missing {missing}"):
        ps.parse({"script": "a.py", "source_connection": {k: v for k, v in SOURCE.items() if k != missing}},
                 command_name="TEST-099")


def test_a_password_in_the_source_is_refused():
    """The one thing this exists to keep out of the command."""
    with pytest.raises(ps.PythonSourceError, match=r"does not take \['password'\]"):
        ps.parse({"script": "a.py", "source_connection": dict(SOURCE, password="secret")},
                 command_name="TEST-099")


# ------------------------------------------------------------------ what the program is handed

def test_the_source_connection_reaches_the_program_on_stdin_and_the_key_does_not(tmp_path, monkeypatch):
    monkeypatch.setenv("DBABRAIN_SECRET_KEY", "the-master-key")
    source = ps.PythonSource(script_path=write(tmp_path, "read.py", READS_STDIN),
                             source_connection=task_input.SourceRef(**SOURCE))
    handed = {"server_id": "ACME-192-0-2-57", "db_type": "mysql", "password": "reader-password"}

    row = ps.run(source, tool_root=tmp_path, source_connection=handed).rows[0]

    assert row["stdin"] == {"source_connection": handed}
    assert row["key"] == "", "the passphrase is still withheld"


def test_both_connections_travel_together_when_both_are_asked_for(tmp_path):
    source = ps.PythonSource(script_path=write(tmp_path, "read.py", READS_STDIN),
                             target_connection=True,
                             source_connection=task_input.SourceRef(**SOURCE))

    row = ps.run(source, tool_root=tmp_path, connection={"server_id": "target"},
                 source_connection={"server_id": "ACME-192-0-2-57"}).rows[0]

    assert row["stdin"] == {"connection": {"server_id": "target"},
                            "source_connection": {"server_id": "ACME-192-0-2-57"}}


def test_a_source_the_runner_could_not_resolve_stops_the_program_before_it_runs(tmp_path):
    source = ps.PythonSource(script_path=write(tmp_path, "read.py", READS_STDIN),
                             source_connection=task_input.SourceRef(**SOURCE))

    with pytest.raises(ps.PythonSourceError, match="input.source_connection"):
        ps.run(source, tool_root=tmp_path, source_connection=None)


# ------------------------------------------------------------------ how the runner resolves it

def _run(tmp_path, monkeypatch, *, source_credentials):
    from conftest import patch_sql_runner
    from test_sql_task_claims import (RecordingSqlRunStore, inventory_and_credentials, sql_command,
                                      sql_target)

    from dbabrain.sql_tasks import runner

    data_dir = tmp_path / "data"
    (data_dir / "sql").mkdir(parents=True)
    (data_dir / "sql" / "load.sql").write_text("SELECT 1;", encoding="utf-8")
    command = dataclasses.replace(
        sql_command(script_files=("sql/load.sql",)),
        python_source=ps.PythonSource(script_path="read.py",
                                      source_connection=task_input.SourceRef(**SOURCE)))
    handed = []
    patch_sql_runner(monkeypatch, "_run_python_source",
                     lambda **kwargs: handed.append(kwargs.get("source_connection")) or ["[]"])
    patch_sql_runner(monkeypatch, "execute_sql", lambda **_: {"row_count": 0, "result_sets": []})
    patch_sql_runner(monkeypatch, "log_event", lambda *args, **kwargs: None)
    patch_sql_runner(monkeypatch, "enqueue_sql_task_message", lambda **_: None)
    inventory, credentials = inventory_and_credentials()
    # The record states its port and its database, as every instance must (rules R50).
    inventory.append({"server_id": "ACME-192-0-2-57", "ip": "192.0.2.57", "databases": [
        {"db_type": "mysql", "instance_name": "", "port": 3306, "database": "assets"}]})
    store = RecordingSqlRunStore()
    ok = runner.run_one_sql_task(
        store=store, data_dir=data_dir, telegram_groups={}, command=command, target=sql_target(),
        inventory=inventory, credentials=credentials, secrets={"ASSETS_READER_PASSWORD": "reader-pw"},
        logger=None, source_credentials=source_credentials)
    return ok, handed, store


MYSQL_GROUPS = [{"server_id": "ACME-192-0-2-57", "db_type": "mysql", "credentials": [
    {"credential_name": "assets_reader", "username": "reader",
     "password_ref": "ASSETS_READER_PASSWORD"}]}]


def test_the_runner_resolves_the_source_like_a_target_password_from_its_ref(tmp_path, monkeypatch):
    ok, handed, _ = _run(tmp_path, monkeypatch, source_credentials=MYSQL_GROUPS)

    assert ok is True
    assert handed[0]["server_id"] == "ACME-192-0-2-57"
    assert (handed[0]["host"], handed[0]["port"], handed[0]["database"]) == ("192.0.2.57", 3306, "assets")
    assert (handed[0]["username"], handed[0]["password"]) == ("reader", "reader-pw")


def test_a_login_the_source_does_not_have_fails_the_task_before_the_program_runs(tmp_path, monkeypatch):
    ok, handed, store = _run(tmp_path, monkeypatch, source_credentials=[])

    assert ok is False
    assert handed == [], "the program never started"
    assert "input.source_connection" in json.dumps(store.updated, default=str)


def test_the_source_logins_are_taken_from_its_own_engine():
    """The target's groups are SQL Server's; the MariaDB login is under mysql."""
    from test_sql_task_claims import sql_command

    from dbabrain.sql_tasks import runner

    command = dataclasses.replace(
        sql_command(), python_source=ps.PythonSource(
            script_path="read.py", source_connection=task_input.SourceRef(**SOURCE)))
    groups = {"sqlserver": [{"server_id": "server"}], "mysql": MYSQL_GROUPS}

    assert runner.source_credentials(command, groups) == MYSQL_GROUPS
    assert runner.source_credentials(sql_command(), groups) is None


# ------------------------------------------------------------------ registering it

@pytest.fixture()
def estate(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "sql_commands.json").write_text(json.dumps({"sql_commands": []}), encoding="utf-8")
    (data / "db_instances.json").write_text(json.dumps({"db_instances": [
        {"server_id": "ACME-192-0-2-57", "db_type": "mysql", "instance_name": ""}]}), encoding="utf-8")
    for path, body in (("assets/tasks/sqlserver/load.sql", "SELECT 1;\n"),
                       ("assets/tasks/python/read.py", "print('{}')\n")):
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(body, encoding="utf-8")
    return tmp_path


def add_command(estate, source):
    return sql_task_admin.add_sql_command(
        {"sql_name": "Load assets", "db_type": "sqlserver",
         "script_path": "assets/tasks/sqlserver/load.sql", "input_type": "python",
         "input": {"script": "assets/tasks/python/read.py", "source_connection": source},
         "parameters": [{"name": "payload", "type": "nvarchar(max)"}]},
        data_dir=estate / "data", tool_root=estate)


def test_the_registrar_writes_the_source_by_name(estate):
    add_command(estate, dict(SOURCE))

    written = json.loads((estate / "data" / "sql_commands.json").read_text(encoding="utf-8"))
    assert written["sql_commands"][0]["input"]["source_connection"] == SOURCE


def test_the_registrar_refuses_a_source_the_inventory_lacks(estate):
    with pytest.raises(sql_task_admin.SqlTaskAdminError, match="ACME-192-0-2-58"):
        add_command(estate, dict(SOURCE, server_id="ACME-192-0-2-58"))


def test_the_registrar_refuses_a_password_in_the_source(estate):
    with pytest.raises(sql_task_admin.SqlTaskAdminError, match="password"):
        add_command(estate, dict(SOURCE, password="secret"))


# ------------------------------------------------------------------ a target older than OPENJSON

def test_an_xml_payload_is_one_row_element_per_row_and_one_attribute_per_key():
    """The first task fed this way writes to SQL Server 2008 R2, which has no OPENJSON (2016+) and
    reads this with nodes() / value() instead."""
    (payload,) = ps.batches([{"McSerialNumber": "COMP0001", "UserID": None, "Factory": 'A&B "F1"'},
                             {"McSerialNumber": "COMP0002", "Count": 3}], 10, "xml")

    assert payload == ('<rows><row McSerialNumber="COMP0001" Factory="A&amp;B &quot;F1&quot;" />'
                       '<row McSerialNumber="COMP0002" Count="3" /></rows>')


def test_an_empty_xml_pull_still_runs_the_sql_once():
    assert ps.batches([], 100, "xml") == ["<rows />"]


def test_the_default_payload_is_still_json():
    assert ps.batches([{"a": 1}], 10) == ['[{"a": 1}]']


def test_a_key_that_cannot_be_an_attribute_is_refused_naming_the_row():
    with pytest.raises(ps.PythonSourceError, match="row 2 has the key 'user id'"):
        ps.batches([{"ok": 1}, {"user id": "x"}], 10, "xml")


def test_a_character_xml_cannot_carry_is_refused_rather_than_dropped():
    with pytest.raises(ps.PythonSourceError, match="U[+]0001"):
        ps.batches([{"note": "a\x01b"}], 10, "xml")


def test_the_payload_format_is_read_off_the_command_and_an_unknown_one_refused():
    assert ps.parse({"script": "a.py", "payload_format": "XML"}, command_name="T").payload_format == "xml"
    assert ps.parse({"script": "a.py"}, command_name="T").payload_format == "json"
    with pytest.raises(ps.PythonSourceError, match="payload_format must be one of"):
        ps.parse({"script": "a.py", "payload_format": "csv"}, command_name="T")


def test_the_registrar_writes_xml_and_leaves_the_default_unwritten(estate):
    sql_task_admin.add_sql_command(
        {"sql_name": "Load assets", "db_type": "sqlserver",
         "script_path": "assets/tasks/sqlserver/load.sql", "input_type": "python",
         "input": {"script": "assets/tasks/python/read.py", "payload_format": "xml"},
         "parameters": [{"name": "payload", "type": "nvarchar(max)"}]},
        data_dir=estate / "data", tool_root=estate)
    add_command(estate, dict(SOURCE))   # a second command, json by default

    first, second = json.loads((estate / "data" / "sql_commands.json").read_text(encoding="utf-8"))["sql_commands"]
    assert first["input"]["payload_format"] == "xml"
    assert "payload_format" not in second["input"]
