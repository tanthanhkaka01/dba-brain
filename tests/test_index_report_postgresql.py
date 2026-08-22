"""The PostgreSQL index report — the one engine whose catalog can actually answer the question.

Before this, `index-usage_<slug>.html` was published for SQL Server and Oracle and returned 404
for every PostgreSQL instance. That is backwards: `pg_stat_user_indexes.idx_scan` is a real
per-index read counter and, from PG 16, `last_idx_scan` says *when* it was last read — which SQL
Server cannot tell you at all, and which Oracle only knows if every index was individually put
into `ALTER INDEX ... MONITORING USAGE`.

So PostgreSQL reuses the shared usage report rather than getting its own renderer the way Oracle
did. What it does not share is the three places the engines genuinely differ, and each of those is
a way to put a wrong instruction in a report:

- **There is no clustered index.** The drop rule excluded anything that was not `NONCLUSTERED`,
  which on PostgreSQL is everything: the page would report `droppable: 22` in its totals and list
  none of them.
- **There is no REBUILD.** An invalid index is dropped and re-created; telling somebody to rebuild
  it is a command that does not exist.
- **A constraint owns its index.** `DROP INDEX` on a primary-key or unique-constraint index fails
  outright, so "review, then DROP" is an instruction that errors when run.

The messages below are real output from `assets/metrics/postgresql/072_postgresql_index_usage.sql`
against the `db_ops` store on 2026-08-17.
"""

import pytest

from db_ops.reports.index_report import collect_index_rows, format_index_report


class _FakeMetricStore:
    """Stands in for MetricStore so the parser can be driven from literal rows.

    `collect_index_rows` constructs its own store from a path, so the class is swapped in the
    module rather than passed — the drop rule under test lives in that parser, not in the
    renderer the other index tests exercise directly.
    """

    def __init__(self, rows):
        self._rows = rows

    def fetch_health_metrics(self, **_):
        return self._rows


@pytest.fixture(autouse=True)
def _fake_store(monkeypatch):
    holder: dict[str, list] = {"rows": []}
    monkeypatch.setattr("db_ops.reports.index_report.MetricStore",
                        lambda _source: _FakeMetricStore(holder["rows"]))
    return holder


def _row(item, value, unit, message, status="OK"):
    return {"server_id": "ACME-192-0-2-249-PGLAB-5433", "ip": "192.0.2.249",
            "collected_at": "2026-08-17T18:00:00Z", "metric_code": "MAINTENANCE_INDEX_USAGE",
            "metric_item": item, "metric_value": value, "metric_unit": unit,
            "status": status, "message": message}


SUMMARY = _row(
    "index_usage :: summary", "65", "summary",
    "indexes_total=65, used=43, unused=22, cold=0, disabled=0, disabled_clustered=0, "
    "droppable=22, missing_suggestions=0, database=db_ops, counters_since=never reset "
    "| counters have never been reset: idx_scan covers the whole life of the statistics "
    "| scope=db_ops only")

UNUSED = _row(
    "db_ops.db_ops.backup_restore_history.ix_backup_restore_history_db_created", "0", "idx_scan",
    "UNUSED: db=db_ops, schema=db_ops, table=backup_restore_history, "
    "index_name=ix_backup_restore_history_db_created, index_id=25353, type_desc=btree, "
    "is_unique=0, is_primary_key=0, is_unique_constraint=0, is_disabled=0, has_filter=0, "
    "idx_scan=0, idx_tup_read=0, idx_tup_fetch=0, user_seeks=0, user_scans=0, user_lookups=0, "
    "user_updates=5029, table_writes=5029, size_mb=0.06, last_read=never, "
    "last_stats_update=2026-08-16 23:14:41, counters_since=never reset "
    "| never read, and the table took 5029 write(s) that all maintained it; "
    "action=review, then DROP INDEX")

