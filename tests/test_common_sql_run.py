"""The shared 'run SQL on one database' path: the JSON request object, the runner, the
``run-sql`` CLI facade, and the SQL Server type converters the driver needs."""

import datetime
import json
import struct

import pytest

from db_ops.common import cli, sql_execution, sql_run
from db_ops.lib.target_profile import TargetProfile


# --------------------------------------------------------------------------- #
# datetimeoffset (SQL_SS_TIMESTAMPOFFSET, type -155)
# --------------------------------------------------------------------------- #


def _dto_bytes(year, month, day, hour, minute, second, nanoseconds, tz_hour, tz_minute):
    return struct.pack(
        "<6hI2h", year, month, day, hour, minute, second, nanoseconds, tz_hour, tz_minute
    )


def test_decode_timestampoffset_returns_aware_datetime():
    raw = _dto_bytes(2026, 7, 28, 13, 39, 5, 123_456_700, 7, 0)
    value = sql_execution.decode_timestampoffset(raw)
    assert value == datetime.datetime(
        2026, 7, 28, 13, 39, 5, 123456, datetime.timezone(datetime.timedelta(hours=7))
    )
    assert value.utcoffset() == datetime.timedelta(hours=7)


def test_decode_timestampoffset_handles_negative_offset():
    value = sql_execution.decode_timestampoffset(_dto_bytes(2026, 1, 1, 0, 0, 0, 0, -3, -30))
    assert value.utcoffset() == datetime.timedelta(hours=-3, minutes=-30)


def test_decode_timestampoffset_of_unexpected_length_does_not_raise():
    # A readable cell beats failing the whole export.
    assert sql_execution.decode_timestampoffset(b"1234") is not None
    assert sql_execution.decode_timestampoffset(None) is None


def test_connect_sqlserver_registers_the_datetimeoffset_converter():
    """Without this converter pyodbc fails the query with
    'ODBC SQL type -155 is not yet supported' (HY106) on any datetimeoffset column."""

    class FakeConn:
        def __init__(self):
            self.converters = {}

        def add_output_converter(self, sql_type, func):
            self.converters[sql_type] = func

    class FakePyodbc:
        def __init__(self):
            self.conn = FakeConn()

        def drivers(self):
            return ["ODBC Driver 18 for SQL Server"]

        def connect(self, conn_str, timeout):  # noqa: ARG002 - the fake ignores both.
            return self.conn

    module = FakePyodbc()
    conn = sql_execution.connect_sqlserver(
        module, server="h,1433", database="SALESDB", username="u", password="p", timeout=5
    )
    assert sql_execution.SQL_SS_TIMESTAMPOFFSET == -155
    assert -155 in conn.converters
    assert conn.converters[-155](_dto_bytes(2026, 7, 28, 1, 2, 3, 0, 7, 0)).year == 2026


def test_register_output_converters_ignores_drivers_without_support():
    class PymssqlLikeConn:
        pass

    conn = PymssqlLikeConn()
    assert sql_execution.register_output_converters(conn) is conn  # no crash, returns conn


# --------------------------------------------------------------------------- #
# The JSON request object
# --------------------------------------------------------------------------- #


def test_request_from_json_accepts_dict_and_text():
    payload = {"target": "ACME-x", "sql": "SELECT 1", "database": "SALESDB", "max_rows": 10}
    parsed = sql_run.SqlRunRequest.from_json(payload)
    assert parsed.target == "ACME-x"
    assert parsed.database == "SALESDB"
    assert parsed.max_rows == 10
    assert parsed.timeout_seconds == sql_run.DEFAULT_TIMEOUT_SECONDS
    assert parsed.commit is False
    assert sql_run.SqlRunRequest.from_json(json.dumps(payload)) == parsed


def test_request_from_json_reads_sql_file(tmp_path):
    sql_file = tmp_path / "query.sql"
    sql_file.write_text("﻿SELECT 1 AS x", encoding="utf-8")  # SSMS writes a BOM
    parsed = sql_run.SqlRunRequest.from_json({"target": "ACME-x", "sql_file": str(sql_file)})
    assert parsed.sql == "SELECT 1 AS x"


@pytest.mark.parametrize(
    "payload, message",
    [
        ({}, "target is required"),
        ({"target": "ACME-x"}, "sql is required"),
        ({"target": "ACME-x", "sql": "SELECT 1", "sql_file": "q.sql"}, "not both"),
        ({"target": "ACME-x", "sql_file": "missing.sql"}, "not found"),
        ({"target": "ACME-x", "sql": "SELECT 1", "max_rows": 0}, "max_rows must be >= 1"),
        ({"target": "ACME-x", "sql": "SELECT 1", "timeout_seconds": "soon"}, "must be an integer"),
        ("[]", "must be a JSON object"),
        ("{oops", "not valid JSON"),
    ],
)
def test_request_from_json_rejects_bad_input(payload, message):
    with pytest.raises(sql_run.SqlRunError, match=message):
        sql_run.SqlRunRequest.from_json(payload)


