"""``server-metrics.html``: the verdict: the health areas, the problems grouped by metric, the timeline, the score.

Split out of ``reports/server_report.py`` on 2026-10-03 (Q11,
``audits/20261002_audit_typed_requests_and_errors.md`` section 5). ``server_report`` re-exports
every name, so no import changes.
"""

from __future__ import annotations
import datetime
import re
from dbabrain.lib import health_model, interval_rates
from dbabrain.lib.coerce import as_epoch, as_float
from dbabrain.reports import page_texts
from dbabrain.reports.server_series import (
    OS_DISK_ACTIVITY_ITEMS,
    OS_DISK_NOT_A_VOLUME_ITEMS,
    SEVERITY_RANK,
    _is_percent_unit,
    metric_catalog,
    metric_label,
    severity_of,
)


# A collection older than this means the page is describing the past, not the present. The
# densest metrics run every 5 minutes and the sparsest every 2 hours, so nothing being newer
# than 3 hours means collection itself has stopped — which is a finding, not a healthy server.
STALE_AFTER_SECONDS = 3 * 3600

# The health areas, in the order a DBA triages them. ``report_judged`` marks the areas the collector
# deliberately does not alert on (their SQL returns 'OK' on every branch), so the page must not
# pretend the collector agreed.
#
# **What an area is CALLED, the rule its tile prints and the note under it are not here** (0.39.0):
# ``label``, ``threshold`` and ``note`` are the page's texts, in ``reports_config.json`` ->
# ``page_texts.areas`` (``reports/page_texts.py``), each sayable per engine. They were literals in
# this table, one wording for every engine - the Memory note explained page life expectancy to a
# PostgreSQL host and the temporary-space tile was called TempDB on Oracle (the operator,
# 2026-11-13: *fix what is hard-coded, the texts too*). What stays is the structure: which items
# answer each area's question.
#
# ``selectors`` names **items**, not just metric codes, and their order is the area's priority.
# Whole-code membership plus "take the largest number" produced four confident lies on one run:
# the CPU tile read `WARNING 75.63 pct` from SYSTEM_CPU_MEMORY/sql_memory while CPU was 5-8%, the
# memory tile read `255314 MB` next to a percentage threshold, and Disk space read `53083 KB/s` —
# disk read throughput — because KB/s is a bigger number than any percentage. An area is a
# question ("how full is the storage"), and only items that answer *that* question belong in it.
#
# Each selector is ``{"code": ..., "items": (...), "exclude": (...), "units": ...}``; ``items``
# absent means every item of the code except ``exclude``, and ``units`` restricts to a unit family
# so a byte-rate row can never stand in for a percentage. ``item_suffix`` matches an item by its
# end, for a collector that names items after something of the estate's - DOCKER_CONTAINER_STATS
# writes ``<container>:cpu``, and the container's name is the operator's.
#
# A database in a container (the 0.26 node's PostgreSQL 5433 and SQL Server MSSQL25) has no host
# login, so no OS_* collector runs for it and its CPU and Memory tiles read "not collected" -
# while the container's own CPU and memory were collected every five minutes beside its SQL
# metrics (one target is one container). They are the last selector of each area: a target with
# OS or SQL readings shows those, a container target shows its container's. A container's CPU is
# percent of ONE core (200% = two cores busy) and the collector judges none of it; its memory is
# percent of the container's limit, WARNING at 90%.
AREAS: list[dict] = [
    {"key": "availability",
     "selectors": [{"code": "INSTANCE_STATUS"}, {"code": "DATABASE_STATUS"},
                   {"code": "DATABASE_SUSPECT_PAGES"}]},
    {"key": "cpu",
     # Only the two items that are CPU. OS_CPU_USAGE also carries processor queue length and load
     # average; SYSTEM_CPU_MEMORY also carries two memory percentages.
     # A container's share of the CPUs it may use before its raw `docker stats` figure, which is
     # in CPUs (319% = 3.19 busy) and kept only for rows from before the share was collected.
     # Its tile reads the hour, not the newest sample: one sample at 95% is a moment (2026-10-26).
     "selectors": [{"code": "OS_CPU_USAGE", "items": ("cpu_usage",)},
                   {"code": "SYSTEM_CPU_MEMORY", "items": ("cpu",)},
                   {"code": "DOCKER_CONTAINER_STATS", "item_suffix": (":cpu_share",), "units": "percent"},
                   {"code": "DOCKER_CONTAINER_STATS", "item_suffix": (":cpu",), "units": "percent"}]},
    {"key": "memory",
     "selectors": [{"code": "OS_MEMORY_USAGE", "items": ("memory_usage",)},
                   {"code": "SYSTEM_CPU_MEMORY", "items": ("system_memory", "sql_memory")},
                   {"code": "OS_MEMORY_USAGE", "items": ("swap_usage", "pagefile_usage")},
                   {"code": "PAGE_LIFE_EXPECTANCY"},
                   {"code": "DOCKER_CONTAINER_STATS", "item_suffix": (":memory",), "units": "percent"}],
     "report_judged": True},
    {"key": "disk_space",
     # Mount points and file-fullness only. The throughput and queue-length rows of OS_DISK_USAGE
     # are storage *activity*; they have their own area below.
     "selectors": [{"code": "OS_DISK_USAGE",
                    "exclude": OS_DISK_NOT_A_VOLUME_ITEMS},
                   {"code": "STORAGE_DISK_FREE_SPACE"},
                   # A database in a container: the volume its data is on, as `df` inside the
                   # container reads it. Before this the tile fell to the database's own size in
                   # bytes, which says how big it is and nothing about how full the disk is.
                   {"code": "DOCKER_CONTAINER_STATS", "item_contains": ":disk:", "units": "percent"},
                   # How full each data file is - a percentage. SQL Server's rows are; Oracle 8i's
                   # are a file's size in MB and PostgreSQL's a database's size in bytes, and the
                   # tile printed "20000 MB" and "48803043007 bytes" as if they were fullness.
                   {"code": "STORAGE_DATA_FILE_SPACE", "units": "percent"},
                   {"code": "LOG_FILE_SPACE", "units": "percent"}]},
    # Its threshold text states each collector's own rule, as the tile grades every one of them
    # (audit 2026-11-05, B9): the host's OS collector, and SQL Server's by free space.
    {"key": "disk_latency",
     # Until 2026-10-26 this was graded on the newest row: the average since the engine started,
     # which kept a stall eight days old CRITICAL. The window is dbabrain.lib.report_windows.
     "selectors": [{"code": "PERFORMANCE_IO_LATENCY"}],
     "report_judged": True},
    {"key": "storage_activity",
     "selectors": [{"code": "OS_DISK_USAGE",
                    "items": OS_DISK_ACTIVITY_ITEMS}],
     "report_judged": True},
    {"key": "blocking", "empty_is_ok": True,
     "selectors": [{"code": "LOCK_BLOCKING_SESSIONS"}, {"code": "LOCK_DEADLOCK_RECENT"},
                   {"code": "LOCK_TRANSACTION_HOLDERS"}]},
    {"key": "long_queries", "empty_is_ok": True,
     "selectors": [{"code": "QUERY_LONG_RUNNING"},
                   {"code": "QUERY_LONG_WAITING_OR_ROLLBACK_REQUESTS"}]},
    {"key": "backup",
     # Deliberately has no selectors: this area is decided by the per-database backup policy in
     # dbabrain.lib.backup_policy, not by whichever backup row happens to carry the largest
     # number. "OK 3185 hours_since_last_backup" under a 48-hour threshold is what code-only
     # membership produced here.
     "selectors": [],
     "policy": "backup"},
    {"key": "jobs",
     "selectors": [{"code": "JOB_FAILED"}, {"code": "SQL_AGENT_JOB_RUNTIME"}]},
    {"key": "tempdb",
     "selectors": [{"code": "STORAGE_TEMP_SPACE"}]},
    {"key": "security",
     "selectors": [{"code": "SECURITY_FAILED_LOGINS"}, {"code": "SECURITY_LOGIN_HEALTH"},
                   {"code": "SECURITY_CERTIFICATE_EXPIRY"}, {"code": "DATABASE_USER_PERMISSIONS"}]},
    {"key": "ha",
     "selectors": [{"code": "AVAILABILITY_DATABASE_HEALTH"}, {"code": "POSTGRES_REPLICATION"},
                   {"code": "POSTGRES_REPLICATION_SLOTS"}, {"code": "POSTGRES_WAL_ARCHIVE"}]},
    # --- What an engine measures under its own name (0.28.0) --------------------------------
    #
    # The thirteen areas above are SQL Server's questions, and an Oracle or PostgreSQL page
    # answered most of them "not collected" while the store held that engine's own answers: the
    # process ceiling, the tablespaces, the wraparound horizon, the vacuum backlog. Each area
    # below is one of those questions.
    #
    # ``only_when_collected`` is how a page stays its engine's own without the builder being told
    # which engine it is looking at - the rule the Oracle sections further down the page already
    # follow. Every code here is exclusive to one engine, so the tile exists exactly where the
    # metric does: a SQL Server page gains nothing, an Oracle page no PostgreSQL tile. (The
    # thirteen keep printing "not collected" for every engine, as they did.)
    #
    # ``empty_is_ok`` is for an area whose collectors return a row only when something is wrong -
    # a blocked session, a long transaction, an invalid index. Oracle's and PostgreSQL's do; SQL
    # Server's write a zero row instead. With no row to show, such an area printed "not
    # collected" for a metric that had run a minute before and found nothing, which is the
    # healthy answer. Where the metric's own freshness says it ran clean, the tile says so.
    #
    # ``lowest_is_worst`` turns the last tie-break round: among rows of equal status these areas
    # show the *smallest* value, because the tablespace to look at is the one with the least free
    # space - "the fullest disk wins" is the same rule, read on a metric that counts what is left.
    {"key": "oracle_processes", "only_when_collected": True,
     "selectors": [{"code": "PROCESS_LIMIT"}, {"code": "PROCESS_DEAD_WEIGHT"}]},
    {"key": "oracle_tablespaces", "only_when_collected": True,
     "lowest_is_worst": True,
     "selectors": [{"code": "TABLESPACE_FREE_SPACE"}, {"code": "SEGMENT_EXTENT_LIMIT"}]},
    {"key": "oracle_sga", "only_when_collected": True,
     "lowest_is_worst": True,
     # The shared pool's free memory only: the java and large pools are logged, not judged, and a
     # large pool at half a megabyte free is its normal state.
     "selectors": [{"code": "SHARED_POOL_FREE", "items": ("shared pool:free memory",)},
                   {"code": "BUFFER_CACHE_HIT"}]},
    {"key": "oracle_rollback", "only_when_collected": True,
     "selectors": [{"code": "ROLLBACK_SEGMENT_CONTENTION"}]},
    {"key": "oracle_objects", "only_when_collected": True,
     "empty_is_ok": True,
     "selectors": [{"code": "INVALID_OBJECTS"}, {"code": "INDEX_UNUSABLE"}]},
    {"key": "postgres_wraparound",
     "only_when_collected": True,
     "selectors": [{"code": "POSTGRES_XID_WRAPAROUND"}]},
    {"key": "postgres_vacuum", "only_when_collected": True,
     "selectors": [{"code": "POSTGRES_VACUUM_HEALTH"}, {"code": "POSTGRES_TABLE_BLOAT"}]},
    {"key": "postgres_transactions", "only_when_collected": True,
     "empty_is_ok": True,
     "selectors": [{"code": "POSTGRES_LONG_TRANSACTIONS"}]},
    {"key": "postgres_indexes", "only_when_collected": True,
     "empty_is_ok": True,
     "selectors": [{"code": "POSTGRES_INVALID_INDEXES"}]},
]

