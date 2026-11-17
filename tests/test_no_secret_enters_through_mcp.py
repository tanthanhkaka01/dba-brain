"""No secret enters through MCP - the twin of no secret crosses (0.39.0).

The operator, 2026-11-14, on ``ai-key`` and ``secret-set`` being offered as MCP tools: *no secret goes
in through MCP*. A request over MCP is written by the model, so a password, an API key or a private key
in it came through the chat - and on way 2 through the relay. Every answer was already redacted on the
way out; now a request carrying a secret is refused on the way in, before anything runs, and says where
the secret is typed instead: on the node (``secret-set``, ``ai-key``), then named here by its reference.

A login the node completes from its own store is not such a secret - the model never sees it - so
``run-sql`` by ``server_id`` runs as before, and ``instance-add`` with ``password_ref`` too. A command
that could only ever be refused (its purpose is a secret, or one it cannot run without) is not offered.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from dbabrain.lib import mcp_tools as M
from dbabrain.lib import shared_objects
from dbabrain.mcp import server as S

REPO = Path(__file__).resolve().parents[1]


def _node(tmp_path):
    catalogue = json.loads((REPO / "dbabrain/common/catalogue/emergency_operations.json").read_text(encoding="utf-8"))
    node = S.Node(data_dir=tmp_path, config_path=None, settings=M.parse_mcp_tools(dict(M.DEFAULTS)),
                  reference=shared_objects.load(REPO / "data"), operations=M.operation_commands(catalogue))
    node._secrets = {}
    return node


@pytest.fixture
def ran(monkeypatch):
    calls = []
    monkeypatch.setattr(S, "_log", lambda node, **row: None)
    monkeypatch.setattr(S.common_cli, "spawn", lambda command, request, **_k: (calls.append((command, request)) or (
        SimpleNamespace(stdout=json.dumps({"success": True, "operation": command}), stderr="", returncode=0), "")))
    return calls


# --------------------------------------------------------------------------- what counts as a secret
@pytest.mark.parametrize("command, request_, where", [
    ("instance-add", {"server_id": "S", "password": "p"}, ["password"]),
    ("run-sql", {"server_id": "S", "connection": {"host": "h", "password": "p"}}, ["connection.password"]),
    ("create-db-docker", {"password": "p", "sudo_password": "q"}, ["password", "sudo_password"]),
    ("restore-full", {"encryption_password": "p"}, ["encryption_password"]),
    ("rotate-password", {"passwords": [{"ref": "R", "value": "v"}]}, ["passwords"]),
    ("secret-set", {"ref": "TELEGRAM_BOT_TOKEN", "value": "t"}, ["value"]),
    ("secret-remove", {"ref": "TELEGRAM_BOT_TOKEN", "value": "t"}, ["value"]),
    ("ai-key", {"value": "sk-ant-x"}, ["value"]),
    ("smb-list", {"items": [{"share": "x", "password": "p"}]}, ["items[0].password"]),
])
def test_a_secret_is_found_wherever_it_sits(command, request_, where):
    assert M.secrets_carried(command, request_) == where


@pytest.mark.parametrize("request_", [
    {"server_id": "S", "password_ref": "SQL_PASSWORD"},
    {"server_id": "S", "password": ""},
    {"password_length": 24, "password_env": "X"},
    {"server_id": "S", "sql_text": "SELECT 'password' AS word"},
])
def test_a_reference_an_empty_value_or_the_word_alone_is_not_a_secret(request_):
    assert M.secrets_carried("instance-add", request_) == []


# --------------------------------------------------------------------------- refused before anything runs
def test_a_request_carrying_a_password_is_refused_before_it_runs_and_says_where_to_type_it(tmp_path, ran):
    answer = json.loads(S.call_tool(_node(tmp_path), "instance-add",
                                    {"request": {"server_id": "S", "db_type": "sqlserver", "password": "Hunter2!"}}))

    assert answer["success"] is False and ran == []
    assert "no secret enters through MCP" in answer["error"] and "password" in answer["error"]
    assert "secret-set" in answer["error"] and "password_ref" in answer["error"]
    assert "Hunter2!" not in json.dumps(answer), "what was sent is never echoed"


def test_the_same_registration_by_its_reference_runs(tmp_path, ran):
    answer = json.loads(S.call_tool(_node(tmp_path), "instance-add",
                                    {"request": {"server_id": "S", "db_type": "sqlserver",
                                                 "password_ref": "S_SQL_PASSWORD"}}))

    assert answer["success"] is True and [command for command, _r in ran] == ["instance-add"]


def test_a_login_the_node_completes_is_not_one_the_model_sent(tmp_path, ran, monkeypatch):
    from dbabrain.lib.data_sources import request_fill

    monkeypatch.setattr(request_fill, "fill_request", lambda command, request, **_k: {
        **request, "connection": {"host": "h", "username": "u", "password": "filled-by-the-node"}})

    answer = json.loads(S.call_tool(_node(tmp_path), "run-sql", {"request": {"server_id": "S", "sql_text": "SELECT 1"}}))

    assert answer["success"] is True and ran[0][1]["connection"]["password"] == "filled-by-the-node"


# --------------------------------------------------------------------------- not offered at all
def test_a_command_that_could_only_be_refused_is_not_a_tool(tmp_path):
    names = {tool["name"] for tool in S.tool_list(_node(tmp_path))}

    assert not {"secret-set", "secret-remove", "ai-key", "decrypt-on-host", "fetch-decrypted-file", "verify-encrypted-file"} & names
    assert {"instance-add", "remote-credential-add", "run-sql", "list-targets"} <= names, "a reference is enough"


def test_called_by_name_anyway_it_is_refused(tmp_path, ran):
    answer = json.loads(S.call_tool(_node(tmp_path), "ai-key", {"request": {"value": "sk-ant-x"}}))
    assert answer["success"] is False and "no secret enters through MCP" in answer["error"] and ran == []


# --------------------------------------------------------------------------- a new command cannot slip by
#: Request fields whose names look like a secret and are not one (a ``*_ref`` / ``*_env`` is a name,
#: never a value): a switch, a length, a prefix, what to do when one is missing.
NOT_SECRETS = {"include_secrets", "password_length", "secret_prefix", "on_missing_secret"}
_LOOKS = ("password", "secret", "token", "identity", "passphrase", "api_key")


def test_every_request_field_that_looks_like_a_secret_is_judged():
    """A field named like a secret is either one (``SECRET_KEYS``, refused over MCP) or named here as
    not one - so a command added later with a ``token`` or a ``private_key`` cannot pass unseen."""
    unjudged = set()
    for entry in M.command_entries(shared_objects.load(REPO / "data")).values():
        for field in entry.get("fields") or []:
            name = str(field.get("field"))
            if any(word in name.lower() for word in _LOOKS) and name not in M.SECRET_KEYS and name not in NOT_SECRETS \
                    and not name.endswith(("_ref", "_env", "_refs")):
                unjudged.add(name)
    assert not unjudged, f"say whether these hold a secret (lib.mcp_tools.SECRET_KEYS) or not (NOT_SECRETS): {unjudged}"
