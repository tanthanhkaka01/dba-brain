"""``server-metrics.html``: the Oracle-only sections: tablespaces and datafiles, the instance, objects, redo, top SQL.

Split out of ``reports/server_report.py`` on 2026-10-03 (Q11,
``audits/20261002_audit_typed_requests_and_errors.md`` section 5). ``server_report`` re-exports
every name, so no import changes.
"""

from __future__ import annotations
from db_ops.lib.coerce import as_float
import re
from db_ops.reports.server_series import CRITICAL_STATUSES, SEVERITY_RANK, WARNING_STATUSES, _int_or_none, _message_kv


#: Oracle's storage unit. The tablespace is the object an operator grows, moves and runs out of;
#: the datafile is where that growth physically lands. Nothing else on the page carries either.
TABLESPACE_CODE = "TABLESPACE_FREE_SPACE"
DATAFILE_CODE = "STORAGE_DATA_FILE_SPACE"
TEMP_SPACE_CODE = "STORAGE_TEMP_SPACE"

#: What the storage section reads. ``STORAGE_DATA_FILE_SPACE`` is deliberately included even
#: though every engine writes it: only the Oracle variant names a ``tablespace=``, which is what
#: :func:`build_tablespaces` keys the file list on, so the SQL Server and PostgreSQL rows fall out
#: on their own rather than needing the server's engine to be threaded down here.
TABLESPACE_SECTION_CODES = [TABLESPACE_CODE, DATAFILE_CODE, TEMP_SPACE_CODE]


def _leading_float(value) -> float | None:
    """The number a field starts with, ignoring what the collector appended to it.

    ``effective_free_mb=65418.97 (99.8% of max)`` is one field carrying two facts, and
    :func:`as_float` reads the whole string and returns ``None`` — so every free-space figure on
    the page rendered as an em dash while the store held the number.

    The leading digit is optional because Oracle's ``TO_CHAR`` writes a value below one without
    it: the large pool reported ``free_mb=.59`` and a pattern requiring a digit first read the
    smallest pool on the instance as "not collected".
    """
    match = re.match(r"\s*(-?(?:\d+(?:\.\d+)?|\.\d+))", str(value or ""))
    return float(match.group(1)) if match else None


def _datafiles_by_tablespace(rows: list[dict]) -> dict[str, list[dict]]:
    """The datafile rows of ``STORAGE_DATA_FILE_SPACE``, grouped by the tablespace they belong to.

    Only rows that state a ``tablespace=`` are taken. The same metric code carries three unrelated
    shapes — SQL Server writes ``database=/file=/used_pct=``, PostgreSQL writes ``database=/size=``
    (a database size, not a file at all) — and grouping those under a tablespace heading would
    invent Oracle storage on servers that have none.
    """
    files: dict[str, list[dict]] = {}
    for row in rows:
        if str(row.get("metric_code") or row.get("code") or "") != DATAFILE_CODE:
            continue
        fields = _message_kv(row.get("message"))
        tablespace = str(fields.get("tablespace") or "").strip()
        path = str(fields.get("file") or "").strip()
        if not tablespace or not path:
            continue
        files.setdefault(tablespace, []).append({
            "file": path,
            "sizeMB": _leading_float(fields.get("size_mb")),
            "status": str(row.get("status") or "OK"),
            "asOf": str(row.get("collected_at") or ""),
        })
    for entries in files.values():
        entries.sort(key=lambda entry: (-(entry["sizeMB"] or 0), entry["file"].casefold()))
    return files


def _temp_usage_by_tablespace(rows: list[dict]) -> dict[str, dict]:
    """Sort usage per temporary tablespace, from the Oracle variant of ``STORAGE_TEMP_SPACE``.

    ``max_used_mb`` is the high-water mark, and it is the number ORA-01652 is measured against —
    current usage is near zero between sorts, so reporting only that would call a temp tablespace
    that failed a report last night completely idle.
    """
    usage: dict[str, dict] = {}
    for row in rows:
        if str(row.get("metric_code") or row.get("code") or "") != TEMP_SPACE_CODE:
            continue
        fields = _message_kv(row.get("message"))
        # The Oracle variant writes ``temp tablespace=NAME``; the shared parser keys that on the
        # last word, so ``tablespace`` is the key here. SQL Server's tempdb variant writes no
        # tablespace name at all, which is what keeps tempdb out of this section.
        name = str(fields.get("tablespace") or "").strip()
        if not name:
            continue
        usage[name] = {
            "tempUsedMB": _leading_float(fields.get("used_mb")),
            "tempMaxUsedMB": _leading_float(fields.get("max_used_mb")),
            "tempTotalMB": _leading_float(fields.get("total_mb")),
            "currentSorts": _int_or_none(fields.get("current_sorts")),
            "status": str(row.get("status") or "OK"),
        }
    return usage


