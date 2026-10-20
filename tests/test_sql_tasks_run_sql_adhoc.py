"""`sql_tasks run-sql-adhoc` - SQL that is not a task, the login resolved by the app, nothing kept unasked.

Reading one server's DMVs by hand took `common.cli run-sql` and a scratch wrapper: `run-sql` reads no
secret store (rules R09), so the wrapper decrypted the password itself. A script is a missing command,
and this one belongs to an app - the way a task's run does: the app finishes the request from `data/`
and calls `run-sql` over the transport, the password on stdin.

What is held here, with no database: the request is refused rather than half-read (a misspelt
`comit` would roll back what the operator meant to keep and still say success); a read is rolled back
and asks nobody; a write asks one typed `yes` first and runs nothing when refused; `run-sql`'s answer
reaches the caller as `run-sql` gave it; a rehearsal never shows the password.
"""

from __future__ import annotations

import json

import pytest

from dbabrain.lib import common_cli as lib_common_cli
from dbabrain.lib.data_sources import request_fill
from dbabrain.sql_tasks import adhoc, runner
from dbabrain.transport import common_cli
from dbabrain.transport.process import ProcessResult

SERVER = "ACME-192-0-2-10-MSSQL-1433"
PASSWORD = "s3cret-not-for-stdout"


@pytest.fixture
def calls(monkeypatch):
    """The transport faked: `authorize` answers as the test sets it, `run-sql` answers an envelope."""
    seen: dict = {"spawn": [], "authorize": [], "authorized": True}

    def fill(command, request, **_kwargs):
        filled = dict(request)
        if command == "run-sql":
            filled["connection"] = {"db_type": "sqlserver", "host": "192.0.2.10", "port": 1433,
                                    "username": "dba", "password": PASSWORD}
        return filled

    def run_allowing_failure(command, request, **_kwargs):
        assert command == "authorize"
        seen["authorize"].append(request)
        if seen["authorized"]:
            return True, {"gates": [{"name": "confirm", "status": "OK"}]}, ""
        return False, {"gates": [{"name": "confirm", "status": "FAIL", "detail": "not confirmed"}]}, ""

    def spawn(command, request, **_kwargs):
        seen["spawn"].append((command, request))
        return ProcessResult(returncode=0, stderr="",
                             stdout='{"success": true, "operation": "run-sql"}\n'), ""

    monkeypatch.setattr(request_fill, "fill_request", fill)
    monkeypatch.setattr(common_cli, "run_allowing_failure", run_allowing_failure)
    monkeypatch.setattr(common_cli, "spawn", spawn)
    return seen


def _run(capsys, request: dict, *, dry_run: bool = False, tmp_path=None) -> tuple[int, str, str]:
    code = adhoc.main(json.dumps(request), data_dir=tmp_path, dry_run=dry_run)
    out, err = capsys.readouterr()
    return code, out, err


# --------------------------------------------------------------------------- #
# The request: everything named, nothing guessed
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("request_, reason", [
    ({"server_id": SERVER, "sql_text": "UPDATE t SET x = 1", "comit": True}, "unknown field"),
    ({"sql_text": "SELECT 1"}, "server_id is required"),
    ({"server_id": SERVER}, "exactly one of sql_text and sql_file"),
    ({"server_id": SERVER, "sql_text": "SELECT 1", "sql_file": "q.sql"}, "exactly one"),
    ({"server_id": SERVER, "sql_text": "SELECT 1", "commit": "true"}, "true or false"),
])
def test_a_request_that_cannot_be_read_as_written_is_refused(request_, reason):
    with pytest.raises(adhoc.AdhocRequestError, match=reason):
        adhoc.checked(request_)


def test_the_sql_file_is_read_by_the_app_so_run_sql_receives_the_whole_of_what_runs(tmp_path):
    (tmp_path / "q.sql").write_text("SELECT name FROM sys.databases;", encoding="utf-8")

    request = adhoc.checked({"server_id": SERVER, "sql_file": "@q.sql"}, cwd=tmp_path)

    assert request["sql_text"] == "SELECT name FROM sys.databases;"
    assert "sql_file" not in request


def test_an_empty_or_missing_sql_file_is_refused(tmp_path):
    (tmp_path / "empty.sql").write_text("  \n", encoding="utf-8")
    with pytest.raises(adhoc.AdhocRequestError, match="is empty"):
        adhoc.checked({"server_id": SERVER, "sql_file": "empty.sql"}, cwd=tmp_path)
    with pytest.raises(adhoc.AdhocRequestError, match="sql_file"):
        adhoc.checked({"server_id": SERVER, "sql_file": "missing.sql"}, cwd=tmp_path)


def test_the_run_sql_request_states_the_rollback_and_passes_run_sqls_own_options():
    built = adhoc.run_sql_request(adhoc.checked({
        "server_id": SERVER, "database": "master", "credential_name": "lab_sa",
        "sql_text": "SELECT 1", "format": "csv", "max_rows": 10, "timeout_seconds": 5,
        "confirm": "yes", "reason": "reading", "assume_yes": False,
    }))

    assert built == {"target": SERVER, "sql_text": "SELECT 1", "database_name": "master",
                     "credential_name": "lab_sa", "commit": False, "autocommit": False,
                     "format": "csv", "max_rows": 10, "timeout_seconds": 5}


