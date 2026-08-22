"""Uploading a spreadsheet through Telegram and getting a queryable table back.

The flow is four ordinary prompts — server, database, schema, file — so almost nothing here is
new machinery. Two things are, and both are the kind of mistake that only shows up against a
real file:

**A .xlsx is not text.** The existing attachment path decodes a download as ``utf-8-sig``,
which is right for the ``.sql`` body it was written for and wrong for a zip: it either raises,
or — worse on a lenient codec — succeeds and hands the action something that is no longer the
file. The awaited parameter now says which it wants, and a workbook says base64.

**A Telegram user must not be able to drop a table by re-sending a file.** ``if_exists`` is left
at the module default of ``error`` rather than pinned to ``drop`` in the command config, and
that is a decision worth a test rather than a comment, because "make it overwrite" is exactly
the change someone will make the first time they re-upload a corrected sheet.
"""

from __future__ import annotations

import base64
import json

import pytest

from db_ops.telegram import command_processor
from conftest import shipped_config


def _command(action_config=None):
    return command_processor.SupportCommand(
        command_id=28,
        command_text="spbot_xlsx_to_table",
        command_type=10,
        reply_default=1,
        reply_text="{status}",
        is_group=0,
        is_private=1,
        need_file=1,
        action_type="create_table_from_xlsx",
        action_config=action_config if action_config is not None else {},
    )


# --------------------------------------------------------------------------- #
# The request the handler builds is the one the CLI would take
# --------------------------------------------------------------------------- #

def test_the_four_prompted_answers_become_the_json_request(monkeypatch):
    """The Telegram path and the shell path must not drift, so the handler builds the same
    object `common.cli create-table-from-xlsx` takes and calls the same function."""
    seen = {}

    def fake_create(request):
        seen.update(request)
        return {"server_id": "ACME-1", "db_type": "sqlserver", "database": "Staging",
                "schema": "dbo", "table_name": "temp_ab12cd34",
                "qualified_name": "[dbo].[temp_ab12cd34]", "column_count": 3,
                "rows_inserted": 42, "column_type": "NVARCHAR(4000)"}

    # Stubbed at the CLI boundary since 2026-08-15: the load runs in
    # `db_ops.common.cli create-table-from-xlsx`, so patching the in-process function would patch
    # something this path no longer calls. The request it receives is asserted unchanged.
    from db_ops.lib import common_cli
    monkeypatch.setattr(common_cli, "run", lambda command, request: fake_create(request))

    result = command_processor.execute_create_table_from_xlsx_command(
        command=_command(), args=["ACME-1", "Staging", "dbo", "UEsDBBQ="])

    assert seen["target"] == "ACME-1"
    assert seen["database"] == "Staging"
    assert seen["schema"] == "dbo"
    assert seen["file_base64"] == "UEsDBBQ="
    assert result["table_name"] == "temp_ab12cd34"
    assert result["rows_inserted"] == 42 and result["row_count"] == 42
    assert result["status"] == "success"


def test_the_reply_actually_renders_every_field_it_promises(monkeypatch):
    """The bug this catches shipped on 2026-08-13: the handler returned keys already prefixed
    `result_`, and `render_reply_text` prefixes them itself — so every placeholder resolved to
    `{result_result_...}`, matched nothing, and was blanked. The operator got

        xlsx -> table: done
         on
        database= schema=
        columns= () rows=7

    which does not say which table was written, or to which database, or whether "done" was
    even true. Asserting the handler's keys is not enough; the template and the renderer have
    to agree, so this drives the real renderer with the real shipped template.
    """
    from db_ops.lib import common_cli
    monkeypatch.setattr(common_cli, "run", lambda command, request: {
        "server_id": "ACME-192-0-2-248", "db_type": "sqlserver", "database": "Globex_Prod",
        "schema": "guest", "table_name": "temp_e24623de",
        "qualified_name": "[guest].[temp_e24623de]", "column_count": 19,
        "rows_inserted": 7, "column_type": "NVARCHAR(4000)"})

    shipped = json.loads(
        open(shipped_config("telegram_support_commands.json"), encoding="utf-8").read()
    )["telegram_support_commands"]
    entry = next(c for c in shipped if c["command_text"] == "spbot_xlsx_to_table")

    action_result = command_processor.execute_create_table_from_xlsx_command(
        command=_command(), args=["ACME-192-0-2-248", "Globex_Prod", "guest", "UEsDBBQ="])
    rendered = command_processor.render_reply_text(
        entry["reply_text"],
        row={"telegram_command_message_id": 1},
        command=_command(),
        args=["ACME-192-0-2-248"],
        action_result=action_result,
        action_error=None,
    )

    for expected in ("ACME-192-0-2-248", "Globex_Prod", "guest", "temp_e24623de",
                     "[guest].[temp_e24623de]", "19", "7", "NVARCHAR(4000)"):
        assert expected in rendered, f"{expected!r} missing from the reply:\n{rendered}"
    # And nothing was left as an unfilled hole.
    assert "{" not in rendered and "}" not in rendered


