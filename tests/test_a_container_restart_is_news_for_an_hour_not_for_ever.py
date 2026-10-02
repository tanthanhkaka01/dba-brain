"""A container's restart warns while it is news, and a docker field cannot break the metric's JSON.

The docker stats collector (review 0.25.0, F3.4):

* **`restart_count > 0` was WARNING for ever.** Docker's count only grows, so one restart months ago
  warned on every pass. A policy restart resets ``StartedAt``; a restart now warns while the current
  run is younger than ``DOCKER_RESTART_WARN_MINUTES`` (60 by default), and older restarts are still
  named in the message at OK.
* **`json_escape` escaped backslash and quote only.** A tab or a newline in any docker field - here
  the container's own name - made the whole metric "stdout is not valid JSON".

The script runs here for real, under bash, against a fake ``docker`` on PATH.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from db_ops.metrics import collector

SCRIPT = Path(collector.__file__).parent / "collectors" / "docker" / "009_docker_container_stats.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="needs bash")

# A shell function, not a file on PATH: a function wins over any docker installed here, on every
# platform, and the real one is never asked.
FAKE_DOCKER = """docker() {
    case "$1" in
        inspect) printf 'running|true|false|%s|%s\\n' "$FAKE_RESTARTS" "$FAKE_STARTED_AT" ;;
        stats) printf '1.50%%|10.00%%|100MiB / 1GiB|1kB / 2kB|0B / 0B|7\\n' ;;
    esac
}
"""


def _run(tmp_path: Path, *, restarts: int, started_minutes_ago: int, container: str = "db_lab",
         **env) -> dict[str, dict]:
    started = datetime.now(timezone.utc) - timedelta(minutes=started_minutes_ago)
    environment = {**os.environ, "DOCKER_CONTAINER": container, "FAKE_RESTARTS": str(restarts),
                   "FAKE_STARTED_AT": started.strftime("%Y-%m-%dT%H:%M:%S.000000000Z"), **env}
    # Through a file: as a `bash -c` argument the script would pass through the Windows argv rules,
    # which rewrite its quotes and backslashes before bash sees them.
    runnable = tmp_path / "collector.sh"
    runnable.write_bytes(FAKE_DOCKER.encode() + SCRIPT.read_bytes().replace(b"\r\n", b"\n"))
    out = subprocess.run([BASH, str(runnable)], capture_output=True, env=environment, timeout=60)
    rows = json.loads(out.stdout.decode("utf-8"))
    return {row["metric_item"].split(":", 1)[1]: row for row in rows}


def test_a_restart_months_ago_is_history(tmp_path):
    rows = _run(tmp_path, restarts=1, started_minutes_ago=90 * 24 * 60)

    assert rows["restart_count"]["status"] == "OK"
    assert "restarted 1 time(s)" in rows["restart_count"]["message"]


def test_a_restart_in_the_last_hour_warns(tmp_path):
    rows = _run(tmp_path, restarts=2, started_minutes_ago=5)

    assert rows["restart_count"]["status"] == "WARNING"


def test_the_window_is_the_operator_s(tmp_path):
    rows = _run(tmp_path, restarts=1, started_minutes_ago=90, DOCKER_RESTART_WARN_MINUTES="120")

    assert rows["restart_count"]["status"] == "WARNING"


def test_a_tab_or_newline_in_a_docker_field_keeps_the_json_valid(tmp_path):
    rows = _run(tmp_path, restarts=0, started_minutes_ago=5, container="db\tlab\nx")

    assert rows["status"]["metric_item"] == "db\tlab\nx:status"
