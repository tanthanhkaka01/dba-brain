"""An MCP tool states its request's fields in its input schema, not in prose.

Every command tool takes one ``request`` object, and the SDK advertised it as an object of any shape: the fields
were a list inside the description, which an agent had to read as text before it could build a call, and which
Glama's score of the server (2026-11-26) counted as parameters left undocumented. The schema is now the reference's
own - each field's type, allowed values, bounds and purpose, and which are required - and the prose list is gone.

What the node fills from its inventory (the connection, the host login, the policy, the rules, the secrets) is left
out, and ``server_id`` leads: the agent names a server and is never offered a place to put a password.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from dbabrain.lib import mcp_tools as M
from dbabrain.lib import shared_objects
from dbabrain.lib.data_sources import request_fill
from dbabrain.mcp import server as S

REPO = Path(__file__).resolve().parents[1]


def _node(tmp_path):
    catalogue = json.loads((REPO / "dbabrain/common/catalogue/emergency_operations.json").read_text(encoding="utf-8"))
    return S.Node(data_dir=tmp_path, config_path=None, settings=M.parse_mcp_tools(dict(M.DEFAULTS)),
                  reference=shared_objects.load(), operations=M.operation_commands(catalogue))


@pytest.fixture(scope="module")
def entries():
    return M.command_entries(shared_objects.load())


def test_every_field_of_the_reference_becomes_a_property_with_its_purpose(entries):
    entry = entries["db-activity"]
    schema = M.request_schema("db-activity", entry, fills=request_fill.FILLS["db-activity"])
    view = schema["properties"]["view"]
    assert view["type"] == "string" and set(view["enum"]) == {"requests", "blocking", "waits", "top_queries"}
    rows = schema["properties"]["max_rows"]
    assert rows["type"] == "integer" and rows["minimum"] == 1 and rows["maximum"] == 1000
    assert all(prop.get("description") for prop in schema["properties"].values())


def test_a_field_the_node_fills_is_never_offered_and_server_id_leads(entries):
    for command, fills in request_fill.FILLS.items():
        if command not in entries:
            continue
        schema = M.request_schema(command, entries[command], fills=fills)
        hidden = {name for kind in fills for name in M.FILLED_FIELDS.get(kind, ())}
        assert not hidden & set(schema["properties"]), command
        assert not hidden & set(schema.get("required", [])), command
        if command not in M.FILLED_PER_SIDE:
            assert next(iter(schema["properties"])) == "server_id", command


def test_a_field_an_mcp_request_is_refused_for_is_never_offered(entries):
    """A secret, or how a server is reached: the call refuses them, so the schema must not invite them."""
    refused = set(M.SECRET_KEYS) | {"sql_access"}
    offered = sorted(f"{command}.{name}" for command, entry in entries.items()
                     for name in M.request_schema(command, entry, fills=request_fill.FILLS.get(command, ()))["properties"]
                     if name in refused)
    assert offered == []


def test_metric_batch_is_named_by_server_id_not_by_a_connection_object(entries):
    """Its reference states `target` as the connection facts, required: offered as such, it asked the agent to
    write a login - which the call refuses. The node builds that object from `server_id`."""
    schema = M.request_schema("metric-batch", entries["metric-batch"], fills=request_fill.FILLS["metric-batch"])
    assert next(iter(schema["properties"])) == "server_id"
    assert "target" not in schema.get("required", []) and "items" in schema["required"]
    assert "secrets" not in schema["properties"]


def test_every_reference_type_is_a_json_schema_type(entries):
    """A type the schema could not carry would leave a field untyped without anyone noticing."""
    unknown = sorted({str(f.get("type")) for e in entries.values() for f in e.get("fields") or []}
                     - {"string", "boolean", "object", "integer", "array", "number"})
    assert unknown == []


def test_a_required_field_is_required_in_the_schema(entries):
    for command, entry in entries.items():
        fills = request_fill.FILLS.get(command, ())
        schema = M.request_schema(command, entry, fills=fills)
        hidden = {name for kind in fills for name in M.FILLED_FIELDS.get(kind, ())} | M.SECRET_KEYS | {"sql_access"}
        expected = [f["field"] for f in entry.get("fields") or [] if f.get("required") and f["field"] not in hidden
                    and not (fills and command not in M.FILLED_PER_SIDE and f["field"] == "target")]
        assert schema.get("required", []) == expected, command


def test_the_sdk_advertises_the_schema_and_the_description_no_longer_lists_fields(tmp_path):
    pytest.importorskip("mcp")
    node = _node(tmp_path)
    server = S.build_server(node)
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    request = tools["table-info"].input_schema["properties"]["request"]
    assert request["type"] == "object" and "object" in request["properties"] and "connection" not in request["properties"]
    assert set(tools["table-info"].input_schema["properties"]) == {"request"}
    assert all("Request fields" not in tool.description for tool in tools.values() if tool.name not in
               ("store_schema", "store_query"))
    assert tools[M.SQL_WRITE_TOOL].input_schema == tools["run-sql"].input_schema
