"""``server-metrics.html``: the tables beside the charts: linked servers, volumes, capacity, Query Store (findings and coverage), databases, jobs, access.

Split out of ``reports/server_report.py`` on 2026-10-03 (Q11,
``audits/20261002_audit_typed_requests_and_errors.md`` section 5). ``server_report`` re-exports
every name, so no import changes.
"""

from __future__ import annotations
from db_ops.lib.coerce import as_float
import re
from db_ops.lib import data_sources
from db_ops.lib import backup_policy, capacity_forecast
from db_ops.lib import health_model
from db_ops.reports import inventory_health
from db_ops.reports.server_series import QUERY_STORE_CODE, SEVERITY_RANK, _int_or_none, _int_or_zero, _message_kv, severity_of


LINKED_SERVER_CODE = "LINKED_SERVER_STATUS"

#: What to do about one linked server, keyed by (is it usable, does any code call it).
#: The pair is the whole point: neither half is actionable alone. A dead linked server nothing
#: references is cleanup; the same server with a procedure behind it is an outage waiting for
#: that procedure to run. A healthy one nothing references is the opposite question — why is it
#: still configured, with credentials, pointing at a host somebody has to keep alive?
_LINKED_VERDICTS = {
    (False, True): ("FIX", "critical",
                    "Unreachable and code calls it — this fails the next time that code runs."),
    (False, False): ("DROP", "warning",
                     "Unreachable and nothing references it: dead configuration, safe to remove."),
    (True, True): ("KEEP", "ok", "Answers, and code depends on it."),
    (True, False): ("REVIEW", "warning",
                    "Answers, but nothing references it — remove it or find out who uses it "
                    "outside the database."),
}

#: A failure state names the machine to go to, which is the part an operator gets wrong most
#: often. CREDENTIAL_UNREADABLE especially: the remote host is usually fine.
_LINKED_FAILURE_FIX = {
    "CREDENTIAL_UNREADABLE": "LOCAL fix — this instance cannot decrypt the stored remote login "
                             "(service master key changed, or it was restored elsewhere). "
                             "Re-enter the linked server login here; the remote host is likely fine.",
    "LOGIN_REJECTED": "The remote login is rejected (bad, expired, or must-change password). "
                      "Renew the credential on the remote server, then re-enter it here.",
    "UNREACHABLE": "The remote host did not answer — check that it is up, that the instance is "
                   "listening, and that the network/firewall path still exists.",
    "ERROR": "sp_testlinkedserver failed for another reason; the error text is on the row.",
}


def build_linked_servers(rows_in: list[dict]) -> list[dict]:
    """One row per linked server: is it usable, does anything call it, and so what to do.

    Its own section rather than a chart or a status chip. A linked server is not a time series —
    it is reachable or it is not — and the fleet's real question about one is never "what was it
    doing on Tuesday" but "should this still exist". Answering that needs reachability and usage
    on the same row, which is exactly what the metric already reports and what a chip listing
    ``REACHABLE`` could not show.

    Built from the **raw store rows**, like ``backup`` and ``freshness`` and for the same reason:
    the chart pipeline drops anything with fewer than ``MIN_POINTS`` samples, which is right for a
    session id and wrong for this. A linked server that has only been collected twice is not a
    thin series to be discarded — it is a linked server, and the two 2008 R2 hosts whose metric
    had been failing for days had exactly one sample each the moment it was fixed. Passing
    ``series`` here showed 1 linked server across the estate where there are 21.
    """
    rows: list[dict] = []
    for entry in rows_in:
        if str(entry.get("metric_code") or entry.get("code") or "") != LINKED_SERVER_CODE:
            continue
        name = str(entry.get("metric_item") or entry.get("item") or "").strip()
        # A failed collection is stored as a row of this metric with no item. It is a monitoring
        # problem (already reported as one), not a nameless linked server to recommend dropping.
        if not name:
            continue
        fields = _message_kv(entry.get("message", ""))
        usable = str(fields.get("usable", "")).strip().lower() == "yes"
        # The message's own `failure=` decides, not the displayed value: it is the field the SQL
        # sets deliberately (CREDENTIAL_UNREADABLE / LOGIN_REJECTED / UNREACHABLE / ERROR), and it
        # is what picks which machine the operator is sent to. lastText is only a fallback for a
        # row whose message predates that field.
        failure = str(fields.get("failure") or "").strip().upper()
        # The SQL appends a human explanation straight after the state on CREDENTIAL_UNREADABLE
        # ("CREDENTIAL_UNREADABLE (LOCAL problem: ...)"), and _message_kv reads to the next comma,
        # so the whole sentence arrived as the state. Keep the token, drop the prose.
        failure = failure.split("(")[0].split()[0] if failure else ""
        if failure in ("", "NONE"):
            failure = str(entry.get("metric_value") or entry.get("lastText") or "").strip().upper()
        state = "REACHABLE" if usable else (failure or "ERROR")
        procs = _int_or_zero(fields.get("referenced_by_procedures"))
        objects = _int_or_zero(fields.get("referenced_by_objects"))
        # "Referenced" counts every object kind, not only procedures: a view over a four-part
        # name breaks exactly as loudly as a procedure does. The metric's own severity uses the
        # procedure count alone, which is why a view-only reference read as droppable.
        referenced = max(procs, objects) > 0
        verdict, level, why = _LINKED_VERDICTS[(usable, referenced)]
        # CREDENTIAL_UNREADABLE means the test could not be RUN - this instance cannot decrypt the
        # stored remote login - not that the target is dead. Never recommend dropping something
        # that was never actually tested: on 192.0.2.111 one service-master-key problem made
        # all 8 linked servers unusable at once, and five of them read as "safe to remove".
        if not usable and failure == "CREDENTIAL_UNREADABLE":
            verdict = "FIX"
            level = "critical" if referenced else "warning"
            why = ("Not tested: this instance could not decrypt the stored remote login, so "
                   "whether the target answers is unknown. Do not drop it on this evidence.")
        rows.append({
            "name": name,
            "usable": usable,
            "state": state,
            "verdict": verdict,
            "level": level,
            "why": why + ("" if usable else " " + _LINKED_FAILURE_FIX.get(
                state, _LINKED_FAILURE_FIX["ERROR"])),
            "procedures": procs,
            "objects": objects,
            "databases": _int_or_zero(fields.get("databases_referencing")),
            "product": str(fields.get("product") or "").strip() or "?",
            "provider": str(fields.get("provider") or "").strip() or "?",
            "dataSource": str(fields.get("data_source") or "").strip() or "?",
            "sample": str(fields.get("sample") or "").strip(),
            "error": str(fields.get("error") or "").strip(),
            "collectedAt": entry.get("collected_at") or entry.get("lastAt"),
        })
    # Worst first, then the ones that cost the most to keep: a DROP with 40 objects behind it is
    # a bigger decision than a DROP with none.
    order = {"FIX": 0, "DROP": 1, "REVIEW": 2, "KEEP": 3}
    rows.sort(key=lambda r: (order[r["verdict"]], -r["objects"], r["name"].casefold()))
    return rows


