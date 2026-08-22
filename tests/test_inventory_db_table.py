"""The per-database table and the data-freshness marking in the inventory report.

Both exist because the page could not answer "how is this database?" and could not tell a
value collected an hour ago from one carried over from a manual inventory months earlier.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from db_ops.reports.inventory_health import build_inventory_health
from db_ops.reports.inventory_report import build_models, render_html
from db_ops.reports.inventory_summary import _merge_overlay


# Anchored to the clock, not to a literal date. These tests were written with NOW frozen at
# 2026-07-29T07:00:00Z and read the store through a rolling `days=7` window, so they passed for a
# week and then failed at 2026-08-05T07:00:00Z — the exact minute the fixture data aged out of the
# window. A suite that starts failing on a wall-clock boundary teaches you to distrust it on the
# one morning it is telling the truth.
_NOW_DT = datetime.now(timezone.utc).replace(microsecond=0)
NOW = _NOW_DT.strftime("%Y-%m-%dT%H:%M:%SZ")
OLD = (_NOW_DT - timedelta(days=39)).strftime("%Y-%m-%dT%H:%M:%SZ")
TODAY = _NOW_DT.strftime("%Y-%m-%d")


def _ago(*, days=0, hours=0, fmt="%Y-%m-%d %H:%M:%S") -> str:
    """A timestamp that stays the same distance from now, whenever the suite runs."""
    return (_NOW_DT - timedelta(days=days, hours=hours)).strftime(fmt)


def _sqlite(path, rows):
    # result_id / db_type / collector_type / error_type are part of the real metric_results and
    # are read by the freshness query (which metric last succeeded, was it the collector or the
    # check that failed, does this target even have a cmd collector), so a fixture without them
    # tests a table the store never has.
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE metric_results (result_id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " ip TEXT, server_id TEXT, db_type TEXT, db_name TEXT, metric_code TEXT,"
        " metric_item TEXT, metric_value TEXT, metric_unit TEXT, status TEXT, message TEXT,"
        " collected_at TEXT, collector_type TEXT, error_type TEXT)"
    )
    con.executemany(
        "INSERT INTO metric_results (ip, server_id, db_type, db_name, metric_code, metric_item,"
        " metric_value, metric_unit, status, message, collected_at, collector_type)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    con.commit()
    con.close()


def _row(code, item, value, message="", unit="", status="OK", when=NOW,
         sid="ACME-192-0-2-115", ip="192.0.2.115"):
    return (ip, sid, "sqlserver", "SALESDB-PROD", code, item, value, unit, status, message, when, "sql")


@pytest.fixture
def built(tmp_path):
    """Render the report from metrics shaped like the real ERP box."""
    rows = [
        # SALESDB: FULL recovery, daily FULL, log backup 121 days behind, log nearly full.
        _row("DATABASE_STATUS", "SALESDB", "ONLINE", "read_only=0"),
        _row("DATABASE_CONFIG", "SALESDB", "FULL",
             "compatibility_level=130, page_verify=TORN_PAGE_DETECTION, is_read_only=0, state=ONLINE"),
        _row("DATABASE_CHECKDB", "SALESDB", "unknown", "Last successful DBCC CHECKDB"),
        _row("DATABASE_DATA_SIZE", "SALESDB", "1664.31", unit="GB"),
        _row("DATABASE_LOG_SIZE", "SALESDB", "217.0", unit="GB"),
        _row("LOG_FILE_SPACE", "SALESDB", "99.87", unit="pct"),
        _row("LOG_REUSE_WAIT", "SALESDB", "LOG_BACKUP"),
        _row("BACKUP_AGE", "SALESDB", "8", unit="hours"),
        _row("BACKUP_LAST_RESULT", "SALESDB / FULL", "7",
             f"backup_type=D, backup_finish_date={_ago(hours=8)}, recovery_model=FULL"),
        _row("BACKUP_LAST_RESULT", "SALESDB / DIFF", "18",
             f"backup_type=I, backup_finish_date={_ago(hours=20)}, recovery_model=FULL"),
        _row("BACKUP_LAST_RESULT", "SALESDB / LOG", "2897",
             f"backup_type=L, backup_finish_date={_ago(days=121)}, recovery_model=FULL"),
        # A healthy database, for contrast.
        _row("DATABASE_STATUS", "OrchestratorData", "ONLINE", "read_only=0"),
        _row("DATABASE_CONFIG", "OrchestratorData", "FULL",
             "compatibility_level=150, page_verify=CHECKSUM, is_read_only=0, state=ONLINE"),
        _row("DATABASE_CHECKDB", "OrchestratorData", _ago(days=2, hours=4)),
        _row("DATABASE_DATA_SIZE", "OrchestratorData", "0.01", unit="GB"),
        _row("DATABASE_LOG_SIZE", "OrchestratorData", "0.07", unit="GB"),
        _row("LOG_FILE_SPACE", "OrchestratorData", "14.29", unit="pct"),
        _row("BACKUP_LAST_RESULT", "OrchestratorData / FULL", "7",
             f"backup_type=D, backup_finish_date={_ago(hours=9)}, recovery_model=FULL"),
        _row("BACKUP_LAST_RESULT", "OrchestratorData / LOG", "2",
             f"backup_type=L, backup_finish_date={_ago(hours=2)}, recovery_model=FULL"),
    ]
    sqlite_path = tmp_path / "db_ops.sqlite"
    _sqlite(sqlite_path, rows)

    inventory = {
        "servers": [
            {
                "server_id": "ACME-192-0-2-115", "ip": "192.0.2.115", "company_code": "ACME",
                "databases": [{"db_type": "sqlserver", "service_name": "SALESDB-PROD",
                               "server_name": "SALESCLUSTER", "database_names": ["SALESDB", "OrchestratorData"]}],
                # Stale carry-overs the metrics overlay never refreshes.
                "backup": {"jobs": [{"job_name": "Backup_Full", "last_status": "SUCCEEDED",
                                     "last_run_datetime": _ago(days=39, hours=9)}]},
                "sqlserver_resources": {"database_sizes": [{"database_name": "SALESDB", "size_mb": 999999}]},
            },
            {
                # No metrics at all: keeps whatever it was last given, and must say so.
                "server_id": "ACME-192-0-2-113", "ip": "192.0.2.113", "company_code": "ACME",
                "databases": [{"db_type": "sqlserver", "service_name": "SALESDB-PROD-PASSIVE",
                               "server_name": "ACMESQL02", "database_names": ["SALESDB"]}],
                "database_health": [{"database_name": "SALESDB", "state": "ONLINE",
                                     "recovery_model": "FULL", "last_good_checkdb": "unknown"}],
                "inventory_status": {"last_checked": _ago(days=39, fmt="%Y-%m-%d"), "db_metadata": "failed",
                                     "reason": "odbc18 login failed"},
            },
        ]
    }
    health = build_inventory_health(sqlite_path=str(sqlite_path), output_dir=tmp_path, days=7)
    overlay = json.loads(Path(health["file"]).read_text(encoding="utf-8"))
    _merge_overlay(overlay, inventory)
    scope, models = build_models(inventory)
    html = render_html(scope, models, [], TODAY)
    return {m["ip"]: m for m in models}, html


def test_every_database_gets_a_row_with_what_it_is_and_how_big(built):
    models, _ = built
    dbs = {d["name"]: d for d in models["192.0.2.115"]["databases"]}
    assert set(dbs) == {"SALESDB", "OrchestratorData"}
    salesdb = dbs["SALESDB"]
    assert salesdb["state"] == "ONLINE"
    assert salesdb["recovery"] == "FULL"
    assert salesdb["dataGB"] == 1664.31
    assert salesdb["logGB"] == 217.0
    assert salesdb["logUsedPct"] == 99.87
    assert salesdb["compat"] == 130
    assert salesdb["pageVerify"] == "TORN_PAGE_DETECTION"
    assert salesdb["checkdb"] == "unknown"


def test_each_backup_type_keeps_its_own_age(built):
    """A daily FULL next to a LOG backup 121 days behind is a broken chain; one combined
    'last backup' number reports that as healthy."""
    models, _ = built
    salesdb = {d["name"]: d for d in models["192.0.2.115"]["databases"]}["SALESDB"]
    assert salesdb["fullAgeHours"] == 7
    assert salesdb["diffAgeHours"] == 18
    assert salesdb["logAgeHours"] == 2897
    assert salesdb["logReuseWait"] == "LOG_BACKUP"


def test_live_sizes_win_over_the_static_inventory_number(built):
    """The static block said 999999 MB for SALESDB; the metric says 1664.31 GB."""
    models, html = built
    salesdb = {d["name"]: d for d in models["192.0.2.115"]["databases"]}["SALESDB"]
    assert salesdb["dataGB"] == 1664.31
    assert salesdb["staticSize"] is False
    # And the stale "Database sizes" detail row is gone now that live rows exist.
    assert not any(row[0] == "Database sizes" for row in models["192.0.2.115"]["detail"])
    assert "999999" not in html


def test_a_static_detail_row_is_labelled_as_static(built):
    """backup.jobs is carried in the inventory file and never refreshed — its June timestamp
    must not read like the live backup evidence above it."""
    models, html = built
    jobs = [row for row in models["192.0.2.115"]["detail"] if row[0] == "Backup jobs"]
    assert jobs and jobs[0][2] == "static"
    assert "src-tag" in html


def test_a_server_with_live_metrics_is_marked_fresh(built):
    models, _ = built
    fresh = models["192.0.2.115"]["freshness"]
    assert fresh["asOf"].startswith(TODAY)
    assert fresh["collectFailed"] is False


def test_a_server_the_overlay_never_reached_is_marked_stale_and_failed(built):
    """It keeps its last known blocks — which is fine — but the page must not present them
    as current, and must say the collection failed."""
    models, html = built
    stale = models["192.0.2.113"]["freshness"]
    assert stale["asOf"] == ""
    assert stale["stale"] is True
    assert stale["collectFailed"] is True
    assert "login failed" in stale["collectReason"]
    assert "collection failed" in html


def test_the_html_renders_the_table_and_its_columns(built):
    _, html = built
    assert "db-tbl" in html
    for column in ("Recovery", "Log (used)", "Page verify", "Last CHECKDB"):
        assert column in html
    assert "TORN_PAGE_DETECTION" in html


# --------------------------------------------------------------------------- #
# Security posture and per-database users
# --------------------------------------------------------------------------- #
@pytest.fixture
def secured(tmp_path):
    """Metrics shaped like the real ERP box: one login hammered, ten stale passwords."""
    rows = [
        _row("DATABASE_STATUS", "SALESDB", "ONLINE", "read_only=0"),
        _row("DATABASE_CONFIG", "SALESDB", "FULL", "compatibility_level=130, page_verify=CHECKSUM"),
        _row("SECURITY_FAILED_LOGINS", "failed_logins :: 24h", "10065",
             "distinct_logins=2 total_failed_24h=10065", status="WARNING"),
        _row("SECURITY_FAILED_LOGINS", r"failed_login\aws_admin", "10064",
             "failed login attempts for this principal in the last 24h", status="WARNING"),
        _row("SECURITY_FAILED_LOGINS", r"failed_login\tsiiplan", "1", "", status="OK"),
        _row("SECURITY_LOGIN_HEALTH", "login_health :: summary", "13",
             "failed=1 long_session=0 password_old=12 dormant=0", status="WARNING"),
        _row("SECURITY_LOGIN_HEALTH", r"password_old\sa", "1337",
             "password_age_days=1337 last_set=2022-11-30 (threshold=180d)", status="WARNING"),
        _row("SECURITY_LOGIN_HEALTH", r"password_old\salesdbadmin", "1335",
             "password_age_days=1335 last_set=2022-12-02 (threshold=180d)", status="WARNING"),
        _row("SECURITY_CERTIFICATE_EXPIRY", "certificates :: summary", "0", "expired=0 expiring_30d=0"),
        # Per-database principals: two of them own the database.
        _row("DATABASE_USER_PERMISSIONS", r"SALESDB\salesdbadmin", "SQL_USER",
             "login=salesdbadmin | roles=[db_owner] | HIGH_PRIVILEGE"),
        _row("DATABASE_USER_PERMISSIONS", r"SALESDB\ACME\svc-SALESSF$", "WINDOWS_USER",
             r"login=allianceone\svc-SALESSF$ | roles=[db_owner] | HIGH_PRIVILEGE"),
        _row("DATABASE_USER_PERMISSIONS", r"SALESDB\david", "SQL_USER", "login=david | roles=[db_datareader]"),
    ]
    sqlite_path = tmp_path / "db_ops.sqlite"
    _sqlite(sqlite_path, rows)
    inventory = {"servers": [{
        "server_id": "ACME-192-0-2-115", "ip": "192.0.2.115", "company_code": "ACME",
        "databases": [{"db_type": "sqlserver", "service_name": "SALESDB-PROD",
                       "server_name": "SALESCLUSTER", "database_names": ["SALESDB"]}],
    }]}
    health = build_inventory_health(sqlite_path=str(sqlite_path), output_dir=tmp_path, days=7)
    overlay = json.loads(Path(health["file"]).read_text(encoding="utf-8"))
    _merge_overlay(overlay, inventory)
    scope, models = build_models(inventory)
    from db_ops.reports.inventory_report import build_triage
    return models[0], build_triage(models), render_html(scope, models, build_triage(models), TODAY)


def test_failed_logins_reach_the_report(secured):
    """~10k failed attempts a day against one login was collected all week and shown nowhere."""
    model, triage, html = secured
    sec = model["security"]
    assert sec["failed24h"] == 10065
    assert sec["worstLogin"] == {"login": "aws_admin", "attempts": 10064.0, "status": "WARNING"}
    assert "aws_admin" in html
    assert "failed logins 24h" in html


def test_a_hammered_login_becomes_a_critical_triage_card(secured):
    _model, triage, _html = secured
    card = next(c for c in triage if "failed logins" in c["title"].lower())
    assert card["sev"] == "crit"
    assert "aws_admin" in card["body"] and "10,064" in card["body"]
    assert "raise the alert threshold" in card["action"]   # says what NOT to do, too


def test_stale_passwords_are_listed_and_warned_about(secured):
    model, triage, html = secured
    sec = model["security"]
    assert sec["oldestPasswordDays"] == 1337
    assert [p["login"] for p in sec["passwordsOld"]] == ["sa", "salesdbadmin"]   # oldest first
    assert any("password" in c["title"].lower() for c in triage)
    assert "1337" in html


def test_each_database_reports_its_users_and_owners(secured):
    model, _triage, html = secured
    salesdb = {d["name"]: d for d in model["databases"]}["SALESDB"]
    assert salesdb["users"] == 3
    assert salesdb["highPrivUsers"] == 2
    assert salesdb["owners"] == [r"ACME\svc-SALESSF$", "salesdbadmin"]
    assert "Users" in html            # the column exists
    assert "db_owner:" in html        # owners are named in the cell tooltip


def test_a_server_without_security_metrics_shows_no_security_block(tmp_path):
    rows = [_row("DATABASE_STATUS", "SALESDB", "ONLINE", "read_only=0")]
    sqlite_path = tmp_path / "db_ops.sqlite"
    _sqlite(sqlite_path, rows)
    inventory = {"servers": [{
        "server_id": "ACME-192-0-2-115", "ip": "192.0.2.115",
        "databases": [{"db_type": "sqlserver", "database_names": ["SALESDB"]}],
    }]}
    health = build_inventory_health(sqlite_path=str(sqlite_path), output_dir=tmp_path, days=7)
    _merge_overlay(json.loads(Path(health["file"]).read_text(encoding="utf-8")), inventory)
    _scope, models = build_models(inventory)
    sec = models[0]["security"]
    assert sec["failed24h"] is None and sec["passwordsOld"] == []
    assert sec["worstLogin"] is None
