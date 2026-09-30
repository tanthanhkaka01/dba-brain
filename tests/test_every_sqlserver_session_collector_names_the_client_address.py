"""A SQL Server collector that can see a session's connection names the address it came from.

``host=`` is whatever the client says it is - an application can send any name or none - and
``client_ip=`` (``sys.dm_exec_connections.client_net_address``) is where SQL Server actually saw the
connection come from. 0.25.0 added it to every collector that joins the view, and left the SQL Server
2008 R2 variants as they were. After the upgrade on 2026-09-30, 144 of the 214 per-session rows of
``LOCK_SLEEPING_OPEN_TRANSACTION`` named no address - every one of them from the two 2008 R2
servers (0.26.0 §1.71). The view exists there too.

The rule, for both folders: a collector that joins ``sys.dm_exec_connections`` in live SQL says
``client_ip=``. A join that is only in a commented-out draft does not count.
"""

from __future__ import annotations

from pathlib import Path

import pytest

COLLECTORS = Path(__file__).resolve().parents[1] / "db_ops" / "metrics" / "collectors" / "sqlserver"


def _live_sql(path: Path) -> str:
    return "\n".join(line.split("--", 1)[0] for line in path.read_text(encoding="utf-8").splitlines())


def _joining_collectors() -> list[Path]:
    return sorted(p for p in COLLECTORS.rglob("*.sql") if "dm_exec_connections" in _live_sql(p))


def test_the_2008r2_sleeping_transaction_collector_is_one_of_them():
    assert COLLECTORS / "legacy_2008r2" / "024_sqlserver_sleeping_open_transaction.sql" in _joining_collectors()


@pytest.mark.parametrize("path", _joining_collectors(), ids=lambda p: str(p.relative_to(COLLECTORS)))
def test_a_collector_that_joins_the_connection_names_the_client_address(path):
    assert "client_ip=" in _live_sql(path)