#: Volume free space, in GB — the series a "when does this run out" question is answered from.
CAPACITY_CODE = "STORAGE_DISK_FREE_SPACE"

#: The OS view of the volumes, and the engine's. Both are collected, and each covers a host the
#: other does not: ``OS_DISK_USAGE`` is the only source on a host with no database on it (the
#: Ubuntu worker, the four Service Fabric nodes), and ``STORAGE_DISK_FREE_SPACE`` is the only one
#: on an instance whose ``disabled_collector_types`` turns ``cmd`` off (192.0.2.253).
OS_VOLUME_CODE = "OS_DISK_USAGE"
VOLUME_SECTION_CODES = [OS_VOLUME_CODE, CAPACITY_CODE]

#: ``OS_DISK_USAGE`` items that are storage *activity*, not a volume. They share the metric code
#: with the mount points and would otherwise arrive in the volume table as rows with no size.
_NOT_A_VOLUME = {"disk_read_kbps", "disk_write_kbps", "disk_iops", "disk_queue_length",
                 "disk_perf_counters", "disk_usage"}


def _volume_key(name: str) -> str:
    """``C:\\`` and ``C:`` are one volume; ``/`` and ``/boot`` are two.

    The two collectors spell the same Windows drive differently — ``sys.dm_os_volume_stats``
    returns the mount point ``C:\\`` while ``[System.IO.DriveInfo]`` is trimmed to ``C:`` — so
    without this the table lists every Windows volume twice, once per source. Only a *trailing*
    separator is dropped, and never the whole name: on Linux the root mount **is** ``/``.
    """
    trimmed = str(name or "").strip().rstrip("\\/")
    return trimmed or str(name or "").strip()


def _volume_from_os_row(row: dict) -> dict | None:
    """One mount point as the OS sees it: size, use, filesystem, and the device behind it."""
    item = str(row.get("metric_item") or "").strip()
    if not item or item in _NOT_A_VOLUME:
        return None
    fields = _message_kv(str(row.get("message") or ""))
    total = as_float(fields.get("total_gb"))
    if total is None or total <= 0:
        # No size means this is not a volume row after all (an UNKNOWN placeholder, or an item a
        # future collector adds). Guessing a size here is how a table of capacities gets a row
        # that is not a capacity.
        return None
    free = as_float(fields.get("free_gb"))
    used = as_float(fields.get("used_gb"))
    if used is None and free is not None:
        used = total - free
    if free is None and used is not None:
        free = total - used
    return {
        "name": item,
        "source": "os",
        "status": str(row.get("status") or "OK"),
        "totalGB": round(total, 2),
        "usedGB": round(used, 2) if used is not None else None,
        "freeGB": round(free, 2) if free is not None else None,
        "usedPct": as_float(row.get("metric_value")),
        "freePct": as_float(fields.get("free_percent")),
        "filesystem": str(fields.get("filesystem") or ""),
        # Windows names the volume, Linux names the device behind it. One column, because the
        # question both answer is the same: which physical thing am I looking at.
        "device": str(fields.get("device") or fields.get("label") or ""),
        "collectedAt": str(row.get("collected_at") or ""),
    }


