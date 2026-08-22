import json
import re
import sqlite3
import zipfile
from html import unescape

import pytest

from db_ops.common.sql_run import execute_capture_first
from db_ops.db import DbOpsStore
from db_ops.lib import common_cli
from db_ops.telegram import command_processor, sql_commands
from db_ops.telegram.commands import can_run_command
from db_ops.telegram.command_processor import process_one_command_message
from db_ops.telegram.sql_commands import SqlToXlsxError, run_sql_to_xlsx
from db_ops.lib.xlsx_export import MAX_CELL_TEXT, write_result_set_xlsx


# --------------------------------------------------------------------------- #
# Level-based permission gate (allow_command >= command_type AND user_type >= command_type)
# --------------------------------------------------------------------------- #


def test_can_run_command_tiers_1_and_2():
    # command_type 1: any clearance >= 1 on both axes runs.
    assert can_run_command(allow_command=1, user_type=1, command_type=1) is True
    assert can_run_command(allow_command=2, user_type=1, command_type=1) is True
    # command_type 2: both axes must reach level 2.
    assert can_run_command(allow_command=2, user_type=2, command_type=2) is True
    assert can_run_command(allow_command=1, user_type=2, command_type=2) is False
    assert can_run_command(allow_command=2, user_type=1, command_type=2) is False
    # A tier-1 command still needs clearance >= 1 on each axis.
    assert can_run_command(allow_command=0, user_type=2, command_type=1) is False
    assert can_run_command(allow_command=2, user_type=0, command_type=1) is False


def test_can_run_command_public_tier_zero_runs_for_everyone():
    # command_type == 0 is public: runs regardless of user/group clearance (even 0).
    assert can_run_command(allow_command=0, user_type=0, command_type=0) is True
    assert can_run_command(allow_command=2, user_type=2, command_type=0) is True


def test_can_run_command_negative_is_disabled():
    # Only a negative command_type disables the command; -1 is the "off" switch.
    assert can_run_command(allow_command=2, user_type=2, command_type=-1) is False
    assert can_run_command(allow_command=100, user_type=100, command_type=-1) is False


@pytest.mark.parametrize("level", [3, 4, 10, 100])
def test_can_run_command_supports_arbitrary_higher_levels(level):
    # Exactly at the required level: runs.
    assert can_run_command(allow_command=level, user_type=level, command_type=level) is True
    # Higher clearance than required: still runs.
    assert can_run_command(allow_command=level + 5, user_type=level + 1, command_type=level) is True
    # One notch short on either axis: refused.
    assert can_run_command(allow_command=level - 1, user_type=level, command_type=level) is False
    assert can_run_command(allow_command=level, user_type=level - 1, command_type=level) is False


# --------------------------------------------------------------------------- #
# XLSX writer
# --------------------------------------------------------------------------- #


def test_write_result_set_xlsx_produces_a_valid_workbook(tmp_path):
    path = tmp_path / "out.xlsx"
    write_result_set_xlsx(
        path,
        columns=["Id", "Name", "Amount"],
        rows=[[1, "Alice & Co <x>", 12.5], [2, None, 0]],
    )
    assert path.exists()
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        assert {"[Content_Types].xml", "_rels/.rels", "xl/workbook.xml", "xl/worksheets/sheet1.xml"} <= names
        sheet = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
    # Header present, XML-escaped text, numeric cell kept numeric.
    assert "<t xml:space=\"preserve\">Id</t>" in sheet
    assert "Alice &amp; Co &lt;x&gt;" in sheet
    assert "<v>1</v>" in sheet  # numeric, not inline string
    assert "<v>12.5</v>" in sheet


def test_write_result_set_xlsx_with_no_rows(tmp_path):
    path = tmp_path / "empty.xlsx"
    write_result_set_xlsx(path, columns=["A", "B"], rows=[])
    with zipfile.ZipFile(path) as archive:
        sheet = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
    assert "<row r=\"1\">" in sheet  # header row only


