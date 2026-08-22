"""The bot commands that **write** to a production row, and the one field that keeps them working.

`/spbot_update_allow_re_inspect` and its siblings run a `.sql` file from
`assets/sql_telegram_commands/` with arguments typed into Telegram. They are `sql_execute`
commands, and they are the only Telegram path that changes data on purpose.

Two things about them are easy to break and impossible to notice:

* **`commit` must be true.** `common.cli run-sql` rolls back unless asked, because it is also the
  read-only engine behind `/spbot_sql_to_xlsx`. Sending these without `commit` would make every
  one of them a no-op that still replies "done" — the update would appear to work, for months.
* **The arguments must be bound, never pasted.** They arrive in a chat message. Until 2026-08-15
  `run-sql` could not bind at all, which is why this command opened its own connection; the
  conversion to the CLI was blocked on that missing field, not on anything about the command.

`row_count` is the third: these report **affected** rows, and `run-sql`'s `row_count` is the rows
a SELECT returned — 0 for every one of them.

There was no test here before the conversion. That is the reason there is one now.
"""

from __future__ import annotations

import pytest

from db_ops.lib import common_cli
from db_ops.common import data_sources
from db_ops.telegram import sql_commands


class _Command:
    def __init__(self, action_config):
        self.action_config = action_config


def _config(**overrides):
    return {
        "db_type": "sqlserver",
        "server_id": "ACME-192-0-2-111",
        "service_name": "APPDB-PROD",
        "instance_name": "MSSQLSERVER",
        "credential_name": "sqlserver_100.111_dba",
        "database_name": "APPDB_Testing",
        "sql_file": "update_flag.sql",
        "parameters": [{"name": "ticket_id", "position": 1, "required": True}],
        **overrides,
    }


@pytest.fixture
def stub(monkeypatch, tmp_path):
    """A resolvable target, a real .sql file, and a recorded `run-sql` call."""
    sql_file = tmp_path / "update_flag.sql"
    sql_file.write_text("UPDATE Ticket SET AllowReInspect = 1 WHERE Id = ?;", encoding="utf-8")
    monkeypatch.setattr(sql_commands, "resolve_telegram_sql_file", lambda name: sql_file)
    monkeypatch.setattr(data_sources, "load_inventory", lambda *a, **k: [{
        "server_id": "ACME-192-0-2-111", "ip": "192.0.2.111", "company_code": "ACME",
        "databases": [{"db_type": "sqlserver", "service_name": "APPDB-PROD",
                       "instance_name": "MSSQLSERVER", "database_name": "APPDB_Testing"}],
    }])

    seen: dict = {}

    def fake_run(command, request, **_kwargs):
        assert command == "run-sql"
        seen.clear()
        seen.update(request)
        return True, {"ok": True, "columns": [], "rows": [], "row_count": 0, "affected_rows": 1,
                      "truncated": False, "committed": True}, ""

    monkeypatch.setattr(common_cli, "run_allowing_failure", fake_run)
    return seen


def test_a_write_command_commits(stub):
    """Without this the command reports success and changes nothing."""
    sql_commands.execute_sql_support_command(command=_Command(_config()), args=["4711"])

    assert stub["commit"] is True


def test_the_chat_argument_is_bound_not_pasted(stub):
    sql_commands.execute_sql_support_command(command=_Command(_config()), args=["4711"])

    assert stub["params"] == ["4711"]
    assert "4711" not in stub["sql"]


def test_the_target_database_and_login_come_from_the_command_config(stub):
    """A support command names exactly which instance, database and login it runs as — none of
    the three is inferred, because the SQL writes."""
    sql_commands.execute_sql_support_command(command=_Command(_config()), args=["4711"])

    assert stub["target"] == "ACME-192-0-2-111"
    assert stub["database"] == "APPDB_Testing"
    assert stub["credential_name"] == "sqlserver_100.111_dba"


def test_it_reports_the_rows_it_changed_not_the_rows_it_returned(stub):
    result = sql_commands.execute_sql_support_command(
        command=_Command(_config()), args=["4711"])

    assert result["ok"] is True
    assert result["row_count"] == 1          # affected_rows, not run-sql's row_count (0)
    assert result["parameter_count"] == 1


def test_a_missing_required_argument_is_refused_before_anything_runs(stub):
    with pytest.raises(RuntimeError, match="Missing required argument: ticket_id"):
        sql_commands.execute_sql_support_command(command=_Command(_config()), args=[])
    assert stub == {}                        # no request was built


def test_a_failed_run_is_raised_with_its_reason(monkeypatch, stub):
    monkeypatch.setattr(common_cli, "run_allowing_failure", lambda command, request, **_kw: (
        False, {}, "Invalid column name 'AllowReInspect'."))

    with pytest.raises(RuntimeError, match="AllowReInspect"):
        sql_commands.execute_sql_support_command(command=_Command(_config()), args=["4711"])


def test_a_non_sqlserver_command_is_refused(stub):
    """These `.sql` files are T-SQL. The refusal predates the CLI conversion and is kept."""
    with pytest.raises(RuntimeError, match="Unsupported Telegram SQL command db_type"):
        sql_commands.execute_sql_support_command(
            command=_Command(_config(db_type="oracle")), args=["4711"])