def _volume_from_engine_row(row: dict) -> dict | None:
    """One volume as the database engine sees it — the fallback where the OS metric cannot run.

    The size is allowed to be unknown here, and the row still counts. On a SQL Server too old for
    ``sys.dm_os_volume_stats`` the metric falls back to ``xp_fixeddrives``, which returns free
    megabytes and nothing else (``total_gb=unknown`` — 192.0.2.253 reports all three of its
    volumes that way). Dropping those rows for having no total would leave that host with no
    volume table at all, having thrown away the one column that decides anything: free space.
    """
    fields = _message_kv(str(row.get("message") or ""))
    item = str(row.get("metric_item") or fields.get("drive") or "").strip()
    total = as_float(fields.get("total_gb"))
    if total is not None and total <= 0:
        total = None
    free = as_float(fields.get("free_gb"))
    if free is None:
        free = as_float(row.get("metric_value"))
    if not item or (total is None and free is None):
        return None
    used = total - free if (total is not None and free is not None) else None
    used_pct = as_float(fields.get("used_pct"))
    if used_pct is None and used is not None and total:
        used_pct = used / total * 100
    return {
        "name": item,
        "source": "engine",
        "status": str(row.get("status") or "OK"),
        "totalGB": round(total, 2) if total is not None else None,
        "usedGB": round(used, 2) if used is not None else None,
        "freeGB": round(free, 2) if free is not None else None,
        "usedPct": round(used_pct, 2) if used_pct is not None else None,
        "freePct": as_float(fields.get("free_pct")),
        # The engine reads free space, not the file system it sits on.
        "filesystem": "",
        "device": "",
        "collectedAt": str(row.get("collected_at") or ""),
    }


def build_volumes(rows: list[dict]) -> dict:
    """How large each volume is and how full — the numbers the Disk space tile reduces to one.

    The tile answers "is anything nearly full"; a machine with a 2 TB data volume at 28% and a
    200 GB system volume at 14% got one green tile reading ``28%`` and no way to see either size.
    Every figure here was already collected — ``total_gb``, ``used_gb``, ``free_gb`` ride in the
    OS collector's message on both Windows and Linux — and none of it was on the page: the
    percentage was charted and the capacity it was a percentage *of* was thrown away.

    Both sources are read and merged per volume (see :data:`VOLUME_SECTION_CODES`), OS first
    because it carries the file system and the device. Hosts that have only one of the two are
    the normal case, not the exception.
    """
    volumes: dict[str, dict] = {}
    for parse, code in ((_volume_from_os_row, OS_VOLUME_CODE),
                        (_volume_from_engine_row, CAPACITY_CODE)):
        for row in rows:
            if str(row.get("metric_code") or "") != code:
                continue
            volume = parse(row)
            if volume is None:
                continue
            volumes.setdefault(_volume_key(volume["name"]), volume)

    listed = sorted(volumes.values(),
                    key=lambda v: (-(v["usedPct"] or 0), v["name"].casefold()))
    if not listed:
        return {"volumes": [], "summary": {}}
    # Free space is summed over every volume that reported one — it is the host's actual headroom
    # and is meaningful whether or not the sizes are known. The *ratio* is not: a host where one
    # volume reports its size and two do not has no honest "58% used", so used/total are stated
    # only when nothing is missing, and ``unsized`` says how many were left out.
    unsized = [v for v in listed if v["totalGB"] is None]
    total = sum(v["totalGB"] for v in listed if v["totalGB"] is not None)
    free = sum(v["freeGB"] for v in listed if v["freeGB"] is not None)
    complete = not unsized and total > 0
    return {
        "volumes": listed,
        "summary": {
            "count": len(listed),
            "unsized": len(unsized),
            "freeGB": round(free, 2),
            "totalGB": round(total, 2) if complete else None,
            "usedGB": round(total - free, 2) if complete else None,
            "usedPct": round((total - free) / total * 100, 1) if complete else None,
            "fullest": listed[0]["name"],
            "fullestPct": listed[0]["usedPct"],
            "warning": sum(1 for v in listed if severity_of(v["status"]) == "WARNING"),
            "critical": sum(1 for v in listed if severity_of(v["status"]) == "CRITICAL"),
        },
    }


