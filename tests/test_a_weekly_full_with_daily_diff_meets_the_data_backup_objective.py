"""A weekly-FULL plus daily-DIFF plan must judge the data backup as a unit, not as FULL alone.

The operator, 2026-10-15: "one production database's backups are a FULL on Sunday and a DIFF on the other days,
so a FULL older than 48 h is normal and is not an SLA breach. Check FULL and DIFF together at 48 h,
and LOG at about 120 minutes."

Until now `dbabrain/lib/backup_policy.py` judged each backup type on its own against the policy in
`defaults.types` (FULL at 24h warning, 48h critical). On a weekly-FULL schedule, Wednesday's backup
evaluation scored a 4-day-old FULL as CRITICAL even though a fresh differential ran ten hours ago
and the database had suffered no data-protection breach. The only work-around was an estate-wide
or per-server override loosening FULL — which then had to be repeated for every server using that
schedule and left DIFF unvalidated against the baseline.

The `data_backup` objective evaluates the newest data backup across `of_types` (FULL and DIFF by
default) at 30h warning and 48h critical, while FULL retains an upper bound of its own (192h warning,
240h critical) so a differential chain cannot rest on an ancient base. When `data_backup` is omitted
from a policy document, the evaluator preserves its legacy per-type behaviour exactly.
"""

from __future__ import annotations

import datetime

import pytest

from dbabrain.lib import backup_policy, data_sources

from conftest import shipped_config


NOW = datetime.datetime(2026, 10, 15, 12, 0, 0)


def _finish(hours_ago: float) -> str:
    dt = NOW - datetime.timedelta(hours=hours_ago)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _row(database: str, backup_type: str, hours_ago: float | None,
         recovery_model: str = "FULL", *, code: str = "BACKUP_LAST_RESULT") -> dict:
    if hours_ago is None:
        finish = ""
        val = "-1"
    else:
        finish = _finish(hours_ago)
        val = str(round(hours_ago, 1))
    msg = (f"database={database}, recovery_model={recovery_model}, "
           f"backup_type={backup_type}, backup_finish_date={finish}")
    return {
        "metric_code": code,
        "metric_item": f"{database} / {backup_type}",
        "metric_value": val,
        "status": "OK",
        "message": msg,
        "collected_at": NOW.isoformat(),
    }


@pytest.fixture
def policy() -> dict:
    return data_sources.load_backup_policy(shipped_config("backup_policy.json"))


# --------------------------------------------------------------------------- #
# The data backup objective: newest of FULL or DIFF
# --------------------------------------------------------------------------- #
def test_a_database_with_a_four_day_old_full_and_recent_diff_meets_the_data_backup_objective(policy):
    """A weekly-FULL schedule with daily DIFFs has a 96h-old FULL and a 10h-old DIFF.

    The data backup objective judges the newest of FULL and DIFF (10h <= 30h), so it is OK.
    FULL is 96h old, which is well within its own 192h bound. The database is compliant.
    """
    rows = [
        _row("app_db", "FULL", 96.0),
        _row("app_db", "DIFF", 10.0),
        _row("app_db", "LOG", 0.5),
    ]
    verdict = backup_policy.evaluate_backup_policy(
        rows, server_id="ACME-192-0-2-10", policy=policy, now=NOW
    )
    db = verdict["databases"][0]
    assert db["status"] == "OK"
    assert db["reason"] == ""

    # data_backup entry
    data = db["data_backup"]
    assert data["state"] == "OK"
    assert data["required"] is True
    assert data["age_hours"] == 10.0
    assert data["from_type"] == "DIFF"
    assert data["latest_finish"] == _finish(10.0)
    assert data["warn_hours"] == 30
    assert data["critical_hours"] == 48

    # Per-type entries
    assert db["types"]["FULL"]["state"] == "OK"
    assert db["types"]["FULL"]["age_hours"] == 96.0
    assert db["types"]["FULL"]["warn_hours"] == 192
    assert db["types"]["FULL"]["critical_hours"] == 240
    assert db["types"]["DIFF"]["state"] == "NOT_REQUIRED"
    assert db["types"]["LOG"]["state"] == "OK"


