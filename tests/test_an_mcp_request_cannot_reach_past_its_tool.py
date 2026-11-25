"""An MCP request gets what its tool is for, and nothing behind it (1.1.0, 1.415 and 1.416).

Why it matters: an MCP request is written by a model, and a model writes what the text it read told it
to. Reviewed on 2026-11-22: three tools the client is told only read could start a program on the node
(`metric-batch`; `sql_access` in `run-sql` and the `list-*` commands), `sql_access` could also send the
node's login or any stored secret to an address the request named, and what `define` and `prelude` put
into the SQL was never read by the check. The operator, the same day: *MCP keeps all its functions, none
is removed* - so every command stays a tool, and these hold what a request may not reach:

* how a server is reached is the inventory's, whatever tool asks - and a login this node fills in goes
  only there, for the console and the bot too;
* what becomes part of `run-sql`'s statement is checked as the statement is;
* what is switched off by name stays off through any other tool;
* a tool the client is told only reads is named here with its fields: a new one is judged, not assumed.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from dbabrain.common import smb
from dbabrain.lib import mcp_tools as M
from dbabrain.lib import shared_objects
from dbabrain.lib.data_sources import request_fill
from dbabrain.lib.read_only_sql import ReadOnlyRefusal
from dbabrain.mcp import server as S

REPO = Path(__file__).resolve().parents[1]
SECRET = "S3cret-Value-42!"


def _operations():
    catalogue = json.loads((REPO / "dbabrain/common/catalogue/emergency_operations.json").read_text(encoding="utf-8"))
    return M.operation_commands(catalogue)


def _node(tmp_path, **settings):
    node = S.Node(data_dir=tmp_path, config_path=None, settings=M.parse_mcp_tools({**M.DEFAULTS, **settings}),
                  reference=shared_objects.load(REPO / "data"), operations=_operations())
    node._secrets = {"SQL_PASSWORD": SECRET}
    return node


@pytest.fixture
def ran(monkeypatch):
    """The command and the request each call would have run; nothing is started."""
    calls: list[tuple[str, dict]] = []
    answers: dict[str, dict] = {}

    def spawn(command, request, **_k):
        calls.append((command, request))
        envelope = {"success": True, "operation": command, "data": answers.get(command, {})}
        return SimpleNamespace(stdout=json.dumps(envelope), stderr="", returncode=0), ""

    monkeypatch.setattr(S, "_log", lambda node, **row: None)
    monkeypatch.setattr(request_fill, "fill_request", lambda command, request, **_k: dict(request))
    monkeypatch.setattr(S.common_cli, "spawn", spawn)
    return SimpleNamespace(calls=calls, answers=answers)


def _call(node, tool, request):
    return json.loads(S.call_tool(node, tool, {"request": request}))


# --------------------------------------------------------------------------- #
# How a server is reached is the inventory's
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("stated", [
    {"method": "subprocess", "launcher": ["prog"]}, {"method": "subprocess", "tool_dir": "/x"},
    {"method": "subprocess", "python_exe": "/x"}, {"method": "API", "bridge_url": "http://192.0.2.1/"},
    {"secret_ref": "A"}, {"method": "direct", "connect_ref": "A"}, [["method", "subprocess"]], "api",
])
@pytest.mark.parametrize("tool", ["run-sql", M.SQL_WRITE_TOOL, "list-databases", "list-jobs", "list-schemas"])
def test_no_tool_takes_a_way_in_from_the_request(tmp_path, ran, tool, stated):
    answer = _call(_node(tmp_path), tool, {"server_id": "SRV", "sql_text": "SELECT 1", "sql_access": stated})

    assert answer["success"] is False and "inventory" in answer["error"]
    assert ran.calls == []


@pytest.mark.parametrize("request_", [
    {"connection": {"db_type": "oracle", "host": "192.0.2.1", "sql_access": {"method": "api", "bridge_url": "u"}}},
    {"target": {"sql_access": {"method": "subprocess", "launcher": ["p"]}}, "items": []},
    {"source": {"target": "A", "sql_access": {"method": "api"}}},
])
def test_a_way_in_is_seen_wherever_it_sits_in_the_request(request_):
    assert M.routes_stated(request_)


@pytest.mark.parametrize("request_", [{}, {"sql_access": None}, {"sql_access": {}}, {"sql_access": {"method": "direct"}},
                                      {"sql_text": "SELECT launcher, bridge_url FROM t"}])
def test_the_ordinary_connection_is_not_one(request_):
    assert M.routes_stated(request_) == []


# --------------------------------------------------------------------------- #
# What becomes part of the statement is checked as the statement is
# --------------------------------------------------------------------------- #
def test_run_sql_keeps_the_fields_it_takes(tmp_path, ran):
    request = {"server_id": "SRV", "sql_text": "SELECT @a, '&JOB' AS j", "prelude": "DECLARE @a int = ?;\n", "params": [1],
               "define": {"JOB": "AA1"}, "driver": "ODBC Driver 18 for SQL Server", "database_name": "D", "capture": "all",
               "max_rows": 5, "timeout_seconds": 9, "credential_name": "reader", "profile": {"major_version": "16"},
               "format": "xlsx", "output_path": "runtime/output/mcp/a.xlsx", "sql_access": {"method": "direct"}}

    assert _call(_node(tmp_path), "run-sql", request)["success"] is True
    [(_command, sent)] = ran.calls
    assert sent["sql_text"] == "SELECT @a, 'AA1' AS j" and "define" not in sent
    assert {key: sent[key] for key in request if key not in ("sql_text", "define")} == {
        key: request[key] for key in request if key not in ("sql_text", "define")}


@pytest.mark.parametrize("request_", [
    {"sql_text": "SELECT 1 FROM &T", "define": {"T": "x DROP TABLE y"}},
    {"sql_text": "SELECT 1 FROM &T", "defines": {"T": "x; DELETE y"}},
    {"sql_text": "DEFINE W = DELETE\n&W FROM t"},
    # A value that brings a DEFINE line of its own would be substituted after the check.
    {"sql_text": "SELECT 1 &A", "define": {"A": "\nDEFINE W = DROP\n&W TABLE t"}},
    {"sql_text": "SELECT 1", "prelude": "DROP TABLE t;"},
    {"sql_text": "SELECT 1", "prelude": "DECLARE @a int = ?; DELETE t;"},
    {"sql_text": "SELECT 1", "prelude": "DECLARE @a int = (SELECT 1);"},
    {"sql_text": "SELECT 1", "driver": "/tmp/lib.so"},
    {"sql_text": "SELECT 1", "define": ["T"]},
])
def test_what_is_put_into_the_statement_is_read_before_it_runs(request_):
    with pytest.raises(ReadOnlyRefusal):
        M.read_only_request({"server_id": "SRV", **request_})


def test_a_substitution_that_names_itself_is_refused_not_followed():
    with pytest.raises(ReadOnlyRefusal, match="do not end"):
        M.read_only_request({"sql_text": "SELECT &A", "define": {"A": "&A x"}})


@pytest.mark.parametrize("prelude", ["DECLARE @a int = ?;", "declare @n nvarchar(4000) = ?;\nDECLARE @d decimal(10, 2) = ?;\n",
                                     "DECLARE @m nvarchar(max) = ?;"])
def test_the_prelude_the_parameters_are_declared_with_is_kept(prelude):
    assert M.read_only_request({"sql_text": "SELECT 1", "prelude": prelude})["prelude"] == prelude


# --------------------------------------------------------------------------- #
# A tool that only reads writes nowhere but the node's output folders
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", ["/etc/cron.d/x", "C:/x.xlsx", "data/mcp_tools.json", "runtime/dbabrain.sqlite",
                                  "runtime/output/../../data/a.json", "runtime/output/", "runtime\\output\\..\\a",
                                  "runtime/output//a"])
def test_a_read_only_tool_refuses_to_write_elsewhere(tmp_path, ran, path):
    for tool, field in (("run-sql", "output_path"), ("sqlserver-verify-instance", "output_dir")):
        answer = _call(_node(tmp_path), tool, {"server_id": "SRV", "sql_text": "SELECT 1", field: path})
        assert answer["success"] is False and field in answer["error"]
    assert ran.calls == []


def test_a_tool_that_changes_things_writes_where_it_is_told(tmp_path, ran):
    assert _call(_node(tmp_path), M.SQL_WRITE_TOOL, {"server_id": "S", "sql_text": "SELECT 1", "output_path": "/x/a.xlsx"
                                                     })["success"] is True


# --------------------------------------------------------------------------- #
# What is switched off stays off through any other tool
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kind", ["local", "script"])
def test_a_metric_item_runs_no_command_where_run_cmd_is_off(tmp_path, ran, kind):
    request = {"target": {"target_id": "t"}, "items": [{"id": "1", "kind": kind, "argv": ["p"], "script": "x"}]}

    assert "run-cmd is switched off" in _call(_node(tmp_path, disabled_tools=["run-cmd"]), "metric-batch", request)["error"]
    assert ran.calls == []
    assert _call(_node(tmp_path), "metric-batch", request)["success"] is True


def test_a_metric_items_sql_only_reads_where_writing_is_off(tmp_path, ran):
    off = _node(tmp_path, disabled_tools=[M.SQL_WRITE_TOOL])
    write = {"server_id": "SRV", "items": [{"id": "1", "kind": "sql", "sql": "DELETE FROM t"}]}

    assert "DELETE" in _call(off, "metric-batch", write)["error"] and ran.calls == []
    assert _call(off, "metric-batch", {"server_id": "SRV", "items": [{"id": "1", "kind": "sql", "sql": "SELECT 1"}]})["success"]
    assert _call(_node(tmp_path), "metric-batch", write)["success"] is True


def test_the_client_is_told_what_the_tools_that_change_things_do(tmp_path):
    tools = {tool["name"]: tool for tool in S.tool_list(_node(tmp_path))}

    assert all(tools[name]["destructive"] for name in ("metric-batch", "guide", "inventory-summary"))
    assert tools["key-status"]["read_only"]


# --------------------------------------------------------------------------- #
# read-file answers base64, where no redaction matches
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", ["/app/data/encrypted_secret_text.json", "C:\\db\\secrets\\secret_text.json",
                                  "/app/data/ssh_keys/worker.key", "/app/secrets/ssh/id", "/app/.env", "/x/prod.ENV"])
def test_a_nodes_own_secret_files_are_not_read(tmp_path, ran, path):
    answer = _call(_node(tmp_path), "read-file", {"path": path})

    assert answer["success"] is False and "secret" in answer["error"] and ran.calls == []


@pytest.mark.parametrize("coding", ["utf-8", "utf-16-le"])
def test_a_file_that_holds_a_stored_secret_is_not_answered(tmp_path, ran, coding):
    ran.answers["read-file"] = {"base64": base64.b64encode(f"password is {SECRET}".encode(coding)).decode()}
    answer = _call(_node(tmp_path), "read-file", {"path": "/srv/app.conf"})

    assert answer["success"] is False and "base64" not in json.dumps(answer)


def test_the_certificate_pair_it_is_for_is_still_read(tmp_path, ran):
    ran.answers["read-file"] = {"base64": base64.b64encode(b"\x1e\xf1\xb5\xb0 a key").decode()}

    assert _call(_node(tmp_path), "read-file", {"path": "D:/BK/cert/a.pvk"})["data"]["base64"]


def test_a_share_path_never_ends_its_own_quotes():
    for path in ('a" ; !prog ; cd "b', "a\n!prog", "a\r!prog"):
        with pytest.raises(smb.SmbError, match="quote"):
            smb._subpath(path)
    assert smb._subpath("/SQLBK/APP DB's/") == "SQLBK\\APP DB's"


# --------------------------------------------------------------------------- #
# A login this node fills in goes only where its inventory sends it
# --------------------------------------------------------------------------- #
BRIDGE = {"method": "api", "bridge_url": "http://192.0.2.9:8765/query", "secret_ref": "BRIDGE_REF"}
SECRETS = {"P_REF": "p-value", "BRIDGE_REF": "sign-me", "OTHER_REF": "other"}


@pytest.fixture
def data_dir(tmp_path):
    def instance(server_id, **more):
        return {"server_id": server_id, "ip": "192.0.2.5", "port": 1521, "db_type": "oracle", "platform": "linux",
                "service_name": "ORCL", "instance_name": "ORCL", "database": "ORCL", "default_credential_name": "lab", **more}

    (tmp_path / "db_instances.json").write_text(json.dumps({"db_instances": [
        instance("LAB-DIRECT", sql_access={"method": "direct"}), instance("LAB-8I", sql_access=BRIDGE)]}), encoding="utf-8")
    (tmp_path / "users.json").write_text(json.dumps({"database_credentials": [
        {"server_id": server_id, "db_type": "oracle", "credentials": [
            {"credential_name": "lab", "username": "u", "password_ref": "P_REF", "role": "DBA"}]}
        for server_id in ("LAB-DIRECT", "LAB-8I")], "remote_credentials": []}), encoding="utf-8")
    return tmp_path


def _fill(command, request, data_dir):
    return request_fill.fill_request(command, request, data_dir=data_dir, secrets=SECRETS)


@pytest.mark.parametrize("command", ["run-sql", "list-databases"])
@pytest.mark.parametrize("stated", [{**BRIDGE, "bridge_url": "http://198.51.100.1/"}, {"method": "subprocess", "launcher": ["p"]},
                                    {"method": "api", "bridge_url": "http://198.51.100.1/", "connect_ref": "OTHER_REF"}])
def test_a_filled_login_is_not_sent_where_the_request_says(data_dir, command, stated):
    for server_id in ("LAB-DIRECT", "LAB-8I"):
        with pytest.raises(request_fill.RequestFillError, match="sql_access is the inventory's"):
            _fill(command, {"target": server_id, "sql_text": "select 1", "sql_access": stated}, data_dir)


def test_a_bridge_the_inventory_names_is_reached_with_its_own_secret_only(data_dir):
    for request in ({"target": "LAB-8I"}, {"target": "LAB-8I", "sql_access": BRIDGE}):
        filled = _fill("run-sql", {**request, "sql_text": "select 1"}, data_dir)
        assert filled["connection"]["sql_access"] == BRIDGE and filled["secrets"] == {"BRIDGE_REF": "sign-me"}


def test_a_connection_the_caller_states_gets_no_secret_of_this_node(data_dir):
    stated = {"db_type": "oracle", "host": "192.0.2.5", "username": "u", "password": "theirs"}

    assert "secrets" not in _fill("run-sql", {"connection": stated, "sql_text": "select 1", "sql_access": BRIDGE}, data_dir)


def test_a_metric_batch_is_completed_from_a_server_id(data_dir):
    items = [{"id": "1", "kind": "sql", "sql": "select 1 from dual"}]
    direct = _fill("metric-batch", {"server_id": "LAB-DIRECT", "items": items}, data_dir)
    bridged = _fill("metric-batch", {"target": "LAB-8I", "items": items}, data_dir)

    assert (direct["target"]["host"], direct["target"]["password"]) == ("192.0.2.5", "p-value")
    assert "server_id" not in direct and "secrets" not in direct
    assert bridged["target"]["sql_access"] == BRIDGE and bridged["secrets"] == {"BRIDGE_REF": "sign-me"}
    assert bridged["target"]["credential"]["username"] == "u"


def test_the_metrics_apps_own_request_is_kept_as_it_is(data_dir):
    request = {"target": {"target_id": "t", "password": "x"}, "secrets": {"A": "b"}, "items": [{"id": "1", "kind": "local"}]}

    assert _fill("metric-batch", request, data_dir) == request


# --------------------------------------------------------------------------- #
# Every tool the client is told only reads, with what its request may state
# --------------------------------------------------------------------------- #
#: A tool or a field that is not here fails the test below. Before adding one, read what it does with the
#: request: starts a program, writes a file, sends a login somewhere, or becomes part of a statement or a
#: command line. If it does any of these it is not a read, or the MCP server holds it (`lib.mcp_tools`).
READ_ONLY_TOOLS = {
    "check-credentials": "data_dir format",
    "check-identifiers": "allow extensions extra_terms from_inventory max_examples paths root",
    "check-objects": "data_dir format",
    "check-references": "data_dir format",
    "check-secret": "allow_name_host match refs timeout_seconds",
    "check-secret-literals": "paths root store",
    "db-status": "connection database_name database_names depth target",
    "db-activity": "connection credential_name database_name max_rows object sql_access sql_text target timeout_seconds "
                   "view",
    "describe-object": "field object",
    "due-check": "default_repeat last_run last_status local_now now retry_default row time_window timeout_default",
    "file-checksum": "algorithm container folder host include max_files paths timeout_seconds",
    "host-facts": "access action assume_yes authorized_by confirm dry_run evidence ignore_window overrides platform policy "
                  "reason rules services sudo target wait window",
    "key-status": "online server",
    "list-backup-files": "after before cleanup_retention database_name db_type delete host kinds latest mode path target "
                         "timeout_seconds wal_segments",
    "list-databases": "connection credential_name database_name enabled_only include_system key key_base64 sql_access "
                      "target timeout_seconds",
    "list-files": "folder host max_files timeout_seconds with_folders",
    "list-jobs": "connection credential_name database_name enabled_only include_system key key_base64 sql_access target "
                 "timeout_seconds",
    "list-schemas": "connection credential_name database_name enabled_only include_system key key_base64 sql_access "
                    "target timeout_seconds",
    "list-targets": "format",
    "object-source": "connection credential_name database_name max_rows object sql_access sql_text target "
                     "timeout_seconds view",
    "probe-host": "host ports target timeout_seconds",
    "query-plan": "connection credential_name database_name max_rows object sql_access sql_text target timeout_seconds "
                  "view",
    "read-file": "host max_bytes path timeout_seconds",
    "self-status": "format last_runs store_error",
    "smb-list": "backend domain force host local_path password path paths recurse remote_path share suffixes target "
                "timeout_seconds username",
    "sqlserver-precheck": "access assume_yes authorized_by confirm connection credential_name dry_run expected_build "
                          "ignore_window installer installer_sha256 instance_name kb policy reason registry_key rules "
                          "setup_account skip_hash target window",
    "sqlserver-verify-build": "access assume_yes authorized_by confirm connection credential_name dry_run expected_build "
                              "ignore_window installer installer_sha256 instance_name kb policy reason registry_key rules "
                              "setup_account skip_hash target window",
    "sqlserver-verify-instance": "assume_yes authorized_by bundle_dir confirm connection credential_name dry_run include "
                                 "on_missing_secret on_unsupported output_dir overrides phase policy reason rules "
                                 "secret_prefix secrets target",
    "table-info": "connection credential_name database_name max_rows object sql_access sql_text target timeout_seconds "
                  "view",
    "trace-session": "blocking_only connection key key_base64 min_tran_seconds session_id target",
    "verify-encrypted-file": "host identity path plain_sha256 plain_size_bytes sha256 size_bytes timeout_seconds transfer",
    "verify-restore": "consistency consistency_jobs consistency_timeout_seconds database_name database_names db_type host "
                      "oracle_sid port target timeout_seconds username",
}


def test_a_tool_that_only_reads_and_each_of_its_fields_was_judged():
    operations = _operations()
    entries = M.command_entries(shared_objects.load(REPO / "data"))
    now = {command: " ".join(sorted(str(field["field"]) for field in entry.get("fields") or []))
           for command, entry in entries.items() if M.is_read_only(command, operations)}

    assert now == READ_ONLY_TOOLS
