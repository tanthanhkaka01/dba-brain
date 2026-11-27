"""Over MCP, commands too alike are one tool, and each tool says what it does and which sibling to take instead.

Glama's review of 1.1.0 named ``delete-verified-file`` / ``delete-verified-files`` and ``delete-empty-folder`` /
``delete-empty-folders`` as tools split one per variant, and ``key-request`` / ``key-renew`` as two an agent could not
choose between. The plural takes a one-item list, and renewing a key is the same sign-in with the new key replacing the
old, so each pair is one tool over MCP (1.3.0, 1.434); the commands themselves stay in common.cli.

Its review of 1.2.0 (C 2.5/5) named the families an agent picks wrongly in - file transfer, deletion, SQL execution,
*numerous backup/restore variants* - snake_case ``store_query`` / ``store_schema`` among kebab-case names, and 108
tools. The operator, 2026-11-27 (translated): *ok, do it - and group a few tools that are too alike (same operation,
singular, plural ...)*; the plan of that morning was 108 -> 97. So (1.439):

* commands that share one request entry and one hint, the choice one enum field, are one tool - ``restore`` (step),
  ``list-objects`` (kind), ``check-config`` (check), ``sqlserver-cu-check`` (check), ``list-backup-files`` (action);
* ``add-sql`` (the older one-call form of sql-command-add + sql-target-add) is covered; ``ask`` and ``authorize`` ask
  a person at the node's terminal, which an MCP call does not have;
* the store tools are ``store-schema`` / ``store-query``;
* a tool switched off under a name it had before stays off, and a call by an old name answers the new one;
* each description is the tool's own text (lib.mcp_tool_text), with no ``dbabrain common.cli `x`:`` before it.

``prune-backup-files`` was marked destructive for its name, though since 0.38.0 its ``delete`` is refused and it only
answers a retention rule's plan (1.435): a client asked for approval of a read, and a node without a key refused it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dbabrain.lib import mcp_tools as M
from dbabrain.lib import shared_objects
from dbabrain.lib.mcp_tool_text import SQL_WRITE_TEXT, TOOL_TEXT

ROOT = Path(__file__).resolve().parents[1]
GROUPED = {command for _field, choices, _text in M.GROUPED_TOOLS.values() for command in choices.values()}


def _catalogue():
    return json.loads((ROOT / "dbabrain/common/catalogue/emergency_operations.json").read_text(encoding="utf-8"))


def _node(tmp_path, **settings):
    from dbabrain.mcp import server as S

    node = S.Node(data_dir=tmp_path, config_path=None, settings=M.parse_mcp_tools({**M.DEFAULTS, **settings}),
                  reference=shared_objects.load(), operations=M.operation_commands(_catalogue()))
    return S, node


@pytest.fixture
def ran(monkeypatch):
    from dbabrain.mcp import server as S

    calls: list[tuple[str, dict]] = []

    def spawn(command, request, **_k):
        calls.append((command, request))
        return type("R", (), {"stdout": json.dumps({"success": True, "operation": command}), "stderr": "",
                              "returncode": 0})(), ""
    monkeypatch.setattr(S.common_cli, "spawn", spawn)
    return calls


def _names(S, node):
    return {tool["name"] for tool in S.tool_list(node)}


# --------------------------------------------------------------------------- #
# Covered and terminal-only commands
# --------------------------------------------------------------------------- #
def test_a_covered_command_is_not_listed_and_the_tools_that_do_it_are(tmp_path):
    S, node = _node(tmp_path)
    names = _names(S, node)
    assert not names & (set(M.COVERED_TOOLS) | M.TERMINAL_TOOLS)
    covers = {tool for cover in M.COVERED_TOOLS.values() for tool in cover.split(" + ")}
    assert covers <= names


def test_every_covered_command_and_its_cover_are_commands_of_the_reference():
    commands = set(M.command_entries(shared_objects.load()))
    covers = {tool for cover in M.COVERED_TOOLS.values() for tool in cover.split(" + ")}
    assert set(M.COVERED_TOOLS) | covers | M.TERMINAL_TOOLS <= commands


def test_a_call_to_a_command_that_is_not_a_tool_says_what_to_call(tmp_path, ran):
    S, node = _node(tmp_path)
    for covered, cover in M.COVERED_TOOLS.items():
        answer = json.loads(S.call_tool(node, covered, {"request": {}}))
        assert answer["success"] is False and cover in answer["error"] and "not an MCP tool" in answer["error"]
    for asking in M.TERMINAL_TOOLS:
        answer = json.loads(S.call_tool(node, asking, {"request": {"prompt": "go?"}}))
        assert answer["success"] is False and "terminal" in answer["error"]
    assert ran == []


def test_a_command_that_is_not_a_tool_is_not_offered_even_when_nothing_is_switched_off():
    settings = M.parse_mcp_tools(dict(M.DEFAULTS))
    assert settings.disabled_tools == ()
    assert not any(settings.offers(name) for name in [*M.COVERED_TOOLS, *M.TERMINAL_TOOLS, *M.RENAMED_TOOLS])
    assert not any(settings.offers(name) for name in GROUPED - set(M.GROUPED_TOOLS))


# --------------------------------------------------------------------------- #
# Grouped tools
# --------------------------------------------------------------------------- #
def test_a_groups_commands_share_one_request_and_one_hint():
    """The operator's limit on grouping: the same request, the same hint, the choice one enum field."""
    entries = M.command_entries(shared_objects.load())
    operations = M.operation_commands(_catalogue())
    for tool, (field, choices, _text) in M.GROUPED_TOOLS.items():
        members = list(choices.values())
        assert len({id(entries[command]) for command in members}) == 1, tool
        assert len({M.is_read_only(command, operations) for command in members}) == 1, tool
        assert not set(members) & operations, f"{tool}: an emergency operation keeps its own tool"
        assert field not in {f["field"] for f in entries[members[0]].get("fields") or []}, tool