def test_a_database_with_stale_full_and_stale_diff_is_critical_on_the_data_objective(policy):
    """When both FULL (4 days) and DIFF (3 days) exceed critical hours, data_backup breaches."""
    rows = [
        _row("app_db", "FULL", 96.0),
        _row("app_db", "DIFF", 72.0),
        _row("app_db", "LOG", 0.5),
    ]
    verdict = backup_policy.evaluate_backup_policy(
        rows, server_id="ACME-192-0-2-10", policy=policy, now=NOW
    )
    db = verdict["databases"][0]
    assert db["status"] == "CRITICAL"
    assert "data backup (FULL or DIFF) 3d old" in db["reason"]

    data = db["data_backup"]
    assert data["state"] == "CRITICAL"
    assert data["age_hours"] == 72.0
    assert data["from_type"] == "DIFF"

    # FULL itself is within 192h, so the violation is purely the data_backup objective
    assert db["types"]["FULL"]["state"] == "OK"


def test_a_daily_full_without_differential_meets_the_data_backup_objective(policy):
    """A daily-FULL plan takes no DIFFs; the data backup objective is met by FULL alone."""
    rows = [
        _row("app_db", "FULL", 20.0),
        _row("app_db", "LOG", 0.5),
    ]
    verdict = backup_policy.evaluate_backup_policy(
        rows, server_id="ACME-192-0-2-10", policy=policy, now=NOW
    )
    db = verdict["databases"][0]
    assert db["status"] == "OK"

    data = db["data_backup"]
    assert data["state"] == "OK"
    assert data["age_hours"] == 20.0
    assert data["from_type"] == "FULL"
    assert db["types"]["DIFF"]["state"] == "NOT_REQUIRED"


def test_neither_full_nor_diff_on_record_is_critical_on_the_data_objective(policy):
    """A database with no data backups at all is CRITICAL and reports that none exist."""
    rows = [
        _row("app_db", "LOG", 0.5),
    ]
    verdict = backup_policy.evaluate_backup_policy(
        rows, server_id="ACME-192-0-2-10", policy=policy, now=NOW
    )
    db = verdict["databases"][0]
    assert db["status"] == "CRITICAL"
    assert "no FULL or DIFF backup on record" in db["reason"]

    data = db["data_backup"]
    assert data["state"] == "CRITICAL"
    assert data["age_hours"] is None
    assert data["from_type"] is None
    assert data["latest_finish"] == ""


def test_a_full_recovery_database_grades_log_stale_at_two_hours(policy):
    """LOG backup threshold is 1h warning and 2h critical (the operator's 120 minutes)."""
    # 150 minutes (2.5 hours) is beyond critical_hours = 2
    crit_rows = [
        _row("app_db", "FULL", 20.0),
        _row("app_db", "LOG", 2.5),
    ]
    crit_verdict = backup_policy.evaluate_backup_policy(
        crit_rows, server_id="ACME-192-0-2-10", policy=policy, now=NOW
    )
    crit_db = crit_verdict["databases"][0]
    assert crit_db["types"]["LOG"]["state"] == "CRITICAL"
    assert crit_db["status"] == "CRITICAL"

    # 90 minutes reaches warn_hours (1.5 - not 1, which an hourly log backup would flap on) and stays
    # under critical_hours = 2
    warn_rows = [
        _row("app_db", "FULL", 20.0),
        _row("app_db", "LOG", 1.5),
    ]
    warn_verdict = backup_policy.evaluate_backup_policy(
        warn_rows, server_id="ACME-192-0-2-10", policy=policy, now=NOW
    )
    warn_db = warn_verdict["databases"][0]
    assert warn_db["types"]["LOG"]["state"] == "WARNING"
    assert warn_db["status"] == "WARNING"


def test_a_policy_without_data_backup_evaluates_identically_to_legacy_behaviour():
    """An older policy without `data_backup` must evaluate exactly as before.

    Neither `data_backup` on databases nor `dataBackup` on summary is emitted, and
    thresholds follow the legacy per-type rules.
    """
    legacy_policy = {
        "defaults": {
            "types": {
                "FULL": {"required": True, "warn_hours": 24, "critical_hours": 48},
                "DIFF": {"required": False, "warn_hours": 48, "critical_hours": 96},
                "LOG": {
                    "required_for_recovery_models": ["FULL", "BULK_LOGGED"],
                    "warn_hours": 2,
                    "critical_hours": 6,
                },
            }
        }
    }
    rows = [
        _row("app_db", "FULL", 36.0),
        _row("app_db", "DIFF", 10.0),
        _row("app_db", "LOG", 1.0),
    ]
    verdict = backup_policy.evaluate_backup_policy(
        rows, server_id="ACME-192-0-2-10", policy=legacy_policy, now=NOW
    )
    db = verdict["databases"][0]

    # No data_backup key added
    assert "data_backup" not in db

    # 36h FULL under legacy 24/48 policy is WARNING
    assert db["status"] == "WARNING"
    assert db["types"]["FULL"]["state"] == "WARNING"
    assert db["types"]["FULL"]["warn_hours"] == 24
    assert db["types"]["FULL"]["critical_hours"] == 48

    # Summary has no dataBackup key
    summary = verdict["summary"]
    assert "dataBackup" not in summary
    assert summary["status"] == "WARNING"
    assert summary["compliant"] == 0
    assert summary["eligible"] == 1


