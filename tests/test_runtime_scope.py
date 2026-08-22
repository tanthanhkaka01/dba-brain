import pytest

from db_ops.lib.time_window import TimeWindow
from db_ops.sql_tasks import runner


def command(sql_id, code):
    return runner.SqlCommand(
        sql_id=sql_id,
        sql_code=code,
        sql_name=code,
        db_type="sqlserver",
        script_type="single",
        script_path="deploy.sql",
        script_paths=(),
        script_files=("deploy.sql",),
        active=True,
    )


def target(sql_id, target_no, database_name, *, service_name="svc"):
    return runner.SqlTarget(
        sql_id=sql_id,
        target_no=target_no,
        server_id="server",
        db_type="sqlserver",
        service_name=service_name,
        instance_name="inst",
        credential_name="cred",
        time_window=TimeWindow(from_day=1, to_day=31, from_hour=0, to_hour=23, repeat_interval=1, timeout=60),
        active=True,
        database_name=database_name,
    )


def due_pairs(commands, targets):
    return runner.due_sql_tasks(
        commands=commands,
        targets=targets,
        latest_runs={},
    )


@pytest.mark.xfail(strict=True, reason="Design gap: no runtime scope overlap policy blocks same database commands.")
def test_cross_command_same_scope_blocks_second_command_when_overlap_disallowed():
    commands = {
        21: command(21, "DEPLOY-APPDB-A"),
        22: command(22, "DEPLOY-APPDB-B"),
    }
    targets = [
        target(21, 1, "APPDB"),
        target(22, 1, "APPDB"),
    ]

    due = due_pairs(commands, targets)

    assert len(due) == 1
    assert due[0][0].sql_id == 21
    assert due[0][1].database_name == "APPDB"


def test_cross_command_different_scope_can_run_in_parallel():
    commands = {
        21: command(21, "DEPLOY-APPDB"),
        22: command(22, "DEPLOY-FINANCE"),
    }
    targets = [
        target(21, 1, "APPDB"),
        target(22, 1, "FINANCE"),
    ]

    due = due_pairs(commands, targets)

    assert len(due) == 2
    assert {pair[1].database_name for pair in due} == {"APPDB", "FINANCE"}


@pytest.mark.xfail(strict=True, reason="Design gap: no ALL-scope containment rule blocks specific database commands.")
def test_partial_overlap_all_database_scope_blocks_specific_database():
    commands = {
        31: command(31, "DEPLOY-ALL"),
        32: command(32, "DEPLOY-APPDB"),
    }
    targets = [
        target(31, 1, "ALL"),
        target(32, 1, "APPDB"),
    ]

    due = due_pairs(commands, targets)

    assert len(due) == 1
    assert due[0][0].sql_id == 31


@pytest.mark.xfail(strict=True, reason="Design gap: due_sql_tasks checks exact run_key only, not nested/broader running scopes.")
def test_nested_scope_all_running_blocks_single_target():
    commands = {
        31: command(31, "DEPLOY-ALL"),
        32: command(32, "DEPLOY-APPDB"),
    }
    single = target(32, 1, "APPDB")
    latest_runs = {
        "31|1|server|sqlserver|svc|inst|ALL": {
            "status": "running",
            "started_at": "2026-01-01T11:59:00Z",
            "finished_at": None,
            "created_at": "2026-01-01T11:59:00Z",
        }
    }

    due = runner.due_sql_tasks(
        commands=commands,
        targets=[single],
        latest_runs=latest_runs,
    )

    assert due == []