#: Every metric code any area selects. ``build_problems`` uses it to find an entry's area.
AREA_CODES: dict[str, list[str]] = {
    spec["key"]: sorted({selector["code"] for selector in spec["selectors"]}) for spec in AREAS
}

# What to do about it. Keyed by metric code; the area falls back to its own note.
#: What to do about a metric is the metric's own text, in ``metric_definitions.json`` -
#: ``report_policy.action``, with ``action_by_platform`` / ``action_by_db_type`` where the right
#: words differ by operating system or engine. It was a table in this module keyed by the code
#: alone, so one sentence was shown on every platform: a Linux host with errors in its journal was
#: told to read the Windows event log, on its own page and in the fleet page's card for a group
#: holding both (the operator, 2026-11-13: *the VM is Ubuntu - why the Windows event log? fix
#: what is hard-coded, the texts too*).
GENERIC_ACTION = "Check the metric detail below."

#: The words a group of servers on several platforms is told apart by.
PLATFORM_NAMES = {"windows": "Windows", "linux": "Linux"}

_METRIC_ACTIONS: dict[str, dict] | None = None


def _texts(value: object) -> dict[str, str]:
    """``{key: sentence}`` from an ``action_by_*`` object - lower-cased keys, empty sentences dropped."""
    if not isinstance(value, dict):
        return {}
    return {str(key).strip().lower(): str(text).strip() for key, text in value.items() if str(text or "").strip()}


