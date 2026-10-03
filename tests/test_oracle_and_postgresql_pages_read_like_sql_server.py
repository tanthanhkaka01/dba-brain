"""An Oracle or PostgreSQL server's page reads as completely as a SQL Server one.

The operator asked (2026-10-02) whether the inventory, server-metrics and index pages show Oracle
and PostgreSQL "fully, like SQL Server". Reading the node's pages found two places where they did
not, both in what the server page *expects* rather than in what was collected:

* the coverage line counted every metric with a variant for the engine - including the variants the
  catalog marks ``"supported": false`` - so the one Oracle instance read *26 of 66 metrics* and the
  PostgreSQL one *22 of 66*, forty SQL Server-only metrics listed as "not collected";
* the Backup area was fed ``BACKUP_LAST_RESULT`` alone, so a PostgreSQL server whose backups were
  collected under ``POSTGRES_BACKUP_LAST_RESULT`` read "not collected".
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from db_ops.db import DbOpsStore
from db_ops.reports import server_report


def _catalog(tmp_path):
    path = tmp_path / "metric_definitions.json"
    path.write_text(json.dumps({"metrics": [
        {"metric_code": "QUERY_STORE_QUERY_ISSUES", "db_type": "multi", "collector_type": "sql",
         "active": True, "variants": [
             {"db_type": "sqlserver", "file": "x.sql"},
             {"db_type": "oracle", "supported": False, "unsupported_reason": "no Query Store"},
             {"db_type": "postgresql", "supported": False}]},
        {"metric_code": "TABLESPACE_FREE_SPACE", "db_type": "multi", "collector_type": "sql",
         "active": True, "variants": [{"db_type": "oracle", "file": "y.sql"}]},
    ]}), encoding="utf-8")
    return path


def test_a_variant_the_catalog_marks_unsupported_does_not_make_the_metric_expected(tmp_path):
    catalog = {entry["code"]: entry for entry in server_report.metric_catalog(_catalog(tmp_path))}
    assert catalog["QUERY_STORE_QUERY_ISSUES"]["db_types"] == ["sqlserver"]
    assert catalog["TABLESPACE_FREE_SPACE"]["db_types"] == ["oracle"]


def _stamp(minutes_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_a_postgresql_backup_result_reaches_the_backup_area(tmp_path):
    sqlite_path = tmp_path / "store.sqlite"
    DbOpsStore(sqlite_path).initialize()
    finish = (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(sqlite_path) as conn:
        conn.execute(
            "INSERT INTO metric_results (run_id, target_id, server_id, ip, db_type, db_name, metric_code,"
            " metric_item, metric_value, metric_unit, status, importance, message, collected_at)"
            " VALUES (1, 't', 'PG-1', '192.0.2.10', 'postgresql', 'store', 'POSTGRES_BACKUP_LAST_RESULT',"
            " 'store / FULL', '2', 'hours', 'OK', 4, ?, ?)",
            (f"database=store, recovery_model=FULL, backup_type=FULL, backup_finish_date={finish}",
             _stamp(5)))
    out_dir = tmp_path / "reports"
    server_report.build_server_pages(
        sqlite_path=sqlite_path, output_dir=out_dir, stamp="20261002_150000",
        snapshot_date="2026-10-02", days=7, inventory_href="database-inventory.html",
        models=[{"ip": "192.0.2.10", "server_id": "PG-1", "role": "PG-1", "dbType": "postgresql",
                 "platform": "PostgreSQL 18", "status": "ok"}])

    payload = json.loads((out_dir / server_report.series_file_name("PG-1")).read_text(encoding="utf-8"))
    backup_area = next(area for area in payload["areas"] if area["key"] == "backup")
    assert backup_area["value"] != "not collected"
    assert [row["database"] for row in payload["backup"]["databases"]] == ["store"]