def build_tablespaces(rows: list[dict]) -> dict:
    """Oracle storage: one row per tablespace, with its datafiles folded underneath.

    The one thing an Oracle DBA looks up first had no place on this page. Every number was in the
    store — ``TABLESPACE_FREE_SPACE`` has been collecting on 192.0.2.236 since 13 August — and
    all of it rendered as unreadable chart cards: 10 sparklines called "Tablespace free space"
    with the tablespace name only in a tooltip, and the 15 datafiles as 15 more cards saying
    ``15000 MB`` with no indication of which tablespace they extend.

    Three fields decide the row, and each answers a question the others cannot:

    - **Effective free** (``effective_free_mb``) is free space *plus* autoextend headroom — what
      the tablespace can still absorb before ORA-01653. Reporting ``free_now_mb`` alone calls a
      2 GB tablespace with 63 GB of headroom nearly full, which is the reading that gets a
      datafile added that was never needed.
    - **Largest free extent** is what an allocation actually has to fit in. A tablespace can hold
      4 GB of free space in fragments too small for a 100 MB extent and still raise ORA-01653, so
      the number is carried per row rather than derived from the percentage.
    - **Autoextending files vs total files** says whether the headroom is real. Headroom counted
      from files that cannot grow is a promise the database will not keep.

    Built from the raw store rows like ``jobs`` and ``access``: this is an inventory of what
    exists, so only the newest collection counts, and the chart pipeline's series would drop the
    datafile rows for having too few samples anyway.
    """
    files = _datafiles_by_tablespace(rows)
    temp = _temp_usage_by_tablespace(rows)

    tablespaces: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        if str(row.get("metric_code") or row.get("code") or "") != TABLESPACE_CODE:
            continue
        name = str(row.get("metric_item") or row.get("item") or "").strip()
        if not name or name in seen:
            continue                  # itemless rows are a failed collection, not a tablespace
        seen.add(name)
        fields = _message_kv(row.get("message"))
        allocated = _leading_float(fields.get("allocated_mb"))
        maximum = _leading_float(fields.get("max_mb"))
        effective_free = _leading_float(fields.get("effective_free_mb"))
        # Percent used against the ceiling the tablespace can reach, not against what is allocated
        # today: on an autoextending file those two differ by the whole headroom, and only the
        # first one predicts the error. The collector states its own "(99.8% of max)" free figure
        # inside the same field; that one is preferred, so the page and the alert cannot round to
        # two different numbers from the same sample.
        stated_free_pct = re.search(r"effective_free_mb=[\d.]+\s*\(([\d.]+)%\s*of max\)",
                                    str(row.get("message") or ""))
        if stated_free_pct:
            used_pct = round(max(0.0, 100.0 - float(stated_free_pct.group(1))), 2)
        elif maximum:
            used_pct = round(max(0.0, (maximum - (effective_free or 0.0)) / maximum * 100.0), 2)
        else:
            used_pct = None
        entry = {
            "name": name,
            "status": str(row.get("status") or "OK"),
            "effectiveFreeMB": effective_free,
            "freeNowMB": _leading_float(fields.get("free_now_mb")),
            "autoextendHeadroomMB": _leading_float(fields.get("autoextend_headroom_mb")),
            "allocatedMB": allocated,
            "maxMB": maximum,
            "usedPct": used_pct,
            "largestFreeExtentMB": _leading_float(fields.get("largest_free_extent_mb")),
            "files": files.get(name, []),
            "fileCount": len(files.get(name, [])),
            "autoextendFiles": None,
            "temp": name in temp,
            "asOf": str(row.get("collected_at") or ""),
        }
        # "datafiles=2 (autoextend=2)" — the count the metric states, which covers files whose own
        # row was capped out of the datafile list.
        declared = re.search(r"datafiles=(\d+)\s*\(autoextend=(\d+)\)", str(row.get("message") or ""))
        if declared:
            entry["declaredFiles"] = int(declared.group(1))
            entry["autoextendFiles"] = int(declared.group(2))
        else:
            entry["declaredFiles"] = entry["fileCount"]
        entry.update(temp.get(name, {}))
        entry["status"] = str(row.get("status") or "OK")
        tablespaces.append(entry)

    # Fullest first: the table is opened to find what is about to run out, and on an instance with
    # 12 tablespaces that answer must not be somewhere in the middle of an alphabetical list.
    tablespaces.sort(key=lambda r: (-SEVERITY_RANK.get(r["status"], 0),
                                    -(r["usedPct"] if r["usedPct"] is not None else -1),
                                    r["name"].casefold()))
    # Datafiles whose tablespace produced no row of its own. Dropping them would hide storage that
    # exists — a tablespace missing from TABLESPACE_FREE_SPACE is itself worth seeing.
    orphan_files = [dict(entry, tablespace=name)
                    for name, entries in sorted(files.items()) if name not in seen
                    for entry in entries]
    return {
        "tablespaces": tablespaces,
        "orphanFiles": orphan_files,
        "summary": {
            "tablespaces": len(tablespaces),
            "datafiles": sum(t["declaredFiles"] for t in tablespaces) + len(orphan_files),
            "temp": sum(1 for t in tablespaces if t["temp"]),
            "warning": sum(1 for t in tablespaces if t["status"] in WARNING_STATUSES),
            "critical": sum(1 for t in tablespaces if t["status"] in CRITICAL_STATUSES),
            # Headroom that only exists on paper: no file in the tablespace can autoextend, so
            # "effective free" is whatever is already allocated and nothing more.
            "noAutoextend": sum(1 for t in tablespaces if t["autoextendFiles"] == 0),
            "totalAllocatedMB": round(sum(t["allocatedMB"] or 0 for t in tablespaces), 2),
            "totalMaxMB": round(sum(t["maxMB"] or 0 for t in tablespaces), 2),
            "asOf": max((t["asOf"] for t in tablespaces if t["asOf"]), default=""),
        },
    }


