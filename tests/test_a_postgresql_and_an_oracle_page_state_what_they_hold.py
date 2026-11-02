"""A PostgreSQL or an Oracle server page states what SQL Server's states - from what it really reads.

Asked on 2026-10-26, reading a PostgreSQL store's page: *how much CPU of how many cores, how many GB
the database uses, how many files and how large each* - SQL Server's page says all of it, and this
one said ``CPU 319%`` (``docker stats`` counts in CPUs), ``Disk space 48803043007 bytes`` (a
database's size, not a disk), and of an Oracle 8i ``CPU not collected`` beside a Workload section
reading 0 % all day, and a database row with no size. Then the operator's rule for all of it:
*don't hard-code, show what is real, don't guess.*

So every figure below is one a collector read - a container's CPU limit or the host's count from the
daemon, ``df`` inside the container, the instance's own ``timed_statistics``, the WAL directory - and
where nothing was read the page says so rather than choosing a number.
"""

from __future__ import annotations

from pathlib import Path

from dbabrain.reports import inventory_health
from dbabrain.reports.server_health import _value_text, build_areas
from dbabrain.reports.server_report import _per_engine

ROOT = Path(__file__).resolve().parents[1]
COLLECTORS = ROOT / "dbabrain" / "metrics" / "collectors"


def _entry(code, item, value, unit, message, status="OK"):
    return {"code": code, "label": code, "item": item, "unit": unit, "status": status,
            "numeric": True, "static": False, "last": value, "lastText": str(value),
            "message": message, "lastAt": 1_792_900_000, "points": [], "tier": "primary"}


def _area(series, key, workload=None):
    return next(area for area in build_areas(series, workload=workload) if area["key"] == key)


# --------------------------------------------------------------------------- CPU of a container
def test_a_containers_cpu_is_its_share_of_the_cpus_it_may_use_and_says_which():
    share = _entry("DOCKER_CONTAINER_STATS", "store:cpu_share", 16.86, "percent",
                   "cpu_share_pct=16.86, cpus_busy=1.35, cpus=8, cpus_basis=host_no_limit, host_cpus=8")
    raw = _entry("DOCKER_CONTAINER_STATS", "store:cpu", 134.85, "percent",
                 "cpu=134.85%, cpus=8, cpus_basis=host_no_limit, host_cpus=8")
    cpu = _area([raw, share], "cpu")

    assert cpu["value"] == "16.86%"
    assert cpu["capacity"] == "1.35 of 8 CPUs busy"
    assert cpu["capacityNote"] == "No CPU limit is set on the container: its share is of the host's 8 CPUs."


def test_a_row_that_never_read_the_cpu_count_gets_none_chosen_for_it():
    raw = _entry("DOCKER_CONTAINER_STATS", "store:cpu", 319.0, "percent", "cpu=319%")
    cpu = _area([raw], "cpu")

    assert cpu["capacity"] == ""
    assert "does not say how many CPUs" in cpu["capacityNote"]


# --------------------------------------------------------------------------- CPU of an engine
def _workload(cpu_pct=None, timed=""):
    groups = [] if cpu_pct is None else [{"name": "CPU", "rows": [
        {"key": "cpu_pct", "label": "Database CPU", "unit": "pct", "source": "v$sysstat",
         "values": {"now": {"total": cpu_pct}, "hour": {"total": cpu_pct}, "day": {"total": cpu_pct}}}]}]
    return {"cpu": {"total": 16, "online": 16, "engine": "Oracle", "timedStatistics": timed},
            "groups": groups, "asOf": "2026-10-26T04:00:00Z"}


def test_an_oracle_with_no_host_reads_its_cpu_from_its_own_counters():
    cpu = _area([], "cpu", _workload(cpu_pct=12.5, timed="TRUE"))

    assert (cpu["status"], cpu["value"]) == ("OK", "12.5%")
    assert cpu["capacity"] == "16 / 16 logical CPUs online / total"


def test_an_instance_that_times_nothing_says_so_instead_of_zero():
    """timed_statistics=FALSE keeps CPU used by this session at 0 however busy the instance is."""
    cpu = _area([], "cpu", _workload(cpu_pct=0.0, timed="FALSE"))

    assert (cpu["status"], cpu["value"]) == ("UNKNOWN", "not timed")
    assert cpu["detail"].startswith("timed_statistics=FALSE on this instance")


