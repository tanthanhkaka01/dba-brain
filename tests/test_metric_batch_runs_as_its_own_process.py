"""`metric-batch` as the metrics app meets it in production: a process of its own, JSON in and out.

Everywhere else in the suite the command runs in the test's process (conftest), so a test's fakes
reach it. That leaves the one thing only a real process can show: that the request arrives on
stdin, that each item answers on its own - a failed connect next to a script that ran - and that
nothing a driver or a script prints lands inside the JSON the metrics app parses.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ROW = {"metric_item": "probe", "metric_value": "1", "metric_unit": "count", "status": "OK", "message": "ran"}


def _run(request: dict | None, *args: str) -> dict:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONPATH": str(REPO)}
    completed = subprocess.run(
        [sys.executable, "-m", "db_ops.common.cli", "metric-batch", *args],
        input=json.dumps(request) if request is not None else "", capture_output=True, text=True,
        encoding="utf-8", cwd=REPO, env=env, timeout=120)
    return json.loads(completed.stdout)


def _script(tmp_path: Path) -> Path:
    if os.name == "nt":
        script = tmp_path / "rows.cmd"
        script.write_text("@echo " + json.dumps([ROW]) + "\r\n@echo on stderr 1>&2\r\n", encoding="utf-8")
    else:
        script = tmp_path / "rows.sh"
        script.write_text("echo '" + json.dumps([ROW]) + "'\necho 'on stderr' >&2\n", encoding="utf-8")
    return script


def test_each_item_answers_on_its_own_and_the_batch_still_succeeds(tmp_path):
    script = _script(tmp_path)
    answer = _run({
        "target": {"target_id": "LAB", "db_type": "postgresql", "host": "127.0.0.1", "port": 1,
                   "username": "u", "password": "p"},
        "items": [
            {"id": "SCRIPT", "kind": "local", "path": str(script), "timeout_seconds": 30,
             "argv": ["cmd.exe", "/c", str(script)] if os.name == "nt" else ["bash", str(script)]},
            {"id": "SQL", "kind": "sql", "sql": "select 1", "timeout_seconds": 3},
        ],
    }, "-")

    assert answer["success"] is True, answer
    script, sql = answer["data"]["items"]
    assert (script["id"], script["exit_code"]) == ("SCRIPT", 0)
    assert json.loads(script["stdout"]) == [ROW]
    assert "on stderr" in script["stderr"]
    # Graded as "never reached the target", which is what connection_error_severity is for.
    assert sql["error"]["kind"] == "connect" and sql["error"]["failure_phase"] == "connect"
    assert sql["error"]["message"].startswith("Connection failed: ")


def test_the_request_is_refused_anywhere_but_on_stdin():
    """It carries a target's password - inline it would sit on the process list."""
    answer = _run(None, '{"target": {}, "items": []}')
    assert answer["success"] is False
    assert "stdin" in answer["error"]
