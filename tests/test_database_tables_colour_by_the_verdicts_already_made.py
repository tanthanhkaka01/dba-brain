"""Both database tables colour a backup age and a log's fullness by the verdict already made for it.

Audit 2026-11-05, A5 / B1 and A6 / B2. The fleet page and the server page coloured each backup age on
hours of their own - 26 / 48 h for a FULL or DIFF, 4 / 24 h for a LOG - while `data/backup_policy.json`
says FULL 192 / 240 h and LOG 1.5 / 2 h: a weekly FULL five days old read red while the policy called it
in time, and a log three hours old (past the policy's two) read plain. The log's used percentage was
coloured at 95 / 85 % on one page and 90 / 75 % on the other, while `LOG_FILE_SPACE` grades fullness only
when the log cannot grow. The rows now carry the policy's verdict per type and the collector's verdict
for the log, and the templates read those.
"""

from __future__ import annotations

from pathlib import Path

from dbabrain.reports import inventory_health

TEMPLATES = Path(__file__).resolve().parents[1] / "dbabrain" / "reports" / "templates"


def _row(item, value, status, message):
    return {"metric_item": item, "metric_value": value, "status": status, "message": message}


def test_each_backup_type_carries_the_policys_verdict():
    policy = {"databases": [{"database": "SALES", "status": "WARNING", "reason": "LOG 3 h old",
                             "types": {"FULL": {"state": "OK", "required": True},
                                       "DIFF": {"state": "NOT_REQUIRED", "required": False},
                                       "LOG": {"state": "CRITICAL", "required": True}}}]}
    code_map = {("BACKUP_AGE", "SALES"): _row("SALES", "120", "OK", "")}
    (row,) = inventory_health.build_backup_by_database(code_map, policy)
    assert row["type_states"] == {"FULL": "OK", "DIFF": "NOT_REQUIRED", "LOG": "CRITICAL"}
    (db,) = inventory_health.build_database_rows([{"database_name": "SALES"}], [row])
    assert db["typeStates"]["LOG"] == "CRITICAL"


def test_the_log_carries_the_collectors_verdict_before_any_remap():
    code_map = {
        ("DATABASE_STATUS", "SALES"): _row("SALES", "ONLINE", "OK", ""),
        ("LOG_FILE_SPACE", "SALES"): _row("SALES", "99.77", "LOGGING",
                                          "log_used_pct=99.77 Policy: severity remapped WARNING->LOGGING by metric_overrides."),
        ("DATABASE_STATUS", "HR"): _row("HR", "ONLINE", "OK", ""),
        ("LOG_FILE_SPACE", "HR"): _row("HR", "97.10", "LOGGING", "log_used_pct=97.10, log_can_grow=1"),
    }
    health = {h["database_name"]: h for h in inventory_health.build_database_health(code_map)}
    assert health["SALES"]["log_used_status"] == "WARNING"
    # A full log on autogrow is LOGGING by design, so not coloured.
    assert health["HR"]["log_used_status"] == "OK"


def test_neither_template_colours_by_hours_or_percentages_of_its_own():
    fleet = (TEMPLATES / "inventory_report.html").read_text(encoding="utf-8")
    server = (TEMPLATES / "server_report.html").read_text(encoding="utf-8")
    assert "26,48" not in fleet and "4,24" not in fleet
    assert "ageCell(r.fullAgeHours, 26, 48)" not in server
    assert "pct>=95 ? 'var(--crit)'" not in fleet
    assert 'pct >= 90 ? "crit"' not in server
    assert "typeStates" in fleet and "typeStates" in server
    assert "logUsedStatus" in fleet and "logUsedStatus" in server


def test_an_oracle_database_open_read_write_is_online_on_the_fleet_table():
    """A7: the row badge compared the state with 'ONLINE', so every Oracle database read red."""
    rows = inventory_health.build_database_rows(
        [{"database_name": "ORCL", "state": "READ WRITE"}, {"database_name": "SALES", "state": "SUSPECT"}], [])
    assert [row["online"] for row in rows] == [True, False]
    assert "d.online?" in (TEMPLATES / "inventory_report.html").read_text(encoding="utf-8")
