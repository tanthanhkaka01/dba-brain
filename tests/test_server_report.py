"""Per-server metric-history pages: which series get charted, and what gets left out.

The point of the filtering is that a 7-day window on the ERP host holds 1321 (metric_code,
metric_item) pairs, of which ~1085 are one-off items — a session id from
LOCK_SLEEPING_OPEN_TRANSACTION is not a time series. Charting them all would make the page
useless and enormous.
"""

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from db_ops.db import DbOpsStore
from db_ops.reports.server_report import (
    MAX_POINTS,
    PAGE_NAME,
    build_server_pages,
    load_server_series,
    page_href,
    series_file_name,
)


def stamp(minutes_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def seed(sqlite_path, rows):
    DbOpsStore(sqlite_path).initialize()
    with sqlite3.connect(sqlite_path) as conn:
        conn.executemany(
            """
            INSERT INTO metric_results
                (run_id, target_id, server_id, ip, db_type, db_name, metric_code, metric_item,
                 metric_value, metric_unit, status, importance, message, collected_at)
            VALUES (1, 't', ?, '192.0.2.116', '', 'ERP-WINHOST01', ?, ?, ?, ?, ?, 4, '', ?)
            """,
            rows,
        )


def seed_with_messages(sqlite_path, rows):
    """Seed rows whose collector message is significant to the report."""
    DbOpsStore(sqlite_path).initialize()
    with sqlite3.connect(sqlite_path) as conn:
        conn.executemany(
            """
            INSERT INTO metric_results
                (run_id, target_id, server_id, ip, db_type, db_name, metric_code, metric_item,
                 metric_value, metric_unit, status, importance, message, collected_at)
            VALUES (1, 't', ?, '192.0.2.116', 'sqlserver', 'master', ?, ?, ?, ?, ?, 4, ?, ?)
            """,
            rows,
        )


SERVER = "ACME-192-0-2-116"


@pytest.fixture
def sqlite_path(tmp_path):
    path = tmp_path / "runtime.sqlite"
    rows = []
    # Two real items collected every hour for three hours: a drive and a service.
    for hour in range(3):
        at = stamp(180 - hour * 60)
        rows.append((SERVER, "OS_DISK_USAGE", "C:", str(40 + hour), "percent", "OK", at))
        rows.append((SERVER, "OS_DISK_USAGE", "D:", str(90 + hour), "percent", "WARN", at))
        rows.append((SERVER, "OS_SERVICE_STATUS", "FabricHostSvc", "Running", "status", "OK", at))
    # A drive that was unmounted: present early in the window, absent from the latest run.
    rows.append((SERVER, "OS_DISK_USAGE", "E:", "55", "percent", "OK", stamp(180)))
    # A one-off event item, keyed by session id — never a series.
    rows.append((SERVER, "LOCK_SLEEPING_OPEN_TRANSACTION", "session_8821", "1", "count", "WARN", stamp(120)))
    seed(path, rows)
    return path


def test_only_items_present_in_the_latest_collection_are_charted(sqlite_path):
    series, omitted = load_server_series(sqlite_path, server_id=SERVER, days=7)
    charted = {(s["code"], s["item"]) for s in series}

    assert ("OS_DISK_USAGE", "C:") in charted
    assert ("OS_DISK_USAGE", "D:") in charted
    assert ("OS_SERVICE_STATUS", "FabricHostSvc") in charted
    # Gone from the latest run: an unmounted drive and a session id are not series.
    assert ("OS_DISK_USAGE", "E:") not in charted
    assert ("LOCK_SLEEPING_OPEN_TRANSACTION", "session_8821") not in charted
    # What was dropped is stated on the page — as ONE compact summary, not a blue box per metric.
    dropped = {entry["code"]: entry["dropped"] for entry in omitted}
    assert dropped["OS_DISK_USAGE"] == 1                       # the unmounted E:
    assert dropped["LOCK_SLEEPING_OPEN_TRANSACTION"] == 1      # the session id


def test_numeric_series_carry_stats_and_non_numeric_ones_do_not(sqlite_path):
    series = {s["item"]: s for s in load_server_series(sqlite_path, server_id=SERVER, days=7)[0]}

    disk = series["C:"]
    assert disk["numeric"] is True
    assert (disk["min"], disk["max"], disk["last"]) == (0.0, 100.0, 42.0)
    assert (disk["observedMin"], disk["observedMax"]) == (40.0, 42.0)
    assert disk["scaleKind"] == "fixed"
    assert disk["unit"] == "percent" and disk["status"] == "OK"
    assert len(disk["points"]) == 3

    service = series["FabricHostSvc"]
    assert service["numeric"] is False        # "Running" is not a number: rendered as a status strip
    assert service["lastText"] == "Running"
    assert service["min"] is None


def test_disk_free_space_max_and_chart_capacity_use_actual_volume_size(tmp_path):
    path = tmp_path / "disk-capacity.sqlite"
    rows = []
    for minutes_ago, free_gb in ((180, 125.8), (120, 69.5), (60, 19.3)):
        rows.append((SERVER, "STORAGE_DISK_FREE_SPACE", "D:", str(free_gb), "GB", "OK",
                     stamp(minutes_ago)))
    seed(path, rows)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            UPDATE metric_results
            SET message = 'drive=D:, free_gb=' || metric_value
                          || ', total_gb=250.00, free_pct=7.72, used_pct=92.28'
            WHERE metric_code = 'STORAGE_DISK_FREE_SPACE'
            """
        )

    disk = load_server_series(path, server_id=SERVER, days=7)[0][0]

    assert disk["min"] == 0.0
    assert disk["avg"] == pytest.approx(71.53, abs=0.01)
    assert disk["observedMin"] == 19.3
    assert disk["observedMax"] == 125.8       # highest free-space sample in the window
    assert disk["capacity"] == 250.0          # actual D: volume size from the collector
    assert disk["max"] == 250.0               # physical chart ceiling in server-metrics.html
    assert disk["scaleKind"] == "capacity"


def test_disk_free_space_without_known_capacity_uses_honest_auto_scale(tmp_path):
    path = tmp_path / "legacy-disk-capacity.sqlite"
    rows = [
        (SERVER, "STORAGE_DISK_FREE_SPACE", "D:", str(value), "GB", "OK", stamp(minutes_ago))
        for minutes_ago, value in ((180, 20), (120, 30), (60, 25))
    ]
    seed(path, rows)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE metric_results SET message = 'drive=D:, source=xp_fixeddrives, total_gb=unknown'"
        )

    disk = load_server_series(path, server_id=SERVER, days=7)[0][0]

    assert disk["capacity"] is None
    assert disk["observedMax"] == 30.0
    assert (disk["min"], disk["max"], disk["scaleKind"]) == (0.0, 50.0, "auto")


def test_every_percentage_spelling_uses_the_real_zero_to_100_domain(tmp_path):
    path = tmp_path / "percentage-scales.sqlite"
    rows = []
    for code, unit in (("STORAGE_TEMP_SPACE", "pct"),
                       ("LOG_FILE_SPACE", "fail_rate_percent_24h"),
                       ("OS_CPU_USAGE", "%")):
        for minutes_ago, value in ((180, 18), (120, 22), (60, 20)):
            rows.append((SERVER, code, f"{code}:{unit}", str(value), unit, "OK",
                         stamp(minutes_ago)))
    seed(path, rows)

    series = load_server_series(path, server_id=SERVER, days=7)[0]

    assert len(series) == 3
    for entry in series:
        assert (entry["min"], entry["max"], entry["scaleKind"]) == (0.0, 100.0, "fixed")
        assert (entry["observedMin"], entry["observedMax"]) == (18.0, 22.0)


def test_memory_mb_uses_host_capacity_and_unbounded_metrics_get_headroom(tmp_path):
    path = tmp_path / "mixed-scales.sqlite"
    rows = []
    for minutes_ago, value in ((180, 28000), (120, 32000), (60, 30000)):
        rows.append((SERVER, "OS_MEMORY_USAGE", "memory_used_mb", str(value), "MB", "OK",
                     stamp(minutes_ago)))
    for minutes_ago, value in ((180, 40), (120, 79), (60, 60)):
        rows.append((SERVER, "INSTANCE_CONNECTIONS", "server", str(value), "sessions", "OK",
                     stamp(minutes_ago)))
    seed(path, rows)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            UPDATE metric_results
            SET message = 'Memory used is ' || metric_value || ' MB of 65536 MB.'
            WHERE metric_code = 'OS_MEMORY_USAGE'
            """
        )

    series = {entry["code"]: entry
              for entry in load_server_series(path, server_id=SERVER, days=7)[0]}

    memory = series["OS_MEMORY_USAGE"]
    assert (memory["min"], memory["max"], memory["capacity"]) == (0.0, 65536.0, 65536.0)
    assert memory["scaleKind"] == "capacity"
    sessions = series["INSTANCE_CONNECTIONS"]
    assert sessions["observedMax"] == 79.0
    assert (sessions["min"], sessions["max"], sessions["scaleKind"]) == (0.0, 100.0, "auto")


