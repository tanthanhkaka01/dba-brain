"""The per-database table, and the rule that one metric's scope cannot erase another's findings.

`server-metrics.html` had every per-database fact on it — size, log usage, recovery model, CHECKDB
age, backup ages, Query Store — and no table that put them on one line per database, so "what is on
this server and how is each one doing" meant reading six sections and joining them by eye.

The bug underneath it is the one worth protecting against. `DATABASE_STATUS` is used as the
authority on which databases still exist, so that a database dropped this morning does not keep a
row until yesterday's daily size sample ages out. But its SQL Server variant selects
`WHERE d.database_id > 4` — it never reports master, tempdb, model or msdb. Letting it decide the
whole list therefore *deleted* those four from every database table on every page, while
`DATABASE_CHECKDB` went on raising warnings against them. Three databases on 192.0.2.115 each
carried "no CHECKDB has ever been recorded" with no row anywhere to attach it to.
"""

from db_ops.reports import inventory_health
from db_ops.reports.server_report import build_databases


def _row(code, item, value, message="", collected_at="2026-08-14T01:00:00Z"):
    return {"metric_code": code, "metric_item": item, "metric_value": value,
            "message": message, "collected_at": collected_at, "status": "OK"}


def _code_map(*rows):
    return {(r["metric_code"], r["metric_item"]): r for r in rows}


def test_a_system_database_keeps_its_row_although_database_status_never_names_it():
    """The whole reason the section was asked for: master carries a CHECKDB finding and had
    nowhere to appear."""
    code_map = _code_map(
        _row("DATABASE_STATUS", "SALESDB", "ONLINE"),
        _row("DATABASE_CHECKDB", "SALESDB", "never"),
        _row("DATABASE_CHECKDB", "master", "never"),
        _row("DATABASE_CHECKDB", "msdb", "never"),
        _row("DATABASE_CONFIG", "master", "SIMPLE"),
    )

    names = [d["database_name"] for d in inventory_health.build_database_health(code_map)]

    assert names == ["SALESDB", "master", "msdb"]


def test_a_dropped_user_database_still_disappears():
    """The authority rule is not simply removed — that is what it is for. A user database absent
    from the newest DATABASE_STATUS is gone, even while yesterday's size sample still mentions it."""
    code_map = _code_map(
        _row("DATABASE_STATUS", "SALESDB", "ONLINE"),
        _row("DATABASE_DATA_SIZE", "SALESDB", "1744.77"),
        _row("DATABASE_DATA_SIZE", "RetiredDb", "12.5"),
        _row("DATABASE_CHECKDB", "RetiredDb", "never"),
    )

    names = [d["database_name"] for d in inventory_health.build_database_health(code_map)]

    assert names == ["SALESDB"]


def test_with_no_database_status_at_all_every_named_database_is_kept():
    """An engine or a target whose DATABASE_STATUS has not run has no authority to apply, and an
    empty table is a worse answer than a possibly-stale one."""
    code_map = _code_map(
        _row("DATABASE_CHECKDB", "SALESDB", "never"),
        _row("DATABASE_DATA_SIZE", "SALESDW", "0.4"),
    )

    names = [d["database_name"] for d in inventory_health.build_database_health(code_map)]

    assert names == ["SALESDB", "SALESDW"]


def test_the_section_counts_databases_that_have_never_been_checked():
    """"never" and an empty value both mean no successful CHECKDB was ever recorded; counting only
    one of them understates the finding the header exists to raise."""
    code_map = _code_map(
        _row("DATABASE_STATUS", "SALESDB", "ONLINE"),
        _row("DATABASE_CHECKDB", "SALESDB", "never"),
        _row("DATABASE_CHECKDB", "master", ""),
        _row("DATABASE_CHECKDB", "msdb", "2026-08-11 10:04:32"),
    )

    section = build_databases(code_map)

    assert section["summary"]["count"] == 3
    assert section["summary"]["neverCheckdb"] == 2