def metric_actions(path=None) -> dict[str, dict]:
    """``{code: {"action", "by_platform", "by_db_type"}}`` from each metric's ``report_policy``, cached.

    Best-effort, as every reader of the catalog here is: without the file a problem row falls back
    to its area's note rather than failing the page.
    """
    global _METRIC_ACTIONS
    if _METRIC_ACTIONS is not None and path is None:
        return _METRIC_ACTIONS
    from dbabrain.lib import data_sources
    from dbabrain.reports.server_series import METRIC_DEFINITIONS

    found: dict[str, dict] = {}
    try:
        for definition in data_sources.load_metric_definition_records(path or METRIC_DEFINITIONS):
            policy = definition.get("report_policy") if isinstance(definition.get("report_policy"), dict) else {}
            entry = {"action": str(policy.get("action") or "").strip(),
                     "by_platform": _texts(policy.get("action_by_platform")),
                     "by_db_type": _texts(policy.get("action_by_db_type"))}
            code = str(definition.get("metric_code") or "")
            if code and (entry["action"] or entry["by_platform"] or entry["by_db_type"]):
                found[code] = entry
    except (OSError, ValueError, AttributeError):
        found = {}
    if path is None:
        _METRIC_ACTIONS = found
    return found


def _platform_key(platform: object) -> str:
    """``windows`` / ``linux`` from what a collector or the inventory said (``Linux``, ``windows``), else ``""``."""
    text = str(platform or "").strip().lower()
    return text if text in PLATFORM_NAMES else ""


def _engine_key(db_type: object) -> str:
    """The engine's key as the definitions spell it (``postgres`` -> ``postgresql``); ``""`` for a bare host."""
    from dbabrain.lib import engine_sections

    key = engine_sections.section_of(db_type) if str(db_type or "").strip() else ""
    return key if key not in ("host", "other") else ""


def series_severity(entry: dict) -> str:
    return health_model.current_severity(
        code=entry["code"], status=entry.get("status"), value=entry.get("last"))


def series_downgraded(entry: dict) -> str:
    """The severity ``metrics.metric_overrides`` took off this entry, ``""`` when none was."""
    return health_model.downgraded_from(str(entry.get("message") or ""))


def _bytes_text(value: float) -> str:
    for unit, size in (("TB", 1024 ** 4), ("GB", 1024 ** 3), ("MB", 1024 ** 2)):
        if abs(value) >= size:
            return f"{value / size:,.2f} {unit}"
    return f"{value:,.0f} bytes"


def _value_text(entry: dict) -> str:
    if entry.get("numeric") and entry.get("last") is not None:
        value = entry["last"]
        unit = str(entry.get("unit") or "")
        # PostgreSQL's database size is stored in bytes: "48803043007 bytes" was the tile.
        if unit.lower() == "bytes":
            return _bytes_text(value)
        number = f"{value:.0f}" if abs(value) >= 100 else f"{value:g}"
        suffix = ("%" if unit.lower().startswith(("percent", "pct"))
                  else (f" {unit}" if unit else ""))
        return f"{number}{suffix}"
    # A collector-failure row has no value at all — that is what it is saying. "—" on the tile
    # reads as "nothing to report", which is the opposite of the truth.
    if entry.get("item") == health_model.COLLECTOR_ITEM and not entry.get("lastText"):
        return "collector failed"
    return str(entry.get("lastText") or "—")


def _selector_matches(selector: dict, entry: dict) -> bool:
    if entry["code"] != selector["code"]:
        return False
    item = str(entry.get("item") or "")
    items = selector.get("items")
    if items is not None and item not in items:
        return False
    suffixes = selector.get("item_suffix")
    if suffixes is not None and not item.endswith(tuple(suffixes)):
        return False
    contains = selector.get("item_contains")
    if contains is not None and contains not in item:
        return False
    if item in (selector.get("exclude") or ()):
        return False
    if selector.get("units") == "percent" and not _is_percent_unit(entry.get("unit")):
        return False
    return True


def area_members(spec: dict, series: list[dict]) -> list[tuple[int, dict]]:
    """``(priority, entry)`` for every series entry this area selects.

    ``priority`` is the selector's position in the area: the first selector is what the area is
    *about*, the rest are supporting evidence. It decides which value the tile shows when nothing
    is wrong, so a healthy CPU area shows CPU rather than whichever of its members happens to
    carry the largest number.
    """
    return [
        (priority, entry)
        for priority, selector in enumerate(spec.get("selectors") or [])
        for entry in series
        if _selector_matches(selector, entry)
    ]


