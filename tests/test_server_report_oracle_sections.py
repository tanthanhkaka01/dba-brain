"""The Oracle facts `server-metrics.html` collected for weeks and could not show.

Eleven Oracle metrics were running on 192.0.2.236 with no table anywhere to read one in. They
rendered as chart cards whose item name lives in a tooltip, which is how `SHARED_POOL_FREE` — the
instance's *only* CRITICAL finding, at 9 MB free — sat on the page as an unlabelled sparkline
among thirty others.

Each of the four sections here exists because one specific question could not be answered:

- **Instance health** — is the instance about to refuse work? The two numbers that say so are
  ratios the collector does not compute: library cache hit ratio, and percent of the process
  limit. Reporting reloads and a raw session count leaves the reader to divide.
- **Segments & objects** — four failures invisible to the tablespace numbers. An INVALID object
  fails at *call* time; an UNUSABLE index is an absent index, not a slow one; a segment at
  MAX_EXTENTS fails while its tablespace still shows gigabytes free; and rollback contention is
  the 8i write ceiling nothing else measures.
- **Redo & archiving** — `NOARCHIVELOG` means no point-in-time recovery exists for this instance.
  That is the most consequential fact about it and it lived inside a card titled "Log file space",
  a name that on every other server means per-database log usage.
- **Top SQL** — the statement text is full of commas, and the shared message parser ends a value
  at the first one. `sql=` is written last by both variants precisely so it can be read whole.

The tests that matter most are the ones asserting a section stays *empty*: none of these builders
is told which engine it is looking at, and three of the metric codes are written by SQL Server
too. A section that invents Oracle storage on a SQL Server page is worse than one that is missing.
"""

from db_ops.reports.server_report import (
    build_oracle_instance,
    build_oracle_objects,
    build_oracle_redo,
    build_oracle_top_sql,
)


def _row(code, item, value, message, status="OK"):
    return {"metric_code": code, "metric_item": item, "metric_value": value,
            "message": message, "status": status, "collected_at": "2026-08-17T05:45:20Z"}


# --------------------------------------------------------------------------- #
# Instance health
# --------------------------------------------------------------------------- #
def test_a_pool_below_one_megabyte_is_still_read_as_a_number():
    """Oracle's TO_CHAR writes a value under one without its leading zero. A parser requiring a
    digit first read `free_mb=.59` as "not collected", losing the smallest pool on the instance —
    which is the one closest to failing."""
    section = build_oracle_instance([
        _row("SHARED_POOL_FREE", "large pool:free memory", ".59",
             "pool=large pool, name=free memory, free_mb=.59"),
    ])

    assert section["pools"][0]["freeMB"] == 0.59


def test_a_critical_shared_pool_leads_the_section_and_is_counted():
    section = build_oracle_instance([
        _row("SHARED_POOL_FREE", "java pool:free memory", "17.46",
             "pool=java pool, name=free memory, free_mb=17.46"),
        _row("SHARED_POOL_FREE", "shared pool:free memory", "9.14",
             "pool=shared pool, name=free memory, free_mb=9.14", status="CRITICAL"),
    ])

    assert section["pools"][0]["pool"] == "shared pool"
    assert section["summary"]["criticalPools"] == 1
    assert section["summary"]["worstStatus"] == "CRITICAL"


def test_the_process_limit_is_reported_as_a_percentage_of_its_ceiling():
    """215 sessions means nothing without the 610 beside it, and ORA-00020 is what the row is
    read to avoid. 8i pads the limit with spaces, so the field is a padded string."""
    section = build_oracle_instance([
        _row("PROCESS_LIMIT", "processes", "204",
             "resource=processes, current=204, max_seen=221, limit=       550"),
    ])

    row = section["limits"][0]
    assert row["current"] == 204 and row["peak"] == 221 and row["limit"] == 550
    assert row["usedPct"] == 37.1
    # The ceiling is reached at the peak, never at the moment the collector happened to look.
    assert row["peakPct"] == 40.2


def test_a_library_cache_namespace_nothing_asked_for_has_no_hit_ratio():
    """Folding a zero-get namespace to 0% would rank the untouched ones as the worst on the
    instance — the exact opposite of true."""
    section = build_oracle_instance([
        _row("LIBRARY_CACHE", "SQL AREA", "11363",
             "namespace=SQL AREA, gets=663851, gethits=658074, reloads=11363, invalidations=80"),
        _row("LIBRARY_CACHE", "OBJECT", "0",
             "namespace=OBJECT, gets=0, gethits=0, reloads=0, invalidations=0"),
    ])

    by_name = {entry["namespace"]: entry for entry in section["libraryCache"]}
    assert by_name["OBJECT"]["hitPct"] is None
    assert by_name["SQL AREA"]["hitPct"] == 99.13
    # Most reloads first: a reload is a statement re-parsed because the pool aged it out.
    assert section["libraryCache"][0]["namespace"] == "SQL AREA"