#: The Oracle-only metrics, grouped by the question they answer. Every one of them was already
#: collecting and had no table to be read in: they rendered as chart cards whose item name lives
#: in a tooltip, which is how a CRITICAL shared pool sat on the page as an unlabelled sparkline.
#:
#: All four builders read the raw store rows, not the charted series. The chart pipeline caps a
#: metric at :data:`MAX_ITEMS_PER_METRIC` items — it dropped 18 of the 28 top-SQL rows — and drops
#: whole metrics for having too few samples, which is right for a trend and wrong for an inventory.
ORACLE_POOL_CODE = "SHARED_POOL_FREE"
ORACLE_LIBRARY_CACHE_CODE = "LIBRARY_CACHE"
ORACLE_BUFFER_CACHE_CODE = "BUFFER_CACHE_HIT"
ORACLE_PROCESS_LIMIT_CODE = "PROCESS_LIMIT"
ORACLE_INSTANCE_CODES = [ORACLE_POOL_CODE, ORACLE_LIBRARY_CACHE_CODE,
                         ORACLE_BUFFER_CACHE_CODE, ORACLE_PROCESS_LIMIT_CODE]

ORACLE_INVALID_OBJECTS_CODE = "INVALID_OBJECTS"
ORACLE_INDEX_UNUSABLE_CODE = "INDEX_UNUSABLE"
ORACLE_EXTENT_LIMIT_CODE = "SEGMENT_EXTENT_LIMIT"
ORACLE_TOP_SEGMENT_CODE = "TOP_SEGMENT_SIZE"
ORACLE_ROLLBACK_CODE = "ROLLBACK_SEGMENT_CONTENTION"
ORACLE_OBJECT_CODES = [ORACLE_INVALID_OBJECTS_CODE, ORACLE_INDEX_UNUSABLE_CODE,
                       ORACLE_EXTENT_LIMIT_CODE, ORACLE_TOP_SEGMENT_CODE, ORACLE_ROLLBACK_CODE]

#: Redo and archiving. On Oracle ``LOG_FILE_SPACE`` is not log-file fullness at all — it carries
#: the archive log mode, the archive destinations and the unarchived redo backlog. The SQL Server
#: variant of the same code is per-database log usage, which the databases table already renders;
#: the two are told apart by the items, not by the engine.
ORACLE_REDO_CODE = "LOG_FILE_SPACE"

