"""A backup's age grows after it was collected - the SLA and the pages read it as of now.

`BACKUP_LAST_RESULT`'s value is the engine's age of the newest backup *at collection*. Read later, it was
taken as it stood: a row collected 20 hours ago that said "LOG 1 h old" still said 1 h, so the SLA's
ALL_BACKUP_RPO_24H passed a server whose backups - or whose collection - had stopped (audit 2026-11-05,
D1). The age now grows by the time since `collected_at`, as of the moment the caller reads it.
"""

from __future__ import annotations

import datetime

from dbabrain.lib import backup_policy

UTC = datetime.timezone.utc


def _row(age, collected_at, backup_type="LOG"):
    return {"metric_code": "BACKUP_LAST_RESULT", "metric_item": f"SALES / {backup_type}", "metric_value": str(age),
            "status": "OK", "collected_at": collected_at,
            "message": f"database=SALES, backup_type={backup_type}, recovery_model=FULL, "
                       "backup_finish_date=2026-11-04 12:00:00"}


def test_an_age_read_twenty_hours_after_collection_is_twenty_hours_older():
    evidence = backup_policy.collect_evidence([_row(1.0, "2026-11-04T12:00:00Z")],
                                              as_of_utc=datetime.datetime(2026, 11, 5, 8, 0, tzinfo=UTC))
    assert evidence["SALES"]["types"]["LOG"]["age_hours"] == 21.0


def test_a_row_read_at_its_own_collection_keeps_its_age():
    evidence = backup_policy.collect_evidence([_row(1.0, "2026-11-04T12:00:00Z")],
                                              as_of_utc=datetime.datetime(2026, 11, 4, 12, 0, tzinfo=UTC))
    assert evidence["SALES"]["types"]["LOG"]["age_hours"] == 1.0


def test_a_backup_age_fallback_grows_the_same_way():
    row = {"metric_code": "BACKUP_AGE", "metric_item": "SALES", "metric_value": "10", "status": "OK",
           "collected_at": "2026-11-04T12:00:00Z", "message": ""}
    evidence = backup_policy.collect_evidence([row], as_of_utc=datetime.datetime(2026, 11, 4, 18, 0, tzinfo=UTC))
    assert evidence["SALES"]["fallback_age_hours"] == 16.0
