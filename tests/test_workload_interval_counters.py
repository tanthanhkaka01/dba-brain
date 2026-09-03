""""How much did this instance do in the last hour" — answered from cumulative counters.

Until these collectors existed the estate could not answer it at all. `sys.dm_exec_query_stats`,
`sys.dm_io_virtual_file_stats`, `sys.dm_os_performance_counters` and `sys.dm_os_wait_stats` all
report totals since the engine started, so a single reading answers "since it booted" — on
one production instance that was nine months — and the one metric that did difference them,
PERFORMANCE_WAIT_STATS, samples five seconds out of every 150 and therefore cannot see an hour.

So three collectors now record the totals raw and grade nothing, and this module subtracts two
collections. The tests below are about the arithmetic being *readable* rather than merely correct:
an hour that is really 15 minutes, a percentage taken without the core count, a plan-cache total
presented as the instance's, and a zero that means "did not collect" are all numbers that look
right on the page and send a DBA the wrong way.
"""

from db_ops.reports import workload


def _rows(code, stamp, values, *, since="2026-08-01 03:00:00", cpu_count=8, extra=""):
    """One collection of one metric: the store shape, with the message the collector writes."""
    return [
        {
            "metric_code": code,
            "metric_item": item,
            "metric_value": str(value),
            "collected_at": stamp,
            "message": (f"value={value}, counters_since={since}, cpu_count={cpu_count}"
                        + (f", {extra}" if extra else "")),
        }
        for item, value in values.items()
    ]


def _counters(stamp, **values):
    return _rows(workload.COUNTER_CODE, stamp, values)


def _hour_of_counters(**per_quarter):
    """Five collections 15 minutes apart, each adding ``per_quarter`` to every counter."""
    rows = []
    for step, minute in enumerate((0, 15, 30, 45, 60)):
        stamp = f"2026-09-03T{8 + minute // 60:02d}:{minute % 60:02d}:00+00:00"
        rows += _counters(stamp, **{k: v * step for k, v in per_quarter.items()})
    return rows


def test_an_hour_of_collections_reports_the_hour_and_not_the_last_quarter_of_it():
    """The reading a DBA asked for. Four quarters of 7,500,000 ms of CPU is 30,000,000 ms in the
    hour; reporting the newest pair under an "hour" heading would understate it four-fold."""
    rows = _hour_of_counters(cpu_usage_ms=7_500_000, page_lookups=625_000_000,
                             batch_requests=90_000)

    block = workload.build_workload(rows)

    assert block["available"] is True
    by_window = {w["key"]: w["seconds"] for w in block["windows"]}
    assert by_window["hour"] == 3600
    assert by_window["now"] == 900

    rows_by_key = {r["key"]: r for g in block["groups"] for r in g["rows"]}
    assert rows_by_key["page_lookups"]["values"]["hour"]["total"] == 2_500_000_000
    assert rows_by_key["page_lookups"]["values"]["now"]["total"] == 625_000_000
    assert rows_by_key["page_lookups"]["values"]["hour"]["perSecond"] == round(2_500_000_000 / 3600, 3)


def test_cpu_milliseconds_are_turned_into_a_percentage_of_the_cores_that_exist():
    """30,000,000 ms of CPU in an hour is every core of an 8-way box and 8% of a 100-way one. The
    raw figure is unreadable without the core count, which is why the collector carries it."""
    rows = _hour_of_counters(cpu_usage_ms=7_200_000)          # 28,800,000 ms over 3600 s, 8 CPU

    block = workload.build_workload(rows)

    cpu = {r["key"]: r for g in block["groups"] for r in g["rows"]}["cpu_pct"]
    assert cpu["unit"] == "pct"
    assert round(cpu["values"]["hour"]["total"], 1) == 100.0
    # A percentage is already normalised over the window; a "per second" on top of it is nothing.
    assert "perSecond" not in cpu["values"]["hour"]


def test_io_latency_is_the_stall_the_interval_saw_not_the_average_since_startup():
    """PERFORMANCE_IO_LATENCY's per-file average covers the whole uptime and cannot catch a spike.
    The same counters differenced over an hour can."""
    rows = _hour_of_counters(io_reads=250_000, io_stall_read_ms=45_000_000,
                             io_writes=100_000, io_stall_write_ms=500_000,
                             io_bytes_read=2 * 1024 * 1024 * 1024)

    block = workload.build_workload(rows)
    by_key = {r["key"]: r for g in block["groups"] for r in g["rows"]}

    assert by_key["io_read_latency_ms"]["values"]["hour"]["total"] == 180.0
    assert by_key["io_write_latency_ms"]["values"]["hour"]["total"] == 5.0
    assert by_key["io_mb_read"]["values"]["hour"]["total"] == 8192.0


def test_a_counter_absent_from_this_build_costs_its_row_and_not_the_page():
    """CXCONSUMER does not exist before 2016 and counters get renamed between versions. Dropping a
    sample for a field it never had would empty every column instead of one row."""
    rows = _hour_of_counters(cpu_usage_ms=1_000, batch_requests=10)

    block = workload.build_workload(rows)
    keys = {r["key"] for g in block["groups"] for r in g["rows"]}

    assert "batch_requests" in keys
    assert "transactions" not in keys           # never collected: absent, not zero
    assert block["windows"], "the window must survive a counter this build does not have"


