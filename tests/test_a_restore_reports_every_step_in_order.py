"""A restore reports every step it takes, in order - and each step is a `common.cli` command.

Read in Telegram on 2026-09-25, a PostgreSQL drill said *started*, *copy started*, *copy
finished*, *finished status=done*, and nothing else. The operator asked where the restore was,
where the verify was, then the delete and the metadata, then asked for the steps to be split:
"not one long command - common cli copy, common cli verify, common cli metadata ...".

Behind the silence, the script path (PostgreSQL, Oracle, container SQL Server) did less than
the SQL Server engine path around the same work:

* no event between COPY_DONE and END - an Oracle DUPLICATE is minutes, a large one hours;
* no VERIFY_START / VERIFY_DONE, which the engine path has said since 2026-09-15;
* its staging cleanup ran inside the copy - before the restore, and even for a drill that then
  failed - and said nothing but a count in COPY_DONE's metadata;
* no instance metadata at all: the move onto `restore_by_id` (2.69.52) left the replay behind;
* an END of `status=done`, without what was restored or whether it opened, and without the
  warning a "done with a warning" is about.

The copy and the cleanup were also the two steps that were not `common.cli` commands: the app
opened its own SSH sessions for them. They are `backup-chain`, `copy-backup-dir` and
`prune-staged-backups` now.

The scenario, success first, then each way it stops:

    START  COPY_START COPY_DONE  METADATA(pre)  RESTORE_START RESTORE_DONE
           VERIFY_START VERIFY_DONE  METADATA(post)  DELETE_START DELETE_DONE  END
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from db_ops.backup_restore import events, restore_by_id, restore_script
from db_ops.common import backup_copy
from db_ops.lib import restore_space

SUCCESS = ["COPY_START", "COPY_DONE", "METADATA_SKIP", "RESTORE_START", "RESTORE_DONE",
           "VERIFY_START", "VERIFY_DONE", "DELETE_START", "DELETE_DONE"]


def _job(db_type="postgresql", *, remote=True, metadata=False):
    return SimpleNamespace(
        restore_id="LAB_A_TO_B", db_type=db_type, label=f"LAB_A_TO_B ({db_type})",
        server_id="LAB-192-0-2-49", target_server_id="LAB-192-0-2-50",
        target_container="PG_LAB_B", is_remote=remote, env={}, env_secrets={},
        backup_dir="/b/PG_LAB_A", source_backup_host_dir="/b/PG_LAB_A",
        target_backup_dir="/b/pg_restore_from_a", target_visible_dir="/b/pg_restore_from_a",
        cleanup_retention=259200, copy_mode="auto", space_check=restore_space.SpaceCheck(),
        copy_selection="chain", copy_recent_hours=24,
        server_metadata=SimpleNamespace(enabled=metadata))


PG_PLAN = [
    {"op": "restore-diff", "request": {"db_type": "postgresql", "wal_dir": "/b/wal",
                                       "backup_paths": ["/b/base/20260925T0047Z_FULL",
                                                        "/b/base/20260925T0600Z_INCR"]}},
    {"op": "verify-restore", "request": {"db_type": "postgresql"}},
]


@pytest.fixture
def drill(monkeypatch):
    """Run restore_by_id with every host call faked; return (events, ops run, calls made)."""
    state = {"events": [], "ops": [], "calls": []}

    def run(job, *, plan=PG_PLAN, verify_ok=True, fail_op=None, copy_fails=False,
            point_in_time="", metadata_report=None):
        monkeypatch.setattr(restore_script, "load_script_restores", lambda _p=None: [job])
        monkeypatch.setattr(restore_by_id, "_host_block", lambda j, **_: {"host": "h"})
        monkeypatch.setattr(restore_by_id, "assert_target_is_not_source", lambda *a, **k: None)
        monkeypatch.setitem(restore_by_id._PLANNERS, job.db_type, lambda *a, **k: plan)
        monkeypatch.setattr("db_ops.backup_restore.backup.resolve_ssh_target",
                            lambda *a, **k: SimpleNamespace(host="h", port=22, username="u",
                                                            container_name="c", key_file=None))

        def transfer(*a, **k):
            state["calls"].append(("copy", k.get("prune"), k.get("point_in_time")))
            if copy_fails:
                raise RuntimeError("copy-backup-dir failed: target not writable")
            return {"copied": 3, "skipped": 1, "bytes_copied": 99, "include": ["base/x", "wal/"],
                    "removed_absent_at_source": 0}

        def execute(op, request):
            state["ops"].append(op)
            if op == fail_op:
                raise restore_by_id.RestoreByIdError(f"{op} failed: RMAN-06054")
            if op == "verify-restore":
                return {"ok": verify_ok, "checked": 2, "failed": 0 if verify_ok else 1}
            return {"ok": True}

        def replay(job, *, phase, data_dir=None, on_phase=None):
            state["calls"].append(("metadata", phase))
            events.announce(on_phase, "METADATA_START", f"replaying {phase}")
            events.announce(on_phase, "METADATA_DONE", f"{phase} replayed")
            return "", (metadata_report or {"ok": True})

        monkeypatch.setattr(restore_script, "transfer_backup_to_target", transfer)
        monkeypatch.setattr(restore_script, "prune_staged_backups",
                            lambda *a, **k: state["calls"].append(("prune",))
                            or {"pruned": 4, "retention_seconds": 259200})
        monkeypatch.setattr(restore_script, "replay_metadata_phase", replay)
        monkeypatch.setattr(restore_by_id, "_execute", execute)
        request = {"restore_id": job.restore_id,
                   **({"point_in_time": point_in_time} if point_in_time else {})}
        return restore_by_id.restore_by_id(
            request, on_phase=lambda p, m, e=None: state["events"].append((p, m)))

    run.state = state
    return run


def _phases(drill):
    return [phase for phase, _ in drill.state["events"]]


def _said(drill, phase):
    return next(message for p, message in drill.state["events"] if p == phase)


# --------------------------------------------------------------------------- #
# The scenario
# --------------------------------------------------------------------------- #
def test_a_remote_restore_reports_copy_metadata_restore_verify_and_delete_in_that_order(drill):
    outcome = drill(_job())

    assert _phases(drill) == SUCCESS
    assert drill.state["ops"] == ["restore-diff", "verify-restore"]
    assert outcome["verify"] == {"checked": 2, "failed": 0}


def test_the_delete_runs_after_the_verify_and_not_inside_the_copy(drill):
    drill(_job())

    assert ("copy", False, "") in drill.state["calls"], "the copy must leave the prune to the caller"
    assert drill.state["calls"].index(("prune",)) == len(drill.state["calls"]) - 1


def test_an_in_place_restore_has_no_copy_and_no_delete(drill):
    drill(_job(remote=False))

    assert _phases(drill) == ["METADATA_SKIP", "RESTORE_START", "RESTORE_DONE",
                              "VERIFY_START", "VERIFY_DONE"]


def test_a_failed_copy_stops_at_copy_start(drill):
    with pytest.raises(RuntimeError, match="not writable"):
        drill(_job(), copy_fails=True)

    assert _phases(drill) == ["COPY_START"]
    assert drill.state["ops"] == []


def test_a_failed_restore_step_stops_before_the_verify_and_deletes_nothing(drill):
    with pytest.raises(restore_by_id.RestoreByIdError, match="RMAN-06054"):
        drill(_job(), fail_op="restore-diff")

    assert _phases(drill) == ["COPY_START", "COPY_DONE", "METADATA_SKIP", "RESTORE_START"]
    assert ("prune",) not in drill.state["calls"]


def test_a_failed_verify_is_announced_then_fails_the_run_and_deletes_nothing(drill):
    with pytest.raises(restore_by_id.RestoreByIdError, match="1 of 2 database"):
        drill(_job(), verify_ok=False)

    assert _phases(drill)[-2:] == ["VERIFY_START", "VERIFY_DONE"]
    assert "2 database(s) checked, 1 unusable" in _said(drill, "VERIFY_DONE")
    assert ("prune",) not in drill.state["calls"]


def test_instance_metadata_is_replayed_before_the_databases_and_after_them(drill):
    drill(_job("sqlserver", metadata=True), plan=[
        {"op": "restore-full", "request": {"database_name": "APPDB", "backup_path": "/b/APPDB_FULL.bak"}},
        {"op": "verify-restore", "request": {}}])

    assert _phases(drill) == ["COPY_START", "COPY_DONE", "METADATA_START", "METADATA_DONE",
                              "RESTORE_START", "RESTORE_DONE", "VERIFY_START", "VERIFY_DONE",
                              "METADATA_START", "METADATA_DONE", "DELETE_START", "DELETE_DONE"]
    assert [c for c in drill.state["calls"] if c[0] == "metadata"] == [
        ("metadata", "pre-database"), ("metadata", "post-database")]


def test_no_post_database_metadata_after_a_restore_that_failed(drill):
    with pytest.raises(restore_by_id.RestoreByIdError):
        drill(_job("sqlserver", metadata=True), fail_op="restore-full", plan=[
            {"op": "restore-full", "request": {"database_name": "APPDB"}}])

    assert [c for c in drill.state["calls"] if c[0] == "metadata"] == [("metadata", "pre-database")]


@pytest.mark.parametrize("engine, reason", [
    ("postgresql", "inside the physical backup"), ("oracle", "inside the physical backup"),
    ("sqlserver", "server_metadata is off for this entry")])
def test_an_entry_without_metadata_says_why_once(drill, engine, reason):
    drill(_job(engine))

    assert _phases(drill).count("METADATA_SKIP") == 1
    assert reason in _said(drill, "METADATA_SKIP")


# --------------------------------------------------------------------------- #
# What the messages say
# --------------------------------------------------------------------------- #
def test_the_restore_start_names_what_goes_in_and_to_which_point(drill):
    drill(_job())
    assert ("base backup 20260925T0047Z_FULL + 1 incremental(s), then WAL replay, "
            "to the newest backup") in _said(drill, "RESTORE_START")

    drill.state["events"].clear()
    drill(_job(), point_in_time="2026-09-25 08:23:46 +08:00")
    assert "to 2026-09-25 08:23:46 +08:00" in _said(drill, "RESTORE_START")


def test_a_sql_server_plan_is_named_per_database_and_oracle_by_its_duplicate():
    assert restore_by_id._plan_summary([
        {"op": "restore-full", "request": {"database_name": "APPDB", "backup_path": "/b/APPDB_FULL.bak"}},
        {"op": "restore-log", "request": {"database_name": "APPDB", "backup_paths": ["/b/1.trn", "/b/2.trn"]}},
    ]) == "APPDB: full APPDB_FULL.bak + 2 log(s)"
    assert "RMAN DUPLICATE of FREE from /stage/ora" in restore_by_id._plan_summary([
        {"op": "restore-full", "request": {"mode": "duplicate", "oracle_sid": "FREE",
                                           "backup_location": "/stage/ora"}}])


def test_a_step_without_a_named_path_is_described_not_indexed():
    """A summary that raised would fail the restore it describes."""
    assert restore_by_id._plan_summary([{"op": "restore-full", "request": {}}]) == "the planned backup"


def test_the_telegram_end_says_what_was_restored_whether_it_opened_and_the_warning():
    text = events._format_restore_workflow_telegram_message(level="warning", message="x", metadata={
        "command": "restore-workflow", "phase": "END", "status": "done", "restore_id": "R",
        "restored": "base backup F + 1 incremental(s), then WAL replay",
        "verified": "2 database(s) checked, 0 unusable",
        "warnings": ["1 file(s) named like backups could not be read"]})

    assert "restored=base backup F + 1 incremental(s), then WAL replay" in text
    assert "verified=2 database(s) checked, 0 unusable" in text
    assert "warning=1 file(s) named like backups could not be read" in text


def test_a_step_event_leads_with_its_own_message():
    text = events._format_restore_workflow_telegram_message(level="logging", message=(
        "Restore R: restoring base backup F, to the newest backup."), metadata={
        "command": "restore-workflow", "phase": "RESTORE_START", "restore_id": "R"})

    assert text.splitlines()[0].endswith("Restore R: restoring base backup F, to the newest backup.")


# --------------------------------------------------------------------------- #
# Each step is its own common.cli command
# --------------------------------------------------------------------------- #
def test_the_copy_is_two_common_commands_and_the_delete_a_third(monkeypatch):
    called = []

    def run(command, request, **kwargs):
        called.append((command, request))
        return {"backup-chain": {"include": ["base/F", "wal/"], "narrowed": True},
                "copy-backup-dir": {"copied": 1, "skipped": 0, "bytes_copied": 5,
                                    "removed_absent_at_source": 0, "opened_for_engine": True},
                "prune-staged-backups": {"pruned": 2, "retention_seconds": 60}}[command]

    monkeypatch.setattr(restore_script.common_cli, "run", run)
    monkeypatch.setattr(restore_script, "_ssh_login",
                        lambda target, **k: {"host": target.host, "username": "u", "password": "p"})
    job = _job()
    source = SimpleNamespace(host="192.0.2.49", container_name="PG_LAB_A")
    target = SimpleNamespace(host="192.0.2.50", container_name="")

    out = restore_script.transfer_backup_to_target(job, source=source, target=target, prune=False,
                                                   point_in_time="2026-09-25 08:23:46 +08:00")
    restore_script.prune_staged_backups(job, target=target)

    assert [c for c, _ in called] == ["backup-chain", "copy-backup-dir", "prune-staged-backups"]
    assert called[0][1]["point_in_time"] == "2026-09-25 08:23:46 +08:00"
    assert called[1][1]["include"] == ["base/F", "wal/"]
    assert called[1][1]["source"]["host"] == "192.0.2.49" and called[1][1]["target"]["host"] == "192.0.2.50"
    assert out["include"] == ["base/F", "wal/"]


def test_a_point_in_time_copies_the_whole_directory_without_asking_the_source():
    """The narrowings take the NEWEST chain; a moment before the newest full needs an older one."""
    class _NoTouch:
        def open_stream(self, *_a, **_k):
            raise AssertionError("the source was asked about a chain it cannot choose")

        def run(self, *_a, **_k):
            raise AssertionError("the source was asked about a chain it cannot choose")

    for engine in ("postgresql", "oracle"):
        assert backup_copy.chain_include(engine, _NoTouch(), source_dir="/b", container="c",
                                         point_in_time="2026-09-25 08:23:46 +08:00") == ()


def test_the_copy_commands_take_their_request_on_stdin_only(capsys):
    from db_ops.common import cli_backup_copy

    for command in cli_backup_copy.COMMANDS:
        code = cli_backup_copy.run(command, ['{"source_dir": "/b"}'], read_request=None)
        assert code != 0
        assert "stdin" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# A backup: START, then END or ERROR - and a refusal names the id it was given
# --------------------------------------------------------------------------- #
class _Store:
    """The four calls run_backup makes on the store."""

    @classmethod
    def from_config(cls, config, **kwargs):
        return cls()

    def fetch_latest_job_runs_by_job_code(self):
        return {}

    def fetch_running_job_runs(self, _prefix=""):
        return []

    def insert_job_run(self, _run):
        return 1

    def update_job_run(self, **kwargs):
        pass


@pytest.mark.parametrize("status, expected", [("done", ["START", "END"]), ("error", ["START", "ERROR"])])
def test_a_backup_reports_start_then_its_end_or_its_error_with_the_reason(monkeypatch, status, expected):
    from db_ops.backup_restore import backup as backup_module
    from db_ops.backup_restore.server_metadata import ServerMetadataPlan
    from db_ops.lib.time_window import TimeWindow

    emitted = []
    job = backup_module.BackupJob(
        backup_id="PG_LAB_A_DB", job="database_full", db_type="postgresql",
        server_id="LAB-192-0-2-49", script="assets/backup/postgresql/pg_basebackup_database.sh",
        backup_dir="/b/PG_LAB_A", cleanup_retention=259200, time_window=TimeWindow(),
        server_metadata=ServerMetadataPlan())
    monkeypatch.setattr(backup_module, "DbOpsStore", _Store)
    monkeypatch.setattr(backup_module, "emit_backup_restore_event", lambda **k: emitted.append(k))
    monkeypatch.setattr(backup_module.schedule, "reap_stale_runs", lambda **k: [])
    monkeypatch.setattr(backup_module, "resolve_backup_target", lambda *a, **k: None)
    monkeypatch.setattr(backup_module, "load_backup_jobs", lambda _p: [job])
    monkeypatch.setattr(backup_module, "execute_backup_job", lambda item, **k: backup_module.BackupRunResult(
        job=item, status=status, exit_code=0 if status == "done" else 1, duration_ms=1,
        stdout="RESULT=ok" if status == "done" else "", stderr="",
        error_text=None if status == "done" else "container 'PG_LAB_A' is not running"))

    backup_module.run_backup(app_config=SimpleNamespace(sqlite_path=":memory:"), config_path="x", force=True)

    assert [e["phase"] for e in emitted] == expected
    assert all(e["metadata"]["backup_id"] == "PG_LAB_A_DB" for e in emitted)
    if status == "error":
        assert "is not running" in emitted[-1]["message"], "the reason, not only the exit code"


@pytest.fixture
def empty_root(tmp_path, monkeypatch):
    from db_ops.backup_restore import backup as backup_module
    from db_ops.backup_restore import config as config_module

    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.chdir(tmp_path)
    for module in (config_module, backup_module, restore_script):
        monkeypatch.setattr(module, "DEFAULT_RESTORE_CONFIG_PATH", data / "restore_config.json",
                            raising=False)
    return tmp_path


@pytest.mark.parametrize("argv, key, value", [
    (["backup", "--config", "config.json", "--backup-id", "NO_SUCH_BACKUP"], "backup_id", "NO_SUCH_BACKUP"),
    (["restore-workflow", "--config", "config.json", "--restore-id", "NO_SUCH_RESTORE"],
     "restore_id", "NO_SUCH_RESTORE")])
def test_a_refused_id_is_named_in_the_refusal(empty_root, monkeypatch, argv, key, value):
    """The bot's refusal cases read `backup_id=<unknown>` - the one message that most needs to
    name the id it was given (2026-09-25)."""
    from db_ops.backup_restore import cli
    from db_ops.lib.config import DbOpsConfig

    emitted = []
    monkeypatch.setattr(cli, "load_config", lambda _path: DbOpsConfig(
        log_dir=empty_root / "logs", runtime_dir=empty_root / "runtime",
        sqlite_path=empty_root / "runtime" / "db_ops.sqlite"))
    monkeypatch.setattr(cli, "patch_stdout", lambda *a, **k: None)
    monkeypatch.setattr(cli, "setup_app_logger", lambda *a, **k: None)
    monkeypatch.setattr(cli, "emit_backup_restore_event", lambda **k: emitted.append(k))

    assert cli.main(argv) != 0

    error = [e for e in emitted if e["phase"] == "ERROR"][-1]
    assert events.resolve_run_id(error["command"], error["metadata"]) == (key, value)


def test_what_a_copy_command_says_while_working_never_reaches_its_answer(monkeypatch, capsys):
    """Opening a session prints "Connecting to ..." on stdout; the first lab run's answer arrived
    behind it and the app could not read it ("backup-chain exited 0 without a JSON response")."""
    import json

    from db_ops.common import cli_backup_copy

    class _Client:
        def close(self):
            pass

    def chatty_open(login, *, role):
        print(f"Connecting to u@{login['host']}:22 ...")
        return _Client()

    monkeypatch.setattr(cli_backup_copy, "_open", chatty_open)
    monkeypatch.setattr(backup_copy, "chain_include", lambda *a, **k: ("wal/",))
    request = {"db_type": "postgresql", "source": {"host": "192.0.2.49"}, "source_dir": "/b"}

    code = cli_backup_copy.run("backup-chain", ["-"], read_request=lambda _s, _u: (request, 0))

    out = capsys.readouterr()
    assert code == 0
    assert json.loads(out.out)["data"] == {"include": ["wal/"], "narrowed": True}
    assert "Connecting to" in out.err



def test_metadata_asked_for_and_not_replayed_is_a_warning_on_the_restore(drill):
    """The 100.250 drill restored its three databases, replayed no login and ended SUCCESS with no
    word of it in its answer - the skip was an event only, and 23 users were orphaned (1.88)."""
    skipped = {"ok": False, "status": "SKIPPED",
               "error": "no bundle at runtime/instance_bundles/SRC: it is written by the node that "
                        "runs the backup entry of SRC with server_metadata on"}

    answer = drill(_job("sqlserver", metadata=True), metadata_report=skipped, plan=[
        {"op": "restore-full", "request": {"database_name": "APPDB", "backup_path": "/b/APPDB_FULL.bak"}},
        {"op": "verify-restore", "request": {}}])

    assert len(answer["warnings"]) == 2, "one per phase that was asked for"
    assert all("instance metadata" in w and "not replayed" in w for w in answer["warnings"])
    assert "written by the node that runs the backup entry" in answer["warnings"][0]
    assert answer["verify"] == {"checked": 2, "failed": 0}, "still a restore that worked"


def test_metadata_replayed_adds_no_warning(drill):
    answer = drill(_job("sqlserver", metadata=True), plan=[
        {"op": "restore-full", "request": {"database_name": "APPDB", "backup_path": "/b/APPDB_FULL.bak"}},
        {"op": "verify-restore", "request": {}}])

    assert answer["warnings"] == []


def test_the_skip_names_the_node_that_writes_the_bundle_and_the_command_that_exports_it(tmp_path, monkeypatch):
    """It said "the backup entry needs server_metadata.enabled too" - on the soak node the entry had
    it on and was inactive, the worker running it; the advice pointed at a setting already set."""
    from db_ops.backup_restore import server_metadata

    monkeypatch.setattr(server_metadata, "instance_bundle_dir", lambda server_id: tmp_path / server_id)
    plan = SimpleNamespace(enabled=True, phases=("pre-database", "post-database"))

    line, report = server_metadata.replay_phase(
        plan, phase="pre-database", label="R (sqlserver)", source_server_id="SRC",
        target_server_id="DST", target_container="", target_host="", data_dir=None,
        announce=None)

    assert report["status"] == "SKIPPED"
    assert "server_metadata.enabled too" not in line
    assert "export-instance-bundle" in report["error"] and "inactive" in report["error"]
