"""An instance that asks for a verified TLS connection gets one or an error, and the store's sslmode is
honoured.

No hop in the tool authenticated the machine it sent credentials to (review 0.25.0, B5.5). The
defaults stay - a lab estate connects exactly as before - and two switches now exist:

* **`sqlserver_tls_verify: true` on an instance** (`db_instances.json`): `Encrypt=yes;
  TrustServerCertificate=no`, only the ODBC drivers that verify, and neither fallback - to plaintext
  ODBC, or to pymssql, which checks nothing. It reaches every metric and every `run-sql`.
* **`sslmode` of the PostgreSQL store** (`data/store_config.json`) was read and never passed to
  pg8000, so every store connection was plaintext whatever it said. `require`, `verify-ca` and
  `verify-full` now mean what libpq means; `prefer`, the default, still connects without TLS.
"""

from __future__ import annotations

import ssl
import types

import pytest

from db_ops.common import db_connect, metric_batch, sql_execution, sql_run
from db_ops.db import postgres_store
from db_ops.lib.connection_spec import ConnectionSpec
from db_ops.lib.data_sources import request_fill

DRIVERS = ["ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server"]


def _pyodbc(fail_with: str = ""):
    tried: list[str] = []

    def connect(conn_str, timeout):  # noqa: ARG001
        tried.append(conn_str)
        if fail_with:
            raise RuntimeError(fail_with)
        return types.SimpleNamespace()

    return types.SimpleNamespace(drivers=lambda: DRIVERS, connect=connect), tried


def test_a_verified_connection_asks_for_the_certificate_to_be_checked():
    pyodbc, tried = _pyodbc()

    opened = sql_execution.open_sqlserver_odbc(pyodbc, server="h,1433", database="master",
                                               username="u", password="p", timeout=5, tls_verify=True)

    assert opened.encryption == "yes"
    assert "Encrypt=yes;TrustServerCertificate=no;" in tried[0]


def test_a_certificate_failure_is_the_answer_not_a_step_down_to_plaintext():
    pyodbc, tried = _pyodbc("SSL Provider: certificate chain was issued by an untrusted party")

    with pytest.raises(RuntimeError, match="certificate chain"):
        sql_execution.open_sqlserver_odbc(pyodbc, server="h,1433", database="master",
                                          username="u", password="p", timeout=5, tls_verify=True)

    assert tried and all("Encrypt=yes;TrustServerCertificate=no;" in conn for conn in tried)


def test_the_default_walk_is_unchanged():
    pyodbc, tried = _pyodbc()

    sql_execution.open_sqlserver_odbc(pyodbc, server="h,1433", database="master",
                                      username="u", password="p", timeout=5)

    assert "Encrypt=optional;TrustServerCertificate=yes;" in tried[0]


def test_pymssql_is_refused_when_verification_is_asked_for():
    with pytest.raises(RuntimeError, match="cannot verify"):
        sql_execution.connect_sqlserver_with_fallback(host="h", username="u", password="p",
                                                      driver="pymssql", tls_verify=True)


def test_the_switch_travels_from_the_instance_to_the_connect(monkeypatch):
    seen: list[bool] = []
    monkeypatch.setattr(db_connect, "connect_engine",
                        lambda **kw: seen.append(kw["sqlserver_tls_verify"]) or object())
    instance = {"db_type": "sqlserver", "ip": "192.0.2.30", "port": 1433,
                "sqlserver_tls_verify": True}
    block = request_fill.connection_from(instance, {"username": "u"}, "p", server_id="lab-01")
    spec = ConnectionSpec.from_json(block)

    sql_run.connect_target(spec.to_resolved(password="p"))
    metric_batch._connect({**block, "sqlserver_tls_verify": spec.sqlserver_tls_verify}, "master",
                          timeout=5)

    assert block["sqlserver_tls_verify"] is True and seen == [True, True]


@pytest.mark.parametrize("mode, encrypted, verifies, checks_name", [
    ("prefer", False, False, False), ("disable", False, False, False),
    ("require", True, False, False), ("verify-ca", True, True, False), ("verify-full", True, True, True)])
def test_the_store_s_sslmode_means_what_libpq_means(mode, encrypted, verifies, checks_name):
    context = postgres_store.ssl_context_for(mode)

    assert (context is not None) == encrypted
    if context is not None:
        assert (context.verify_mode == ssl.CERT_REQUIRED) == verifies
        assert context.check_hostname == checks_name


def test_an_unknown_sslmode_is_refused():
    with pytest.raises(postgres_store.PostgresStoreError, match="sslmode"):
        postgres_store.ssl_context_for("verify_full")


def test_the_switch_reaches_a_metric_s_request(tmp_path, monkeypatch):
    from db_ops.metrics.executor import connection_block
    from db_ops.metrics.targets import load_metric_targets

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "db_instances.json").write_text(
        '{"db_instances": [{"site": "LAB", "ip": "192.0.2.30", "db_type": "sqlserver", '
        '"service_name": "LAB01", "enabled": true, "sqlserver_tls_verify": true}]}', encoding="utf-8")
    monkeypatch.setattr("db_ops.metrics.targets.load_database_inventory", lambda *_a, **_k: [])
    monkeypatch.setattr("db_ops.metrics.targets.load_credentials_file", lambda *_a, **_k: [])

    [target] = load_metric_targets(data_dir=data_dir)

    assert connection_block(target, {})["sqlserver_tls_verify"] is True
