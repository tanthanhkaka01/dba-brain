import sys
import types

import pytest

from db_ops.common import sql_execution
from db_ops.metrics import executor
from db_ops.metrics.models import MetricTarget


def metric_target(*, connection_info=None):
    return MetricTarget(
        target_id="sqlserver:server",
        server_id="server",
        ip="192.0.2.10",
        db_type="sqlserver",
        db_name="master",
        credential_name="cred",
        port=1433,
        service_name="svc",
        instance_name="inst",
        connection_info=connection_info or {},
        credential={"username": "monitor", "password_ref": "SQL_PASSWORD"},
    )


class FakePymssqlCursor:
    """Enough of a pymssql cursor for one result set."""

    def __init__(self, rows):
        self.description = [("metric_item",), ("metric_value",)]
        self.rowcount = len(rows)
        self._rows = list(rows)

    def execute(self, sql_text):  # noqa: ARG002 - the fake ignores the SQL text.
        return None

    def fetchmany(self, size):
        chunk = self._rows[:size]
        self._rows = self._rows[size:]
        return chunk

    def nextset(self):
        return None


class FakePymssqlConn:
    def __init__(self, rows, calls):
        self._rows = rows
        self.calls = calls

    def cursor(self):
        return FakePymssqlCursor(self._rows)

    def close(self):
        self.calls.append("closed")


def _fake_pymssql_module(rows, calls):
    def connect(**kwargs):
        calls.append(kwargs)
        return FakePymssqlConn(rows, calls)

    return types.SimpleNamespace(connect=connect)


def test_pyodbc_failure_falls_back_to_pymssql_and_preserves_timeout(monkeypatch):
    """The fallback lives in common.sql_execution — metrics and the shared sql_run engine
    reach SQL Server through the same function, so this rule has exactly one implementation."""
    pyodbc = types.SimpleNamespace(drivers=lambda: ["ODBC Driver 18 for SQL Server"])
    monkeypatch.setitem(sys.modules, "pyodbc", pyodbc)
    calls = []
    monkeypatch.setitem(sys.modules, "pymssql", _fake_pymssql_module([["connected", "1"]], calls))

    def fail_odbc(*_args, **_kwargs):
        raise RuntimeError("ODBC TLS handshake failed")

    monkeypatch.setattr(sql_execution, "open_sqlserver_odbc", fail_odbc)

    rows = executor.execute_metric_sql(
        target=metric_target(),
        sql_text="SELECT 1;",
        secrets={"SQL_PASSWORD": "secret"},
        sql_timeout_seconds=7,
    )

    assert rows == [{"metric_item": "connected", "metric_value": "1"}]
    connect_kwargs = calls[0]
    assert connect_kwargs["server"] == "192.0.2.10"
    assert connect_kwargs["password"] == "secret"
    assert connect_kwargs["database"] == "master"
    # Opening the session and running the SQL are different bounds, and both are preserved.
    assert connect_kwargs["login_timeout"] == 5
    assert connect_kwargs["timeout"] == 7
    assert connect_kwargs["autocommit"] is True
    assert "closed" in calls


def test_configured_pymssql_driver_skips_pyodbc_connect(monkeypatch):
    pyodbc = types.SimpleNamespace(drivers=lambda: ["ODBC Driver 18 for SQL Server"])
    monkeypatch.setitem(sys.modules, "pyodbc", pyodbc)
    calls = []
    monkeypatch.setitem(sys.modules, "pymssql", _fake_pymssql_module([["driver", "pymssql"]], calls))

    def fail_if_odbc_called(*_args, **_kwargs):
        raise AssertionError("ODBC should not be used when sqlserver_driver=pymssql")

    monkeypatch.setattr(sql_execution, "open_sqlserver_odbc", fail_if_odbc_called)

    rows = executor.execute_metric_sql(
        target=metric_target(connection_info={"sqlserver_driver": "pymssql"}),
        sql_text="SELECT 1;",
        secrets={"SQL_PASSWORD": "secret"},
        sql_timeout_seconds=9,
    )

    assert rows == [{"metric_item": "driver", "metric_value": "pymssql"}]
    assert calls[0]["login_timeout"] == 5
    assert calls[0]["timeout"] == 9