ORACLE_TOP_DISK_SQL_CODE = "TOP_DISK_READ_SQL"
ORACLE_TOP_GETS_SQL_CODE = "TOP_BUFFER_GETS_SQL"
ORACLE_TOP_SQL_CODES = [ORACLE_TOP_DISK_SQL_CODE, ORACLE_TOP_GETS_SQL_CODE]

ORACLE_SECTION_CODES = [*ORACLE_INSTANCE_CODES, *ORACLE_OBJECT_CODES,
                        ORACLE_REDO_CODE, *ORACLE_TOP_SQL_CODES]


def _tail_field(message, key: str, *, stop: str = "") -> str:
    """Everything a message says after ``key=``, commas included.

    :func:`_message_kv` ends a value at the first comma, and both the SQL text and the
    invalid-object example list are full of them — ``sql=SELECT a, b FROM t`` arrived as
    ``SELECT a``, which is not the statement anybody would recognise. The collectors write these
    fields last for exactly this reason, so reading to the end of the message (or to ``stop``,
    where a trailing explanation follows) is what recovers them whole.
    """
    text = str(message or "")
    marker = f"{key}="
    at = text.find(marker)
    if at < 0:
        return ""
    tail = text[at + len(marker):]
    if stop:
        cut = tail.find(stop)
        if cut >= 0:
            tail = tail[:cut]
    return tail.strip()


def _rows_of(rows: list[dict], code: str) -> list[dict]:
    """The rows of one metric code, from a mixed bag of store rows."""
    return [row for row in rows
            if str(row.get("metric_code") or row.get("code") or "") == code]


def _item_of(row: dict) -> str:
    return str(row.get("metric_item") or row.get("item") or "").strip()


def build_oracle_instance(rows: list[dict]) -> dict:
    """Oracle instance health: the SGA pools, the caches, and how close the instance is to its
    process and session ceilings.

    These four metrics decide whether the instance can keep taking work, and none of them had a
    row anywhere. ``SHARED_POOL_FREE`` on 192.0.2.236 has been CRITICAL at 9.14 MB free — the
    instance's *only* critical finding — while rendering as one nameless sparkline among thirty.

    Two of the numbers are ratios the collector does not compute, and both are the point of their
    metric: library cache **hit ratio** (``gethits/gets``) says whether SQL is being re-parsed,
    and the **percent of limit** on processes/sessions says how much headroom is left before
    ORA-00020. Reporting reloads and a raw session count instead leaves the reader to divide.
    """
    pools = [{
        "pool": _item_of(row).split(":", 1)[0],
        "name": _message_kv(row.get("message")).get("name") or "",
        "freeMB": _leading_float(_message_kv(row.get("message")).get("free_mb")),
        "status": str(row.get("status") or "OK"),
        "asOf": str(row.get("collected_at") or ""),
    } for row in _rows_of(rows, ORACLE_POOL_CODE) if _item_of(row)]
    pools.sort(key=lambda r: (-SEVERITY_RANK.get(r["status"], 0), r["freeMB"] or 0))

    caches: list[dict] = []
    for row in _rows_of(rows, ORACLE_LIBRARY_CACHE_CODE):
        namespace = _item_of(row)
        if not namespace:
            continue
        fields = _message_kv(row.get("message"))
        gets = _int_or_none(fields.get("gets"))
        gethits = _int_or_none(fields.get("gethits"))
        caches.append({
            "namespace": namespace,
            "gets": gets,
            "getHits": gethits,
            # A namespace nothing asked for has no hit ratio. Folding that to 0% would rank the
            # untouched namespaces as the worst ones on the instance.
            "hitPct": round(gethits / gets * 100.0, 2) if gets else None,
            "reloads": _int_or_none(fields.get("reloads")),
            "invalidations": _int_or_none(fields.get("invalidations")),
            "status": str(row.get("status") or "OK"),
        })
    caches.sort(key=lambda r: (-(r["reloads"] or 0), r["namespace"]))

    buffer_cache = None
    for row in _rows_of(rows, ORACLE_BUFFER_CACHE_CODE):
        fields = _message_kv(row.get("message"))
        buffer_cache = {
            "hitPct": as_float(row.get("metric_value")),
            "dbBlockGets": _int_or_none(fields.get("db_block_gets")),
            "consistentGets": _int_or_none(fields.get("consistent_gets")),
            "physicalReads": _int_or_none(fields.get("physical_reads")),
            "status": str(row.get("status") or "OK"),
        }
        break

    limits: list[dict] = []
    for row in _rows_of(rows, ORACLE_PROCESS_LIMIT_CODE):
        resource = _item_of(row)
        if not resource:
            continue
        fields = _message_kv(row.get("message"))
        current = _int_or_none(fields.get("current"))
        # 8i pads the limit with spaces ("limit=       550"), so the field is a string with
        # leading whitespace rather than a number.
        limit = _int_or_none(str(fields.get("limit") or "").strip())
        peak = _int_or_none(fields.get("max_seen"))
        limits.append({
            "resource": resource,
            "current": current,
            "peak": peak,
            "limit": limit,
            "usedPct": round(current / limit * 100.0, 1) if limit and current is not None else None,
            "peakPct": round(peak / limit * 100.0, 1) if limit and peak is not None else None,
            "status": str(row.get("status") or "OK"),
        })
    limits.sort(key=lambda r: -(r["usedPct"] or 0))

    worst = max((SEVERITY_RANK.get(entry["status"], 0)
                 for entry in [*pools, *caches, *limits] + ([buffer_cache] if buffer_cache else [])),
                default=0)
    return {
        "pools": pools,
        "libraryCache": caches,
        "bufferCache": buffer_cache,
        "limits": limits,
        "summary": {
            "pools": len(pools),
            "worstStatus": next((name for name, rank in SEVERITY_RANK.items() if rank == worst),
                                "OK") if worst else "OK",
            "criticalPools": sum(1 for p in pools if p["status"] in CRITICAL_STATUSES),
            "reloads": sum(c["reloads"] or 0 for c in caches),
            "invalidations": sum(c["invalidations"] or 0 for c in caches),
            "asOf": max((p["asOf"] for p in pools if p["asOf"]), default=""),
        },
    }