# --------------------------------------------------------------------------- disk
def test_a_containers_volume_is_the_disk_tile_with_its_size_and_free_space():
    volume = _entry("DOCKER_CONTAINER_STATS", "store:disk:/var/lib/postgresql", 85.6, "percent",
                    "mount=/var/lib/postgresql, filesystem=/dev/mapper/vg-lv, total_gb=488.58, "
                    "used_gb=400.34, free_gb=67.32, used_pct=85.60", status="WARNING")
    size = _entry("STORAGE_DATA_FILE_SPACE", "app_store", 48803043007.0, "bytes", "database=app_store, size=45 GB")
    disk = _area([size, volume], "disk_space")

    assert (disk["status"], disk["value"]) == ("WARNING", "85.6%")
    assert disk["capacity"].startswith("/var/lib/postgresql 400 / 489 GB used / total")


def test_a_size_is_never_read_as_how_full_a_disk_is():
    oracle_file = _entry("STORAGE_DATA_FILE_SPACE", "USERS:D:\\USERS01.DBF", 20000.0, "MB",
                         "tablespace=USERS, file=D:\\USERS01.DBF, size_mb=20000")
    pg_database = _entry("STORAGE_DATA_FILE_SPACE", "app_store", 48803043007.0, "bytes",
                         "database=app_store, size=45 GB")

    assert _area([oracle_file, pg_database], "disk_space")["value"] == "not collected"


def test_a_size_in_bytes_is_printed_in_a_unit_a_reader_reads():
    assert _value_text(_entry("X", "app_store", 48803043007.0, "bytes", "")) == "45.45 GB"


# --------------------------------------------------------------------------- databases
def test_an_oracle_database_is_the_size_and_count_of_its_data_files():
    code_map = {
        ("DATABASE_STATUS", "SALES"): {"metric_value": "READ WRITE", "message": "database=SALES, log_mode=NOARCHIVELOG",
                                     "status": "OK", "collected_at": "2026-10-26T04:00:00Z"},
        ("STORAGE_DATA_FILE_SPACE", "USERS:a"): {"metric_value": "20000", "status": "OK",
                                                 "message": "tablespace=USERS, file=a, size_mb=20000",
                                                 "collected_at": "2026-10-26T04:00:00Z"},
        ("STORAGE_DATA_FILE_SPACE", "USERS:b"): {"metric_value": "1000", "status": "OK",
                                                 "message": "tablespace=USERS, file=b, size_mb=1000",
                                                 "collected_at": "2026-10-26T04:00:00Z"},
    }
    row = inventory_health.build_database_health(code_map)[0]

    assert (row["data_size_gb"], row["data_files"]) == (20.51, 2)


def test_an_engine_with_no_checkdb_reads_not_applicable_and_postgresql_states_its_wal():
    databases = {"databases": [{"name": "app_store", "checkdb": ""}], "summary": {"neverCheckdb": 1}}
    freshness = {"metrics": [{"code": "DATABASE_STATUS"}], "notCollected": [{"code": "DATABASE_DATA_SIZE"}]}
    code_map = {("LOG_FILE_SPACE", "pg_wal"): {
        "status": "OK", "message": "wal_files=30, wal_size_mb=480.00, max_wal_size_mb=4096"}}
    out = _per_engine(databases, code_map, freshness)

    assert out["databases"][0]["checkdb"] is None
    assert out["summary"]["neverCheckdb"] == 0
    assert out["summary"]["wal"] == {"files": 30.0, "sizeMB": 480.0, "maxWalSizeMB": 4096.0, "status": "OK"}


def test_sql_servers_checkdb_column_is_left_as_it_is():
    databases = {"databases": [{"name": "master", "checkdb": "never"}], "summary": {"neverCheckdb": 1}}
    freshness = {"metrics": [{"code": "DATABASE_CHECKDB"}], "notCollected": []}

    assert _per_engine(databases, {}, freshness)["summary"]["neverCheckdb"] == 1


# --------------------------------------------------------------------------- what the collectors read
def test_the_collectors_read_what_the_page_now_states():
    docker = (COLLECTORS / "docker" / "009_docker_container_stats.sh").read_text(encoding="utf-8")
    assert "{{.NCPU}}" in docker and "{{.HostConfig.NanoCpus}}" in docker
    assert 'docker exec "$container" df -P -B1' in docker
    for basis in ("limit_cpus", "limit_cpu_quota", "cpuset_", "host_no_limit"):
        assert f'cpus_basis="{basis}' in docker            # one token: a comma would cut the value

    for path in ("oracle/081_oracle_workload_counters.sql",
                 "oracle/legacy_8i/081_oracle_workload_counters.sql"):
        assert "timed_statistics=" in (COLLECTORS / path).read_text(encoding="utf-8"), path

    wal = (COLLECTORS / "postgresql" / "007_postgresql_wal_space.sql").read_text(encoding="utf-8")
    assert "pg_ls_waldir()" in wal and "max_wal_size" in wal