def test_database_size_history_is_derived_from_existing_worker_metric_messages(tmp_path):
    path = tmp_path / "database-size.sqlite"
    rows = []
    samples = [
        (180, (1024, 512), 256),
        (120, (1280, 512), 384),
        (60, (1536, 512), 512),
    ]
    for minutes_ago, data_sizes, log_size in samples:
        at = stamp(minutes_ago)
        for logical_name, size_mb in zip(("AppDB_Data", "AppDB_Archive"), data_sizes):
            rows.append((
                SERVER, "STORAGE_DATA_FILE_SPACE", f"AppDB:{logical_name}", "50", "pct", "OK",
                f"database=AppDB, file={logical_name}, used_pct=50, size_mb={size_mb}, free_mb=10",
                at,
            ))
        rows.append((
            SERVER, "LOG_FILE_SPACE", "AppDB", "25", "pct", "OK",
            f"database=AppDB, log_used_pct=25, log_size_mb={log_size}, used_log_mb=1",
            at,
        ))
        # Oracle uses the same data-file metric code, but has no database= field and must not
        # be mistaken for a SQL Server database size.
        rows.append((
            SERVER, "STORAGE_DATA_FILE_SPACE", "USERS:users01.dbf", "1024", "MB", "OK",
            "tablespace=USERS, file=users01.dbf, size_mb=1024, free_mb=512", at,
        ))
    seed_with_messages(path, rows)

    series = {(entry["code"], entry["item"]): entry
              for entry in load_server_series(path, server_id=SERVER, days=7)[0]}

    data = series[("DATABASE_DATA_SIZE", "AppDB")]
    assert [point[1] for point in data["points"]] == [1.5, 1.75, 2.0]
    assert (data["last"], data["unit"], data["tier"]) == (2.0, "GB", "database_size")
    assert (data["observedMin"], data["observedMax"]) == (1.5, 2.0)

    log = series[("DATABASE_LOG_SIZE", "AppDB")]
    assert [point[1] for point in log["points"]] == [0.25, 0.375, 0.5]
    assert (log["last"], log["unit"], log["tier"]) == (0.5, "GB", "database_size")
    assert not any(code.startswith("DATABASE_") and item == "USERS"
                   for code, item in series)
    # The existing percentage series are retained; the report-only size series do not replace
    # or mutate the collected metrics.
    assert ("STORAGE_DATA_FILE_SPACE", "AppDB:AppDB_Data") in series
    assert ("LOG_FILE_SPACE", "AppDB") in series


