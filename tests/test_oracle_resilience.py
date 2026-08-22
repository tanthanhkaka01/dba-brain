import sys
import types

import pytest

from db_ops.metrics import executor
from db_ops.metrics.models import MetricTarget


class FakeOracleConnection:
    """Oracle used to be opened with `with oracledb.connect(...)`; the shared connect returns a
    connection the caller closes, the same as every other engine, so `close()` is what the
    "was it released?" assertions look at now."""

    def __init__(self):
        self.call_timeout = None
        self.closed = False

    def close(self):
        self.closed = True

    def cursor(self):
        return object()


def oracle_target():
    return MetricTarget(
        target_id="oracle:server",
        server_id="server",
        ip="192.0.2.20",
        db_type="oracle",
        db_name="ORCL",
        credential_name="cred",
        port=1521,
        service_name="ORCL",
        connection_info={"service_name": "ORCL"},
        credential={"username": "monitor", "password_ref": "ORACLE_PASSWORD"},
    )


def test_oracle_timeout_sets_call_timeout_and_closes_connection(monkeypatch):
    connection = FakeOracleConnection()
    oracle_module = types.SimpleNamespace(
        makedsn=lambda host, port, service_name: f"{host}:{port}/{service_name}",
        connect=lambda **_kwargs: connection,
    )
    monkeypatch.setitem(sys.modules, "oracledb", oracle_module)

    def timeout_execute(*_args, **_kwargs):
        raise TimeoutError("Oracle query timed out")

    monkeypatch.setattr(executor, "execute_cursor_batches", timeout_execute)

    # Every engine now reports a post-connect failure the same way. The driver's own message
    # has to survive the wrapping, or an operator loses the only clue about what timed out.
    with pytest.raises(RuntimeError, match="Oracle query timed out"):
        executor.execute_metric_sql(
            target=oracle_target(),
            sql_text="SELECT 1 FROM dual",
            secrets={"ORACLE_PASSWORD": "secret"},
            sql_timeout_seconds=3,
        )

    assert connection.call_timeout == 3000
    assert connection.closed is True, "the connection must be released even when the query fails"


def test_oracle_success_uses_service_name_and_returns_first_result_set(monkeypatch):
    connection = FakeOracleConnection()
    connect_calls = []

    def fake_connect(**kwargs):
        connect_calls.append(kwargs)
        return connection

    oracle_module = types.SimpleNamespace(
        makedsn=lambda host, port, service_name: f"{host}:{port}/{service_name}",
        connect=fake_connect,
    )
    monkeypatch.setitem(sys.modules, "oracledb", oracle_module)
    monkeypatch.setattr(
        executor,
        "execute_cursor_batches",
        lambda *_args, **_kwargs: {"result_sets": [{"columns": ["metric_item", "metric_value"], "rows": [["connected", "1"]]}]},
    )

    rows = executor.execute_metric_sql(
        target=oracle_target(),
        sql_text="SELECT 1 FROM dual",
        secrets={"ORACLE_PASSWORD": "secret"},
        sql_timeout_seconds=4,
    )

    assert rows == [{"metric_item": "connected", "metric_value": "1"}]
    assert connect_calls[0]["user"] == "monitor"
    assert connect_calls[0]["password"] == "secret"
    assert connect_calls[0]["dsn"] == "192.0.2.20:1521/ORCL"
    assert connection.call_timeout == 4000
    assert connection.closed is True