def test_the_summary_shape_for_individual_backup_types_is_preserved(policy):
    """The report layer renders summary['byType'] and requires its exact shape.

    When `data_backup` is present, `dataBackup` in summary follows the same shape cleanly.
    """
    rows = [
        _row("app_db_1", "FULL", 20.0),
        _row("app_db_1", "LOG", 0.5),
        _row("app_db_2", "FULL", 96.0),
        _row("app_db_2", "DIFF", 10.0),
        _row("app_db_2", "LOG", 0.5),
    ]
    verdict = backup_policy.evaluate_backup_policy(
        rows, server_id="ACME-192-0-2-10", policy=policy, now=NOW
    )
    summary = verdict["summary"]

    # byType shape is unchanged
    assert set(summary["byType"].keys()) == {"FULL", "DIFF", "LOG"}
    for backup_type in ("FULL", "DIFF", "LOG"):
        slot = summary["byType"][backup_type]
        assert set(slot.keys()) == {"required", "compliant", "state", "worstAgeHours"}

    # dataBackup in summary mirrors the byType shape
    assert "dataBackup" in summary
    data_summary = summary["dataBackup"]
    assert set(data_summary.keys()) == {"required", "compliant", "state", "worstAgeHours"}
    assert data_summary["required"] == 2
    assert data_summary["compliant"] == 2
    assert data_summary["state"] == "OK"
    assert data_summary["worstAgeHours"] == 20.0


def test_an_override_can_tune_or_disable_the_data_backup_objective():
    """An override can customize the data_backup objective or relax its thresholds."""
    policy_with_override = {
        "defaults": {
            "data_backup": {
                "of_types": ["FULL", "DIFF"],
                "required": True,
                "warn_hours": 30,
                "critical_hours": 48,
            },
            "types": {
                "FULL": {"required": True, "warn_hours": 192, "critical_hours": 240},
                "DIFF": {"required": False, "warn_hours": 48, "critical_hours": 96},
                "LOG": {"required_for_recovery_models": ["FULL"], "warn_hours": 1, "critical_hours": 2},
            },
        },
        "overrides": [
            {
                "server_id": "ACME-192-0-2-99",
                "data_backup": {
                    "warn_hours": 60,
                    "critical_hours": 72,
                },
            }
        ],
    }
    # DIFF is 65 hours old: CRITICAL under the defaults (48 h), WARNING under the override (60-72 h)
    rows = [
        _row("app_db", "FULL", 96.0),
        _row("app_db", "DIFF", 65.0),
        _row("app_db", "LOG", 0.5),
    ]
    verdict = backup_policy.evaluate_backup_policy(
        rows, server_id="ACME-192-0-2-99", policy=policy_with_override, now=NOW
    )
    db = verdict["databases"][0]
    assert db["data_backup"]["state"] == "WARNING"
    assert db["data_backup"]["warn_hours"] == 60
    assert db["data_backup"]["critical_hours"] == 72


def test_a_database_without_a_data_objective_beside_one_with_it_still_summarises():
    """`"data_backup": null` on a database override (a database held to no data objective)
    leaves that record without it, beside records that have it. The summary read every
    record's objective and raised KeyError - found putting the backup RPO policy in front of the
    estate on 2026-10-16."""
    policy = {
        "defaults": {
            "data_backup": {"of_types": ["FULL", "DIFF"], "required": True,
                            "warn_hours": 30, "critical_hours": 48},
            "types": {"FULL": {"required": True, "warn_hours": 192, "critical_hours": 240}},
        },
        "overrides": [{"database_name": "ReportServerTempDB", "data_backup": None,
                       "types": {"FULL": {"required": False}}}],
    }
    rows = [_row("app_db", "FULL", 10.0), _row("ReportServerTempDB", "FULL", None)]

    verdict = backup_policy.evaluate_backup_policy(rows, server_id="ACME-192-0-2-99",
                                                   policy=policy, now=NOW)

    assert verdict["summary"]["dataBackup"]["required"] == 1
    assert verdict["summary"]["dataBackup"]["state"] == "OK"