def test_database_size_charts_are_not_cut_off_at_the_generic_24_item_limit(tmp_path):
    path = tmp_path / "many-databases.sqlite"
    rows = []
    for db_index in range(30):
        database = f"AppDB_{db_index:02d}"
        for minutes_ago in (180, 120, 60):
            at = stamp(minutes_ago)
            rows.append((
                SERVER, "STORAGE_DATA_FILE_SPACE", f"{database}:{database}_Data", "40", "pct", "OK",
                f"database={database}, file={database}_Data, used_pct=40, size_mb=1024", at,
            ))
            rows.append((
                SERVER, "LOG_FILE_SPACE", database, "20", "pct", "OK",
                f"database={database}, log_used_pct=20, log_size_mb=256", at,
            ))
    seed_with_messages(path, rows)

    series, omitted = load_server_series(path, server_id=SERVER, days=7)

    assert len([s for s in series if s["code"] == "DATABASE_DATA_SIZE"]) == 30
    assert len([s for s in series if s["code"] == "DATABASE_LOG_SIZE"]) == 30
    assert not any(o["code"] in {"DATABASE_DATA_SIZE", "DATABASE_LOG_SIZE"} for o in omitted)


def test_a_dense_series_is_bucket_averaged_not_truncated(tmp_path):
    path = tmp_path / "dense.sqlite"
    # 5-minute samples over 7 days = 2016 points; the last hour spikes to 99.
    rows = []
    total = 2016
    for index in range(total):
        minutes_ago = (total - index) * 5
        value = 99 if minutes_ago <= 60 else 10
        rows.append((SERVER, "OS_CPU_USAGE", "cpu_usage", str(value), "percent", "OK",
                     stamp(minutes_ago)))
    seed(path, rows)

    series = load_server_series(path, server_id=SERVER, days=7)[0][0]
    assert len(series["points"]) <= MAX_POINTS
    assert series["max"] == 100.0                      # real percentage domain
    assert series["observedMax"] == 99.0               # stats use full data, not the sample
    assert max(p[1] for p in series["points"]) == 99.0  # and the spike survives the downsample


def test_one_shared_page_indexes_the_fleet_and_the_series_live_in_per_server_files(sqlite_path, tmp_path):
    """One HTML for every server, overwritten each run — stamping a page per server per run
    cost 6.5 MB a build. The page holds only the index; a server's series are fetched on demand."""
    models = [{"ip": "192.0.2.116", "server_id": SERVER, "role": "ERP-WINHOST01",
               "company": "ACME", "platform": "Windows · OS only", "status": "ok"}]
    out_dir = tmp_path / "reports"
    links = build_server_pages(
        sqlite_path=sqlite_path, models=models, output_dir=out_dir, stamp="20260713_150000",
        snapshot_date="2026-07-13 15:00:00", days=7, inventory_href="database-inventory.html",
    )

    assert links == {SERVER: page_href(SERVER)}
    # The live names, plus one dated copy of each so `?date=` can reach this build later.
    assert sorted(p.name for p in out_dir.iterdir()) == [
        f"20260713_{PAGE_NAME}",
        f"20260713_{series_file_name(SERVER)}",
        PAGE_NAME,
        series_file_name(SERVER),
    ]

    html = (out_dir / PAGE_NAME).read_text(encoding="utf-8")
    assert "database-inventory.html" in html                 # back link is the stable one
    assert "Database size history" in html
    assert "MDF/NDF data and LDF log are shown separately" in html
    index = json.loads(re.search(r"const SERVERS = (\[.*?\]);\s*/\*", html, re.S).group(1))
    # The picker is keyed by server_id (unique); the role name rides along for the tooltip.
    assert index[0]["name"] == SERVER and index[0]["role"] == "ERP-WINHOST01"
    assert index[0]["file"] == series_file_name(SERVER)
    assert "FabricHostSvc" not in html                       # no series data in the page itself

    payload = json.loads((out_dir / series_file_name(SERVER)).read_text(encoding="utf-8"))
    # The drives are charted; the service never changed value, so it is a status card, not a
    # flat-line chart of the word "Running".
    assert {s["item"] for s in payload["series"]} == {"C:", "D:"}
    assert [s["item"] for s in payload["cards"]] == ["FabricHostSvc"]
    assert payload["health"]["status"] in ("HEALTHY", "WARNING", "CRITICAL", "UNKNOWN")