def build_areas(series: list[dict], *, backup: dict | None = None,
                freshness: dict | None = None, workload: dict | None = None,
                db_type: object = "", platform: object = "") -> list[dict]:
    """Current state of each health area: the worst thing in it, its value, and the rule.

    Selection is ``(severity, downgraded severity, selector priority, value)`` in that order.
    Severity first because an area exists to say whether something is wrong; the severity a
    ``severity_map`` removed second, so a silenced finding is not hidden by a healthy neighbour;
    priority third so the tile answers its own question; value last so that among equals the
    fullest disk wins. Comparing raw values *first* is what let 53083 KB/s decide an area measured
    in percent.

    ``workload`` is the Workload block, read for one thing: how many of the host's CPUs the engine
    may use, which the CPU tile states beside its percentage (``capacity``).
    """
    stale_codes = {row["code"] for row in (freshness or {}).get("metrics", [])
                   if row.get("state") in ("LATE", "FAILED")}
    # The metrics that ran on time and without an error - whether or not they returned a row.
    ran_clean = {row["code"] for row in (freshness or {}).get("metrics", [])
                 if row.get("state") == "OK"}
    # Every metric this target has or should have: what it produced, plus what the catalogue
    # says fits its engine and produced nothing. An area built only from SQL metrics none of
    # which is in this set is not a gap in collection - the engine has no such metric (TempDB
    # on PostgreSQL, the Agent's jobs on Oracle), and "not collected" sends a reader looking
    # for a collector that does not exist. Known only where freshness was computed.
    fits_target = ({row["code"] for row in (freshness or {}).get("metrics", [])}
                   | {row["code"] for row in (freshness or {}).get("notCollected", [])})
    sql_codes = {entry["code"] for entry in metric_catalog()
                 if entry["collector_type"] == "sql"} if fits_target else set()

    areas = []
    for spec in AREAS:
        # The area's own words, as they are said for this server's engine (page_texts.areas).
        base = {"key": spec["key"], **{field: page_texts.area_text(spec["key"], field, db_type=db_type,
                                                                   platform=platform)
                                       for field in ("label", "threshold", "note")}}
        base["reportJudged"] = bool(spec.get("report_judged"))
        if spec.get("policy") == "backup":
            areas.append({**base, **_backup_area_state(backup)})
            continue
        members = area_members(spec, series)
        if not members and spec.get("empty_is_ok"):
            ran = sorted(ran_clean.intersection(
                selector["code"] for selector in spec["selectors"]))
            if ran:
                areas.append({
                    **base, "status": "OK", "value": "none found",
                    "detail": ", ".join(metric_label(code) for code in ran)
                              + " ran and returned no rows",
                    "sourceCode": ran[0], "sourceItem": "", "collectedAt": None,
                    "stale": False, "downgradedFrom": ""})
                continue
        if not members and spec["key"] == "cpu":
            # No host and no engine CPU metric (Oracle without host access): the engine's own CPU
            # counters, from the Workload block, or why there is no reading.
            engine_tile = _engine_cpu_tile(series, workload)
            if engine_tile:
                areas.append({**base, **engine_tile})
                continue
        if not members and spec.get("only_when_collected"):
            # An engine's own area on a server that is not that engine: no tile at all. "Not
            # collected" would be a gap on the page for a metric the server does not have.
            continue
        if not members:
            codes = {selector["code"] for selector in spec["selectors"]}
            no_such_metric = bool(codes) and codes <= sql_codes and not codes & fits_target
            # The value is two words on purpose: it sits beside the tile's title, and a whole
            # sentence there wrapped the title at 1366 px. The sentence is the detail line.
            areas.append({**base, "status": "UNKNOWN",
                          "value": "no such metric" if no_such_metric else "not collected",
                          "detail": ("this engine has no metric for this area"
                                     if no_such_metric else ""),
                          "sourceCode": "", "sourceItem": "", "collectedAt": None, "stale": False,
                          "downgradedFrom": ""})
            continue
        # Severity, then the severity config *removed*, then priority, then value. The second key
        # is what keeps a silenced finding from being hidden by a genuinely healthy neighbour: a
        # CPU area holding a downgraded-from-CRITICAL cpu_usage and an OK load_average must show
        # the one somebody decided not to alert on, not the one that had nothing to say.
        # The last key is "the fullest disk wins" - and for an area that counts what is *left*
        # (free megabytes, a hit ratio) the fullest is the smallest number, so the sign turns.
        direction = -1 if spec.get("lowest_is_worst") else 1
        priority, worst = max(
            members,
            key=lambda pair: (SEVERITY_RANK[series_severity(pair[1])],
                              SEVERITY_RANK.get(series_downgraded(pair[1]), 0), -pair[0],
                              direction * (pair[1].get("last") or 0)),
        )
        status = series_severity(worst)
        not_ok = sum(1 for _priority, entry in members if series_severity(entry) != "OK")
        detail = f"{worst['label']} · {worst['item']}"
        if not_ok > 1:
            detail += f" (+{not_ok - 1} more not OK)"
        # An area whose own metric is late or failing does not get to report OK from the last
        # sample that worked. It reports what it is: unknown, and why.
        stale = sorted(stale_codes.intersection(
            selector["code"] for selector in spec["selectors"]))
        if stale and status == "OK":
            status = "UNKNOWN"
            detail = f"{', '.join(stale)} is late or failing — this area is not current"
        capacity, capacity_note = (
            _cpu_capacity(series, workload, worst) if spec["key"] == "cpu"
            else (_memory_capacity(series), "") if spec["key"] == "memory"
            else (_disk_capacity(worst, members), "") if spec["key"] == "disk_space"
            # The graded hour beside the latest interval and the day, so a spike that is over
            # and a slow day that is not both show on the tile.
            else (worst.get("windowLine") or "", "") if spec["key"] == "disk_latency"
            else ("", ""))
        if worst.get("windowLine") and spec["key"] != "disk_latency":
            # Every other tile judged on the hour says so on its source line.
            detail = f"{detail} · {worst['windowLine']}"
        areas.append({
            **base, "status": status, "value": _value_text(worst), "detail": detail,
            # What the percentage is of, in figures; and, where it changes how to read the tile, why.
            "capacity": capacity, "capacityNote": capacity_note,
            "sourceCode": worst["code"], "sourceItem": worst["item"],
            "unit": str(worst.get("unit") or ""), "collectedAt": worst.get("lastAt"),
            "stale": bool(stale), "priority": priority,
            # Not part of the status: the tile stays the colour config asked for. It is the
            # missing half of the sentence — "this value crossed the rule printed above it, and
            # somebody decided that is not an alert here".
            "downgradedFrom": series_downgraded(worst),
        })
    return areas