def build_oracle_objects(rows: list[dict]) -> dict:
    """Segments and objects: what is broken, what is about to break, and what used the space.

    Four different failures live here and each is invisible to the tablespace numbers above:

    - **INVALID objects** raise ORA-04068 at *call* time, not when they broke. 73 INVALID views
      in LTR is a list of things that will fail on next use, and the page never said so.
    - **UNUSABLE indexes** are not slow indexes, they are absent ones: queries silently full-scan
      and DML raises ORA-01502.
    - **Segments near MAX_EXTENTS** fail with ORA-01631 while the tablespace still shows
      gigabytes free — the one 8i failure a capacity percentage cannot predict.
    - **Rollback segment contention** is the 8i throughput ceiling, and no other metric sees it.

    ``TOP_SEGMENT_SIZE`` is the counterweight: it answers "what used the room" where the
    tablespace table answers "is there room left".
    """
    invalid: list[dict] = []
    for row in _rows_of(rows, ORACLE_INVALID_OBJECTS_CODE):
        fields = _message_kv(row.get("message"))
        owner = str(fields.get("owner") or "").strip()
        if not owner:
            continue
        invalid.append({
            "owner": owner,
            "objectType": str(fields.get("object_type") or "").strip(),
            "count": _int_or_none(fields.get("invalid")),
            # The example list is comma-separated and ends where the explanation begins.
            "examples": [part.strip() for part
                         in _tail_field(row.get("message"), "examples", stop=";").split(",")
                         if part.strip()],
            "status": str(row.get("status") or "OK"),
        })
    invalid.sort(key=lambda r: (-(r["count"] or 0), r["owner"], r["objectType"]))

    unusable = [{
        "index": _item_of(row),
        "status": str(row.get("status") or "CRITICAL"),
        "detail": str(row.get("message") or ""),
        # dba_ind_partitions rows are keyed OWNER.INDEX:PARTITION — the partition half is what
        # says the whole index is not broken, only one partition of it.
        "partition": _item_of(row).split(":", 1)[1] if ":" in _item_of(row) else "",
    } for row in _rows_of(rows, ORACLE_INDEX_UNUSABLE_CODE) if _item_of(row)]
    unusable.sort(key=lambda r: r["index"])

    extents: list[dict] = []
    for row in _rows_of(rows, ORACLE_EXTENT_LIMIT_CODE):
        segment = _item_of(row)
        if not segment:
            continue
        fields = _message_kv(row.get("message"))
        used, _, limit = str(fields.get("extents") or "").partition("/")
        extents.append({
            "segment": segment,
            "type": str(fields.get("type") or "").strip(),
            "tablespace": str(fields.get("tablespace") or "").strip(),
            "extents": _int_or_none(used),
            "maxExtents": _int_or_none(limit.split("(")[0]),
            "usedPct": as_float(row.get("metric_value")),
            "status": str(row.get("status") or "WARNING"),
        })
    extents.sort(key=lambda r: -(r["usedPct"] or 0))

    segments: list[dict] = []
    for row in _rows_of(rows, ORACLE_TOP_SEGMENT_CODE):
        segment = _item_of(row)
        if not segment:
            continue
        fields = _message_kv(row.get("message"))
        segments.append({
            "segment": segment,
            "type": str(fields.get("type") or "").strip(),
            "tablespace": str(fields.get("tablespace") or "").strip(),
            "sizeMB": _leading_float(fields.get("size_mb")),
            "extents": _int_or_none(fields.get("extents")),
        })
    segments.sort(key=lambda r: -(r["sizeMB"] or 0))

    rollback: list[dict] = []
    for row in _rows_of(rows, ORACLE_ROLLBACK_CODE):
        name = _item_of(row)
        if not name:
            continue
        fields = _message_kv(row.get("message"))
        rollback.append({
            "name": name,
            "waits": _int_or_none(fields.get("waits")),
            "gets": _int_or_none(fields.get("gets")),
            "waitPct": as_float(fields.get("wait_ratio_pct")),
            "activeTransactions": _int_or_none(fields.get("active_transactions")),
            "extents": _int_or_none(fields.get("extents")),
            "shrinks": _int_or_none(fields.get("shrinks")),
            "extends": _int_or_none(fields.get("extends")),
            "sizeMB": _leading_float(fields.get("size_mb")),
            "status": str(row.get("status") or "OK"),
        })
    rollback.sort(key=lambda r: (-(r["waitPct"] or 0), r["name"]))

    return {
        "invalidObjects": invalid,
        "unusableIndexes": unusable,
        "extentLimits": extents,
        "topSegments": segments,
        "rollbackSegments": rollback,
        "summary": {
            "invalidObjects": sum(entry["count"] or 0 for entry in invalid),
            "invalidOwners": len({entry["owner"] for entry in invalid}),
            "unusableIndexes": len(unusable),
            "extentLimits": len(extents),
            "topSegments": len(segments),
            "largestSegmentMB": segments[0]["sizeMB"] if segments else None,
            "rollbackSegments": len(rollback),
            # A rollback segment that had to grow back after being shrunk is the contention the
            # wait ratio understates: the work happened, it just did not have to wait for a slot.
            "rollbackResizes": sum((entry["shrinks"] or 0) + (entry["extends"] or 0)
                                   for entry in rollback),
        },
    }