# --------------------------------------------------------------------------- #
# Segments & objects
# --------------------------------------------------------------------------- #
def test_the_invalid_object_examples_survive_the_commas_between_them():
    """`examples=A,B` is one field whose separator is the character the shared parser ends a
    value at, and a trailing explanation follows it."""
    section = build_oracle_objects([
        _row("INVALID_OBJECTS", "LTR/VIEW", "73",
             "owner=LTR, object_type=VIEW, invalid=73, examples=CAPSEWVIEWLT,VSUPERMARKET2; "
             "INVALID code raises ORA-04068 at call time, not at the time it broke.",
             status="WARNING"),
    ])

    row = section["invalidObjects"][0]
    assert row["examples"] == ["CAPSEWVIEWLT", "VSUPERMARKET2"]
    assert row["count"] == 73 and row["objectType"] == "VIEW"
    assert section["summary"]["invalidObjects"] == 73


def test_an_unusable_index_partition_is_distinguished_from_a_whole_unusable_index():
    """dba_ind_partitions rows are keyed OWNER.INDEX:PARTITION. The partition half is what says
    only one partition is broken, not the index — a different piece of work."""
    section = build_oracle_objects([
        _row("INDEX_UNUSABLE", "LTR.IDX_A", "UNUSABLE", "index=LTR.IDX_A on table LTR.T is UNUSABLE",
             status="CRITICAL"),
        _row("INDEX_UNUSABLE", "LTR.IDX_B:P2026", "UNUSABLE",
             "index partition=LTR.IDX_B:P2026 is UNUSABLE", status="CRITICAL"),
    ])

    by_index = {entry["index"]: entry for entry in section["unusableIndexes"]}
    assert by_index["LTR.IDX_A"]["partition"] == ""
    assert by_index["LTR.IDX_B:P2026"]["partition"] == "P2026"
    assert section["summary"]["unusableIndexes"] == 2


def test_a_segment_near_max_extents_reports_both_halves_of_its_ratio():
    """It fails with ORA-01631 while the tablespace still shows free space, so the storage table
    above predicts nothing about it. The percentage alone does not say how much room is left."""
    section = build_oracle_objects([
        _row("SEGMENT_EXTENT_LIMIT", "LTR.BIG_TABLE", "96.5",
             "segment=LTR.BIG_TABLE, type=TABLE, tablespace=USERS, extents=193/200 (96.5%); "
             "at the ceiling the next extent fails with ORA-01631 even with free space in the "
             "tablespace.", status="CRITICAL"),
    ])

    row = section["extentLimits"][0]
    assert row["extents"] == 193 and row["maxExtents"] == 200
    assert row["usedPct"] == 96.5 and row["tablespace"] == "USERS"


def test_the_largest_segment_comes_first_because_that_is_what_used_the_room():
    section = build_oracle_objects([
        _row("TOP_SEGMENT_SIZE", "LTR.SMALL", "97.88",
             "segment=LTR.SMALL, type=INDEX, tablespace=USERS, size_mb=97.88, extents=7"),
        _row("TOP_SEGMENT_SIZE", "LTR.PI_BARCODE_D", "1564.5",
             "segment=LTR.PI_BARCODE_D, type=TABLE, tablespace=USERS, size_mb=1564.5, extents=17"),
    ])

    assert [row["segment"] for row in section["topSegments"]] == ["LTR.PI_BARCODE_D", "LTR.SMALL"]
    assert section["summary"]["largestSegmentMB"] == 1564.5


def test_rollback_segments_that_keep_regrowing_are_counted_even_at_a_zero_wait_ratio():
    """A segment shrunk and grown back did that work on every long transaction. The wait ratio
    only counts transactions that had to queue for a slot, so it reports zero throughout."""
    section = build_oracle_objects([
        _row("ROLLBACK_SEGMENT_CONTENTION", "RBS26", "0",
             "rollback_segment=RBS26, waits=0, gets=2712, wait_ratio_pct=0, "
             "active_transactions=0, extents=8, shrinks=5, extends=2, size_mb=3.99"),
    ])

    assert section["summary"]["rollbackResizes"] == 7
    assert section["rollbackSegments"][0]["shrinks"] == 5


# --------------------------------------------------------------------------- #
# Redo & archiving
# --------------------------------------------------------------------------- #
def _redo_rows(log_mode, unarchived="2"):
    return [
        _row("LOG_FILE_SPACE", "log_mode", log_mode,
             "database log_mode=%s; no archiving, so no point-in-time recovery from this "
             "instance." % log_mode),
        _row("LOG_FILE_SPACE", "unarchived_logs", unarchived,
             "redo groups filled but not archived=%s" % unarchived),
        _row("LOG_FILE_SPACE", "archive_dest_1", "VALID",
             "destination=D:\\oracle\\ora81\\RDBMS, status=VALID, binding=MANDATORY, error=none"),
    ]


