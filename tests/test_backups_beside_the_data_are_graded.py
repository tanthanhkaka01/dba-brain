"""Backups kept beside the data are graded, not only listed (0.35.0, 1.239).

The ransomware plan of 2026-10-30 measured the estate from `STORAGE_FILE_PLACEMENT`: 7 of 12 SQL
Server instances wrote their backups to the drive holding their data, and all 12 kept every backup on
the database host - and nothing alerted, because the metric was logging only. R-1: same drive is
CRITICAL, same host WARNING. Run read-only on a current instance and on a 2008 R2 one when written
(11 CRITICAL; 4 CRITICAL and 3 WARNING).

* the collector grades one `PLACEMENT | <db>` row per database with a recent backup;
* the grade is computed per path before it is aggregated - SQL Server refuses an aggregate over a
  subquery (Msg 130), found running it;
* the inventory's disk block reads the file and backup rows as before and skips the grade.
"""

from __future__ import annotations

from pathlib import Path

from dbabrain.reports import inventory_health

SQL = (Path(__file__).resolve().parents[1] / "dbabrain" / "metrics" / "collectors" / "sqlserver"
       / "055_sqlserver_file_placement.sql").read_text(encoding="utf-8")


def test_the_collector_grades_same_drive_critical_and_same_host_warning():
    assert "'PLACEMENT | ' + p.db" in SQL
    assert "WHEN p.same_drive = 1 THEN 'CRITICAL'" in SQL
    assert "WHEN p.off_host = 0 THEN 'WARNING'" in SQL
    assert "LIKE '\\\\%'" in SQL, "a UNC share is off the host"


def test_it_runs_on_the_oldest_supported_engine():
    code = " ".join(line.split("--", 1)[0] for line in SQL.splitlines())
    assert "STRING_AGG" not in code, "SQL Server 2008 R2 has no STRING_AGG"
    assert "MAX(b.same_drive)" in SQL, "graded per path, aggregated after (Msg 130)"


def test_the_inventory_disk_block_skips_the_grade(monkeypatch):
    rows = [("STORAGE_FILE_PLACEMENT", "SALESDB | ROWS", "D:\\", "D:\\DATA\\SALESDB.mdf"),
            ("STORAGE_FILE_PLACEMENT", "SALESDB | LOG", "D:\\", "D:\\DATA\\SALESDB.ldf"),
            ("STORAGE_FILE_PLACEMENT", "BACKUP | SALESDB", "D:\\", "D:\\SQLBK\\SALESDB.bak"),
            ("STORAGE_FILE_PLACEMENT", "PLACEMENT | SALESDB", "same drive", "same_drive_as_data=1")]
    monkeypatch.setattr(inventory_health, "items_for", lambda code_map, code: [
        (item, {"metric_value": value, "message": message, "status": "OK"})
        for metric, item, value, message in rows if metric == code])
    disk = inventory_health.build_disk_health({})
    assert disk["data_file_drives"] == ["D:\\"] and disk["backup_path"] == ["D:\\SQLBK\\SALESDB.bak"]
    assert "same drive" not in disk["data_file_drives"] + disk["log_file_drives"]