def test_pinned_odbc_driver_never_falls_back_to_pymssql(monkeypatch):
    """A driver pinned in config is a decision, not a preference: if it fails, that is the
    error. Only an unconfigured target may try pymssql."""
    pyodbc = types.SimpleNamespace(drivers=lambda: ["ODBC Driver 17 for SQL Server"])
    monkeypatch.setitem(sys.modules, "pyodbc", pyodbc)
    monkeypatch.setitem(
        sys.modules, "pymssql",
        types.SimpleNamespace(connect=lambda **_kw: pytest.fail("pymssql must not be tried")),
    )
    monkeypatch.setattr(
        sql_execution, "open_sqlserver_odbc",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("ODBC login failed")),
    )

    with pytest.raises(RuntimeError, match="ODBC login failed"):
        executor.execute_metric_sql(
            target=metric_target(connection_info={"sqlserver_driver": "ODBC Driver 17 for SQL Server"}),
            sql_text="SELECT 1;",
            secrets={"SQL_PASSWORD": "secret"},
            sql_timeout_seconds=5,
        )


def test_driver17_never_uses_optional_encrypt_value():
    """ODBC Driver 17 rejects Encrypt=optional (it accepts only yes/no). Driver 18 may
    use 'optional'. Regression guard for the prod incident where Driver 17 hosts failed
    with 'Invalid value specified for connection string attribute Encrypt'."""
    from db_ops.common.sql_execution import sqlserver_driver_candidates

    drivers = ["ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server"]
    pyodbc = types.SimpleNamespace(drivers=lambda: drivers)

    # Auto-detect path.
    auto = sqlserver_driver_candidates(pyodbc)
    for driver, mode in auto:
        if driver == "ODBC Driver 17 for SQL Server":
            assert mode in {"no", "yes"}, f"Driver 17 got invalid Encrypt={mode!r}"

    # Explicitly-configured Driver 17 path.
    forced17 = sqlserver_driver_candidates(pyodbc, preferred_driver="ODBC Driver 17 for SQL Server")
    assert forced17 == [("ODBC Driver 17 for SQL Server", "no")]

    # Driver 18 keeps the encryption-preferring order.
    forced18 = sqlserver_driver_candidates(pyodbc, preferred_driver="ODBC Driver 18 for SQL Server")
    assert forced18[0] == ("ODBC Driver 18 for SQL Server", "optional")


def test_the_candidate_walk_records_what_it_tried_and_what_answered():
    """Until 2026-08-19 this walk reported itself as the single word "odbc". Which of five drivers
    answered, at which Encrypt setting, and whether it fell back at all were invisible — and that
    is exactly the evidence needed to judge the *order*. The first thing the report was used for
    was rejecting a version-based re-ordering that would have downgraded four production
    connections to plaintext."""
    tried = []

    def connect(conn_str, timeout):  # noqa: ARG001 - the fake ignores the timeout.
        tried.append(conn_str)
        if "Driver 18" in conn_str:
            raise RuntimeError("SSL Provider: certificate chain was issued by an untrusted party")
        return types.SimpleNamespace()

    pyodbc = types.SimpleNamespace(
        drivers=lambda: ["ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server"],
        connect=connect,
    )

    opened = sql_execution.open_sqlserver_odbc(
        pyodbc, server="h,1433", database="master", username="u", password="p", timeout=5
    )

    assert opened.driver == "ODBC Driver 17 for SQL Server"
    assert opened.encryption == "no"
    # Every attempt, in order, with the losers' errors kept: two Driver 18 encryption modes first.
    assert [(a.driver.split()[2], a.encryption, a.ok) for a in opened.attempts] == [
        ("18", "optional", False), ("18", "no", False), ("17", "no", True),
    ]
    assert "certificate chain" in opened.attempts[0].error


