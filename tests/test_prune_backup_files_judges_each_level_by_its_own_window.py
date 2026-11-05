"""``prune-backup-files`` judges each level by its own window (0.35.0, 1.231).

The command and ``prune-backups`` used one window over a directory's every file. A
``cleanup_retention`` that names types now reaches the planner as a window per level -
``full`` / ``diff`` / ``log``, the listing's own kinds (``lib.cleanup_retention.windows_by_level``):

* under **age**, each file by its own level's cutoff; ``keep`` keeps the level;
* under **recovery_window**, the anchor is the full's window - a chain is anchored on its full;
* the floor over everything is unchanged: each database's newest full, and everything after it;
* the one-window form reads exactly as it did;
* Oracle's log level - its archivelog backups - follows the level 0 (1.230).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from dbabrain.lib import cleanup_retention as R
from dbabrain.lib.backupfiles_retention import AGE, RECOVERY_WINDOW, plan_retention

DAY = 86400
NOW = datetime(2026, 11, 3, 12, 0, 0)


def _row(kind, days_ago, name, database="APPDB"):
    stamp = (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
    return {"path": f"/b/{name}", "kind": kind, "database_name": database, "finished_at": stamp}


FILES = [_row("full", 20, "f20.bak"), _row("full", 10, "f10.bak"), _row("full", 1, "f1.bak"),
         _row("diff", 12, "d12.bak"), _row("log", 9, "l9.trn"), _row("log", 0.1, "l0.trn")]


def _obsolete(plan):
    return sorted(row["path"].rsplit("/", 1)[-1] for row in plan["obsolete"])


def test_under_age_each_level_has_its_own_cutoff():
    plan = plan_retention(FILES, mode=AGE, now=NOW, windows={"full": 14 * DAY, "diff": 14 * DAY, "log": 7 * DAY})
    # The 10-day full stays under its 14 days although the log window is 7: the plan's §0 case.
    assert _obsolete(plan) == ["f20.bak", "l9.trn"]


def test_keep_keeps_its_level():
    plan = plan_retention(FILES, mode=AGE, now=NOW, windows={"full": None, "diff": 14 * DAY, "log": 7 * DAY})
    assert _obsolete(plan) == ["l9.trn"]


def test_the_newest_chain_is_kept_whatever_any_level_says():
    plan = plan_retention([_row("full", 30, "only.bak"), _row("log", 29, "after.trn")], mode=AGE, now=NOW,
                          windows={"full": DAY, "diff": DAY, "log": DAY})
    assert _obsolete(plan) == []


def test_under_recovery_window_the_full_anchors_the_chain():
    plan = plan_retention(FILES, mode=RECOVERY_WINDOW, now=NOW, windows={"full": 14 * DAY, "diff": DAY, "log": DAY})
    # The anchor is the newest full at or before 14 days ago: f20. Nothing after it is spared or cut by
    # the shorter diff / log windows - a chain is judged whole.
    assert _obsolete(plan) == []
    plan = plan_retention(FILES, mode=RECOVERY_WINDOW, now=NOW, windows={"full": 5 * DAY, "diff": DAY, "log": DAY})
    # Anchored on f10, the newest full at or before 5 days ago: what is older goes, l9 came after it.
    assert _obsolete(plan) == ["d12.bak", "f20.bak"]


def test_the_one_window_form_reads_as_it_did():
    one = plan_retention(FILES, mode=AGE, now=NOW, retention_seconds=7 * DAY)
    assert "windows" not in one and _obsolete(one) == ["d12.bak", "f10.bak", "f20.bak", "l9.trn"]


def test_each_engine_maps_its_types_onto_the_listing_levels():
    sql = R.parse_per_type({R.FIELD: {"FULL": 14 * DAY, "LOG": 7 * DAY, "default": 14 * DAY}},
                           context="t", engine="sqlserver")
    assert R.windows_by_level(sql, engine="sqlserver") == {"full": 14 * DAY, "diff": 14 * DAY, "log": 7 * DAY}
    ora = R.parse_per_type({R.FIELD: {"RMAN_L0": 10 * DAY, "ARCHIVELOG": DAY, "default": 10 * DAY}},
                           context="t", engine="oracle")
    assert R.windows_by_level(ora, engine="oracle")["log"] == 10 * DAY
    assert R.windows_by_level(R.parse_per_type({R.FIELD: 8 * DAY}, context="t"), engine="sqlserver") is None


def test_the_request_carries_the_object_back():
    value = {"FULL": 14 * DAY, "LOG": "keep", "default": 14 * DAY}
    assert R.parse_per_type({R.FIELD: value}, context="t", engine="sqlserver").as_config() == value
