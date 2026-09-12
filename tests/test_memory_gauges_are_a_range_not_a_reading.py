"""A memory gauge read once is an instant, and an instant is not the instance's memory profile.

Written on 2026-09-11, after the per-server page was asked why it had CPU, I/O, waits and workload
and nothing at all about memory. Every memory question about this estate was therefore answered by
running a DMV by hand — and the conclusion reached that way did not survive reading the same
counter a second time:

    "PLE hôm nay 54s so với 447s hôm qua — tệ hơn — mà engine lại nhanh hơn."
    ... "Đọc lại chính counter đó ra 447 (per-NUMA: 378/391/476/721)."

Three separate mistakes are available to a page that prints one memory number, and this file is
about all three:

* **Differencing a gauge.** The workload counters are totals since the engine started and only
  mean something subtracted. These are readings of the moment and mean nothing subtracted — the
  difference between two page-life-expectancy readings is a figure that looks like a rate and is
  not one. The two arithmetics live side by side in `interval_rates` under names that say which.
* **Quoting one reading as the window.** Measured on 192.0.2.250 while writing this, minutes
  apart: 141 then 255 for the instance, 60 then 200 for the worst NUMA node. Every cell states a
  range, and names which end of it is the finding.
* **Reading page life expectancy before the rows that settle it.** Memory grants pending was zero
  throughout, and the pool was holding its target — so the low readings were the workload reading
  data, not an instance short of memory. Those rows are above PLE in the table for that reason.
"""

from db_ops.lib import interval_rates
from db_ops.reports import workload


def _memory(stamp, *, since="2026-09-06 15:58:55", **values):
    return [
        {
            "metric_code": workload.MEMORY_CODE,
            "metric_item": item,
            "metric_value": str(value),
            "collected_at": stamp,
            "message": f"value={value}, counters_since={since}, cpu_count=32",
        }
        for item, value in values.items()
    ]


def _a_day_of_readings(ple=(447, 141, 255), node=(378, 60, 200), pending=(0, 0, 0)):
    """Three collections an hour apart, with the real spread measured on 192.0.2.250."""
    rows = []
    for index, hour in enumerate((6, 7, 8)):
        rows += _memory(
            f"2026-09-11T{hour:02d}:00:00+00:00",
            page_life_expectancy=ple[index],
            ple_node_min=node[index],
            ple_node_count=5,
            memory_grants_pending=pending[index],
            memory_grants_outstanding=2,
            total_server_memory_kb=117441520,
            target_server_memory_kb=117440520,
            database_cache_memory_kb=101484656,
            stolen_server_memory_kb=13428280,
            free_memory_kb=2528584,
            available_physical_kb=9834292,
            total_physical_kb=134216640,
            sql_physical_memory_in_use_kb=117623812,
            system_low_memory_signal=0,
            process_physical_memory_low=0,
            brokers_shrinking=0,
        )
    return rows


def _row(block, key):
    for group in block["groups"]:
        for row in group["rows"]:
            if row["key"] == key:
                return row
    raise AssertionError(f"{key} is not in the memory table")


def test_a_window_states_the_range_the_gauge_actually_covered():
    """The whole point. 141 alone reads as an instance in trouble; 141–447 does not."""
    block = workload.build_workload(_a_day_of_readings())["memory"]

    day = _row(block, "page_life_expectancy")["values"]["day"]
    assert day["min"] == 141
    assert day["max"] == 447
    assert day["latest"] == 255
    assert day["count"] == 3


def test_each_row_names_which_end_of_the_range_is_the_finding():
    """A range the reader has to interpret is only half the fix.

    For page life expectancy the lowest reading is what a memory conversation is about. For
    pending grants it is the highest: one query made to wait is the event, and the other ninety-
    five samples of zero are not evidence against it.
    """
    block = workload.build_workload(_a_day_of_readings())["memory"]

    assert _row(block, "page_life_expectancy")["watch"] == "min"
    assert _row(block, "ple_node_min")["watch"] == "min"
    assert _row(block, "memory_grants_pending")["watch"] == "max"
    assert _row(block, "brokers_shrinking")["watch"] == "max"
    # A level, not an extreme: the pool's size is read against its target, not against its own
    # worst minute.
    assert _row(block, "total_server_memory_kb")["watch"] == ""


