"""Switching a backup or restore entry off, and back on, without restating it.

On 2026-10-22 the operator switched off the two lab VMs .251 and .252. Seven backup entries ran
against the first and four restores into the second - one of them the nightly 100.250 drill - and
each would have failed every cycle against a machine that was not there. The only command that
could stop one was `backup-add` / `restore-add` with `replace`, which restates the whole entry:
its jobs, windows, notify routes and secrets. `entry-active` writes the one flag, so turning an
entry back on is the same call with `true`, and nothing else in it can drift on the way.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from dbabrain.backup_restore import registration

REPO = Path(__file__).resolve().parents[1]

WINDOW = {"from_hour": 1, "to_hour": 4, "repeat_interval": 72000,
          "retry_interval": 1800, "timeout": 7200}
NOTIFY = {"logging_on_run": {"enabled": True, "telegram_chat": "backup"},
          "alert_on_error": {"enabled": True, "telegram_chat": "backup"}}


def _job(**over) -> dict:
    job = {"job": "database", "script": "assets/backup/x.sh", "cleanup_retention": 691200,
           "time_window": dict(WINDOW), "notify": dict(NOTIFY)}
    job.update(over)
    return job


def _seed(tmp_path: Path, *, backups=None, restores=None) -> Path:
    document = {"backup_restore": {
        "backups": backups if backups is not None else [
            {"backup_id": "LAB_FULL", "server_id": "ACME-192-0-2-10", "db_type": "sqlserver",
             "active": True, "backup_dir": "/backup/lab", "note": "the lab's weekly full.",
             "jobs": [_job()]},
            {"backup_id": "LAB_LOG", "server_id": "ACME-192-0-2-10", "db_type": "sqlserver",
             "backup_dir": "/backup/lab", "jobs": [_job(job="log")]},
        ],
        "restores": restores if restores is not None else [
            {"restore_id": "LAB_TO_DRILL", "active": True, "server_id": "ACME-192-0-2-10",
             "target_server_id": "ACME-192-0-2-40", "note": "the nightly drill."},
        ],
    }}
    path = tmp_path / "restore_config.json"
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _entries(path: Path) -> dict[str, dict]:
    section = json.loads(path.read_text(encoding="utf-8"))["backup_restore"]
    return {**{e["backup_id"]: e for e in section["backups"]}, **{e["restore_id"]: e for e in section["restores"]}}


def test_entries_are_switched_off_by_their_flag_and_nothing_else_in_them_changes(tmp_path):
    path = _seed(tmp_path)
    before = _entries(path)
    answer = registration.set_entries_active(
        {"backup_ids": ["LAB_FULL", "LAB_LOG"], "restore_ids": ["LAB_TO_DRILL"], "active": False,
         "note": "Off since 2026-10-22: the VM is switched off."}, data_dir=tmp_path)
    after = _entries(path)
    assert answer["changed"] is True and answer["files_written"] == ["restore_config.json"]
    assert [item["changed"] for item in answer["items"]] == [True, True, True]
    for entry_id in ("LAB_FULL", "LAB_LOG", "LAB_TO_DRILL"):
        assert after[entry_id]["active"] is False
        unchanged = {k: v for k, v in after[entry_id].items() if k not in {"active", "note"}}
        assert unchanged == {k: v for k, v in before[entry_id].items() if k not in {"active", "note"}}
    # The note says why, after what it already said.
    assert after["LAB_FULL"]["note"] == "the lab's weekly full. Off since 2026-10-22: the VM is switched off."
    assert after["LAB_LOG"]["note"] == "Off since 2026-10-22: the VM is switched off."


def test_an_entry_already_in_that_state_is_not_rewritten(tmp_path):
    path = _seed(tmp_path)
    registration.set_entries_active({"restore_ids": ["LAB_TO_DRILL"], "active": False, "note": "off."},
                                    data_dir=tmp_path)
    before = path.read_bytes()
    answer = registration.set_entries_active({"restore_ids": ["LAB_TO_DRILL"], "active": False, "note": "off."},
                                             data_dir=tmp_path)
    assert answer["changed"] is False and answer["files_written"] == []
    assert path.read_bytes() == before


@pytest.mark.parametrize("request_, refusal", [
    ({"backup_ids": ["LAB_FULL", "NOT_THERE"], "active": False}, "backup_id NOT_THERE"),
    ({"restore_ids": ["NOT_THERE"], "active": False}, "restore_id NOT_THERE"),
    ({"backup_ids": ["LAB_FULL"], "active": "no"}, "active is required"),
    ({"active": False}, "name at least one entry"),
    ({"backup_ids": "LAB_FULL", "active": False}, "is a list of ids"),
    ({"server_id": "ACME-192-0-2-10", "active": False}, "unknown field"),
])
def test_a_request_naming_an_entry_that_is_not_there_or_no_state_writes_nothing(tmp_path, request_, refusal):
    path = _seed(tmp_path)
    before = path.read_bytes()
    with pytest.raises(registration.RegistrationError, match=refusal):
        registration.set_entries_active(request_, data_dir=tmp_path)
    assert path.read_bytes() == before


def test_an_entry_the_scheduled_app_could_not_run_is_not_switched_on(tmp_path):
    # Off, it is skipped; on, it would fail every cycle. A job with no cleanup_retention is one
    # the loader refuses - the same hold backup-add puts on a new entry.
    path = _seed(tmp_path, backups=[
        {"backup_id": "LAB_FULL", "server_id": "ACME-192-0-2-10", "db_type": "sqlserver",
         "active": False, "backup_dir": "/backup/lab",
         "jobs": [{k: v for k, v in _job().items() if k != "cleanup_retention"}]},
    ], restores=[])
    before = path.read_bytes()
    with pytest.raises(registration.RegistrationError, match="refused by the loader"):
        registration.set_entries_active({"backup_ids": ["LAB_FULL"], "active": True}, data_dir=tmp_path)
    assert path.read_bytes() == before


def test_a_backup_switched_back_on_is_scheduled_again(tmp_path):
    from dbabrain.backup_restore.backup import load_backup_jobs

    path = _seed(tmp_path, restores=[])
    registration.set_entries_active({"backup_ids": ["LAB_FULL"], "active": False}, data_dir=tmp_path)
    assert not [job for job in load_backup_jobs(path) if job.backup_id == "LAB_FULL" and job.active]
    registration.set_entries_active({"backup_ids": ["LAB_FULL"], "active": True}, data_dir=tmp_path)
    assert [job for job in load_backup_jobs(path) if job.backup_id == "LAB_FULL" and job.active]


def test_the_command_answers_in_the_envelope_and_prints_its_help_without_a_request():
    out = subprocess.run([sys.executable, "-m", "dbabrain.backup_restore.cli", "entry-active"],
                         capture_output=True, text=True, encoding="utf-8", cwd=REPO)
    assert out.returncode == 2 and "entry-active" in out.stdout and "backup_ids" in out.stdout
    out = subprocess.run([sys.executable, "-m", "dbabrain.backup_restore.cli", "entry-active",
                          json.dumps({"active": False})],
                         capture_output=True, text=True, encoding="utf-8", cwd=REPO)
    answer = json.loads(out.stdout.strip().splitlines()[-1])
    assert answer["success"] is False and "name at least one entry" in answer["message"]