def test_a_grouped_tool_is_listed_once_with_its_choice_first_and_required(tmp_path):
    S, node = _node(tmp_path)
    tools = {tool["name"]: tool for tool in S.tool_list(node)}
    for tool, (field, choices, _text) in M.GROUPED_TOOLS.items():
        schema = tools[tool]["request_schema"]
        assert list(schema["properties"])[0] == field and schema["required"][0] == field
        assert schema["properties"][field]["enum"] == list(choices)
    assert tools["restore"]["destructive"] and tools["restore"]["needs_key"]
    for reads in ("list-objects", "check-config", "sqlserver-cu-check", "list-backup-files"):
        assert tools[reads]["read_only"] and not tools[reads]["needs_key"], reads


@pytest.mark.parametrize("tool,request_,command", [
    ("restore", {"step": "log", "db_type": "sqlserver", "server_id": "SRV"}, "restore-log"),
    ("list-objects", {"kind": "schemas", "server_id": "SRV"}, "list-schemas"),
    ("check-config", {"check": "references"}, "check-references"),
    ("sqlserver-cu-check", {"check": "build", "server_id": "SRV"}, "sqlserver-verify-build"),
    ("list-backup-files", {"action": "prune-plan", "db_type": "sqlserver", "path": "/b"}, "prune-backup-files"),
    ("list-backup-files", {"action": "list", "db_type": "sqlserver", "path": "/b"}, "list-backup-files"),
])
def test_a_grouped_tool_runs_the_command_its_field_names_without_the_field(tmp_path, ran, monkeypatch, tool,
                                                                           request_, command):
    S, node = _node(tmp_path)
    monkeypatch.setattr(node, "key_decision", lambda: type("D", (), {"allowed": True})())
    from dbabrain.lib.data_sources import request_fill
    monkeypatch.setattr(request_fill, "fill_request", lambda _c, request, **_k: request)

    json.loads(S.call_tool(node, tool, {"request": request_}))

    field = M.GROUPED_TOOLS[tool][0]
    assert [c for c, _ in ran] == [command] and field not in ran[0][1]


