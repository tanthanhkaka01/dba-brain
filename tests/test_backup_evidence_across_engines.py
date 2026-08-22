"""Backup evidence must read the same on every engine, not just on SQL Server.

The fleet page's Backup column is built from one metric contract: a row per ``(database, type)``
whose message carries ``database=``, ``recovery_model=``, ``backup_type=`` and
``backup_finish_date=``. For a long time only ``BACKUP_LAST_RESULT`` produced it, and that metric
had a SQL Server variant and nothing else — so every Oracle and PostgreSQL server in the estate
printed "No metrics" in the Backup column while pg_basebackup and RMAN ran successfully every
night. An operator reading that page would conclude those databases were unprotected.

PostgreSQL needs its own metric code (``collector_type`` is per metric, and a base backup is a
directory on disk rather than a catalog row), so the fix is a *set* of codes that all mean
"backup evidence" — :data:`db_ops.lib.backup_policy.BACKUP_LAST_RESULT_CODES`. These tests pin
the property that matters: which code a row arrived under must not change the verdict.
"""

from __future__ import annotations

import datetime

from db_ops.lib import backup_policy
from db_ops.reports import inventory_health

from db_ops.common import data_sources

# `evaluate_backup_policy` no longer reads data/backup_policy.json itself (2026-08-15):
# reading is data_sources' job, judging is lib's. These tests exercised the shipped policy
# through that default, so they pass the same document in explicitly.
_SHIPPED_POLICY = data_sources.load_backup_policy()


NOW = datetime.datetime(2026, 8, 4, 12, 0, 0)


def _row(code, item, message, *, value="1", collected="2026-08-04T07:00:00Z"):
    return {
        "metric_code": code,
        "metric_item": item,
        "metric_value": value,
        "status": "OK",
        "message": message,
        "collected_at": collected,
    }


def _message(database, backup_type, finish, recovery_model="FULL"):
    return (
        f"database={database}, recovery_model={recovery_model}, "
        f"backup_type={backup_type}, backup_finish_date={finish}"
    )


def test_a_postgresql_backup_row_is_read_as_backup_evidence_like_any_other():
    rows = [
        _row("POSTGRES_BACKUP_LAST_RESULT", "pg_ha-primary / FULL",
             _message("pg_ha-primary", "FULL", "2026-08-04 06:00:00")),
    ]
    evidence = backup_policy.collect_evidence(rows, now=NOW)
    assert "pg_ha-primary" in evidence
    assert evidence["pg_ha-primary"]["types"]["FULL"]["latest_finish"] == "2026-08-04 06:00:00"


def test_the_same_backup_reported_under_either_code_gets_the_same_verdict():
    """A PostgreSQL server must not be judged differently from a SQL Server one for the sole
    reason that its evidence arrived through a docker collector."""
    finish = "2026-08-04 06:00:00"
    verdicts = []
    for code in ("BACKUP_LAST_RESULT", "POSTGRES_BACKUP_LAST_RESULT"):
        rows = [
            _row(code, "sales / FULL", _message("sales", "FULL", finish)),
            _row(code, "sales / LOG", _message("sales", "LOG", finish)),
        ]
        verdicts.append(backup_policy.evaluate_backup_policy(rows, server_id="X", now=NOW, policy=_SHIPPED_POLICY))
    assert verdicts[0] == verdicts[1]


def test_a_postgresql_only_server_reports_coverage_instead_of_no_metrics():
    """The regression that made this whole change necessary: evidence present, column empty."""
    code_map = {
        ("POSTGRES_BACKUP_LAST_RESULT", "pg_ha-primary / FULL"):
            _row("POSTGRES_BACKUP_LAST_RESULT", "pg_ha-primary / FULL",
                 _message("pg_ha-primary", "FULL", "2026-08-04 06:00:00")),
        ("POSTGRES_BACKUP_LAST_RESULT", "pg_ha-primary / LOG"):
            _row("POSTGRES_BACKUP_LAST_RESULT", "pg_ha-primary / LOG",
                 _message("pg_ha-primary", "LOG", "2026-08-04 11:30:00")),
    }
    evidence = inventory_health.build_backup_evidence(code_map)
    assert set(evidence) == {"FULL", "LOG"}
    assert evidence["FULL"]["latest_finish"] == "2026-08-04 06:00:00"


def test_the_postgresql_backup_metric_is_loaded_by_the_inventory_overlay():
    """Being readable is not enough — the overlay must actually fetch the code. It did not, and
    that alone was enough to keep the Backup column empty."""
    assert "POSTGRES_BACKUP_LAST_RESULT" in inventory_health.HEALTH_CODES
    for code in backup_policy.BACKUP_LAST_RESULT_CODES:
        assert code in inventory_health.HEALTH_CODES