# --------------------------------------------------------------------------- #
# run_sql
# --------------------------------------------------------------------------- #


class FakeCursor:
    def __init__(self, result_sets):
        self._sets = result_sets
        self._index = -1
        self.description = None
        self.rowcount = -1
        self._rows = []
        self._pos = 0
        self.executed = []
        self.bound: list = []
        self.arity: list[int] = []

    def execute(self, sql, *args):
        # `*args` rather than `params=None` on purpose: it records the **arity** of the call, not
        # just the value. A statement with no placeholders must reach the driver as
        # `execute(sql)` with one argument — pg8000 raises on `execute(sql, ())` — and a default
        # parameter would make the two indistinguishable in these assertions.
        self.executed.append(sql)
        self.bound.append(args[0] if args else None)
        self.arity.append(1 + len(args))
        self._index = 0
        self._load()

    def _load(self):
        if 0 <= self._index < len(self._sets):
            self.description, rows, self.rowcount = self._sets[self._index]
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
        self.committed = False
        self.closed = False

    def cursor(self):
        return self._cursor

    def rollback(self):
        self.rolled_back = True

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


def _patch(monkeypatch, conn, **resolved):
    target = {"server_id": "ACME-x", "db_type": "sqlserver", "database_name": "master", **resolved}

    def fake_resolve(spec, data_dir=None, database="", credential_name="", sql_access=None,
                     profile=None, driver="", oracle_client_mode=""):  # noqa: ARG001
        from db_ops.lib.target_profile import TargetProfile

        stated = profile or TargetProfile()
        return {
            **target,
            "database_name": database or target["database_name"],
            "credential_name": credential_name or target.get("credential_name", ""),
            # Every target here is a normal database connection; the legacy Oracle transports
            # have their own tests.
            "sql_access": sql_access or {"method": "direct"},
            # The resolver merges the request's profile over the inventory's; a double that
            # returned an empty one would hide a caller that stopped passing it through.
            "profile": stated.with_(db_type=target["db_type"]),
            "tool": {"tool": driver or "auto", "chosen_by": "default", "reason": ""},
        }

    monkeypatch.setattr(sql_run, "resolve_sqlserver_target", fake_resolve)
    # Records how it was asked to connect, so a test can assert on autocommit.
    def fake_connect(target, timeout_seconds, connect_timeout_seconds=0,  # noqa: ARG001
                     autocommit=False):
        conn.opened_autocommit = autocommit
        conn.opened_timeouts = (timeout_seconds, connect_timeout_seconds)
        return conn

    monkeypatch.setattr(sql_run, "connect_target", fake_connect)


def test_run_sql_returns_first_result_set_and_rolls_back(monkeypatch):
    conn = FakeConn(FakeCursor([([("Id",), ("Name",)], [[1, "a"], [2, "b"]], -1)]))
    _patch(monkeypatch, conn)

    result = sql_run.run_sql({"target": "ACME-x", "sql": "SELECT Id, Name FROM t", "database": "SALESDB"})
    assert result["ok"] is True
    assert result["columns"] == ["Id", "Name"]
    assert result["row_count"] == 2
    assert result["database"] == "SALESDB"
    assert result["committed"] is False
    assert conn.rolled_back is True and conn.committed is False and conn.closed is True


def test_run_sql_commit_true_commits_and_does_not_roll_back(monkeypatch):
    conn = FakeConn(FakeCursor([(None, [], 4)]))
    _patch(monkeypatch, conn)

    result = sql_run.run_sql({"target": "ACME-x", "sql": "UPDATE t SET a=1", "commit": True})
    assert result["affected_rows"] == 4
    assert result["committed"] is True
    assert conn.committed is True and conn.rolled_back is False


def test_run_sql_splits_go_batches(monkeypatch):
    cursor = FakeCursor([([("x",)], [[1]], -1)])
    conn = FakeConn(cursor)
    _patch(monkeypatch, conn)

    sql_run.run_sql({"target": "ACME-x", "sql": "USE SALESDB;\nGO\nSELECT 1 AS x"})
    assert cursor.executed == ["USE SALESDB;", "SELECT 1 AS x"]


def test_run_sql_wraps_driver_errors_and_still_closes(monkeypatch):
    class BoomCursor(FakeCursor):
        def execute(self, sql):
            raise RuntimeError("('ODBC SQL type -155 is not yet supported', 'HY106')")

    conn = FakeConn(BoomCursor([]))
    _patch(monkeypatch, conn)

    with pytest.raises(sql_run.SqlRunError, match="SQL failed"):
        sql_run.run_sql({"target": "ACME-x", "sql": "SELECT 1"})
    assert conn.rolled_back is True and conn.closed is True