def test_cell_text_is_clamped_to_excels_limit(tmp_path):
    """A string over 32,767 chars makes Excel declare the workbook damaged and DROP it
    ('Repaired Records: String properties'). Real Query Store exports hit this: one
    query_plan cell was 1.6 M characters."""
    path = tmp_path / "huge.xlsx"
    plan_xml = "<ShowPlanXML>" + ("x" * 2_000_000) + "</ShowPlanXML>"
    written = write_result_set_xlsx(path, columns=["plan_id", "query_plan"], rows=[[1, plan_xml]])

    assert written.truncated_cells == 1
    assert written.path == path
    with zipfile.ZipFile(path) as archive:
        sheet = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
    texts = re.findall(r'<t xml:space="preserve">(.*?)</t>', sheet, re.S)
    # Excel counts the decoded characters, not the escaped XML, so unescape before measuring.
    decoded = [unescape(text) for text in texts]
    assert max(len(text) for text in decoded) <= MAX_CELL_TEXT
    assert any(text.endswith("…[truncated]") for text in decoded)


def test_cell_text_at_the_limit_is_left_alone(tmp_path):
    path = tmp_path / "exact.xlsx"
    written = write_result_set_xlsx(path, columns=["t"], rows=[["y" * MAX_CELL_TEXT]])
    assert written.truncated_cells == 0


def test_write_result_set_xlsx_drops_xml_illegal_characters(tmp_path):
    path = tmp_path / "control.xlsx"
    write_result_set_xlsx(path, columns=["t"], rows=[["a\x00b\x0cc￿d\te"]])
    with zipfile.ZipFile(path) as archive:
        sheet = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
    assert "abcd\te" in sheet  # NUL, form feed and U+FFFF gone; tab kept


# --------------------------------------------------------------------------- #
# Result-set capture / SELECT-only enforcement
# --------------------------------------------------------------------------- #


class FakeCursor:
    """Scriptable DB-API cursor over a list of (description, rows, rowcount) result sets."""

    def __init__(self, result_sets):
        self._sets = result_sets
        self._index = -1
        self.description = None
        self.rowcount = -1
        self._rows = []
        self._pos = 0

    def execute(self, sql):  # noqa: ARG002 - the fake ignores the SQL text.
        self._index = 0
        self._load()

    def _load(self):
        if 0 <= self._index < len(self._sets):
            description, rows, rowcount = self._sets[self._index]
            self.description = description
            self.rowcount = rowcount
            self._rows = list(rows)
            self._pos = 0
        else:
            self.description = None
            self.rowcount = -1
            self._rows = []

    def fetchmany(self, size):
        chunk = self._rows[self._pos : self._pos + size]
        self._pos += len(chunk)
        return chunk

    def nextset(self):
        self._index += 1
        if self._index < len(self._sets):
            self._load()
            return True
        return False


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return self._cursor

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def _select_desc(*names):
    return [(name,) for name in names]


def test_capture_first_result_set_for_select():
    cursor = FakeCursor([(_select_desc("Id", "Name"), [[1, "a"], [2, "b"]], -1)])
    columns, rows, affected, truncated = execute_capture_first(cursor, "SELECT ...", max_rows=100)
    assert columns == ["Id", "Name"]
    assert rows == [[1, "a"], [2, "b"]]
    assert affected == 0
    assert truncated is False


def test_capture_counts_affected_rows_for_dml():
    cursor = FakeCursor([(None, [], 7)])  # UPDATE affecting 7 rows -> no result set
    columns, rows, affected, truncated = execute_capture_first(cursor, "UPDATE ...", max_rows=100)
    assert columns == []
    assert rows == []
    assert affected == 7


def test_capture_truncates_beyond_max_rows():
    big = [[n] for n in range(10)]
    cursor = FakeCursor([(_select_desc("N"), big, -1)])
    columns, rows, affected, truncated = execute_capture_first(cursor, "SELECT ...", max_rows=4)
    assert len(rows) == 4
    assert truncated is True
    assert affected == 0


