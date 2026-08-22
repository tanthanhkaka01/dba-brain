"""Restore entries are scheduled exactly like backup jobs, and workflow runs backup first."""
from datetime import datetime, timedelta, timezone

import pytest

from db_ops.backup_restore import schedule, workflow
from db_ops.backup_restore.config import BackupRestoreConfig
from db_ops.lib.time_window import TimeWindow


class _Row(dict):
    """Stands in for the sqlite3.Row returned by fetch_latest_job_runs_by_job_code."""


def _run_row(*, status, minutes_ago):
    started = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return _Row(status=status, started_at=started.strftime("%Y-%m-%dT%H:%M:%SZ"))


def _restore(restore_id="R1", *, active=True, window=None):
    from pathlib import Path

    return BackupRestoreConfig(
        restore_id=restore_id,
        active=active,
        time_window=window or TimeWindow(repeat_interval=72000, retry_interval=600, timeout=7200),
        prod_backup_share=Path("/src"), vm_import_unc=Path("/imp"), vm_import_local=Path("/imp"),
        vm_log_unc=Path("/log"), vm_log_local=Path("/log"),
        prod_smb_credential_target="", prod_smb_username="", prod_smb_password_env="",
        vm_credential_target="", vm_username="", vm_password_env="",
        restore_sql_instance_on_vm="localhost",
    )


def test_a_restore_is_due_when_it_has_never_run():
    assert select_ids(workflow.select_due_restores(configs=[_restore()], latest_runs={})) == ["R1"]


def select_ids(configs):
    return [c.restore_id for c in configs]


def test_a_restore_is_not_due_before_its_interval_elapses():
    config = _restore()
    latest = {schedule.restore_job_code("R1"): _run_row(status="DONE", minutes_ago=60)}

    assert workflow.select_due_restores(configs=[config], latest_runs=latest) == []


def test_a_restore_is_due_again_after_its_interval():
    config = _restore()
    latest = {schedule.restore_job_code("R1"): _run_row(status="DONE", minutes_ago=21 * 60)}

    assert select_ids(workflow.select_due_restores(configs=[config], latest_runs=latest)) == ["R1"]


def test_a_running_restore_is_not_started_a_second_time():
    # Two restores into the same target database is the failure this prevents.
    config = _restore()
    latest = {schedule.restore_job_code("R1"): _run_row(status="RUNNING", minutes_ago=5)}

    assert workflow.select_due_restores(configs=[config], latest_runs=latest) == []


def test_a_stale_running_restore_is_recovered_after_its_timeout():
    config = _restore()
    latest = {schedule.restore_job_code("R1"): _run_row(status="RUNNING", minutes_ago=200)}

    assert select_ids(workflow.select_due_restores(configs=[config], latest_runs=latest)) == ["R1"]


def test_a_failed_restore_backs_off_on_retry_interval():
    config = _restore()
    too_soon = {schedule.restore_job_code("R1"): _run_row(status="ERROR", minutes_ago=2)}
    elapsed = {schedule.restore_job_code("R1"): _run_row(status="ERROR", minutes_ago=30)}

    assert workflow.select_due_restores(configs=[config], latest_runs=too_soon) == []
    assert select_ids(workflow.select_due_restores(configs=[config], latest_runs=elapsed)) == ["R1"]


def test_an_inactive_restore_is_never_due():
    assert workflow.select_due_restores(configs=[_restore(active=False)], latest_runs={}) == []


def test_the_time_window_gates_the_restore():
    config = _restore(window=TimeWindow(from_hour=3, to_hour=17, repeat_interval=72000))
    night = datetime(2026, 7, 25, 1, 0).astimezone()
    daytime = datetime(2026, 7, 25, 10, 0).astimezone()

    assert workflow.select_due_restores(configs=[config], latest_runs={}, local_now=night) == []
    assert select_ids(workflow.select_due_restores(configs=[config], latest_runs={}, local_now=daytime)) == ["R1"]


def test_backup_and_restore_share_one_due_rule():
    """The point of schedule.is_due: both halves answer 'should this run' identically."""
    window = TimeWindow(repeat_interval=900, timeout=1800)
    latest = {"x": _run_row(status="DONE", minutes_ago=5)}

    assert schedule.is_due(job_code="x", time_window=window, latest_runs=latest) is False
    assert schedule.is_due(job_code="unseen", time_window=window, latest_runs=latest) is True