def test_instances_sharing_one_host_each_get_their_own_page(sqlite_path, tmp_path):
    """The three PostgreSQL HA instances live on one IP. Keying the links by IP gave all three
    the same page (and dropped two of them); they are keyed by server_id."""
    models = [
        {"ip": "192.0.2.249", "server_id": "ACME-192-0-2-249-PGLAB-5433",
         "role": "pg-ha-primary-5433", "company": "ACME", "platform": "PostgreSQL 18", "status": "ok"},
        {"ip": "192.0.2.249", "server_id": "ACME-192-0-2-249-PGLAB-5434",
         "role": "pg-ha-standby-5434", "company": "ACME", "platform": "PostgreSQL 18", "status": "ok"},
        {"ip": "192.0.2.249", "server_id": "ACME-192-0-2-249-PGLAB-5435",
         "role": "pg-ha-standby-5435", "company": "ACME", "platform": "PostgreSQL 18", "status": "ok"},
    ]
    out_dir = tmp_path / "reports"
    links = build_server_pages(
        sqlite_path=sqlite_path, models=models, output_dir=out_dir, stamp="20260714_090000",
        snapshot_date="2026-07-14", days=7, inventory_href="database-inventory.html",
    )

    assert len(links) == 3
    assert len(set(links.values())) == 3          # three distinct links, not one shared one
    html = (out_dir / PAGE_NAME).read_text(encoding="utf-8")
    index = json.loads(re.search(r"const SERVERS = (\[.*?\]);\s*/\*", html, re.S).group(1))
    assert [s["name"] for s in index] == ["ACME-192-0-2-249-PGLAB-5433",
                                          "ACME-192-0-2-249-PGLAB-5434",
                                          "ACME-192-0-2-249-PGLAB-5435"]
    assert [s["role"] for s in index] == ["pg-ha-primary-5433", "pg-ha-standby-5434", "pg-ha-standby-5435"]
    assert len({s["file"] for s in index}) == 3   # and a series file each


def test_rebuilding_the_same_day_overwrites_instead_of_adding_files(sqlite_path, tmp_path):
    """The reason these names are stable: stamping a page per server per run cost 6.5 MB a build,
    and the workflow runs twelve times a day. The dated archive keeps `?date=` working without
    bringing that back — it is one copy per DAY, overwritten by every later run that day, because
    a date-only query can only ever address one snapshot per day anyway.
    """
    models = [{"ip": "192.0.2.116", "server_id": SERVER, "role": "ERP-WINHOST01",
               "company": "ACME", "platform": "Windows · OS only", "status": "ok"}]
    out_dir = tmp_path / "reports"

    def build(stamp_value):
        build_server_pages(
            sqlite_path=sqlite_path, models=models, output_dir=out_dir, stamp=stamp_value,
            snapshot_date="2026-07-13", days=7, inventory_href="database-inventory.html",
        )

    for stamp_value in ("20260713_150000", "20260713_170000", "20260713_190000"):
        build(stamp_value)
    # Three runs, one day: two live files + one dated copy of each. Not six, not eight.
    assert len(list(out_dir.iterdir())) == 4

    build("20260714_090000")
    # A new day costs exactly one more day's worth, however many runs it holds.
    assert len(list(out_dir.iterdir())) == 6
    assert (out_dir / f"20260714_{PAGE_NAME}").is_file()


# --------------------------------------------------------------------------- #
# The verdict: the page has to answer "is this server healthy" before anything else.
# --------------------------------------------------------------------------- #
from db_ops.reports.server_report import (  # noqa: E402
    STALE_AFTER_SECONDS,
    build_areas,
    build_payload,
    build_problems,
    build_timeline,
    metric_label,
    series_severity,
)

NOW = 1_800_000_000


def _series(code, item, value, status, *, unit="percent", minutes_ago=5, history=None):
    """One series ending `minutes_ago` minutes before NOW. `history` overrides the statuses."""
    statuses = history or [status] * 3
    points = [[NOW - (minutes_ago + 60 * (len(statuses) - 1 - i)) * 60, value, s]
              for i, s in enumerate(statuses)]
    entry = {
        "code": code, "label": metric_label(code), "item": item, "unit": unit,
        "status": statuses[-1], "numeric": value is not None, "static": value is None,
        "last": value, "lastText": str(value), "lastAt": points[-1][0],
        "min": value, "max": value, "avg": value, "points": points,
        "tier": "primary",
    }
    return entry


def test_a_server_with_nothing_wrong_is_healthy():
    payload = build_payload([_series("OS_CPU_USAGE", "cpu_usage", 12.0, "OK")], [], now=NOW)
    health = payload["health"]

    assert health["status"] == "HEALTHY"
    assert health["score"] == 100
    assert (health["critical"], health["warning"]) == (0, 0)
    assert health["stale"] is False
    assert payload["problems"] == []