# --------------------------------------------------------------------------- #
# run_sql_to_xlsx — what Telegram adds on top of `common.cli run-sql`
# --------------------------------------------------------------------------- #
#
# Stubbed at the CLI boundary since 2026-08-15. Before that these tests patched
# `sql_run.resolve_sqlserver_target` / `connect_target` and asserted the connection was rolled
# back — but the rollback is the *engine's* contract, it is held by
# `tests/test_common_sql_run.py` against the engine itself, and it now happens in another process
# where no fake connection of ours exists. What is genuinely this wrapper's, and all that is left
# here, is: the request it composes, and the one rule it adds — a workbook with no sheet is not a
# deliverable, so a script that returns no result set is an error.

def _patch_run_sql(monkeypatch, answer):
    """Answer ``common.cli run-sql`` with ``answer``; hand back the request it was sent."""
    seen: dict = {}

    def fake_run(command, request, **_kwargs):
        assert command == "run-sql"
        seen.clear()
        seen.update(request)
        resolved = dict(answer)
        resolved.setdefault("database", request.get("database") or "master")
        resolved.setdefault("credential_name", request.get("credential_name") or "default_cred")
        return True, resolved, ""

    monkeypatch.setattr(common_cli, "run_allowing_failure", fake_run)
    return seen


def _answer(**overrides):
    base = {"ok": True, "server_id": "ACME-resolved", "username": "svc_user",
            "columns": ["Id"], "rows": [[1]], "row_count": 1, "affected_rows": 0,
            "truncated": False}
    return {**base, **overrides}


def test_run_sql_to_xlsx_no_result_set_raises(monkeypatch):
    """A write ran, nothing came back. The engine is happy; an export is not."""
    _patch_run_sql(monkeypatch, _answer(columns=[], rows=[], row_count=0, affected_rows=3))

    with pytest.raises(SqlToXlsxError, match="no result set to export"):
        run_sql_to_xlsx(target="ACME-x", sql_text="DELETE FROM t")


def test_run_sql_to_xlsx_allows_temp_table_then_select(monkeypatch):
    # "SELECT ... INTO #tmp" (affected rows) followed by a final SELECT is a legit report shape.
    _patch_run_sql(monkeypatch, _answer(rows=[[1], [2]], row_count=2, affected_rows=27))

    result = run_sql_to_xlsx(target="ACME-x", sql_text="SELECT ... INTO #t ...; SELECT Id FROM #t")
    assert result["columns"] == ["Id"]
    assert result["row_count"] == 2
    assert result["affected_rows"] == 27  # reported, not rejected


def test_run_sql_to_xlsx_returns_first_result_set(monkeypatch):
    seen = _patch_run_sql(monkeypatch, _answer(rows=[[1], [2], [3]], row_count=3,
                                               database="AppDb"))

    result = run_sql_to_xlsx(target="mssql 192.0.2.248", sql_text="SELECT Id FROM t")
    assert seen["target"] == "mssql 192.0.2.248" and seen["sql"] == "SELECT Id FROM t"
    assert result["columns"] == ["Id"]
    assert result["row_count"] == 3
    assert result["affected_rows"] == 0
    assert result["database"] == "AppDb"
    assert result["server_id"] == "ACME-resolved"


def test_run_sql_to_xlsx_database_argument_pins_the_database(monkeypatch):
    # The command may pin the database so the SQL does not have to open with "USE <db>;".
    seen = _patch_run_sql(monkeypatch, _answer())

    result = run_sql_to_xlsx(target="ACME-x", sql_text="SELECT Id FROM t", database="SALESDB")
    assert seen["database"] == "SALESDB"       # it reaches the command...
    assert result["database"] == "SALESDB"     # ...and the command's answer is what is reported