# --------------------------------------------------------------------------------------------- #
# Capacity: what the percentage on a tile is a percentage *of*.
#
# "CPU 5.12%" on a 60-CPU host that SQL Server Standard may use 48 of, "Memory 63%" of 400 GB, a
# disk at 83% that is 172 GB from full - each tile carried the first number and none of the second,
# so a reader had to open the charts to learn whether 83% was a crisis or a week of headroom. The
# collectors already state the totals in their messages; these read them back, for every engine.
# --------------------------------------------------------------------------------------------- #

_CONTAINER_MEMORY = re.compile(r"mem_usage=(\S+)\s*/\s*(\S+)")


def _message_fields(entry: dict | None) -> dict[str, str]:
    if not entry:
        return {}
    return interval_rates.message_fields(entry.get("message"))


def _series_entry(series: list[dict], code: str, item: str) -> dict | None:
    return next((entry for entry in series
                 if entry.get("code") == code and entry.get("item") == item), None)


def _size_pair(used_gb: float, total_gb: float) -> str:
    """``used / total`` in one unit, the one the total reads best in."""
    if total_gb >= 1024:
        return f"{used_gb / 1024:.2f} / {total_gb / 1024:.2f} TB"
    if total_gb >= 100:
        return f"{used_gb:,.0f} / {total_gb:,.0f} GB"
    return f"{used_gb:.1f} / {total_gb:.1f} GB"


def _size(gb: float) -> str:
    if gb >= 1024:
        return f"{gb / 1024:.2f} TB"
    return f"{gb:,.0f} GB" if gb >= 100 else f"{gb:.1f} GB"


def _cpu_topology(fields: dict) -> str:
    """The host's processors as its OS counted them: ``3 sockets, 17 cores (8 + 6 + 3)``.

    One form for every host, and nothing in it the collector did not write. The tile used to choose
    between ``2 sockets × 16 cores`` and ``3 sockets, 17 cores`` by whether the total divided by the
    sockets - so two servers' tiles read differently (asked 2026-10-26), and the ``×`` claimed every
    socket alike, which a virtual machine need not be: the guest behind ``3 sockets, 17 cores``
    has 8 + 6 + 3. The collectors write each socket's cores from 2026-10-26 (``cores_per_socket``);
    a row from before has the totals only, and the tile prints the totals only.
    """
    sockets = as_float(fields.get("sockets"))
    cores = as_float(fields.get("cores"))
    per_socket = [part for part in str(fields.get("cores_per_socket") or "").split("+") if part]
    parts = []
    if sockets:
        parts.append(f"{int(sockets)} socket{'' if int(sockets) == 1 else 's'}")
    if cores:
        text = f"{int(cores)} cores"
        if sockets and int(sockets) > 1 and len(per_socket) == int(sockets):
            text += f" ({' + '.join(per_socket)})"
        parts.append(text)
        # Why 32 logical CPUs sit beside 16 cores (asked 2026-10-30, .245): the OS counts each core's
        # threads. Said only from the two counts the collector read, and only when they divide.
        logical = as_float(fields.get("logical_cpus"))
        if logical and int(logical) > int(cores) and int(logical) % int(cores) == 0:
            parts.append(f"{int(logical) // int(cores)} threads per core")
    return ", ".join(parts)


def _cpu_capacity(series: list[dict], workload: dict | None,
                  shown: dict | None = None) -> tuple[str, str]:
    """``48 / 60 CPUs online / total``, and - when those differ - what that does to the tile.

    ``shown`` is the entry the tile prints. When it is the engine's row on SQL Server on Linux
    (``cpu_basis=sql_process``) the percentage is SQL Server's own share: the host's idle time is
    not reported there (0.31.0 sheet 1.190), and the note says so rather than let it read as the
    host's.
    """
    if (shown or {}).get("code") == "DOCKER_CONTAINER_STATS":
        return _container_cpu_capacity(shown)
    host = _message_fields(_series_entry(series, "OS_CPU_USAGE", "cpu_usage"))
    engine = _message_fields(_series_entry(series, "SYSTEM_CPU_MEMORY", "cpu"))
    facts = (workload or {}).get("cpu") or {}
    total = (as_float(host.get("logical_cpus")) or facts.get("total")
             or as_float(engine.get("sql_visible_cpu_count")))
    online = facts.get("online") or as_float(engine.get("scheduler_count"))
    if not total:
        return "", ""
    total = int(total)
    # The pair whenever the engine's count is known, equal or not: "32 CPUs" alone did not say
    # whether the engine had been asked how many it may use, beside a "48 / 60" on the next server.
    # Logical CPUs, said so: the cores beside them are physical, and "32 CPUs · 16 cores" read as a
    # contradiction (asked 2026-10-30).
    text = f"{int(online)} / {total} logical CPUs online / total" if online else f"{total} logical CPUs"
    online = int(online) if online else total
    topology = _cpu_topology(host)
    if topology:
        text += f" · {topology}"
    note = ""
    if online < total:
        who = facts.get("engine") or "the database"
        reason = facts.get("limit") or f"{who} runs on {online} of them"
        # The tile is the host's CPU. With the engine held to part of the host, the engine at full
        # load is this share of the tile - well under the CRITICAL line drawn above it.
        note = (f"{reason[0].upper()}{reason[1:]}, so {who} saturated reads about "
                f"{round(online * 100 / total)}% on this tile.")
    if (shown or {}).get("code") == "SYSTEM_CPU_MEMORY" and engine.get("cpu_basis") == "sql_process":
        basis = ("SQL Server on Linux does not report the host's idle time: this is SQL Server's "
                 "own share of the CPU, not the host's.")
        note = f"{note} {basis}".strip()
    return text, note