def test_json_safe_result_makes_rows_serializable(monkeypatch):
    stamp = datetime.datetime(2026, 7, 28, 13, 39, tzinfo=datetime.timezone.utc)
    conn = FakeConn(FakeCursor([([("when",)], [[stamp]], -1)]))
    _patch(monkeypatch, conn)

    result = sql_run.run_sql({"target": "ACME-x", "sql": "SELECT SYSDATETIMEOFFSET() AS [when]"})
    assert result["rows"] == [[stamp]]  # native value for the caller
    assert json.dumps(sql_run.json_safe_result(result))  # and a serializable copy


# --------------------------------------------------------------------------- #
# The CLI facade
# --------------------------------------------------------------------------- #


def test_run_sql_cli_prints_json_result(monkeypatch, capsys):
    monkeypatch.setattr(
        sql_run,
        "run_sql",
        lambda payload: {"ok": True, "server_id": "ACME-x", "columns": ["Id"], "rows": [[1]],
                         "row_count": 1, "affected_rows": 0, "truncated": False,
                         "database": "SALESDB", "committed": False},
    )
    assert cli.main(["run-sql", '{"target": "ACME-x", "sql": "SELECT 1"}']) == 0
    payload = json.loads(capsys.readouterr().out)
    # The response envelope since 2026-08-16; the run itself is `data`, unchanged, so a caller
    # that read `rows` reads `data.rows`.
    assert payload["success"] is True and payload["operation"] == "run-sql"
    assert payload["data"]["ok"] is True and payload["data"]["rows"] == [[1]]
    assert "1 row(s)" in payload["message"]


def test_run_sql_cli_reads_request_from_file_and_stdin(monkeypatch, capsys, tmp_path):
    seen = []
    monkeypatch.setattr(sql_run, "run_sql", lambda payload: seen.append(payload) or
                        {"ok": True, "rows": [], "columns": [], "row_count": 0})
    request = tmp_path / "req.json"
    request.write_text('{"target": "ACME-x", "sql": "SELECT 1"}', encoding="utf-8")

    assert cli.main(["run-sql", f"@{request}"]) == 0
    monkeypatch.setattr("sys.stdin", type("S", (), {"read": staticmethod(lambda: '{"target": "B", "sql": "SELECT 2"}')})())
    assert cli.main(["run-sql", "-"]) == 0
    capsys.readouterr()
    # The CLI hands run_sql the already-parsed object rather than re-parsing the same text a
    # second time: since 2026-08-06 it shares `_read_json_request` with every other command,
    # so a malformed payload is reported identically here and there. run_sql takes either.
    assert seen[0]["target"] == "ACME-x"
    assert seen[1]["target"] == "B"


def test_run_sql_cli_reports_failure_as_json_and_exit_1(monkeypatch, capsys):
    def boom(payload):
        raise sql_run.SqlRunError("Unknown target: ACME-nope.")

    monkeypatch.setattr(sql_run, "run_sql", boom)
    assert cli.main(["run-sql", '{"target": "ACME-nope", "sql": "SELECT 1"}']) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is False and payload["operation"] == "run-sql"
    assert payload["error"] == "Unknown target: ACME-nope."


def test_run_sql_cli_without_arguments_shows_usage():
    assert cli.main(["run-sql"]) == 2
    assert cli.main(["run-sql", "--help"]) == 0


def test_run_sql_cli_key_flag_sets_the_secret_env(monkeypatch):
    from db_ops.lib.secret_text import SECRET_KEY_ENV_VAR

    # setenv (not delenv) so monkeypatch owns the variable and unsets it again at teardown —
    # the CLI writes it into os.environ for real, and it must not leak into other tests.
    monkeypatch.setenv(SECRET_KEY_ENV_VAR, "")
    monkeypatch.setattr(sql_run, "run_sql", lambda payload: {"ok": True, "rows": [], "columns": []})
    assert cli.main(["run-sql", '{"target": "ACME-x", "sql": "SELECT 1"}', "--key", "s3cret"]) == 0
    import os

    assert os.environ[SECRET_KEY_ENV_VAR] == "s3cret"


def test_run_sql_cli_rejects_unknown_option(capsys):
    assert cli.main(["run-sql", "{}", "--nope", "1"]) == 2
    assert "Unknown run-sql option" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Which login runs the SQL
# --------------------------------------------------------------------------- #