def test_job_code_namespaces_do_not_collide():
    assert schedule.backup_job_code("A", "database") != schedule.restore_job_code("A")
    assert schedule.restore_job_code("A").startswith(schedule.RESTORE_JOB_PREFIX)
    assert schedule.backup_job_code("A", "database").startswith(schedule.BACKUP_JOB_PREFIX)


def test_workflow_runs_backup_before_restore(monkeypatch):
    """A restore drill validates the backups, so the set it validates must be the fresh one."""
    order = []
    monkeypatch.setattr(workflow, "run_backup", lambda **_: order.append("backup") or {"failed": 0})
    monkeypatch.setattr(workflow, "run_scheduled_restores", lambda **_: order.append("restore") or {"failed": 0})

    result = workflow.run_workflow(app_config=object(), config_path="c.json")

    assert order == ["backup", "restore"]
    assert result["failed"] == 0


def test_workflow_reports_failure_from_either_half(monkeypatch):
    monkeypatch.setattr(workflow, "run_backup", lambda **_: {"failed": 0})
    monkeypatch.setattr(workflow, "run_scheduled_restores", lambda **_: {"failed": 2})

    assert workflow.run_workflow(app_config=object(), config_path="c.json")["failed"] == 2


@pytest.mark.parametrize("skip,expected", [
    ({"skip_backup": True}, ["restore"]),
    ({"skip_restore": True}, ["backup"]),
])
def test_workflow_halves_can_be_skipped(monkeypatch, skip, expected):
    order = []
    monkeypatch.setattr(workflow, "run_backup", lambda **_: order.append("backup") or {"failed": 0})
    monkeypatch.setattr(workflow, "run_scheduled_restores", lambda **_: order.append("restore") or {"failed": 0})

    workflow.run_workflow(app_config=object(), config_path="c.json", **skip)

    assert order == expected


# ---------------------------------------------------------------------------
# Script-driven restores (Oracle / PostgreSQL)
# ---------------------------------------------------------------------------

def _script_config(tmp_path, **over):
    import json
    entry = {
        "restore_id": "ORA_DRILL", "active": True, "db_type": "oracle",
        "server_id": "SRC", "target_container": "target_c",
        "backup_dir": "/backup/dbops", "script": "assets/restore/oracle/oracle_rman_restore.sh",
        "time_window": {"repeat_interval": 72000, "timeout": 7200},
    }
    entry.update(over)
    path = tmp_path / "restore_config.json"
    path.write_text(json.dumps({"backup_restore": {"restores": [entry]}}), encoding="utf-8")
    return path


def test_script_restores_are_loaded_from_the_same_restores_list(tmp_path):
    from db_ops.backup_restore.restore_script import load_script_restores

    jobs = load_script_restores(_script_config(tmp_path))

    assert [j.restore_id for j in jobs] == ["ORA_DRILL"]
    assert jobs[0].db_type == "oracle"
    # It shares the restore job-code namespace, so one due check covers both kinds.
    assert jobs[0].job_code == schedule.restore_job_code("ORA_DRILL")


def test_the_sql_server_parser_skips_script_restores(tmp_path):
    """They live in one list but only the SQL Server engine's entries reach its parser, which
    requires SMB/sqlcmd fields a script entry does not have."""
    from db_ops.backup_restore.config import load_restore_configs

    assert load_restore_configs(_script_config(tmp_path)) == []


def test_a_script_restore_refuses_to_target_its_own_source(monkeypatch, tmp_path):
    """A drill that restores over the database it is validating destroys the thing it proves."""
    from db_ops.backup_restore import restore_script
    from db_ops.backup_restore.backup import BackupTarget

    job = restore_script.load_script_restores(_script_config(tmp_path, target_container="same_c"))[0]
    monkeypatch.setattr(restore_script, "resolve_ssh_target", lambda *a, **k: BackupTarget(
        server_id="SRC", host="h", port=22, username="u",
        container_name="same_c", key_file=None, password_ref=None,
    ))

    with pytest.raises(ValueError, match="must not overwrite"):
        restore_script.run_script_restore(job)


def test_a_script_restore_without_the_ok_receipt_is_an_error(monkeypatch, tmp_path):
    """Exit 0 alone is not proof: the script prints RESULT=ok only after verifying the restore."""
    from db_ops.backup_restore import restore_script
    from db_ops.backup_restore.backup import BackupTarget

    job = restore_script.load_script_restores(_script_config(tmp_path))[0]
    monkeypatch.setattr(restore_script, "resolve_ssh_target", lambda *a, **k: BackupTarget(
        server_id="SRC", host="h", port=22, username="u",
        container_name="source_c", key_file=None, password_ref=None,
    ))
    monkeypatch.setattr(restore_script, "_script_path", lambda *a, **k: __import__("pathlib").Path(__file__))
    monkeypatch.setattr(restore_script, "execute_over_ssh", lambda **_: ("phases ran", "", 0))

    status, exit_code, _out, _err = restore_script.run_script_restore(job)

    assert status == "error" and exit_code == 0


