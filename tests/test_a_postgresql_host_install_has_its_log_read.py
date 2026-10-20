"""The PostgreSQL server log is read on a host install too, not only in a container.

POSTGRES_SERVER_LOG (1.165) read ``docker logs`` and nothing else, and the docker collector skips a
target with no container - so a PostgreSQL installed on the host, whose PANICs, corrupt pages and
full disks are written to files in ``log_directory``, had no server-log reading at all while the
page and the docs read as if it had one. The metric now declares ``host_fallback``: on a target
with no container and an SSH ``cmd_access`` it runs the same script on the host, which reads the
log files and grades them by the same loop.

What a host adds is the way to be wrong about *which* log. Several clusters on one box write
several logs; reading them all would page this target for another cluster's PANIC, so two are
reported by name and not read until ``PG_LOG_FILE`` / ``PG_LOG_DIRECTORY`` says which. And a file
holds more than the window, so a line is cut by its own time - its zone included.

These run the script under bash against files written here and a stub ``docker``; nothing else.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dbabrain.metrics import collector
from dbabrain.metrics.definitions import load_metric_definitions

from conftest import shipped_config

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "dbabrain" / "metrics" / "collectors" / "postgresql" / "023_postgresql_server_log.sh"
BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="needs bash")


def _run(tmp_path: Path, prefix: bytes = b"", **env) -> list[dict]:
    environment = {key: value for key, value in os.environ.items() if not key.startswith(("PG_", "DOCKER_"))}
    environment.update(env)
    # Through a file: as a `bash -c` argument the script would pass through the Windows argv rules.
    runnable = tmp_path / "collector.sh"
    runnable.write_bytes(prefix + SCRIPT.read_bytes().replace(b"\r\n", b"\n"))
    out = subprocess.run([BASH, str(runnable)], capture_output=True, env=environment, timeout=60)
    return json.loads(out.stdout.decode("utf-8"))


def _summary(rows: list[dict]) -> dict:
    return next(row for row in rows if row["metric_item"] == "server_log")


def _stamp(minutes_ago: int) -> str:
    """A log_line_prefix %m time, the way a server at +07 writes it."""
    zone = timezone(timedelta(hours=7))
    moment = datetime.now(zone) - timedelta(minutes=minutes_ago)
    return moment.strftime("%Y-%m-%d %H:%M:%S.123 +07")


def _log(directory: Path, name: str, lines: list[str]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return path


@needs_bash
def test_a_recent_panic_in_a_host_log_file_is_critical(tmp_path):
    logs = tmp_path / "log"
    _log(logs, "postgresql-Thu.log", [
        f"{_stamp(10)} [4121] PANIC:  could not write to file \"pg_wal/xlogtemp.4121\": No space left on device",
        f"{_stamp(30)} [4200] FATAL:  password authentication failed for user \"app\"",
        f"{_stamp(31)} [4200] LOG:  connection received: host=192.0.2.10 port=51234",
    ])

    summary = _summary(_run(tmp_path, PG_LOG_DIRECTORY=str(logs)))

    assert summary["status"] == "CRITICAL"
    assert "critical_recent=1" in summary["message"]
    assert "auth_failures=1" in summary["message"]
    assert "source=host files" in summary["message"]


#: PostgreSQL's own texts for the conditions the metric pages on - not the paraphrases the first
#: pattern list was written from. "checksum verification failed" is pg_basebackup's; a page read
#: that fails its checksum says "page verification failed", and the others were not read at all.
REAL_CRITICAL_LINES = {
    "checksum": ("corruption", "WARNING:  page verification failed, calculated checksum 20919 but expected 15254"),
    "invalid_page": ("corruption", "ERROR:  invalid page in block 7 of relation base/16384/16397"),
    "toast": ("corruption", "ERROR:  missing chunk number 0 for toast value 16490 in pg_toast_16481"),
    "multixact": ("corruption", "ERROR:  MultiXactId 1234567 has not been created yet -- apparent wraparound"),
    "disk_full": ("storage", "ERROR:  could not extend file \"base/16384/16397\": No space left on device"),
    "killed": ("process_crash", "LOG:  server process (PID 4121) was terminated by signal 9: Killed"),
    "restart": ("process_crash", "LOG:  terminating any other active server processes"),
    "recovery": ("process_crash", "LOG:  database system was not properly shut down; automatic recovery in progress"),
}


@needs_bash
@pytest.mark.parametrize("case", sorted(REAL_CRITICAL_LINES))
def test_postgresqls_own_text_for_each_condition_is_recognised(tmp_path, case):
    expected_class, text = REAL_CRITICAL_LINES[case]
    logs = tmp_path / "log"
    _log(logs, "postgresql.log", [f"{_stamp(3)} [4121] {text}"])

    rows = _run(tmp_path, PG_LOG_DIRECTORY=str(logs))

    assert _summary(rows)["status"] == "CRITICAL"
    assert f"latest_class={expected_class}" in _summary(rows)["message"]


@needs_bash
def test_a_line_older_than_the_window_is_cut_by_its_own_time(tmp_path):
    """The file changed today, so it is read - but the PANIC in it is from two days ago."""
    logs = tmp_path / "log"
    _log(logs, "postgresql.log", [
        f"{_stamp(48 * 60)} [77] PANIC:  could not locate a valid checkpoint record",
        f"{_stamp(5)} [78] LOG:  checkpoint complete",
    ])

    summary = _summary(_run(tmp_path, PG_LOG_DIRECTORY=str(logs)))

    assert summary["status"] == "OK"


@needs_bash
def test_an_old_critical_line_inside_the_window_warns_instead_of_paging(tmp_path):
    logs = tmp_path / "log"
    _log(logs, "postgresql.log", [f"{_stamp(5 * 60)} [9] ERROR:  invalid page in block 7 of relation base/5/16384"])

    summary = _summary(_run(tmp_path, PG_LOG_DIRECTORY=str(logs)))

    assert summary["status"] == "WARNING"
    assert "critical_window=1" in summary["message"] and "critical_recent=0" in summary["message"]


@needs_bash
def test_a_line_whose_time_cannot_be_read_never_pages(tmp_path):
    """A custom log_line_prefix without a time: the line is graded, but it is not evidence of now."""
    logs = tmp_path / "log"
    _log(logs, "postgresql.log", ["[123] PANIC:  stuck spinlock detected"])

    summary = _summary(_run(tmp_path, PG_LOG_DIRECTORY=str(logs)))

    assert summary["status"] == "WARNING"


@needs_bash
def test_two_clusters_found_by_the_search_are_named_and_not_read(tmp_path):
    first, second = tmp_path / "16" / "log", tmp_path / "14" / "log"
    _log(first, "postgresql.log", [f"{_stamp(5)} [1] PANIC:  the other cluster"])
    _log(second, "postgresql.log", [f"{_stamp(5)} [2] LOG:  quiet"])

    summary = _summary(_run(tmp_path, PG_LOG_SEARCH_DIRS=f"{first} {second}"))

    assert summary["status"] == "OK"
    assert summary["message"].startswith("not read: logs of 2 clusters")
    assert "PG_LOG_FILE or PG_LOG_DIRECTORY" in summary["message"]


@needs_bash
def test_a_host_with_no_log_found_is_not_configured_rather_than_failing(tmp_path):
    summary = _summary(_run(tmp_path, PG_LOG_SEARCH_DIRS=str(tmp_path / "nowhere")))

    assert summary["status"] == "OK"
    assert summary["message"].startswith("not configured: DOCKER_CONTAINER is not set")
    assert "PG_LOG_FILE" in summary["message"]


@needs_bash
def test_a_container_is_still_read_through_docker_logs(tmp_path):
    panic_at = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
    stub = f"""docker() {{
    case "$1" in
        info) return 0 ;;
        inspect) echo running ;;
        logs) printf '%s\\n' '{panic_at} 2026-10-16 08:00:00.000 UTC [1] PANIC:  could not open file' ;;
    esac
}}
""".encode()

    summary = _summary(_run(tmp_path, prefix=stub, DOCKER_CONTAINER="pg-lab"))

    assert summary["status"] == "CRITICAL"
    assert "source=container pg-lab" in summary["message"]


# --------------------------------------------------------------------------------------------- #
# The collector: a host install is run on its host, through its login
# --------------------------------------------------------------------------------------------- #

class _Target:
    target_id = "ACME-192-0-2-50-PG-5432/postgresql/salesdb"
    db_type = "postgresql"
    ip = "192.0.2.50"
    port = 5432
    metrics_config: dict = {}

    def __init__(self, *, container="", cmd_access=None):
        self.container_name = container
        self.cmd_access = cmd_access or {}


def _definition():
    return next(item for item in load_metric_definitions(shipped_config("metric_definitions.json"))
                if item.metric_code == "POSTGRES_SERVER_LOG")


def test_the_server_log_metric_declares_it_reads_a_host_install():
    assert _definition().host_fallback is True


def test_a_host_install_with_a_login_is_collected_and_one_without_is_skipped_saying_why():
    metric = _definition()
    ssh = {"enabled": True, "method": "ssh", "host": "192.0.2.50"}

    assert collector._metric_unsupported_reason(metric, _Target(cmd_access=ssh)) == ""
    reason = collector._metric_unsupported_reason(metric, _Target())
    assert "no container_name and no SSH cmd_access" in reason
    assert collector._metric_unsupported_reason(metric, _Target(container="pg-lab")) == ""


def test_turning_host_access_off_turns_off_the_host_read_and_not_the_containers():
    metric = _definition()
    ssh = {"enabled": True, "method": "ssh", "host": "192.0.2.50"}
    host = _Target(cmd_access=ssh)
    host.metrics_config = {"disabled_collector_types": ["cmd"]}
    container = _Target(container="pg-lab", cmd_access=ssh)
    container.metrics_config = {"disabled_collector_types": ["cmd"]}

    assert collector._metric_disabled_by_collector_type(metric, host) is True
    assert collector._metric_disabled_by_collector_type(metric, container) is False


def test_only_a_docker_metric_may_declare_a_host_fallback(tmp_path):
    catalogue = json.loads(Path(shipped_config("metric_definitions.json")).read_text(encoding="utf-8"))
    for metric in catalogue["metrics"]:
        if metric["metric_code"] == "OS_CPU_USAGE":
            metric["host_fallback"] = True
    path = tmp_path / "metric_definitions.json"
    path.write_text(json.dumps(catalogue), encoding="utf-8")

    with pytest.raises(Exception, match="host_fallback is for a docker metric"):
        load_metric_definitions(path)