def _patch_inventory(monkeypatch, *, default_credential_name="dba", credentials=None):
    from db_ops.common import data_sources
    from db_ops.common import data_sources as target_resolve

    monkeypatch.setattr(
        target_resolve,
        "resolve_target_instance",
        lambda spec, data_dir=None: {"server_id": "ACME-x", "db_type": "sqlserver", "ip": "10.0.0.1",
                                     "database": "AppDb",
                                     "default_credential_name": default_credential_name},
    )
    monkeypatch.setattr(
        data_sources,
        "load_credentials",
        lambda db_type, data_dir=None: [
            {"server_id": "ACME-x", "credentials": credentials if credentials is not None else [
                {"credential_name": "dba", "username": "dba_user", "password": "p1"},
                {"credential_name": "readonly", "username": "monitor", "password": "p2"},
            ]}
        ],
    )
    monkeypatch.setattr(data_sources, "load_secret_text", lambda data_dir=None, **_k: {})
    return data_sources


def test_resolve_uses_the_instance_default_credential(monkeypatch):
    _patch_inventory(monkeypatch)
    resolved = sql_run.resolve_sqlserver_target("ACME-x")
    assert resolved["credential_name"] == "dba"
    assert resolved["username"] == "dba_user"


def test_credential_name_in_the_request_overrides_the_default(monkeypatch):
    _patch_inventory(monkeypatch)
    resolved = sql_run.resolve_sqlserver_target("ACME-x", credential_name="readonly")
    assert resolved["username"] == "monitor"
    # user_ref is the accepted alias in the JSON object.
    parsed = sql_run.SqlRunRequest.from_json({"target": "ACME-x", "sql": "SELECT 1", "user_ref": "readonly"})
    assert parsed.credential_name == "readonly"


def test_run_sql_reports_the_login_it_connected_as(monkeypatch):
    _patch_inventory(monkeypatch)
    conn = FakeConn(FakeCursor([([("Id",)], [[1]], -1)]))
    # Records how it was asked to connect, so a test can assert on autocommit.
    def fake_connect(target, timeout_seconds, connect_timeout_seconds=0,  # noqa: ARG001
                     autocommit=False):
        conn.opened_autocommit = autocommit
        conn.opened_timeouts = (timeout_seconds, connect_timeout_seconds)
        return conn

    monkeypatch.setattr(sql_run, "connect_target", fake_connect)

    result = sql_run.run_sql({"target": "ACME-x", "sql": "SELECT 1", "credential_name": "readonly"})
    assert result["credential_name"] == "readonly"
    assert result["username"] == "monitor"


def test_unknown_credential_name_lists_what_exists(monkeypatch):
    _patch_inventory(monkeypatch)
    with pytest.raises(sql_run.SqlRunError, match="Available: dba, readonly"):
        sql_run.resolve_sqlserver_target("ACME-x", credential_name="nope")


def test_instance_without_a_declared_credential_is_refused(monkeypatch):
    """No name, no run. Falling back to the first entry made file order decide which login
    a production query used; an unconfigured instance must be fixed, not guessed around."""
    _patch_inventory(monkeypatch, default_credential_name="")
    with pytest.raises(sql_run.SqlRunError, match="No credential configured for ACME-x"):
        sql_run.resolve_sqlserver_target("ACME-x")


def test_server_without_any_credential_is_a_run_error(monkeypatch):
    _patch_inventory(monkeypatch, default_credential_name="", credentials=[])
    with pytest.raises(sql_run.SqlRunError, match="No credential configured"):
        sql_run.resolve_sqlserver_target("ACME-x")


def test_missing_secret_key_is_a_run_error_not_a_traceback(monkeypatch):
    data_sources = _patch_inventory(monkeypatch)

    def no_key(data_dir=None, **_kwargs):
        raise RuntimeError("No decryption key provided. Pass --key or set DB_OPS_SECRET_KEY.")

    monkeypatch.setattr(data_sources, "load_secret_text", no_key)
    with pytest.raises(sql_run.SqlRunError, match="No decryption key provided"):
        sql_run.resolve_sqlserver_target("ACME-x")


def test_autocommit_connects_without_a_transaction_and_never_rolls_back(monkeypatch):
    """Metric SQL catches per-database errors inside a cursor. In a transaction one caught error
    dooms the batch (error 3930) and every later statement fails, so running a metric's .sql
    through run-sql reported a failure the collector itself would never have hit — which is how
    the 2008 R2 linked-server rewrite first looked broken when it was fine.

    db_ops.metrics.executor connects with autocommit=True for that reason; this is the same
    switch, so run-sql can reproduce a metric exactly.
    """
    conn = FakeConn(FakeCursor([([("x",)], [[1]], -1)]))
    _patch(monkeypatch, conn)

    result = sql_run.run_sql({"target": "ACME-x", "sql": "SELECT 1", "autocommit": True})

    assert conn.opened_autocommit is True
    # Nothing to undo: the driver committed each statement as it ran.
    assert conn.rolled_back is False
    assert result["committed"] is True