def test_run_sql_to_xlsx_reports_which_login_ran_it(monkeypatch):
    # Nothing in the command names a user, so the reply/log must say which one was used.
    _patch_run_sql(monkeypatch, _answer())

    assert run_sql_to_xlsx(target="ACME-x", sql_text="SELECT 1")["credential_name"] == "default_cred"
    picked = run_sql_to_xlsx(target="ACME-x", sql_text="SELECT 1", credential_name="readonly")
    assert picked["credential_name"] == "readonly"


def test_a_failed_run_sql_reaches_the_operator_with_its_reason(monkeypatch):
    """The reason has to survive the boundary: without this check an unknown server_id became a
    KeyError on `columns` — a traceback where a sentence belongs."""
    monkeypatch.setattr(common_cli, "run_allowing_failure", lambda command, request, **_kw: (
        False, {}, "Unknown server_id: ACME-nope."))

    with pytest.raises(SqlToXlsxError, match="Unknown server_id"):
        run_sql_to_xlsx(target="ACME-nope", sql_text="SELECT 1")


# --------------------------------------------------------------------------- #
# End-to-end command wiring
# --------------------------------------------------------------------------- #


def write_json(path, root_key, rows):
    path.write_text(json.dumps({root_key: rows}, ensure_ascii=False), encoding="utf-8")


def write_sql_to_xlsx_command(path):
    write_json(
        path,
        "telegram_support_commands",
        [
            {
                "command_id": 13,
                "command_text": "spbot_sql_to_xlsx",
                "reply_default": 1,
                "reply_text": "SQL -> xlsx: {status} rows={result_row_count} affected={result_affected_rows} {error}",
                "command_type": 2,
                "is_group": 1,
                "is_private": 1,
                "need_file": 1,
                "action_type": "sql_to_xlsx",
                "action_config": {
                    "db_type": "sqlserver",
                    "output_dir": "runtime/output/telegram/sql_to_xlsx",
                    "parameters": [
                        {"name": "target", "source": "arg", "position": 1, "required": True,
                         "prompt_text": "Target?"},
                        {"name": "sql_text", "source": "arg", "position": 2, "required": True,
                         "consume_rest": True, "accept_file": True, "prompt_text": "Paste SELECT:"},
                    ],
                },
            }
        ],
    )


def insert_command_message(sqlite_path, text, *, user_id="100"):
    store = DbOpsStore(sqlite_path)
    store.upsert_telegram_messages(
        [
            {
                "update_id": 10,
                "message_id": 10,
                "message_date": 1_779_478_400,
                "chat_id": user_id,
                "chat_type": "private",
                "user_id": user_id,
                "text": text,
                "raw": {"from": {"id": int(user_id), "username": "admin"}},
            }
        ]
    )
    store.sync_telegram_command_messages(command_prefix="/spbot")
    with sqlite3.connect(sqlite_path) as conn:
        row = conn.execute(
            "SELECT telegram_command_message_id FROM telegram_command_messages WHERE chat_id = ? AND message_id = 10",
            (user_id,),
        ).fetchone()
    return int(row[0])


def fetch_send_messages(sqlite_path):
    with sqlite3.connect(sqlite_path) as conn:
        conn.row_factory = sqlite3.Row
        return list(
            conn.execute(
                "SELECT message_text, metadata_json FROM telegram_send_messages ORDER BY send_tlgmsg_id ASC"
            )
        )