PRIMARY_KEY = _row(
    "db_ops.db_ops.backup_restore_history.backup_restore_history_pkey", "252", "idx_scan",
    "USED: db=db_ops, schema=db_ops, table=backup_restore_history, "
    "index_name=backup_restore_history_pkey, index_id=18584, type_desc=btree, is_unique=1, "
    "is_primary_key=1, is_unique_constraint=0, is_disabled=0, has_filter=0, idx_scan=252, "
    "idx_tup_read=256, idx_tup_fetch=254, user_seeks=252, user_scans=0, user_lookups=0, "
    "user_updates=5029, table_writes=5029, size_mb=0.04, "
    "last_read=2026-08-16T23:14:41.901687+00:00, last_stats_update=2026-08-16 23:14:41, "
    "counters_since=never reset | primary key: enforces uniqueness; action=KEEP")


def _entry(store, *rows):
    store["rows"] = list(rows)
    return collect_index_rows("unused-path")["ACME-192-0-2-249-PGLAB-5433"]


def test_an_unread_btree_index_is_a_drop_candidate(_fake_store):
    """The rule keyed on `type_desc == "NONCLUSTERED"`, a SQL Server word. PostgreSQL reports the
    access method (`btree`, `gin`, `brin`), so the list came out empty while the totals said 22."""
    entry = _entry(_fake_store, SUMMARY, UNUSED)

    assert entry["engine"] == "postgresql"
    assert [row["item"] for row in entry["droppable"]] == [
        "db_ops.db_ops.backup_restore_history.ix_backup_restore_history_db_created"]
    assert entry["totals"]["droppable"] == 22


def test_the_totals_and_the_listed_candidates_do_not_contradict_each_other(_fake_store):
    """A page reporting `droppable: 22` and listing none is worse than one reporting neither —
    the reader concludes the indexes are unnamed rather than that the rule failed."""
    entry = _entry(_fake_store, SUMMARY, UNUSED, PRIMARY_KEY)

    assert entry["totals"]["droppable"] >= len(entry["droppable"])
    assert len(entry["droppable"]) == 1


def test_a_primary_key_is_never_offered_as_a_drop_candidate(_fake_store):
    entry = _entry(_fake_store, SUMMARY, PRIMARY_KEY)

    assert entry["droppable"] == []


def test_the_page_renders_and_names_the_database_it_covered(_fake_store):
    """PostgreSQL cannot read another database's catalog from one connection, so a page that
    described one database while looking like the whole cluster would be a quiet lie."""
    text = format_index_report(_entry(_fake_store, SUMMARY, UNUSED, PRIMARY_KEY), limit=None)

    assert "Index Usage Report" in text
    assert "ix_backup_restore_history_db_created" in text
    assert "db_ops" in text


def test_an_invalid_index_is_dropped_and_recreated_never_rebuilt(_fake_store):
    """PostgreSQL has no REBUILD. `is_disabled=1` carries INVALID here — the planner ignores the
    index while every write still maintains it, which is all of the cost and none of the benefit.
    It is also not the "table is inaccessible" incident a disabled clustered index is."""
    invalid = _row(
        "db_ops.db_ops.metric_results.ix_broken", "0", "idx_scan",
        "COLD: db=db_ops, schema=db_ops, table=metric_results, index_name=ix_broken, "
        "index_id=99, type_desc=btree, is_unique=0, is_primary_key=0, is_unique_constraint=0, "
        "is_disabled=1, has_filter=0, idx_scan=0, user_seeks=0, user_scans=0, user_lookups=0, "
        "user_updates=10, size_mb=1.00, last_read=never, last_stats_update=never "
        "| INVALID index: action=DROP INDEX and re-create it", status="WARNING")
    entry = _entry(_fake_store, SUMMARY, invalid)

    assert len(entry["disabled"]) == 1
    # Not flagged as the table-inaccessible incident: that is a SQL Server clustered index.
    assert entry["disabled"][0]["clustered"] is False
    text = format_index_report(entry, limit=None)
    assert "REBUILD now" not in text