#: The ``LOG_FILE_SPACE`` items only Oracle writes. Anything else under that code is SQL Server's
#: per-database log usage, which the databases table already renders — so the redo section is
#: selected by item, never by engine.
_ORACLE_REDO_ITEMS = ("log_mode", "unarchived_logs", "redo_logs", "fast_recovery_area")


def build_oracle_redo(rows: list[dict]) -> dict:
    """Redo and archiving — whether this instance can be recovered to a point in time at all.

    ``log_mode=NOARCHIVELOG`` means no archiving, so the only restore possible is to the moment of
    the last full backup. That is the single most consequential fact about 192.0.2.236 and no
    page stated it: the value sat inside a ``LOG_FILE_SPACE`` chart card titled "Log file space",
    a name that on every other server means something entirely different.

    The unarchived-log count is reported **with its mode**, because it means opposite things in
    each: a backlog in ARCHIVELOG is archiving falling behind and heading for a frozen instance,
    while in NOARCHIVELOG no group is ever archived and the same number is normal.
    """
    log_mode = ""
    destinations: list[dict] = []
    unarchived = None
    recovery_area = None
    redo_total_mb = None
    status = "OK"
    as_of = ""

    for row in _rows_of(rows, ORACLE_REDO_CODE):
        item = _item_of(row)
        fields = _message_kv(row.get("message"))
        value = str(row.get("metric_value") or "").strip()
        if item.startswith("archive_dest_"):
            destinations.append({
                "id": item.replace("archive_dest_", ""),
                "destination": str(fields.get("destination") or "").strip(),
                "state": value,
                "binding": str(fields.get("binding") or "").strip(),
                "error": str(fields.get("error") or "").strip(),
                "status": str(row.get("status") or "OK"),
            })
        elif item == "log_mode":
            log_mode = value
        elif item == "unarchived_logs":
            unarchived = _int_or_none(value)
        elif item == "redo_logs":
            redo_total_mb = _leading_float(value)
        elif item == "fast_recovery_area":
            recovery_area = {
                "name": str(fields.get("name") or "").strip(),
                "usedPct": as_float(fields.get("used_pct")),
                "reclaimablePct": as_float(fields.get("reclaimable_pct")),
                "unreclaimablePct": as_float(fields.get("unreclaimable_pct")),
                "spaceLimitMB": _leading_float(fields.get("space_limit_mb")),
                "spaceUsedMB": _leading_float(fields.get("space_used_mb")),
                "files": _int_or_none(fields.get("files")),
                "status": str(row.get("status") or "OK"),
            }
        else:
            continue                  # SQL Server's per-database log usage; not this section
        if SEVERITY_RANK.get(str(row.get("status") or "OK"), 0) > SEVERITY_RANK.get(status, 0):
            status = str(row.get("status") or "OK")
        as_of = max(as_of, str(row.get("collected_at") or ""))

    destinations.sort(key=lambda r: r["id"])
    archiving = bool(log_mode) and log_mode.upper() != "NOARCHIVELOG"
    return {
        "logMode": log_mode,
        "archiving": archiving,
        "destinations": destinations,
        "unarchivedLogs": unarchived,
        "recoveryArea": recovery_area,
        "redoTotalMB": redo_total_mb,
        "summary": {
            # The verdict this section exists for. Stated as its own field so the page does not
            # have to re-derive "can this be restored to a point in time" from the mode string.
            "pointInTimeRecovery": archiving,
            "destinations": len(destinations),
            "failedDestinations": sum(1 for entry in destinations
                                      if entry["state"].upper() != "VALID"),
            "status": status,
            "asOf": as_of,
        },
    }


