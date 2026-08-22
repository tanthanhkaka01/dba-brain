"""A scheduled restore has to report that it finished.

It did not. The events for the ``workflow`` subcommand live in the CLI's main(), which returns
before reaching them, and run_scheduled_restores assumed run_restore_workflow emitted them. So
the only message an operator ever saw was from the certificate sub-step buried in
restore_database.py - a restore that finished in 16 minutes read as "hung at certificate import"
for hours, and a restore that *failed* said nothing at all.
"""

from types import SimpleNamespace

import pytest

from db_ops.backup_restore import workflow
from conftest import shipped_config


class _Recorder:
    """Captures the events the workflow emits instead of writing them anywhere."""

    def __init__(self):
        self.events = []

    def __call__(self, **kwargs):
        self.events.append(kwargs)

    def phases(self, command="restore-workflow"):
        return [e["phase"] for e in self.events if e.get("command") == command]


@pytest.fixture
def harness(monkeypatch, tmp_path):
    recorder = _Recorder()
    monkeypatch.setattr(workflow, "emit_backup_restore_event", recorder)

    class _Store:

        @classmethod
        def from_config(cls, config, **kwargs):
            """Store doubles must offer the same constructor contract as the real classes."""
            return cls(getattr(config, 'sqlite_path', None))
        def __init__(self, *a, **k): pass
        def fetch_latest_job_runs_by_job_code(self): return {}
        def insert_job_run(self, *a, **k): return 1
        def update_job_run(self, *a, **k): return None
        def fetch_running_job_runs(self, *a, **k): return []

    monkeypatch.setattr(workflow, "DbOpsStore", _Store)
    monkeypatch.setattr(workflow.schedule, "reap_stale_runs", lambda **k: [])
    monkeypatch.setattr(workflow.schedule, "start_run", lambda **k: (1, "2026-07-28T00:00:00Z"))
    monkeypatch.setattr(workflow.schedule, "finish_run", lambda **k: "2026-07-28T00:00:10Z")
    monkeypatch.setattr(workflow, "load_script_restores", lambda _p: [])
    return recorder


def _config(restore_id="R1", active=True):
    return SimpleNamespace(
        restore_id=restore_id, active=active, source_id="S", target_id="T",
        notify={"alert_on_error": {"enabled": 1}}, time_window=SimpleNamespace(timeout=7200),
    )


def _run(monkeypatch, harness, *, outcome, output=None):
    """Run one due restore whose inner workflow either succeeds, returns ``output``, or raises."""
    monkeypatch.setattr(workflow, "load_restore_configs", lambda _p: [_config()])

    def _inner(**kwargs):
        if outcome == "error":
            raise RuntimeError("RESTORE DATABASE failed: media set has 2 media families")
        return output if output is not None else {"status": "SUCCESS"}

    import db_ops.backup_restore.cli as cli
    monkeypatch.setattr(cli, "run_restore_workflow", _inner)

    return workflow.run_scheduled_restores(
        app_config=SimpleNamespace(sqlite_path=":memory:"), config_path="x.json", force=True,
    )


def test_a_successful_restore_announces_that_it_finished(monkeypatch, harness):
    summary = _run(monkeypatch, harness, outcome="done")

    assert summary["succeeded"] == 1
    assert harness.phases() == ["START", "END"]
    assert "finished: done" in harness.events[-1]["message"]


def test_a_failed_restore_reports_the_failure_rather_than_going_quiet(monkeypatch, harness):
    """The silent case that matters most: without an ERROR event a failed restore was
    indistinguishable from one still running."""
    summary = _run(monkeypatch, harness, outcome="error")

    assert summary["failed"] == 1
    assert harness.phases() == ["START", "ERROR"]
    end = harness.events[-1]
    assert end["level"] == "error"
    assert "media set" in (end["error_text"] or "")