def test_sql_to_xlsx_command_queues_document_and_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(command_processor, "TOOL_ROOT", tmp_path)
    sqlite_path = tmp_path / "runtime.sqlite"
    commands_path = tmp_path / "telegram_support_commands.json"
    write_sql_to_xlsx_command(commands_path)
    write_json(tmp_path / "telegram_users.json", "telegram_users",
               [{"user_id": "100", "user_type": 2, "status": "active"}])
    write_json(tmp_path / "telegram_groups.json", "telegram_groups", [])

    def fake_run_sql_to_xlsx(*, target, sql_text, **_kwargs):
        assert sql_text == "SELECT Id, Name FROM Employee"
        return {
            "server_id": target,
            "database": "AppDb",
            "columns": ["Id", "Name"],
            "rows": [[1, "Alice"], [2, "Bob"]],
            "row_count": 2,
            "affected_rows": 0,
            "truncated": False,
        }

    monkeypatch.setattr(sql_commands, "run_sql_to_xlsx", fake_run_sql_to_xlsx)

    command_message_id = insert_command_message(
        sqlite_path, "/spbot_sql_to_xlsx ACME-192-0-2-248 SELECT Id, Name FROM Employee"
    )
    result = process_one_command_message(
        sqlite_path=sqlite_path,
        telegram_command_message_id=command_message_id,
        commands_path=commands_path,
    )
    assert result["status"] == "processed"

    messages = fetch_send_messages(sqlite_path)
    # One document message (with document_path) + one summary reply.
    document = next(m for m in messages if json.loads(m["metadata_json"]).get("document_path"))
    summary = next(m for m in messages if "SQL -> xlsx:" in m["message_text"])
    document_path = json.loads(document["metadata_json"])["document_path"]
    assert document_path.endswith(".xlsx")
    from pathlib import Path

    assert Path(document_path).exists()
    with zipfile.ZipFile(document_path) as archive:
        sheet = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
    assert "Alice" in sheet and "Bob" in sheet
    assert "rows=2" in summary["message_text"]
    assert "affected=0" in summary["message_text"]


def test_sql_to_xlsx_error_is_reported_to_user(tmp_path, monkeypatch):
    monkeypatch.setattr(command_processor, "TOOL_ROOT", tmp_path)
    sqlite_path = tmp_path / "runtime.sqlite"
    commands_path = tmp_path / "telegram_support_commands.json"
    write_sql_to_xlsx_command(commands_path)
    write_json(tmp_path / "telegram_users.json", "telegram_users",
               [{"user_id": "100", "user_type": 2, "status": "active"}])
    write_json(tmp_path / "telegram_groups.json", "telegram_groups", [])

    def boom(*, target, sql_text, **_kwargs):
        raise SqlToXlsxError("Refused: the statement changed 4 row(s) (rolled back).")

    monkeypatch.setattr(sql_commands, "run_sql_to_xlsx", boom)

    command_message_id = insert_command_message(
        sqlite_path, "/spbot_sql_to_xlsx ACME-x UPDATE t SET a=1"
    )
    result = process_one_command_message(
        sqlite_path=sqlite_path,
        telegram_command_message_id=command_message_id,
        commands_path=commands_path,
    )
    assert result["status"] == "action_failed"
    messages = fetch_send_messages(sqlite_path)
    # No document was queued; the summary carries the error.
    assert all(not json.loads(m["metadata_json"]).get("document_path") for m in messages)
    assert any("changed 4 row(s)" in m["message_text"] for m in messages)
    # A failed run has no action_result, so its {result_*} placeholders must be dropped, not
    # echoed to the user as literal "rows={result_row_count}".
    assert all("{result_" not in m["message_text"] for m in messages)


def test_render_reply_text_drops_unfilled_placeholders_but_keeps_braces_in_values():
    command = command_processor.SupportCommand(
        command_id=1,
        command_text="spbot_sql_to_xlsx",
        command_type=2,
        reply_default=1,
        reply_text="{status} rows={result_row_count} affected={result_affected_rows}\n{error}",
        is_group=1,
        is_private=1,
        need_file=0,
        action_type="sql_to_xlsx",
    )
    rendered = command_processor.render_reply_text(
        command.reply_text,
        row={"telegram_command_message_id": "7"},
        command=command,
        args=["ACME-x"],
        action_result=None,
        action_error="SQL failed: ('ODBC SQL type -155 ... {not a placeholder}', 'HY106')",
    )
    assert "{result_row_count}" not in rendered
    assert rendered.startswith("error rows= affected=")
    # Braces inside a substituted value (driver error text) survive untouched.
    assert "{not a placeholder}" in rendered