def test_a_script_restore_with_the_receipt_is_done(monkeypatch, tmp_path):
    from db_ops.backup_restore import restore_script
    from db_ops.backup_restore.backup import BackupTarget

    job = restore_script.load_script_restores(_script_config(tmp_path))[0]
    monkeypatch.setattr(restore_script, "resolve_ssh_target", lambda *a, **k: BackupTarget(
        server_id="SRC", host="h", port=22, username="u",
        container_name="source_c", key_file=None, password_ref=None,
    ))
    monkeypatch.setattr(restore_script, "_script_path", lambda *a, **k: __import__("pathlib").Path(__file__))
    monkeypatch.setattr(restore_script, "execute_over_ssh", lambda **_: ("RESULT=ok restore_id=ORA_DRILL", "", 0))

    status, _code, _out, _err = restore_script.run_script_restore(job)

    assert status == "done"


def test_restore_id_filter_finds_a_script_only_entry(tmp_path, monkeypatch):
    """Regression: --restore-id was judged against the SQL Server list alone, so a valid
    script-driven restore_id was rejected as 'No backup_restore entry found'."""
    from db_ops.backup_restore import workflow as wf

    class _Store:

        @classmethod
        def from_config(cls, config, **kwargs):
            """Store doubles must offer the same constructor contract as the real classes."""
            return cls(getattr(config, 'sqlite_path', None))
        def __init__(self, *_a, **_k): pass
        def fetch_latest_job_runs_by_job_code(self): return {}
        def fetch_running_job_runs(self, _prefix=""): return []

    monkeypatch.setattr(wf, "DbOpsStore", _Store)
    monkeypatch.setattr(wf, "load_restore_configs", lambda _p: [])

    class _Cfg:
        sqlite_path = ":memory:"

    summary = wf.run_scheduled_restores(
        app_config=_Cfg(), config_path=str(_script_config(tmp_path)),
        restore_id="ORA_DRILL", dry_run=True, force=True,
    )

    assert summary["configured"] == 1
    assert [r["restore_id"] for r in summary["restores"]] == ["ORA_DRILL"]


# ---------------------------------------------------------------------------
# Restoring onto another machine
# ---------------------------------------------------------------------------

def test_a_remote_target_requires_somewhere_to_put_the_backup(tmp_path):
    """It cannot be shared through a mount any more, so the transfer needs a destination."""
    from db_ops.backup_restore.restore_script import load_script_restores

    cfg = _script_config(tmp_path, target_container="", target_server_id="VM1")
    with pytest.raises(ValueError, match="target_backup_dir"):
        load_script_restores(cfg)


def test_an_entry_must_name_a_target_at_all(tmp_path):
    from db_ops.backup_restore.restore_script import load_script_restores

    cfg = _script_config(tmp_path, target_container="", target_server_id="")
    with pytest.raises(ValueError, match="target_container.*or target_server_id"):
        load_script_restores(cfg)


def test_remote_and_native_modes_are_derived_from_the_entry(tmp_path):
    from db_ops.backup_restore.restore_script import load_script_restores

    remote = load_script_restores(_script_config(
        tmp_path, target_container="", target_server_id="VM1", target_backup_dir="/backup",
        source_backup_host_dir="/host/backup",
    ))[0]
    assert remote.is_remote and remote.target_mode == "native"

    in_place = load_script_restores(_script_config(tmp_path))[0]
    assert not in_place.is_remote and in_place.target_mode == "docker"

    # A container ON another machine is remote but still driven through docker.
    remote_docker = load_script_restores(_script_config(
        tmp_path, target_server_id="VM1", target_backup_dir="/backup",
        source_backup_host_dir="/host/backup",
    ))[0]
    assert remote_docker.is_remote and remote_docker.target_mode == "docker"