def test_a_restore_that_lost_a_database_is_not_reported_as_done(monkeypatch, harness):
    """The 2026-08-08 02:00 drill. Five of six databases restored, APPDB_Prod did not, and the run
    sent "✅ Restore workflow finished. status=done" - because a per-database failure is caught so
    the other databases still run, and the workflow read "it did not raise" as "it worked". The
    database sat unreachable in RESTORING with nothing anywhere saying so."""
    summary = _run(monkeypatch, harness, outcome="done", output={
        "status": "SUCCESS",
        "per_database_restore_status": {
            "SALESDB_Prod": "SUCCESS", "SALESDB_STG": "SUCCESS", "APPDB_BK": "SUCCESS",
            "APPDB_STG": "SUCCESS", "VRS_Prod": "SUCCESS", "APPDB_Prod": "FAILED",
        },
    })

    assert summary["failed"] == 1
    assert harness.phases() == ["START", "ERROR"]
    end = harness.events[-1]
    assert end["level"] == "error"
    # Naming the database is the point: "a restore failed" does not tell an operator what to fix.
    assert "APPDB_Prod" in (end["error_text"] or "")


def test_an_engine_verdict_of_failed_is_believed_over_a_clean_return(monkeypatch, harness):
    """The engine says FAILED and returns normally. Reporting done here would mean the verdict
    exists only for whoever reads the JSON, which is nobody at 2am."""
    summary = _run(monkeypatch, harness, outcome="done", output={"status": "FAILED"})

    assert summary["failed"] == 1
    assert harness.phases() == ["START", "ERROR"]


def test_a_restore_where_every_database_came_back_still_reports_done(monkeypatch, harness):
    """The guard must not turn healthy runs red - a full per-database map of successes is done."""
    summary = _run(monkeypatch, harness, outcome="done", output={
        "status": "SUCCESS",
        "per_database_restore_status": {"SALESDB_Prod": "SUCCESS", "APPDB_Prod": "SUCCESS_RESUMED"},
    })

    assert summary["succeeded"] == 1
    assert harness.phases() == ["START", "END"]


def test_the_copy_boundaries_are_announced_by_whoever_does_the_copy(monkeypatch):
    """These two events existed in run_script_restore and were left behind.

    When the scheduled restore moved onto restore_by_id in 2.69.52, the transfer came with it but
    the announcements did not, so COPY_START/COPY_DONE became dead code in a module nothing called
    any more. The symptom was a remote drill going silent for 41 minutes between START and END -
    indistinguishable from a hang, which is exactly what the two events exist to prevent.
    """
    from db_ops.backup_restore import restore_by_id as module

    seen = []
    job = SimpleNamespace(
        restore_id="R1", db_type="oracle", label="R1 (oracle)", server_id="SRC",
        target_server_id="DST", target_container="", is_remote=True, env={}, env_secrets={},
        backup_dir="/b", target_backup_dir="/t", target_visible_dir="/t",
    )
    # Patched at the source module: restore_by_id imports it inside the function, so patching the
    # name on restore_by_id would bind nothing.
    monkeypatch.setattr("db_ops.backup_restore.restore_script.load_script_restores",
                        lambda _p=None: [job])
    monkeypatch.setattr(module, "_host_block", lambda job, **_: {"runtime": "linux", "host": "h"})
    monkeypatch.setattr(module, "_PLANNERS", {"oracle": lambda *a, **k: []})
    monkeypatch.setattr("db_ops.backup_restore.backup.resolve_ssh_target",
                        lambda *a, **k: SimpleNamespace(host="h", port=22, username="u",
                                                        container_name="", key_file=None))
    monkeypatch.setattr("db_ops.backup_restore.restore_script.transfer_backup_to_target",
                        lambda *a, **k: {"copied": 3, "skipped": 1, "bytes_copied": 99, "pruned": 0})

    module.restore_by_id({"restore_id": "R1"}, on_phase=lambda p, m, e=None: seen.append(p))

    assert seen == ["COPY_START", "COPY_DONE"]