def build_oracle_top_sql(rows: list[dict]) -> dict:
    """The heaviest statements on the instance, by disk reads and by buffer gets.

    Both lists exist because they answer different questions. Disk reads name the statement that
    makes storage the bottleneck; buffer gets name the one burning CPU on logical I/O — and a
    statement can top one list while being absent from the other. On 192.0.2.236 the worst
    buffer-gets statement runs 4.4 million times at 3.5 gets each and does no disk I/O at all,
    which the disk-read list cannot show.

    **Reads per execution is the column to sort a fix by**, not the total: a statement with
    378,431 disk reads over 3 executions is one bad plan, while the same total over 4 million
    executions is a statement doing its job. Both are in the table and the ranking is by total,
    because that is what the instance is actually paying.

    Uncapped, from the raw rows: the chart pipeline's :data:`MAX_ITEMS_PER_METRIC` dropped 18 of
    the 28 statements collected, and a top-SQL list missing two thirds of its entries is worse
    than no list — the reader believes they have seen the worst.
    """
    def statements(code: str, value_key: str) -> list[dict]:
        out: list[dict] = []
        for row in _rows_of(rows, code):
            fields = _message_kv(row.get("message"))
            # sql= is written last by both variants precisely so it can be read to end-of-message:
            # a statement is full of commas and the shared parser stops at the first one.
            text = _tail_field(row.get("message"), "sql")
            out.append({
                "sqlId": _item_of(row),
                "value": as_float(row.get("metric_value")),
                "executions": _int_or_none(fields.get("executions")),
                "perExecution": as_float(fields.get(value_key)),
                "diskReads": _int_or_none(fields.get("disk_reads")),
                "rowsProcessed": _int_or_none(fields.get("rows_processed")),
                "sql": text,
                "status": str(row.get("status") or "OK"),
            })
        out.sort(key=lambda r: -(r["value"] or 0))
        return out

    by_disk = statements(ORACLE_TOP_DISK_SQL_CODE, "reads_per_exec")
    by_gets = statements(ORACLE_TOP_GETS_SQL_CODE, "gets_per_exec")
    return {
        "byDiskReads": by_disk,
        "byBufferGets": by_gets,
        "summary": {
            "byDiskReads": len(by_disk),
            "byBufferGets": len(by_gets),
            "worstDiskReads": by_disk[0]["value"] if by_disk else None,
            "worstBufferGets": by_gets[0]["value"] if by_gets else None,
        },
    }