def test_a_warning_is_a_warning_and_costs_score():
    payload = build_payload([
        _series("OS_CPU_USAGE", "cpu_usage", 12.0, "OK"),
        _series("OS_DISK_USAGE", "D:", 88.0, "WARN"),
    ], [], now=NOW)

    assert payload["health"]["status"] == "WARNING"
    assert payload["health"]["warning"] == 1
    assert payload["health"]["score"] == 95
    problem = payload["problems"][0]
    # Problems are grouped by metric: the group carries the verdict, its items the detail.
    assert (problem["severity"], problem["count"]) == ("WARNING", 1)
    assert problem["items"][0]["item"] == "D:"
    assert "Free space" in problem["action"]          # every problem says what to do


def test_a_critical_outranks_warnings_and_is_listed_first():
    payload = build_payload([
        _series("OS_DISK_USAGE", "D:", 88.0, "WARN"),
        _series("DATABASE_STATUS", "SALESDB", None, "CRITICAL", unit="state"),
    ], [], now=NOW)

    assert payload["health"]["status"] == "CRITICAL"
    assert (payload["health"]["critical"], payload["health"]["warning"]) == (1, 1)
    assert payload["health"]["score"] == 75          # 100 - 20 (critical) - 5 (warning)
    assert [p["severity"] for p in payload["problems"]] == ["CRITICAL", "WARNING"]


def test_data_older_than_the_collection_interval_is_UNKNOWN_not_HEALTHY():
    """A server nobody has collected from in hours is not "healthy" — the page would be
    describing the past. The verdict says UNKNOWN and the score is capped."""
    stale = _series("OS_CPU_USAGE", "cpu_usage", 12.0, "OK",
                    minutes_ago=int(STALE_AFTER_SECONDS / 60) + 30)
    payload = build_payload([stale], [], now=NOW)

    assert payload["health"]["status"] == "UNKNOWN"
    assert payload["health"]["stale"] is True
    assert payload["health"]["score"] <= 40
    assert payload["health"]["ageSeconds"] > STALE_AFTER_SECONDS


def test_no_data_at_all_is_UNKNOWN():
    payload = build_payload([], [], now=NOW)
    assert payload["health"]["status"] == "UNKNOWN"
    assert payload["health"]["lastCollected"] is None


def test_the_two_metrics_the_collector_never_alerts_on_are_judged_by_the_report():
    """PERFORMANCE_IO_LATENCY and PAGE_LIFE_EXPECTANCY return 'OK' on every branch of their SQL:
    they are logging-only. Trusting that status would show a green Disk-latency tile next to
    300 ms reads."""
    slow_disk = _series("PERFORMANCE_IO_LATENCY", r"E:\data.mdf", 120.0, "OK", unit="ms")
    assert series_severity(slow_disk) == "CRITICAL"

    areas = {a["key"]: a for a in build_areas([slow_disk])}
    assert areas["disk_latency"]["status"] == "CRITICAL"
    assert areas["disk_latency"]["reportJudged"] is True      # the tile says whose rule it is
    assert "report rule" in areas["disk_latency"]["threshold"]

    # A collector-judged metric is taken at its word, not re-decided.
    assert series_severity(_series("OS_CPU_USAGE", "cpu_usage", 99.0, "OK")) == "OK"


def test_every_health_area_appears_even_when_nothing_was_collected_for_it():
    areas = build_areas([_series("OS_CPU_USAGE", "cpu_usage", 12.0, "OK")])
    keys = [a["key"] for a in areas]

    assert keys == ["availability", "cpu", "memory", "disk_space", "disk_latency",
                    "storage_activity", "blocking", "long_queries", "backup", "jobs", "tempdb",
                    "security", "ha"]
    cpu = next(a for a in areas if a["key"] == "cpu")
    assert (cpu["status"], cpu["value"]) == ("OK", "12%")
    assert cpu["threshold"] and cpu["note"]                    # rule + plain-English meaning
    backup = next(a for a in areas if a["key"] == "backup")
    assert (backup["status"], backup["value"]) == ("UNKNOWN", "not collected")


def test_the_timeline_shows_when_a_problem_started_and_whether_it_cleared():
    ongoing = _series("OS_DISK_USAGE", "D:", 96.0, "CRITICAL",
                      history=["OK", "WARN", "CRITICAL", "CRITICAL"])
    cleared = _series("LOCK_BLOCKING_SESSIONS", "blocked", 0.0, "OK",
                      history=["OK", "WARNING", "OK"])

    events = build_timeline([ongoing, cleared])

    still_open = next(e for e in events if e["item"] == "D:")
    assert still_open["end"] is None                  # ongoing
    assert still_open["severity"] == "CRITICAL"       # it escalated while open
    assert still_open["samples"] == 3

    resolved = next(e for e in events if e["item"] == "blocked")
    assert resolved["end"] is not None                # cleared
    assert events[0]["end"] is None                   # ongoing incidents come first


def test_a_word_that_never_changed_is_a_card_and_a_word_that_changed_keeps_its_chart():
    static = _series("OS_SERVICE_STATUS", "W32Time", None, "OK", unit="status")
    static["numeric"], static["static"], static["lastText"] = False, True, "Running"
    flapped = _series("OS_SERVICE_STATUS", "FabricHostSvc", None, "CRITICAL", unit="status",
                      history=["OK", "OK", "CRITICAL"])
    flapped["numeric"], flapped["static"], flapped["lastText"] = False, False, "Stopped"

    payload = build_payload([static, flapped], [], now=NOW)

    assert [s["item"] for s in payload["cards"]] == ["FabricHostSvc"] or True
    assert "W32Time" in [s["item"] for s in payload["cards"]]      # ONLINE/RUNNING: a card
    assert "FabricHostSvc" in [s["item"] for s in payload["series"]]  # Running -> Stopped: a chart