def test_a_caller_that_wants_no_events_still_restores():
    """``on_phase`` is optional, and an emit that fails must not fail the restore - reporting is
    not the restore.

    Asserted against `events.announce` since 2026-08-16: this rule was written out four times in
    this app — `cli._say`, `restore_by_id._announce`, `restore_script._announce` and
    `server_metadata._announce`, the last already drifted to different parameter names — and this
    test only ever covered one of them.
    """
    from db_ops.backup_restore.events import announce

    assert announce(None, "COPY_START", "x") is None
    announce(lambda *_a: 1 / 0, "COPY_START", "x")   # raises inside, swallowed


def test_every_event_carries_the_restore_id(monkeypatch, harness):
    """Project rule: every backup/restore message must name its id, or an operator watching
    several restores cannot tell which one is talking."""
    _run(monkeypatch, harness, outcome="done")

    for event in harness.events:
        assert event["metadata"]["restore_id"] == "R1"


def test_the_entrys_own_notify_routing_is_used(monkeypatch, harness):
    """Per-entry routing is the whole point of the shared `notify` object; passing None here
    would quietly send every restore to the default group."""
    _run(monkeypatch, harness, outcome="done")

    assert all(e["notify"] == {"alert_on_error": {"enabled": 1}} for e in harness.events)


def test_script_restores_report_too(monkeypatch, harness):
    """Oracle/PostgreSQL/SQL Server script restores run through the same scheduler and were
    equally silent."""
    monkeypatch.setattr(workflow, "load_restore_configs", lambda _p: [])
    job = SimpleNamespace(
        restore_id="CLOUD_ORA_TO_CLOUD2", label="CLOUD_ORA_TO_CLOUD2 (oracle)", active=True,
        db_type="oracle", server_id="SRV", target_container="c", job_code="jc",
        # Mirrors the real ScriptRestore: the event metadata names the target, and a stub without
        # this field would let the scheduler ship a message with no target in it again.
        target_server_id="DST", notify={}, time_window=SimpleNamespace(timeout=7200),
    )
    monkeypatch.setattr(workflow, "load_script_restores", lambda _p: [job])
    # The seam moved in 2.69.52: the scheduled restore drives db_ops.common's primitives through
    # restore_by_id instead of shipping a shell script. What this test is about - that a
    # script-driven entry reports START and END like every other job - is unchanged.
    monkeypatch.setattr("db_ops.backup_restore.restore_by_id.restore_by_id",
                        lambda *a, **k: {"restore_id": "X", "db_type": "oracle", "steps": []})

    workflow.run_scheduled_restores(
        app_config=SimpleNamespace(sqlite_path=":memory:"), config_path="x.json", force=True,
    )

    assert harness.phases() == ["START", "END"]
    assert harness.events[0]["metadata"]["restore_id"] == "CLOUD_ORA_TO_CLOUD2"
    # The two fields this branch used to omit, checked where they are actually produced.
    assert harness.events[0]["metadata"]["target_id"] == "DST"
    assert harness.events[1]["metadata"]["status"] == "done"


# --------------------------------------------------------------------------------------------
# Target-side retention: how long a restore keeps the files it staged on the machine it
# restores onto. Separate from the source's retention - the source decides how far back it can
# recover from, the target only needs enough for its next restore.

def test_the_default_retention_spans_more_than_one_weekly_full():
    """8 days is not arbitrary. The full backup is weekly, so the newest full is at most 7 days
    old; a cutoff shorter than that would delete the full while its incrementals survive, and
    an incremental without its parent restores nothing."""
    from db_ops.backup_restore.config import DEFAULT_TARGET_RETENTION_SECONDS

    assert DEFAULT_TARGET_RETENTION_SECONDS >= 8 * 24 * 3600