def build_capacity(rows: list[dict], *, server_id: str = "") -> list[dict]:
    """Per volume: how fast it is draining and when it runs out.

    Reads the raw store rows rather than the charted series for the same reason ``backup`` and
    ``linkedServers`` do — the chart pipeline drops and caps series for display, and a forecast
    wants every sample it can get.

    The analysis itself lives in :mod:`db_ops.lib.capacity_forecast`, so the fleet page and this
    page cannot disagree about which volume is about to fill.
    """
    by_item: dict[str, list[tuple[str, str]]] = {}
    for row in rows:
        if str(row.get("metric_code") or "") != CAPACITY_CODE:
            continue
        if str(row.get("metric_unit") or "").strip().upper() != "GB":
            continue
        item = str(row.get("metric_item") or "").strip()
        if not item:
            continue
        by_item.setdefault(item, []).append((row.get("collected_at"), row.get("metric_value")))

    # The read is data_sources' (the one reader of data/); the forecasting is lib's.
    policy = data_sources.load_capacity_policy()
    out: list[dict] = []
    for item, samples in by_item.items():
        reserve = capacity_forecast.reserve_gb(policy, server_id=server_id, item=item)
        result = capacity_forecast.forecast(samples, floor=reserve)
        days = result.get("days_to_threshold")
        out.append({
            "item": item,
            "status": capacity_forecast.severity_for(days, policy, server_id=server_id, item=item),
            "freeGb": result.get("latest"),
            "perDay": result.get("per_day"),
            "daysToFull": days,
            "reserveGb": reserve,
            "points": result.get("points", 0),
            "spanHours": result.get("span_hours", 0),
            "resets": result.get("resets", 0),
            "enough": result.get("status") == "ok",
            "text": capacity_forecast.describe(result),
        })
    # Soonest to run out first; volumes with no date (flat, growing, or unknown) after them.
    out.sort(key=lambda r: (r["daysToFull"] is None, r["daysToFull"] if r["daysToFull"] is not None else 0))
    return out



#: The metric whose findings the Query Store findings table lists - one row per query and plan.
QUERY_STORE_ISSUES_CODE = "QUERY_STORE_QUERY_ISSUES"

#: How far back the findings table reaches. A day, at the operator's word (2026-10-02): the newest
#: collection alone loses track of a regression the moment it clears - the 03:00-13:28 plan flip
#: of that morning would have left nothing on the page by 14:00 - and a day is what the next
#: morning's reader needs to see what happened overnight.
QUERY_STORE_FINDINGS_HOURS = 24

_QS_NUMBERS = ("recentExecutions", "recentCpuSec", "recentAvgCpuMs", "cpuRatio", "maxDurationSec",
               "maxLogicalReads", "queryBadCpuSec")


def _query_store_finding(row: dict) -> dict:
    fields = _message_kv(row.get("message"))
    recent_avg = as_float(fields.get("recent_avg_cpu_sec"))
    best_avg = as_float(fields.get("best_avg_cpu_sec"))
    return {
        "severity": health_model.severity_of(str(row.get("status") or "")),
        "kind": fields.get("issue_type") or str(row.get("metric_item") or ""),
        "database": fields.get("db_name") or "",
        "queryId": fields.get("query_id") or "",
        "planId": fields.get("plan_id") or "",
        "otherPlanId": fields.get("best_cpu_plan_id") or fields.get("old_plan_id") or "",
        "recentExecutions": as_float(fields.get("recent_executions")),
        "recentCpuSec": as_float(fields.get("recent_total_cpu_sec")),
        "recentAvgCpuMs": None if recent_avg is None else round(recent_avg * 1000, 2),
        "bestAvgCpuMs": None if best_avg is None else round(best_avg * 1000, 3),
        "cpuRatio": as_float(fields.get("cpu_ratio")),
        # The query's bad plans together - what the frequency finding is judged on since 2026-10-03:
        # a statement that flips between several bad plans burns as much as one bad plan does.
        "queryBadPlans": as_float(fields.get("query_bad_plan_count")),
        "queryBadCpuSec": as_float(fields.get("query_bad_cpu_sec")),
        "maxDurationSec": as_float(fields.get("max_duration_sec")),
        "maxLogicalReads": as_float(fields.get("max_logical_reads")),
        "lastExecution": fields.get("last_execution_time") or "",
    }


