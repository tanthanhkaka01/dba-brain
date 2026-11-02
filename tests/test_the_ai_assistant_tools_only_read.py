"""The assistant's tools only read, and nothing secret leaves through them (0.34.0, 1.260).

The operator, 2026-10-31: *read only for now.* The model chooses what to call and with what, and a
row, a log line or a file it reads can tell it anything - so the limit is held by the tool, not by
the prompt: a statement that writes is refused before it runs, a path outside the readable roots or
on the denied list is refused, and every value of the encrypted secret store is taken out of every
result before the model or the store sees it.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from dbabrain.db.backend import StoreTarget
from dbabrain.lib import ai_assist as A
from dbabrain.telegram import ai_tools as T


@pytest.mark.parametrize("sql", [
    "DELETE FROM job_runs",
    "SELECT 1; DROP TABLE job_runs",
    "UPDATE x SET a = 1",
    "SELECT * INTO copy_of FROM job_runs",
    "WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x",
    "SELECT pg_terminate_backend(123)",
    "EXEC sp_who2",
    "SELECT 1 KILL 55",
    "  ",
])
def test_a_statement_that_does_more_than_read_is_refused(sql):
    with pytest.raises(T.ReadOnlyRefusal):
        T.read_only_sql(sql)


@pytest.mark.parametrize("sql", [
    "SELECT job_code, status FROM job_runs WHERE message LIKE '%DELETE%' AND updated_at > '2026'",
    "with recent as (select * from job_runs) select count(*) from recent;",
    "SELECT REPLACE(name, '-', '_') FROM sys.databases -- drop me",
])
def test_a_plain_read_goes_whatever_its_strings_and_columns_say(sql):
    assert T.read_only_sql(sql)


def test_a_blocked_table_is_refused():
    with pytest.raises(T.ReadOnlyRefusal, match="web_users"):
        T.read_only_sql("SELECT * FROM web_users", blocked_tables=["web_users"])


def _context(tmp_path: Path, **changes) -> T.ToolContext:
    document = A.shipped_document()
    document.update(changes)
    settings = A.parse_ai_assist(document)
    db = tmp_path / "store.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE job_runs (job_code TEXT, status TEXT)")
        conn.executemany("INSERT INTO job_runs VALUES (?, ?)", [("A", "DONE"), ("B", "ERROR"), ("C", "DONE")])
        conn.execute("CREATE TABLE web_users (name TEXT, password_hash TEXT)")
    (tmp_path / "data").mkdir()
    (tmp_path / "logs").mkdir()
    (tmp_path / "data" / "db_instances.json").write_text('{"x": "token=abc"}', encoding="utf-8")
    (tmp_path / "data" / "encrypted_secret_text.json").write_text("{}", encoding="utf-8")
    (tmp_path / "logs" / "metrics.log").write_text("ok\nERROR connect to db with stored-value-kept-out-41\nok\n",
                                                   encoding="utf-8")
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "secret_text.json").write_text("{}", encoding="utf-8")
    return T.ToolContext(settings=settings, store_target=StoreTarget.coerce(str(db)), tool_root=tmp_path,
                         commands_path=tmp_path / "data" / "telegram_support_commands.json",
                         users_path=tmp_path / "data" / "telegram_users.json", asker_user_id="1",
                         redact=T.Redactor({"STORE_REF": "stored-value-kept-out-41"}))


def test_store_query_reads_and_caps_its_rows(tmp_path):
    context = _context(tmp_path, store_query_max_rows=2)
    output = T.run_tool(context, "store_query", {"sql": "SELECT job_code, status FROM job_runs ORDER BY job_code"})
    data = json.loads(output.content)
    assert not output.is_error
    assert data["rows"] == [["A", "DONE"], ["B", "ERROR"]] and data["truncated"] is True


def test_store_query_cannot_write_even_past_the_check(tmp_path):
    context = _context(tmp_path)
    # The guard refuses it by word; the read-only connection is the second line.
    output = T.run_tool(context, "store_query", {"sql": "INSERT INTO job_runs VALUES ('X', 'DONE')"})
    assert output.is_error
    with sqlite3.connect(tmp_path / "store.sqlite") as conn:
        assert conn.execute("SELECT COUNT(*) FROM job_runs").fetchone()[0] == 3


def test_store_schema_leaves_out_the_blocked_tables(tmp_path):
    data = json.loads(T.run_tool(_context(tmp_path), "store_schema", {}).content)
    assert "job_runs" in data["tables"] and "web_users" not in data["tables"]


def test_a_file_outside_the_roots_or_on_the_denied_list_is_refused(tmp_path):
    context = _context(tmp_path)
    for path in ("data/encrypted_secret_text.json", "secrets/secret_text.json", "../outside.txt", "/etc/passwd"):
        output = T.run_tool(context, "read_file", {"path": path})
        assert output.is_error, path


def test_a_secret_in_a_log_never_reaches_the_model(tmp_path):
    context = _context(tmp_path)
    output = T.run_tool(context, "grep_files", {"pattern": "ERROR", "path": "logs"})
    assert "stored-value-kept-out-41" not in output.content and "***" in output.content
    read = T.run_tool(context, "read_file", {"path": "data/db_instances.json"})
    assert "abc" not in read.content, "a token=... shape is masked even when its value is not in the store"


def test_list_files_hides_what_is_denied(tmp_path):
    data = json.loads(T.run_tool(_context(tmp_path), "list_files", {"path": "data"}).content)
    names = [entry["name"] for entry in data["entries"]]
    assert "db_instances.json" in names and "encrypted_secret_text.json" not in names


def test_tools_mode_off_offers_no_tool_and_runs_none(tmp_path):
    context = _context(tmp_path, tools_mode="off")
    assert T.tool_definitions(context.settings) == []
    assert T.run_tool(context, "store_query", {"sql": "SELECT 1"}).is_error


def test_a_switched_off_tool_is_neither_offered_nor_run(tmp_path):
    context = _context(tmp_path, run_sql_enabled=False)
    assert "run_sql" not in [tool["name"] for tool in T.tool_definitions(context.settings)]
    assert T.run_tool(context, "run_sql", {"server_id": "X", "sql": "SELECT 1"}).is_error


def test_run_sql_refuses_a_server_off_its_list_before_connecting(tmp_path):
    context = _context(tmp_path, run_sql_server_ids=["SQLSERVER-A"])
    output = T.run_tool(context, "run_sql", {"server_id": "SQLSERVER-B", "sql": "SELECT 1"})
    assert output.is_error and "may not reach SQLSERVER-B" in output.content


def test_a_long_result_is_cut_and_says_so():
    text = T.for_the_model(T.ToolOutput("x" * 5000), 1000)
    assert len(text) < 1200 and "cut: 4000 more characters" in text


def test_the_tool_list_never_changes_between_questions(tmp_path):
    """It is part of the cached prefix: the same settings give the same bytes."""
    context = _context(tmp_path)
    assert json.dumps(T.tool_definitions(context.settings)) == json.dumps(T.tool_definitions(context.settings))


# --------------------------------------------------------------------------- #
# Found by the read-only review of 2026-10-31 (Gemini), each one checked and held here.
# --------------------------------------------------------------------------- #
def test_a_blocked_table_named_in_double_quotes_is_still_refused():
    """A double-quoted name is an identifier: "web_users" is that table, not a string."""
    with pytest.raises(T.ReadOnlyRefusal, match="web_users"):
        T.read_only_sql('SELECT * FROM "web_users"', blocked_tables=["web_users"])


def test_a_comment_mysql_executes_is_refused():
    """MySQL runs the text of /*! ... */ - taking comments out would hide it from every check."""
    with pytest.raises(T.ReadOnlyRefusal, match="executed by MySQL"):
        T.read_only_sql("SELECT 1 /*!50000 , (SELECT 1) */")
    assert T.read_only_sql("SELECT '/*! not a comment */' AS x")


def test_grep_on_the_tool_root_searches_the_readable_roots_only(tmp_path):
    context = _context(tmp_path)
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("needle outside the roots\n", encoding="utf-8")
    (tmp_path / "logs" / "x.log").write_text("needle inside\n", encoding="utf-8")
    for path in ("", ".", "/", "./"):
        output = T.run_tool(context, "grep_files", {"pattern": "needle", "path": path})
        assert "inside" in output.content and "outside" not in output.content, path
    assert T.run_tool(context, "read_file", {"path": "."}).is_error


def test_the_plaintext_secret_source_is_denied_wherever_it_lies(tmp_path):
    context = _context(tmp_path)
    (tmp_path / "data" / "secret_text.json").write_text('{"A": "x"}', encoding="utf-8")
    assert T.run_tool(context, "read_file", {"path": "data/secret_text.json"}).is_error
    assert "secret_text.json" not in T.run_tool(context, "list_files", {"path": "data"}).content


def test_bot_commands_gives_what_a_one_line_command_needs_without_the_menu_file(tmp_path):
    """The worker carries the commands' JSON, not the BotFather menu: a command still says what it
    does, and each argument its position and whether it takes the rest of the line."""
    context = _context(tmp_path)
    (tmp_path / "data" / "telegram_users.json").write_text(
        json.dumps({"telegram_users": [{"user_id": "1", "user_type": 100, "active": True}]}), encoding="utf-8")
    (tmp_path / "data" / "telegram_support_commands.json").write_text(json.dumps({"telegram_support_commands": [{
        "command_id": 20, "command_text": "spbot_run_sql_task", "command_type": 50, "reply_default": 0,
        "reply_text": "", "is_group": 1, "is_private": 1, "need_file": 0, "action_type": "cli_execute",
        "action_config": {"start_text": "SQL task #{sql_id} started", "parameters": [
            {"name": "sql_id", "source": "arg", "position": 1, "required": True, "prompt_text": "Which sql_id?"},
            {"name": "task_params", "source": "arg", "position": 3, "consume_rest": True,
             "prompt_text": "This task takes its parameters in order"},
            {"name": "confirm", "source": "arg", "position": 2, "required": True, "pattern": "yes"}]}}]}),
        encoding="utf-8")
    data = json.loads(T.run_tool(context, "bot_commands", {"find": "parameters in order"}).content)
    command = data["commands"][0]
    assert command["description"] == "SQL task #{sql_id} started"
    by_name = {item["name"]: item for item in command["parameters"]}
    assert by_name["task_params"]["position"] == 3 and by_name["task_params"]["takes_rest_of_line"] is True
    assert by_name["confirm"]["pattern"] == "yes"
    assert command["asker_may_run_in_private_chat"] is True