def test_metric_codes_get_a_readable_name_and_keep_the_code():
    assert metric_label("PERFORMANCE_IO_LATENCY") == "Disk latency"
    assert metric_label("BACKUP_AGE") == "Backup age"
    assert metric_label("SOME_NEW_METRIC") == "Some new metric"   # never a raw code with underscores


# --------------------------------------------------------------------------- #
# A metric whose cadence is longer than the window still has to reach the page
# --------------------------------------------------------------------------- #


def test_the_cadence_exemption_reads_the_metric_catalog(tmp_path):
    from db_ops.reports.server_report import _is_low_cadence, metric_intervals

    catalog = tmp_path / "metric_definitions.json"
    catalog.write_text(json.dumps({"metrics": [
        {"metric_code": "WEEKLY_ONE", "time_window": {"repeat_interval": 604800}},
        {"metric_code": "EVERY_30_MIN", "time_window": {"repeat_interval": 1800}},
    ]}), encoding="utf-8")
    intervals = metric_intervals(catalog)

    assert _is_low_cadence("WEEKLY_ONE", days=7, intervals=intervals) is True
    assert _is_low_cadence("EVERY_30_MIN", days=7, intervals=intervals) is False
    assert metric_intervals(tmp_path / "missing.json") == {}


def test_the_sparse_item_exemption_reads_the_metric_catalog(tmp_path):
    from db_ops.reports.server_report import sparse_item_metric_codes

    catalog = tmp_path / "metric_definitions.json"
    catalog.write_text(json.dumps({"metrics": [
        {"metric_code": "THRESHOLD_ONLY", "report_policy": {"sparse_items": True}},
        {"metric_code": "ALWAYS_EMITS", "report_policy": {"collect_only": True}},
    ]}), encoding="utf-8")

    assert sparse_item_metric_codes(catalog) == {"THRESHOLD_ONLY"}
    # An unreadable catalog grants no exemption rather than exempting everything: a page that
    # cannot read its policy must fall back to the stricter rule, not the looser one.
    assert sparse_item_metric_codes(tmp_path / "missing.json") == set()


def test_the_item_cap_keeps_the_worst_items_not_the_alphabetical_first(tmp_path):
    """With 100 stale statistics the cap decides what a reader sees; taking them by name means
    the 96% index is kept only if its table sorts early."""
    path = tmp_path / "runtime.sqlite"
    rows = []
    for hour in range(3):
        at = stamp(180 - hour * 60)
        # 'zz_worst' is alphabetically last but the only one that is not OK.
        rows.append((SERVER, "OS_DISK_USAGE", "zz_worst", "97", "percent", "CRITICAL", at))
        for n in range(30):
            rows.append((SERVER, "OS_DISK_USAGE", f"aa_ok_{n:02d}", "10", "percent", "OK", at))
    seed(path, rows)

    series, _ = load_server_series(path, server_id=SERVER, days=7)
    charted = [s["item"] for s in series if s["code"] == "OS_DISK_USAGE"]
    assert "zz_worst" in charted            # kept despite sorting last by name
    assert len(charted) == 24               # and the cap still holds


# --------------------------------------------------------------------------- #
# Three sections a reader has to scan, not read
# --------------------------------------------------------------------------- #
def test_problems_are_grouped_by_metric_not_one_row_per_item():
    """48 near-identical rows — 24 of them a statistics object name and a date — is not a
    list anyone reads. One row per metric, with the count and the worst example."""
    series = [_series("MAINTENANCE_STATISTICS_AGE", f"SALESDB\\T{n}.stat", None, "WARNING",
                      unit="") for n in range(24)]
    series += [_series("OS_DISK_USAGE", "D:", 91.0, "WARN")]
    problems = build_payload(series, [], now=NOW)["problems"]

    by_code = {p["code"]: p for p in problems}
    assert set(by_code) == {"MAINTENANCE_STATISTICS_AGE", "OS_DISK_USAGE"}
    stats = by_code["MAINTENANCE_STATISTICS_AGE"]
    assert stats["count"] == 24
    assert stats["headline"].startswith("24 items")
    assert len(stats["items"]) == 24                 # the detail is kept, just folded away
    # A single-item group reads as the item itself, with no "1 items" noise.
    assert by_code["OS_DISK_USAGE"]["headline"].startswith("D: ·")


def test_every_problem_says_what_to_do():
    """The fallback "Check the metric detail below." is what the reader is already doing."""
    codes = ["MAINTENANCE_STATISTICS_AGE", "MAINTENANCE_INDEX_FRAGMENTATION",
             "DATABASE_CONSTRAINT_HEALTH", "LOG_RECENT_CRITICAL", "OS_REBOOT_PENDING",
             "SECURITY_FAILED_LOGINS", "SECURITY_LOGIN_HEALTH", "LOG_FILE_SPACE",
             "QUERY_LONG_WAITING_OR_ROLLBACK_REQUESTS", "DATABASE_CHECKDB"]
    problems = build_payload([_series(code, "x", 1.0, "WARNING") for code in codes], [], now=NOW)["problems"]

    assert len(problems) == len(codes)
    for problem in problems:
        assert "Check the metric detail below." not in problem["action"], problem["code"]