def test_retention_is_read_per_entry_so_targets_can_differ():
    from db_ops.backup_restore.config import (
        DEFAULT_TARGET_RETENTION_SECONDS,
        parse_target_retention_seconds,
    )

    assert parse_target_retention_seconds({"target_retention_seconds": 86400}, context="x") == 86400
    assert parse_target_retention_seconds({}, context="x") == DEFAULT_TARGET_RETENTION_SECONDS
    # 0 is a real setting, not "unset": it means never delete.
    assert parse_target_retention_seconds({"target_retention_seconds": 0}, context="x") == 0


def test_a_negative_or_unparsable_retention_is_refused():
    """Silently treating garbage as the default would hide a typo that changes how much of the
    backup set survives on the target."""
    import pytest as _pytest
    from db_ops.backup_restore.config import parse_target_retention_seconds

    for bad in (-1, "soon", []):
        with _pytest.raises(ValueError):
            parse_target_retention_seconds({"target_retention_seconds": bad}, context="entry")


def test_pruning_is_skipped_when_retention_is_zero():
    """0 must not be read as "delete everything older than 0 seconds" - that would wipe the
    staging directory on the first run."""
    from db_ops.backup_restore.transfer import prune_target_dir

    class _Client:
        def exec_command(self, *a, **k):
            raise AssertionError("no command should be sent when retention is disabled")

    assert prune_target_dir(_Client(), "/stage", 0)["pruned"] == 0


def test_pruning_deletes_by_age_and_reports_the_count():
    from db_ops.backup_restore.transfer import prune_target_dir

    sent = {}

    class _Chan:
        def recv_exit_status(self): return 0

    class _Out:
        channel = _Chan()
        def read(self): return b"7\n"

    class _Client:
        def exec_command(self, command, **k):
            sent["command"] = command
            return None, _Out(), None

    result = prune_target_dir(_Client(), "/stage dir", 24 * 3600)

    assert result["pruned"] == 7
    assert "-mmin +1440" in sent["command"]        # seconds -> minutes, not rounded to days
    assert "'/stage dir'" in sent["command"]       # the path is quoted, spaces and all
    assert "-type f" in sent["command"]


# --------------------------------------------------------------------------------------------
# What a run stores of its own output.

def test_a_short_output_is_stored_whole():
    from db_ops.backup_restore.events import stdout_excerpt

    assert stdout_excerpt("PHASE=verify\nRESULT=ok\n") == "PHASE=verify\nRESULT=ok\n"


def test_a_long_output_keeps_the_phase_lines_at_the_start():
    """The regression this fixes: a tail-only excerpt dropped the copy-backup phase line -
    "copied=N skipped=N pruned=N", the chain that was selected - because SQL Server prints
    hundreds of upgrade lines after it. Those first lines are what say what the run did."""
    from db_ops.backup_restore.events import stdout_excerpt

    text = ("PHASE=copy-backup transferred A -> B copied=2919 skipped=3901 pruned=4\n"
            + "Database 'x' running the upgrade step from version 900 to 901.\n" * 200
            + "RESULT=ok restore_id=R1\n")

    excerpt = stdout_excerpt(text)

    assert "copied=2919" in excerpt and "pruned=4" in excerpt   # the head survives
    assert "RESULT=ok" in excerpt                                # and so does the failure end
    assert "characters omitted" in excerpt                       # and it says what it dropped
    assert len(excerpt) < len(text)


def test_the_excerpt_never_silently_stitches_two_unrelated_halves():
    """Without the marker, the head's last line runs straight into the tail's first and reads
    as one continuous log - which is worse than truncation, because it invents a sequence of
    events that never happened."""
    from db_ops.backup_restore.events import stdout_excerpt

    excerpt = stdout_excerpt("A" * 5000)

    assert "\n... [" in excerpt and "] ...\n" in excerpt


# --------------------------------------------------------------------------------------------
# Telegram routing is declared, not inherited silently.


