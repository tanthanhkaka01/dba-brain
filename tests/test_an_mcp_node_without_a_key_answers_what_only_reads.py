"""An MCP node without a dbabrain key answers the tools that only read, and nothing that changes (1.1.0, 1.425).

The operator, 2026-11-25 (translated): *make MCP let a few tools be called without signing in*. In 1.0.0 the whole
server refused to start without a key. Now the line is the one the client already shows beside each tool:

* too tight is the old way - an agent pointed at a fresh install, or a directory reading what a server offers, got
  nothing at all, so ``serve`` starts with no key and a tool marked ``readOnlyHint`` answers;
* too loose would give the key's work away - a tool marked destructive (``run-sql`` among them: its check reads text,
  and the login's rights are what bound it) is refused before any of it runs, with the sentence that says how to
  get a key; with a key it runs as before;
* linking the node to dbabrain.dev's connector (``connect``) still needs the key up front.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dbabrain.lib import dbabrain_key as dk
from dbabrain.lib import mcp_tools as M
from dbabrain.lib import secret_text, shared_objects
from dbabrain.mcp import cli
from dbabrain.mcp import server as S

from tests.test_dbabrain_key import Signer

pytestmark = pytest.mark.real_key_gate

REPO = Path(__file__).resolve().parents[1]
PASSPHRASE = "a-throwaway-passphrase-for-this-test-only"


def _operations():
    catalogue = json.loads((REPO / "dbabrain/common/catalogue/emergency_operations.json").read_text(encoding="utf-8"))
    return M.operation_commands(catalogue)


@pytest.fixture
def node(tmp_path, monkeypatch):
    monkeypatch.setenv(secret_text.SECRET_KEY_ENV_VAR, PASSPHRASE)
    data = tmp_path / "data"
    data.mkdir()
    return S.Node(data_dir=data, config_path=None, settings=M.parse_mcp_tools(dict(M.DEFAULTS)),
                  reference=shared_objects.load(REPO / "data"), operations=_operations())


@pytest.fixture
def ran(monkeypatch):
    """What reached the command or the store - nothing runs for real here."""
    calls = []
    monkeypatch.setattr(S, "_command_tool", lambda node, tool, request: (calls.append(tool), ({"success": True}, True))[1])
    monkeypatch.setattr(S, "_store_tool", lambda node, tool, arguments: (calls.append(tool), {"rows": []})[1])
    monkeypatch.setattr(S, "_log", lambda node, **row: None)
    return calls


def _with_key(node, monkeypatch):
    signer = Signer()
    monkeypatch.setattr(dk, "SIGNING_PUBLIC_KEYS", signer.keys)
    from datetime import datetime, timezone

    secret_text.set_secret_text(node.data_dir, dk.KEY_REF, signer.token(issued=datetime.now(timezone.utc)),
                                key=PASSPHRASE)


def test_a_tool_needs_a_key_exactly_when_the_client_is_told_it_changes_something(node):
    tools = S.tool_list(node)
    assert tools, "the shipped settings offer tools"
    for tool in tools:
        assert tool["needs_key"] is (not tool["read_only"]), tool["name"]
    by_name = {tool["name"]: tool for tool in tools}
    assert not by_name["list-objects"]["needs_key"] and not by_name["store-query"]["needs_key"]
    assert by_name["run-sql"]["needs_key"] and by_name["run-sql-write"]["needs_key"]
    assert by_name["run-cmd"]["needs_key"] and by_name["backup-database"]["needs_key"]


def test_without_a_key_a_tool_that_only_reads_answers(node, ran):
    answer = json.loads(S.call_tool(node, "list-objects", {"request": {"server_id": "SRV-1", "kind": "databases"}}))
    assert answer == {"success": True}
    assert json.loads(S.call_tool(node, "store-query", {"sql": "select 1"})) == {"rows": []}
    assert ran == ["list-databases", "store-query"]


@pytest.mark.parametrize("tool", ["run-sql", "run-sql-write", "run-cmd", "backup-database"])
def test_without_a_key_a_tool_marked_destructive_is_refused_before_it_runs(node, ran, tool):
    answer = json.loads(S.call_tool(node, tool, {"request": {"server_id": "SRV-1", "sql_text": "select 1"}}))
    assert answer["success"] is False
    assert "needs a dbabrain key" in answer["error"] and "dbabrain key-request" in answer["error"]
    assert ran == []


def test_with_a_key_the_same_tool_runs(node, ran, monkeypatch):
    _with_key(node, monkeypatch)
    answer = json.loads(S.call_tool(node, "run-sql", {"request": {"server_id": "SRV-1", "sql_text": "select 1"}}))
    assert answer == {"success": True}
    assert ran == ["run-sql"]


def test_serve_starts_without_a_key_and_says_so_once(node, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_node", lambda home: node)
    started = []

    class _Server:
        def run(self, transport):
            started.append(transport)

    monkeypatch.setattr(S, "build_server", lambda _node: _Server())
    assert cli.main(["serve"]) == 0
    assert started == ["stdio"]
    said = capsys.readouterr()
    assert said.out == "", "a stdio server's stdout is the protocol's"
    assert "dbabrain key-request" in said.err and "the tools that only read answer" in said.err


def test_call_on_a_node_without_a_key_reaches_the_tool_and_the_tool_decides(node, ran, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_node", lambda home: node)
    assert cli.main(["call", "list-objects", '{"request": {"server_id": "SRV-1", "kind": "databases"}}']) == 0
    assert ran == ["list-databases"]
    capsys.readouterr()
    assert cli.main(["call", "run-sql", '{"request": {"server_id": "SRV-1", "sql_text": "select 1"}}']) == 0
    assert "needs a dbabrain key" in capsys.readouterr().out
    assert ran == ["list-databases"]


def test_connect_still_needs_the_key_up_front(node, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_node", lambda home: node)
    import dbabrain.mcp.relay as relay

    monkeypatch.setattr(relay, "connect_node", lambda *a, **k: pytest.fail("connect ran without a key"))
    assert cli.main(["connect"]) == 0  # never had a key: a state, said, exit 0 (as in 1.0.0)
    assert "dbabrain key-request" in capsys.readouterr().err