def build_query_store_findings(rows: list[dict]) -> dict:
    """``QUERY_STORE_QUERY_ISSUES`` over the last :data:`QUERY_STORE_FINDINGS_HOURS`, one row per
    query plan: when it was first and last reported, how often, its worst severity and its peak
    numbers, and whether the newest collection still reports it.

    The chart pipeline cannot show these, and on 2026-10-02 it did not: the metric's
    ``metric_item`` is the *kind* of finding (``query_store_plan_regressed_frequent``), not the
    query, so three regressed queries in one run became one series carrying one query's message,
    a kind seen for the first time - fewer than ``MIN_POINTS`` samples - was not drawn at all while
    the fleet page already carried a WARNING card for it, and a cleared regression left no trace.
    A finding is about one statement, so it gets a table; and it keeps a day, so the morning after
    a plan flip still shows what happened overnight (the operator, 2026-10-02).

    ``rows`` is every row of the metric in the window. An itemless row is the collector saying it
    found nothing; it still dates the newest collection, which is what *now* is measured against.
    """
    newest = ""
    collected = False
    groups: dict[tuple, dict] = {}
    for row in sorted(rows, key=lambda r: str(r.get("collected_at") or "")):
        if str(row.get("metric_code") or "") != QUERY_STORE_ISSUES_CODE:
            continue
        collected = True
        stamp = str(row.get("collected_at") or "")
        newest = max(newest, stamp)
        if not str(row.get("metric_item") or "").strip():
            continue
        finding = _query_store_finding(row)
        key = (finding["database"], finding["queryId"], finding["planId"], finding["kind"])
        group = groups.get(key)
        if group is None:
            group = groups[key] = dict(finding, firstSeen=stamp, lastSeen=stamp, reports=0,
                                       worst=finding["severity"])
            for name in _QS_NUMBERS:
                group["peak" + name[0].upper() + name[1:]] = finding[name]
        group["reports"] += 1
        group["lastSeen"] = stamp
        if SEVERITY_RANK.get(finding["severity"], 0) > SEVERITY_RANK.get(group["worst"], 0):
            group["worst"] = finding["severity"]
        for name in _QS_NUMBERS:
            peak = "peak" + name[0].upper() + name[1:]
            if finding[name] is not None and (group[peak] is None or finding[name] > group[peak]):
                group[peak] = finding[name]
        # The latest reading of each field, so a row still happening reads as it is now.
        group.update({name: value for name, value in finding.items() if value not in (None, "")})
    findings = list(groups.values())
    for finding in findings:
        finding["now"] = finding["lastSeen"] == newest
    # Three stable sorts, least important first: still happening before cleared, worst first, the
    # most recently seen first, then the heaviest.
    findings.sort(key=lambda f: (-(f["peakRecentCpuSec"] or 0), -(f["peakMaxDurationSec"] or 0)))
    findings.sort(key=lambda f: f["lastSeen"], reverse=True)
    findings.sort(key=lambda f: (not f["now"], -SEVERITY_RANK.get(f["worst"], 0)))
    current = [f for f in findings if f["now"]]
    return {
        "findings": findings,
        "summary": {
            "collected": collected,
            "windowHours": QUERY_STORE_FINDINGS_HOURS,
            "count": len(findings),
            "now": len(current),
            "cleared": len(findings) - len(current),
            "criticalNow": sum(1 for f in current if f["severity"] == "CRITICAL"),
            "warningNow": sum(1 for f in current if f["severity"] == "WARNING"),
            "asOf": newest,
        },
    }


def build_query_store(rows: list[dict]) -> dict:
    """Per-database Query Store state and settings for this server.

    Its own section, not a status chip, for the reason the access and linked-server tables have
    one: the question an operator brings here is "can I still investigate yesterday's slowdown on
    this database", and the answer is a per-database table with the settings beside it — a chip
    saying WARNING cannot carry it.

    **The state shown is the actual one.** A Query Store that reaches its ``max_storage_size``
    flips itself to READ_ONLY and stops capturing, while ``sys.databases.is_query_store_on`` still
    reads 1: reporting the configured value calls that database covered when it has captured
    nothing since the day it filled up. ``storagePct`` is what predicts the next one.

    Built from the raw store rows like the access and linked-server sections: this is an
    inventory collected once a day, so the chart pipeline drops it for having too few samples.
    """
    databases: list[dict] = []
    for entry in rows:
        if str(entry.get("metric_code") or entry.get("code") or "") != QUERY_STORE_CODE:
            continue
        name = str(entry.get("metric_item") or entry.get("item") or "").strip()
        if not name:
            continue                  # a failed collection is stored itemless; not a database
        detail = inventory_health.build_query_store_entry(entry)
        databases.append({
            "name": name,
            "state": detail.get("state") or "",
            "on": bool(detail.get("on")),
            "capturing": bool(detail.get("capturing")),
            "desiredState": detail.get("desired_state") or "",
            "actualState": detail.get("actual_state") or "",
            "readonlyReason": detail.get("readonly_reason"),
            "readonlyReasonDesc": detail.get("readonly_reason_desc") or "",
            "storageMB": detail.get("current_storage_mb"),
            "maxStorageMB": detail.get("max_storage_mb"),
            "storagePct": detail.get("storage_used_pct"),
            "captureMode": detail.get("capture_mode") or "",
            "cleanupMode": detail.get("cleanup_mode") or "",
            "waitStatsCapture": detail.get("wait_stats_capture") or "",
            "staleQueryThresholdDays": detail.get("stale_query_threshold_days"),
            "intervalLengthMinutes": detail.get("interval_length_minutes"),
            "flushIntervalSeconds": detail.get("flush_interval_seconds"),
            "maxPlansPerQuery": detail.get("max_plans_per_query"),
            "issueType": detail.get("issue_type") or "",
            "offReason": detail.get("off_reason") or "",
            "offReasonDesc": detail.get("off_reason_desc") or "",
            "status": detail.get("status") or "OK",
            "asOf": detail.get("as_of") or "",
        })

    # Not capturing first: the table exists to answer "where can I not investigate", and on an
    # instance with 13 databases that answer must not be somewhere in the middle of the list.
    databases.sort(key=lambda r: (r["capturing"], r["name"].casefold()))
    return {
        "databases": databases,
        "summary": {
            "databases": len(databases),
            "capturing": sum(1 for r in databases if r["capturing"]),
            "off": sum(1 for r in databases if r["state"] == "OFF"),
            # Switched on and still not recording — the failure the configured flag hides.
            "onButNotCapturing": sum(
                1 for r in databases if r["on"] and not r["capturing"]),
            # Not capturing because it broke, rather than because somebody turned it off. The
            # two need different actions, so the header counts them apart.
            "stoppedOnItsOwn": sum(
                1 for r in databases
                if r["offReason"] in ("ERROR_STATE", "SIZE_LIMIT_REACHED", "STOPPED_WHILE_ENABLED")),
            "asOf": max((r["asOf"] for r in databases if r["asOf"]), default=""),
        },
    }