def test_a_non_tls_error_stops_the_walk_instead_of_trying_every_driver():
    """A wrong password is not a reason to ask four more drivers the same question — and doing so
    is how a login gets locked out. Only a TLS/cert failure means "try the next candidate"."""
    def connect(conn_str, timeout):  # noqa: ARG001
        raise RuntimeError("Login failed for user 'monitor'")

    pyodbc = types.SimpleNamespace(
        drivers=lambda: ["ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server"],
        connect=connect,
    )

    with pytest.raises(RuntimeError, match="Login failed"):
        sql_execution.open_sqlserver_odbc(
            pyodbc, server="h,1433", database="master", username="u", password="p", timeout=5
        )


# --------------------------------------------------------------------------- #
# The connect error has to name the cause, because the driver's text does not
# --------------------------------------------------------------------------- #
REFUSED_PROTOCOL = (
    "('08001', '[08001] [Microsoft][ODBC Driver 18 for SQL Server]SSL Provider: "
    "[error:0A000102:SSL routines::unsupported protocol] (-1) (SQLDriverConnect)')"
)
REJECTED_CONFIG = (
    "('08001', '[08001] [Microsoft][ODBC Driver 18 for SQL Server]SSL Provider: "
    "[error:0A000180:SSL routines::bad value:cmd=MinProtocol, value=TLSv1.0]"
    "[error:0A0001A3:SSL routines::error in system default config] (-1) (SQLDriverConnect)')"
)


def test_an_old_instance_refused_by_tls_policy_says_whose_policy_it_is():
    """On 2026-09-03 five SQL Server instances that had answered every check for months went
    CRITICAL together, carrying only the driver's own text. Nothing in it says the cause is the
    OpenSSL policy of the machine running the toolkit - so it reads like the network, the
    credential or the driver, and none of those is it."""
    hint = sql_execution.sqlserver_tls_policy_hint(REFUSED_PROTOCOL)

    assert "TLS policy of the machine running the toolkit" in hint
    assert "MinProtocol = TLSv1" in hint
    assert "pip install inherits the host's setting" in hint


def test_a_malformed_tls_stanza_is_told_apart_from_an_old_instance():
    """The trap that follows the fix: `TLSv1.0` is not a token OpenSSL accepts. 3.0 tolerated it,
    3.5 refuses it, and a config error breaks *every* connection rather than only the old ones -
    so the advice for the previous case would send the reader in the wrong direction."""
    hint = sql_execution.sqlserver_tls_policy_hint(REJECTED_CONFIG)

    assert "every connection fails" in hint
    assert "'TLSv1'" in hint and "TLSv1.0" in hint
    assert "TLS policy of the machine" not in hint, "this is a typo, not a policy decision"


def test_an_ordinary_failure_gets_no_tls_lecture():
    """A hint that appears on unrelated errors is noise, and noise is how a good hint stops being
    read at all."""
    assert sql_execution.sqlserver_tls_policy_hint("Login failed for user 'sa'.") == ""
    assert sql_execution.sqlserver_tls_policy_hint("timeout expired") == ""


def test_the_hint_reaches_the_error_the_operator_actually_sees():
    """The hint is only worth writing if it travels with the exception the report prints."""
    def connect(conn_str, timeout):  # noqa: ARG001 - every candidate refuses the same way
        raise RuntimeError(REFUSED_PROTOCOL)

    pyodbc = types.SimpleNamespace(
        drivers=lambda: ["ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server"],
        connect=connect,
    )

    with pytest.raises(RuntimeError) as raised:
        sql_execution.open_sqlserver_odbc(
            pyodbc, server="h,1433", database="master", username="u", password="p", timeout=5
        )

    message = str(raised.value)
    assert "connect failed after driver fallback" in message, "the attempts are still reported"
    assert "MinProtocol = TLSv1" in message, "and now the cause is named alongside them"