def write_status_command(path, *, command_type):
    write_json(
        path,
        "telegram_support_commands",
        [
            {
                "command_id": 1,
                "command_text": "spbot_status",
                "reply_default": 1,
                "reply_text": "OK",
                "command_type": command_type,
                "is_group": 1,
                "is_private": 1,
                "need_file": 0,
            }
        ],
    )


def test_cli_action_values_resolves_target_to_server_id(monkeypatch):
    from db_ops.common import data_sources as target_resolve

    monkeypatch.setattr(
        target_resolve,
        "resolve_target_instance",
        lambda spec, data_dir=None: {"server_id": "ACME-resolved", "ip": "1.2.3.4",
                                     "db_type": "sqlserver", "port": 1433},
    )
    cmd = command_processor.SupportCommand(
        command_id=99, command_text="spbot_x", command_type=2, reply_default=0, reply_text="",
        is_group=1, is_private=1, need_file=0, action_type="cli_execute",
        action_config={"parameters": [
            {"name": "target", "position": 1, "required": True, "consume_rest": True, "resolve": "target"}]},
    )
    values = command_processor.cli_action_values(
        command=cmd, args=["mssql", "1.2.3.4"], config_path="config.json"
    )
    # The unified target spec is resolved to the canonical server_id (+ip/db_type/port).
    assert values["server_id"] == "ACME-resolved"
    assert values["target_ip"] == "1.2.3.4"
    assert values["db_type"] == "sqlserver"
    assert values["port"] == 1433


def _cli_command(**action_config):
    return command_processor.SupportCommand(
        command_id=99, command_text="spbot_x", command_type=2, reply_default=0, reply_text="",
        is_group=1, is_private=1, need_file=0, action_type="cli_execute",
        action_config=action_config,
    )


def test_an_optional_argument_left_out_keeps_its_default(monkeypatch):
    """`defaults` only means anything if an absent optional value falls back to it.

    It did not: the empty string was written over the default, so
    `"session_id":{session_id}` rendered as `"session_id":,` and `/spbot_trace_session` with no
    argument died on its own payload with "request is not valid JSON" (2026-08-12, in production).
    """
    cmd = _cli_command(
        defaults={"session_id": "0", "min_tran_seconds": "300"},
        parameters=[{"name": "session_id", "position": 1, "required": False}],
    )

    values = command_processor.cli_action_values(command=cmd, args=[], config_path="config.json")

    assert values["session_id"] == "0"
    assert values["min_tran_seconds"] == "300"


def test_an_optional_argument_that_was_typed_still_wins_over_the_default():
    cmd = _cli_command(
        defaults={"session_id": "0"},
        parameters=[{"name": "session_id", "position": 1, "required": False}],
    )

    values = command_processor.cli_action_values(command=cmd, args=["505"], config_path="config.json")

    assert values["session_id"] == "505"


def test_an_optional_argument_with_no_default_is_still_the_empty_string():
    """`conditional_args` tests these with equals/not_equals, so "" has to stay reachable."""
    cmd = _cli_command(parameters=[{"name": "point_in_time", "position": 1, "required": False}])

    values = command_processor.cli_action_values(command=cmd, args=[], config_path="config.json")

    assert values["point_in_time"] == ""


def test_the_rendered_payload_is_valid_json_with_no_argument():
    """The end-to-end shape of the production failure: argv is rendered, then parsed as JSON."""
    import json as _json

    cmd = _cli_command(
        defaults={"session_id": "0", "min_tran_seconds": "300"},
        command_argv=['{"target":"S1","session_id":{session_id},"min_tran_seconds":{min_tran_seconds}}'],
        parameters=[{"name": "session_id", "position": 1, "required": False}],
    )
    values = command_processor.cli_action_values(command=cmd, args=[], config_path="config.json")

    argv = command_processor.build_cli_argv(dict(cmd.action_config), values)

    assert _json.loads(argv[0]) == {"target": "S1", "session_id": 0, "min_tran_seconds": 300}


