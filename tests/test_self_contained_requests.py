"""Reaching a database or a host that is in no inventory at all.

``run-sql`` had one way in: name a ``target``, and the resolver reads ``db_instances.json`` for the
host and ``users.json`` for the login. That is the right default for a runbook or a scheduled task
and it stays the default. What it made impossible is the case an incident tends to be — a machine
nobody has registered yet — and it made every caller inherit two file reads whether it wanted them
or not.

So both commands grew a second door: ``run-sql`` takes a ``connection`` block, ``run-cmd`` takes an
``access`` block that carries its own login. The promise these tests hold is narrow and exact:
**with that door, no inventory file is opened**. The way they hold it is to point ``data_dir`` at an
empty directory, which is the only assertion that cannot be satisfied by a lookup that happens to
succeed.
"""

import pytest

from db_ops.common import host_ops, sql_run
from db_ops.lib.cmd_access import resolve_cmd_credential
from db_ops.lib.connection_spec import ConnectionSpec, ConnectionSpecError


# --------------------------------------------------------------------------- #
# run-sql: the connection block
# --------------------------------------------------------------------------- #
def _spec(**overrides):
    payload = {
        "db_type": "sqlserver", "host": "10.0.0.5", "username": "monitor",
        "password": "secret", "major_version": 16,
    }
    payload.update(overrides)
    return ConnectionSpec.from_json(payload)


def test_a_connection_block_carries_everything_the_resolver_used_to_look_up():
    spec = _spec(port=14330, database="AppDb", label="lab-mssql")
    resolved = spec.to_resolved(password="secret")

    assert resolved["ip"] == "10.0.0.5" and resolved["port"] == 14330
    assert resolved["database_name"] == "AppDb"
    assert resolved["username"] == "monitor" and resolved["password"] == "secret"
    assert resolved["server_id"] == "lab-mssql"
    # The version rides in the same block as the host it describes: a request that is
    # self-contained about the connection must be self-contained about the tool too.
    assert resolved["profile"].major_version == 16


def test_the_label_defaults_to_the_host_so_an_answer_still_names_something():
    assert _spec().to_resolved(password="x")["server_id"] == "10.0.0.5"
    assert _spec().to_resolved(password="x")["credential_name"] == "inline:monitor"


@pytest.mark.parametrize(
    "missing, message",
    [
        ({"db_type": ""}, "db_type is required"),
        ({"db_type": "informix"}, "not supported"),
        ({"host": ""}, "host is required"),
        ({"username": ""}, "username is required"),
        ({"password": ""}, "needs a password"),
    ],
)
def test_an_incomplete_connection_is_refused_by_the_field_it_is_missing(missing, message):
    """Three fields no default can invent — which engine, which machine, which login. A block
    missing one is not an under-specified connection, it is a different target."""
    with pytest.raises(ConnectionSpecError, match=message):
        _spec(**missing)


def test_run_sql_reads_no_inventory_when_the_request_carries_the_connection(monkeypatch, tmp_path):
    """The assertion that cannot be faked: `data_dir` is an empty directory, so any lookup at all
    would fail rather than quietly succeed against this repo's own data folder."""
    opened = {}

    class _Cursor:
        description = [("x",)]
        rowcount = 1

        def execute(self, sql, *args):  # noqa: ARG002
            return None

        _served = False

        def fetchmany(self, size):  # noqa: ARG002
            # One page then empty, or the capture loop keeps asking until it hits max_rows.
            if self._served:
                return []
            self._served = True
            return [[1]]

        def nextset(self):
            return None

    class _Conn:
        def cursor(self):
            return _Cursor()

        def rollback(self):
            return None

        def close(self):
            return None

    def fake_connect(target, **_kwargs):
        opened.update(target)
        return _Conn()

    monkeypatch.setattr(sql_run, "connect_target", fake_connect)

    result = sql_run.run_sql({
        "connection": {"db_type": "sqlserver", "host": "10.0.0.5", "username": "monitor",
                       "password": "secret", "major_version": 16, "label": "lab-mssql"},
        "sql": "SELECT 1 AS x",
        "data_dir": str(tmp_path),
    })

    assert result["rows"] == [[1]]
    assert result["server_id"] == "lab-mssql"
    assert opened["ip"] == "10.0.0.5"
    # Every fact is attributed to the request, which is the same statement as "nothing was read".
    assert result["engine"]["sources"]["major_version"] == "request"
    assert result["tool"]["chosen_by"] == "default"


def test_without_either_door_the_error_names_both(tmp_path):
    with pytest.raises(sql_run.SqlRunError, match='"connection" object'):
        sql_run.run_sql({"sql": "SELECT 1", "data_dir": str(tmp_path)})


def test_a_version_in_the_connection_still_refuses_an_impossible_driver(tmp_path):
    """The self-contained door does not skip the tool rule — an 8i host stated inline is refused
    the same way one resolved from the inventory is."""
    with pytest.raises(sql_run.SqlRunError, match="thin mode"):
        sql_run.run_sql({
            "connection": {"db_type": "oracle", "host": "192.0.2.9", "service_name": "LEGACYDB",
                           "username": "sys", "password": "x", "major_version": 8},
            "sql": "select 1 from dual",
            "data_dir": str(tmp_path),
        })


# --------------------------------------------------------------------------- #
# run-cmd: the access block that carries its own login
# --------------------------------------------------------------------------- #
def test_an_access_block_with_its_own_login_never_consults_the_credentials_file():
    """`groups` is deliberately a list this call must not need: passing entries that would match
    proves the inline answer wins rather than merely working when the file happens to be absent."""
    block = {"method": "winrm", "host": "10.0.0.7", "username": "svc", "password": "p"}
    decoys = [{"host": "10.0.0.7", "credentials": [{"credential_name": "other", "username": "wrong"}]}]

    credential = resolve_cmd_credential(block, decoys)

    assert credential == {"credential_name": "inline:svc", "username": "svc", "password": "p"}


def test_a_named_credential_still_wins_over_an_inline_one():
    """Naming an entry is asking for *that* entry, not for whatever else the block happens to
    hold — otherwise a leftover username in a block would silently redirect the login."""
    block = {"method": "winrm", "host": "10.0.0.7", "credential_name": "named",
             "username": "svc", "password": "p"}
    groups = [{"host": "10.0.0.7", "credentials": [{"credential_name": "named", "username": "real"}]}]

    assert resolve_cmd_credential(block, groups)["username"] == "real"


def test_an_inline_username_without_a_password_is_still_refused():
    """Half a credential is not a credential. Without this, a block naming only a user would fall
    through to "no credential" and the connect would fail somewhere less obvious."""
    block = {"method": "winrm", "host": "10.0.0.7", "username": "svc"}
    with pytest.raises(RuntimeError, match="credential_name is required"):
        resolve_cmd_credential(block, [])


def test_resolve_host_builds_a_target_from_an_inline_block_against_an_empty_data_dir(tmp_path):
    target = host_ops.resolve_host(
        {"access": {"method": "winrm", "host": "10.0.0.7", "username": "svc", "password": "p",
                    "os": "Windows Server 2008 R2"}},
        data_dir=str(tmp_path),
    )

    assert target.host == "10.0.0.7" and target.platform == "windows"
    assert (target.credential or {})["credential_name"] == "inline:svc"
    # The OS came in with the block, so the dialect rule can answer — this host predates
    # Get-CimInstance and a fact script must know that.
    assert target.to_dict()["shell_dialect"]["tool"] == "wmi"