def _container_cpu_capacity(entry: dict) -> tuple[str, str]:
    """``1.35 of 8 CPUs busy``, and what the 8 is: the container's limit, or the host's count.

    `docker stats` counts in CPUs, so the tile used to print ``319%`` with nothing to divide it by
    (2026-10-26). The collector reads the denominator - ``--cpus``, a CPU quota, a cpuset, else the
    daemon's count of the host's CPUs - and names which; a row from before it has none, and the
    tile says so instead of choosing one.
    """
    fields = _message_fields(entry)
    cpus = as_float(fields.get("cpus"))
    busy = as_float(fields.get("cpus_busy"))
    if busy is None:
        raw = as_float(str(fields.get("cpu") or "").rstrip("%"))
        busy = raw / 100 if raw is not None else None
    if not cpus:
        return ("", "docker stats counts in CPUs (100% is one CPU busy); this row does not say how "
                    "many CPUs the container may use.")
    text = f"{busy:.2f} of {cpus:g} CPUs busy" if busy is not None else f"{cpus:g} CPUs"
    basis = str(fields.get("cpus_basis") or "")
    if basis == "host_no_limit":
        note = f"No CPU limit is set on the container: its share is of the host's {cpus:g} CPUs."
    elif basis == "limit_cpus":
        note = f"The container is limited to {cpus:g} CPUs (--cpus)."
    elif basis == "limit_cpu_quota":
        note = f"The container is limited to {cpus:g} CPUs by its CPU quota."
    elif basis.startswith("cpuset_"):
        note = f"The container is pinned to CPUs {basis[len('cpuset_'):].replace(';', ',')}."
    else:
        note = ""
    return text, note


def _engine_cpu_tile(series: list[dict], workload: dict | None) -> dict | None:
    """The CPU tile from the engine's own CPU counters, where nothing else measures the CPU.

    An Oracle reached without host access has no OS metric and no engine CPU row; its Workload
    section differences ``v$sysstat`` / ``v$sys_time_model`` into a percentage of the CPUs it may
    use, and the tile read *not collected* beside it. With ``timed_statistics`` off the instance
    times nothing - the collector reads the parameter, and the tile says that rather than 0%.
    """
    facts = (workload or {}).get("cpu") or {}
    capacity, _note = _cpu_capacity(series, workload)
    common = {"capacity": capacity, "capacityNote": "", "sourceCode": "PERFORMANCE_WORKLOAD_COUNTERS",
              "sourceItem": "cpu_usage_ms", "collectedAt": as_epoch((workload or {}).get("asOf")),
              "stale": False, "downgradedFrom": "", "unit": "percent", "reportJudged": True}
    if str(facts.get("timedStatistics") or "").upper() == "FALSE":
        return {**common, "status": "UNKNOWN", "value": "not timed",
                "detail": ("timed_statistics=FALSE on this instance: the database records no CPU "
                           "time, so its CPU use cannot be read from it")}
    row = next((row for group in (workload or {}).get("groups") or []
                for row in group.get("rows") or [] if row.get("key") == "cpu_pct"), None)
    if not row:
        return None
    values = row.get("values") or {}
    reading = {key: (values.get(key) or {}).get("total") for key in ("now", "hour", "day")}
    graded = reading["hour"] if reading["hour"] is not None else reading["now"]
    if graded is None:
        return None
    status = "CRITICAL" if graded >= 90 else "WARNING" if graded >= 80 else "OK"
    shown = " · ".join(f"{label} {reading[key]:g}%" for key, label in
                       (("hour", "1 h"), ("now", "latest"), ("day", "24 h"))
                       if reading[key] is not None)
    return {**common, "status": status, "value": f"{graded:g}%",
            "detail": f"the database's own CPU, from its counters: {shown} · {row.get('source') or ''}".rstrip(" ·")}


def _memory_capacity(series: list[dict]) -> str:
    """``254 / 400 GB used / total`` from the OS, the engine's view, or the container's limit.

    On SQL Server on Linux the engine's view is of its own limit (``memory_scope=sql_server_limit``,
    mssql-conf ``memory.memorylimitmb``), and a server page printed it as the host's RAM - 24 GB
    on a host of about 64 (2026-10-23, 0.31.0 sheet 1.191). The OS's total still comes first; the
    engine's is labelled for what it is.
    """
    os_mem = _message_fields(_series_entry(series, "OS_MEMORY_USAGE", "memory_usage"))
    sys_mem = _message_fields(_series_entry(series, "SYSTEM_CPU_MEMORY", "system_memory"))
    sql_mem = _message_fields(_series_entry(series, "SYSTEM_CPU_MEMORY", "sql_memory"))
    text = ""
    of_what = "total"
    total, used = as_float(os_mem.get("total_gb")), as_float(os_mem.get("used_gb"))
    available = as_float(os_mem.get("available_gb"))
    if not total:
        total_mb = as_float(sys_mem.get("total_physical_memory_mb"))
        available_mb = as_float(sys_mem.get("available_physical_memory_mb"))
        if total_mb and available_mb is not None:
            total, available = total_mb / 1024, available_mb / 1024
            used = total - available
            if sys_mem.get("memory_scope") == "sql_server_limit":
                of_what = "SQL Server's limit"
    if total and used is not None:
        text = f"{_size_pair(used, total)} used / {of_what}"
        if available is not None:
            text += f" · {_size(available)} available"
    else:
        container = next((entry for entry in series if entry.get("code") == "DOCKER_CONTAINER_STATS"
                          and str(entry.get("item") or "").endswith(":memory")), None)
        # DOCKER_CONTAINER_STATS separates its fields with spaces, not commas: read the pair itself.
        usage = _CONTAINER_MEMORY.search(str((container or {}).get("message") or ""))
        if usage:
            text = f"{usage.group(1)} / {usage.group(2)} used / container limit"
    # `sql_memory_held_mb` is the collector's own choice of figure (committed on Linux, where the
    # process figure read 4.1 GB beside a Total Server Memory of 21.2); rows from before 0.34.0
    # carry only the process figure.
    held_mb = (as_float(sql_mem.get("sql_memory_held_mb"))
               or as_float(sql_mem.get("sql_physical_memory_in_use_mb")))
    if text and held_mb:
        text += f" · SQL Server holds {_size(held_mb / 1024)}"
    return text