def test_public_command_type_zero_runs_for_unknown_user(tmp_path):
    sqlite_path = tmp_path / "runtime.sqlite"
    commands_path = tmp_path / "telegram_support_commands.json"
    write_status_command(commands_path, command_type=0)
    # user_type 0 (unknown) and no group clearance: a public command must still run.
    write_json(tmp_path / "telegram_users.json", "telegram_users",
               [{"user_id": "100", "user_type": 0, "status": "active"}])
    write_json(tmp_path / "telegram_groups.json", "telegram_groups", [])

    command_message_id = insert_command_message(sqlite_path, "/spbot_status")
    result = process_one_command_message(
        sqlite_path=sqlite_path,
        telegram_command_message_id=command_message_id,
        commands_path=commands_path,
    )
    assert result["status"] == "processed"
    messages = fetch_send_messages(sqlite_path)
    assert any(m["message_text"] == "OK" for m in messages)


def test_negative_command_type_is_disabled(tmp_path):
    sqlite_path = tmp_path / "runtime.sqlite"
    commands_path = tmp_path / "telegram_support_commands.json"
    write_status_command(commands_path, command_type=-1)
    write_json(tmp_path / "telegram_users.json", "telegram_users",
               [{"user_id": "100", "user_type": 2, "status": "active"}])
    write_json(tmp_path / "telegram_groups.json", "telegram_groups", [])

    command_message_id = insert_command_message(sqlite_path, "/spbot_status")
    result = process_one_command_message(
        sqlite_path=sqlite_path,
        telegram_command_message_id=command_message_id,
        commands_path=commands_path,
    )
    # A -1 command is off even for a top-level user; no reply is queued.
    assert result["status"] == "command_disabled"
    assert fetch_send_messages(sqlite_path) == []


def test_list_server_id_command_replies_with_targets(tmp_path):
    sqlite_path = tmp_path / "runtime.sqlite"
    commands_path = tmp_path / "telegram_support_commands.json"
    write_json(
        commands_path,
        "telegram_support_commands",
        [
            {
                "command_id": 14,
                "command_text": "spbot_list_server_id",
                "reply_default": 1,
                "reply_text": "{result_listing}",
                "command_type": 1,
                "is_group": 1,
                "is_private": 1,
                "need_file": 0,
                "action_type": "list_server_id",
                "action_config": {"parameters": []},
            }
        ],
    )
    write_json(tmp_path / "telegram_users.json", "telegram_users",
               [{"user_id": "100", "user_type": 2, "status": "active"}])
    write_json(tmp_path / "telegram_groups.json", "telegram_groups", [])

    command_message_id = insert_command_message(sqlite_path, "/spbot_list_server_id")
    result = process_one_command_message(
        sqlite_path=sqlite_path,
        telegram_command_message_id=command_message_id,
        commands_path=commands_path,
    )
    assert result["status"] == "processed"
    messages = fetch_send_messages(sqlite_path)
    # Reads the real db_instances.json; the listing header + a known target must be present.
    assert any("Server targets" in m["message_text"] for m in messages)
    assert any("<db_type> <ip> [port]" in m["message_text"] for m in messages)


def test_sql_to_xlsx_denied_for_underprivileged_user(tmp_path):
    sqlite_path = tmp_path / "runtime.sqlite"
    commands_path = tmp_path / "telegram_support_commands.json"
    write_sql_to_xlsx_command(commands_path)
    # command_type=2 but the user is only level 1 -> denied by the level gate.
    write_json(tmp_path / "telegram_users.json", "telegram_users",
               [{"user_id": "100", "user_type": 1, "status": "active"}])
    write_json(tmp_path / "telegram_groups.json", "telegram_groups", [])

    command_message_id = insert_command_message(
        sqlite_path, "/spbot_sql_to_xlsx ACME-x SELECT 1"
    )
    result = process_one_command_message(
        sqlite_path=sqlite_path,
        telegram_command_message_id=command_message_id,
        commands_path=commands_path,
    )
    assert result["status"] == "permission_denied"