def test_without_autocommit_the_rollback_contract_is_unchanged(monkeypatch):
    conn = FakeConn(FakeCursor([([("x",)], [[1]], -1)]))
    _patch(monkeypatch, conn)

    sql_run.run_sql({"target": "ACME-x", "sql": "SELECT 1"})

    assert conn.opened_autocommit is False
    assert conn.rolled_back is True


def test_autocommit_does_not_also_issue_an_explicit_commit(monkeypatch):
    """`commit` is meaningless once the driver is in autocommit, and calling commit() on such a
    connection raises on some drivers — a request carrying both must not break."""
    conn = FakeConn(FakeCursor([(None, [], 2)]))
    _patch(monkeypatch, conn)

    result = sql_run.run_sql(
        {"target": "ACME-x", "sql": "UPDATE t SET a=1", "autocommit": True, "commit": True})

    assert conn.committed is False
    assert result["committed"] is True


# --------------------------------------------------------------------------- #
# params / prelude — values that came from a chat message must not enter the SQL text
# --------------------------------------------------------------------------- #
#
# Added 2026-08-15. `run-sql` had `commit` and `autocommit` but no way to bind a value, which is
# why two callers could not use it: `telegram`'s support commands write to production rows with
# arguments typed into Telegram, and `sql_tasks` declares typed parameters in `sql_commands.json`.
# Both were opening their own connection instead — a second implementation of driver choice, the
# ODBC->pymssql fallback and the timeouts, kept alive by a missing field.

def test_params_are_bound_and_never_reach_the_sql_text(monkeypatch):
    """The guarantee the whole feature exists for. If the value were interpolated, a chat message
    would be a statement."""
    cursor = FakeCursor([([("n",)], [[1]], -1)])
    conn = FakeConn(cursor)
    _patch(monkeypatch, conn)

    sql_run.run_sql({"target": "ACME-x", "sql": "SELECT * FROM t WHERE spid = ?",
                     "params": [505, "'; DROP TABLE t; --"]})

    assert cursor.executed == ["SELECT * FROM t WHERE spid = ?"]
    assert cursor.bound == [(505, "'; DROP TABLE t; --")]
    assert "DROP TABLE" not in cursor.executed[0]


@pytest.mark.parametrize("request_extra", [
    {},                       # the ordinary run: nothing said about parameters
    {"params": []},           # said, and empty
    {"params": None},         # said as null, which is what a JSON template with no values sends
])
def test_a_run_that_needs_no_parameters_is_unchanged(monkeypatch, request_extra):
    """The regression to fear from adding `params`: every existing caller passes none.

    A statement with no placeholders must still reach the driver as `execute(sql)` with **one**
    argument. pg8000 raises on `execute(sql, ())` and pyodbc treats an empty sequence as a
    parameter-count mismatch, so "empty list" must not become "bind nothing to nothing" — it must
    stay "do not bind".
    """
    cursor = FakeCursor([([("n",)], [[1]], -1)])
    _patch(monkeypatch, FakeConn(cursor))

    result = sql_run.run_sql({"target": "ACME-x", "sql": "SELECT 1 AS n", **request_extra})

    assert result["ok"] is True and result["rows"] == [[1]]
    assert cursor.arity == [1]               # execute(sql), not execute(sql, ())
    assert cursor.bound == [None]
    assert cursor.executed == ["SELECT 1 AS n"]


def test_an_empty_prelude_does_not_touch_the_sql_text(monkeypatch):
    """`"" + batch` is the same string, but only if nothing else is prepended — this pins that a
    caller who sends no prelude gets byte-identical SQL."""
    cursor = FakeCursor([([("n",)], [[1]], -1)])
    _patch(monkeypatch, FakeConn(cursor))

    sql_run.run_sql({"target": "ACME-x", "sql": "  SELECT 1 AS n  ", "prelude": ""})

    assert cursor.executed == ["SELECT 1 AS n"]


def test_the_prelude_goes_in_front_of_every_batch_with_the_values_bound_again(monkeypatch):
    """A T-SQL variable does not survive a GO. A prelude declared once would leave the second
    batch referring to a name that no longer exists — the failure this repeats per batch to avoid.
    """
    cursor = FakeCursor([([("n",)], [[1]], -1)])
    _patch(monkeypatch, FakeConn(cursor))

    sql_run.run_sql({
        "target": "ACME-x",
        "sql": "SELECT @spid AS n\nGO\nSELECT @spid AS n",
        "prelude": "DECLARE @spid int = ?;\n",
        "params": [505],
    })

    assert len(cursor.executed) == 2
    assert all(text.startswith("DECLARE @spid int = ?;") for text in cursor.executed)
    assert cursor.bound == [(505,), (505,)]