#: What the per-database section reads. The same codes the fleet page's Server Detail uses, so the
#: two tables cannot disagree about the same database — the rows are built by the same function.
DATABASE_SECTION_CODES = [
    "DATABASE_STATUS", "DATABASE_CONFIG", "DATABASE_CHECKDB",
    "DATABASE_DATA_SIZE", "DATABASE_LOG_SIZE", "LOG_FILE_SPACE", "LOG_REUSE_WAIT",
    QUERY_STORE_CODE, "DATABASE_USER_PERMISSIONS",
    "BACKUP_AGE", *backup_policy.BACKUP_LAST_RESULT_CODES,
]


def build_databases(code_map, backup_result=None) -> dict:
    """The instance's databases, one row each.

    The page had every per-database fact — size, log usage, recovery model, CHECKDB age, backup
    ages, Query Store — and no table that put them on one line per database. A reader asking "what
    is on this server and how is each one doing" had to read six sections and join them by eye.

    **System databases are included.** They are the reason this section was asked for: master,
    model and msdb each carried a CHECKDB warning that no row on any page could be attached to,
    because DATABASE_STATUS excludes them and the database list was built from it alone (see
    :data:`inventory_health.SYSTEM_DATABASE_NAMES`).
    """
    rows = inventory_health.build_database_rows(
        inventory_health.build_database_health(code_map),
        inventory_health.build_backup_by_database(code_map, backup_result),
    )
    protected = sum(1 for row in rows if (row.get("backupStatus") or "") == "OK")
    return {
        "databases": rows,
        "summary": {
            "count": len(rows),
            # The same definition the fleet page counts with. Oracle reports its open_mode
            # ("READ WRITE"), never "ONLINE", so a literal comparison here said 0 of 1 online on
            # an instance that was open and serving.
            "online": sum(1 for row in rows if inventory_health.is_database_online(row["state"])),
            # "never" and "" both mean no known-good CHECKDB has ever been recorded.
            "neverCheckdb": sum(1 for row in rows if row["checkdb"] in ("", "never")),
            "backupOk": protected,
            "backupGraded": sum(1 for row in rows if row.get("backupStatus")),
            "asOf": max((row["asOf"] for row in rows if row["asOf"]), default=""),
        },
    }


JOB_INVENTORY_CODE = "SQL_AGENT_JOB_INVENTORY"

#: The keys every engine's job inventory writes, in the order they appear. Used as delimiters:
#: a value ends where the next key of this set begins, which is what lets ``command`` hold the
#: commas and equals signs that a job step is made of.
_JOB_FIELDS = (
    "enabled", "last_outcome", "category", "owner", "schema", "job_class", "steps",
    "schedule", "next_run", "last_run", "max_duration_seconds_7d", "last_duration",
    "runs_7d", "succeeded_7d", "failed_7d", "runs_total", "failed_total",
    "consecutive_failures", "total_runtime_seconds", "broken", "restartable", "command",
)


def _job_fields(message) -> dict[str, str]:
    """``key=value`` fields from a job inventory message, where a value may contain commas.

    Not :func:`_message_kv`: that one ends a value at the first comma, and the field this section
    exists for is ``command`` — ``EXEC dbo.p @a = 1, @b = 2`` would arrive as ``EXEC dbo.p @a = 1``
    and the page would show a job running something it does not run. Here a value runs to the next
    **known key**, so only a job step that literally contains ``, command=`` could confuse it, and
    ``command`` is written last by every variant precisely so nothing follows it to lose.
    """
    text = str(message or "")
    names = "|".join(re.escape(name) for name in _JOB_FIELDS)
    fields: dict[str, str] = {}
    # DOTALL, because a value may span lines: an Oracle DBMS_JOB stores its `what` as the PL/SQL
    # block it was submitted with, newlines and all. Without it the pattern could not reach the
    # end of the value, the whole match failed, and `command` came back **missing** rather than
    # truncated — the field the section exists for, silently absent on the first real 8i job
    # (2.236, run 28790). Whitespace is collapsed so a multi-line block renders as one line.
    for match in re.finditer(rf"(?:^|,\s*)({names})=(.*?)(?=,\s*(?:{names})=|$)", text, re.DOTALL):
        fields[match.group(1)] = " ".join(match.group(2).split())
    return fields