def test_the_rows_that_settle_the_question_come_before_page_life_expectancy():
    """Order is the argument. PLE is the number everyone reaches for and the weakest evidence
    here, so the table is built to be read downwards from what actually waited for memory."""
    block = workload.build_workload(_a_day_of_readings())["memory"]
    names = [group["name"] for group in block["groups"]]

    assert names.index("Did anything wait for memory") < names.index("Page life expectancy")
    assert names.index("Buffer pool") < names.index("Page life expectancy")


def test_a_pool_holding_its_target_is_stated_rather_than_left_to_the_eye():
    """Total == Target rules the operating system out as the cause, whatever PLE reads. Two
    counters updated independently sit a few MB apart when a pool has finished growing, so the
    comparison is a tolerance and not an equality."""
    block = workload.build_workload(_a_day_of_readings())["memory"]
    assert block["atTarget"] is True

    squeezed = []
    for row in _a_day_of_readings():
        if row["metric_item"] == "total_server_memory_kb":
            row = {**row, "metric_value": "60000000"}
        squeezed.append(row)
    assert workload.build_workload(squeezed)["memory"]["atTarget"] is False


def test_the_numa_node_count_is_carried_so_the_two_ple_rows_can_be_told_apart():
    """On a NUMA box the instance figure is a combination of the nodes, not the worst of them —
    which is how a node at 60 seconds reads as an instance at 141."""
    block = workload.build_workload(_a_day_of_readings())["memory"]
    assert block["facts"]["ple_node_count"] == 5
    assert _row(block, "ple_node_min")["values"]["day"]["min"] == 60


def test_an_instance_with_one_numa_node_has_no_node_rows_rather_than_zeroes():
    """`Buffer Node` does not exist on a single-node instance. An absent item says "this build has
    no such thing"; a zero would say "the worst node is at zero seconds"."""
    rows = [row for row in _a_day_of_readings()
            if row["metric_item"] not in ("ple_node_min", "ple_node_count")]
    block = workload.build_workload(rows)["memory"]

    assert "ple_node_count" not in block["facts"]
    keys = {row["key"] for group in block["groups"] for row in group["rows"]}
    assert "ple_node_min" not in keys
    assert "page_life_expectancy" in keys


def test_the_latest_interval_column_is_one_reading_and_claims_no_range():
    """It is a column about *now*. A range invented from a single sample would read as a measured
    spread that happened to be flat."""
    block = workload.build_workload(_a_day_of_readings())["memory"]
    now = _row(block, "page_life_expectancy")["values"]["now"]

    assert now["count"] == 1
    assert now["min"] == now["max"] == now["latest"] == 255
    assert next(w for w in block["windows"] if w["key"] == "now")["seconds"] == 0


def test_memory_is_available_before_the_workload_rates_are():
    """A gauge needs one collection to say something; an interval needs two. Blanking the memory
    table until a rate exists would be the page withholding a fact it already holds."""
    one = _memory("2026-09-11T08:00:00+00:00", page_life_expectancy=255,
                  memory_grants_pending=0, total_server_memory_kb=117441520,
                  target_server_memory_kb=117440520)

    built = workload.build_workload(one)
    assert built["available"] is False           # no counter pair yet
    assert built["memory"]["available"] is True
    assert _row(built["memory"], "page_life_expectancy")["values"]["now"]["latest"] == 255


def test_a_window_spanning_a_restart_is_marked_rather_than_dropped():
    """A gauge survives a restart — it still reads the current state — so the window is kept. But
    page life expectancy starts near zero after one and climbs, so a minimum drawn across a restart
    is the restart, and the page has to be able to say so."""
    rows = (_memory("2026-09-11T06:00:00+00:00", page_life_expectancy=447,
                    since="2026-09-06 15:58:55")
            + _memory("2026-09-11T08:00:00+00:00", page_life_expectancy=90,
                      since="2026-09-11 07:40:00"))
    block = workload.build_workload(rows)["memory"]

    assert next(w for w in block["windows"] if w["key"] == "day")["restarted"] is True
    assert next(w for w in block["windows"] if w["key"] == "now")["restarted"] is False