def test_a_remote_restore_transfers_then_runs_on_the_target_host(monkeypatch, tmp_path):
    """The script must run where the database will live, not where the backup came from."""
    from db_ops.backup_restore import restore_script as rs
    from db_ops.backup_restore.backup import BackupTarget

    job = load_remote_job(tmp_path)
    hosts = {"SRC": "src.host", "VM1": "vm1.host"}
    monkeypatch.setattr(rs, "resolve_ssh_target", lambda sid, **k: BackupTarget(
        server_id=sid, host=hosts[sid], port=22, username="u",
        container_name="source_c", key_file=None, password_ref=None,
    ))
    transferred = {}

    def fake_transfer(_job, **_kwargs):
        transferred["done"] = True
        return {"copied": 3, "skipped": 1, "bytes_copied": 99}

    monkeypatch.setattr(rs, "transfer_backup_to_target", fake_transfer)
    monkeypatch.setattr(rs, "_script_path", lambda *a, **k: __import__("pathlib").Path(__file__))
    ran_on = {}

    def fake_exec(*, script_text, env, target, **_):
        ran_on["host"] = target.host
        ran_on["env"] = env
        return ("RESULT=ok", "", 0)

    monkeypatch.setattr(rs, "execute_over_ssh", fake_exec)

    status, _code, out, _err = rs.run_script_restore(job)

    assert status == "done"
    assert transferred.get("done"), "the backup must be transferred before the restore runs"
    assert ran_on["host"] == "vm1.host", "the script must run on the target host"
    # The script reads the transferred copy, not the source path.
    assert ran_on["env"]["BACKUP_DIR"] == "/target/backup"
    assert ran_on["env"]["TARGET_MODE"] == "native"
    assert "PHASE=copy-backup transferred" in out


def load_remote_job(tmp_path):
    from db_ops.backup_restore.restore_script import load_script_restores

    return load_script_restores(_script_config(
        tmp_path, target_container="", target_server_id="VM1", target_backup_dir="/target/backup",
        source_backup_host_dir="/host/backup",
    ))[0]


# --------------------------------------------------------------------------- #
# A long restore must not be silent between its start and its end
# --------------------------------------------------------------------------- #
def _remote_restore_with_phases(monkeypatch, tmp_path, *, transfer=None, fail_announce=False):
    """Run a remote restore, collecting the phase boundaries it announces."""
    from db_ops.backup_restore import restore_script as rs
    from db_ops.backup_restore.backup import BackupTarget

    job = load_remote_job(tmp_path)
    hosts = {"SRC": "src.host", "VM1": "vm1.host"}
    monkeypatch.setattr(rs, "resolve_ssh_target", lambda sid, **k: BackupTarget(
        server_id=sid, host=hosts[sid], port=22, username="u",
        container_name="source_c", key_file=None, password_ref=None,
    ))
    monkeypatch.setattr(rs, "transfer_backup_to_target",
                        lambda _job, **_k: transfer or {"copied": 3, "skipped": 1, "bytes_copied": 99})
    monkeypatch.setattr(rs, "_script_path", lambda *a, **k: __import__("pathlib").Path(__file__))
    monkeypatch.setattr(rs, "execute_over_ssh",
                        lambda **_k: ("RESULT=ok", "", 0))

    seen = []

    def on_phase(phase, message, extra=None):
        seen.append((phase, message, extra or {}))
        if fail_announce:
            raise RuntimeError("telegram queue is down")

    status, _code, _out, _err = rs.run_script_restore(job, on_phase=on_phase)
    return status, seen


def test_the_copy_announces_when_it_starts_and_when_it_finishes(monkeypatch, tmp_path):
    """The copy is the long half - 17 GB over two internet hops took hours on the CLOUD pair.
    Without these two, START and END are the only events and "still copying" is indistinguishable
    from "hung" until the timeout reaper speaks, two hours later."""
    status, seen = _remote_restore_with_phases(monkeypatch, tmp_path)

    assert status == "done"
    assert [phase for phase, _m, _e in seen] == ["COPY_START", "COPY_DONE"]


def test_the_copy_done_message_carries_what_actually_moved(monkeypatch, tmp_path):
    """'Copy done' that does not say how much moved cannot distinguish a real copy from one
    that skipped everything - which is what a wrongly narrowed chain looks like."""
    _status, seen = _remote_restore_with_phases(
        monkeypatch, tmp_path, transfer={"copied": 61, "skipped": 192, "bytes_copied": 5368709120})

    _phase, message, extra = seen[1]
    assert "61 piece(s)" in message and "192 already present" in message
    assert extra["copied"] == 61 and extra["bytes_copied"] == 5368709120