def test_the_handler_returns_unprefixed_keys_because_the_renderer_adds_the_prefix(monkeypatch):
    """The mechanism behind the test above, stated directly: `render_reply_text` exposes each
    scalar as `{result_<key>}`. A handler that prefixes its own keys double-prefixes them."""
    from db_ops.lib import common_cli
    monkeypatch.setattr(common_cli, "run", lambda command, request: {
        "server_id": "s", "database": "d", "schema": "sc", "table_name": "t",
        "qualified_name": "[sc].[t]", "column_count": 1, "rows_inserted": 0,
        "column_type": "NVARCHAR(4000)"})

    result = command_processor.execute_create_table_from_xlsx_command(
        command=_command(), args=["s", "d", "sc", "UEsDBBQ="])

    offenders = sorted(key for key in result if key.startswith("result_"))
    assert not offenders, f"these would render as {{result_result_*}}: {offenders}"


def test_the_generated_table_name_comes_back_because_nothing_else_names_it(monkeypatch):
    """The operator did not choose it, so the reply is the only place they can find it. A blank
    table_name in the request is what asks for one."""
    from db_ops.lib import common_cli
    monkeypatch.setattr(common_cli, "run", lambda command, request: {
        "server_id": "ACME-1", "database": "d", "schema": "s", "table_name": "temp_deadbeef",
        "qualified_name": "[s].[temp_deadbeef]", "column_count": 1, "rows_inserted": 0,
        "column_type": "NVARCHAR(4000)"})

    result = command_processor.execute_create_table_from_xlsx_command(
        command=_command(), args=["ACME-1", "d", "s", "UEsDBBQ="])

    assert result["qualified_name"] == "[s].[temp_deadbeef]"


def test_a_fifth_word_names_the_table_instead_of_generating_one(monkeypatch):
    seen = {}
    from db_ops.lib import common_cli
    monkeypatch.setattr(common_cli, "run",
                        lambda command, request: seen.update(request) or {
                            "server_id": "ACME-1", "database": "d", "schema": "s",
                            "table_name": "chosen", "qualified_name": "[s].[chosen]",
                            "column_count": 1, "rows_inserted": 0,
                            "column_type": "NVARCHAR(4000)"})

    command_processor.execute_create_table_from_xlsx_command(
        command=_command(), args=["ACME-1", "d", "s", "UEsDBBQ=", "chosen"])

    assert seen["table_name"] == "chosen"


def test_a_missing_attachment_says_to_attach_the_file(monkeypatch):
    """The prompt loop can deliver an empty value if the user answers with a word instead of a
    file. 'target is required' would send them to the wrong end of the conversation."""
    with pytest.raises(command_processor.TelegramCommandError, match="Attach the .xlsx or"):
        command_processor.execute_create_table_from_xlsx_command(
            command=_command(), args=["ACME-1", "d", "s", ""])


def test_a_load_failure_is_reported_to_the_user_not_raised_as_a_crash(monkeypatch):
    # The CLI reports the failure as `success: false`; the client turns that into
    # CommonCliError, and the handler must still turn *that* into a message the operator reads
    # rather than a traceback.
    from db_ops.lib import common_cli

    def boom(command, request):
        raise common_cli.CommonCliError("[dbo].[t] already exists on ACME-1.")

    monkeypatch.setattr(common_cli, "run", boom)

    with pytest.raises(command_processor.TelegramCommandError, match="already exists"):
        command_processor.execute_create_table_from_xlsx_command(
            command=_command(), args=["ACME-1", "d", "s", "UEsDBBQ="])


# --------------------------------------------------------------------------- #
# What the command config may and may not decide
# --------------------------------------------------------------------------- #

def test_the_shipped_command_does_not_let_a_telegram_user_drop_a_table():
    """`if_exists` is absent from action_config, so the module default (`error`) applies. If
    someone pins it to `drop` here, re-sending a corrected file silently destroys whatever was
    in the table before — from a chat message, with no confirmation step."""
    shipped = json.loads(
        open(shipped_config("telegram_support_commands.json"), encoding="utf-8").read()
    )["telegram_support_commands"]
    entry = next(c for c in shipped if c["command_text"] == "spbot_xlsx_to_table")

    assert entry["action_config"].get("if_exists") in (None, "error")
    assert entry["is_private"] == 1 and entry["is_group"] == 0


def test_a_deployment_may_still_pin_the_options_in_its_own_config(monkeypatch):
    """The config is the one place that decides; the handler does not hardcode the policy."""
    seen = {}
    from db_ops.lib import common_cli
    monkeypatch.setattr(common_cli, "run",
                        lambda command, request: seen.update(request) or {
                            "server_id": "ACME-1", "database": "d", "schema": "s",
                            "table_name": "t", "qualified_name": "[s].[t]",
                            "column_count": 1, "rows_inserted": 0,
                            "column_type": "NVARCHAR(4000)"})

    command_processor.execute_create_table_from_xlsx_command(
        command=_command({"if_exists": "append", "text_length": 200}),
        args=["ACME-1", "d", "s", "UEsDBBQ="])

    assert seen["if_exists"] == "append"
    assert seen["text_length"] == 200