# --------------------------------------------------------------------------- #
# A read, a write, a refusal
# --------------------------------------------------------------------------- #
def test_a_read_asks_nobody_and_answers_with_run_sqls_own_answer(calls, capsys, tmp_path):
    code, out, _err = _run(capsys, {"server_id": SERVER, "sql_text": "SELECT 1"}, tmp_path=tmp_path)

    assert code == 0
    assert out == '{"success": true, "operation": "run-sql"}\n'
    assert calls["authorize"] == []
    [(command, request)] = calls["spawn"]
    assert command == "run-sql"
    assert request["commit"] is False and request["autocommit"] is False
    assert request["connection"]["password"] == PASSWORD


@pytest.mark.parametrize("flag", ["commit", "autocommit"])
def test_a_write_asks_for_one_typed_yes_before_anything_runs(calls, capsys, tmp_path, flag):
    code, _out, _err = _run(capsys, {"server_id": SERVER, "sql_text": "UPDATE t SET x = 1",
                                     flag: True, "confirm": "yes"}, tmp_path=tmp_path)

    assert code == 0
    [gate] = calls["authorize"]
    assert gate["operation"] == "run-sql-adhoc"
    assert gate["target_id"] == SERVER
    assert gate["confirm"] == "yes"
    assert len(calls["spawn"]) == 1


def test_a_refused_write_runs_nothing(calls, capsys, tmp_path):
    calls["authorized"] = False

    code, out, _err = _run(capsys, {"server_id": SERVER, "sql_text": "DELETE FROM t",
                                    "commit": True, "confirm": True}, tmp_path=tmp_path)
    answer = json.loads(out)

    assert code == 1
    assert calls["spawn"] == []
    # An answer on stdout like every other, so a caller parsing it reads the refusal.
    assert answer["success"] is False and answer["error_kind"] == "refused"
    assert "Nothing ran" in answer["error"]
    assert calls["authorize"][0]["confirm"] is True, "intent alone - the terminal still asks"


def test_the_write_is_priced_on_the_ladder_of_the_data_dir_the_login_came_from(calls, capsys,
                                                                                monkeypatch, tmp_path):
    dirs = []
    fill = request_fill.fill_request

    def recording(command, request, **kwargs):
        dirs.append((command, kwargs.get("data_dir")))
        return fill(command, request, **kwargs)

    monkeypatch.setattr(request_fill, "fill_request", recording)
    _run(capsys, {"server_id": SERVER, "sql_text": "UPDATE t SET x = 1", "commit": True,
                  "confirm": "yes"}, tmp_path=tmp_path)

    assert dirs == [("run-sql", tmp_path), ("authorize", tmp_path)]


def test_a_sql_file_that_is_not_utf8_is_refused_with_an_answer_not_a_traceback(calls, capsys, tmp_path,
                                                                               monkeypatch):
    (tmp_path / "q.sql").write_bytes("SELECT 'Hà Nội'".encode("utf-16-le"))
    monkeypatch.chdir(tmp_path)

    code, out, _err = _run(capsys, {"server_id": SERVER, "sql_file": "q.sql"}, tmp_path=tmp_path)
    answer = json.loads(out)

    assert code == 1
    assert answer["error_kind"] == "request" and "sql_file" in answer["error"]
    assert calls["spawn"] == []


def test_a_rehearsal_resolves_the_login_but_never_shows_the_password(calls, capsys, tmp_path):
    code, out, _err = _run(capsys, {"server_id": SERVER, "sql_text": "UPDATE t SET x = 1",
                                    "commit": True}, dry_run=True, tmp_path=tmp_path)

    assert code == 0
    assert PASSWORD not in out
    shown = json.loads(out)
    assert shown["writes"] is True
    assert shown["request"]["connection"]["password"] == "***"
    assert calls["spawn"] == [] and calls["authorize"] == []


def test_a_request_that_is_not_an_object_answers_the_envelope(calls, capsys, tmp_path):
    code = adhoc.main("[1, 2]", data_dir=tmp_path)
    answer = json.loads(capsys.readouterr().out)

    assert code == 1
    assert answer["success"] is False and answer["error_kind"] == "request"
    assert calls["spawn"] == []


def test_the_password_travels_on_stdin_never_on_the_command_line():
    spec = lib_common_cli.build_command("run-sql", {"connection": {"password": PASSWORD}})

    assert PASSWORD not in " ".join(spec.argv)
    assert PASSWORD.encode() in spec.stdin


def test_the_runner_routes_the_command_without_opening_the_store(calls, capsys, monkeypatch):
    def no_store(*_args, **_kwargs):
        raise AssertionError("run-sql-adhoc opened the runtime store")

    def no_config(*_args, **_kwargs):
        raise OSError("no configuration in this test")

    monkeypatch.setattr(runner.DbaBrainStore, "from_config", no_store)
    # The log line is not what runs the SQL: without a configuration the run still goes ahead.
    monkeypatch.setattr(runner, "load_config", no_config)

    code = runner.main(["run-sql-adhoc", json.dumps({"server_id": SERVER, "sql_text": "SELECT 1"})])

    assert code == 0
    assert len(calls["spawn"]) == 1
    assert "no log for this run" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# What a write costs
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("relative", ["dbabrain/common/catalogue/emergency_operations.json",
                                      "data/emergency_operations.example.json"])
def test_every_shipped_ladder_prices_a_write_at_one_typed_yes(relative):
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / relative
    operations = json.loads(path.read_text(encoding="utf-8-sig"))["operations"]

    assert operations[adhoc.WRITE_OPERATION]["level"] == 50