def test_an_in_place_restore_announces_no_copy_because_none_happens(monkeypatch, tmp_path):
    """In place, both ends share the directory through a mount. Reporting a copy there would
    describe work that never ran."""
    from db_ops.backup_restore import restore_script as rs
    from db_ops.backup_restore.backup import BackupTarget
    from db_ops.backup_restore.restore_script import load_script_restores

    job = load_script_restores(_script_config(tmp_path, target_container="target_c"))[0]
    monkeypatch.setattr(rs, "resolve_ssh_target", lambda sid, **k: BackupTarget(
        server_id=sid, host="src.host", port=22, username="u",
        container_name="source_c", key_file=None, password_ref=None,
    ))
    monkeypatch.setattr(rs, "_script_path", lambda *a, **k: __import__("pathlib").Path(__file__))
    monkeypatch.setattr(rs, "execute_over_ssh", lambda **_k: ("RESULT=ok", "", 0))
    seen = []

    rs.run_script_restore(job, on_phase=lambda p, m, e=None: seen.append(p))

    assert seen == []


def test_a_failing_announcement_does_not_fail_the_restore(monkeypatch, tmp_path):
    """A Telegram queue that is down is a bad reason to fail a restore that worked."""
    status, seen = _remote_restore_with_phases(monkeypatch, tmp_path, fail_announce=True)

    assert status == "done"
    assert len(seen) == 2, "both boundaries are still attempted after the first one raises"


def test_a_remote_restore_requires_the_source_host_path(tmp_path):
    """backup_dir is a container path; SFTP reads the host, so the two cannot be the same field."""
    from db_ops.backup_restore.restore_script import load_script_restores

    cfg = _script_config(tmp_path, target_server_id="VM1", target_backup_dir="/backup")
    with pytest.raises(ValueError, match="source_backup_host_dir"):
        load_script_restores(cfg)


def test_a_remote_restore_hides_the_source_container_from_the_script(tmp_path, monkeypatch):
    """The source container does not exist on the target host, so guarding on it would fail a
    perfectly good restore."""
    from db_ops.backup_restore import restore_script as rs
    from db_ops.backup_restore.backup import BackupTarget

    job = load_remote_job(tmp_path)
    source = BackupTarget(server_id="SRC", host="src", port=22, username="u",
                          container_name="source_c", key_file=None, password_ref=None)

    assert rs._script_env(job, source)["SOURCE_CONTAINER"] == ""


def test_the_transfer_recreates_empty_directories(monkeypatch):
    """A PostgreSQL backup needs its empty dirs (pg_tblspc, pg_replslot). Copying only files
    yields a directory that looks complete and that pg_combinebackup refuses."""
    import stat as stat_mod
    from db_ops.backup_restore import transfer

    class _Attr:
        def __init__(self, name, is_dir, size=0):
            self.filename = name
            self.st_mode = (stat_mod.S_IFDIR if is_dir else stat_mod.S_IFREG) | 0o700
            self.st_size = size

    made = []

    class _Probe:
        def write(self, _data): pass
        def __enter__(self): return self
        def __exit__(self, *_a): return False

    class _Sftp:
        def __init__(self, listing): self.listing = listing
        def listdir_attr(self, path): return self.listing.get(path, [])
        def stat(self, path):
            if path not in made and path not in self.listing:
                raise IOError("missing")
        def mkdir(self, path): made.append(path)
        def open(self, path, *a, **k):
            # The writability probe is expected; a real file copy in this fixture is not.
            if path.endswith(".db_ops_write_probe"):
                return _Probe()
            raise AssertionError("no files to copy in this fixture")
        def remove(self, path): pass
        def putfo(self, *a, **k): raise AssertionError("no files to copy in this fixture")
        def close(self): pass

    source = _Sftp({"/src": [_Attr("pg_tblspc", True)], "/src/pg_tblspc": []})
    target = _Sftp({})
    monkeypatch.setattr(source, "open", lambda *a, **k: None, raising=False)

    class _Client:
        def __init__(self, sftp): self._sftp = sftp
        def open_sftp(self): return self._sftp

    result = transfer.sync_backup_dir(
        source_client=_Client(source), source_dir="/src",
        target_client=_Client(target), target_dir="/dst",
    )

    assert result.copied == 0
    assert any(p.endswith("/dst/pg_tblspc") for p in made), f"empty dir not recreated: {made}"


def test_a_failure_before_the_entries_resolve_reports_the_real_error(tmp_path, capsys):
    """The notify object is read on the failure path, so leaving it unbound until the entries
    resolve turned every early failure — an unknown restore_id, a bad config — into an
    UnboundLocalError that hid the actual cause."""
    from db_ops.backup_restore import cli

    rc = cli.main(["restore-workflow", "NO_SUCH_RESTORE_ID", "--config", str(tmp_path / "missing.json")])

    assert rc == 1
    err = capsys.readouterr().err
    assert "UnboundLocalError" not in err