def test_a_sqlserver_report_is_unchanged_by_any_of_this(_fake_store):
    """The shared path still has to behave exactly as it did — the engine is read from the unit,
    and SQL Server's detail rows carry `user_updates`, not `idx_scan`."""
    mssql_summary = _row(
        "index_usage :: summary", "3", "summary",
        "indexes_total=3, used=1, unused=1, cold=1, disabled=0, disabled_clustered=0, "
        "droppable=1, missing_suggestions=0, uptime_days=30.0, counters_since=2026-07-18 01:00:00")
    clustered = _row(
        "SALESDB.dbo.Orders.PK_Orders", "500", "user_updates",
        "USED: db=SALESDB, schema=dbo, table=Orders, index_name=PK_Orders, index_id=1, "
        "type_desc=CLUSTERED, is_unique=1, is_primary_key=1, is_unique_constraint=0, "
        "is_disabled=0, has_filter=0, user_seeks=10, user_scans=0, user_lookups=0, "
        "user_updates=500, last_read=2026-08-17 09:00:00, last_stats_update=2026-08-01 00:00:00")
    cold = _row(
        "SALESDB.dbo.Orders.IX_Orders_Cold", "500", "user_updates",
        "COLD: db=SALESDB, schema=dbo, table=Orders, index_name=IX_Orders_Cold, index_id=5, "
        "type_desc=NONCLUSTERED, is_unique=0, is_primary_key=0, is_unique_constraint=0, "
        "is_disabled=0, has_filter=0, user_seeks=0, user_scans=0, user_lookups=0, "
        "user_updates=500, last_read=never, last_stats_update=2026-08-01 00:00:00")
    entry = _entry(_fake_store, mssql_summary, clustered, cold)

    assert entry.get("engine") in (None, "")
    assert [row["item"] for row in entry["droppable"]] == ["SALESDB.dbo.Orders.IX_Orders_Cold"]


DB_OPS_SUMMARY = _row(
    "db_ops :: index_usage summary", "65", "summary",
    "db=db_ops, indexes_total=65, used=43, unused=22, cold=0, disabled=0, disabled_clustered=0, "
    "droppable=22, tables=19, total_size_mb=1815.61, droppable_size_mb=157.65, "
    "counters_since=never reset | scope=this database only")

TEST01_SUMMARY = _row(
    "test01 :: index_usage summary", "1", "summary",
    "db=test01, indexes_total=1, used=0, unused=1, cold=0, disabled=0, disabled_clustered=0, "
    "droppable=0, tables=1, total_size_mb=0.02, droppable_size_mb=0.00, "
    "counters_since=never reset | scope=this database only")


def test_a_servers_totals_are_the_sum_of_its_databases(_fake_store):
    """The PostgreSQL SQL emits no cluster-wide summary, and must not: it is declared
    `per_database`, so the collector runs it once per database and a row calling itself the total
    would be written once per database under the same metric_item. On the PGLAB cluster that was
    three rows each claiming to be the server total, of which the parser keeps whichever arrived
    last — which is how a server with 66 indexes came to report 1."""
    entry = _entry(_fake_store, DB_OPS_SUMMARY, TEST01_SUMMARY, UNUSED)

    assert entry["totals"]["indexes_total"] == 66
    assert entry["totals"]["used"] == 43
    assert entry["totals"]["unused"] == 23
    assert entry["totals"]["droppable"] == 22
    assert entry["totals"]["databases_covered"] == 2


def test_a_sqlserver_servers_own_summary_is_never_overwritten_by_the_sum(_fake_store):
    """SQL Server does emit a cluster-wide summary, because one connection reaches every
    database. Adding its per-database rows on top would double every count."""
    global_summary = _row(
        "index_usage :: summary", "3", "summary",
        "indexes_total=3, used=1, unused=1, cold=1, disabled=0, disabled_clustered=0, "
        "droppable=1, missing_suggestions=0, uptime_days=30.0")
    per_db = _row(
        "SALESDB :: index_usage summary", "3", "summary",
        "db=SALESDB, indexes_total=3, used=1, unused=1, cold=1, disabled=0, disabled_clustered=0, "
        "droppable=1, tables=1")
    entry = _entry(_fake_store, global_summary, per_db)

    assert entry["totals"]["indexes_total"] == 3
    assert "databases_covered" not in entry["totals"]