def test_the_hour_stops_at_a_restart_instead_of_reaching_across_it():
    """The totals begin again at zero after a restart, so a delta across one reads as a burst of
    work that never happened — and a busy instance can pass its own previous totals within the
    hour, so only the carried baseline marker catches it. What the hour reports is the part of it
    that shares a baseline, and it says how long that part was."""
    rows = _rows(workload.COUNTER_CODE, "2026-09-03T08:00:00+00:00",
                 {"cpu_usage_ms": 900_000_000}, since="2025-10-27 02:00:00")
    for minute, value in ((30, 20_000_000), (60, 40_000_000)):
        rows += _rows(workload.COUNTER_CODE,
                      f"2026-09-03T{8 + minute // 60:02d}:{minute % 60:02d}:00+00:00",
                      {"cpu_usage_ms": value}, since="2026-09-03 08:10:00")

    block = workload.build_workload(rows)

    hour = next(w for w in block["windows"] if w["key"] == "hour")
    assert hour["seconds"] == 1800, "the hour is only as long as the samples that share a baseline"
    cpu_ms = {r["key"]: r for g in block["groups"] for r in g["rows"]}["cpu_usage_ms"]
    # 20,000,000 ms of real work, not the 940,000,000 that reaching across the restart would give.
    assert cpu_ms["values"]["hour"]["total"] == 20_000_000


def test_one_collection_is_not_an_interval_and_says_so():
    """A freshly onboarded instance has a total and no rate. Showing the total instead would be the
    since-boot number this whole mechanism exists to keep off the page."""
    block = workload.build_workload(_counters("2026-09-03T08:00:00+00:00", cpu_usage_ms=1))

    assert block["available"] is False
    assert block["samples"] == 1


def test_the_plan_cache_is_reported_beside_the_share_of_cpu_it_accounts_for():
    """sys.dm_exec_query_stats only holds plans that are still cached, so its totals are always an
    undercount of an unknown size. The share is what tells a reader whether the rows below it are
    the hour or a fragment of it."""
    rows = _hour_of_counters(cpu_usage_ms=8_000_000)
    for step, minute in enumerate((0, 15, 30, 45, 60)):
        rows += _rows(workload.QUERY_STATS_CODE,
                      f"2026-09-03T{8 + minute // 60:02d}:{minute % 60:02d}:00+00:00",
                      {"query_cpu_ms": 2_000_000 * step, "query_logical_reads": 10_000 * step,
                       "cached_plans": 4200, "cache_baseline_minutes": 90})

    block = workload.build_workload(rows)

    assert block["cache"]["coveragePct"]["hour"] == 25.0
    cache_keys = {r["key"] for r in block["cache"]["rows"]}
    assert "query_logical_reads" in cache_keys
    # A gauge is not a counter: differencing "how many plans are cached" produces nothing.
    assert "cached_plans" not in cache_keys
    assert block["cache"]["plans"] == 4200


def test_waits_are_ranked_by_the_hour_and_carry_their_share_of_every_wait():
    """A wait type only means something against the others: 30 seconds of WRITELOG is nothing on an
    instance that waited an hour in total and the whole story on one that waited 35 seconds."""
    rows = _hour_of_counters(cpu_usage_ms=1_000)
    for step, minute in enumerate((0, 15, 30, 45, 60)):
        rows += _rows(workload.WAIT_TOTALS_CODE,
                      f"2026-09-03T{8 + minute // 60:02d}:{minute % 60:02d}:00+00:00",
                      {"WRITELOG": 100_000 * step, "PAGEIOLATCH_SH": 300_000 * step,
                       "THREADPOOL": 0, "all_waits": 800_000 * step})

    block = workload.build_workload(rows)

    assert [w["item"] for w in block["waits"]] == ["PAGEIOLATCH_SH", "WRITELOG"]
    top = block["waits"][0]["values"]["hour"]
    assert top["seconds"] == 1200.0
    assert top["sharePct"] == 37.5
    # THREADPOOL is emitted every collection so the item set stays subtractable. A row that waited
    # for nothing all hour is not a finding and must not fill the table.
    assert "THREADPOOL" not in [w["item"] for w in block["waits"]]


def test_the_denominator_is_not_offered_as_a_wait_type_of_its_own():
    """all_waits exists to divide by. Listed as a row it would always be the top "wait" and would
    push the real one below it."""
    rows = _hour_of_counters(cpu_usage_ms=1_000)
    for step, minute in enumerate((0, 30, 60)):
        rows += _rows(workload.WAIT_TOTALS_CODE,
                      f"2026-09-03T{8 + minute // 60:02d}:{minute % 60:02d}:00+00:00",
                      {"WRITELOG": 1_000 * step, "all_waits": 5_000 * step})

    assert [w["item"] for w in workload.build_workload(rows)["waits"]] == ["WRITELOG"]


def test_an_itemless_failure_row_is_not_read_as_a_sample():
    """A failed collection is stored with no item. Treated as a sample it would look like every
    counter dropping to nothing and leaping back — a restart on one row and a spike on the next."""
    rows = _hour_of_counters(cpu_usage_ms=1_000_000)
    rows.append({"metric_code": workload.COUNTER_CODE, "metric_item": "",
                 "metric_value": "", "collected_at": "2026-09-03T08:52:00+00:00",
                 "message": "sqlserver connect to 192.0.2.250:1433 failed"})

    block = workload.build_workload(rows)

    assert block["available"] is True
    assert {w["key"] for w in block["windows"]} >= {"now", "hour"}
