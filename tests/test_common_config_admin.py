"""Registering a SQL task writes three files, and the runner has to be able to read all three.

The engine is ``db_ops.common.config_admin`` and its face is ``common.cli add-sql``. It was
reached through a ``db_ops.sql_tasks.config_admin`` shim until 2026-08-15 — this file was named
after the shim, and the shim was deleted: one command, one name.

What these tests hold is the round trip. ``add_sql_task`` writes the ``.sql``, appends to
``sql_commands.json`` and appends to ``sql_targets.json``; the assertions then load all three back
**through the runner's own loaders**, because a config the writer is happy with and the runner
cannot parse is the failure this pairing exists to catch.
"""

import json
from pathlib import Path

import pytest

from db_ops.common import config_admin
from db_ops.common.config_admin import ConfigAdminError, add_sql_task, next_sql_id, slugify
from db_ops.sql_tasks.runner import load_sql_commands, load_sql_targets, resolve_sql_file


def _seed(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    (data / "sql_commands.json").write_text(
        json.dumps({"sql_commands": [
            {"sql_id": 5, "sql_code": "SQLSERVER-005", "sql_name": "existing", "db_type": "sqlserver",
             "script_type": "single", "script_path": "assets/tasks/sqlserver/x/005_existing.sql",
             "version_from": "2017", "version_to": None, "active": True}
        ]}), encoding="utf-8")
    (data / "sql_targets.json").write_text(json.dumps({"sql_targets": []}), encoding="utf-8")
    return data


def test_slugify_is_file_safe():
    assert slugify("TANS Employee mapping!!") == "TANS_Employee_mapping"
    assert slugify("   ") == "sql"
    assert slugify("a/b\\c:d") == "a_b_c_d"


def test_next_sql_id_increments_from_max():
    assert next_sql_id({"sql_commands": [{"sql_id": 5}, {"sql_id": 9}]}) == 10
    assert next_sql_id({"sql_commands": []}) == 1


def test_add_sql_task_writes_file_and_config_and_runner_loads(tmp_path):
    data = _seed(tmp_path)
    result = add_sql_task(
        db_type="sqlserver", server_id="ACME-192-0-2-250", sql_name="Nightly cleanup",
        sql_text="DELETE FROM staging WHERE created < DATEADD(day,-7,GETDATE());",
        instance_name="APPDB", credential_name="cred_x",
        time_window={"from_hour": 20, "to_hour": 23, "repeat_interval": 3600, "timeout": 600},
        data_dir=data, tool_root=tmp_path,
    )
    assert result["ok"] and result["sql_id"] == 6 and result["active"] is True
    assert result["script_path"] == "assets/tasks/sqlserver/ACME_192_0_2_250/006_Nightly_cleanup.sql"

    # .sql file written with the content
    script_abs = tmp_path / result["script_path"]
    assert script_abs.exists()
    assert "DELETE FROM staging" in script_abs.read_text(encoding="utf-8")

    # runner can load the mutated config and resolve the new script
    commands = load_sql_commands(data / "sql_commands.json")
    assert 6 in commands
    cmd = commands[6]
    assert cmd.script_type == "single" and cmd.active is True and cmd.db_type == "sqlserver"
    # In production tool_root == runner.TOOL_ROOT, so resolve_sql_file finds it under assets/tasks.
    # Here we assert the written absolute path exists (runner path resolution is covered elsewhere).
    assert Path(result["script_abs"]).exists()
    assert cmd.script_files[0] == result["script_path"]

    targets = load_sql_targets(data / "sql_targets.json")
    new_targets = [t for t in targets if t.sql_id == 6]
    assert len(new_targets) == 1
    assert new_targets[0].time_window.repeat_interval == 3600


def test_add_sql_task_from_bytes_and_inactive(tmp_path):
    data = _seed(tmp_path)
    result = add_sql_task(
        db_type="postgresql", server_id="pg-01", sql_name="vacuum",
        sql_bytes="VACUUM ANALYZE;".encode("utf-8"), active=False,
        data_dir=data, tool_root=tmp_path,
    )
    assert result["active"] is False and result["db_type"] == "postgresql"
    # folder keyed by server_id only
    assert result["script_path"] == "assets/tasks/postgresql/pg_01/006_vacuum.sql"
    commands = json.loads((data / "sql_commands.json").read_text(encoding="utf-8"))
    entry = [c for c in commands["sql_commands"] if c["sql_id"] == result["sql_id"]][0]
    assert entry["active"] is False


def test_add_sql_task_rejects_bad_input(tmp_path):
    data = _seed(tmp_path)
    with pytest.raises(ConfigAdminError):
        add_sql_task(db_type="mongodb", server_id="s", sql_name="n", sql_text="x",
                     data_dir=data, tool_root=tmp_path)
    with pytest.raises(ConfigAdminError):
        add_sql_task(db_type="mysql", server_id="", sql_name="n", sql_text="x",
                     data_dir=data, tool_root=tmp_path)
    with pytest.raises(ConfigAdminError):  # both text and bytes
        add_sql_task(db_type="mysql", server_id="s", sql_name="n", sql_text="x",
                     sql_bytes=b"y", data_dir=data, tool_root=tmp_path)
    with pytest.raises(ConfigAdminError):  # empty content
        add_sql_task(db_type="mysql", server_id="s", sql_name="n", sql_text="   ",
                     data_dir=data, tool_root=tmp_path)


def test_add_sql_task_atomic_no_partial_on_bad_timewindow(tmp_path):
    data = _seed(tmp_path)
    before_cmds = (data / "sql_commands.json").read_text(encoding="utf-8")
    with pytest.raises(ConfigAdminError):
        add_sql_task(db_type="mysql", server_id="s", sql_name="n", sql_text="SELECT 1;",
                     time_window={"repeat_interval": -5}, data_dir=data, tool_root=tmp_path)
    # config unchanged and no stray .sql file
    assert (data / "sql_commands.json").read_text(encoding="utf-8") == before_cmds
    assert not list((tmp_path / "assets").rglob("*.sql"))


# --------------------------------------------------------------------------- #
# The CLI face: one JSON object out, success or failure
# --------------------------------------------------------------------------- #

def _run_cli(monkeypatch, capsys, argv: list[str], *, data_dir: Path) -> tuple[int, dict]:
    """Run ``config_admin.main`` and parse the one JSON object it prints.

    ``TOOL_ROOT`` is redirected as well as ``--data-dir``: the command writes the registered
    ``.sql`` under ``<tool root>/assets/tasks/``, and the CLI has no flag for that — without this
    the test would leave real files in the repository's own assets folder, which is exactly what
    the first run of it did.
    """
    monkeypatch.setattr(config_admin, "TOOL_ROOT", data_dir.parent)
    code = config_admin.main(argv)
    return code, json.loads(capsys.readouterr().out)


def test_add_sql_answers_in_the_response_envelope(tmp_path, monkeypatch, capsys):
    """Before 2026-08-15 both commands printed a bare result dict and, on failure, an ``ERROR:``
    line on **stderr** with exit 2 and nothing at all on stdout. A caller reading only stdout —
    which is every caller that reaches `common` through the CLI — could not tell a rejected task
    from a process that never started, so the Telegram app could not use this command until the
    answer became a response."""
    data = _seed(tmp_path)
    code, answer = _run_cli(monkeypatch, capsys, [
        "add-sql",
        json.dumps({"db_type": "mysql", "server_id": "s1", "sql_name": "nightly",
                    "sql_text": "SELECT 1;", "data_dir": str(data)}),
    ], data_dir=data)

    assert code == 0
    assert answer["success"] is True and answer["operation"] == "add-sql"
    assert answer["error"] is None
    assert answer["data"]["sql_code"] in answer["message"]
    assert answer["data"]["script_path"].endswith("_nightly.sql")


def test_a_refused_add_sql_is_a_response_not_an_exit_code(tmp_path, monkeypatch, capsys):
    data = _seed(tmp_path)
    code, answer = _run_cli(monkeypatch, capsys, [
        "add-sql",
        json.dumps({"db_type": "mongodb", "server_id": "s1", "sql_name": "n",
                    "sql_text": "SELECT 1;", "data_dir": str(data)}),
    ], data_dir=data)

    assert code == 1                       # the response says what happened; the code summarizes
    assert answer["success"] is False and answer["operation"] == "add-sql"
    assert "mongodb" in answer["error"]
    assert answer["data"] == {} and answer["metrics"] == {}


def test_an_unknown_field_is_refused_as_a_response_too(tmp_path, monkeypatch, capsys):
    """A misspelled key is invisible otherwise — the task registers with a setting silently
    missing. The refusal has to arrive the same way every other answer does."""
    data = _seed(tmp_path)
    code, answer = _run_cli(monkeypatch, capsys, [
        "add-sql",
        json.dumps({"db_type": "mysql", "server_id": "s1", "sql_name": "n",
                    "sql_text": "SELECT 1;", "sql_nmae": "typo", "data_dir": str(data)}),
    ], data_dir=data)

    assert code == 1 and answer["success"] is False
    assert "sql_nmae" in answer["error"]
