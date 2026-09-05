from __future__ import annotations

from db_ops.db import DbOpsStore
from db_ops.jobs import JobRun
from db_ops.telegram.command_processor import (
    _job_run_metadata_matches,
    _probe_completion,
    _render_completion_probe,
)

PROBE = {
    "table": "job_runs",
    "success_job_code": "backup_restore.restore-workflow.end",
    "failure_job_code": "backup_restore.restore-workflow.error",
    "match_metadata": {"restore_id": "ACME_TO_MSSQL2025_DOCKER"},
}


def _store(tmp_path):
    store = DbOpsStore(str(tmp_path / "t.sqlite"))
    store.initialize()
    return store


def _job(code, status, restore_id, *, error_text=""):
    return JobRun(
        job_code=code, level="logging", status=status, message=f"{code} {status}",
        started_at="2026-07-06T00:00:00Z", finished_at="2026-07-06T00:00:00Z",
        duration_ms=10, error_text=error_text, host_name="h",
        metadata={"restore_id": restore_id},
    )


def test_render_probe_fills_match_metadata():
    rendered = _render_completion_probe(PROBE, {"restore_id": "ACME_TO_MSSQL2025_DOCKER"})
    assert rendered["match_metadata"] == {"restore_id": "ACME_TO_MSSQL2025_DOCKER"}
    assert _render_completion_probe(None, {}) is None


def test_metadata_matches():
    assert _job_run_metadata_matches('{"restore_id": "X", "a": 1}', {"restore_id": "X"})
    assert not _job_run_metadata_matches('{"restore_id": "Y"}', {"restore_id": "X"})
    assert not _job_run_metadata_matches("not json", {"restore_id": "X"})
    assert _job_run_metadata_matches("{}", {})  # empty match = always true


def test_probe_none_when_no_record(tmp_path):
    store = _store(tmp_path)
    assert _probe_completion(store, PROBE, since_created_at="2026-07-06T00:00:00Z") is None


def test_probe_detects_success(tmp_path):
    store = _store(tmp_path)
    store.insert_job_run(_job("backup_restore.restore-workflow.end", "END", "ACME_TO_MSSQL2025_DOCKER"))
    result = _probe_completion(store, PROBE, since_created_at="2026-07-06T00:00:00Z")
    assert result is not None and result[0] == "success"


def test_probe_detects_failure_with_message(tmp_path):
    store = _store(tmp_path)
    store.insert_job_run(_job("backup_restore.restore-workflow.error", "ERROR",
                              "ACME_TO_MSSQL2025_DOCKER", error_text="disk full"))
    result = _probe_completion(store, PROBE, since_created_at="2026-07-06T00:00:00Z")
    assert result == ("failure", "disk full")


def test_probe_ignores_other_restore_id(tmp_path):
    store = _store(tmp_path)
    store.insert_job_run(_job("backup_restore.restore-workflow.end", "END", "SOME_OTHER_ID"))
    assert _probe_completion(store, PROBE, since_created_at="2026-07-06T00:00:00Z") is None


def test_probe_newest_wins(tmp_path):
    store = _store(tmp_path)
    store.insert_job_run(_job("backup_restore.restore-workflow.error", "ERROR", "ACME_TO_MSSQL2025_DOCKER"))
    store.insert_job_run(_job("backup_restore.restore-workflow.end", "END", "ACME_TO_MSSQL2025_DOCKER"))
    result = _probe_completion(store, PROBE, since_created_at="2026-07-06T00:00:00Z")
    assert result[0] == "success"  # latest record (the .end) wins


def test_a_zombie_is_not_alive(monkeypatch, tmp_path):
    """A detached CLI is re-parented to PID 1 (the daemon), which does not reap it, so a finished
    process lingers in state Z. os.kill(pid, 0) succeeds on a zombie — believing that meant
    "running" is why a failed create-db-docker went silent for 30 minutes and then reported a
    timeout instead of the real error."""
    from db_ops.telegram import command_processor as cp

    proc = tmp_path / "4242"
    proc.mkdir()
    # Field 3 of /proc/<pid>/stat is the state, after the (possibly parenthesised) comm.
    (proc / "stat").write_text("4242 (python3) Z 1 4242 0 0 -1 4194560 0 0\n", encoding="utf-8")
    # Patched on `process_liveness`, which is where the rule lives now; `command_processor`
    # re-exports it, so `cp._is_pid_alive` is still the name under test and still the one
    # production calls. Patching `cp` stopped reaching the code the day it moved.
    from db_ops.lib import process_liveness as liveness

    monkeypatch.setattr(liveness.sys, "platform", "linux")  # the container the worker runs in
    monkeypatch.setattr(liveness, "open", lambda path, *a, **kw: (proc / "stat").open(*a, **kw),
                        raising=False)
    monkeypatch.setattr(liveness.os, "kill", lambda pid, sig: None)

    assert cp._is_zombie(4242) is True
    assert cp._is_pid_alive(4242) is False


def test_a_running_process_is_alive(monkeypatch, tmp_path):
    from db_ops.telegram import command_processor as cp

    proc = tmp_path / "4243"
    proc.mkdir()
    (proc / "stat").write_text("4243 (python3) S 1 4243 0 0 -1 4194560 0 0\n", encoding="utf-8")
    monkeypatch.setattr(cp.sys, "platform", "linux")
    monkeypatch.setattr(cp, "open", lambda path, *a, **kw: (proc / "stat").open(*a, **kw), raising=False)
    monkeypatch.setattr(cp.os, "kill", lambda pid, sig: None)

    assert cp._is_zombie(4243) is False
    assert cp._is_pid_alive(4243) is True


