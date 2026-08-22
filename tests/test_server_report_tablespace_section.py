"""Oracle storage on the page that is supposed to describe an Oracle instance.

`server-metrics.html` had every tablespace number in the store and nowhere to read one. The
collected rows rendered as ten sparklines all titled "Tablespace free space", with the tablespace
name reachable only through a chart tooltip, and the datafiles as more cards saying "15000 MB"
with nothing saying which tablespace they extend. The first thing an Oracle DBA looks up was the
one thing the page could not answer.

Three of the numbers only mean anything together, and each of them has a way of being read wrong:

- **Free-right-now is not what is available.** RBS on 192.0.2.236 has 2.3 GB free inside 65 GB
  of autoextend headroom. Reported alone it reads as a tablespace about to fill, and the datafile
  added on that reading was never needed. The row leads with the effective figure.
- **Headroom from files that cannot autoextend is not headroom.** So the count of autoextending
  files travels with it; a tablespace where that count is zero can only ever use what is already
  allocated.
- **The largest free extent is what an allocation has to fit in.** Free space held in small
  fragments still raises ORA-01653, which no percentage on the row would predict.

`STORAGE_DATA_FILE_SPACE` is the same metric code on all three engines and means something
different on each — a datafile on Oracle, a per-file fullness percentage on SQL Server, a database
size on PostgreSQL. Only the rows that name a `tablespace=` are Oracle's, and that is the test
below that stops this section inventing Oracle storage on a server that has none.
"""

from db_ops.reports.server_report import build_tablespaces


def _tablespace(name, message, status="OK"):
    return {"metric_code": "TABLESPACE_FREE_SPACE", "metric_item": name,
            "status": status, "message": message, "collected_at": "2026-08-17T05:45:18Z"}


def _datafile(item, message, status="OK"):
    return {"metric_code": "STORAGE_DATA_FILE_SPACE", "metric_item": item,
            "status": status, "message": message, "collected_at": "2026-08-17T05:28:06Z"}


RBS = _tablespace(
    "RBS",
    "tablespace=RBS, effective_free_mb=65418.97 (99.8% of max), free_now_mb=2348.98, "
    "autoextend_headroom_mb=63069.98, allocated_mb=2465, max_mb=65534.98, "
    "datafiles=2 (autoextend=2), largest_free_extent_mb=14.49",
)


def test_the_headline_free_figure_includes_the_autoextend_headroom():
    section = build_tablespaces([RBS])

    row = section["tablespaces"][0]
    assert row["effectiveFreeMB"] == 65418.97
    assert row["freeNowMB"] == 2348.98
    assert row["autoextendHeadroomMB"] == 63069.98
    # 99.8% of the maximum is free, so 0.2% is used: nearly empty, not nearly full. The figure is
    # the collector's own — recomputing it here would let the page and the alert round the same
    # sample to two different numbers.
    assert row["usedPct"] == 0.2


def test_used_percent_is_measured_against_the_maximum_not_against_what_is_allocated_today():
    """A tablespace allocated 2.4 GB of a 64 GB ceiling is not 95% used because its current
    files are nearly full — autoextend is what it will do next, and the page has to say so."""
    section = build_tablespaces([RBS])

    assert section["tablespaces"][0]["allocatedMB"] == 2465
    assert section["tablespaces"][0]["maxMB"] == 65534.98
    assert section["tablespaces"][0]["usedPct"] < 1


def test_a_tablespace_carries_the_datafiles_that_extend_it():
    section = build_tablespaces([
        _tablespace("USERS",
                    "tablespace=USERS, effective_free_mb=54068.72 (82.5% of max), "
                    "free_now_mb=4533.73, autoextend_headroom_mb=49534.98, allocated_mb=16000, "
                    "max_mb=65534.98, datafiles=2 (autoextend=2), largest_free_extent_mb=3601.62"),
        _datafile("USERS:D:\\ORACLE\\ORADATA\\LEGACYDB\\USERS01.DBF",
                  "tablespace=USERS, file=D:\\ORACLE\\ORADATA\\LEGACYDB\\USERS01.DBF, size_mb=15000"),
        _datafile("USERS:D:\\ORACLE\\ORADATA\\LEGACYDB\\USERS02.BDF",
                  "tablespace=USERS, file=D:\\ORACLE\\ORADATA\\LEGACYDB\\USERS02.BDF, size_mb=1000"),
    ])

    row = section["tablespaces"][0]
    # Largest first: the file somebody is about to extend is the one already carrying the data.
    assert [f["sizeMB"] for f in row["files"]] == [15000, 1000]
    assert row["fileCount"] == 2
    assert row["autoextendFiles"] == 2