def test_a_grouped_tool_without_its_choice_is_refused_before_anything_runs(tmp_path, ran):
    S, node = _node(tmp_path)
    answer = json.loads(S.call_tool(node, "list-objects", {"request": {"kind": "tables", "server_id": "SRV"}}))
    assert answer["success"] is False and "databases, schemas, jobs" in answer["error"]
    assert ran == []


def test_a_call_by_a_grouped_commands_own_name_says_the_tool_and_the_value(tmp_path, ran):
    S, node = _node(tmp_path)
    answer = json.loads(S.call_tool(node, "restore-diff", {"request": {}}))
    assert "restore with step 'diff'" in answer["error"]
    answer = json.loads(S.call_tool(node, "prune-backup-files", {"request": {}}))
    assert "list-backup-files with action 'prune-plan'" in answer["error"]
    assert ran == []


def test_a_tool_switched_off_under_a_name_it_had_stays_off(tmp_path):
    """A node that switched restore-log or store_query off is not handed it back by a grouping or a rename."""
    S, node = _node(tmp_path, disabled_tools=["restore-log", "store_query", "list-jobs"])
    names = _names(S, node)
    assert not {"restore", "store-query", "list-objects"} & names
    assert {"store-schema", "check-config"} <= names


# --------------------------------------------------------------------------- #
# Renamed tools
# --------------------------------------------------------------------------- #
def test_the_store_tools_are_kebab_case_and_their_old_names_answer_the_new(tmp_path):
    S, node = _node(tmp_path)
    names = _names(S, node)
    assert {"store-schema", "store-query"} <= names and not {"store_schema", "store_query"} & names
    assert all("_" not in name for name in names), "every tool name is kebab-case"
    answer = json.loads(S.call_tool(node, "store_query", {"sql": "select 1"}))
    assert answer["success"] is False and "store-query" in answer["error"]


# --------------------------------------------------------------------------- #
# What a tool says
# --------------------------------------------------------------------------- #
def test_every_offered_tool_has_its_own_text_and_no_text_is_for_a_tool_not_offered(tmp_path):
    S, node = _node(tmp_path)
    names = _names(S, node) - {M.SQL_WRITE_TOOL}
    assert names == set(TOOL_TEXT), (sorted(names - set(TOOL_TEXT)), sorted(set(TOOL_TEXT) - names))


def test_a_description_opens_with_what_the_tool_does(tmp_path):
    S, node = _node(tmp_path)
    for tool in S.tool_list(node):
        first = tool["description"].split("\n", 1)[0]
        assert not first.startswith("dbabrain common.cli"), tool["name"]
        assert first[0].isupper() and first.split()[0].rstrip(",").isalpha(), (tool["name"], first[:40])
    assert SQL_WRITE_TEXT.startswith("Run one statement that changes")


@pytest.mark.parametrize("tool,siblings", [
    ("fetch-file", ("pull-file", "relay-file")), ("send-file", ("push-file",)),
    ("pull-file", ("fetch-file",)), ("push-file", ("send-file",)),
    ("pack-files", ("pack-backup",)), ("delete-file", ("delete-verified-files",)),
    ("run-sql", ("run-sql-write", "run-sqlcmd")), ("run-sqlcmd", ("run-sql", "run-sql-write")),
    ("host-service", ("host-restart",)), ("list-files", ("list-backup-files", "smb-list")),
    ("restore-metadata", ("sqlserver-replay-instance",)), ("sql-command-add", ("sql-target-add",)),
    ("instance-remove", ("credential-remove", "entry-remove")), ("check-config", ("check-secret",)),
])
def test_a_tool_in_a_family_names_the_sibling_to_take_instead(tool, siblings):
    """Glama, 2026-11-27: *an agent can easily pick the wrong variant for a file transfer, deletion, or SQL
    execution* - each says when the other one is the right one."""
    assert all(sibling in TOOL_TEXT[tool] for sibling in siblings), tool


def test_a_retention_plan_is_a_read(tmp_path):
    operations = M.operation_commands(_catalogue())
    assert M.is_read_only("prune-backup-files", operations) and not M.needs_key("prune-backup-files", operations)
    # Its name still makes every other prune-, delete- command destructive.
    assert M.is_destructive("prune-anything", set()) and M.is_destructive("delete-file", set())