def test_params_as_an_object_is_refused_by_name():
    """Named binding is `:name` on Oracle, `%(name)s` on pg8000 and unsupported on pyodbc. A dict
    coerced to a list would bind by insertion order and swap silently when the JSON was reordered.
    """
    with pytest.raises(sql_run.SqlRunError, match="not an object"):
        sql_run.SqlRunRequest.from_json(
            {"target": "ACME-x", "sql": "SELECT 1", "params": {"spid": 505}})


def test_a_bare_string_is_not_a_parameter_list():
    """`"params": "505"` would otherwise bind three characters as three parameters."""
    with pytest.raises(sql_run.SqlRunError, match="got a single string"):
        sql_run.SqlRunRequest.from_json({"target": "ACME-x", "sql": "SELECT 1", "params": "505"})


def test_the_legacy_oracle_bridge_refuses_params_instead_of_dropping_them(monkeypatch):
    """The 8i tool inlines one statement and binds nothing. Running the request anyway would
    either fail on a stray `?` or — for a prelude that happens to parse — run with the values
    missing and report success."""
    monkeypatch.setattr(
        sql_run, "resolve_sqlserver_target",
        lambda spec, data_dir=None, database="", credential_name="", sql_access=None,
        profile=None, driver="", oracle_client_mode="": {
            "server_id": "LEGACYDB-8I", "db_type": "oracle", "database_name": "LTR",
            "credential_name": "c", "username": "sys", "password": "x", "ip": "10.0.0.1",
            "port": 1521, "service_name": "LEGACYDB",
            "sql_access": {"method": "subprocess"},
            "profile": TargetProfile(db_type="oracle", major_version=8),
            "tool": {"tool": "subprocess", "chosen_by": "config", "reason": "legacy bridge"},
        })

    with pytest.raises(sql_run.SqlRunError, match="binds no parameters"):
        sql_run.run_sql({"target": "LEGACYDB-8I", "sql": "SELECT 1 FROM dual", "params": [1]})


# --------------------------------------------------------------------------- #
# capture: all — every result set, as a JSON array
# --------------------------------------------------------------------------- #
#
# Added 2026-08-16 for `sql_tasks`, whose runner stores up to five result sets per task
# (`sql_runs.result_json`, and the markdown tables in its Telegram message). Until then `run-sql`
# kept only the first, which is right for an export and wrong for a report script — and was the
# last thing standing between that app and the CLI boundary.

def test_the_default_still_keeps_only_the_first_set_and_drains_the_rest(monkeypatch):
    """The behaviour every existing caller depends on. The later sets' ROWS are never fetched —
    only their rowcounts — because fetching a result set nobody asked for is the unbounded read
    `max_rows` exists to prevent."""
    cursor = FakeCursor([
        ([("a",)], [[1]], -1),
        ([("b",)], [[2], [3]], -1),
    ])
    _patch(monkeypatch, FakeConn(cursor))

    result = sql_run.run_sql({"target": "ACME-x", "sql": "SELECT 1; SELECT 2"})

    assert result["columns"] == ["a"] and result["rows"] == [[1]]
    assert len(result["result_sets"]) == 1          # always present, holding the one set
    assert result["result_sets_truncated"] is False  # not asked for them, so nothing was lost


def test_capture_all_returns_every_set_as_an_array(monkeypatch):
    cursor = FakeCursor([
        ([("a",)], [[1]], -1),
        ([("b",), ("c",)], [[2, "x"], [3, "y"]], -1),
    ])
    _patch(monkeypatch, FakeConn(cursor))

    result = sql_run.run_sql({"target": "ACME-x", "sql": "SELECT 1; SELECT 2", "capture": "all"})

    assert [item["columns"] for item in result["result_sets"]] == [["a"], ["b", "c"]]
    assert result["result_sets"][1]["rows"] == [[2, "x"], [3, "y"]]
    assert result["result_sets"][1]["row_count"] == 2
    # The top-level four keep meaning the first set — that is the compatibility promise.
    assert result["columns"] == ["a"] and result["rows"] == [[1]] and result["row_count"] == 1


def test_each_set_is_capped_and_says_so_on_its_own(monkeypatch):
    """Truncation is per set. A caller reading set 2 must not have to infer it from a flag about
    set 1."""
    cursor = FakeCursor([
        ([("a",)], [[1]], -1),
        ([("b",)], [[n] for n in range(10)], -1),
    ])
    _patch(monkeypatch, FakeConn(cursor))

    result = sql_run.run_sql(
        {"target": "ACME-x", "sql": "SELECT 1; SELECT 2", "capture": "all", "max_rows": 3})

    assert result["result_sets"][0]["truncated"] is False
    assert result["result_sets"][1]["truncated"] is True
    assert result["result_sets"][1]["row_count"] == 3