def _volume_key(item: str) -> str:
    return str(item or "").strip().rstrip("\\/").rstrip(":").upper()


def _volume_sizes(entry: dict) -> tuple[float, float, float] | None:
    """``(used, total, free)`` GB of a volume row, from whichever fields its collector wrote."""
    fields = _message_fields(entry)
    total = as_float(fields.get("total_gb"))
    free = as_float(fields.get("free_gb"))
    used = as_float(fields.get("used_gb"))
    if not total:
        return None
    if used is None and free is not None:
        used = total - free
    if free is None and used is not None:
        free = total - used
    if used is None or free is None:
        return None
    return used, total, free


def _disk_capacity(worst: dict, members: list[tuple[int, dict]]) -> str:
    """The worst volume as ``used / total`` and its free space, then every volume together."""
    volumes: dict[str, tuple[str, tuple[float, float, float]]] = {}
    seen_at: dict[str, float] = {}
    # The newer reading of a volume wins when the other collector has stopped (audit 2026-11-05,
    # B5). A container's volume is named by its mount, not by the
    # item that carries the container's name.
    for code in ("OS_DISK_USAGE", "STORAGE_DISK_FREE_SPACE", "DOCKER_CONTAINER_STATS"):
        for _priority, entry in members:
            if entry.get("code") != code:
                continue
            sizes = _volume_sizes(entry)
            key = _volume_key(entry.get("item"))
            name = _message_fields(entry).get("mount") or str(entry.get("item") or "")
            at = float(entry.get("lastAt") or 0)
            # A later collector replaces an earlier one only when that one has stopped (older by
            # more than two hours); within one cycle OS_DISK_USAGE, read first, is kept.
            if sizes and key and (key not in volumes or at - seen_at[key] > 2 * 3600):
                volumes[key] = (name, sizes)
                seen_at[key] = at
    if not volumes:
        return ""
    worst_key = _volume_key(worst.get("item"))
    if worst_key in volumes:
        name, (used, total, free) = volumes[worst_key]
        text = f"{name} {_size_pair(used, total)} used / total · {_size(free)} free"
    else:
        # The worst reading is a file, not a volume - the fullest volume is still worth a line.
        name, (used, total, free) = max(volumes.values(), key=lambda pair: pair[1][0] / pair[1][1])
        text = f"fullest volume {name} {_size_pair(used, total)} · {_size(free)} free"
    if len(volumes) > 1:
        all_free = sum(sizes[2] for _name, sizes in volumes.values())
        all_total = sum(sizes[1] for _name, sizes in volumes.values())
        text += f" · {len(volumes)} volumes, {_size(all_free)} free of {_size(all_total)}"
    return text


def _backup_area_state(backup: dict | None) -> dict:
    """The Backup tile, from the per-database policy rather than from a metric row.

    See :mod:`dbabrain.lib.backup_policy`. The old rule took the largest number out of mixed
    FULL/DIFF/LOG/job rows, which on the ERP FCI produced ``OK 3185 hours_since_last_backup``
    under a stated 48-hour threshold — the number was the finding and the status was the wrong
    row's.
    """
    if not backup or not backup.get("databases"):
        return {"status": "UNKNOWN", "value": "not collected", "detail": "",
                "sourceCode": "", "sourceItem": "", "collectedAt": None, "stale": False,
                "downgradedFrom": ""}
    summary = backup.get("summary") or {}
    return {
        "status": str(summary.get("status") or "UNKNOWN"),
        "value": f"{summary.get('compliant', 0)}/{summary.get('eligible', 0)} compliant",
        "detail": str(summary.get("reason") or ""),
        "sourceCode": "BACKUP_LAST_RESULT", "sourceItem": "policy",
        "collectedAt": summary.get("collectedAt"), "stale": False,
        # The backup tile is decided by policy, not by a metric row, so no severity_map ever
        # reaches it. Stated rather than left absent, so every tile has the same shape.
        "downgradedFrom": "",
    }


_age_hint = health_model.age_hint


def metric_action_for(code: str, *, platform: object = "", db_type: object = "") -> tuple[str, str]:
    """``(what to do, whom it is for)`` for ``code`` on a server of that operating system and engine.

    The engine's own sentence first, then the operating system's, then the metric's general one,
    then its area's note. *Whom it is for* is the platform or engine a specific sentence was chosen
    by (``Linux``, ``SQL Server``) and ``""`` for a general one - what the fleet page labels the
    sentences with when one card holds servers of several.

    A metric whose only sentences are per platform, on a server whose platform nobody read, gets
    every one of them, each named: the page does not guess which applies.
    """
    from dbabrain.lib import engine_sections

    stated = metric_actions().get(code) or {}
    engine, system = _engine_key(db_type), _platform_key(platform)
    if engine and stated.get("by_db_type", {}).get(engine):
        return stated["by_db_type"][engine], engine_sections.section_label(engine)
    if system and stated.get("by_platform", {}).get(system):
        return stated["by_platform"][system], PLATFORM_NAMES[system]
    if stated.get("action"):
        return stated["action"], ""
    every = [f"{PLATFORM_NAMES.get(key, key)}: {sentence}" for key, sentence in sorted(stated.get("by_platform", {}).items())]
    every += [f"{engine_sections.section_label(key)}: {sentence}"
              for key, sentence in sorted(stated.get("by_db_type", {}).items())]
    if every:
        return " ".join(every), ""
    area = next((spec for spec in AREAS if code in AREA_CODES[spec["key"]]), None)
    note = page_texts.area_text(area["key"], "note", db_type=db_type, platform=platform) if area else ""
    return note or GENERIC_ACTION, ""