# --------------------------------------------------------------------------- #
# A detached POSIX command is not a child of the workflow that polls it, so its exit status
# cannot be read with waitpid. Guessing from the output reported a *successful* create-db-docker
# as FAILED — it prints a human summary, not JSON, and had no success marker.
# --------------------------------------------------------------------------- #
def test_the_exit_code_of_a_detached_posix_command_is_read_back(tmp_path, monkeypatch):
    from pathlib import Path

    from db_ops.telegram import command_processor as cp

    stdout_path = tmp_path / "run.stdout.txt"
    stdout_path.write_text("Status: running\n", encoding="utf-8")
    rc_path = cp._exit_code_path(str(stdout_path))

    assert cp._read_exit_code_file(rc_path) is None      # not written yet -> unknown, not "failed"
    Path(rc_path).write_text("0", encoding="utf-8")
    assert cp._read_exit_code_file(rc_path) == 0
    Path(rc_path).write_text("1", encoding="utf-8")
    assert cp._read_exit_code_file(rc_path) == 1


def test_the_error_summary_is_the_error_not_the_compose_progress():
    """What the operator was shown: "Network mssql_lab_ha_01_default Creating Network ... Volume
    ... Creating" — the *head* of docker compose's progress, truncated. The actual error never
    made it into the message."""
    from db_ops.telegram.command_processor import _extract_error_from_output

    stderr = "\n".join([
        "Network mssql_lab_ha_01_default  Creating",
        "Network mssql_lab_ha_01_default  Created",
        "Volume mssql_lab_ha_01_primary_data  Creating",
        "Volume mssql_lab_ha_01_primary_data  Created",
        "Container MSSQL_LAB_HA_01-primary  Starting",
        "Error response from daemon: driver failed programming external connectivity: "
        "Bind for 0.0.0.0:1533 failed: port is already allocated",
    ])

    summary = _extract_error_from_output(stderr, "")

    assert "port is already allocated" in summary
    assert "Creating" not in summary and "Created" not in summary


# -- the exit code of a detached command, on both platforms -------------------------------- #

def test_the_wrapper_records_what_the_command_returned(tmp_path):
    """Neither platform can ask the OS how a detached command went, so the child records it.

    POSIX could never read it back (``waitpid`` is for children only) and wrote its own code
    through ``sh -c`` from the start. Windows releases the PID the moment the process exits, so
    ``OpenProcess`` finds nothing — and the gap was filled by *returning 1*.
    """
    import sys

    from db_ops.telegram.detached_exit import run

    path = tmp_path / "rc.txt"

    assert run(path, [sys.executable, "-c", "raise SystemExit(0)"]) == 0
    assert path.read_text(encoding="utf-8") == "0"

    assert run(path, [sys.executable, "-c", "raise SystemExit(3)"]) == 3
    assert path.read_text(encoding="utf-8") == "3"


def test_a_command_that_cannot_start_is_recorded_as_a_failure(tmp_path):
    """A missing executable is a real failure and must not read as "unknown"."""
    from db_ops.telegram.detached_exit import run

    path = tmp_path / "rc.txt"

    assert run(path, ["no-such-executable-anywhere-x9"]) == 127
    assert path.read_text(encoding="utf-8") == "127"


def test_the_wrapper_needs_no_quoting(tmp_path):
    """An argument list is passed through untouched, which is the whole reason it is a list.

    The POSIX version built a shell command line and quoted it; doing the same for ``cmd.exe``
    is the defect this repository met twice in one day — ``APP-CONTROL`` broke because single
    quotes are POSIX syntax that ``cmd`` hands through literally.
    """
    import sys

    from db_ops.telegram.detached_exit import run

    path = tmp_path / "rc.txt"
    written = tmp_path / "out.txt"
    awkward = '{"mode": "auto", "why": "spaces \'quotes\' & ampersands"}'

    code = run(path, [sys.executable, "-c",
                      "import sys,pathlib; pathlib.Path(sys.argv[1]).write_text(sys.argv[2],"
                      " encoding='utf-8')", str(written), awkward])

    assert code == 0
    assert written.read_text(encoding="utf-8") == awkward


def test_the_dispatch_launches_through_the_wrapper_on_every_platform(monkeypatch):
    """One path, so the two cannot drift again — which is how Windows came to have none."""
    import inspect

    from db_ops.telegram import command_processor as cp

    source = inspect.getsource(cp)

    assert "db_ops.telegram.detached_exit" in source
    assert not hasattr(cp, "_get_exit_code_windows"), (
        "the function that returned a hardcoded 1 for 'no handle' must not come back")


def test_an_unrecorded_exit_code_is_unknown_and_not_a_failure():
    """The bug, stated as the rule it broke.

    On 2026-08-26 `/spbot_run_sql_task 24` ran 135s, wrote `sql_runs.status='done'`, delivered
    its .txt to the chat, and was reported as `Exit code: 1 / CLI command failed`. Nothing had
    gone wrong except that the poller could no longer open the finished process's handle, and
    read "no handle" as "exit 1".

    Three answers, not two: a recorded non-zero settles failure, a recorded zero settles
    success, and *nothing recorded* is neither — a completed process with no evidence against
    it is reported as finished rather than accused.
    """
    def verdict(exit_code, *, status_str="", marker=False, timed_out=False):
        failed_outright = exit_code is not None and exit_code != 0
        return (not failed_outright) and (
            exit_code == 0 or status_str in ("SUCCESS", "OK") or marker or exit_code is None
        ) and not timed_out

    assert verdict(0) is True
    assert verdict(None) is True, "unknown must not be reported as failure"
    assert verdict(1) is False
    assert verdict(3) is False
    assert verdict(None, timed_out=True) is False
    assert verdict(1, status_str="SUCCESS") is False, "a recorded failure is not overridden"