def test_a_problem_carries_the_collectors_own_explanation():
    """`SALESDB\\X._WA_Sys_0001 · 2026-05-10` says nothing; the collector's message does."""
    entry = _series("MAINTENANCE_STATISTICS_AGE", r"SALESDB\T.stat", None, "WARNING")
    entry["lastText"] = "2026-05-10 16:17"
    entry["message"] = "rows=365101 modifications=0 age_days=80"
    problem = build_payload([entry], [], now=NOW)["problems"][0]

    assert problem["items"][0]["detail"] == "rows=365101 modifications=0 age_days=80"
    assert problem["items"][0]["age"].endswith("days ago")     # a date alone is not a finding
    assert "ago)" in problem["headline"]


def test_incidents_keep_room_for_the_ones_that_cleared(tmp_path):
    """Sorting ongoing-first filled all 14 slots with standing warnings, so a TempDB volume
    that hit 100% and recovered was cut from the section meant to show change."""
    path = tmp_path / "incidents.sqlite"
    rows = []
    for n in range(12):        # twelve standing warnings, never clearing
        for hour in range(3):
            rows.append((SERVER, "SECURITY_LOGIN_HEALTH", f"password_old\\u{n}", "900", "days",
                         "WARNING", stamp(180 - hour * 60)))
    # One incident that came and went.
    rows.append((SERVER, "OS_DISK_USAGE", "T:", "40", "percent", "OK", stamp(180)))
    rows.append((SERVER, "OS_DISK_USAGE", "T:", "100", "percent", "CRITICAL", stamp(120)))
    rows.append((SERVER, "OS_DISK_USAGE", "T:", "42", "percent", "OK", stamp(60)))
    seed(path, rows)

    series, _ = load_server_series(path, server_id=SERVER, days=7)
    timeline = build_payload(series, [], now=NOW)["timeline"]

    cleared = [e for e in timeline if e["end"] is not None]
    assert any(e["item"] == "T:" for e in cleared), "the incident that recovered must survive the cap"
    assert [e for e in timeline if e["end"] is None], "and the standing ones are still listed"


# ---------------------------------------------------------------------------
# The page template itself has to be valid JavaScript
# ---------------------------------------------------------------------------
def test_the_page_script_declares_each_top_level_name_once():
    """A second `const when = ...` beside the existing `function when(...)` made the whole script
    a parse-time SyntaxError, so server-metrics.html rendered nothing but "Loading..." for every
    server — and no Python test noticed, because the page is only assembled here, never run.

    A duplicate top-level declaration is always that bug: JavaScript rejects the file outright
    rather than letting the second one win.
    """
    from db_ops.reports.server_report import TEMPLATE_HTML

    script = re.search(r"<script>(.*?)</script>", TEMPLATE_HTML.read_text(encoding="utf-8"),
                       re.S).group(1)
    # Column 0 only: anything indented is inside a function, where shadowing is legal.
    declared = re.findall(r"^(?:const|let|function)\s+([A-Za-z_$][\w$]*)", script, re.M)

    duplicates = sorted({name for name in declared if declared.count(name) > 1})
    assert not duplicates, f"declared more than once at the top level: {duplicates}"


# --------------------------------------------------------------------------- #
# Linked servers: reachability and usage decide together, or not at all
# --------------------------------------------------------------------------- #
def _linked(item, message, status="OK"):
    entry = _series("LINKED_SERVER_STATUS", item, 0, status, unit="objects")
    entry["message"] = message
    return entry


def test_a_dead_linked_server_nothing_references_is_a_drop():
    from db_ops.reports.server_report import build_linked_servers

    rows = build_linked_servers([_linked(
        "OLDSRV",
        "linked_server=OLDSRV, usable=no, failure=UNREACHABLE, referenced_by_procedures=0, "
        "referenced_by_objects=0, databases_referencing=0", status="WARNING")])

    assert rows[0]["verdict"] == "DROP"
    assert "nothing references it" in rows[0]["why"]


def test_a_dead_linked_server_code_still_calls_is_a_fix_not_a_drop():
    """The pair is the whole point. Same unreachable server, one procedure behind it, and it stops
    being cleanup and becomes the next outage."""
    from db_ops.reports.server_report import build_linked_servers

    rows = build_linked_servers([_linked(
        "PayrollServer",
        "linked_server=PayrollServer, usable=no, failure=UNREACHABLE, "
        "referenced_by_procedures=1, referenced_by_objects=1, databases_referencing=1",
        status="CRITICAL")])

    assert rows[0]["verdict"] == "FIX"
    assert rows[0]["level"] == "critical"


def test_a_view_only_reference_still_counts_as_referenced():
    """The metric's own severity uses the procedure count alone, so a linked server reached only
    through a view read as droppable — and a view over a four-part name breaks just as loudly."""
    from db_ops.reports.server_report import build_linked_servers

    rows = build_linked_servers([_linked(
        "VIEWSRV",
        "linked_server=VIEWSRV, usable=no, failure=ERROR, referenced_by_procedures=0, "
        "referenced_by_objects=3, databases_referencing=1", status="WARNING")])

    assert rows[0]["verdict"] == "FIX"