def test_the_shipped_command_asks_for_the_file_as_base64():
    """Without `file_encoding: base64` the download is decoded as utf-8 and the workbook is
    destroyed on the way in — the failure this whole path exists to avoid."""
    shipped = json.loads(
        open(shipped_config("telegram_support_commands.json"), encoding="utf-8").read()
    )["telegram_support_commands"]
    entry = next(c for c in shipped if c["command_text"] == "spbot_xlsx_to_table")
    file_param = next(p for p in entry["action_config"]["parameters"]
                      if p["name"] == "xlsx_base64")

    assert file_param["accept_file"] is True
    assert file_param["file_encoding"] == "base64"
    assert file_param["position"] == 4


# --------------------------------------------------------------------------- #
# The download itself
# --------------------------------------------------------------------------- #

def test_a_binary_attachment_is_carried_as_base64_not_decoded_as_text(monkeypatch, tmp_path):
    """A zip's first bytes are `PK\\x03\\x04`; `utf-8-sig` on the whole thing is not the file."""
    from db_ops.telegram import api

    payload = b"PK\x03\x04\x14\x00\x00\x00\x08\x00\xff\xfe binary \x00 bytes"
    monkeypatch.setattr(api, "get_file_bytes", lambda **kwargs: payload)

    class _Config:
        class telegram:
            resolved_bot_token = "t"
            api_url = "https://example.invalid"

    monkeypatch.setattr("db_ops.config.load_config", lambda path: _Config)

    encoded = command_processor._download_document_base64(
        {"file_id": "AAA"}, config_path=tmp_path / "config.json")

    assert base64.b64decode(encoded) == payload


def test_an_empty_attachment_is_refused_rather_than_loaded_as_an_empty_table(monkeypatch,
                                                                            tmp_path):
    from db_ops.telegram import api
    monkeypatch.setattr(api, "get_file_bytes", lambda **kwargs: b"")

    class _Config:
        class telegram:
            resolved_bot_token = "t"
            api_url = "https://example.invalid"

    monkeypatch.setattr("db_ops.config.load_config", lambda path: _Config)

    with pytest.raises(RuntimeError, match="empty"):
        command_processor._download_document_base64(
            {"file_id": "AAA"}, config_path=tmp_path / "config.json")


def _stub_download(monkeypatch, body: bytes, *, reported_size: int | None = None):
    """Answer `getFile` and the file fetch without a network, returning `body`."""
    from db_ops.telegram import api

    monkeypatch.setattr(api, "call_telegram_api", lambda **kwargs: {
        "ok": True,
        "result": {"file_path": "documents/file.xlsx",
                   "file_size": len(body) if reported_size is None else reported_size},
    })

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, amount=None):
            return body if amount is None else body[:amount]

    monkeypatch.setattr(api.request, "urlopen", lambda req, timeout=None: _Response())


def test_a_multi_megabyte_attachment_is_downloaded_whole(monkeypatch):
    """There used to be a 1 MB cap here, and it refused the ordinary case this command exists
    for: an operator's 5 MB leave list came back as 'Telegram file too large: 5001134 > 1000000'
    — a number that corresponded to nothing anyone had chosen. The only ceiling left is
    Telegram's own, which no setting on this side can lift."""
    from db_ops.telegram import api

    body = b"PK\x03\x04" + b"x" * (5 * 1024 * 1024)
    _stub_download(monkeypatch, body)

    assert api.get_file_bytes(bot_token="t", file_id="AAA") == body


def test_a_caller_that_wants_its_own_bound_still_gets_one(monkeypatch):
    """`max_bytes` is not gone, only defaulted off: a caller with a reason to cap can still say
    so, and it is checked against the reported size before anything is fetched."""
    from db_ops.telegram import api

    _stub_download(monkeypatch, b"x" * 4096)

    with pytest.raises(RuntimeError, match="too large"):
        api.get_file_bytes(bot_token="t", file_id="AAA", max_bytes=1024)


def test_telegrams_own_refusal_explains_whose_limit_it_is(monkeypatch):
    """Telegram answers 'Bad Request: file is too big' above 20 MB, whatever the sender saw when
    they attached the file. Passed through verbatim it reads as a db_ops limit the operator
    should ask us to raise, and there is nothing to raise."""
    from db_ops.telegram import api

    def refuse(**kwargs):
        raise RuntimeError("Telegram HTTP 400: {\"description\":\"Bad Request: file is too big\"}")

    monkeypatch.setattr(api, "call_telegram_api", refuse)

    with pytest.raises(RuntimeError, match="caps a bot's own download at 20 MB"):
        api.get_file_bytes(bot_token="t", file_id="AAA")


def test_the_action_type_is_wired_into_the_dispatcher():
    """A handler that exists but is not in the allowlist replies 'ok' and does nothing at all."""
    import inspect

    source = inspect.getsource(command_processor)
    allowlist_line = next(line for line in source.splitlines()
                          if '"sql_execute", "cli_execute"' in line)

    assert "create_table_from_xlsx" in allowlist_line