def test_more_sets_than_the_cap_is_reported_not_swallowed(monkeypatch):
    """A script that loops can produce hundreds. The reader should learn that from a flag, not
    from the worker's memory."""
    cursor = FakeCursor([([("a",)], [[n]], -1) for n in range(5)])
    _patch(monkeypatch, FakeConn(cursor))

    result = sql_run.run_sql({"target": "ACME-x", "sql": "SELECT 1", "capture": "all",
                              "max_result_sets": 2})

    assert len(result["result_sets"]) == 2
    assert result["result_sets_truncated"] is True


def test_max_result_sets_zero_means_no_cap(monkeypatch):
    cursor = FakeCursor([([("a",)], [[n]], -1) for n in range(5)])
    _patch(monkeypatch, FakeConn(cursor))

    result = sql_run.run_sql({"target": "ACME-x", "sql": "SELECT 1", "capture": "all",
                              "max_result_sets": 0})

    assert len(result["result_sets"]) == 5 and result["result_sets_truncated"] is False


def test_a_misspelled_capture_mode_is_refused_rather_than_defaulted():
    """Defaulting would be invisible: the caller asked for every set, got one, and the only
    symptom is a report that looks a little short."""
    with pytest.raises(sql_run.SqlRunError, match="capture must be one of"):
        sql_run.SqlRunRequest.from_json({"target": "ACME-x", "sql": "SELECT 1", "capture": "ALL "})
        sql_run.SqlRunRequest.from_json({"target": "ACME-x", "sql": "SELECT 1", "capture": "every"})


def test_json_safe_result_converts_the_rows_inside_every_set():
    """Missing these would leave a datetime in the object the CLI is about to json.dumps — a
    failure at the very end of a run that already did all its work."""
    import datetime

    safe = sql_run.json_safe_result({
        "rows": [[datetime.date(2026, 8, 16)]],
        "result_sets": [
            {"columns": ["d"], "rows": [[datetime.date(2026, 8, 16)]], "truncated": False},
            {"columns": ["t"], "rows": [[datetime.datetime(2026, 8, 16, 1, 2, 3)]],
             "truncated": False},
        ],
    })

    assert safe["result_sets"][0]["rows"] == [["2026-08-16"]]
    assert "2026-08-16" in str(safe["result_sets"][1]["rows"][0][0])
    json.dumps(safe)  # the thing that used to fail


# --------------------------------------------------------------------------- #
# execute_cursor_batches — the multi-result-set reader `metrics` runs on
# --------------------------------------------------------------------------- #
#
# Moved here from `tests/test_sql_tasks_runner.py` on 2026-08-16, when `sql_tasks` stopped
# calling this function and started calling `common.cli run-sql`. The function is not dead —
# `metrics/executor.py` is its caller now, and metrics is the one app exempt from the CLI rule
# because it runs ~388 SQL executions per collect pass. Coverage of a `common` function had no
# business hanging off an app's test file, where it would have been deleted along with it.


class BatchConn:
    def __init__(self):
        self.committed = False

    def commit(self):
        self.committed = True


class BatchCursor:
    def __init__(self):
        self._sets = []
        self._index = 0
        self.description = None
        self.rowcount = -1

    def execute(self, _sql_text):
        self._sets = [
            {"columns": [], "rows": [], "rowcount": 0},
            {"columns": ["metric_item", "metric_value"], "rows": [("db:file", "99.9")], "rowcount": -1},
        ]
        self._index = 0
        self._apply_current_set()

    def fetchmany(self, _size):
        return self._sets[self._index]["rows"]

    def nextset(self):
        if self._index + 1 >= len(self._sets):
            return False
        self._index += 1
        self._apply_current_set()
        return True

    def _apply_current_set(self):
        current = self._sets[self._index]
        columns = current["columns"]
        self.description = [(column,) for column in columns] if columns else None
        self.rowcount = current["rowcount"]



def test_execute_cursor_batches_reads_later_result_sets():
    conn = BatchConn()
    cursor = BatchCursor()

    result = sql_execution.execute_cursor_batches(conn, cursor, ["batch"], commit=False)

    assert result["row_count"] == 1
    assert result["result_sets"] == [
        {
            "columns": ["metric_item", "metric_value"],
            "rows": [["db:file", "99.9"]],
            # A short result set was not cut by the cap, and now says so: a truncated set used to
            # be indistinguishable from a complete one.
            "truncated": False,
        }
    ]
    assert result["truncated"] is False
    assert conn.committed is False