def test_an_offline_database_is_counted_apart_from_the_online_ones():
    code_map = _code_map(
        _row("DATABASE_STATUS", "SALESDB", "ONLINE"),
        _row("DATABASE_STATUS", "Archive", "RECOVERY_PENDING"),
    )

    summary = build_databases(code_map)["summary"]

    assert (summary["count"], summary["online"]) == (2, 1)


def test_the_two_reports_build_their_database_rows_from_one_function():
    """The fleet page's Server Detail and the per-server page render the same table. A second copy
    of the join is a second set of columns that can disagree about the same database."""
    rows = inventory_health.build_database_rows(
        [{"database_name": "SALESDB", "state": "ONLINE", "recovery_model": "FULL",
          "log_used_percent": 99.8, "last_good_checkdb": "never"}],
        [{"database_name": "SALESDB", "full_age_hours": 8.0, "log_age_hours": 3480.0,
          "status": "CRITICAL"}],
    )

    assert len(rows) == 1
    assert rows[0]["name"] == "SALESDB"
    assert rows[0]["logUsedPct"] == 99.8
    assert rows[0]["checkdb"] == "never"
    assert rows[0]["logAgeHours"] == 3480.0


def test_databases_are_listed_in_catalog_order_not_alphabetically():
    """`database_id` order — master, tempdb, model, msdb, then user databases as created. It is
    how a DBA reads a database list and how every other tool shows it; alphabetical put `SALESDB`
    above `master` and scattered the system four through the middle of the user ones."""
    health = [
        {"database_name": "SALESDB", "database_id": 5},
        {"database_name": "master", "database_id": 1},
        {"database_name": "msdb", "database_id": 4},
        {"database_name": "SALESDW", "database_id": 7},
        {"database_name": "tempdb", "database_id": 2},
        {"database_name": "model", "database_id": 3},
    ]

    names = [r["name"] for r in inventory_health.build_database_rows(health, [])]

    assert names == ["master", "tempdb", "model", "msdb", "SALESDB", "SALESDW"]


def test_a_database_whose_id_was_not_collected_sorts_after_the_ones_that_have_one():
    """`database_id` was added to the metrics on 2026-08-14, so a target that has not re-collected
    since carries none. Folding a missing id to 0 would sort its databases ahead of `master`;
    they go last, by name, until the metric next runs."""
    health = [
        {"database_name": "master", "database_id": 1},
        {"database_name": "Zulu"},
        {"database_name": "SALESDB", "database_id": 5},
        {"database_name": "Alpha"},
    ]

    names = [r["name"] for r in inventory_health.build_database_rows(health, [])]

    assert names == ["master", "SALESDB", "Alpha", "Zulu"]


def test_a_database_known_only_from_its_backup_evidence_still_appears():
    """The id lives on the health block, so a row that exists only in the backup block has no
    sort key at all. It must still be listed — a database with backup evidence and no health
    row is exactly the kind of gap the table should show."""
    rows = inventory_health.build_database_rows(
        [{"database_name": "master", "database_id": 1}],
        [{"database_name": "OrphanDb", "full_age_hours": 12.0}],
    )

    assert [r["name"] for r in rows] == ["master", "OrphanDb"]


def test_a_size_the_metrics_did_not_carry_is_marked_as_the_static_fallback():
    """The fleet report falls back to the canonical inventory file for a size the live metrics
    missed. A number from a manual inventory months old must not pass for today's."""
    live = inventory_health.build_database_rows(
        [{"database_name": "SALESDB", "data_size_gb": 1744.77}], [])
    fallback = inventory_health.build_database_rows(
        [{"database_name": "SALESDB"}], [], static_sizes={"SALESDB": 2048.0})

    assert (live[0]["dataGB"], live[0]["staticSize"]) == (1744.77, False)
    assert (fallback[0]["dataGB"], fallback[0]["staticSize"]) == (2.0, True)