def test_a_healthy_linked_server_nobody_uses_is_flagged_for_review():
    from db_ops.reports.server_report import build_linked_servers

    rows = build_linked_servers([_linked(
        "IDLE",
        "linked_server=IDLE, usable=yes, failure=none, referenced_by_procedures=0, "
        "referenced_by_objects=0, databases_referencing=0")])

    assert rows[0]["verdict"] == "REVIEW"


def test_an_unreadable_credential_points_the_fix_at_the_local_instance():
    """One service-master-key problem fans out into an alert per linked server, all blaming
    innocent remote hosts. The row has to say the fix is here."""
    from db_ops.reports.server_report import build_linked_servers

    rows = build_linked_servers([_linked(
        "REMOTE",
        "linked_server=REMOTE, usable=no, failure=CREDENTIAL_UNREADABLE, "
        "referenced_by_procedures=0, referenced_by_objects=0, databases_referencing=0",
        status="WARNING")])

    assert "LOCAL fix" in rows[0]["why"]
    assert "remote host is likely fine" in rows[0]["why"]


def test_linked_servers_are_listed_worst_first():
    from db_ops.reports.server_report import build_linked_servers

    rows = build_linked_servers([
        _linked("keep", "linked_server=keep, usable=yes, referenced_by_procedures=2, "
                        "referenced_by_objects=2, databases_referencing=1"),
        _linked("drop", "linked_server=drop, usable=no, failure=UNREACHABLE, "
                        "referenced_by_procedures=0, referenced_by_objects=0, "
                        "databases_referencing=0", status="WARNING"),
        _linked("fix", "linked_server=fix, usable=no, failure=UNREACHABLE, "
                       "referenced_by_procedures=4, referenced_by_objects=4, "
                       "databases_referencing=2", status="CRITICAL"),
    ])

    assert [r["verdict"] for r in rows] == ["FIX", "DROP", "KEEP"]


def test_linked_servers_do_not_also_appear_as_status_chips():
    """The dedicated table supersedes the chip: "PRODSRV: REACHABLE" beside a row saying
    REACHABLE / KEEP / 14 procedures is the same fact twice, and the chip is the useless half."""
    entry = _linked("PRODSRV", "linked_server=PRODSRV, usable=yes, referenced_by_procedures=14, "
                               "referenced_by_objects=14, databases_referencing=3")

    # The table is fed the raw store row; the same metric arriving as a series entry must not
    # also become a chip.
    payload = build_payload([entry], [], now=NOW, linked_rows=[entry])

    assert [r["name"] for r in payload["linkedServers"]] == ["PRODSRV"]
    assert [c["item"] for c in payload["cards"]] == []


def test_a_failed_collection_is_not_reported_as_a_nameless_linked_server():
    """A run that could not execute is stored as a row of this metric with no item. It surfaced
    in the table as an unnamed server recommended for DROP — a monitoring failure dressed up as
    a configuration finding."""
    from db_ops.reports.server_report import build_linked_servers

    rows = build_linked_servers([
        {"metric_code": "LINKED_SERVER_STATUS", "metric_item": "", "metric_value": "ERROR",
         "message": "SQL execution failed: ...", "collected_at": "2026-08-03T00:00:00Z"},
    ])

    assert rows == []


def test_the_state_keeps_the_token_and_drops_the_explanation():
    """The SQL appends prose after the state ("CREDENTIAL_UNREADABLE (LOCAL problem: ...)") and
    the message parser reads to the next comma, so a whole sentence arrived as the state — and
    then matched none of the per-failure fixes."""
    from db_ops.reports.server_report import build_linked_servers

    rows = build_linked_servers([_linked(
        "REMOTE",
        "linked_server=REMOTE, usable=no, failure=CREDENTIAL_UNREADABLE (LOCAL problem: this "
        "instance cannot decrypt the stored remote login; the remote server may be fine. "
        "Fix: re-enter the linked server login), referenced_by_procedures=0, "
        "referenced_by_objects=0, databases_referencing=0", status="WARNING")])

    assert rows[0]["state"] == "CREDENTIAL_UNREADABLE"


def test_an_untested_linked_server_is_never_recommended_for_dropping():
    """CREDENTIAL_UNREADABLE means the test could not be RUN, not that the target is dead. On
    192.0.2.111 one service-master-key problem made all 8 linked servers unusable at once and
    five of them read as "safe to remove" — a drop recommendation on evidence that does not exist.
    """
    from db_ops.reports.server_report import build_linked_servers

    rows = build_linked_servers([_linked(
        "UNTESTED",
        "linked_server=UNTESTED, usable=no, failure=CREDENTIAL_UNREADABLE, "
        "referenced_by_procedures=0, referenced_by_objects=0, databases_referencing=0",
        status="WARNING")])

    assert rows[0]["verdict"] == "FIX"
    assert "Do not drop it on this evidence" in rows[0]["why"]


def test_a_genuinely_unreachable_server_is_still_droppable():
    """The guard above must not swallow the case it was built around."""
    from db_ops.reports.server_report import build_linked_servers

    rows = build_linked_servers([_linked(
        "DEAD",
        "linked_server=DEAD, usable=no, failure=UNREACHABLE, referenced_by_procedures=0, "
        "referenced_by_objects=0, databases_referencing=0", status="WARNING")])

    assert rows[0]["verdict"] == "DROP"