def test_a_result_set_cut_by_the_cap_says_so():
    """Silent truncation is the defect, not the cap. STORAGE_FILE_PLACEMENT reporting exactly 100
    rows meant "the first 100 of an unknown number", and nothing in the result distinguished that
    from an instance that genuinely has 100 mis-placed files."""

    class _Cursor:
        description = [("metric_item",), ("metric_value",)]
        rowcount = 0

        def __init__(self):
            self.remaining = [[f"row{i}", "1"] for i in range(150)]

        def execute(self, _sql):
            return None

        def fetchmany(self, n):
            taken, self.remaining = self.remaining[:n], self.remaining[n:]
            return taken

        def nextset(self):
            return False

    result = sql_execution.execute_cursor_batches(
        BatchConn(), _Cursor(), ["batch"], commit=False, max_rows=100)

    assert len(result["result_sets"][0]["rows"]) == 100
    assert result["result_sets"][0]["truncated"] is True
    assert result["truncated"] is True




# --------------------------------------------------------------------------- #
# Which tool the resolved target implies (2026-08-19)
# --------------------------------------------------------------------------- #
def _patch_oracle_inventory(monkeypatch, **instance_extra):
    """An 8i instance shaped like `ACME-192-0-2-136`: legacy, and with no bridge configured."""
    from db_ops.common import data_sources
    from db_ops.common import data_sources as target_resolve

    monkeypatch.setattr(
        target_resolve, "resolve_target_instance",
        lambda spec, data_dir=None: {"server_id": "ACME-192-0-2-136", "db_type": "oracle",
                                     "ip": "192.0.2.136", "port": 1521, "service_name": "LEGACYDB",
                                     "default_credential_name": "sys_cred", **instance_extra},
    )
    monkeypatch.setattr(
        data_sources, "load_credentials",
        lambda db_type, data_dir=None: [
            {"server_id": "ACME-192-0-2-136",
             "credentials": [{"credential_name": "sys_cred", "username": "sys", "password": "p"}]}
        ],
    )
    monkeypatch.setattr(data_sources, "load_secret_text", lambda data_dir=None, **_k: {})


def test_an_8i_target_with_no_bridge_is_refused_by_name_instead_of_dying_as_dpy_3010(monkeypatch):
    """`ACME-192-0-2-136` is the real one: Oracle 8.1.7, `major_version` unset in config and no
    `sql_access` block, so run-sql handed it to python-oracledb — which speaks 12.1 and newer — and
    failed with a driver code naming neither the cause nor the fix. Stating the version in the
    request is now enough to get the sentence instead."""
    _patch_oracle_inventory(monkeypatch)

    with pytest.raises(sql_run.SqlRunError) as raised:
        sql_run.resolve_sqlserver_target("ACME-192-0-2-136", profile=TargetProfile(major_version=8))

    message = str(raised.value)
    assert "ACME-192-0-2-136" in message      # which target, not just "a connect failed"
    assert '"method": "api"' in message and "thick" in message   # both ways out


def test_the_same_8i_target_is_not_refused_when_it_is_configured_for_the_bridge(monkeypatch):
    """A legacy target opens no driver at all, so asking the driver rule about it would refuse an
    instance for being 8i — which is exactly why it is routed around the driver."""
    _patch_oracle_inventory(
        monkeypatch,
        major_version=8,
        sql_access={"method": "api", "bridge_url": "http://192.0.2.93:8765/query"},
    )

    resolved = sql_run.resolve_sqlserver_target("ACME-192-0-2-136")

    assert resolved["tool"]["tool"] == "api"
    assert resolved["tool"]["chosen_by"] == "config"
    assert resolved["profile"].major_version == 8


def test_the_inventorys_version_is_read_when_the_request_states_none(monkeypatch):
    """The inventory is still the source of record; the request only overrides it. Without this,
    filling `major_version` in `db_instances.json` would buy nothing."""
    _patch_oracle_inventory(monkeypatch, major_version=8)

    with pytest.raises(sql_run.SqlRunError, match="thin mode"):
        sql_run.resolve_sqlserver_target("ACME-192-0-2-136")


def test_a_modern_target_reports_its_driver_and_who_chose_it(monkeypatch):
    """The answer says what ran the SQL. Before this, pyodbc and pymssql were indistinguishable
    from the outside — and they bind parameters differently, which is a difference callers feel."""
    _patch_inventory(monkeypatch)

    resolved = sql_run.resolve_sqlserver_target("ACME-x", driver="pymssql")

    assert resolved["tool"] == {"tool": "pymssql", "chosen_by": "request",
                                "reason": "explicitly requested"}
    assert resolved["sqlserver_driver"] == "pymssql"
