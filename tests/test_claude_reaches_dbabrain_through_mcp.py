"""Claude reaches dbabrain through MCP - every common.cli command and the store, on the CLI's own terms (0.38.0).

Why it matters: the operator, 2026-11-11 (translated): *let MCP run every common cli, SQL included, and
read the dbabrain store.* An MCP server hands a model the same power a person at the terminal has, so
what made the terminal safe must hold here unchanged, and each piece has a test:

* every command the reference describes is a tool - a new command cannot be forgotten;
* every command that takes something away is marked destructive, so the client asks first;
* a request is completed from a ``server_id``, and the password it was completed with never comes back;
* a secret value a command prints is taken out before the model reads it;
* an answer is capped, and says so; a switched-off tool refuses; the store is read, never written;
* ``data/mcp_tools.json`` states every field, and ``upgrade-config`` writes what a node lacks.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from dbabrain.lib import mcp_tools as M
from dbabrain.lib import shared_objects
from dbabrain.mcp import server as S

REPO = Path(__file__).resolve().parents[1]
SECRET = "S3cret-Value-42!"


def _reference():
    return shared_objects.load(REPO / "data")


def _operations():
    catalogue = json.loads((REPO / "dbabrain/common/catalogue/emergency_operations.json").read_text(encoding="utf-8"))
    return M.operation_commands(catalogue)


def _node(tmp_path, **settings):
    document = {**M.DEFAULTS, **settings}
    node = S.Node(data_dir=tmp_path, config_path=None, settings=M.parse_mcp_tools(document),
                  reference=_reference(), operations=_operations())
    node._secrets = {"SQL_PASSWORD": SECRET}
    return node


@pytest.fixture
def logged(monkeypatch):
    rows = []
    monkeypatch.setattr(S, "_log", lambda node, **row: rows.append(row))
    return rows


# --------------------------------------------------------------------------- #
# The tool list
# --------------------------------------------------------------------------- #
def test_every_command_the_reference_describes_is_a_tool_and_the_store_has_two(tmp_path):
    node = _node(tmp_path)
    names = {tool["name"] for tool in S.tool_list(node)}
    commands = set(M.command_entries(_reference()))
    assert commands and commands <= names, "a command without its tool"
    assert {"run-sql", "db-status", "list-targets", "host-facts", "store_schema", "store_query"} <= names
    assert names - commands == set(M.STORE_TOOLS) | {M.SQL_WRITE_TOOL}


def test_every_command_that_takes_something_away_is_marked_destructive(tmp_path):
    tools = {tool["name"]: tool for tool in S.tool_list(_node(tmp_path))}
    must = {name for name in _operations() if name in tools}
    must |= {name for name in tools if name.startswith(("delete-", "prune-", "kill-", "shrink-", "rotate-"))}
    must |= {name for name in ("run-cmd", "run-sqlcmd", "copy-backup-dir", "smb-delete") if name in tools}
    assert must, "the catalogue names no command - the check would pass on nothing"
    assert [name for name in sorted(must) if not tools[name]["destructive"]] == []
    assert all(not tools[name]["read_only"] for name in must)
    assert tools["list-databases"]["read_only"] and not tools["list-databases"]["destructive"]
    assert "Changes or removes" in tools["kill-spid"]["description"] and "confirm" in tools["kill-spid"]["description"]


def test_a_switched_off_tool_is_not_offered_and_refuses_when_called_anyway(tmp_path, logged):
    node = _node(tmp_path, disabled_tools=["run-cmd", "store_query"])
    names = {tool["name"] for tool in S.tool_list(node)}
    assert "run-cmd" not in names and "store_query" not in names and "run-sql" in names
    answer = json.loads(S.call_tool(node, "run-cmd", {"request": {"server_id": "X"}}))
    assert answer["success"] is False and "switched off" in answer["error"]
    assert S.tool_list(_node(tmp_path, enabled=False)) == []


# --------------------------------------------------------------------------- #
# One call
# --------------------------------------------------------------------------- #
@pytest.fixture
def ran(monkeypatch):
    """The request each command was run with, and what it printed."""
    calls = []
    from dbabrain.lib.data_sources import request_fill

    def fill(command, request, *, data_dir=None, secrets=None):
        filled = dict(request)
        if request_fill.FILLS.get(command):
            filled["connection"] = {"host": "192.0.2.10", "username": "u", "password": SECRET}
        return filled

    def spawn(command, request, *, timeout_seconds=None, **_k):
        calls.append((command, request, timeout_seconds))
        envelope = {"success": True, "operation": command, "message": f"answered with {SECRET} in it",
                    "data": {"rows": [[1]], "echo": json.dumps(request)}, "request": request}
        return SimpleNamespace(stdout=json.dumps(envelope), stderr="", returncode=0), ""

    monkeypatch.setattr(request_fill, "fill_request", fill)
    monkeypatch.setattr(S.common_cli, "spawn", spawn)
    return calls


def test_a_request_is_completed_from_its_server_id_and_run_through_transport(tmp_path, ran, logged):
    answer = json.loads(S.call_tool(_node(tmp_path), "run-sql", {"request": {"server_id": "SRV", "sql_text": "SELECT 1"}}))
    [(command, request, timeout)] = ran
    assert command == "run-sql" and request["connection"]["password"] == SECRET and timeout == 600
    assert answer["success"] is True and answer["data"]["rows"] == [[1]]
    assert logged[0]["tool"] == "run-sql" and logged[0]["server_id"] == "SRV" and logged[0]["ok"] is True


def test_no_secret_value_and_never_the_filled_request_reaches_the_model(tmp_path, ran, logged):
    text = S.call_tool(_node(tmp_path), "db-status", {"request": {"server_id": "SRV"}})
    assert SECRET not in text and "***" in text
    assert '"request"' not in text, "the request a command ran holds the password it was completed with"


def test_an_answer_past_the_cap_is_cut_and_says_how_much_more_there_was(tmp_path, monkeypatch, logged):
    monkeypatch.setattr(S.common_cli, "spawn", lambda *_a, **_k: (SimpleNamespace(
        stdout=json.dumps({"success": True, "data": "x" * 5000}), stderr="", returncode=0), ""))
    text = S.call_tool(_node(tmp_path, max_chars=1000), "list-targets", {"request": {}})
    assert len(text) < 1200 and "[cut: " in text and "max_chars" in text
    short = S.call_tool(_node(tmp_path, max_chars=100000), "list-targets", {"request": {}})
    assert "[cut: " not in short


def test_an_unknown_tool_or_a_request_that_is_not_an_object_is_an_answer_not_a_crash(tmp_path, logged):
    node = _node(tmp_path)
    assert "not a common.cli command" in json.loads(S.call_tool(node, "drop-everything", {"request": {}}))["error"]
    assert "an object" in json.loads(S.call_tool(node, "run-sql", {"request": "SELECT 1"}))["error"]
    assert [row["ok"] for row in logged] == [False, False]


# --------------------------------------------------------------------------- #
# The store: read, never written
# --------------------------------------------------------------------------- #
@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "store.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE job_runs (log_id INTEGER, job_code TEXT)")
        conn.execute("CREATE TABLE web_users (name TEXT, password_hash TEXT)")
        conn.executemany("INSERT INTO job_runs VALUES (?, ?)", [(i, f"JOB{i}") for i in range(10)])

    class _Target:
        is_sqlite = True

        def connect(self):
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            return conn

    from dbabrain.db import backend

    monkeypatch.setattr(backend.StoreTarget, "from_config", classmethod(lambda cls, _c, **_k: _Target()))
    monkeypatch.setattr(S.Node, "config", lambda self: object())
    return path


def test_the_store_is_read_capped_and_never_written(tmp_path, store, logged):
    node = _node(tmp_path, store_query_max_rows=3)
    answer = json.loads(S.call_tool(node, "store_query", {"sql": "SELECT job_code FROM job_runs ORDER BY log_id",
                                                         "max_rows": 50}))
    assert answer["rows"] == [["JOB0"], ["JOB1"], ["JOB2"]] and answer["truncated"] is True
    refused = json.loads(S.call_tool(node, "store_query", {"sql": "DELETE FROM job_runs"}))
    assert refused["success"] is False and "ReadOnlyRefusal" in refused["error"]
    with sqlite3.connect(store) as conn:
        assert conn.execute("SELECT COUNT(*) FROM job_runs").fetchone()[0] == 10


def test_a_blocked_table_is_neither_listed_nor_read(tmp_path, store, logged):
    node = _node(tmp_path)
    tables = json.loads(S.call_tool(node, "store_schema", {}))["tables"]
    assert "job_runs" in tables and "web_users" not in tables
    assert "not readable" in json.loads(S.call_tool(node, "store_query", {"sql": "SELECT * FROM web_users"}))["error"]


# --------------------------------------------------------------------------- #
# The settings file
# --------------------------------------------------------------------------- #
def test_every_field_is_stated_and_a_missing_or_unknown_one_is_refused_by_name():
    assert M.parse_mcp_tools(dict(M.DEFAULTS)).enabled is True
    with pytest.raises(M.McpToolsError, match="max_chars"):
        M.parse_mcp_tools({k: v for k, v in M.DEFAULTS.items() if k != "max_chars"})
    with pytest.raises(M.McpToolsError, match="unknown"):
        M.parse_mcp_tools({**M.DEFAULTS, "allow_everything": True})
    with pytest.raises(M.McpToolsError, match="1000 or more"):
        M.parse_mcp_tools({**M.DEFAULTS, "max_chars": 10})


def test_the_shipped_file_the_masters_file_and_the_example_state_every_field():
    for path in ("dbabrain/mcp/catalogue/mcp_tools.json", "data/mcp_tools.json", "data/mcp_tools.example.json"):
        if path == "data/mcp_tools.json" and not (REPO / path).is_file():
            continue  # the master's own file: a public checkout has only the example
        M.parse_mcp_tools(json.loads((REPO / path).read_text(encoding="utf-8")))


def test_upgrade_config_gives_a_node_the_file_and_fills_what_one_lacks(tmp_path):
    from dbabrain.common import config_upgrade

    assert config_upgrade._stated_mcp_tools(tmp_path, dry_run=False, before_write=lambda _p: None)["files"]
    M.parse_mcp_tools(json.loads((tmp_path / M.FILE_NAME).read_text(encoding="utf-8")))
    (tmp_path / M.FILE_NAME).write_text(json.dumps({"enabled": False}), encoding="utf-8")
    config_upgrade._stated_mcp_tools(tmp_path, dry_run=False, before_write=lambda _p: None)
    settings = M.parse_mcp_tools(json.loads((tmp_path / M.FILE_NAME).read_text(encoding="utf-8")))
    assert settings.enabled is False, "what a node states is left as it is"


# --------------------------------------------------------------------------- #
# The SDK's server
# --------------------------------------------------------------------------- #
def test_the_sdk_server_offers_each_tool_with_its_hints_and_only_its_own_parameters(tmp_path):
    pytest.importorskip("mcp")
    server = S.build_server(_node(tmp_path))
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    assert tools["kill-spid"].annotations.destructive_hint is True
    assert tools["list-databases"].annotations.read_only_hint is True
    assert set(tools["run-sql"].input_schema["properties"]) == {"request"}
    assert set(tools["store_query"].input_schema["properties"]) == {"sql", "max_rows"}


# --------------------------------------------------------------------------- #
# Over HTTP - way 2's origin: nothing passes without the Worker's token
# --------------------------------------------------------------------------- #
def _asgi(app, path, headers=()):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(app({"type": "http", "path": path, "method": "POST", "headers": list(headers)}, receive, send))
    return sent[0]["status"], b"".join(m.get("body", b"") for m in sent[1:])


def test_the_http_origin_answers_nothing_without_its_token_and_only_ok_to_a_health_check():
    reached = []

    async def inner(scope, receive, send):
        reached.append(scope["path"])
        await S._plain(send, 200, b"mcp")

    token = "t" * 40
    gate = S._BearerOnly(inner, token)
    assert _asgi(gate, "/mcp") == (401, b"unauthorized")
    assert _asgi(gate, "/mcp", [(b"authorization", b"Bearer " + b"x" * 40)]) == (401, b"unauthorized")
    assert _asgi(gate, "/healthz") == (200, b"ok")
    assert reached == [], "the server is never reached without the token"
    assert _asgi(gate, "/mcp", [(b"authorization", f"Bearer {token}".encode())]) == (200, b"mcp")


def test_the_http_origin_refuses_to_start_with_a_short_token(tmp_path):
    with pytest.raises(S.McpToolError, match=str(S.HTTP_TOKEN_MIN_CHARS)):
        S.http_app(_node(tmp_path), token="short")


# --------------------------------------------------------------------------- #
# A user's own node: frames over one outbound WebSocket, answered by the SDK's server in-process
# --------------------------------------------------------------------------- #
class _Socket:
    """A WebSocket as the node sees it: the Worker's frames in, the node's replies out."""

    def __init__(self, frames):
        self.frames, self.sent = list(frames), []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.frames:
            await asyncio.sleep(0.2)          # the replies of the last frames go out first
            raise StopAsyncIteration
        return self.frames.pop(0)

    async def send(self, text):
        self.sent.append(json.loads(text))


def _frame(rid, body, user="person@example.com"):
    return json.dumps({"id": rid, "headers": {"x-dbabrain-user": user}, "body": json.dumps(body)})


def test_a_node_answers_each_frame_with_the_sdks_own_server_and_says_who_asked(tmp_path, logged, monkeypatch):
    pytest.importorskip("mcp")
    from dbabrain.mcp import relay

    # store_query opens the node's config.json before its read-only check; in the operator's checkout
    # that was the operator's own file in the working directory, and a public checkout has none.
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_bytes((REPO / "config.example.json").read_bytes())
    monkeypatch.chdir(home)

    socket = _Socket([
        _frame("a", {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}),
        _frame("b", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        _frame("c", {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                     "params": {"name": "store_query", "arguments": {"sql": "DELETE FROM job_runs"}}}),
        json.dumps({"type": "ping"})])
    asyncio.run(relay.serve(_node(tmp_path), server="https://relay.example", token="t",
                            connect=lambda *_a, **_k: socket, once=True))
    replies = {reply.get("id"): reply for reply in socket.sent}
    assert {"a", "b", "c", None} <= set(replies), socket.sent
    assert replies["a"]["status"] == 200 and '"serverInfo"' in replies["a"]["body"]
    assert len(json.loads(replies["b"]["body"])["result"]["tools"]) > 90
    assert "ReadOnlyRefusal" in replies["c"]["body"]
    assert {"type": "pong"} in socket.sent
    assert logged[-1]["tool"] == "store_query"


def test_a_refused_node_token_stops_the_node_with_the_sentence_that_says_so(tmp_path):
    pytest.importorskip("mcp")
    from dbabrain.mcp import relay

    class _Refused(Exception):
        response = SimpleNamespace(status_code=401)

    def connect(*_a, **_k):
        raise _Refused("HTTP 401")

    with pytest.raises(relay.RelayError, match="unlinked"):
        asyncio.run(relay.serve(_node(tmp_path), server="https://relay.example", token="t", connect=connect))


def test_linking_prints_a_code_and_waits_for_the_person_to_approve_it():
    from dbabrain.mcp import relay

    answers = iter([(200, {"device_code": "dc", "user_code": "ABCD-EFGH", "verification_uri": "https://x/link",
                           "interval": 2, "expires_in": 60}),
                    (428, {"error": "authorization_pending"}),
                    (200, {"node_token": "nt", "email": "person@example.com"})])
    calls = []
    answer = relay.link("https://x", sleep=lambda _s: None, post=lambda url, body: calls.append(url) or next(answers))
    assert answer["node_token"] == "nt" and calls == ["https://x/node/link/start", "https://x/node/link/poll",
                                                       "https://x/node/link/poll"]


def test_the_socket_url_is_the_connectors_own_over_tls():
    from dbabrain.mcp import relay

    assert relay.socket_url("https://mcp.dbabrain.dev") == "wss://mcp.dbabrain.dev/node/connect"


# --------------------------------------------------------------------------- #
# The directory's review rules (claude.com/docs/connectors/building/review-criteria)
# --------------------------------------------------------------------------- #
def test_every_tool_says_it_reads_or_it_changes_never_neither_nor_both(tmp_path):
    tools = S.tool_list(_node(tmp_path))
    assert [t["name"] for t in tools if t["read_only"] == t["destructive"]] == []


def test_no_description_tells_claude_how_to_behave(tmp_path):
    for tool in S.tool_list(_node(tmp_path)):
        text = tool["description"].lower()
        assert "ask the person" not in text and "you must" not in text and "always call" not in text, tool["name"]
    assert "ask the person" not in S.INSTRUCTIONS.lower()


def test_run_sql_reads_only_and_names_the_sql_it_takes_and_run_sql_write_is_its_other_half(tmp_path, ran, logged):
    tools = {t["name"]: t for t in S.tool_list(_node(tmp_path))}
    assert tools["run-sql"]["read_only"] and tools[M.SQL_WRITE_TOOL]["destructive"]
    assert "learn.microsoft.com" in tools["run-sql"]["description"] and "postgresql.org" in tools["run-sql"]["description"]
    node = _node(tmp_path)
    S.call_tool(node, "run-sql", {"request": {"server_id": "SRV", "sql_text": "SELECT 1", "commit": True}})
    S.call_tool(node, M.SQL_WRITE_TOOL, {"request": {"server_id": "SRV", "sql_text": "UPDATE t SET a = 1",
                                                     "commit": True}})
    (read_cmd, read_req, _), (write_cmd, write_req, _) = ran
    assert read_cmd == write_cmd == "run-sql"
    assert "commit" not in read_req and read_req["sql_text"] == "SELECT 1"
    assert write_req["commit"] is True and write_req["sql_text"] == "UPDATE t SET a = 1"


def test_the_read_only_run_sql_refuses_a_write_and_a_file_before_anything_runs(tmp_path, ran, logged):
    node = _node(tmp_path)
    for request, words in (({"server_id": "SRV", "sql_text": "DROP TABLE orders"}, "DROP"),
                           ({"server_id": "SRV", "sql_file": "q.sql"}, "sql_text")):
        answer = json.loads(S.call_tool(node, "run-sql", {"request": request}))
        assert answer["success"] is False and words in answer["error"]
    assert ran == []

# --------------------------------------------------------------------------- #
# Who asked - the account a call is recorded with (0.38.0, 1.343)
# --------------------------------------------------------------------------- #
# The operator asked on 2026-11-12 whether the connector kept who used it. Every call was a row, and
# the row had a place for the account - but nothing held that place to be filled: the tests above
# replace `_log`, and the 124 rows on the public demo all had it empty (they were this project's own
# checks, sent straight to the origin). These hold the three steps it takes to get there.
def test_the_http_origin_takes_who_asked_from_the_worker_and_only_behind_its_token():
    seen = []

    async def inner(scope, receive, send):
        seen.append(S.ASKED_BY.get())
        await S._plain(send, 200, b"mcp")

    token = "t" * 40
    gate = S._BearerOnly(inner, token)
    bearer = (b"authorization", f"Bearer {token}".encode())

    assert _asgi(gate, "/mcp", [bearer, (b"x-dbabrain-user", b"person@example.com")])[0] == 200
    assert _asgi(gate, "/mcp", [bearer])[0] == 200
    assert _asgi(gate, "/mcp", [bearer, (b"x-dbabrain-user", b"p" * 500)])[0] == 200
    assert seen == ["person@example.com", "", "p" * 200], "stated, absent, and cut to a length a row can hold"

    assert _asgi(gate, "/mcp", [(b"x-dbabrain-user", b"forged@example.com")])[0] == 401
    assert len(seen) == 3, "a caller named without the token never reaches the server"
    assert S.ASKED_BY.get() == "", "and the name does not outlive its request"


def test_a_recorded_call_carries_the_account_that_asked(tmp_path, monkeypatch):
    from dbabrain.db import store as store_module

    rows = []

    class _Store:
        def insert_job_run(self, run):
            rows.append(run)

    monkeypatch.setattr(store_module.DbaBrainStore, "from_config", classmethod(lambda cls, config: _Store()))
    monkeypatch.setattr(S.Node, "config", lambda self: None)
    node = _node(tmp_path)

    asked = S.ASKED_BY.set("person@example.com")
    try:
        S._log(node, tool="run-sql", server_id="SRV", ok=True, started_at="2026-11-12T00:00:00Z", duration_ms=12, text="")
    finally:
        S.ASKED_BY.reset(asked)
    S._log(node, tool="list-targets", server_id="", ok=False, started_at="2026-11-12T00:00:01Z", duration_ms=1,
           text="refused: " + SECRET)

    by_hand, local = rows
    assert (by_hand.job_code, by_hand.status) == ("mcp.run-sql", "DONE")
    assert by_hand.metadata["asked_by"] == "person@example.com" and by_hand.metadata["server_id"] == "SRV"
    assert local.metadata["asked_by"] == "", "a call from this machine's own Claude has no account to name"
    assert (local.status, local.level) == ("ERROR", "error") and local.error_text.startswith("refused")
    assert set(by_hand.metadata) == {"tool", "server_id", "ok", "user", "via", "asked_by"}, "what was asked is never in the row"


def test_a_call_through_a_node_is_recorded_with_the_account_the_worker_named(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    from dbabrain.mcp import relay

    recorded = []
    monkeypatch.setattr(S, "_log", lambda node, **row: recorded.append((row["tool"], S.ASKED_BY.get())))
    socket = _Socket([
        _frame("a", {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
               user="owner@example.com"),
        _frame("b", {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                     "params": {"name": "store_query", "arguments": {"sql": "DELETE FROM job_runs"}}},
               user="owner@example.com")])

    asyncio.run(relay.serve(_node(tmp_path), server="https://relay.example", token="t",
                            connect=lambda *_a, **_k: socket, once=True))

    assert recorded == [("store_query", "owner@example.com")]
    assert S.ASKED_BY.get() == ""
