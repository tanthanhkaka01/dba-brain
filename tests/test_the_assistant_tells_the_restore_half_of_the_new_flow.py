"""The assistant tells the restore half of the new flow as it runs (0.39.0, 1.359).

The operator, 2026-11-13: *0.39 still needs [the assistant] and the CLI and Telegram commands fixed so one
backup and restore flow runs end to end in the new flow.* Read that day, the runbook's restore half
was behind the code: a repository entry the workflow refuses (share-driven, Oracle, in place) was
given as runnable; the SQL Server restore left out the vault's certificate and where the files go;
the copy said nothing of the decryption through this node or the room; the dry run was said to
report `finished: done` and to plan only what was staged; a point in time did not say it copies the
whole folder; and a chat had no way to ask for the plan.
"""

from __future__ import annotations

import copy

import pytest

from tests.test_the_ai_assistant_says_how_to_run_a_backup_or_a_restore_by_hand import (
    ENVIRONMENTS, ESTATE, NOW, TWINS, WORKER, _step)

from dbabrain.lib import backup_runbook as R

MSSQL_FROM_REPO = {
    "restore_id": "ACME_APPDB_FROM_REPO", "db_type": "sqlserver", "active": True, "server_id": "ACME-192-0-2-10",
    "target_server_id": "ACME-192-0-2-40-HOST", "target_container": "MSSQL_LAB", "backup_dir": "D:\\SQLBK\\dbops",
    "target_backup_dir": "/opt/restore/appdb_repo", "script": "assets/restore/sqlserver/mssql_restore.sh",
    "env": {"MSSQL_USER": "sa", "MSSQL_DATABASES": "APPDB"}, "cleanup_retention": 86400,
    "time_window": {"from_hour": 0, "to_hour": 23, "repeat_interval": 604800, "retry_interval": 3600, "timeout": 43200},
    "notify": {}, "action": "restore", "restore_from": "repository"}


def _runbook(entry: dict) -> dict:
    estate = copy.deepcopy(ESTATE)
    estate["backup_restore"]["restores"].append(entry)
    return R.build(estate, restore_id=entry["restore_id"], node=WORKER, twins=TWINS,
                   environments=ENVIRONMENTS, now=NOW)["restores"][0]


def _changing(runbook: dict) -> list[dict]:
    return [step for step in runbook["steps"] if step["changes"]]


def test_the_runbook_and_the_restore_read_one_list_of_engines():
    from dbabrain.backup_restore import restore_script
    from dbabrain.lib.restore import kind

    assert R.REPOSITORY_ENGINES is kind.REPOSITORY_ENGINES is restore_script.REPOSITORY_ENGINES


def test_a_sql_server_restore_from_the_repository_says_each_hop():
    runbook = _runbook(MSSQL_FROM_REPO)
    flow = {item["step"]: item["does"] for item in runbook["flow"]}
    assert "decrypted through this node" in flow["Copy"] and "DBABRAIN_SECRET_KEY" in flow["Copy"]
    assert "held to the free space before the first file" in flow["Copy"] and "opened to the engine" in flow["Copy"]
    assert "moved to the target's own data and log folders" in flow["Restore"]
    assert "take-certificate" in flow["Restore"] and "certificates lists it" in flow["Restore"]
    assert any("a point in time copies every file" in guard for guard in runbook["guards"])
    assert not any("will_be_refused" in step for step in _changing(runbook))


@pytest.mark.parametrize("change, words", [
    ({"db_type": "oracle", "script": "assets/backup/oracle/rman_restore_drill.sh"}, "oracle is not built"),
    ({"target_server_id": "ACME-192-0-2-10"}, "needs a target_server_id other than the source"),
])
def test_a_repository_entry_the_workflow_refuses_is_said_refused_before_its_lines(change, words):
    runbook = _runbook({**MSSQL_FROM_REPO, **change})
    assert all(words in step["will_be_refused"] for step in _changing(runbook) if step["gate"] == R.GATE_RESTORE)
    assert any(guard.startswith("REFUSED before anything runs") and words in guard for guard in runbook["guards"])


def test_a_share_driven_entry_reading_the_repository_is_said_refused():
    share = copy.deepcopy(next(e for e in ESTATE["backup_restore"]["restores"]
                               if e["restore_id"] == "ACME_APPDB_TO_DRILL"))
    share.update({"restore_id": "ACME_APPDB_SHARE_REPO", "restore_from": "repository"})
    runbook = _runbook(share)
    refused = [step["will_be_refused"] for step in _changing(runbook) if "will_be_refused" in step]
    assert refused and all("share-driven SQL Server entry does not read the repository" in text for text in refused)


def test_the_dry_run_is_said_as_it_is_now():
    runbook = _runbook(MSSQL_FROM_REPO)
    workflow, by_id = _step(runbook, "Dry run of the workflow"), _step(runbook, "Plan the restore (dry run)")
    for step in (workflow, by_id):
        assert "(the repository)" in step["does"] and "whether the staging folder is usable and the files fit" in step["does"]
        assert "finished: done" not in step["does"]
    assert "--dry-run" in workflow["cli"] and "no run is claimed" in workflow["does"]
    names = [step["step"] for step in runbook["steps"]]
    assert names.index("Dry run of the workflow") < names.index("Restore to the newest backup")


def test_a_chat_is_offered_the_plan_below_the_restores_level():
    runbook = _runbook(MSSQL_FROM_REPO)
    plan = runbook["telegram_plan"]
    assert plan["available"] and plan["line"] == "/spbot_restore_plan ACME_APPDB_FROM_REPO LATEST"
    assert plan["level"] < runbook["telegram"]["restore"]["level"]
    answer = R.build({"backup_restore": {"backups": [], "restores": [MSSQL_FROM_REPO]}},
                     restore_id="ACME_APPDB_FROM_REPO", node=WORKER, twins=TWINS, environments=ENVIRONMENTS, now=NOW)
    R.for_asker(answer, asker_level=2, may_run=lambda level: level <= 2)
    asked = answer["restores"][0]
    assert asked["telegram_plan"]["asker_may_run"] is True and "line" in asked["telegram_plan"]
    assert asked["telegram"]["restore"]["asker_may_run"] is False and "line" not in asked["telegram"]["restore"]