def build_jobs(rows: list[dict]) -> dict:
    """The instance's scheduled jobs: what runs, when, and how it has been going.

    Its own section for the same reason the linked-server and access tables are: a job is not a
    time series. "Which jobs run on this box, what do they actually execute, and which of them has
    been failing" is a question the chart grid cannot answer at all — the metric is collected once
    a night and the chart pipeline drops it for having too few samples, so this reads the store
    rows directly like the other inventory sections.

    **Only enabled jobs are listed.** A disabled job is not a schedule, it is a note; the count of
    them is reported instead, and the inventory report already has a `disabled_jobs` block built
    from the same metric. Keeping them out is what makes this table the answer to "what runs here".

    One table for every engine: SQL Server fills it from msdb, Oracle from dba_jobs (8i) or
    dba_scheduler_jobs (10g+). The engines disagree about what a run counter means and the section
    does not paper over it — SQL Server's counts are the last 7 days, Oracle's scheduler counters
    are for the life of the job, so each row carries the window its numbers belong to.
    """
    jobs: list[dict] = []
    disabled = 0
    for entry in rows:
        if str(entry.get("metric_code") or entry.get("code") or "") != JOB_INVENTORY_CODE:
            continue
        name = str(entry.get("metric_item") or entry.get("item") or "").strip()
        if not name:
            continue                      # a failed collection is stored itemless; not a job
        state = str(entry.get("metric_value") or entry.get("lastText") or "").strip().upper()
        if state == "DISABLED":
            disabled += 1
            continue
        fields = _job_fields(entry.get("message"))
        runs = _int_or_none(fields.get("runs_7d"))
        failed = _int_or_none(fields.get("failed_7d"))
        window = "7d"
        if runs is None and failed is None:
            runs, failed, window = (_int_or_none(fields.get("runs_total")),
                                    _int_or_none(fields.get("failed_total")), "total")
        succeeded = _int_or_none(fields.get("succeeded_7d"))
        if succeeded is None and runs is not None and failed is not None:
            succeeded = max(0, runs - failed)
        jobs.append({
            "name": name,
            "owner": fields.get("owner", "") or "",
            "category": fields.get("category") or fields.get("job_class") or "",
            "lastOutcome": (fields.get("last_outcome", "") or "").upper(),
            "schedule": fields.get("schedule", "") or "",
            "nextRun": fields.get("next_run", "") or "",
            "lastRun": fields.get("last_run", "") or "",
            "command": fields.get("command", "") or "",
            "steps": _int_or_none(fields.get("steps")),
            "runs": runs,
            "succeeded": succeeded,
            "failed": failed,
            "window": window,
            # Oracle stops running a job after 16 consecutive failures; the count is the warning
            # before that happens, and it has no SQL Server equivalent.
            "consecutiveFailures": _int_or_none(fields.get("consecutive_failures")),
            # Three engines, three different things they can say about how long a run takes, and
            # none of them convertible into the others: SQL Server has the worst run of the last
            # 7 days in seconds, Oracle 10g+ has the last run as an INTERVAL, 8i has only a
            # lifetime total. The page shows whichever arrived and labels it, rather than picking
            # one and leaving the other two engines with an empty column.
            "maxDurationSeconds": _int_or_none(fields.get("max_duration_seconds_7d")),
            "lastDuration": fields.get("last_duration", "") or "",
            "totalRuntimeSeconds": _int_or_none(fields.get("total_runtime_seconds")),
            # 8i's schema_user: whose objects the job's PL/SQL resolves against, which is not
            # necessarily who submitted it (owner = log_user). A job that suddenly cannot see a
            # table is usually this column disagreeing with the one beside it.
            "schema": fields.get("schema", "") or "",
            # Oracle only, and only worth a word when it is on: a restartable job is retried after
            # a failure, so its failure count does not mean what it means on a job that is not.
            "restartable": (fields.get("restartable", "") or "").upper() in ("TRUE", "Y", "YES"),
            "asOf": str(entry.get("collected_at") or entry.get("asOf") or ""),
        })
    # Failing first: the table exists to be read top-down when something is wrong.
    jobs.sort(key=lambda job: (-(job["failed"] or 0),
                               0 if job["lastOutcome"] == "FAILED" else 1,
                               job["name"].casefold()))
    return {
        "jobs": jobs,
        "summary": {
            "enabled": len(jobs),
            "disabled": disabled,
            "failing": sum(1 for job in jobs if (job["failed"] or 0) > 0
                           or job["lastOutcome"] == "FAILED"),
            "neverRun": sum(1 for job in jobs if job["lastRun"] in ("", "never")),
            "asOf": max((job["asOf"] for job in jobs if job["asOf"]), default=""),
        },
    }


SERVER_PRINCIPALS_CODE = "SECURITY_SERVER_PRINCIPALS"
DATABASE_USERS_CODE = "DATABASE_USER_PERMISSIONS"

#: Roles and permissions the report marks as privileged. Kept here rather than trusted from the
#: metric's own HIGH_PRIVILEGE marker alone, so the page can still sort a bundle collected by an
#: older metric build that did not set it.
_HIGH_SERVER_ROLES = {"sysadmin", "securityadmin", "serveradmin", "setupadmin"}
_HIGH_DATABASE_ROLES = {"db_owner", "db_securityadmin", "db_accessadmin", "db_ddladmin"}