def metric_action(code: str, *, platform: object = "", db_type: object = "") -> str:
    """What to do about ``code`` on that platform and engine. Shared with the fleet page."""
    return metric_action_for(code, platform=platform, db_type=db_type)[0]


def build_problems(series: list[dict], *, now: int | None = None, platform: object = "",
                   db_type: object = "") -> list[dict]:
    """What is not OK right now, **grouped by metric**, worst first, each saying what to do.

    The grouping itself lives in :func:`dbabrain.lib.health_model.group_findings`, because the
    fleet page's Priority Attention is built from exactly the same structure — that is what stops
    the two pages describing the same server differently.
    """
    now = now if now is not None else int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    return health_model.group_findings([
        {
            "code": entry["code"],
            "label": entry["label"],
            "item": entry["item"],
            "value": _value_text(entry),
            "severity": series_severity(entry),
            # A windowed metric says first which window decided and what the others read: the
            # collector's own message under it is the since-boot figure (report_windows).
            "message": (f"{entry['windowSummary']} | {entry.get('message', '')}"
                        if entry.get("windowSummary") else entry.get("message", "")),
            "lastText": entry.get("lastText", ""),
            **_action_fields(entry["code"], platform=platform, db_type=db_type),
            "collectedAt": entry.get("lastAt"),
        }
        for entry in series
    ], now=now)


def _action_fields(code: str, *, platform: object, db_type: object) -> dict:
    """A finding's ``action`` and ``actionFor`` - the sentence for this server, and whom it was chosen for."""
    sentence, whom = metric_action_for(code, platform=platform, db_type=db_type)
    return {"action": sentence, "actionFor": whom}


def build_timeline(series: list[dict], *, limit: int = 14) -> list[dict]:
    """When each problem started, and whether it has cleared.

    Answers "is this getting better or worse" — the one question a table of current values
    cannot. Built from the status of every stored sample, so a warning that came and went at
    03:00 is still visible at 09:00.
    """
    incidents = []
    for entry in series:
        # A weekly metric has one sample per item, so every fragmented index would open its
        # own "ongoing incident" and bury the events this section exists for. Its state is
        # already in Problems; an incident needs a before and an after.
        if entry.get("lowCadence"):
            continue
        open_incident = None
        for epoch, _value, status in entry["points"]:
            severity = severity_of(status)
            if severity in ("CRITICAL", "WARNING"):
                if open_incident is None:
                    open_incident = {"label": entry["label"], "item": entry["item"],
                                     "severity": severity, "start": epoch, "end": None, "samples": 1}
                else:
                    open_incident["samples"] += 1
                    if SEVERITY_RANK[severity] > SEVERITY_RANK[open_incident["severity"]]:
                        open_incident["severity"] = severity   # it got worse while it was open
            elif open_incident is not None:
                open_incident["end"] = epoch
                incidents.append(open_incident)
                open_incident = None
        if open_incident is not None:
            incidents.append(open_incident)      # still open at the end of the window

    # Ongoing and cleared get their own share of the list. Sorting "ongoing first, then by
    # start" filled all 14 slots on the ERP host with the same 14 standing security warnings,
    # so every incident that came *and went* — a TempDB volume hitting 100% — was cut, and the
    # section meant to show "better or worse" showed only "still bad".
    ongoing = sorted((i for i in incidents if i["end"] is None), key=lambda i: -i["start"])
    cleared = sorted((i for i in incidents if i["end"] is not None), key=lambda i: -i["end"])
    half = max(1, limit // 2)
    keep_ongoing = ongoing[:max(half, limit - len(cleared))]
    keep_cleared = cleared[:max(half, limit - len(keep_ongoing))]
    return keep_ongoing + keep_cleared


def build_health(series: list[dict], problems: list[dict], *, now: int,
                 freshness: dict | None = None) -> dict:
    """The verdict. UNKNOWN when the data is too old to describe the present.

    The score is a blunt instrument on purpose: a critical costs 20 and a warning 5, so one
    critical can never be hidden by a wall of green. It is a shorthand for "how bad", not a
    measurement.
    """
    last_at = max((entry["lastAt"] for entry in series), default=None)
    age = None if last_at is None else max(0, now - last_at)
    stale = last_at is None or age > STALE_AFTER_SECONDS

    # Counted per failing item, not per group: grouping is how the page is *read*, but "3
    # databases offline" must not score the same as one.
    items = [row for problem in problems for row in problem["items"]]
    critical = sum(1 for row in items if row["severity"] == "CRITICAL")
    warning = sum(1 for row in items if row["severity"] == "WARNING")

    if stale:
        status = "UNKNOWN"
    elif critical:
        status = "CRITICAL"
    elif warning:
        status = "WARNING"
    else:
        status = "HEALTHY"

    score = max(0, 100 - min(60, 20 * critical) - min(30, 5 * warning))
    if stale:
        score = min(score, 40)   # a server nobody is collecting from is not a healthy server

    failed = list((freshness or {}).get("failed") or [])
    late = list((freshness or {}).get("late") or [])
    return {
        "status": status,
        "score": score,
        "critical": critical,
        "warning": warning,
        "lastCollected": last_at,
        "ageSeconds": age,
        "stale": stale,
        "staleAfterSeconds": STALE_AFTER_SECONDS,
        "seriesCount": len(series),
        # Monitoring's own health, kept separate from the server's. A page saying HEALTHY while
        # four of its metrics have not returned in two days is describing what it can still see,
        # and it has to say how much that is.
        "failedMetrics": failed,
        "lateMetrics": late,
        "metricsSeen": (freshness or {}).get("seen"),
        "metricsExpected": (freshness or {}).get("expected"),
        "metricsNotCollected": [entry["code"] for entry in (freshness or {}).get("notCollected") or []],
    }