def test_a_sql_server_data_file_row_is_not_filed_under_a_tablespace():
    """The same metric code carries a per-file fullness percentage on SQL Server and a database
    size on PostgreSQL. Neither names a tablespace, and grouping them under one would put Oracle
    storage on a server that has none."""
    section = build_tablespaces([
        _datafile("PowerPick:PowerPick",
                  "database=PowerPick, file=PowerPick, used_pct=92.67, size_mb=11616.25, "
                  "free_mb=851.25, growth_mb=10%, is_percent_growth=1, max_size_mb=UNLIMITED"),
        _datafile("db_ops", "database=db_ops, size=8537 MB"),
    ])

    assert section["tablespaces"] == []
    assert section["orphanFiles"] == []
    assert section["summary"]["datafiles"] == 0


def test_a_datafile_whose_tablespace_reported_nothing_is_still_listed():
    """A tablespace missing from TABLESPACE_FREE_SPACE is itself the finding. Dropping its files
    would hide storage that exists behind the absence of the row that should have described it."""
    section = build_tablespaces([
        _datafile("TOOLS:D:\\ORACLE\\ORADATA\\LEGACYDB\\TOOLS02.BDF",
                  "tablespace=TOOLS, file=D:\\ORACLE\\ORADATA\\LEGACYDB\\TOOLS02.BDF, size_mb=1000"),
    ])

    assert [f["tablespace"] for f in section["orphanFiles"]] == ["TOOLS"]
    assert section["summary"]["datafiles"] == 1


def test_a_temporary_tablespace_reports_its_high_water_mark_not_its_idle_usage():
    """Current temp usage is near zero between sorts. ORA-01652 is measured against the peak, so
    a temp tablespace that failed a report last night must not read as completely idle."""
    section = build_tablespaces([
        _tablespace("LEGACYDB_TMP",
                    "tablespace=LEGACYDB_TMP, effective_free_mb=34062.84 (99.4% of max), "
                    "free_now_mb=2295.84, autoextend_headroom_mb=31767, allocated_mb=2500, "
                    "max_mb=34267, datafiles=2 (autoextend=1), largest_free_extent_mb=1184.05"),
        {"metric_code": "STORAGE_TEMP_SPACE", "metric_item": "LEGACYDB_TMP", "status": "OK",
         "collected_at": "2026-08-17T05:33:50Z",
         "message": "temp tablespace=LEGACYDB_TMP, used_mb=0, max_used_mb=204.14, total_mb=204.14, "
                    "current_sorts=0, extent_hits=20412"},
    ])

    row = section["tablespaces"][0]
    assert row["temp"] is True
    assert row["tempUsedMB"] == 0 and row["tempMaxUsedMB"] == 204.14
    assert section["summary"]["temp"] == 1


def test_tempdb_does_not_become_a_tablespace():
    """SQL Server writes STORAGE_TEMP_SPACE too, and states no tablespace name in it."""
    section = build_tablespaces([
        {"metric_code": "STORAGE_TEMP_SPACE", "metric_item": "tempdb", "status": "OK",
         "message": "tempdb_used_pct=7.96, total_mb=3080.00, used_mb=245.13, free_mb=2803.63"},
    ])

    assert section["tablespaces"] == [] and section["summary"]["temp"] == 0


def test_a_tablespace_no_file_can_autoextend_is_counted_so_its_headroom_is_not_believed():
    section = build_tablespaces([
        _tablespace("SYSTEM",
                    "tablespace=SYSTEM, effective_free_mb=120.5 (12.0% of max), free_now_mb=120.5, "
                    "autoextend_headroom_mb=0, allocated_mb=1000, max_mb=1000, "
                    "datafiles=1 (autoextend=0), largest_free_extent_mb=64",
                    status="WARNING"),
        RBS,
    ])

    assert section["summary"]["noAutoextend"] == 1
    assert section["summary"]["warning"] == 1
    # Worst first: the tablespace that cannot grow is why the section is opened.
    assert [row["name"] for row in section["tablespaces"]] == ["SYSTEM", "RBS"]


def test_a_failed_collection_does_not_become_a_nameless_tablespace():
    section = build_tablespaces([
        {"metric_code": "TABLESPACE_FREE_SPACE", "metric_item": None, "status": "OK",
         "message": "SQL returned no rows."},
    ])

    assert section["tablespaces"] == []


def test_a_server_with_no_oracle_storage_produces_an_empty_section():
    """The template renders nothing for an empty section, which is how every engine but Oracle
    gets the right page without the builder being told which engine it is looking at."""
    section = build_tablespaces([])

    assert section["tablespaces"] == [] and section["summary"]["tablespaces"] == 0