def _pipe_fields(message) -> dict[str, str]:
    """``key=value`` fields from a message whose segments are separated by ``|`` as well as ``,``.

    Both security metrics write ``login=x | roles=[a,b] | HIGH_PRIVILEGE``. :func:`_message_kv`
    reads a value to the next **comma**, so ``login`` swallowed the rest of the line — and ``roles``
    was then never seen at all, because ``re.findall`` resumes after the previous match and the
    previous match had eaten the string. Splitting on the pipe first makes each segment an ordinary
    comma-delimited fragment the shared parser handles correctly, without changing that parser for
    the dozen metrics that do not use pipes.
    """
    fields: dict[str, str] = {}
    for segment in str(message or "").split("|"):
        fields.update(_message_kv(segment))
    return fields


def _bracket_list(message, key: str) -> list[str]:
    """The members of a ``key=[a,b,c]`` field, read straight off the message.

    Not through :func:`_message_kv`: that parser ends a value at the first comma, which is exactly
    the character separating the members here. ``roles=[db_datareader,db_datawriter,db_ddladmin]``
    arrived as ``[db_datareader``, so a user holding db_ddladmin rendered as a plain reader — the
    one row in that table an operator would stop on, shown as the one kind that is harmless.
    """
    match = re.search(rf"{re.escape(key)}=\[(.*?)\]", str(message or ""))
    if not match:
        return []
    return [part.strip() for part in match.group(1).split(",") if part.strip()]


def build_access(rows: list[dict]) -> dict:
    """Who can connect to this instance, and what each principal holds.

    Its own section rather than more status chips, for the reason the linked-server table exists:
    a login is not a time series. The question an operator brings to this page is "who has
    sysadmin here" or "which user in this database has no login behind it", and neither is
    answerable from a chip that says OK.

    Two tables from two metrics, because SQL Server keeps the answer in two scopes and the join
    between them is the interesting part. A database user carries the *source* instance's SID
    after a restore, so it resolves to nothing until the login is recreated with that SID: the
    2026-08-10 migration onto 192.0.2.11 landed 13 databases whose 55 users all pointed at
    logins that did not exist, and the page had no way to show it. ``login`` on a user row is
    exactly that mapping — ``<orphaned/none>`` from the metric means the user cannot be reached by
    anyone.

    Built from the **raw store rows** like the linked-server and backup sections: both metrics are
    inventories collected once a night, so the chart pipeline drops them for having too few
    samples.
    """
    logins: list[dict] = []
    users: list[dict] = []
    for entry in rows:
        code = str(entry.get("metric_code") or entry.get("code") or "")
        name = str(entry.get("metric_item") or entry.get("item") or "").strip()
        if not name:
            continue                       # a failed collection is stored itemless; not a principal
        message = str(entry.get("message") or "")
        fields = _pipe_fields(message)

        if code == SERVER_PRINCIPALS_CODE:
            roles = _bracket_list(message, "server_roles")
            perms = _bracket_list(message, "server_perms")
            high = ("HIGH_PRIVILEGE" in message
                    or any(role.lower() in _HIGH_SERVER_ROLES for role in roles))
            logins.append({
                "name": name,
                "type": str(entry.get("metric_value") or entry.get("lastText") or "").strip(),
                "disabled": str(fields.get("disabled", "")).strip().lower() == "yes",
                "defaultDatabase": fields.get("default_db", "") or "",
                "roles": roles,
                "permissions": perms,
                "passwordAgeDays": _int_or_none(fields.get("password_age_days")),
                "checkPolicy": fields.get("check_policy", "") or "",
                "created": fields.get("created", "") or "",
                "high": high,
            })
            continue

        if code == DATABASE_USERS_CODE:
            # metric_item is "<database>\<user>"; a database name may itself contain a backslash,
            # so the split is from the right.
            database, _, user = name.rpartition("\\")
            roles = _bracket_list(message, "roles")
            login = str(fields.get("login", "") or "").strip()
            orphaned = login.lower() in ("<orphaned/none>", "", "-")
            high = ("HIGH_PRIVILEGE" in message
                    or any(role.lower() in _HIGH_DATABASE_ROLES for role in roles))
            users.append({
                "database": database or "(unknown)",
                "name": user or name,
                "type": str(entry.get("metric_value") or entry.get("lastText") or "").strip(),
                "login": "" if orphaned else login,
                "orphaned": orphaned,
                "roles": roles,
                "high": high,
                "status": str(entry.get("status") or "OK").strip().upper(),
                "note": message.split("|")[0].strip() if "guest" in message else "",
            })

    # Worst first in both tables, then alphabetical: a page opened to answer "who has sysadmin"
    # must not need scrolling to find out, and an orphaned user is the thing a restore leaves
    # behind that nobody notices.
    logins.sort(key=lambda r: (not r["high"], r["name"].casefold()))
    users.sort(key=lambda r: (not r["orphaned"], not r["high"],
                              r["database"].casefold(), r["name"].casefold()))
    return {
        "logins": logins,
        "databaseUsers": users,
        "summary": {
            "logins": len(logins),
            "highPrivilegeLogins": sum(1 for r in logins if r["high"]),
            "disabledLogins": sum(1 for r in logins if r["disabled"]),
            "databaseUsers": len(users),
            "databases": len({r["database"] for r in users}),
            "orphanedUsers": sum(1 for r in users if r["orphaned"]),
            "highPrivilegeUsers": sum(1 for r in users if r["high"]),
        },
    }