def test_a_gauge_is_never_differenced():
    """The mistake `window_gauge` exists to make impossible. Subtracting two readings of a gauge
    gives a number that looks like a rate: 141 - 447 = -306 "per hour" describes nothing."""
    samples = [
        {"collected_at": "2026-09-11T06:00:00+00:00", "counters_since": "x", "ple": 447},
        {"collected_at": "2026-09-11T07:00:00+00:00", "counters_since": "x", "ple": 141},
    ]
    summary = interval_rates.window_gauge(samples, fields=["ple"], window_hours=24)

    assert "deltas" not in summary
    assert summary["readings"]["ple"] == {"latest": 141.0, "min": 141.0, "max": 447.0,
                                          "avg": 294.0, "count": 2}


def test_one_failed_item_costs_that_reading_and_not_the_window():
    """Per field, not per sample. `window_delta` drops a sample whole for the opposite reason: a
    counter missing from one end of a subtraction has no difference at all."""
    samples = [
        {"collected_at": "2026-09-11T06:00:00+00:00", "ple": 447, "pending": 0},
        {"collected_at": "2026-09-11T07:00:00+00:00", "pending": 0},          # ple missing
        {"collected_at": "2026-09-11T08:00:00+00:00", "ple": 141, "pending": 3},
    ]
    summary = interval_rates.window_gauge(samples, fields=["ple", "pending"], window_hours=24)

    assert summary["readings"]["ple"]["count"] == 2
    assert summary["readings"]["pending"]["count"] == 3
    assert summary["readings"]["pending"]["max"] == 3


def test_the_collector_records_and_grades_nothing():
    """It is a recording metric like the workload counters, not an alert. None of these gauges has
    a threshold that holds on every instance — and the single-reading verdict is the mistake the
    section exists to prevent, so the collector must not be able to reach one either."""
    from pathlib import Path

    sql = Path("db_ops/metrics/collectors/sqlserver/075_sqlserver_memory_gauges.sql").read_text(
        encoding="utf-8")
    assert "'OK' AS varchar(16)" in sql
    assert "WARNING" not in sql and "CRITICAL" not in sql
    # Buffer cache hit ratio reads 99.99% on exactly the workload this section describes.
    assert "Buffer cache hit ratio" not in sql.split("SET NOCOUNT ON")[1]


def test_every_item_the_collector_emits_has_a_home_on_the_page():
    """An item collected and never rendered is a row nobody sees and a cost nobody knows about."""
    from pathlib import Path
    import re

    sql = Path("db_ops/metrics/collectors/sqlserver/075_sqlserver_memory_gauges.sql").read_text(
        encoding="utf-8")
    body = sql.split("SET NOCOUNT ON")[1]
    emitted = set(re.findall(r"'([a-z_]{4,})'\s*(?:AS metric_item|,)", body))
    emitted &= {
        "total_server_memory_kb", "target_server_memory_kb", "database_cache_memory_kb",
        "stolen_server_memory_kb", "free_memory_kb", "memory_grants_pending",
        "memory_grants_outstanding", "page_life_expectancy", "ple_node_min", "ple_node_count",
        "total_physical_kb", "available_physical_kb", "system_low_memory_signal",
        "sql_physical_memory_in_use_kb", "process_physical_memory_low", "brokers_shrinking",
    }
    rendered = {key for _, specs in workload._MEMORY_ROWS for key, _, _, _ in specs}
    rendered |= set(workload._MEMORY_FACTS)

    assert emitted <= rendered, f"collected but never shown: {sorted(emitted - rendered)}"
    assert rendered <= emitted, f"shown but never collected: {sorted(rendered - emitted)}"