def test_noarchivelog_is_reported_as_no_point_in_time_recovery():
    """The single most consequential fact about this instance, and no page stated it."""
    section = build_oracle_redo(_redo_rows("NOARCHIVELOG"))

    assert section["logMode"] == "NOARCHIVELOG"
    assert section["archiving"] is False
    assert section["summary"]["pointInTimeRecovery"] is False


def test_archivelog_is_reported_as_recoverable():
    section = build_oracle_redo(_redo_rows("ARCHIVELOG"))

    assert section["archiving"] is True and section["summary"]["pointInTimeRecovery"] is True


def test_a_failing_archive_destination_is_counted():
    section = build_oracle_redo([
        _row("LOG_FILE_SPACE", "log_mode", "ARCHIVELOG", "database log_mode=ARCHIVELOG"),
        _row("LOG_FILE_SPACE", "archive_dest_2", "ERROR",
             "destination=/u02/arch, status=ERROR, binding=MANDATORY, error=ORA-19502",
             status="CRITICAL"),
    ])

    assert section["summary"]["failedDestinations"] == 1
    assert section["destinations"][0]["error"] == "ORA-19502"


def test_a_sql_server_log_file_space_row_produces_no_redo_section():
    """LOG_FILE_SPACE is per-database log usage on SQL Server — a different fact under the same
    code, already rendered by the databases table. The section is selected by item, never by
    being told which engine it is looking at."""
    section = build_oracle_redo([
        _row("LOG_FILE_SPACE", "Ledger", "3.94",
             "database=Ledger, log_used_pct=3.94, log_size_mb=5485.87, used_log_mb=216.06, "
             "free_log_mb=5269.81"),
    ])

    assert section["logMode"] == "" and section["destinations"] == []


# --------------------------------------------------------------------------- #
# Top SQL
# --------------------------------------------------------------------------- #
def test_a_statement_is_not_cut_at_the_first_comma_in_its_select_list():
    """The shared message parser ends a value at the first comma, which would render every
    multi-column SELECT as its first column. Both variants write `sql=` last for this reason."""
    section = build_oracle_top_sql([
        _row("TOP_DISK_READ_SQL", "437805498", "378431",
             "disk_reads=378431, executions=3, reads_per_exec=126143.67, "
             "sql=SELECT a, b, c FROM t WHERE x = 1, y = 2"),
    ])

    row = section["byDiskReads"][0]
    assert row["sql"] == "SELECT a, b, c FROM t WHERE x = 1, y = 2"
    assert row["executions"] == 3 and row["perExecution"] == 126143.67


def test_the_two_lists_are_kept_apart_because_they_name_different_problems():
    """A statement can top buffer gets while doing no disk I/O at all — 4.4 million executions at
    3.5 gets each is the heaviest thing on the instance and absent from the disk-read list."""
    section = build_oracle_top_sql([
        _row("TOP_DISK_READ_SQL", "970936777", "469950",
             "disk_reads=469950, executions=150, reads_per_exec=3133, sql=select * from PP_COLOR"),
        _row("TOP_BUFFER_GETS_SQL", "1446557931", "15376917",
             "buffer_gets=15376917, executions=4399572, gets_per_exec=3.5, disk_reads=0, "
             "rows_processed=2178201, sql=SELECT PO_NO FROM CT_JO_MASTER WHERE OU_CODE = :b1"),
    ])

    assert section["summary"]["byDiskReads"] == 1 and section["summary"]["byBufferGets"] == 1
    gets = section["byBufferGets"][0]
    assert gets["diskReads"] == 0 and gets["perExecution"] == 3.5
    assert gets["rowsProcessed"] == 2178201


def test_top_sql_is_ranked_by_total_because_that_is_what_the_instance_pays():
    section = build_oracle_top_sql([
        _row("TOP_DISK_READ_SQL", "a", "1000", "disk_reads=1000, executions=1, "
             "reads_per_exec=1000, sql=SELECT 1"),
        _row("TOP_DISK_READ_SQL", "b", "9000", "disk_reads=9000, executions=900, "
             "reads_per_exec=10, sql=SELECT 2"),
    ])

    # b costs the instance nine times more even though a is nine times worse per execution; both
    # numbers are on the row so the reader can tell a bad plan from a busy caller.
    assert [row["sqlId"] for row in section["byDiskReads"]] == ["b", "a"]


# --------------------------------------------------------------------------- #
# Nothing on a non-Oracle server
# --------------------------------------------------------------------------- #
def test_every_oracle_section_is_empty_when_no_oracle_metric_produced_a_row():
    """The template renders "" for an empty section, which is how a SQL Server or PostgreSQL page
    is unchanged without any of these builders knowing the engine."""
    assert build_oracle_instance([])["pools"] == []
    assert build_oracle_instance([])["bufferCache"] is None
    assert build_oracle_objects([])["summary"]["topSegments"] == 0
    assert build_oracle_redo([])["logMode"] == ""
    assert build_oracle_top_sql([])["byDiskReads"] == []
